"""MCP 测试服务器 · 基础:数学运算(get_rerank_retriever 之外最直观的工具)。

覆盖:多参数工具 / 返回数字 / 返回字符串。注册到项目系统配置 → 工具页即可让
agent 调用「计算器」「今天星期几」等(模型会自主决定调用)。

运行:
    python demo_mcp/math_server.py
"""
import asyncio
from datetime import datetime

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo-math")


@mcp.tool()
async def add(a: float, b: float) -> float:
    """两数相加。"""
    return a + b


@mcp.tool()
async def multiply(a: float, b: float) -> float:
    """两数相乘。"""
    return a * b


@mcp.tool()
async def weekday() -> str:
    """今天是星期几(中文)。"""
    return "今天是" + "一二三四五六日"[datetime.now().weekday()] + "曜日"


if __name__ == "__main__":
    mcp.run(transport="stdio")
