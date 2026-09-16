"""失败处理组件的配置读取层。

配置来源与优先级(高 → 低):
  1. 调用点显式传入的 kwargs(在 core.call/guard 里收口,本模块不管)
  2. 进程环境变量 RESILIENCE_<大写键名>(便于调试/测试临时压参数,不改文件)
       - 全局键:      RESILIENCE_BASE_DELAY=0.1
       - 按调用点键:  RESILIENCE_SITES_REWRITER_MAX_RETRIES=0
  3. config/resilience.yml 的 sites.<调用点标识>.<键>(仅该调用点生效)
  4. config/resilience.yml 的顶层键
  5. 本文件内置 _DEFAULTS

**所有数值都能在这里或 yml 里调**,不写死。缺键、类型不对、yml 整个读不到,都回退
内置默认并记一条 warning —— 配置问题不能拖垮调用链路,更不能让「兜底层」自己先炸。

不做运行时热加载:resilience.yml 是静态兜底(与 model.yml / rag.yml 一致),改完重启生效。
"""
from __future__ import annotations

import os

from utils.config_tool import resilience_conf
from utils.logger_tool import logger

# 内置默认值与 config/resilience.yml 的注释保持同一套语义,改一处要同步另一处
_DEFAULTS: dict = {
    # 总开关
    "enabled": True,
    # 重试与退避
    "default_max_retries": 2,
    "base_delay": 0.5,
    "max_delay": 8.0,
    "jitter_ratio": 0.5,
    "retry_after_cap": 60.0,
    # 分场景重试预算
    "interactive_max_retries": 1,
    "background_max_retries": 3,
    "streaming_max_retries": 0,
    "default_timeout_budget": 20.0,
    # 熔断
    "breaker_enabled": True,
    "breaker_failure_threshold": 5,
    "breaker_window_seconds": 60.0,
    "breaker_cooldown_seconds": 30.0,
    "breaker_probe_calls": 1,
    "breaker_open_backoff_factor": 2.0,
    "breaker_max_cooldown_seconds": 300.0,
    # 缓存
    "cache_enabled": False,
    "cache_ttl_seconds": 300.0,
    "cache_max_size": 512,
    # 观测
    "log_enabled": True,
    "alert_enabled": True,
}

# 这些键属于全局配置,不允许出现在 sites.<site> 段里(写了也忽略并告警)
_GLOBAL_ONLY = frozenset({"enabled", "sites"})

# site 段里键名 → 顶层键名 的别名。调用点用「max_retries」比「default_max_retries」自然,
# 但两者是同一个东西,这里做一次映射,避免 yml 里出现两个名字表达同一含义。
_SITE_ALIASES = {
    "max_retries": "default_max_retries",
    "timeout_budget": "default_timeout_budget",
}

# 反向映射:规范键 → 它在 site 段/环境变量里被允许的别名(含自身)。
# 让 get() 对别名也生效,而不是只在 resolve_site 里补一次 —— 否则
# get('default_max_retries', site=...) 和 resolve_site(site) 会给出不一致的答案。
_ALIAS_OF: dict[str, tuple[str, ...]] = {}
for _alias, _real in _SITE_ALIASES.items():
    _ALIAS_OF.setdefault(_real, ())
    _ALIAS_OF[_real] += (_alias,)


def _site_key_names(key: str) -> tuple[str, ...]:
    """该规范键在 site 段/环境变量里所有可接受的写法(别名在前,规范键兜底)。"""
    return _ALIAS_OF.get(key, ()) + (key,)


def _top() -> dict:
    """取 resilience.yml 顶层;文件空/损坏(loader 已兜成 {})一律当空 dict。"""
    return resilience_conf if isinstance(resilience_conf, dict) else {}


def _site_section(site: str) -> dict:
    sec = _top().get("sites")
    if not isinstance(sec, dict):
        return {}
    one = sec.get(site)
    return one if isinstance(one, dict) else {}


def _coerce(key: str, raw):
    """按内置默认值的类型强制转换;转不动返回哨兵 _BAD。"""
    default = _DEFAULTS[key]
    try:
        if isinstance(default, bool):
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, int):
            return int(raw)
        return raw
    except (TypeError, ValueError):
        return _BAD


class _Bad:
    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        return "<invalid>"


_BAD = _Bad()


def get(key: str, *, site: str | None = None):
    """读一项配置。site 非空时「按调用点覆盖」优先于顶层值。

    解析顺序:env(site) > env(global) > yml.site > yml.top > 内置默认。
    """
    if key not in _DEFAULTS:
        # 打错键名是最常见的配置事故,显式报出来而不是静默 None
        logger.warning(f"resilience.config: 未知配置键 {key!r},已按 None 处理")
        return None
    default = _DEFAULTS[key]

    # 1) 环境变量:按调用点的形式优先(更具体)。别名与规范键都认。
    if site:
        for name in _site_key_names(key):
            env = f"RESILIENCE_SITES_{site.upper()}_{name.upper()}"
            raw = os.getenv(env)
            if raw not in (None, ""):
                v = _coerce(key, raw)
                if v is not _BAD:
                    return v
                logger.warning(f"resilience.config: {env}={raw!r} 无法转换,忽略")
    raw = os.getenv(f"RESILIENCE_{key.upper()}")
    if raw not in (None, ""):
        v = _coerce(key, raw)
        if v is not _BAD:
            return v
        logger.warning(f"resilience.config: RESILIENCE_{key.upper()}={raw!r} 无法转换,忽略")

    # 2) yml 的 sites.<site> 段(别名优先,便于 yml 里写 max_retries)
    if site:
        sec = _site_section(site)
        for name in _site_key_names(key):
            if name in sec:
                v = _coerce(key, sec[name])
                if v is not _BAD:
                    return v
                logger.warning(
                    f"resilience.config: sites.{site}.{name}={sec[name]!r} "
                    f"无法转为 {type(default).__name__},用默认"
                )

    # 3) yml 顶层
    top = _top()
    if key in top:
        v = _coerce(key, top[key])
        if v is not _BAD:
            return v
        logger.warning(f"resilience.config: {key}={top[key]!r} 无法转为 {type(default).__name__},用默认")

    return default


def get_float(key: str, *, site: str | None = None) -> float:
    return float(get(key, site=site))


def get_int(key: str, *, site: str | None = None) -> int:
    return int(get(key, site=site))


def get_bool(key: str, *, site: str | None = None) -> bool:
    return bool(get(key, site=site))


def resolve_site(site: str) -> dict:
    """把一个调用点的全部策略摊平成 dict,供 core 直接使用。

    只含「策略类」键;enabled/sites 这类全局开关不在其中。调用点不传 kwargs 时,
    core 就照这份结果跑,即「调用点只需提供 site + 降级函数」。
    """
    out: dict = {}
    for key in _DEFAULTS:
        if key in _GLOBAL_ONLY:
            continue
        out[key] = get(key, site=site)

    # 全局开关类键按调用点合并进来,便于 core 一处读取
    out["enabled"] = get_bool("enabled")
    return out


def site_names() -> list[str]:
    """yml 里已配置的调用点标识(供 README / 排查用)。"""
    sec = _top().get("sites")
    return sorted(sec.keys()) if isinstance(sec, dict) else []
