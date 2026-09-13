from typing import Callable, Any
import threading
import time as _time
from langchain.agents.middleware import wrap_tool_call, before_model, after_model, dynamic_prompt, ModelRequest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from utils.logger_tool import logger
from langchain.agents import AgentState
from langgraph.runtime import Runtime
from utils.prompts_tool import get_prompt
from utils.path_tool import get_abs_path
from ReAct.tools.retry_util import tool_error_block


# ---------------- web 事件收集 ----------------
# server.py 在 stream_output 的 context 里放入 {"_web_trace": holder},本模块各中间件
# 在执行阶段把 trace 事件/来源/报告元信息写入该 holder(与运行时 context 同生命周期,
# 按请求隔离,与 generator 迭代线程无关)。agent_tools 因拿不到 runtime,通过线程本地
# sources_set/sources_take 与 monitor_tool 交接(工具执行与 monitor_tool 同线程)。
TRACE_KEY = "_web_trace"

_tool_sources = threading.local()
# 工具执行期间的"子步骤发射器"(由 monitor_tool 在 handler() 前后设置/清除,
# 供工具内部无 runtime 访问权限时上报细化步骤)
_tool_step_emitter = threading.local()


def new_trace_holder() -> dict:
    """server.py 用于注入 context 的收集器:{events, sources, report, meta, usage}。

    usage: 本轮 token 用量聚合(模型调用累加 + embedding/rerank 工具消耗),
           以及 _usage_v 版本号(供 SSE 侧增量推送,见 server.drain_ctx)。
    """
    return {"events": [], "sources": None, "report": False, "meta": {}, "_ph": 0,
            "usage": {}, "_usage_v": 0}


def _next_ph(holder: dict | None) -> int | None:
    """为阶段分配请求内自增 id(用于把 phase_end/substep 精确归属到某个阶段)。"""
    if holder is None:
        return None
    holder["_ph"] = holder.get("_ph", 0) + 1
    return holder["_ph"]


def tool_step_emit(name: str, params: dict | None = None, duration: float | None = None,
                   tokens: int | None = None) -> None:
    """工具内部调用:向当前请求上报一条细化子步骤(无 web 收集时为空操作)。"""
    em = getattr(_tool_step_emitter, "emit", None)
    if em is None:
        return
    em(name, params, duration, tokens)


def _set_tool_step_emitter(emit) -> None:
    """monitor_tool 在 handler 前后设置当前线程的发射器。"""
    _tool_step_emitter.emit = emit


def _clear_tool_step_emitter() -> None:
    try:
        _tool_step_emitter.emit = None
    except Exception:
        pass


def sources_set(sources: list[dict]) -> None:
    """agent_tools 写入本轮结构化命中(线程本地)。"""
    _tool_sources.sources = sources


def sources_take() -> list[dict] | None:
    """monitor_tool 取走 agent_tools 写入的命中;None=本轮工具未写入。"""
    s = getattr(_tool_sources, "sources", None)
    _tool_sources.sources = None
    return s


# ---------------- 工具内的 token 用量累加 ----------------
# embedding / rerank 这类工具内部调用拿不到 runtime,无法直接写 trace_holder,
# 故经线程本地累加器转交(与 sources_set/take 同一套交接思路)。
# 只在工具执行期间(monitor_tool 的 handler 前后)累加,工具外(文档入库、调试检索)
# 自动是空操作,不会污染会话统计。
_tool_usage = threading.local()


def usage_active(on: bool) -> None:
    """monitor_tool 在 handler 前后开关:只有工具执行期间的用量才计入。"""
    _tool_usage.active = bool(on)


def usage_add(kind: str, tokens: int) -> None:
    """累加某类工具的 token 消耗(embedding / rerank)。工具外调用为空操作。"""
    try:
        if not getattr(_tool_usage, "active", False) or tokens <= 0:
            return
        total = getattr(_tool_usage, "total", None)
        if total is None:
            total = _tool_usage.total = {}
            _tool_usage.since = {}
        total[kind] = total.get(kind, 0) + int(tokens)
        _tool_usage.since[kind] = _tool_usage.since.get(kind, 0) + int(tokens)
    except Exception:
        pass


