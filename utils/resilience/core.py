"""统一调用封装:重试 + 退避抖动 + 熔断 + 降级 + 缓存 + 指标,一处实现。

调用点只负责提供两样东西 —— **降级函数**和**调用点标识(site)**;其余(重试几次、
等多久、什么错该重试、什么时候熔断、怎么记日志)全部由本模块按 config/resilience.yml
统一处理。

    def my_call(): ...

    def my_fallback(exc, attempts):
        return "兜底值"

    result = call(my_call, site="my_feature", fallback=my_fallback)

或作为装饰器:

    @guard(site="my_feature", fallback=my_fallback)
    def my_call(): ...

两条硬约束:
  1. **降级函数必填**。没有降级路径的调用点等于把异常丢给用户,这是需求明确禁止的。
  2. **降级函数自身抛错属编程错误** → 记 degrade_error 后原样抛出。静默吞掉会让
     「降级也是坏的」这种状态永远发现不了。

两条必须知道的语义差异:
  - **超时预算**:异步路径用 asyncio.wait_for 真正取消在途请求;同步路径无法中断
    已经发出的阻塞调用(requests/OpenAI SDK 都不支持),预算只能在重试边界生效。
    同步调用的硬超时仍依赖各 SDK 自己的 timeout 参数。
  - **幂等**:idempotent=False 时有效重试次数强制为 0。有副作用的调用(写库、发消息、
    扣费)重放一次可能造成真实损失,宁可降级也不要冒这个险。
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Sequence, TypeVar

from utils.resilience import config as _config
from utils.resilience.breaker import CircuitOpenError, breaker_for
from utils.resilience.classify import Classified, Kind, classify as _classify
from utils.resilience.observe import alert, log_event, registry

T = TypeVar("T")

# 降级函数:(异常, 已尝试次数) -> 兜底值
Fallback = Callable[[BaseException, int], Any]

# 默认时钟(可被 set_clock 整体替换,测试用)
_clock: Callable[[], float] = time.monotonic

# 同步/异步 sleep 都走模块级别名,测试 monkeypatch 一个符号即可两条路径都静音
_sleep = time.sleep
_sleep_async = asyncio.sleep


def set_clock(clock: Callable[[], float]) -> None:
    """替换本模块使用的时钟(必须在首次调用前设置;已建的熔断器另有自己的时钟)。"""
    global _clock
    _clock = clock


def get_clock() -> Callable[[], float]:
    return _clock


@dataclass(frozen=True)
class RetryPolicy:
    """一个调用点本次生效的策略(retries 是「重试次数」,总尝试数 = retries + 1)。"""

    retries: int = 2
    base: float = 0.5
    max_delay: float = 8.0
    jitter_ratio: float = 0.5
    retry_after_cap: float = 60.0
    timeout_budget: float | None = None

    @property
    def attempts(self) -> int:
        return self.retries + 1


@dataclass
class Outcome(Generic[T]):
    """调用结果(仅 map_partial 等需要区分「值」与「降级」时使用)。"""

    value: T | None
    ok: bool
    attempts: int
    kind: Kind | None = None
    code: str = ""
    degraded: bool = False


# ---------------------------------------------------------------- 退避

def backoff_delay(retry_index: int, policy: RetryPolicy, rng=None) -> float:
    """第 retry_index 次重试前的等待时长(0 基:首次重试用 retry_index=0)。

        delay = min(base * 2**retry_index, max_delay)
        sleep = delay + rng(0, delay * jitter_ratio)

    0 基是刻意的:首次重试的退避等于 base,与项目原有 `base * 2**(attempt-1)`
    (attempt 从 1 起)数值完全一致,接入时不改变既有节奏。
    抖动是必须的 —— 下游抖动时,一批同时重试的调用会形成尖峰再把它打垮一次。

    rng 刻意不写成默认参数 `rng=random.uniform`:默认参数在定义时求值并绑死,
    测试就 patch 不动了。延迟到调用时取,测试才能注入固定抖动。
    """
    if rng is None:
        rng = random.uniform
    delay = min(policy.base * (2 ** retry_index), policy.max_delay)
    delay = max(0.0, delay)
    return delay + rng(0.0, delay * policy.jitter_ratio)


# ---------------------------------------------------------------- 缓存

class TTLCache:
    """极简 TTL + LRU 缓存。只缓存成功结果,失败永不入缓存。

    用 OrderedDict 做 LRU(与 utils/query_rewrite/pipeline.py 同风格),多一把锁保证
    web 后台线程安全。默认在组件里关闭 —— 调用点按需 cache=True。
    """

    def __init__(self, *, max_size: int = 512, ttl: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        from collections import OrderedDict
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._max_size = max(1, int(max_size))
        self._ttl = float(ttl)
        self._clock = clock
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bool, Any]:
        """返回 (命中?, 值)。过期视为未命中并顺手删除。"""
        now = self._clock()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return False, None
            expire_at, value = item
            if self._ttl > 0 and now >= expire_at:
                self._data.pop(key, None)
                return False, None
            self._data.move_to_end(key)
            return True, value

    def put(self, key: str, value: Any) -> None:
        expire_at = self._clock() + self._ttl
        with self._lock:
            self._data[key] = (expire_at, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# 按 (site, 是否启用) 共享缓存实例:同一调用点的多次调用要命中同一份缓存
_caches: dict[str, TTLCache] = {}
_caches_lock = threading.Lock()


def cache_for(site: str) -> TTLCache:
    with _caches_lock:
        c = _caches.get(site)
        if c is None:
            c = TTLCache(
                max_size=_config.get_int("cache_max_size", site=site),
                ttl=_config.get_float("cache_ttl_seconds", site=site),
            )
            _caches[site] = c
        return c


def reset_caches() -> None:
    """清空全部缓存实例(测试用)。"""
    with _caches_lock:
        _caches.clear()


# ---------------------------------------------------------------- 策略解析

def resolve_policy(site: str, *, retries=None, base=None, max_delay=None,
                   jitter_ratio=None, retry_after_cap=None,
                   timeout_budget=None) -> RetryPolicy:
    """显式 kwargs 优先,缺的从 config 按 site 取。调用点不传即全走配置。"""
    return RetryPolicy(
        retries=_config.get_int("default_max_retries", site=site) if retries is None else int(retries),
        base=_config.get_float("base_delay", site=site) if base is None else float(base),
        max_delay=_config.get_float("max_delay", site=site) if max_delay is None else float(max_delay),
        jitter_ratio=_config.get_float("jitter_ratio", site=site) if jitter_ratio is None else float(jitter_ratio),
        retry_after_cap=_config.get_float("retry_after_cap", site=site) if retry_after_cap is None else float(retry_after_cap),
        timeout_budget=_config.get_float("default_timeout_budget", site=site) if timeout_budget is None else float(timeout_budget),
    )


def _effective_retries(policy: RetryPolicy, idempotent: bool, site: str) -> int:
    """有副作用的调用不允许重放:强制 0 次重试,并留一条可追溯的告警日志。"""
    if idempotent:
        return max(0, policy.retries)
    if policy.retries > 0:
        log_event("idempotent_guard", site=site, level="warning",
                  configured=policy.retries, enforced=0)
    return 0


def _call_key(site: str, fn, args, kwargs) -> str:
    """缓存 key:调用点 + 函数 + 位置/关键字参数。用 repr 兜底,不要求参数可哈希。"""
    try:
        return f"{site}|{getattr(fn, '__qualname__', fn)}|{args!r}|{sorted(kwargs.items())!r}"
    except Exception:
        return f"{site}|{getattr(fn, '__qualname__', fn)}|{id(args)}"


def _degrade(site: str, fallback: Fallback, exc: BaseException, attempts: int,
             reason: str, *, labels=None) -> Any:
    """走降级路径。降级函数抛错则记事件后原样抛出(那是编程错误,不能静默)。"""
    log_event("degrade", site=site, level="error", reason=reason, attempts=attempts,
              err=type(exc).__name__, labels=",".join(f"{k}={v}" for k, v in (labels or {}).items()) or None)
    registry().record_degrade(site, reason=reason, labels=labels)
    try:
        return fallback(exc, attempts)
    except Exception as fb_exc:
        log_event("degrade_error", site=site, level="error", err=type(fb_exc).__name__)
        raise


def _maybe_alert(site: str, classified: Classified, exc: BaseException, *, labels=None) -> None:
    """确定性中的配置类错误(鉴权/参数/工具不存在)要告警:通常是代码或配置问题。"""
    if not classified.alert:
        return
    alert(site, classified.code, f"{type(exc).__name__}: {exc}", status=classified.status, kind=classified.kind.value)


# ---------------------------------------------------------------- 核心驱动

# call()/acall() 里属于「策略」的关键字,与「转发给 fn 的关键字」靠它区分。
# 只用于 acall 的 **policy 拆包;call() 的策略参数是显式具名的,不走这里。
_POLICY_KEYS = frozenset({
    "retries", "base", "max_delay", "jitter_ratio", "retry_after_cap",
    "timeout_budget", "idempotent", "cache", "cache_ttl", "cache_key",
    "breaker", "classify", "on_retry", "on_success", "labels",
})


def _prepare(site, fallback, breaker, policy_kwargs):
    """校验入参 + 解析策略 + 取熔断器。返回 (policy, breaker_obj 或 None)。"""
    if not site:
        raise ValueError("call() 必须提供 site(调用点标识),否则日志与指标无法区分来源")
    if fallback is None:
        raise ValueError(f"call(site={site!r}) 必须提供 fallback 降级函数:失败不能阻塞主流程")

    policy = resolve_policy(site, **policy_kwargs)
    br = breaker_for(site) if breaker else None
    return policy, br


def _cache_lookup(site, cache, cache_key, fn, args, kwargs, labels):
    """缓存命中则返回 (True, 值);否则 (False, key)。"""
    if not cache:
        return False, None
    key = cache_key(*args, **kwargs) if cache_key else _call_key(site, fn, args, kwargs)
    hit, value = cache_for(site).get(key)
    registry().record_cache(site, hit=hit, labels=labels)
    if hit:
        log_event("cache", site=site, level="info", hit="true")
        return True, value
    return False, key


def call(fn: Callable[..., T], *args, site: str, fallback: Fallback,
         retries: int | None = None, base: float | None = None,
         max_delay: float | None = None, jitter_ratio: float | None = None,
         retry_after_cap: float | None = None, timeout_budget: float | None = None,
         idempotent: bool = True, cache: bool = False, cache_ttl: float | None = None,
         cache_key: Callable[..., str] | None = None, breaker: bool = True,
         classify: Callable[[BaseException], Classified] | None = None,
         on_retry: Callable[[int, BaseException, float, Classified], None] | None = None,
         on_success: Callable[[Any], Any] | None = None,
         labels: dict | None = None, **kwargs) -> T:
    """同步入口。fn 是协程函数时会返回协程(委托异步驱动)。"""
    if inspect.iscoroutinefunction(fn):
        return _adrive(fn, args, kwargs, site=site, fallback=fallback,
                       retries=retries, base=base, max_delay=max_delay,
                       jitter_ratio=jitter_ratio, retry_after_cap=retry_after_cap,
                       timeout_budget=timeout_budget, idempotent=idempotent,
                       cache=cache, cache_ttl=cache_ttl, cache_key=cache_key,
                       breaker=breaker, classify=classify, on_retry=on_retry, labels=labels,
                       on_success=on_success)
    return _sdrive(fn, args, kwargs, site=site, fallback=fallback,
                   retries=retries, base=base, max_delay=max_delay,
                   jitter_ratio=jitter_ratio, retry_after_cap=retry_after_cap,
                   timeout_budget=timeout_budget, idempotent=idempotent,
                   cache=cache, cache_ttl=cache_ttl, cache_key=cache_key,
                   breaker=breaker, classify=classify, on_retry=on_retry, labels=labels,
                   on_success=on_success)


async def acall(fn: Callable[..., T], *args, site: str, fallback: Fallback, **policy) -> T:
    """异步入口(显式拼写,避免调用方关心 fn 是不是协程函数)。

    **policy 里属于策略的键会被取走,其余原样转发给 fn** —— 这样
    `acall(inner.ainvoke, payload, config=None, site=..., fallback=...)` 能自然工作,
    不必为了传一个 fn 参数去套 functools.partial。
    """
    fn_kwargs = {k: v for k, v in policy.items() if k not in _POLICY_KEYS}
    fn_policy = {k: v for k, v in policy.items() if k in _POLICY_KEYS}
    return await _adrive(fn, args, fn_kwargs, site=site, fallback=fallback, **fn_policy)


def _resolve_classifier(classify):
    return classify or _classify


def _apply_success_hook(result: Any, hook) -> Any:
    """成功后的可选加工钩子。

    存在的理由:有些调用点要先「解包/校验」结果才算真成功(例如把模型返回的
    原始文本解析成结构化对象,解析不出来仍算失败并该重试)。把校验放进 fn 内部会让
    「调用」与「校验」耦合到无法替换底座;放在这里,调用点就能把「原始调用」和
    「结果校验」分开表达。
    指标已在钩子之前记为成功——钩子抛错不改变这个事实,避免「失败数 > 调用数」这类
    自相矛盾的数字;异常原样上抛,由调用点决定怎么处理。
    """
    if hook is None:
        return result
    return hook(result)


def _note_retry(site, retry_index, effective, exc, classified, wait, elapsed, on_retry, labels):
    log_event("retry", site=site, level="warning", attempt=retry_index, total=effective,
              kind=classified.kind.value, code=classified.code,
              wait=round(wait, 3), elapsed=round(elapsed, 3), err=type(exc).__name__)
    registry().record_retry(site, wait=wait, kind=classified.kind, code=classified.code, labels=labels)
    if on_retry is not None:
        try:
            on_retry(retry_index, exc, wait, classified)
        except Exception as cb_exc:
            log_event("on_retry_error", site=site, level="warning", err=type(cb_exc).__name__)


def _sdrive(fn, args, kwargs, *, site, fallback, retries=None, base=None, max_delay=None,
            jitter_ratio=None, retry_after_cap=None, timeout_budget=None,
            idempotent=True, cache=False, cache_ttl=None, cache_key=None, breaker=True,
            classify=None, on_retry=None, labels=None, on_success=None) -> Any:
    policy, br = _prepare(site, fallback, breaker,
                          dict(retries=retries, base=base, max_delay=max_delay,
                               jitter_ratio=jitter_ratio, retry_after_cap=retry_after_cap,
                               timeout_budget=timeout_budget))
    if not _config.get_bool("enabled"):
        return fn(*args, **kwargs)

    hit, cached_or_key = _cache_lookup(site, cache, cache_key, fn, args, kwargs, labels)
    if hit:
        return cached_or_key
    cache_key_value = cached_or_key

    classifier = _resolve_classifier(classify)
    effective = _effective_retries(policy, idempotent, site)

    # 熔断短路:直接降级,fn 根本不调用
    if br is not None:
        ok, left = br.allow()
        if not ok:
            registry().record_breaker(site, rejected=True, labels=labels)
            log_event("breaker_reject", site=site, level="warning",
                      state="open", cooldown_left=round(left, 1))
            return _degrade(site, fallback, CircuitOpenError(site, left), 0, "circuit_open", labels=labels)

    start = _clock()
    attempts = 0
    last_exc: BaseException | None = None
    last_classified: Classified | None = None
    registry().record_call(site, labels=labels)

    for retry_index in range(0, effective + 1):
        if retry_index > 0:
            elapsed = _clock() - start
            # Retry-After 优先于算出来的退避;两种都要封顶,否则交互式问答会被卡死
            ra = last_classified.retry_after if last_classified else None
            wait = ra if ra is not None else backoff_delay(retry_index - 1, policy)
            wait = min(wait, policy.retry_after_cap)
            if policy.timeout_budget is not None and elapsed + wait > policy.timeout_budget:
                return _degrade(site, fallback, last_exc, attempts, "timeout_budget", labels=labels)
            _note_retry(site, retry_index, effective, last_exc, last_classified, wait, elapsed, on_retry, labels)
            _sleep(wait)

        attempts = retry_index + 1
        registry().record_attempt(site, labels=labels)
        t0 = _clock()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:                     # 只吞 Exception:CancelledError 必须上抛
            last_exc = e
            last_classified = classifier(e)
            _maybe_alert(site, last_classified, e, labels=labels)
            if last_classified.kind is Kind.TRANSIENT and retry_index < effective:
                if br is not None:
                    br.note_failure(last_classified.kind)
                continue
            if br is not None:
                br.note_failure(last_classified.kind)
            registry().record_failure(site, kind=last_classified.kind, code=last_classified.code,
                                      attempts=attempts, duration=_clock() - t0, labels=labels)
            return _degrade(site, fallback, e, attempts, last_classified.code, labels=labels)

        duration = _clock() - t0
        registry().record_success(site, attempts=attempts, duration=duration, labels=labels)
        if br is not None:
            br.note_success()
        log_event("success", site=site, level="debug", attempts=attempts, duration=round(duration, 3))
        out = _apply_success_hook(result, on_success)
        if cache and cache_key_value is not None:
            cache_for(site).put(cache_key_value, out)
        return out

    # 理论不可达(循环内必 return);保险降级
    return _degrade(site, fallback, last_exc or RuntimeError("unreachable"),
                    attempts, "exhausted", labels=labels)


async def _adrive(fn, args, kwargs, *, site, fallback, retries=None, base=None, max_delay=None,
                  jitter_ratio=None, retry_after_cap=None, timeout_budget=None,
                  idempotent=True, cache=False, cache_ttl=None, cache_key=None, breaker=True,
                  classify=None, on_retry=None, labels=None, on_success=None) -> Any:
    policy, br = _prepare(site, fallback, breaker,
                          dict(retries=retries, base=base, max_delay=max_delay,
                               jitter_ratio=jitter_ratio, retry_after_cap=retry_after_cap,
                               timeout_budget=timeout_budget))
    if not _config.get_bool("enabled"):
        return await fn(*args, **kwargs)

    hit, cached_or_key = _cache_lookup(site, cache, cache_key, fn, args, kwargs, labels)
    if hit:
        return cached_or_key
    cache_key_value = cached_or_key

    classifier = _resolve_classifier(classify)
    effective = _effective_retries(policy, idempotent, site)

    if br is not None:
        ok, left = br.allow()
        if not ok:
            registry().record_breaker(site, rejected=True, labels=labels)
            log_event("breaker_reject", site=site, level="warning",
                      state="open", cooldown_left=round(left, 1))
            return _degrade(site, fallback, CircuitOpenError(site, left), 0, "circuit_open", labels=labels)

    start = _clock()
    attempts = 0
    last_exc: BaseException | None = None
    last_classified: Classified | None = None
    registry().record_call(site, labels=labels)

    for retry_index in range(0, effective + 1):
        if retry_index > 0:
            elapsed = _clock() - start
            ra = last_classified.retry_after if last_classified else None
            wait = ra if ra is not None else backoff_delay(retry_index - 1, policy)
            wait = min(wait, policy.retry_after_cap)
            if policy.timeout_budget is not None and elapsed + wait > policy.timeout_budget:
                return _degrade(site, fallback, last_exc, attempts, "timeout_budget", labels=labels)
            _note_retry(site, retry_index, effective, last_exc, last_classified, wait, elapsed, on_retry, labels)
            await _sleep_async(wait)

        attempts = retry_index + 1
        registry().record_attempt(site, labels=labels)
        t0 = _clock()
        try:
            # 异步路径的预算真正生效:wait_for 能取消在途请求(同步路径做不到)
            if policy.timeout_budget is not None:
                remaining = policy.timeout_budget - (_clock() - start)
                if remaining <= 0:
                    return _degrade(site, fallback, last_exc or TimeoutError("budget exhausted"),
                                    attempts - 1, "timeout_budget", labels=labels)
                result = await asyncio.wait_for(fn(*args, **kwargs), remaining)
            else:
                result = await fn(*args, **kwargs)
        except Exception as e:                     # 只吞 Exception:CancelledError 必须上抛
            last_exc = e
            last_classified = classifier(e)
            _maybe_alert(site, last_classified, e, labels=labels)
            if last_classified.kind is Kind.TRANSIENT and retry_index < effective:
                if br is not None:
                    br.note_failure(last_classified.kind)
                continue
            if br is not None:
                br.note_failure(last_classified.kind)
            registry().record_failure(site, kind=last_classified.kind, code=last_classified.code,
                                      attempts=attempts, duration=_clock() - t0, labels=labels)
            return _degrade(site, fallback, e, attempts, last_classified.code, labels=labels)

        duration = _clock() - t0
        registry().record_success(site, attempts=attempts, duration=duration, labels=labels)
        if br is not None:
            br.note_success()
        log_event("success", site=site, level="debug", attempts=attempts, duration=round(duration, 3))
        out = _apply_success_hook(result, on_success)
        if cache and cache_key_value is not None:
            cache_for(site).put(cache_key_value, out)
        return out

    return _degrade(site, fallback, last_exc or RuntimeError("unreachable"),
                    attempts, "exhausted", labels=labels)


# ---------------------------------------------------------------- 装饰器

def guard(*, site: str | None = None, fallback: Fallback, **policy):
    """把 call()/acall() 包成装饰器,按 fn 是否协程函数自动选驱动。

    必须用 functools.wraps:agent_tools.py 是 @tool 套在 @with_retry 外层,
    __name__/__doc__/__signature__ 丢了会让 langchain 的工具 schema 直接崩。
    """

    def _decorate(fn):
        resolved_site = site or f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', '?')}"

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def _aw(*args, **kwargs):
                return await _adrive(fn, args, kwargs, site=resolved_site, fallback=fallback, **policy)

            return _aw

        @functools.wraps(fn)
        def _w(*args, **kwargs):
            return _sdrive(fn, args, kwargs, site=resolved_site, fallback=fallback, **policy)

        return _w

    return _decorate


# ---------------------------------------------------------------- 并发

def map_partial(fns: Sequence[Callable[[], T]], *, max_workers: int = 4) -> tuple[list, list]:
    """并发跑多个「各自带降级」的调用,返回 (成功值列表, 异常列表)。

    单个失败不影响其他(部分成功策略):能用的先用,不等所有调用都成功。
    传入的通常是 functools.partial(call, ..., fallback=...) —— 已经自带降级,
    所以这里的异常列表一般只会收到「降级函数自己坏了」这种真异常。永不抛。
    """
    results: list = []
    errors: list = []
    if not fns:
        return results, errors

    from concurrent.futures import ThreadPoolExecutor, as_completed

    workers = max(1, min(int(max_workers), len(fns)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(f): i for i, f in enumerate(fns)}
        ordered: list = [None] * len(fns)
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                ordered[idx] = fut.result()
            except Exception as e:
                errors.append(e)
        results = [v for v in ordered if v is not None]
    return results, errors
