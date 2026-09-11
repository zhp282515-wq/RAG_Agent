"""MCP 测试服务器 · 模拟真实外部数据源:城市→ 温度/湿度/天气。

覆盖:字符串枚举入参(city)、结构化返回。用于测试 agent 在知识库之外补一个
"天气数据源" 的调用(与内置 get_weather 形成对比,便于确认外部工具被调用)。

运行:
    python demo_mcp/weather_server.py
"""
import asyncio

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo-weather")

_CITY_DATA = {
    "北京": {"temp": 24, "humidity": 40, "desc": "晴"},
    "上海": {"temp": 26, "humidity": 68, "desc": "多云"},
    "广州": {"temp": 30, "humidity": 80, "desc": "阵雨"},
    "深圳": {"temp": 29, "humidity": 75, "desc": "多云"},
    "杭州": {"temp": 25, "humidity": 60, "desc": "阴"},
}


@mcp.tool()
async def get_city_weather(city: str) -> str:
    """查询指定城市今日气温/湿度/天气。支持:北京、上海、广州、深圳、杭州。"""
    c = (city or "").strip()
    if c not in _CITY_DATA:
        return f"暂不支持城市「{c}」,可用:北京、上海、广州、深圳、杭州"
    d = _CITY_DATA[c]
    return f"{c}:{d['desc']},气温 {d['temp']}℃,相对湿度 {d['humidity']}%"


if __name__ == "__main__":
    mcp.run(transport="stdio")
