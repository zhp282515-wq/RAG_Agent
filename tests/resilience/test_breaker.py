"""熔断与幂等用例 —— 对应需求测试项 11、12、15,以及熔断器的状态机边界。

熔断窗口与冷却都按流逝时间计算,所以这里必须用假时钟 advance 来跨越阈值,
不能靠「调够次数」蒙过去 —— 那正是真实世界里会失效的假设。
"""
from __future__ import annotations

import pytest

from utils.resilience import Kind, call, metrics_snapshot
from utils.resilience.breaker import (
    BreakerPolicy,
    CircuitBreaker,
    CircuitOpenError,
    State,
    breaker_for,
    breaker_snapshots,
    set_default_clock,
)


def _http_error(status: int):
    import requests
    resp = requests.Response()
    resp.status_code = status
    resp.headers = {}
    return requests.HTTPError(f"HTTP {status}", response=resp)


def _always(exc_factory):
    state = {"n": 0}

    def fn():
        state["n"] += 1
        raise exc_factory()

    return fn, state


@pytest.fixture
def breaker_clock(monkeypatch, fake_clock):
    """让新建的熔断器也用同一个假时钟。"""
    set_default_clock(fake_clock)
    yield fake_clock
    set_default_clock(__import__("time").monotonic)


# ---------------------------------------------------------------- 11 / 12

class TestBreakerThroughDriver:
    def test_breaker_trips_after_threshold(self, sleep_rec, no_jitter, breaker_clock):
        """11. 熔断触发 → 后续直接降级,不再调用 fn。"""
        fn, state = _always(lambda: TimeoutError("down"))
        # 阈值 5;每次调用只尝试 1 次(retries=0),方便数调用次数
        for _ in range(7):
            result = call(fn, site="b1", fallback=lambda e, n: "FB", retries=0)
            assert result == "FB"
        assert state["n"] == 5, "达到阈值后应当短路,不再调用"
        assert metrics_snapshot()["sites"]["b1"]["breaker_rejections"] == 2

    def test_breaker_open_short_circuits_without_touching_fn(self, sleep_rec, no_jitter, breaker_clock):
        fn, state = _always(lambda: TimeoutError("down"))
        for _ in range(5):
            call(fn, site="b2", fallback=lambda e, n: "FB", retries=0)
        before = state["n"]
        call(fn, site="b2", fallback=lambda e, n: "FB", retries=0)
        assert state["n"] == before, "熔断期间 fn 不该被调用"

    def test_probe_success_recovers(self, sleep_rec, no_jitter, breaker_clock):
        """12. 熔断探针成功 → 恢复。"""
        fn, state = _always(lambda: TimeoutError("down"))
        for _ in range(5):
            call(fn, site="b3", fallback=lambda e, n: "FB", retries=0)

        br = breaker_for("b3")
        assert br.state() is State.OPEN

        # 冷却到期 → 放探针;这次让它成功
        breaker_clock.advance(31)
        recovered = {"n": 0}

        def good():
            recovered["n"] += 1
            return "OK"

        assert call(good, site="b3", fallback=lambda e, n: "FB", retries=0) == "OK"
        assert br.state() is State.CLOSED
        assert recovered["n"] == 1

    def test_failed_probe_reopens_with_longer_cooldown(self, sleep_rec, no_jitter, breaker_clock):
        """探针失败 → 冷却延长,避免在故障期被反复试探。"""
        fn, _ = _always(lambda: TimeoutError("down"))
        for _ in range(5):
            call(fn, site="b4", fallback=lambda e, n: "FB", retries=0)
        br = breaker_for("b4")

        breaker_clock.advance(31)          # 第一轮冷却 30s 到期
        call(fn, site="b4", fallback=lambda e, n: "FB", retries=0)   # 探针失败
        assert br.state() is State.OPEN
        assert br.cooldown_left() == pytest.approx(60.0, abs=1.0)    # 30 * 2

    def test_deterministic_errors_do_not_trip_breaker(self, sleep_rec, no_jitter, breaker_clock):
        """确定性错误重试结果一样,熔断改变不了什么 —— 不该触发熔断。"""
        fn, state = _always(lambda: _http_error(400))
        for _ in range(10):
            call(fn, site="b5", fallback=lambda e, n: "FB", retries=0)
        assert state["n"] == 10, "确定性错误不该被熔断短路"
        assert breaker_for("b5").state() is State.CLOSED

    def test_breaker_off_keeps_calling(self, sleep_rec, no_jitter, breaker_clock):
        fn, state = _always(lambda: TimeoutError("down"))
        for _ in range(7):
            call(fn, site="b6", fallback=lambda e, n: "FB", retries=0, breaker=False)
        assert state["n"] == 7

    def test_sites_are_isolated(self, sleep_rec, no_jitter, breaker_clock):
        """一个调用点熔断不能牵连另一个。"""
        fn, _ = _always(lambda: TimeoutError("down"))
        for _ in range(5):
            call(fn, site="b7a", fallback=lambda e, n: "FB", retries=0)
        assert breaker_for("b7a").state() is State.OPEN
        assert breaker_for("b7b").state() is State.CLOSED


