"""改写器:单次 LLM 调用同时完成 改写 + 自检 + 决策。

三条实现约束来自需求:
  1. **只调一次** LLM。改写和自检塞进同一个 JSON 输出,不做第二次往返。
  2. **轻量模型**。默认取 model.yml 的 summary_model(qwen3.7-flash),
     不回退对话主模型 —— 改写不需要同档能力。
  3. **失败必须降级**。无 key / 超时 / JSON 解析不出来,一律返回 use_original,
     绝不能把异常抛给检索链路(那等于整轮问答失败)。

历史只喂最近 N 轮且有字符预算:指代消解需要的那点上下文,几轮就够;
把全量历史塞进去既费 token 又拖延迟,还会让模型在长上下文里"自由发挥"。
"""
from __future__ import annotations

import json
import re

from utils.logger_tool import logger
from utils.query_rewrite import config
from utils.query_rewrite.models import (
    DECISION_ASK_CLARIFY,
    DECISION_USE_ORIGINAL,
    PreservedConstraints,
    RewriteResult,
    SelfCheck,
    VALID_DECISIONS,
)
from utils.query_rewrite.prompts import build_system_prompt
from utils.resilience import Kind, call, register_exception_kind

# 历史消息在 prompt 里的渲染上限,防止单条超长消息吃光预算
_PER_MSG_CHARS = 300

# 内部调用的来源标记:写进 langchain config 的 metadata,供 agent 流式层识别并跳过
# (改写发生在工具内部,其 token 流绝不能被当成回答推给前端)。
LC_SOURCE = "query_rewrite"