def usage_take_since(kind: str) -> int:
    """取走自上次调用以来某类工具的累计消耗,并把游标归零(供分步归因)。"""
    since = getattr(_tool_usage, "since", None)
    if not since:
        return 0
    v = int(since.get(kind, 0))
    since[kind] = 0
    return v


def usage_total() -> dict:
    """本轮工具消耗总计(kind -> tokens)。"""
    return dict(getattr(_tool_usage, "total", None) or {})


def usage_reset() -> None:
    """清空累加器(每轮 stream_output 开始时调用,避免跨轮串味)。"""
    _tool_usage.total = {}
    _tool_usage.since = {}
    _tool_usage.active = False


def _trace_of(runtime) -> dict | None:
    ctx = getattr(runtime, "context", None)
    return ctx.get(TRACE_KEY) if isinstance(ctx, dict) else None


def trace_emit(runtime, step: str, action: str = "phase",
               name: str | None = None, duration: float | None = None,
               params: dict | None = None, ph_id: int | None = None,
               tokens: int | None = None) -> None:
    """向当前请求的 trace 收集器追加一条事件;未开启收集(CLI)时为空操作。

    ph_id: 阶段 id(phase_start 分配);phase_end/substep 带同一 id 供前端精确配对,
           避免同名阶段(如多轮"检索"或并行工具)时子步骤错挂。
    tokens: 本步骤的 token 消耗(模型调用的 usage、检索的 embedding+rerank),
            前端按 k 展示;无可用计量时留空。
    """
    t = _trace_of(runtime)
    if t is None:
        return
    ev: dict = {"step": step, "action": action}
    if ph_id is not None:
        ev["ph"] = ph_id
    if name:
        ev["name"] = name
    if duration is not None:
        ev["duration"] = round(duration, 2)
    if params:
        ev["params"] = params
    if tokens is not None:
        ev["tokens"] = int(tokens)
    t["events"].append(ev)


def _display_phase(tool_name: str) -> str:
    """工具名 → 用户可见的阶段名(检索工具独立成「检索」阶段)。"""
    if tool_name == "get_rerank_retriever":
        return "检索"
    if tool_name == "fill_context_for_report":
        return "报告上下文"
    return tool_name


def _extract_user_id(text: str) -> str | None:
    """从文本里提取用户 id(records.csv 用户ID均为数字):
    优先 `用户ID 1001` / `id 1001` 模式,其次独立数字段。"""
    import re
    m = re.search(r"(?:用户\s*id|user\s*id|用户编号)[\s:：=]*(\d+)", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b(?:id|编号)[\s:：=]*(\d{2,})", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(\d{4,10})(?!\d)", text)
    return m.group(1) if m else None


