"""工具调用失败处理:(面向模型的)结构化失败块 + 统一重试适配层。

本模块现在只承担两件事,重试/退避/熔断/日志/指标全部下沉到 `utils.resilience`:

1. **展示层**:`tool_error_block()` 生成 `@@TOOL_ERROR@@` 结构化文本块,供 ReAct 提示词
   解析。为什么留在工具目录而不是放进公共组件:它是**给模型看的展示格式**,与
   「失败该怎么处理」是两回事 —— 换个前端(比如改成 JSON 事件)就该改这里,而公共组件
   不该为一个提示词的约定服务。

2. **适配层**:`with_retry` / `http_get_json` 保持**原有签名与行为**,内部转调公共组件。
   `agent_tools.py` 的调用方式因此零改动,但退避、分类、熔断、指标全部统一了。

设计约定(与 prompts 中 @@TOOL_ERROR@@ 解析指引配套):
- 瞬时错误(网络抖动/下游 5xx/429/408/超时/连不上):由公共组件做指数退避+随机抖动
  重试,耗尽后返回「服务暂时不可用」结构化块,让模型如实告知用户,不机械重复同一请求。
- 确定性错误(参数格式错/城市无法定位/IP定位失败/数据不存在):不重试,直接返回
  结构化失败块(见 tool_error_block),让 ReAct Agent 据「建议」修正参数、补充前提、
  换工具或调整计划。
- 本模块异常(除被组件消化外)不向外抛出导致 agent 整轮中止。
"""

import functools
from typing import Any, Callable

import requests

from utils.logger_tool import logger
from utils.resilience import (
    Kind,
    call as _call,
    classify,
    deterministic_status_codes as _deterministic_status_codes,
)
from utils.resilience.observe import log_event


class ToolRetryableError(Exception):
    """瞬时错误:应重试。"""


class ToolDeterministicError(Exception):
    """确定性错误:不重试,由调用方转为结构化失败块。"""


# ---------------- 重试预算(保留常量名:agent_tools / 其它模块可能引用) ----------------
RETRY_MAX_ATTEMPTS = 4        # 1 次初始 + 3 次重试
RETRY_BASE_SECONDS = 1.0      # 首次重试等待;之后按 2 倍递增,封顶
RETRY_MAX_BACKOFF = 8.0
RETRY_JITTER_FRAC = 0.5       # 抖动 = uniform(0, wait * frac),避免下游打爆

# 分类表来自公共组件 —— 刻意做成别名而不是再抄一份,否则两处判定会慢慢漂移。
# 4xx 里属「业务可判定、重发无用」的确定性状态码 → ToolDeterministicError;
# 429/408/5xx/连接/超时 → 可能是瞬时 → ToolRetryableError。
_DETERMINISTIC_STATUS = _deterministic_status_codes()


# ---------------- 确定性失败:结构化文本块 ----------------
# 首行 marker 便于模型/前端一眼识别;后续为「字段: 值」行,模型可直接据「建议」行动。
ERROR_MARK = "@@TOOL_ERROR@@"


def tool_error_block(
    *,
    etype: str,
    reason: str,
    field: str | None = None,
    suggestion: str | None = None,
    is_retryable: bool = False,
    attempts: int | None = None,
) -> str:
    """生成给模型看的确定性失败文本块(标准格式,见 prompts 解析指引)。"""
    lines = [
        ERROR_MARK,
        f"错误类型: {etype}",
        f"是否可重试: {'是' if is_retryable else '否'}",
    ]
    if field:
        lines.append(f"失败字段: {field}")
    lines.append(f"具体原因: {reason}")
    if attempts is not None:
        lines.append(f"重试次数: {attempts}")
    if suggestion:
        lines.append(f"建议: {suggestion}")
    return "\n".join(lines)


# ---------------- HTTP 请求:统一超时 + 失败分类 ----------------
def _classify_http(e: BaseException) -> Any:
    """按失败性质把 HTTP/传输异常映射成两个哨兵异常。

    2xx 但响应体非 JSON(截断/代理页)也判为瞬时,可重试 —— 组件默认就是这么判的
    (json.JSONDecodeError → 瞬时),这里保持一致。
    """
    c = classify(e)
    if c.kind is Kind.TRANSIENT:
        return ToolRetryableError(f"HTTP {c.status}" if c.status else str(e) or c.code)
    if c.kind is Kind.DETERMINISTIC:
        return ToolDeterministicError(f"HTTP {c.status}" if c.status else str(e) or c.code)
    return ToolDeterministicError(f"HTTP {c.status}" if c.status else str(e) or c.code)


