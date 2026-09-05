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
    """server.py 用于注入 context 的收集器:{events, sources, report, meta}。"""
    return {"events": [], "sources": None, "report": False, "meta": {}, "_ph": 0}


def _next_ph(holder: dict | None) -> int | None:
    """为阶段分配请求内自增 id(用于把 phase_end/substep 精确归属到某个阶段)。"""
    if holder is None:
        return None
    holder["_ph"] = holder.get("_ph", 0) + 1
    return holder["_ph"]


def tool_step_emit(name: str, params: dict | None = None, duration: float | None = None) -> None:
    """工具内部调用:向当前请求上报一条细化子步骤(无 web 收集时为空操作)。"""
    em = getattr(_tool_step_emitter, "emit", None)
    if em is None:
        return
    em(name, params, duration)


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


def _trace_of(runtime) -> dict | None:
    ctx = getattr(runtime, "context", None)
    return ctx.get(TRACE_KEY) if isinstance(ctx, dict) else None


def trace_emit(runtime, step: str, action: str = "phase",
               name: str | None = None, duration: float | None = None,
               params: dict | None = None, ph_id: int | None = None) -> None:
    """向当前请求的 trace 收集器追加一条事件;未开启收集(CLI)时为空操作。

    ph_id: 阶段 id(phase_start 分配);phase_end/substep 带同一 id 供前端精确配对,
           避免同名阶段(如多轮"检索"或并行工具)时子步骤错挂。
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

        def _emit_substep(name, params=None, duration=None):
            if holder is not None:
                holder["events"].append({
                    "step": parent_phase, "action": "substep",
                    "ph": ph_id,          # 精确挂到本阶段,而非"最近未闭合"
                    "name": name, "params": params or {},
                    "duration": round(duration, 2) if duration is not None else None,
                })

        _set_tool_step_emitter(_emit_substep)
        try:
            res = handler(request)
        finally:
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

        trace_emit(request.runtime, parent_phase, "phase_end",
                   tool_name, dur, end_params or None, ph_id=ph_id)
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
        raise e


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
    if t is not None and t.get("_model_t0"):
        trace_emit(runtime, "模型", "phase_end", None, _time.perf_counter() - t["_model_t0"])
        t["_model_t0"] = None
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
