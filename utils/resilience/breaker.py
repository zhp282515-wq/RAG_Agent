"""熔断器:防止单点持续故障被无限放大。

状态机:
    CLOSED   ──瞬时失败在窗口内达阈值──>  OPEN
    OPEN     ──冷却到期──────────────>  HALF_OPEN(放少量探针)
    HALF_OPEN ──探针成功────────────>  CLOSED(复位)
    HALF_OPEN ──探针失败────────────>  OPEN(冷却按因子指数延长)

三条关键约束:
  1. **确定性错误不触发熔断**。重试结果一样的错误,熔断也改变不了什么,徒增复杂度;
     这类错误走告警(通常是代码/配置问题,得有人去看)。
  2. **锁只在纯内存状态迁移期间持有**,绝不跨调用/sleep/日志/指标/告警钩子。
     web/server.py 在后台线程里调工具,锁里做任何 I/O 都会把并发调用串行化。
  3. **计时用注入时钟,默认 time.monotonic**。窗口与冷却都按流逝时间算,不用
     time.time —— NTP 校时回拨会让失败时间戳「来自未来」,窗口永远清不掉。
     测试必须注入假时钟并 advance(),没有纯计数路径。
"""
from __future__ import annotations

import enum
import threading
import time
from collections import deque
from dataclasses import dataclass

from utils.resilience.classify import Kind
from utils.resilience.observe import log_event, registry


