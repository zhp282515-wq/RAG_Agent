"""统一失败处理组件:重试 + 退避抖动 + 熔断 + 降级 + 缓存 + 观测。

全项目所有外部调用(LLM、工具、HTTP、向量库)共用这一套。调用点只提供「降级函数 +
调用点标识」,其余按 config/resilience.yml 统一处理。使用方式与调参见同目录 README.md。

    from utils.resilience import call, guard, Kind, classify

    result = call(fetch, site="weather", fallback=lambda exc, n: "暂时查不到天气")
"""
from utils.resilience.breaker import (
    BreakerPolicy,
    CircuitBreaker,
    CircuitOpenError,
    State,
    breaker_for,
    breaker_snapshots,
    reset_breakers,
    set_default_clock,
)
from utils.resilience.classify import (
    Classified,
    Kind,
    classify,
    clear_registrations,
    deterministic_status_codes,
    is_transient,
    register_exception_kind,
    restore_registrations,
    snapshot_registrations,
    transient_status_codes,
)
from utils.resilience.core import (
    Outcome,
    RetryPolicy,
    TTLCache,
    acall,
    backoff_delay,
    cache_for,
    call,
    get_clock,
    guard,
    map_partial,
    reset_caches,
    resolve_policy,
    set_clock,
)
from utils.resilience.observe import (
    alert,
    alert_suppressed,
    dump_metrics,
    log_event,
    metrics_snapshot,
    register_alert_hook,
    registry,
    reset_alert_state,
    reset_metrics,
    unregister_alert_hook,
)

__all__ = [
    # 调用入口
    "call", "acall", "guard", "map_partial",
    # 策略与结果
    "RetryPolicy", "Outcome", "resolve_policy", "backoff_delay",
    # 缓存
    "TTLCache", "cache_for", "reset_caches",
    # 时钟
    "set_clock", "get_clock",
    # 分类
    "Kind", "Classified", "classify", "is_transient",
    "register_exception_kind", "clear_registrations",
    "snapshot_registrations", "restore_registrations",
    "deterministic_status_codes", "transient_status_codes",
    # 熔断
    "State", "BreakerPolicy", "CircuitBreaker", "CircuitOpenError",
    "breaker_for", "breaker_snapshots", "reset_breakers", "set_default_clock",
    # 观测
    "log_event", "alert", "register_alert_hook", "unregister_alert_hook",
    "alert_suppressed", "reset_alert_state",
    "metrics_snapshot", "reset_metrics", "dump_metrics", "registry",
]