class TestBreakerUnit:
    """直接测状态机,不经驱动。"""

    def test_threshold_and_window(self, fake_clock):
        p = BreakerPolicy(failure_threshold=3, window_seconds=60, cooldown_seconds=10)
        b = CircuitBreaker("u1", p, clock=fake_clock)
        b.note_failure(Kind.TRANSIENT)
        b.note_failure(Kind.TRANSIENT)
        assert b.state() is State.CLOSED
        b.note_failure(Kind.TRANSIENT)
        assert b.state() is State.OPEN

    def test_stale_failures_fall_out_of_window(self, fake_clock):
        p = BreakerPolicy(failure_threshold=3, window_seconds=60, cooldown_seconds=10)
        b = CircuitBreaker("u2", p, clock=fake_clock)
        b.note_failure(Kind.TRANSIENT)
        b.note_failure(Kind.TRANSIENT)
        fake_clock.advance(61)                 # 前两次已出窗口
        b.note_failure(Kind.TRANSIENT)
        assert b.state() is State.CLOSED, "窗口外的失败不该累积"

    def test_success_clears_window(self, fake_clock):
        p = BreakerPolicy(failure_threshold=3, window_seconds=60, cooldown_seconds=10)
        b = CircuitBreaker("u3", p, clock=fake_clock)
        b.note_failure(Kind.TRANSIENT)
        b.note_failure(Kind.TRANSIENT)
        b.note_success()
        b.note_failure(Kind.TRANSIENT)
        assert b.state() is State.CLOSED, "成功应当清空失败窗口"

    def test_budget_and_cooldown_shape(self, fake_clock):
        p = BreakerPolicy(failure_threshold=1, window_seconds=60, cooldown_seconds=10,
                          open_backoff_factor=3.0, max_cooldown_seconds=25.0)
        b = CircuitBreaker("u4", p, clock=fake_clock)
        b.note_failure(Kind.TRANSIENT)
        assert b.cooldown_left() == pytest.approx(10.0)
        for rounds, expected in ((1, 25.0), (2, 25.0)):  # 10*3=30 → 封顶 25
            fake_clock.advance(1000)
            b.allow()
            b.note_failure(Kind.TRANSIENT)
            assert b.cooldown_left() == pytest.approx(expected)

    def test_allow_reports_remaining_cooldown(self, fake_clock):
        p = BreakerPolicy(failure_threshold=1, window_seconds=60, cooldown_seconds=30)
        b = CircuitBreaker("u5", p, clock=fake_clock)
        b.note_failure(Kind.TRANSIENT)
        ok, left = b.allow()
        assert ok is False and left == pytest.approx(30.0)
        fake_clock.advance(10)
        ok, left = b.allow()
        assert ok is False and left == pytest.approx(20.0)

    def test_deterministic_failure_ignored_by_state_machine(self, fake_clock):
        p = BreakerPolicy(failure_threshold=2, window_seconds=60, cooldown_seconds=10)
        b = CircuitBreaker("u6", p, clock=fake_clock)
        for _ in range(5):
            b.note_failure(Kind.DETERMINISTIC)
        assert b.state() is State.CLOSED

    def test_snapshot_shape(self, fake_clock):
        p = BreakerPolicy(failure_threshold=5, window_seconds=60, cooldown_seconds=30)
        b = CircuitBreaker("u7", p, clock=fake_clock)
        snap = b.snapshot()
        assert snap["site"] == "u7" and snap["state"] == "closed"
        assert snap["failure_threshold"] == 5

    def test_registry_returns_same_instance(self):
        assert breaker_for("same") is breaker_for("same")

    def test_breaker_snapshots_covers_known_sites(self):
        breaker_for("snapA")
        assert "snapA" in breaker_snapshots()


# ---------------------------------------------------------------- 15

class TestIdempotency:
    def test_non_idempotent_never_retries(self, sleep_rec, no_jitter):
        """15. 有副作用的调用重试必须幂等 —— 声明不幂等时强制不重试。"""
        state = {"n": 0}

        def charge():
            state["n"] += 1
            raise TimeoutError("gateway timeout")

        result = call(charge, site="idem1", fallback=lambda e, n: "FB",
                      retries=3, idempotent=False)
        assert result == "FB"
        assert state["n"] == 1, "副作用只能发生一次"
        assert sleep_rec.waits == []

    def test_idempotent_retries_within_limit(self, sleep_rec, no_jitter):
        """幂等的调用允许重试,但仍受上限约束(不会无限重放)。"""
        state = {"n": 0}

        def refetch():
            state["n"] += 1
            if state["n"] < 3:
                raise TimeoutError("x")
            return "V"

        assert call(refetch, site="idem2", fallback=lambda e, n: "FB",
                    retries=4, idempotent=True) == "V"
        assert state["n"] == 3

    def test_idempotent_guard_logs_enforcement(self, sleep_rec, no_jitter):
        """强制收敛为 0 次重试这件事应当留痕,便于排查「为什么没重试」。

        注意:项目 logger 设了 propagate=False(utils/logger_tool.py),pytest 的 caplog
        捕获不到它 —— 必须直接往 logger 挂一个 handler。这一点对所有项目日志都成立。
        """
        import logging

        from utils.logger_tool import logger as project_logger

        captured: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = _Cap(level=logging.DEBUG)
        project_logger.addHandler(handler)
        try:
            def side_effect():
                raise TimeoutError("x")

            call(side_effect, site="idem3", fallback=lambda e, n: "FB",
                 retries=2, idempotent=False)
        finally:
            project_logger.removeHandler(handler)

        assert any("idempotent_guard" in m for m in captured), \
            f"应当记录一次 idempotent 守卫事件,实得:{captured}"


class TestCircuitOpenError:
    def test_degrade_receives_circuit_open(self, sleep_rec, no_jitter, breaker_clock):
        """降级函数能区分「调用真失败」与「压根没调用」。"""
        fn, _ = _always(lambda: TimeoutError("down"))
        for _ in range(5):
            call(fn, site="coe", fallback=lambda e, n: "FB", retries=0)

        seen = {}

        def fb(exc, attempts):
            seen["exc"] = exc
            seen["attempts"] = attempts
            return "FB"

        call(fn, site="coe", fallback=fb, retries=0)
        assert isinstance(seen["exc"], CircuitOpenError)
        assert seen["attempts"] == 0
