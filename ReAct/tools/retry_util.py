"""工具调用失败处理:瞬时错误重试 + 确定性错误结构化反馈。

设计约定(与 prompts 中 @@TOOL_ERROR@@ 解析指引配套):
- 瞬时错误(网络抖动/下游 5xx/429/408/超时/连不上):工具栈内用指数退避+随机抖动
  重试(见 with_retry),耗尽后返回「服务暂时不可用」结构化块,让模型如实告知用户,
  不机械重复同一请求。
- 确定性错误(参数格式错/城市无法定位/IP定位失败/数据不存在):不重试,直接返回
  结构化失败块(见 tool_error_block),让 ReAct Agent 据「建议」修正参数、补充前提、
  换工具或调整计划。
- 本模块异常(除被 with_retry 消化外)不向外抛出导致 agent 整轮中止。
"""

import functools
import random
import time
from typing import Any, Callable

import requests

from utils.logger_tool import logger


class ToolRetryableError(Exception):
    """瞬时错误:应重试。"""


class ToolDeterministicError(Exception):
    """确定性错误:不重试,由调用方转为结构化失败块。"""


# ---------------- 重试预算 ----------------
RETRY_MAX_ATTEMPTS = 4        # 1 次初始 + 3 次重试
RETRY_BASE_SECONDS = 1.0      # 等待 = base * 2**(attempt-1),封顶
RETRY_MAX_BACKOFF = 8.0
RETRY_JITTER_FRAC = 0.5       # 抖动 = uniform(0, wait * frac),避免下游打爆


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
# 4xx 里属「业务可判定、重发无用」的确定性状态码 → ToolDeterministicError;
# 429/408/5xx/连接/超时 → 可能是瞬时 → ToolRetryableError。
_DETERMINISTIC_STATUS = {400, 401, 403, 404, 405, 409, 410, 422}


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


# ---------------- 指数退避 + 随机抖动 重试装饰器 ----------------
def _backoff_delay(attempt: int, base: float, max_backoff: float, jitter_frac: float) -> float:
    """第 attempt 次重试前的等待:base * 2**(attempt-1) 封顶后再叠抖动。"""
    exp = base * (2 ** (attempt - 1))
    wait = min(exp, max_backoff)
    return wait + random.uniform(0, wait * jitter_frac)


def with_retry(
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    *,
    tool_name: str = "",
    base: float = RETRY_BASE_SECONDS,
    max_backoff: float = RETRY_MAX_BACKOFF,
    jitter_frac: float = RETRY_JITTER_FRAC,
) -> Callable:
    """只对 ToolRetryableError 重试;耗尽后返回「服务暂时不可用」块而非抛异常。

    用于包在 @tool 内部:@tool 仍是外层,with_retry 用 functools.wraps 保住
    __name__/__doc__/__signature__,langchain 工具 schema/introspection 不变。
    每次重试前经 agent_tools.tool_step 上报子步骤(web 工作流可见;CLI 安全 no-op)。
    """
    def _decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except ToolRetryableError as e:
                    if attempt >= max_attempts:
                        logger.error(f"[with_retry] {tool_name} 重试 {attempt} 次后仍失败:{e}")
                        return tool_error_block(
                            etype="服务暂时不可用",
                            reason=str(e),
                            is_retryable=True,
                            attempts=attempt,
                            suggestion="当前外部服务暂时不可用,请告知用户稍后重试,或改用其他工具/调整提问",
                        )
                    delay = _backoff_delay(attempt, base, max_backoff, jitter_frac)
                    logger.warning(
                        f"[with_retry] {tool_name} 第 {attempt} 次失败,{delay:.1f}s 后重试:{e}"
                    )
                    try:
                        from ReAct.tools.agent_tools import tool_step
                        tool_step(f"自动重试{attempt}", {"原因": str(e), "等待": round(delay, 1)})
                    except Exception:
                        pass  # 子步骤上报失败不影响重试主流程
                    time.sleep(delay)
            return None  # 不可达,保持类型收敛
        return _wrapper
    return _decorate
