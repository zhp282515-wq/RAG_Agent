import os.path
import os
import csv
import re
from langchain_core.tools import tool
from vector_store.vector_store import VectorStoreService, VectorSearchError
from utils.logger_tool import logger
from datetime import datetime
from utils.path_tool import get_abs_path
from utils.config_tool import agent_conf
from ReAct.tools.retry_util import (
    with_retry,
    http_get_json,
    tool_error_block,
    ToolRetryableError,
    ToolDeterministicError,
)
external_data = {}


def tool_step(name: str, params: dict | None = None, tokens: int | None = None) -> None:
    """工具内部上报细化子步骤(供 web 工作流面板;CLI 无 web 收集时为空操作)。"""
    try:
        from ReAct.middleware.agent_middleware import tool_step_emit
        tool_step_emit(name, params, tokens=tokens)
    except Exception:
        pass


# 检索资料单条正文最大字符数(超过则截断,但图片描述段完整保留)
_MAX_CHUNK_CHARS = 300

# 相关度分级(实测校准):高 ≥0.85 直接引用;中 0.60~0.85 可参考;<0.60 不进 context
# 运行时可由「系统配置」页改(存 MySQL app_settings),读 DB 失败回退以下常量
_SCORE_HIGH = 0.85
_SCORE_MIN = 0.60
_TOP_K = 20
_RERANK_N = 5

# sources 推送的正文预览最大字符数(前端溯源记录面板展示用)
_SOURCE_TEXT_CHARS = 500


def _setting(name: str, default):
    """从 MySQL app_settings 现读一项全局设置;失败/未初始化回退 default。"""
    try:
        from utils.settings_store import get_json
        return get_json(name, default)
    except Exception:
        return default


def _score_high() -> float:
    return float(_setting("retrieval.score_high", _SCORE_HIGH))


def _score_min() -> float:
    return float(_setting("retrieval.score_min", _SCORE_MIN))


def _top_k() -> int:
    return int(_setting("retrieval.top_k", _TOP_K))


def _rerank_n() -> int:
    return int(_setting("retrieval.rerank_n", _RERANK_N))


def _current_store() -> str | None:
    """当前向量库名(系统配置页可切);读失败回退 None 由服务层取 yml 默认。

    None 而非空串:VectorStoreService 对空值会回退默认库,交给它统一处理。
    """
    v = str(_setting("rag.current_store", "") or "").strip()
    return v or None

# 月份归一化:接受 YYYY-MM / YYYY-M,并容忍完整日期尾缀 -dd(get_current_month 实返 %Y-%m-%d)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})(?:-\d{1,2})?$")


def _normalize_month(month: str) -> str | None:
    """把月份参数统一为 YYYY-MM;非法格式/越界返回 None。"""
    s = (month or "").strip()
    m = _MONTH_RE.match(s)
    if not m:
        return None
    y, mo = m.group(1), int(m.group(2))
    if not 1 <= mo <= 12:
        return None
    return f"{y}-{mo:02d}"


def _score_label(score: float, high: float | None = None) -> str:
    """相关度分数 → 等级标签(高/中)。high 缺省读运行时设置。"""
    if score >= (high if high is not None else _score_high()):
        return "高"
    return "中"


def _record_sources(hits: list[dict]) -> None:
    """把本轮结构化命中写入中间件线程本地(monitor_tool 转交请求级收集器)。

    未开启 web 收集(CLI 调用)时为空操作;开启后工具每轮执行都会覆盖,
    server 拿到的始终是最近一次检索的完整来源。
    """
    try:
        from ReAct.middleware.agent_middleware import sources_set
        sources_set([
            {
                "file_name": h.get("file_name", ""),
                "score": round(float(h.get("score", 0)), 3),
                "label": _score_label(float(h.get("score", 0))),
                "page": h.get("page"),
                "chapter": h.get("chapter", "") or "",
                "section": h.get("section", "") or "",
                "text": (h.get("text", "") or "")[:_SOURCE_TEXT_CHARS],
            }
            for h in hits
        ])
    except Exception as e:
        logger.debug(f"【工具执行】[get_rerank_retriever] 记录来源失败(忽略):{e}")