def _extract_month(text: str) -> str | None:
    """从文本里提取 YYYY-MM(2026-08 / 2026年8月)格式月份。"""
    import re
    m = re.search(r"(\d{4})\s*[-年/]\s*(\d{1,2})\s*(?:月)?", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


@wrap_tool_call
def monitor_tool(
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command]
) -> ToolMessage | Command:
    tool_name = request.tool_call["name"]
    logger.info(f"【中间件执行】[monitor_tool] 执行工具:{tool_name}")
    logger.info(f"【中间件执行】[monitor_tool] 传入参数:{request.tool_call['args']}")

    # 阶段开始事件:检索带改写后检索词,报告带用户id/月份参数
    start_params: dict = {}
    args = request.tool_call.get("args") or {}
    if tool_name == "get_rerank_retriever":
        q = str(args.get("query", "") or "").strip()
        start_params = {"query": q}
    elif tool_name == "fill_context_for_report":
        uid = str(args.get("user_id", "") or "").strip()
        month = str(args.get("month", "") or "").strip()
        start_params = {}
        if uid:
            start_params["用户"] = uid
        if month:
            start_params["月份"] = month

    t0 = _time.perf_counter()
    try:
        # 工具执行期间开通子步骤发射:工具内部调 tool_step_emit() 上报细化过程
        parent_phase = _display_phase(tool_name)
        holder = _trace_of(request.runtime)
        # 分配唯一阶段 id → phase_end/substep 都带上它,前端按 id 精确归属
        ph_id = _next_ph(holder) if holder is not None else None
        trace_emit(request.runtime, parent_phase, "phase_start",
                   tool_name, None, start_params or None, ph_id=ph_id)

        def _emit_substep(name, params=None, duration=None, tokens=None):
            if holder is not None:
                ev = {
                    "step": parent_phase, "action": "substep",
                    "ph": ph_id,          # 精确挂到本阶段,而非"最近未闭合"
                    "name": name, "params": params or {},
                    "duration": round(duration, 2) if duration is not None else None,
                }
                if tokens:
                    ev["tokens"] = int(tokens)
                holder["events"].append(ev)

        _set_tool_step_emitter(_emit_substep)
        usage_active(True)
        try:
            res = handler(request)
        finally:
            usage_active(False)
            _clear_tool_step_emitter()
        dur = _time.perf_counter() - t0

        # 检索工具:命中数作为阶段参数,并收集来源
        end_params: dict = {}
        if tool_name == "get_rerank_retriever":
            t = _trace_of(request.runtime)
            srcs = sources_take()
            if t is not None and srcs is not None:
                t["sources"] = srcs
                end_params = {"命中": len(srcs)}

        # 工具自身消耗的 token(embedding / rerank):作为本阶段 tokens 上报。
        # 同时把分类累计写进 holder —— 累加器是线程本地的,而读取方(web 的
        # drain_ctx)跑在另一个线程,只有落到 holder 才能被它看到。
        tool_tokens = 0
        totals = usage_total()
        for kind, val in totals.items():
            tool_tokens += int(val or 0)
        t = _trace_of(request.runtime)
        if t is not None and totals:
            u = t.setdefault("usage", {})
            for kind, val in totals.items():
                u[kind] = int(val or 0)
            t["_usage_v"] = t.get("_usage_v", 0) + 1
        trace_emit(request.runtime, parent_phase, "phase_end",
                   tool_name, dur, end_params or None, ph_id=ph_id,
                   tokens=tool_tokens or None)
        logger.info(f"【中间件执行】[monitor_tool] 工具{tool_name}执行成功")

        t = _trace_of(request.runtime)
        if tool_name == "fill_context_for_report":
            request.runtime.context["report"] = True
            if t is not None:
                t["report"] = True
                # 报告元信息:优先工具参数,其次本轮用户消息文本里出现的 id/月份
                uid = str(args.get("user_id", "") or "").strip() or None
                month = str(args.get("month", "") or "").strip() or None
                if not uid or not month:
                    user_text = t["meta"].get("last_user_text", "")
                    uid = uid or _extract_user_id(user_text)
                    month = month or _extract_month(user_text)
                if uid:
                    t["meta"]["user_id"] = uid
                if month:
                    t["meta"]["month"] = month
            trace_emit(request.runtime, "报告场景", "phase", "fill_context_for_report")
        return res

    except Exception as e:
        logger.error(f"【中间件执行】[monitor_tool] 工具{tool_name}执行异常:{str(e)}")
        # 工具栈内 with_retry 会把瞬时错误消化为返回,泄漏到这里的多为未归类意外异常;
        # 补发 phase_end(status=失败)闭合工作流阶段,并转成 ToolMessage 让模型正常应答,
        # 避免整轮中止或前端留下悬挂阶段
        trace_emit(request.runtime, parent_phase, "phase_end", tool_name,
                   _time.perf_counter() - t0,
                   {"status": "失败", "error": _content_preview(str(e), 80)},
                   ph_id=ph_id)
        return ToolMessage(
            content=tool_error_block(
                etype="工具执行异常", reason=str(e),
                suggestion="请如实告知用户该步骤失败,不要编造结果",
            ),
            tool_call_id=request.tool_call["id"],
        )


