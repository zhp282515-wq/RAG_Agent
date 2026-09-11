"""MCP 测试服务器 · 失败与慢速:专测项目的外部工具异常包装(@@TOOL_ERROR@@ / 重试)。

两个工具:
- boom()      :确定性失败 → 应归一为 @@TOOL_ERROR@@(是否可重试:否)。
- flaky(n)    :前 n 次调用抛连接类瞬时错误 → 触发 wrapper 自动重试;成功路径也验证。

运行:
    python demo_mcp/fault_server.py
"""
import asyncio

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo-fault")


@mcp.tool()
async def boom(reason: str = "demo boom") -> str:
    """总是失败的测试工具:抛确定性错误,看是否正确归一为 @@TOOL_ERROR@@。"""
    raise ValueError(f"{reason}")


@mcp.tool()
async def flaky() -> str:
    """演示「瞬时失败 → 自动重试」:约每 3 次调用成功 1 次。

    wrapper(max_attempts=3)单次调用会最多尝试 3 次;本工具随机性失败,
    多问几次即可看到「自动重试后最终成功」与「重试耗尽返回可重试错误」两种结果。
    """
    import random
    if random.random() < 0.5:
        raise TimeoutError("upstream service temporarily unavailable (timed out)")
    return "本次调用成功(可看到重试前失败的日志与最终成功返回)"


@mcp.tool()
async def always_flaky() -> str:
    """总是瞬时失败:测「重试耗尽 → 返回可重试 @@TOOL_ERROR@@」(wrapper 试 3 次仍败)。"""
    raise TimeoutError("upstream service temporarily unavailable (timed out)")


if __name__ == "__main__":
    mcp.run(transport="stdio")