def http_get_json(
    url: str,
    *,
    params: dict | None = None,
    timeout: int = 10,
    headers: dict | None = None,
) -> dict:
    """GET 请求并解析 JSON;按失败性质抛 ToolRetryableError / ToolDeterministicError。

    2xx 但响应体非 JSON(截断/代理页)视为瞬时,可重试。
    """
    try:
        resp = requests.get(url, params=params, timeout=timeout, headers=headers)
    except (requests.Timeout, requests.ConnectionError) as e:
        raise ToolRetryableError(f"请求超时或连接失败:{e}") from e
    if resp.status_code in _DETERMINISTIC_STATUS:
        raise ToolDeterministicError(f"HTTP {resp.status_code}")
    if resp.status_code in {408, 429} or resp.status_code >= 500:
        raise ToolRetryableError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise ToolRetryableError(f"响应非 JSON(status={resp.status_code})") from e


# ---------------- 重试:转调公共组件 ----------------
def _report_retry(attempt: int, _exc, wait: float, _classified) -> None:
    """每次重试前上报子步骤(web 工作流可见;CLI 安全 no-op)。"""
    try:
        from ReAct.tools.agent_tools import tool_step
        tool_step(f"自动重试{attempt}", {"原因": _exc, "等待": round(wait, 1)})
    except Exception:
        pass  # 子步骤上报失败不影响重试主流程


def with_retry(
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    *,
    tool_name: str = "",
    base: float = RETRY_BASE_SECONDS,
    max_backoff: float = RETRY_MAX_BACKOFF,
    jitter_frac: float = RETRY_JITTER_FRAC,
) -> Callable:
    """只对瞬时错误重试;耗尽后返回「服务暂时不可用」块而非抛异常。

    用于包在 @tool 内部:@tool 仍是外层,with_retry 用 functools.wraps 保住
    __name__/__doc__/__signature__,langchain 工具 schema/introspection 不变。

    签名与行为与接入公共组件前保持一致(max_attempts 是「总尝试次数」,不是重试次数),
    内部改由 utils.resilience.call 统一处理退避、熔断、日志与指标。
    """
    retries = max(0, int(max_attempts) - 1)

    def _decorate(fn: Callable) -> Callable:
        site = tool_name or getattr(fn, "__name__", "tool")

        def _fallback(exc: BaseException, attempts: int) -> str:
            """重试耗尽 → 给模型一个可据以行动的结构化块(不抛异常)。"""
            # 确定性错误不会走到这里(组件不重试它,直接降级),但降级函数对两种情况都要成立
            is_retryable = isinstance(exc, ToolRetryableError) or classify(exc).kind is Kind.TRANSIENT
            if is_retryable:
                logger.error(f"[with_retry] {site} 重试 {attempts} 次后仍失败:{exc}")
                return tool_error_block(
                    etype="服务暂时不可用",
                    reason=str(exc),
                    is_retryable=True,
                    attempts=attempts,
                    suggestion="当前外部服务暂时不可用,请告知用户稍后重试,或改用其他工具/调整提问",
                )
            logger.error(f"[with_retry] {site} 确定性失败:{exc}")
            return tool_error_block(
                etype=type(exc).__name__,
                reason=str(exc),
                is_retryable=False,
                attempts=attempts if attempts > 1 else None,
                suggestion="请修正参数后重试一次;若为外部服务暂时不可用,请如实告知用户,不要编造结果",
            )

        @functools.wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            return _call(
                fn, *args,
                site=site,
                fallback=_fallback,
                retries=retries,
                base=base,
                max_delay=max_backoff,
                jitter_ratio=jitter_frac,
                on_retry=_report_retry,
                **kwargs,
            )

        return _wrapper

    return _decorate


__all__ = [
    "ToolRetryableError",
    "ToolDeterministicError",
    "tool_error_block",
    "http_get_json",
    "with_retry",
    "ERROR_MARK",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_BASE_SECONDS",
    "RETRY_MAX_BACKOFF",
    "RETRY_JITTER_FRAC",
]
