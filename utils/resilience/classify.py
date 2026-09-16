"""错误分类器:把任意异常判成「瞬时(可重试)」还是「确定性(不重试)」。

分类原则(需求原文):**重试可能改变结果的 → 瞬时;重试结果一样的 → 确定性。**

判定顺序(先命中先返回):
  1. 调用点自行注册的规则(register_exception_kind)—— 优先级最高,让调用点能覆盖核心
  2. 本项目工具协议的两个哨兵异常(ToolRetryableError / ToolDeterministicError)
  3. openai SDK 的类型层级(最精确,优先于状态码猜)
  4. 标准库传输层异常(timeout / connection / DNS / JSON 截断)
  5. requests / urllib 的异常
  6. 任何带 status_code / code 的异常 → 按状态码判
  7. 文本特征兜底(中文/英文关键词)

**UNKNOWN 不重试**:与项目原有契约一致(`with_retry` 只重试 ToolRetryableError,
`_looks_retryable` 匹配不上也不重试)。把「不认识」当「可重试」会放大未知故障。

阈值都在 config/resilience.yml,这里不写死。
"""
from __future__ import annotations

import enum
import json
import socket
import threading
from dataclasses import dataclass


class Kind(str, enum.Enum):
    """错误性质。STR 枚举便于直接进日志/JSON。"""

    TRANSIENT = "transient"            # 瞬时:重试可能改变结果
    DETERMINISTIC = "deterministic"    # 确定性:重试结果一样,直接降级
    UNKNOWN = "unknown"                # 判不出来:按不重试处理


# ---- 状态码表 ----
# 确定性:重发一次结果一样。402 = 余额不足,属业务拒绝,重试无用且会白烧配额。
_DETERMINISTIC_STATUS = frozenset({
    400,  # 参数错
    401,  # 未鉴权
    402,  # 余额不足 / 需付费
    403,  # 无权限
    404,  # 资源不存在
    405,  # 方法不允许
    406,  # 不接受
    409,  # 冲突
    410,  # 已删除
    413,  # 请求体过大
    414,  # URI 过长
    415,  # 媒体类型不支持
    422,  # 语义校验失败
    451,  # 因法律原因不可用
})

# 瞬时:明确该重试的 4xx。5xx 单独按区间判(500<=st<600),免得漏掉 507/508 之类。
_TRANSIENT_STATUS = frozenset({
    408,  # 请求超时
    425,  # Too Early
    429,  # 限流
})

# 这些状态码属于「配置/代码问题」,要告警:重试无用,得有人去看
_ALERT_STATUS = frozenset({401, 402, 403, 404})


# ---- 文本特征 ----
# 传输层:应当重试
_TRANSPORT_HINTS = (
    "timeout", "timed out", "timedout", "connection", "connection reset",
    "econnreset", "econnrefused", "refused", "broken pipe", "reset by peer",
    "unavailable", "temporarily", "remote disconnected", "read closed",
    "transport", "disconnected", "server busy", "eof occurred", "incomplete read",
    "连接", "超时", "网络", "暂时不可用",
)

# 业务/配置拒绝:不该重试
_BUSINESS_HINTS = (
    "insufficient balance", "arrearage", "quota exhausted", "exceeded your quota",
    "invalid api key", "incorrect api key", "invalid_api_key", "no such model",
    "model not found", "not found", "unsupported", "invalid_request_error",
    "余额不足", "欠费", "配额", "无效的api", "模型不存在",
)

# 内容审核:确定性,重试还是被拒
_MODERATION_HINTS = (
    "content filter", "content_filter", "content policy", "moderation",
    "safecheck", "data_inspection", "datainspection", "risk_control",
    "内容审核", "审核不通过", "违规",
)

# 限流:瞬时
_RATE_HINTS = (
    "rate limit", "rate_limit", "too many requests", "requests per",
    "tpm", "rpm", "限流", "请求过于频繁",
)


@dataclass(frozen=True)
class Classified:
    """分类结果。code 是稳定标识,供指标按错误码归类。"""

    kind: Kind
    code: str                              # "http_429" / "timeout" / "auth" / ...
    status: int | None = None
    retry_after: float | None = None       # 秒;429/503 可能带
    alert: bool = False                    # 是否属「配置/代码问题」需告警
    detail: str = ""


# ---------------------------------------------------------------- 状态码提取

