"""resilience 组件测试的公共装置。

核心诉求:**测试必须瞬时且确定**。退避是真实 sleep,断言真实随机抖动,都会让测试
既慢又飘。这里统一把 sleep 打桩、把抖动固定、把时钟换成假时钟,让每个用例能在
毫秒级跑完并精确断言等待时长。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import utils.resilience.core as core  # noqa: E402


class FakeClock:
    """可推进的假时钟。熔断窗口/冷却都是按流逝时间算的,必须靠 advance 跨越。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class SleepRecorder:
    """记录「本该 sleep 多久」,但实际不睡。断言退避公式与 Retry-After 靠它。"""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(float(seconds))

    @property
    def total(self) -> float:
        return sum(self.waits)

    def clear(self) -> None:
        self.waits.clear()


@pytest.fixture
def sleep_rec(monkeypatch):
    """同步与异步两条路径的 sleep 都换成记录器(异步返回一个已被 await 的假协程)。"""
    rec = SleepRecorder()
    monkeypatch.setattr(core, "_sleep", rec)

    async def _async_noop(seconds: float) -> None:
        rec.waits.append(float(seconds))

    monkeypatch.setattr(core, "_sleep_async", _async_noop)
    return rec


@pytest.fixture
def no_jitter(monkeypatch):
    """把抖动固定为 0,使等待时长可精确断言。"""
    monkeypatch.setattr(core.random, "uniform", lambda a, b: 0.0)


@pytest.fixture
def fake_clock(monkeypatch):
    """把组件的默认时钟换成假时钟(用于熔断窗口/冷却)。"""
    clk = FakeClock()
    monkeypatch.setattr(core, "_clock", clk)
    return clk


@pytest.fixture(autouse=True)
def _isolate():
    """每个用例前后清理全局状态,避免用例之间互相污染(熔断/指标/缓存/告警限流)。

    分类器注册表刻意用**快照/恢复**而不是 clear:rewriter.py、external_tool_wrap.py
    等模块在 import 时就往注册表里加了规则,直接清空会把这些一并抹掉,并且因为模块
    不会重新 import,规则再也回不来 —— 表现为「单个文件跑通、全量跑就挂」的假故障。
    测试自己注册的规则用完即弃,别人的注册原样还原。
    """
    from utils.resilience import (
        reset_alert_state,
        reset_breakers,
        reset_caches,
        reset_metrics,
        restore_registrations,
        snapshot_registrations,
    )

    saved = snapshot_registrations()

    def _cleanup():
        reset_metrics()
        reset_breakers()
        reset_caches()
        reset_alert_state()
        restore_registrations(saved)

    _cleanup()
    yield
    _cleanup()