def _content_preview(content, limit: int | None = None) -> str:
    """把消息内容转成可读文本供日志打印。

    content 可能是纯文本(str),也可能是多模态块列表(list[dict],如图片 image_url);
    这里把多模态块折叠成紧凑描述,避免 logger 里直接 .strip() 在 list 上崩溃。
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "?")
                if btype == "image_url":
                    parts.append("[图片]")
                else:
                    parts.append(str(block.get("text", "")).strip())
            else:
                parts.append(str(block))
        text = " ".join(p for p in parts if p)
    else:
        text = str(content)
    text = text.strip()
    if limit is not None and len(text) > limit:
        text = text[:limit] + "..."
    return text


@before_model
def log_befor_model(
        state: AgentState,          # agent状态记录
        runtime: Runtime            # 上下文信息
):
    trace_emit(runtime, "模型", "phase_start", None, None, {"消息": len(state["messages"])})
    t = _trace_of(runtime)
    if t is not None:
        t["_model_t0"] = _time.perf_counter()
        # 记录本轮用户消息文本(报告元信息提取兜底用)
        for m in reversed(state["messages"]):
            if getattr(m, "type", "") == "human":
                t["meta"]["last_user_text"] = _content_preview(m.content)
                break
    logger.info(f"【中间件执行】[log_befor_model] 即将调用模型:带有{len(state['messages'])}条消息")
    logger.debug(f"【中间件执行】[log_befor_model] {type(state['messages'][-1]).__name__} | 消息内容如下:\n{_content_preview(state['messages'][-1].content)}")

    return None


@after_model
def log_after_model(
        state: AgentState,  # agent状态记录
        runtime: Runtime
):
    t = _trace_of(runtime)
    # 本次模型调用的 token 用量:state 末条是组装好的 AIMessage,带 usage_metadata。
    # after_model 每次调用恰好触发一次(而非每个流式分片),故不会重复计数。
    last = state["messages"][-1] if state.get("messages") else None
    um = getattr(last, "usage_metadata", None) or {}
    inp = int(um.get("input_tokens", 0) or 0)
    out = int(um.get("output_tokens", 0) or 0)
    tot = int(um.get("total_tokens", 0) or 0) or (inp + out)
    reason = int((um.get("output_token_details") or {}).get("reasoning", 0) or 0)

    if t is not None and t.get("_model_t0"):
        trace_emit(runtime, "模型", "phase_end", None,
                   _time.perf_counter() - t["_model_t0"],
                   {"输入": inp, "输出": out, "推理": reason, "合计": tot} if tot else None,
                   tokens=tot or None)
        t["_model_t0"] = None
        if tot:
            u = t.setdefault("usage", {})
            u["model_in"] = u.get("model_in", 0) + inp
            u["model_out"] = u.get("model_out", 0) + out
            u["model_reason"] = u.get("model_reason", 0) + reason
            u["model_total"] = u.get("model_total", 0) + tot
            u["calls"] = u.get("calls", 0) + 1
            # 上下文体积 = 本次调用的输入量(前端进度条口径)
            u["context"] = inp
            t["_usage_v"] = t.get("_usage_v", 0) + 1
    logger.info(f"【中间件执行】[log_after_model] 模型调用完成 带有{len(state['messages'])}条消息")
    logger.debug(f"【中间件执行】[log_after_model] {type(state['messages'][-1]).__name__} | 消息内容如下:\n{_content_preview(state['messages'][-1].content, limit=100)}")
    return None


@dynamic_prompt
def report_prompt_model(request: ModelRequest):
    """报告场景切换为报告提示词;普通场景沿用 create_agent 传入的静态系统提示词。"""
    context = getattr(request.runtime, "context", None) or {}
    is_report = context.get("report", False)

    if is_report:
        logger.info("【中间件执行】[report_prompt_model] 切换为报告提示词")
        return get_prompt(get_abs_path("prompts\\report_prompt.md"))

    if request.system_message is not None:
        return request.system_message
    return None


if __name__ == '__main__':
    print(get_prompt(get_abs_path("prompts\\system_prompt.md")))
    # print(get_prompt(get_abs_path("prompts\\report_prompt.txt")))