@tool(description="根据问题在向量知识库中检索扫地/扫拖机器人相关资料,返回带来源与相关度等级的资料片段列表;资料正文中可能包含===[本页图片 N]===开头的图片文字描述")
@with_retry(max_attempts=3, tool_name="get_rerank_retriever")
def get_rerank_retriever(query: str) -> str:
    """从向量知识库检索资料(向量粗排+rerank 精排),格式化为给模型看的紧凑文本。

    只返回相关度 ≥0.60 的资料;每条标注相关度等级(高/中)。

    Args:
        query: 检索词,须为第一步改写后的专业关键词/句,而非原始用户问题。
    """
    try:
        logger.debug(f"【工具执行】[get_rerank_retriever] 检索词:{query}")
        # 注册子步骤回调:向量召回/精排/过滤 发生时经中间件上报细化事件
        def _step(name, info=None, tokens=None):
            tool_step(name, info, tokens=tokens)
        # 每次检索现读全局设置(系统配置页可改)→「下次提问/下次检索即生效」
        score_high = _score_high()
        score_min = _score_min()
        top_k = _top_k()
        rerank_n = _rerank_n()
        # raise_on_infra_error:基础设施故障(向量化/Milvus)上抛 VectorSearchError,
        # 由下方转为 ToolRetryableError 进入重试;真「无相关文档」仍正常返回 []
        # 检索目标库由「系统配置」的当前向量库决定(现读,切换后下次检索即生效)
        hits = VectorStoreService(collection_name=_current_store()).get_rerank_retriever(
            top_k=top_k, rerank_n=rerank_n,
            on_step=_step, raise_on_infra_error=True,
        )(query)
        if not hits:
            _record_sources([])
            _step("达标过滤", {"说明": "无候选命中"})
            logger.warning(f"【工具执行】[get_rerank_retriever] 未检索到相关资料")
            return "未检索到相关资料"

        relevant = [h for h in hits if h.get("score", 0) >= score_min]
        dropped = len(hits) - len(relevant)
        if dropped:
            logger.debug(f"【工具执行】[get_rerank_retriever] 过滤 {dropped} 条低相关(<{score_min})")
            _step("达标过滤", {"通过": len(relevant), "剔除低相关": dropped, "阈值": score_min})
        else:
            _step("达标过滤", {"通过": len(relevant), "阈值": score_min})
        # 结构化来源同步写入中间件线程本地,web 层据此推送 sources 事件(无命中时写空列表)
        _record_sources(relevant)
        if not relevant:
            logger.warning(f"【工具执行】[get_rerank_retriever] 无相关度达标资料(全部<{score_min})")
            return f"未检索到相关资料(所有候选相关度均低于 {score_min})"

        parts = []
        for h in relevant:
            text = h.get("text", "")
            if len(text) > _MAX_CHUNK_CHARS:
                head = text[:_MAX_CHUNK_CHARS]
                # 若截断点切进了图片描述段(===开头但未闭合),截断点退到该段之前,保证图片描述完整;
                # 若图片段从正文开头开始(img_pos==0),直接截断即可,否则退到 0 会把整段截成空
                img_pos = head.rfind("===[本页图片")
                if img_pos > 0 and "====================" not in head[img_pos:]:
                    text = head[:img_pos].rstrip()
                else:
                    text = head
            score = h.get("score", 0)
            parts.append(
                f"【来源:{h.get('file_name', '')} | 相关度:{_score_label(score, score_high)}({score:.3f})】\n{text}"
            )
        logger.debug(f"【工具执行】[get_rerank_retriever] 命中 {len(parts)} 条资料")
        return "\n\n".join(parts)
    except VectorSearchError as e:
        logger.error(f"【工具执行】[get_rerank_retriever] 检索基础设施故障:{e}")
        raise ToolRetryableError(str(e)) from e
    except Exception as e:
        logger.error(f"【工具执行】[get_rerank_retriever] 检索失败:{e}")
        return "检索失败,请稍后重试"