class State(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """熔断打开、本次调用被短路时抛出(交给降级函数)。

    它的存在让降级函数能区分「调用真的失败了」与「压根没调用」。
    """

    def __init__(self, site: str, cooldown_left: float = 0.0) -> None:
        self.site = site
        self.cooldown_left = cooldown_left
        super().__init__(f"熔断已打开({site}),剩余冷却 {max(0.0, cooldown_left):.1f}s")


@dataclass(frozen=True)
class BreakerPolicy:
    failure_threshold: int = 5
    window_seconds: float = 60.0
    cooldown_seconds: float = 30.0
    probe_calls: int = 1
    open_backoff_factor: float = 2.0
    max_cooldown_seconds: float = 300.0

    @classmethod
    def from_config(cls, site: str) -> "BreakerPolicy":
        from utils.resilience import config as _config
        return cls(
            failure_threshold=max(1, _config.get_int("breaker_failure_threshold", site=site)),
            window_seconds=max(0.0, _config.get_float("breaker_window_seconds", site=site)),
            cooldown_seconds=max(0.0, _config.get_float("breaker_cooldown_seconds", site=site)),
            probe_calls=max(1, _config.get_int("breaker_probe_calls", site=site)),
            open_backoff_factor=max(1.0, _config.get_float("breaker_open_backoff_factor", site=site)),
            max_cooldown_seconds=max(0.0, _config.get_float("breaker_max_cooldown_seconds", site=site)),
        )


class CircuitBreaker:
    """单个调用点的熔断器。所有状态迁移都在 self._lock 内完成。"""

    __slots__ = ("_site", "_p", "_clock", "_lock", "_state", "_failures",
                 "_opened_at", "_open_count", "_probes_in_flight", "_half_opened_at")

    def __init__(self, site: str, policy: BreakerPolicy, *, clock=time.monotonic) -> None:
        self._site = site
        self._p = policy
        self._clock = clock
        self._lock = threading.Lock()
        self._state = State.CLOSED
        self._failures: deque[float] = deque()   # 仅瞬时失败的时钟戳
        self._opened_at = 0.0
        self._open_count = 0                     # 已延长过几次(冷却指数用)
        self._probes_in_flight = 0
        self._half_opened_at = 0.0

    # ---- 策略(测试用假时钟时可替换) ----
    @property
    def site(self) -> str:
        return self._site

    def _cooldown_for(self, n: int) -> float:
        """冷却时长:每延长一次乘一个因子,封顶。探针反复失败时不要退化成永久熔断。"""
        return min(self._p.cooldown_seconds * (self._p.open_backoff_factor ** n),
                   self._p.max_cooldown_seconds)

    def _prune(self, now: float) -> None:
        """丢掉窗口外的失败记录(调用方必须已持锁)。"""
        cutoff = now - self._p.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    # ---- 放行判定 ----
    def allow(self) -> tuple[bool, float]:
        """本次调用是否放行。返回 (放行?, 剩余冷却秒数)。"""
        now = self._clock()
        with self._lock:
            if self._state is State.CLOSED:
                return True, 0.0

            if self._state is State.OPEN:
                left = self._opened_at + self._cooldown_for(self._open_count) - now
                if left > 0:
                    return False, left
                # 冷却到期 → 半开,放探针
                self._state = State.HALF_OPEN
                self._half_opened_at = now
                self._probes_in_flight = 0

            # HALF_OPEN:限制在途探针数;并防止探针卡死后永久半开
            if now - self._half_opened_at > self._cooldown_for(self._open_count):
                self._probes_in_flight = 0
                self._half_opened_at = now
            if self._probes_in_flight < self._p.probe_calls:
                self._probes_in_flight += 1
                return True, 0.0
            return False, max(0.0, self._opened_at + self._cooldown_for(self._open_count) - now)

    # ---- 结果回报 ----
    def note_success(self) -> None:
        """成功:CLOSED 清空失败窗口;HALF_OPEN 视为探针通过,复位为 CLOSED。"""
        with self._lock:
            was = self._state
            self._failures.clear()
            if was is State.HALF_OPEN:
                self._probes_in_flight = max(0, self._probes_in_flight - 1)
                self._state = State.CLOSED
                self._open_count = 0
        if was is State.HALF_OPEN:
            log_event("breaker_close", site=self._site, level="info", probes="ok")
            registry().record_breaker(self._site)

    def note_failure(self, kind: Kind) -> None:
        """失败:只有瞬时错误计入窗口。确定性/未知错误不改熔断状态。"""
        if kind is not Kind.TRANSIENT:
            return
        now = self._clock()
        opened_after: int | None = None
        with self._lock:
            self._prune(now)
            self._failures.append(now)
            n = len(self._failures)
            if self._state is State.HALF_OPEN:
                # 探针失败 → 回到 OPEN,冷却延长
                self._probes_in_flight = max(0, self._probes_in_flight - 1)
                self._state = State.OPEN
                self._opened_at = now
                self._open_count += 1
                opened_after = self._open_count
            elif self._state is State.CLOSED and n >= self._p.failure_threshold:
                self._state = State.OPEN
                self._opened_at = now
                opened_after = self._open_count
        if opened_after is not None:
            cooldown = self._cooldown_for(opened_after)
            log_event("breaker_open", site=self._site, level="warning",
                      failures=f"{n}/{self._p.failure_threshold}",
                      window=self._p.window_seconds, cooldown=round(cooldown, 1))
            registry().record_breaker(self._site, opened=True)

    def state(self) -> State:
        with self._lock:
            return self._state

    def cooldown_left(self) -> float:
        now = self._clock()
        with self._lock:
            if self._state is State.CLOSED:
                return 0.0
            return max(0.0, self._opened_at + self._cooldown_for(self._open_count) - now)

    def reset(self) -> None:
        """强制复位(测试/运维手动解熔断用)。"""
        with self._lock:
            self._state = State.CLOSED
            self._failures.clear()
            self._opened_at = 0.0
            self._open_count = 0
            self._probes_in_flight = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "site": self._site,
                "state": self._state.value,
                "failures_in_window": len(self._failures),
                "failure_threshold": self._p.failure_threshold,
                "window_seconds": self._p.window_seconds,
                "cooldown_left": round(
                    max(0.0, self._opened_at + self._cooldown_for(self._open_count) - self._clock()), 3
                ) if self._state is not State.CLOSED else 0.0,
                "open_count": self._open_count,
            }


# ---------------------------------------------------------------- 注册表

_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()

# 测试/特殊调用点可覆盖默认时钟
_default_clock = time.monotonic


def set_default_clock(clock) -> None:
    """替换新建熔断器使用的时钟(测试用)。已存在的实例不受影响。"""
    global _default_clock
    _default_clock = clock


def breaker_for(site: str, policy: BreakerPolicy | None = None,
                *, clock=None) -> CircuitBreaker:
    """取(或惰性建)某调用点的熔断器。同一 site 始终返回同一实例。"""
    with _breakers_lock:
        b = _breakers.get(site)
        if b is None:
            b = CircuitBreaker(
                site,
                policy or BreakerPolicy.from_config(site),
                clock=clock or _default_clock,
            )
            _breakers[site] = b
        return b


def reset_breakers() -> None:
    """清空注册表(测试隔离用)。"""
    with _breakers_lock:
        _breakers.clear()


def breaker_snapshots() -> dict[str, dict]:
    """所有熔断器状态(排查用,也可挂到 web 端点)。"""
    with _breakers_lock:
        items = list(_breakers.items())
    return {name: b.snapshot() for name, b in items}
