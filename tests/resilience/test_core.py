"""核心驱动用例 —— 对应需求里的测试项 1~6、9~11、13~14。

全部用假可调用对象 + 构造异常,不碰任何真实外部服务;sleep/抖动/时钟都打桩,
所以既瞬时又确定。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from utils.resilience import (
    Kind,
    acall,
    alert_suppressed,
    backoff_delay,
    call,
    guard,
    map_partial,
    metrics_snapshot,
    register_alert_hook,
)
from utils.resilience.breaker import breaker_for
from utils.resilience.core import RetryPolicy


def _fail_then_ok(exc_factory, fail_times: int, value="OK"):
    """造一个「前 fail_times 次抛异常、之后返回 value」的假可调用对象。"""
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] <= fail_times:
            raise exc_factory()
        return value

    return fn, state


def _always_fail(exc_factory):
    state = {"n": 0}

    def fn():
        state["n"] += 1
        raise exc_factory()

    return fn, state


def _http_error(status: int, headers: dict | None = None):
    import requests
    resp = requests.Response()
    resp.status_code = status
    resp.headers = headers or {}
    return requests.HTTPError(f"HTTP {status}", response=resp)


# ---------------------------------------------------------------- 1 / 2

class TestTransientRetry:
    def test_retry_then_success(self, sleep_rec, no_jitter):
        """1. 瞬时超时 → 重试 → 第 2 次成功 → 正常返回。"""
        fn, state = _fail_then_ok(lambda: TimeoutError("timed out"), 1)
        result = call(fn, site="t1", fallback=lambda e, n: "FB", retries=2)
        assert result == "OK"
        assert state["n"] == 2
        assert len(sleep_rec.waits) == 1          # 只等了一次

    def test_exhausted_degrades(self, sleep_rec, no_jitter):
        """2. 连续超时 → 重试耗尽 → 降级。"""
        fn, state = _always_fail(lambda: TimeoutError("down"))
        result = call(fn, site="t2", fallback=lambda e, n: f"FB({n})", retries=2)
        assert result == "FB(3)"                  # 3 次尝试后降级
        assert state["n"] == 3
        assert len(sleep_rec.waits) == 2          # 2 次重试各等一次

    def test_degrade_receives_exception_and_attempts(self, sleep_rec, no_jitter):
        fn, _ = _always_fail(lambda: TimeoutError("down"))
        seen = {}

        def fb(exc, attempts):
            seen["exc"], seen["attempts"] = exc, attempts
            return "FB"

        call(fn, site="t2b", fallback=fb, retries=1)
        assert isinstance(seen["exc"], TimeoutError)
        assert seen["attempts"] == 2


# ---------------------------------------------------------------- 3 / 4

class TestBackoffBehaviour:
    def test_retry_after_header_is_honoured(self, sleep_rec, no_jitter):
        """3. 429 带 Retry-After → 按指示等待,而不是用算出来的退避。"""
        fn, _ = _always_fail(lambda: _http_error(429, {"Retry-After": "7"}))
        call(fn, site="t3", fallback=lambda e, n: "FB", retries=1, base=100.0)
        # base=100 会被算成 100s,但 Retry-After=7 优先
        assert sleep_rec.waits == [7.0]

    def test_retry_after_is_capped(self, sleep_rec, no_jitter):
        """上游给天文数字也不能把交互式问答卡死,必须封顶。"""
        fn, _ = _always_fail(lambda: _http_error(429, {"Retry-After": "3600"}))
        call(fn, site="t3b", fallback=lambda e, n: "FB", retries=1, retry_after_cap=5.0)
        assert sleep_rec.waits == [5.0]

    def test_5xx_exponential_backoff(self, sleep_rec, no_jitter):
        """4. 5xx → 指数退避重试(且严格递增)。"""
        fn, _ = _always_fail(lambda: _http_error(503))
        call(fn, site="t4", fallback=lambda e, n: "FB", retries=3, base=0.5, max_delay=8.0)
        assert sleep_rec.waits == [0.5, 1.0, 2.0]  # base * 2**0,1,2

    def test_backoff_respects_max_delay(self, sleep_rec, no_jitter):
        fn, _ = _always_fail(lambda: _http_error(500))
        call(fn, site="t4b", fallback=lambda e, n: "FB", retries=5, base=1.0, max_delay=3.0)
        assert sleep_rec.waits == [1.0, 2.0, 3.0, 3.0, 3.0]

    def test_backoff_formula_and_jitter_range(self):
        """9. 退避时长符合公式,抖动在范围内。"""
        p = RetryPolicy(retries=4, base=0.5, max_delay=8.0, jitter_ratio=0.5)
        for retry_index in range(4):
            expected = min(0.5 * (2 ** retry_index), 8.0)
            # 取大量样本,确认全部落在 [delay, delay*(1+jitter)] 且真的抖动了(不是恒定值)
            samples = [backoff_delay(retry_index, p) for _ in range(400)]
            assert all(expected <= s <= expected * 1.5 + 1e-9 for s in samples)
            assert min(samples) < max(samples), "抖动应当产生不同取值"

    def test_zero_jitter_is_exact(self):
        p = RetryPolicy(retries=2, base=1.0, max_delay=8.0, jitter_ratio=0.0)
        assert backoff_delay(0, p, rng=lambda a, b: 0.0) == 1.0
        assert backoff_delay(1, p, rng=lambda a, b: 0.0) == 2.0


# ---------------------------------------------------------------- 5 / 6 / 7 / 8

class TestDeterministicNoRetry:
    def test_400_degrades_immediately(self, sleep_rec, no_jitter):
        """5. 400 → 不重试,立即降级(不消耗重试次数、不等待)。"""
        fn, state = _always_fail(lambda: _http_error(400))
        result = call(fn, site="t5", fallback=lambda e, n: "FB", retries=3)
        assert result == "FB"
        assert state["n"] == 1
        assert sleep_rec.waits == []

    def test_401_degrades_and_alerts(self, sleep_rec, no_jitter):
        """6. 401/403 → 不重试,降级并告警。"""
        fired = []
        register_alert_hook(lambda site, code, msg, detail: fired.append((site, code)))
        fn, state = _always_fail(lambda: _http_error(401))
        result = call(fn, site="t6", fallback=lambda e, n: "FB", retries=3)
        assert result == "FB"
        assert state["n"] == 1                    # 没重试
        assert fired and fired[0][0] == "t6"      # 告警发出
        assert alert_suppressed() == 0

    def test_403_alerts_too(self, sleep_rec, no_jitter):
        fired = []
        register_alert_hook(lambda site, code, msg, detail: fired.append(code))
        fn, _ = _always_fail(lambda: _http_error(403))
        call(fn, site="t6b", fallback=lambda e, n: "FB")
        assert fired == ["http_403"]

    def test_tool_arg_validation_degrades(self, sleep_rec, no_jitter):
        """7. 工具参数校验失败 → 不重试,降级。"""
        from ReAct.tools.retry_util import ToolDeterministicError
        fn, state = _always_fail(lambda: ToolDeterministicError("query 字段非法"))
        result = call(fn, site="t7", fallback=lambda e, n: "FB", retries=3)
        assert result == "FB"
        assert state["n"] == 1

    def test_non_json_response_degrades_after_retry(self, sleep_rec, no_jitter):
        """8. 返回非 JSON 无法修复 → 降级(非 JSON 视为瞬时,可重试后耗尽)。"""
        fn, state = _always_fail(lambda: json.JSONDecodeError("bad body", "{", 0))
        result = call(fn, site="t8", fallback=lambda e, n: "FB", retries=1)
        assert result == "FB"
        assert state["n"] == 2


# ---------------------------------------------------------------- 10

class TestTimeoutBudget:
    def test_budget_breach_degrades_without_sleeping(self, sleep_rec, no_jitter):
        """10. 重试总耗时超预算 → 提前降级(不再等待/不再调用)。"""
        fn, state = _always_fail(lambda: TimeoutError("down"))
        # 首次退避就是 10s,预算只有 1s → 第二次尝试前就该放弃
        result = call(fn, site="t10", fallback=lambda e, n: "FB",
                      retries=5, base=10.0, timeout_budget=1.0)
        assert result == "FB"
        assert state["n"] == 1
        assert sleep_rec.waits == []              # 一次都没等

    def test_async_budget_cancels_in_flight_call(self, sleep_rec, no_jitter):
        """异步路径的预算能真正取消在途请求(同步路径做不到)。"""
        async def slow():
            await asyncio.sleep(10)
            return "never"

        async def run():
            return await acall(slow, site="t10b", fallback=lambda e, n: "FB",
                               retries=3, base=0.0, timeout_budget=0.05)

        result = asyncio.run(run())
        assert result == "FB"


# ---------------------------------------------------------------- 13

class TestCache:
    def test_cache_hit_skips_call(self, sleep_rec, no_jitter):
        """13. 缓存命中 → 不调用。"""
        state = {"n": 0}

        def fn():
            state["n"] += 1
            return "V"

        r1 = call(fn, site="t13", fallback=lambda e, n: "FB", cache=True)
        r2 = call(fn, site="t13", fallback=lambda e, n: "FB", cache=True)
        assert (r1, r2) == ("V", "V")
        assert state["n"] == 1
        snap = metrics_snapshot()["sites"]["t13"]
        assert snap["cache_hits"] == 1 and snap["cache_misses"] == 1

    def test_failures_are_not_cached(self, sleep_rec, no_jitter):
        """失败不缓存:修好后下一次调用必须真的打过去。"""
        state = {"n": 0}

        def fn():
            state["n"] += 1
            if state["n"] == 1:
                raise TimeoutError("x")
            return "V"

        r1 = call(fn, site="t13b", fallback=lambda e, n: "FB", retries=0, cache=True)
        r2 = call(fn, site="t13b", fallback=lambda e, n: "FB", retries=0, cache=True)
        assert r1 == "FB" and r2 == "V"
        assert state["n"] == 2

    def test_distinct_inputs_get_distinct_entries(self, sleep_rec, no_jitter):
        state = {"n": 0}

        def fn(x):
            state["n"] += 1
            return x * 2

        assert call(fn, 1, site="t13c", fallback=lambda e, n: -1, cache=True) == 2
        assert call(fn, 2, site="t13c", fallback=lambda e, n: -1, cache=True) == 4
        assert state["n"] == 2                    # 不同输入不应命中同一缓存


# ---------------------------------------------------------------- 14

class TestConcurrency:
    def test_partial_failure_does_not_affect_others(self):
        """14. 并行调用单个失败 → 不影响其他。"""
        from functools import partial

        def ok(v):
            return lambda: v

        def boom():
            raise RuntimeError("degradation function itself is broken")

        fns = [
            partial(call, ok("a"), site="t14", fallback=lambda e, n: "FB"),
            boom,                                 # 降级函数坏掉 → 真异常
            partial(call, ok("c"), site="t14", fallback=lambda e, n: "FB"),
        ]
        results, errors = map_partial(fns, max_workers=3)
        assert sorted(results) == ["a", "c"]
        assert len(errors) == 1

    def test_one_slow_call_does_not_serialize_the_rest(self):
        """独立的调用不应被串行等待 —— 慢的那个不该拖住快的。"""
        import time
        from functools import partial

        def quick():
            return "q"

        def slow():
            time.sleep(0.15)
            return "s"

        start = time.perf_counter()
        results, _ = map_partial([
            partial(call, quick, site="t14b", fallback=lambda e, n: "FB"),
            partial(call, slow, site="t14b", fallback=lambda e, n: "FB"),
        ], max_workers=2)
        elapsed = time.perf_counter() - start
        assert sorted(results) == ["q", "s"]
        assert elapsed < 0.30, f"应当是并行的,实际耗时 {elapsed:.2f}s"


# ---------------------------------------------------------------- 其它契约

class TestContracts:
    def test_fallback_is_mandatory(self):
        # 签名上就是必填 keyword-only:缺了 Python 自己会拦,轮不到我们的 ValueError。
        # 这里断言签名契约本身,确保没人把它改成可选。
        import inspect
        sig = inspect.signature(call)
        assert sig.parameters["fallback"].default is inspect.Parameter.empty
        assert sig.parameters["site"].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["fallback"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_empty_site_rejected(self):
        with pytest.raises(ValueError, match="site"):
            call(lambda: 1, site="", fallback=lambda e, n: 1)

    def test_none_fallback_rejected(self):
        with pytest.raises(ValueError, match="fallback"):
            call(lambda: 1, site="x", fallback=None)

    def test_broken_fallback_raises(self, sleep_rec, no_jitter):
        """降级函数自己坏了属编程错误,不能静默吞掉。"""
        def broken(exc, attempts):
            raise RuntimeError("fallback is broken")

        with pytest.raises(RuntimeError, match="fallback is broken"):
            call(lambda: (_ for _ in ()).throw(TimeoutError("x")),
                 site="tC", fallback=broken, retries=0)

    def test_success_records_metrics(self, sleep_rec, no_jitter):
        fn, _ = _fail_then_ok(lambda: TimeoutError("x"), 1)
        call(fn, site="tM", fallback=lambda e, n: "FB", retries=2)
        snap = metrics_snapshot()["sites"]["tM"]
        assert snap["successes"] == 1
        assert snap["retries"] == 1
        assert snap["retry_histogram"] == {"2": 1}

    def test_unknown_error_degrades_without_retry(self, sleep_rec, no_jitter):
        fn, state = _always_fail(lambda: ValueError("nondescript"))
        call(fn, site="tU", fallback=lambda e, n: "FB", retries=3)
        assert state["n"] == 1
        assert metrics_snapshot()["sites"]["tU"]["unknown"] == 1

    def test_guard_preserves_name_and_doc(self, sleep_rec, no_jitter):
        """langchain 的 @tool 依赖 __name__/__doc__/__signature__,必须保住。"""
        @guard(site="tG", fallback=lambda e, n: "FB", retries=0)
        def my_tool(query: str) -> str:
            """工具说明。"""
            return query

        assert my_tool.__name__ == "my_tool"
        assert my_tool.__doc__ == "工具说明。"
        assert my_tool("hi") == "hi"

    def test_guard_handles_async(self, sleep_rec, no_jitter):
        @guard(site="tGA", fallback=lambda e, n: "FB", retries=0)
        async def my_async():
            raise TimeoutError("x")

        assert asyncio.run(my_async()) == "FB"

    def test_sync_path_handles_coroutine_fn_via_call(self, sleep_rec, no_jitter):
        """call() 拿到协程函数时返回协程(委托异步驱动)。"""
        async def coro():
            return "async-value"

        assert asyncio.run(call(coro, site="tAC", fallback=lambda e, n: "FB")) == "async-value"

    def test_labels_split_metrics_by_dimension(self, sleep_rec, no_jitter):
        """指标可按模型/租户/工具维度区分。"""
        call(lambda: "v", site="tL", fallback=lambda e, n: "FB", labels={"model": "m1"})
        call(lambda: "v", site="tL", fallback=lambda e, n: "FB", labels={"model": "m2"})
        sites = metrics_snapshot()["sites"]
        assert "tL|model=m1" in sites and "tL|model=m2" in sites