@tool(description="获取指定城市的天气，以消息字符串的形式返回")
@with_retry(tool_name="get_weather")
def get_weather(city: str) -> str:
    try:
        tool_step("定位城市", {"城市": city})
        # HTTP 由 http_get_json 统一超时并按失败性质分类:瞬时(超时/5xx)在
        # with_retry 内重试;确定性(HTTP 4xx 业务错误)在此转为结构化失败块
        try:
            geo_data = http_get_json(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "zh"},
                timeout=10,
            )
        except ToolDeterministicError as e:
            return tool_error_block(
                etype="城市查询失败", reason=str(e),
                field=f'city="{city}"',
                suggestion="确认城市名写法后重试,或询问用户标准城市名",
            )

        if "results" not in geo_data or not geo_data["results"]:
            return tool_error_block(
                etype="城市无法定位", reason="未查询到该城市",
                field=f'city="{city}"',
                suggestion="改用更标准的城市名(拼音/带市后缀/英文),或询问用户确认城市",
            )

        result = geo_data["results"][0]
        lat = result["latitude"]
        lon = result["longitude"]
        city_name = result["name"]
        tool_step("城市已定位", {"位置": f"{city_name}({lat:.2f}, {lon:.2f})"})

        try:
            weather_data = http_get_json(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m",
                },
                timeout=10,
            )
        except ToolDeterministicError as e:
            return tool_error_block(
                etype="天气查询失败", reason=str(e),
                field=f'city="{city}"',
                suggestion="确认城市名写法后重试,或询问用户标准城市名",
            )

        current = weather_data["current"]

        wmo_codes = {
            0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴天",
            45: "雾", 48: "雾凇", 51: "小毛毛雨", 53: "中毛毛雨", 55: "大毛毛雨",
            61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨(小)", 67: "冻雨(大)",
            71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
            80: "小阵雨", 81: "中阵雨", 82: "大阵雨",
            85: "小阵雪", 86: "大阵雪",
            95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴大冰雹",
        }

        weather_text = wmo_codes.get(current["weather_code"], f"未知({current['weather_code']})")
        tool_step("获取当前天气", {"天气": f"{city_name} {weather_text} {current['temperature_2m']}℃"})

    except Exception as e:
        logger.error(f"【工具执行】[get_weather] 天气查询失败：{e}")
        return "无法获取天气"
    return (
        f"城市：{city_name}，天气：{weather_text}，"
        f"温度：{current['temperature_2m']}℃，体感温度：{current['apparent_temperature']}℃，"
        f"风速：{current['wind_speed_10m']}km/h，风向：{current['wind_direction_10m']}°，"
        f"相对湿度：{current['relative_humidity_2m']}%"
    )


@tool(description="获取用户所在城市的名称，以纯字符串形式返回")
@with_retry(max_attempts=3, tool_name="get_user_location")
def get_user_location() -> str:
    """IP 自动定位(ipwho.is) + 中文城市名回查(open-meteo geocode)。

    IP 定位失败属「暂时无法自动定位」的确定性业务失败(重试 IP 服务也无济于事),
    返回结构化失败块,建议模型请用户手动提供所在城市;网络瞬时错误由 with_retry 重试。
    """
    try:
        tool_step("IP 定位", {"来源": "ipwho.is"})
        resp = http_get_json("https://ipwho.is/", timeout=5)
        if not resp.get("success") or not resp.get("city"):
            return tool_error_block(
                etype="IP定位失败", reason="IP 归属地服务未能返回城市信息",
                suggestion="无法自动定位,请用户手动提供所在城市",
            )
        city_en = resp["city"]
        country = resp.get("country", "")

        geo_resp = http_get_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city_en, "count": 1, "language": "zh"},
            timeout=10,
        )
        if "results" in geo_resp and geo_resp["results"]:
            cn = geo_resp["results"][0]["name"]
            tool_step("城市已识别", {"位置": f"{cn}({country})"})
            return cn
        tool_step("城市已识别", {"位置": "定位失败,返回未知"})
    except ToolDeterministicError as e:
        logger.warning(f"【工具执行】[get_user_location] IP定位失败:{e}")
    except Exception as e:
        logger.warning(f"【工具执行】[get_user_location] IP定位失败:{e}")
    return "未知城市"