def _status_of(e: BaseException) -> int | None:
    """从三种异常形态里挖出 HTTP 状态码。

    - openai.APIStatusError.status_code
    - requests.HTTPError.response.status_code / httpx.Response.status_code
    - urllib.error.HTTPError.code
    """
    sc = getattr(e, "status_code", None)
    if isinstance(sc, int):
        return sc
    resp = getattr(e, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if isinstance(sc, int):
            return sc
    code = getattr(e, "code", None)
    if isinstance(code, int):
        return code
    return None


def _headers_of(e: BaseException):
    resp = getattr(e, "response", None)
    h = getattr(resp, "headers", None)
    if h is not None:
        return h
    return getattr(e, "headers", None)


def _retry_after(e: BaseException) -> float | None:
    """读 Retry-After。支持 delta-seconds 与 HTTP-date 两种形式。

    429 常见,但 503 也允许带 —— 两处都读,上游让等就别自作主张。
    """
    headers = _headers_of(e)
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    # HTTP-date 形式
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(str(raw))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


# ---------------------------------------------------------------- 按状态/文本判

def _by_status(e: BaseException) -> Classified:
    st = _status_of(e)
    if st is None:
        return Classified(Kind.UNKNOWN, "unknown", detail=type(e).__name__)
    if st in _DETERMINISTIC_STATUS:
        return Classified(
            Kind.DETERMINISTIC, f"http_{st}", st,
            alert=(st in _ALERT_STATUS),
            detail=_short(e),
        )
    if st in _TRANSIENT_STATUS or 500 <= st < 600:
        return Classified(Kind.TRANSIENT, f"http_{st}", st, _retry_after(e), detail=_short(e))
    # 2xx/3xx 带异常、或未收录的 4xx:不认识的当不重试
    return Classified(Kind.UNKNOWN, f"http_{st}", st, detail=_short(e))


def _by_text(e: BaseException) -> Classified:
    txt = f"{type(e).__name__} {e}".lower()
    if any(k in txt for k in _MODERATION_HINTS):
        return Classified(Kind.DETERMINISTIC, "moderation", detail=_short(e))
    if any(k in txt for k in _BUSINESS_HINTS):
        return Classified(Kind.DETERMINISTIC, "business_refusal", alert=True, detail=_short(e))
    if any(k in txt for k in _RATE_HINTS):
        return Classified(Kind.TRANSIENT, "http_429", 429, _retry_after(e), detail=_short(e))
    if any(k in txt for k in _TRANSPORT_HINTS):
        return Classified(Kind.TRANSIENT, "transport", detail=_short(e))
    return Classified(Kind.UNKNOWN, "unknown", detail=type(e).__name__)


def _short(e: BaseException) -> str:
    s = str(e).strip().replace("\n", " ")
    return s[:200]


# ---------------------------------------------------------------- 注册表

_reg_lock = threading.Lock()
# (matcher, kind, code, alert, priority)。matcher 为异常类或谓词。
_registrations: list[tuple] = []


def register_exception_kind(matcher, kind: Kind, *, code: str = "", alert: bool = False,
                            priority: int = 100) -> None:
    """注册一条自定义分类规则,优先于所有内置规则。

    matcher:异常类(子类判定)或谓词 callable(e) -> bool。
    priority:数字越小越先判(默认 100);同优先级按注册顺序。
    调用点借此扩展分类,无需改本文件 —— 例如 MCP 传输层的特征词。
    """
    if not isinstance(kind, Kind):
        kind = Kind(kind)
    with _reg_lock:
        _registrations.append((matcher, kind, code, alert, int(priority)))


def clear_registrations() -> None:
    """清空全部自定义规则。

    注意:模块级注册(如 rewriter.py、external_tool_wrap.py 在 import 时注册的)
    会被一并清掉且不会自动恢复。**测试做隔离时请用 snapshot/restore**,
    否则会把别的模块的注册一起抹掉,造成「单独跑能过、全量跑就挂」的假故障。
    """
    with _reg_lock:
        _registrations.clear()


def snapshot_registrations() -> list:
    """取当前注册表的快照(测试隔离用:先快照,用完 restore 回去)。"""
    with _reg_lock:
        return list(_registrations)


def restore_registrations(snapshot: list) -> None:
    """把注册表恢复到某个快照。"""
    with _reg_lock:
        _registrations[:] = list(snapshot)


def _matches(matcher, e: BaseException) -> bool:
    if isinstance(matcher, type):
        return isinstance(e, matcher)
    try:
        return bool(matcher(e))
    except Exception:
        return False


def _registered(e: BaseException) -> Classified | None:
    with _reg_lock:
        regs = sorted(_registrations, key=lambda r: r[4])
    for matcher, kind, code, alert, _prio in regs:
        if _matches(matcher, e):
            return Classified(kind, code or f"registered_{kind.value}", alert=alert, detail=_short(e))
    return None


# ---------------------------------------------------------------- 依赖的惰性加载

def _retry_util():
    """惰性拿工具侧的两个哨兵异常。

    必须惰性:ReAct.tools.retry_util 反过来要用本模块做分类,顶层 import 会成环。
    项目里已有这个惯例(modelfactory / summarization_mw 都用函数内 import 破环)。
    """
    try:
        from ReAct.tools import retry_util
        return retry_util
    except Exception:
        return None


def _openai():
    try:
        import openai
        return openai
    except Exception:
        return None


def _requests():
    try:
        import requests
        return requests
    except Exception:
        return None


def _urllib_error():
    try:
        from urllib import error as _e
        return _e
    except Exception:
        return None


# ---------------------------------------------------------------- 主入口

def classify(e: BaseException) -> Classified:
    """把异常判成瞬时 / 确定性 / 未知。绝不抛异常(分类器自己不能成为故障点)。"""
    try:
        return _classify(e)
    except Exception as inner:  # 分类器内部的意外:按未知处理,不重试
        return Classified(Kind.UNKNOWN, "classify_error", detail=f"{type(inner).__name__}: {inner}")


def _classify(e: BaseException) -> Classified:
    # 1) 调用点注册的规则最优先
    hit = _registered(e)
    if hit is not None:
        return hit

    # 2) 本项目工具协议哨兵
    ru = _retry_util()
    if ru is not None:
        det = getattr(ru, "ToolDeterministicError", None)
        ret = getattr(ru, "ToolRetryableError", None)
        if det is not None and isinstance(e, det):
            return Classified(Kind.DETERMINISTIC, "tool_deterministic", alert=True, detail=_short(e))
        if ret is not None and isinstance(e, ret):
            return Classified(Kind.TRANSIENT, "tool_retryable", detail=_short(e))

    # 3) openai SDK 类型层级(精确,优于状态码猜)
    oa = _openai()
    if oa is not None:
        def _is(name: str) -> bool:
            cls = getattr(oa, name, None)
            return cls is not None and isinstance(e, cls)

        if _is("APITimeoutError"):
            return Classified(Kind.TRANSIENT, "timeout", detail=_short(e))
        if _is("APIConnectionError"):
            return Classified(Kind.TRANSIENT, "connection", detail=_short(e))
        if _is("AuthenticationError"):
            return Classified(Kind.DETERMINISTIC, "auth", 401, alert=True, detail=_short(e))
        if _is("PermissionDeniedError"):
            return Classified(Kind.DETERMINISTIC, "forbidden", 403, alert=True, detail=_short(e))
        if _is("NotFoundError"):
            return Classified(Kind.DETERMINISTIC, "not_found", 404, alert=True, detail=_short(e))
        if _is("BadRequestError"):
            return Classified(Kind.DETERMINISTIC, "bad_request", 400, detail=_short(e))
        if _is("UnprocessableEntityError"):
            return Classified(Kind.DETERMINISTIC, "validation", 422, detail=_short(e))
        if _is("RateLimitError"):
            return Classified(Kind.TRANSIENT, "http_429", 429, _retry_after(e), detail=_short(e))
        if _is("InternalServerError"):
            return Classified(Kind.TRANSIENT, "http_5xx", _status_of(e), _retry_after(e), detail=_short(e))
        if _is("ContentFilterFinishReasonError"):
            return Classified(Kind.DETERMINISTIC, "moderation", detail=_short(e))
        # 兜底:任何 APIStatusError 按状态码;其余 APIError 走文本
        if _is("APIStatusError") and _status_of(e) is not None:
            return _by_status(e)
        if _is("APIError"):
            c = _by_text(e)
            if c.kind is Kind.UNKNOWN:
                c = Classified(Kind.UNKNOWN, "openai_api_error", detail=_short(e))
            return c

    # 4) 标准库传输层
    if isinstance(e, (TimeoutError, socket.timeout)):
        return Classified(Kind.TRANSIENT, "timeout", detail=_short(e))
    if isinstance(e, socket.gaierror):
        return Classified(Kind.TRANSIENT, "dns", detail=_short(e))
    if isinstance(e, ConnectionError):
        return Classified(Kind.TRANSIENT, "connection", detail=_short(e))
    if isinstance(e, json.JSONDecodeError):
        # 响应体截断 / 非 JSON(代理页、半截流):重发有机会拿到完整内容
        return Classified(Kind.TRANSIENT, "json_decode", detail=_short(e))

    # 5) requests / urllib
    rq = _requests()
    if rq is not None:
        if isinstance(e, rq.Timeout):
            return Classified(Kind.TRANSIENT, "timeout", detail=_short(e))
        if isinstance(e, rq.ConnectionError):
            return Classified(Kind.TRANSIENT, "connection", detail=_short(e))
        if isinstance(e, rq.HTTPError):
            return _by_status(e)
    ur = _urllib_error()
    if ur is not None:
        if isinstance(e, ur.HTTPError):
            return _by_status(e)
        if isinstance(e, ur.URLError):
            reason = getattr(e, "reason", "")
            if isinstance(reason, socket.gaierror):
                return Classified(Kind.TRANSIENT, "dns", detail=_short(e))
            return Classified(Kind.TRANSIENT, "connection", detail=f"{_short(e)} | {reason}")

    # 6) 任何能拿出状态码的异常
    if _status_of(e) is not None:
        return _by_status(e)

    # 7) 文本兜底
    return _by_text(e)


def deterministic_status_codes() -> frozenset[int]:
    """确定性状态码表(对外暴露,供 retry_util 复用,避免两张表各写一份而漂移)。"""
    return _DETERMINISTIC_STATUS


def transient_status_codes() -> frozenset[int]:
    return _TRANSIENT_STATUS


def is_transient(e: BaseException) -> bool:
    return classify(e).kind is Kind.TRANSIENT
