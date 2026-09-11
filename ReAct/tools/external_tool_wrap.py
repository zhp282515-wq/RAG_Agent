"""外部 MCP 工具的统一异常包装:对齐内置 with_retry / @@TOOL_ERROR@@ 协议。

背景:langchain-mcp-adapters 挂载的外部工具是 StructuredTool,只支持 async(_arun);
而本项目的 LangGraph Agent 在同步上下文(web 后台线程 + 同步流式)里调用工具,走工具的
同步 _run 入口。同时,内置工具(agent_tools)用 with_retry + tool_error_block 对模型返回
@@TOOL_ERROR@@ 结构化文本,外部工具若不包装,异常会以 langchain 原生错误文本裸奔给模型,
system_prompt 里那套「识别 @@TOOL_ERROR@@ → 按建议修正后重试一次」约定对它失效。

本模块把每个外部 MCP 工具包一层 BaseTool:
    - _run(同步入口,同步 ToolNode 会调)→ asyncio.run(_arun)
    - _arun(异步入口)→ 委托内层 _inner.ainvoke,带指数退避重试 + 异常归一
    - 瞬时错误(超时/连接/服务端忙)自动重试;确定性错误/重试耗尽 → @@TOOL_ERROR@@ 块
    - 重试期间经 agent_tools.tool_step 上报"自动重试N"子步骤(web 工作流可见)
    - 返回值可能是 content-block 列表 → 归一为纯文本给模型
"""
import asyncio
import inspect

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import PrivateAttr

from ReAct.tools.retry_util import tool_error_block, ToolRetryableError
from utils.logger_tool import logger

# 判定为「瞬时错误」的线索(MCP 传输/连接层;应用层确定性错误多不带这些词)
_RETRYABLE_HINTS = (
    "timeout", "timed out", "timedout", "connection", "connectionreset", "econnreset",
    "server busy", "unavailable", "temporarily", "refused", "broken pipe",
    "remote disconnected", "read closed", "reset by peer", " 429", " 408",
    " 5",  # 5xx
    "transport", "disconnected", "cancelled",
)

# 外部工具错误建议(面向模型/用户的中文兜底)
_GEN_SUGGESTION = "请修正参数后重试一次;若为外部服务暂时不可用,请如实告知用户,不要编造结果"
_GEN_RETRY_SUGGESTION = "该外部服务暂时不可用,已自动重试仍失败,请如实告知用户稍后再试"


def _looks_retryable(e: BaseException) -> bool:
    """按错误类型/文本判断是否瞬时(可重试)。"""
    txt = f"{type(e).__name__} {e}".lower()
    return any(k in txt for k in _RETRYABLE_HINTS)


def _coerce_text(v) -> str:
    """把 MCP 返回(可能 content-block 列表 / 裸值)归一为模型可读纯文本。"""
    if isinstance(v, list):
        parts = []
        for b in v:
            if isinstance(b, dict):
                parts.append(b.get("text", "") if b.get("type") == "text" else str(b))
            elif isinstance(b, BaseTool):  # 防御
                parts.append(str(b))
            else:
                parts.append(str(b))
        return "\n".join(p for p in parts if p) if parts else str(v)
    return v if isinstance(v, str) else str(v)


def _report_step(name: str, params: dict) -> None:
    """上报重试子步骤(web 工作流;CLI 无收集安全 no-op)。"""
    try:
        from ReAct.tools.agent_tools import tool_step
        tool_step(name, params)
    except Exception:
        pass