@tool(description="获取当前月份，以纯字符串形式返回")
def get_current_month() -> str:
    return datetime.now().strftime("%Y-%m-%d")


@tool(description="获取用户id，以纯字符串形式返回")
def get_user_id(user_id: int) -> str:

    try:
        user_data_path = get_abs_path(agent_conf["user_data_path"])

        # 判断当前路径是否存在
        if not os.path.exists(user_data_path):
            logger.warning(f"【工具执行】[get_user_id] 用户数据路径不存在：{user_data_path}")
            return "用户数据路径不存在"

        # 取出csv文件中的用户id(只取得用户id)
        with open(user_data_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            ids = list({row['用户ID'] for row in reader})

        for id in ids:
            if int(id) == user_id:
                return f"当前用户id：{user_id}"

    except Exception as e:
        logger.error(f"【工具执行】[get_user_id] 获取用户id失败：{e}")
    return f"用户{user_id}不存在"


def get_external_data():
    """
    {
        "user_id": {
            "month": {"特征": xxx, "效率": xxx}
            "month": {"特征": xxx, "效率": xxx}
            "month": {"特征": xxx, "效率": xxx}
            ...
        },
        "user_id": {
            "month": {"特征": xxx, "效率": xxx}
            "month": {"特征": xxx, "效率": xxx}
            "month": {"特征": xxx, "效率": xxx}
            ...
        },
        "user_id": {
            "month": {"特征": xxx, "效率": xxx}
            "month": {"特征": xxx, "效率": xxx}
            "month": {"特征": xxx, "效率": xxx}
            ...
        },
        ...
    }
    """

    if not external_data:
        external_data_path = get_abs_path(agent_conf["user_data_path"])

        if not os.path.exists(external_data_path):
            logger.warning(f"【工具执行】[get_external_data] 外部数据路径不存在：{external_data_path}")

        with open(external_data_path, 'r', encoding='utf-8') as f:
            for line in f.readlines()[1:]:
                line: list[str] = line.strip().split(",")

                user_id: str = line[0].replace('"', '')
                feature: str = line[1].replace('"', '')
                efficiency: str = line[2].replace('"', '')
                consumables: str = line[3].replace('"', '')
                comparison: str = line[4].replace('"', '')
                month: str = line[5].replace('"', '')

                if user_id not in external_data:
                    external_data[user_id] = {}
                external_data[user_id][month] = {
                    "特征": feature,
                    "效率": efficiency,
                    "消耗": consumables,
                    "对比": comparison
                }


@tool(description="从外部系统获取指定用户在指定月份的使用记录，以纯字符串形式返回，若该月无记录则返回结构化失败提示")
def fetch_external_data(user_id: str, month: str) -> str:
    """查询某用户某月外部数据。

    月份须为 YYYY-MM(容忍完整日期尾缀);参数格式非法或数据缺失均返回
    @@TOOL_ERROR@@ 结构化块,让模型修正参数/确认后重试,而不是拿到空串无从判断。
    """
    get_external_data()

    norm = _normalize_month(month)
    if norm is None:
        return tool_error_block(
            etype="参数格式错误", reason=f"月份格式无法解析或越界",
            field=f'month="{month}"',
            suggestion="调用 get_current_month 获取当前月份,或向用户确认要查询的月份(YYYY-MM)",
        )

    try:
        return external_data[user_id][norm]
    except KeyError:
        logger.warning(f"【工具执行】[fetch_external_data] 未能检索到用户{user_id}在{norm}的使用记录")
        return tool_error_block(
            etype="数据不存在", reason="该用户该月无记录",
            field=f"user_id={user_id} month={norm}",
            suggestion="确认用户ID是否正确,或向用户询问其他要查询的月份(YYYY-MM)",
        )


@tool(description="无入参，无返回值，调用后触发中间件自动为报告生成的场景动态注入上下文信息，为后续提示词切换提供上下文信息")
def fill_context_for_report():
    return "fill_context_for_report已调用"


if __name__ == '__main__':
    print(get_weather(get_user_location()))