class _RewriteAttemptError(ValueError):
    """本次尝试没能产出可用的改写结果(调用失败 / 输出不可解析 / 缺关键字段)。

    继承 ValueError 是为了兼容既有调用与测试 —— 此前解析失败抛的就是 ValueError
    (见 tests/test_query_rewrite.py 的 TestRewriterParsing),语义上「模型给的输出
    内容不合法」也确实是 ValueError。

    单独一个类型并在下方注册给统一分类器,理由有二:
      1. 降级函数能说清「是调用挂了还是模型吐了坏 JSON」,日志可读;
      2. **重试语义显式化** —— 原实现把「调用失败」与「JSON 解析失败」一律重试一次;
         若放任它走默认分类,不可解析的 JSON 会被判成「未知」而不重试,行为就悄悄变了。
         注册成 TRANSIENT 把老行为固定下来。

    注意:这不属需求里「格式非法且无法修复 → 不重试」那一类。那条指的是重发也永远
    一样的场景;而模型偶尔吐坏 JSON 是随机的,重发一次确实有机会拿到好结果 ——
    原项目及其测试都依赖这一点(test_bad_json_degrades_after_retry:"应恰好重试一次")。
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# 注册后再 import 分类器,避免模块级循环依赖
register_exception_kind(_RewriteAttemptError, Kind.TRANSIENT, code="rewrite_attempt")


def _format_history(history) -> str:
    """把最近若干轮历史渲染成紧凑文本(超预算从头截断,保留最近的)。

    history 接受两种形态:
      - [{"role": "user"/"assistant", "content": str}, ...]
      - [(role, content), ...]
    """
    if not history:
        return ""
    items: list[tuple[str, str]] = []
    for h in history:
        if isinstance(h, dict):
            role = str(h.get("role") or h.get("type") or "user")
            content = h.get("content")
        elif isinstance(h, (tuple, list)) and len(h) >= 2:
            role, content = str(h[0]), h[1]
        else:
            continue
        # 多模态 content(列表)只取文本块:改写不需要图片
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text", "")) for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        text = str(content or "").strip()[:_PER_MSG_CHARS]
        if text:
            items.append(("助手" if role in ("assistant", "ai") else "用户", text))

    turns = int(config.get("history_turns") or 3)
    # 「轮」按用户消息数算:取最近 turns 条用户消息及其之后的助手回复
    if turns > 0:
        user_idx = [i for i, (r, _) in enumerate(items) if r == "用户"]
        if len(user_idx) > turns:
            items = items[user_idx[-turns]:]

    lines = "\n".join(f"{r}:{t}" for r, t in items)
    budget = int(config.get("history_char_budget") or 1200)
    if len(lines) > budget:
        # 从尾部切,保留最近的内容(指代消解依赖最近的上文)
        lines = lines[-budget:]
        # 切点可能落在某行中间,去掉残缺的首行,避免给模型半句话
        if "\n" in lines:
            lines = lines.split("\n", 1)[1]
    return lines


def _extract_json(text: str) -> dict | None:
    """从模型输出里取出 JSON 对象。

    即便用 json_object 模式,也做一层容错:模型偶尔会包一层 ```json 代码块,
    或在 JSON 前后带一句解释。策略是「先直接解析,再退到第一个 { 到最后一个 }」。
    """
    if not text:
        return None
    s = text.strip()
    # 去掉可能的 markdown 围栏
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s.strip())
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(s[i:j + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _parse_result(original: str, raw: str) -> RewriteResult:
    """把模型输出解析成 RewriteResult。解析不出来抛 _RewriteAttemptError(可重试)。"""
    obj = _extract_json(raw)
    if obj is None:
        raise _RewriteAttemptError("模型输出不是可解析的 JSON")

    rewritten = str(obj.get("rewritten") or "").strip()
    if not rewritten:
        # rewritten 缺失是致命的:没有它就没有检索串
        raise _RewriteAttemptError("模型输出缺少 rewritten 字段")

    changes = obj.get("changes")
    if not isinstance(changes, (list, tuple)):
        changes = []
    changes = [str(c).strip() for c in changes if str(c).strip()]

    decision = str(obj.get("decision") or "").strip()
    if decision not in VALID_DECISIONS:
        # 模型给了个不认识的 decision:不猜,直接按最保守的 use_original
        logger.warning(f"query_rewrite.rewriter: 未知 decision {decision!r},按 use_original 处理")
        decision = DECISION_USE_ORIGINAL

    return RewriteResult(
        original=original,
        rewritten=rewritten,
        changed=bool(obj.get("changed")),
        changes=changes,
        constraints=PreservedConstraints.from_dict(obj.get("preserved_constraints")),
        self_check=SelfCheck.from_dict(obj.get("self_check")),
        decision=decision,
        reason=str(obj.get("reason") or "").strip(),
    )


def _report_usage(resp) -> None:
    """把改写调用的 token 消耗并入本轮统计。

    记在 "summary" 桶:agent.py 的 _turn_usage 已把 summary 计入总量,
    复用现成口径比新增桶更省事(且改写与上下文压缩同属"辅助小模型调用")。
    非工具调用期间 usage_add 是空操作,故评测/自测场景不会污染统计。
    """
    try:
        from ReAct.middleware.agent_middleware import usage_add
        um = getattr(resp, "usage_metadata", None) or {}
        total = int(um.get("total_tokens", 0) or 0)
        if total:
            usage_add("summary", total)
    except Exception:
        pass


class Rewriter:
    """把用户问题改写成检索友好表述,并给出自检与决策。

    用法:
        r = Rewriter()
        result = r.rewrite("那它续航多久", history=msgs)
        # result.decision / result.rewritten / result.self_check ...

    延迟与成本:一次 LLM 调用,温度 0,输出封顶 max_tokens。
    """

    def __init__(self, model=None):
        """model: 可注入的 LangChain chat model(测试时传假模型)。None 则懒建。"""
        self._model = model
        self._build_failed = ""

    # ---------- 模型构建(懒建 + 失败缓存) ----------

    def _get_model(self):
        """取改写模型。构建失败(缺 key / base_url 无效)返回 None 并记住原因。

        失败结果缓存下来,避免每次提问都重试一遍建模型(建模型会读 DB 取 key)。
        """
        if self._model is not None:
            return self._model
        if self._build_failed:
            return None
        try:
            from model.modelfactory import get_chat_model
            name = config.rewrite_model_name()
            temperature = float(config.get("temperature") or 0)
            base_url = (config.get("base_url") or "").strip() or None
            # 关掉思维链:改写是机械任务,思考纯属浪费(实测 18.4s→3.1s,且不再因
            # reasoning token 吃光 max_tokens 而失败)。必须在**构造期**传,放 invoke
            # 的 config 里 DashScope 会忽略(见 modelfactory.get_chat_model 的说明)。
            extra_body = None
            if config.get_bool("disable_thinking"):
                extra_body = {"enable_thinking": False}
            self._model = get_chat_model(
                name, temperature=temperature, extra_body=extra_body,
            )
            if base_url:
                # 显式 base_url:只在配置里给了才覆盖,否则沿用 modelfactory 的环境变量
                try:
                    self._model = self._model.bind(base_url=base_url)
                except Exception as e:
                    logger.warning(f"query_rewrite.rewriter: base_url 绑定失败,忽略:{e}")
            thinking = "已关闭" if extra_body else "开启"
            logger.info(
                f"query_rewrite.rewriter: 改写模型就绪({name}, temperature={temperature},"
                f" 思维链{thinking})"
            )
            return self._model
        except Exception as e:
            self._build_failed = str(e)
            logger.warning(f"query_rewrite.rewriter: 构建改写模型失败,改写将降级为原问题:{e}")
            return None

    # ---------- 主流程 ----------

    def rewrite(self, query: str, history=None) -> RewriteResult:
        """执行一次改写。任何失败都返回 RewriteResult.fallback(...),不抛异常。

        重试/退避/日志/指标全部交给 utils.resilience:调用点只负责给出「降级函数」
        (回退原问题)和调用点标识(rewriter)。缓存刻意关闭 —— 决策缓存已由
        pipeline 层按 query 做了 LRU,这里再缓存一层会带来两份状态、互相说不清。
        """
        original = (query or "").strip()
        if not original:
            return RewriteResult.fallback(original, "空问题")

        model = self._get_model()
        if model is None:
            return RewriteResult.fallback(original, self._build_failed or "改写模型不可用")

        system_prompt = build_system_prompt()
        hist = _format_history(history)
        user_block = f"{hist}\n\n用户问题:{original}" if hist else f"用户问题:{original}"

        timeout = float(config.get("timeout") or 15)
        max_tokens = int(config.get("max_tokens") or 1500)

        # 先把 LLM 原始调用与「解析成 RewriteResult」两件事合成一个可调用对象:
        # 调用失败与 JSON 解析失败都应当走同一条重试逻辑,但 model 是本次闭包内的
        # 局部状态,直接在 call() 里传参会让策略驱动去关心业务参数。
        def _invoke_and_parse() -> RewriteResult:
            raw = self._invoke(model, system_prompt, user_block, timeout, max_tokens)
            if raw is None:
                # 抛异常而不是返回 None:让统一驱动把它当成一次失败去重试/降级
                raise _RewriteAttemptError("调用改写模型失败或返回空")
            return _parse_result(original, raw)

        def _fallback(exc: BaseException, attempts: int) -> RewriteResult:
            reason = exc.message if isinstance(exc, _RewriteAttemptError) else str(exc)
            logger.warning(
                f"query_rewrite.rewriter: 改写不可用(已尝试 {attempts} 次,{type(exc).__name__}):{reason}"
            )
            return RewriteResult.fallback(original, reason)

        result = call(
            _invoke_and_parse,
            site="rewriter",
            fallback=_fallback,
            # 交互式链路(检索在等这个结果),退避要短;次数由 resilience.yml 的
            # sites.rewriter 决定(当前 1 次重试),这里不再写死。
        )
        if not result.rewriter_failed:
            logger.info(
                f"【查询改写】original={original!r} → rewritten={result.rewritten!r} "
                f"changed={result.changed} decision={result.decision} "
                f"confidence={result.self_check.confidence:.2f}"
            )
            if result.reason:
                logger.debug(f"【查询改写】理由:{result.reason}")
        return result

    def _invoke(self, model, system_prompt: str, user_block: str, timeout: float,
                max_tokens: int) -> str | None:
        """发一次调用,返回原始文本;超时/异常返回 None(由上层转成失败异常)。

        这里刻意仍返回 None 而不是抛异常:模型构建/绑定失败等「调用前」的问题
        与「调用中」的网络问题在这一层不好区分,统一由上层决定重试与否。
        """
        try:
            m = model
            # 结构化输出:json_object 模式让 provider 保证返回合法 JSON
            try:
                m = m.bind(response_format={"type": "json_object"})
            except Exception:
                pass  # 不支持就退回普通模式,_extract_json 会兜住
            try:
                m = m.bind(max_tokens=max_tokens)
            except Exception:
                pass
            # with_config(timeout=...) 让 SDK 层拦截悬挂请求,避免拖死检索链路
            try:
                m = m.with_config(timeout=timeout)
            except Exception:
                pass

            from langchain_core.messages import HumanMessage, SystemMessage
            resp = m.invoke(
                [SystemMessage(content=system_prompt), HumanMessage(content=user_block)],
                # callbacks=[] 是本模块最容易被"顺手简化"掉的一行,动它前先读这段:
                # 本方法在**工具执行期间**被调用,而工具跑在 agent 的 RunnableConfig
                # 上下文里(langchain_core.runnables.config.ensure_config 会从
                # var_child_runnable_config 继承配置)。不显式清空 callbacks,这次改写
                # 就会继承 agent 推流用的回调 —— 改写的流式 token 会被 stream_mode=
                # "messages" 当成回答推给前端并落库,用户会看到一整段改写 JSON 夹在
                # 回答里(已在真实环境复现过)。
                # metadata.lc_source 给 agent 流式层留一个可识别的标记,作为第二道防线
                # (见 ReAct/ReAct_Agent/agent.py 的 _INTERNAL_SOURCES)。
                # 代价:这次调用不再上报 LangSmith 等外部 tracing;token 用量仍由
                # _report_usage 手动统计,计费口径不受影响。
                config={"callbacks": [], "metadata": {"lc_source": LC_SOURCE}},
            )
            _report_usage(resp)
            text = getattr(resp, "text", None)
            if text is None:
                content = getattr(resp, "content", "")
                text = content if isinstance(content, str) else str(content)
            return text
        except Exception as e:
            # 归一成异常交给统一驱动分类:超时/连接/5xx 会被判为瞬时并重试,
            # 鉴权/参数类会被判为确定性并直接降级。
            logger.warning(f"query_rewrite.rewriter: 调用改写模型异常({type(e).__name__}):{e}")
            raise

    # ---------- 说明:ask_clarify 的合理性校验在 hard_checker 里做 ----------
    # (glossary.clarify_is_plausible) 放在规则层而非本类:它判的是「模型是否
    # 答非所问」,属于规则复核范畴,且这样 rewriter 无需被 hard_checker 反向依赖。


# 澄清追问的默认文案:工具无法直接与用户对话,只能把追问交回主模型转述
def clarify_message(original: str, reason: str = "") -> str:
    """构造 ask_clarify 分支的追问文案(不检索,直接返回给主模型)。"""
    tip = "该问题缺少足够信息,无法确定检索目标。"
    asked = f"请先向用户确认具体想问什么(例如机型、功能、场景),不要凭猜测回答。"
    parts = [f"【需要澄清】{tip}"]
    if reason:
        parts.append(f"判断依据:{reason}")
    parts.append(asked)
    return "\n".join(parts)


__all__ = [
    "Rewriter",
    "clarify_message",
    "LC_SOURCE",
    "DECISION_ASK_CLARIFY",
    "DECISION_USE_ORIGINAL",
]