class ExternalToolWrapper(BaseTool):
    """把一个外部 MCP BaseTool 包成「同步可调 + 重试 + @@TOOL_ERROR@@ 归一」的工具。

    属性(仅转发内层标识/schema):
        name / description / args_schema 原样取自内层 MCP 工具,
        调用逻辑全部经 _run(同步桥) 或 _arun(重试+归一) 委托内层。
    """

    _inner: BaseTool = PrivateAttr()
    _max_attempts: int = PrivateAttr(default=3)
    _base: float = PrivateAttr(default=1.0)
    _max_backoff: float = PrivateAttr(default=8.0)
    _tool_label: str = PrivateAttr(default="")

    def __init__(
        self,
        inner: BaseTool,
        *,
        max_attempts: int = 3,
        base: float = 1.0,
        max_backoff: float = 8.0,
        tool_label: str = "",
    ):
        # 只复制标识/schema;逻辑全部走 _run/_arun,避免内层 StructuredTool 的同步 NotImplementedError
        super().__init__(
            name=getattr(inner, "name", "") or "external_tool",
            description=getattr(inner, "description", "") or "",
            args_schema=getattr(inner, "args_schema", None),
            handle_tool_error=False,  # 错误统一由本包装归一,不让 langchain 原样返回
        )
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_max_attempts", max(1, int(max_attempts)))
        object.__setattr__(self, "_base", float(base))
        object.__setattr__(self, "_max_backoff", float(max_backoff))
        object.__setattr__(self, "_tool_label", tool_label or str(getattr(inner, "name", "")))

    # ---- 同步入口:本项目 Agent 在同步上下文调用工具,走这里 ----
    def _run(self, *args, **kwargs):
        return asyncio.run(self._arun(*args, **kwargs))

    # ---- 异步入口:重试 + 归一 ----
    async def _arun(self, *args, **kwargs):
        # 归一入参:langgraph 会把入参 dict 作为 kwargs;个别路径可能传单个 dict 位置参
        tool_input = dict(kwargs)
        if args:
            if len(args) == 1 and isinstance(args[0], dict) and not tool_input:
                tool_input = dict(args[0])
        label = self._tool_label
        for attempt in range(1, self._max_attempts + 1):
            try:
                out = await self._inner.ainvoke(tool_input, config=None)
                return _coerce_text(out)
            except Exception as e:  # noqa: BLE001 归类后返回,不让异常中止 agent
                retryable = _looks_retryable(e) or isinstance(e, ToolRetryableError)
                if retryable and attempt < self._max_attempts:
                    delay = min(self._base * (2 ** (attempt - 1)), self._max_backoff)
                    logger.warning(
                        f"[ExternalToolWrapper:{label}] 第 {attempt} 次失败,{delay:.1f}s 后重试:{type(e).__name__}:{e}"
                    )
                    _report_step(f"自动重试{attempt}", {"工具": label, "原因": str(e)[:120], "等待": round(delay, 1)})
                    await asyncio.sleep(delay)
                    continue
                # 确定性错误或重试耗尽 → @@TOOL_ERROR@@ 结构化块(与内置协议一致)
                logger.error(f"[ExternalToolWrapper:{label}] 调用失败(重试后):{type(e).__name__}:{e}")
                return tool_error_block(
                    etype=type(e).__name__,
                    reason=str(e)[:200],
                    field=None,
                    suggestion=(_GEN_RETRY_SUGGESTION if retryable else _GEN_SUGGESTION),
                    is_retryable=retryable,
                    attempts=attempt if attempt > 1 else None,
                )
        return None  # 不可达,保类型收敛


def wrap_external_tool(tool: BaseTool, **kw) -> BaseTool:
    """把一个外部 MCP BaseTool 包成带重试 + @@TOOL_ERROR@@ 归一的 BaseTool。

    若传入的已是本项目包装(二次包装会丢内层)则原样返回。
    """
    if isinstance(tool, ExternalToolWrapper):
        return tool
    return ExternalToolWrapper(tool, **kw)


def describe_external_tool(tool: BaseTool) -> dict:
    """取外部工具的 schema 摘要(供注册连接探测 / 前端展示)。"""
    try:
        args_schema = getattr(tool, "args_schema", None)
        props = {}
        if args_schema is not None:
            schema = args_schema.model_json_schema() if hasattr(args_schema, "model_json_schema") else getattr(args_schema, "schema", lambda: {})()
            props = list((schema.get("properties") or {}).keys())
        return {
            "name": getattr(tool, "name", ""),
            "description": (getattr(tool, "description", "") or "")[:200],
            "args": props,
        }
    except Exception:
        return {"name": getattr(tool, "name", ""), "description": "", "args": []}
