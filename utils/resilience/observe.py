"""可观测性:进程内指标注册表 + 结构化日志 + 告警。

三条设计约束:
  1. **锁内只做 O(1) 的计数/浮点累加**,绝不在锁里写日志、发 HTTP、调回调。
     web/server.py 在后台线程里调工具,锁里做 I/O 会把并发调用串行化。
  2. **比值与均值在 snapshot() 时算**,不边写边算 —— 存原始计数才不会出现
     「成功率和失败率加起来不等于 1」这类自相矛盾的数字。
  3. **告警要能被外部接管**:除了写独立日志文件,还留 register_alert_hook() 挂接点,
     将来接邮件/webhook 不用改这里。

指标是**进程内、尽力而为、不持久化**的:进程重启即清零,多 worker 部署各算各的。
定位是「出问题时能立刻看出是哪个调用点在烂」,不是计费级统计。
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, deque

from utils.logger_tool import get_logger

# 主日志:每次调用/重试/降级/熔断各一行
logger = get_logger()

# 告警走独立文件:logs/resilience_alert_YYYY-MM-DD.log
# get_logger 按 name 缓存 handler,所以这里天然拿到一个独立的按天文件,无需额外配置。
# 分开的理由:配置类错误(401/403/参数错)是「需要人来看」的信号,不该淹在常规日志里。
_alert_logger = get_logger("resilience_alert")


# ---------------------------------------------------------------- 日志

# 结构化日志统一前缀,便于 grep:【resilience】 event=retry site=rewriter ...
_LOG_PREFIX = "【resilience】"


def _fmt_fields(fields: dict) -> str:
    """把字段渲染成稳定的 key=value 串(值带空格时加引号,保持可切分)。"""
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v)
        if " " in s or "=" in s:
            s = f'"{s}"'
        parts.append(f"{k}={s}")
    return " ".join(parts)


def log_event(event: str, *, site: str = "", level: str = "info", **fields) -> None:
    """输出一行结构化事件日志。log_enabled=false 时整体静默。

    level: debug / info / warning / error
    """
    from utils.resilience import config as _config

    if not _config.get_bool("log_enabled"):
        return
    msg = f"{_LOG_PREFIX} event={event}"
    if site:
        msg += f" site={site}"
    extra = _fmt_fields(fields)
    if extra:
        msg += f" {extra}"
    getattr(logger, level, logger.info)(msg)


# ---------------------------------------------------------------- 脱敏

# 只脱敏「看起来像凭据」的片段。宁可漏掉也不要把正常文本搅乱:
# 每条规则都要求足够长的随机串,不会误伤普通中文/英文句子。
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\b\s*[=:]\s*\S+"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)


def _redact(text: object) -> str:
    """抹掉消息里的疑似密钥,供降级/告警日志使用(输入摘要必须脱敏)。"""
    s = str(text)
    for pat in _SECRET_PATTERNS:
        s = pat.sub("<redacted>", s)
    return s


# ---------------------------------------------------------------- 告警

_alert_lock = threading.Lock()
_alert_hooks: list = []
# (site, code) → 上次告警时间,用于限流。key 失效时每个请求都会失败,
# 不限流的话日志会被同一件事刷爆。
_alert_last: dict[tuple[str, str], float] = {}
_ALERT_MIN_INTERVAL = 60.0
# 记录被限流压掉的数量,便于事后知道「告警沉默不代表没发生」
_alert_suppressed = 0


def register_alert_hook(fn) -> None:
    """注册一个告警回调:fn(site, code, message, detail)。异常被吞,不影响主流程。"""
    with _alert_lock:
        if fn not in _alert_hooks:
            _alert_hooks.append(fn)


def unregister_alert_hook(fn) -> None:
    with _alert_lock:
        if fn in _alert_hooks:
            _alert_hooks.remove(fn)


def alert(site: str, code: str, message: str, **detail) -> None:
    """发一条告警。按 (site, code) 限流 60s,避免同一配置事故刷屏。

    配置类错误(鉴权/参数/工具不存在)走这里 —— 这类通常意味着代码或配置有问题,
    不是「等一会儿就好」的瞬时故障,所以必须让人看见。
    """
    global _alert_suppressed
    from utils.resilience import config as _config

    if not _config.get_bool("alert_enabled"):
        return

    now = time.monotonic()
    key = (site, code)
    with _alert_lock:
        last = _alert_last.get(key)
        if last is not None and now - last < _ALERT_MIN_INTERVAL:
            _alert_suppressed += 1
            return
        _alert_last[key] = now
        hooks = list(_alert_hooks)

    safe_msg = _redact(message)
    fields = _fmt_fields({k: _redact(v) for k, v in detail.items()})
    line = f"{_LOG_PREFIX} event=alert site={site} code={code} message=\"{safe_msg}\""
    if fields:
        line += f" {fields}"
    # 告警不受 log_enabled 影响:关掉常规日志不等于不要告警
    _alert_logger.error(line)
    logger.error(line)

    for fn in hooks:
        try:
            fn(site, code, message, detail)
        except Exception as e:  # 钩子坏了不能拖垮调用方
            logger.warning(f"resilience.observe: 告警钩子 {fn!r} 抛异常({type(e).__name__}): {e}")


def alert_suppressed() -> int:
    """被限流压掉的告警条数(测试/排查用)。"""
    with _alert_lock:
        return _alert_suppressed


def reset_alert_state() -> None:
    """清空告警限流状态与钩子(测试用)。"""
    global _alert_suppressed
    with _alert_lock:
        _alert_last.clear()
        _alert_hooks.clear()
        _alert_suppressed = 0


# ---------------------------------------------------------------- 指标

class SiteMetrics:
    """单个调用点的累计指标。字段只增不减,比值留给 snapshot() 算。"""

    __slots__ = (
        "site", "calls", "attempts", "successes", "failures",
        "transient", "deterministic", "unknown",
        "retries", "retry_histogram",
        "degradations", "degradation_reasons",
        "cache_hits", "cache_misses",
        "breaker_opens", "breaker_rejections",
        "retry_wait_total", "duration_total",
        "deterministic_by_code",
    )

    def __init__(self, site: str) -> None:
        self.site = site
        self.calls = 0              # 逻辑调用数(不含重试)
        self.attempts = 0           # 实际调用次数(含重试)
        self.successes = 0
        self.failures = 0           # 最终失败(含降级)
        self.transient = 0
        self.deterministic = 0
        self.unknown = 0
        self.retries = 0
        self.retry_histogram: Counter = Counter()   # 每次调用用掉的尝试数 → 次数
        self.degradations = 0
        self.degradation_reasons: Counter = Counter()
        self.cache_hits = 0
        self.cache_misses = 0
        self.breaker_opens = 0
        self.breaker_rejections = 0
        self.retry_wait_total = 0.0
        self.duration_total = 0.0
        self.deterministic_by_code: Counter = Counter()


class MetricsRegistry:
    """进程内指标注册表。一把锁,所有变更为 O(1),snapshot() 在锁内深拷贝。"""

    def __init__(self) -> None:
        self._data: dict[str, SiteMetrics] = {}
        self._lock = threading.Lock()

    def _get(self, key: str) -> SiteMetrics:
        m = self._data.get(key)
        if m is None:
            m = SiteMetrics(key)
            self._data[key] = m
        return m

    @staticmethod
    def _key(site: str, labels: dict | None) -> str:
        """有 labels 时拼成 site|k=v|k=v,可按模型/租户/工具维度切分。"""
        if not labels:
            return site
        parts = [f"{k}={labels[k]}" for k in sorted(labels)]
        return f"{site}|" + "|".join(parts)

    def record_call(self, site: str, *, labels: dict | None = None) -> None:
        with self._lock:
            self._get(self._key(site, labels)).calls += 1

    def record_attempt(self, site: str, *, kind=None, code: str = "",
                       labels: dict | None = None) -> None:
        with self._lock:
            m = self._get(self._key(site, labels))
            m.attempts += 1

    def record_retry(self, site: str, *, wait: float, kind=None, code: str = "",
                     labels: dict | None = None) -> None:
        with self._lock:
            m = self._get(self._key(site, labels))
            m.retries += 1
            m.retry_wait_total += float(wait)

    def record_success(self, site: str, *, attempts: int, duration: float,
                       labels: dict | None = None) -> None:
        with self._lock:
            m = self._get(self._key(site, labels))
            m.successes += 1
            m.retry_histogram[int(attempts)] += 1
            m.duration_total += float(duration)

    def record_failure(self, site: str, *, kind=None, code: str = "", attempts: int = 0,
                       duration: float = 0.0, labels: dict | None = None) -> None:
        with self._lock:
            m = self._get(self._key(site, labels))
            m.failures += 1
            m.retry_histogram[int(attempts)] += 1
            m.duration_total += float(duration)
            name = getattr(kind, "value", kind)
            if name == "transient":
                m.transient += 1
            elif name == "deterministic":
                m.deterministic += 1
                if code:
                    m.deterministic_by_code[code] += 1
            else:
                m.unknown += 1

    def record_degrade(self, site: str, *, reason: str, labels: dict | None = None) -> None:
        with self._lock:
            m = self._get(self._key(site, labels))
            m.degradations += 1
            m.degradation_reasons[reason or "unknown"] += 1

    def record_cache(self, site: str, *, hit: bool, labels: dict | None = None) -> None:
        with self._lock:
            m = self._get(self._key(site, labels))
            if hit:
                m.cache_hits += 1
            else:
                m.cache_misses += 1

    def record_breaker(self, site: str, *, opened: bool = False, rejected: bool = False,
                       labels: dict | None = None) -> None:
        with self._lock:
            m = self._get(self._key(site, labels))
            if opened:
                m.breaker_opens += 1
            if rejected:
                m.breaker_rejections += 1

    @staticmethod
    def _rate(num: int, den: int) -> float:
        return round(num / den, 4) if den else 0.0

    def snapshot(self) -> dict:
        """导出指标快照。锁内深拷贝,调用方可随意序列化。"""
        with self._lock:
            sites = {}
            for key, m in self._data.items():
                total_kind = m.transient + m.deterministic + m.unknown
                outcomes = m.successes + m.failures
                sites[key] = {
                    "calls": m.calls,
                    "attempts": m.attempts,
                    "successes": m.successes,
                    "failures": m.failures,
                    "success_rate": self._rate(m.successes, outcomes),
                    "failure_rate": self._rate(m.failures, outcomes),
                    "transient": m.transient,
                    "deterministic": m.deterministic,
                    "unknown": m.unknown,
                    "transient_ratio": self._rate(m.transient, total_kind),
                    "deterministic_ratio": self._rate(m.deterministic, total_kind),
                    "retries": m.retries,
                    "retry_histogram": {str(k): v for k, v in sorted(m.retry_histogram.items())},
                    "avg_backoff": round(m.retry_wait_total / m.retries, 4) if m.retries else 0.0,
                    "avg_duration": round(m.duration_total / outcomes, 4) if outcomes else 0.0,
                    "degradations": m.degradations,
                    "degradation_reasons": dict(m.degradation_reasons),
                    "cache_hits": m.cache_hits,
                    "cache_misses": m.cache_misses,
                    "breaker_opens": m.breaker_opens,
                    "breaker_rejections": m.breaker_rejections,
                    "deterministic_by_code": dict(m.deterministic_by_code),
                }
            tot_calls = sum(m.calls for m in self._data.values())
            tot_ok = sum(m.successes for m in self._data.values())
            tot_bad = sum(m.failures for m in self._data.values())
            return {
                "sites": sites,
                "totals": {
                    "calls": tot_calls,
                    "successes": tot_ok,
                    "failures": tot_bad,
                    "success_rate": self._rate(tot_ok, tot_ok + tot_bad),
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._data.clear()

    def delete(self, site: str, *, labels: dict | None = None) -> None:
        """删掉某个调用点的指标(测试隔离用)。"""
        with self._lock:
            self._data.pop(self._key(site, labels), None)


_registry = MetricsRegistry()


def metrics_snapshot() -> dict:
    return _registry.snapshot()


def reset_metrics() -> None:
    _registry.reset()


def delete_metrics(site: str, *, labels: dict | None = None) -> None:
    _registry.delete(site, labels=labels)


def registry() -> MetricsRegistry:
    """取底层注册表(core 直接用它记录,O(1) 无中间层)。"""
    return _registry


def dump_metrics() -> str:
    """snapshot 的 JSON 串(排查时手动调用)。"""
    return json.dumps(metrics_snapshot(), ensure_ascii=False, indent=2)
