"""配置解析与可观测性用例。

配置这一层最容易出「静默用了错误的值」这类事故,所以这里断言的重点不是「能读到」,
而是**优先级顺序**和**坏值回退**:yml 顶层 < sites 段 < 环境变量 < 显式 kwargs。
"""
from __future__ import annotations

import pytest

from utils.resilience import config as rconf


class TestConfigPriority:
    def test_global_value(self):
        assert rconf.get_float("base_delay") == pytest.approx(0.5)

    def test_site_section_overrides_global(self):
        # rag.yml 里 rewriter 段把 base_delay 压到 0.2
        assert rconf.get_float("base_delay", site="rewriter") == pytest.approx(0.2)
        assert rconf.get_float("base_delay", site="unknown_site") == pytest.approx(0.5)

    def test_alias_key_max_retries(self):
        """yml 里写 max_retries,与规范键 default_max_retries 必须等价。"""
        assert rconf.get_int("default_max_retries", site="rewriter") == 1
        assert rconf.get_int("default_max_retries", site="get_weather") == 3

    def test_env_overrides_site(self, monkeypatch):
        monkeypatch.setenv("RESILIENCE_SITES_REWRITER_MAX_RETRIES", "0")
        assert rconf.get_int("default_max_retries", site="rewriter") == 0

    def test_env_global_overrides_yml(self, monkeypatch):
        monkeypatch.setenv("RESILIENCE_BASE_DELAY", "9.5")
        assert rconf.get_float("base_delay", site="rewriter") == pytest.approx(9.5)

    def test_env_site_beats_env_global(self, monkeypatch):
        monkeypatch.setenv("RESILIENCE_BASE_DELAY", "9.5")
        monkeypatch.setenv("RESILIENCE_SITES_REWRITER_BASE_DELAY", "1.25")
        assert rconf.get_float("base_delay", site="rewriter") == pytest.approx(1.25)

    def test_explicit_kwargs_beat_everything(self, monkeypatch):
        """call() 显式传参最高优先(由 core.resolve_policy 收口)。"""
        from utils.resilience.core import resolve_policy
        monkeypatch.setenv("RESILIENCE_BASE_DELAY", "9.5")
        p = resolve_policy("rewriter", base=3.0)
        assert p.base == pytest.approx(3.0)
        # 没显式传的仍然按 site 配置走
        assert p.retries == 1


class TestConfigRobustness:
    def test_unknown_key_returns_none(self):
        assert rconf.get("no_such_setting_at_all") is None

    def test_bad_type_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("RESILIENCE_BASE_DELAY", "not-a-number")
        assert rconf.get_float("base_delay") == pytest.approx(0.5)

    def test_string_bool_coercion(self, monkeypatch):
        monkeypatch.setenv("RESILIENCE_CACHE_ENABLED", "yes")
        assert rconf.get_bool("cache_enabled") is True
        monkeypatch.setenv("RESILIENCE_CACHE_ENABLED", "off")
        assert rconf.get_bool("cache_enabled") is False

    def test_resolve_site_is_complete(self):
        site = rconf.resolve_site("rewriter")
        for key in ("default_max_retries", "base_delay", "max_delay", "jitter_ratio",
                    "retry_after_cap", "breaker_failure_threshold", "breaker_enabled",
                    "cache_enabled", "log_enabled", "enabled"):
            assert key in site, f"resolve_site 缺少 {key}"

    def test_resolve_site_respects_site_override(self):
        assert rconf.resolve_site("summarization")["breaker_cooldown_seconds"] == pytest.approx(300.0)
        assert rconf.resolve_site("nobody")["breaker_cooldown_seconds"] == pytest.approx(30.0)

    def test_site_names_lists_configured_sites(self):
        names = rconf.site_names()
        assert "rewriter" in names and "summarization" in names

    def test_missing_yml_does_not_break_import(self):
        """resilience.yml 缺失时必须静默回退内置默认,而不是拖垮全项目 import。"""
        from utils.config_tool import get_resilience_config
        assert get_resilience_config("path\\that\\does\\not\\exist.yml") == {}


class TestMetrics:
    def test_snapshot_has_expected_shape(self):
        from utils.resilience import call, metrics_snapshot

        call(lambda: "v", site="m1", fallback=lambda e, n: "FB")
        snap = metrics_snapshot()
        assert "sites" in snap and "totals" in snap
        site = snap["sites"]["m1"]
        for key in ("calls", "attempts", "successes", "failures", "success_rate",
                    "transient", "deterministic", "unknown", "retries",
                    "retry_histogram", "avg_backoff", "avg_duration",
                    "degradations", "degradation_reasons", "cache_hits",
                    "breaker_opens", "breaker_rejections", "deterministic_by_code"):
            assert key in site, f"快照缺少 {key}"

    def test_success_and_failure_rates_are_consistent(self, sleep_rec, no_jitter):
        from utils.resilience import call, metrics_snapshot

        call(lambda: "v", site="m2", fallback=lambda e, n: "FB")
        call(lambda: (_ for _ in ()).throw(TimeoutError("x")),
             site="m2", fallback=lambda e, n: "FB", retries=0)
        snap = metrics_snapshot()["sites"]["m2"]
        assert snap["success_rate"] + snap["failure_rate"] == pytest.approx(1.0)

    def test_deterministic_errors_grouped_by_code(self, sleep_rec, no_jitter):
        from utils.resilience import call, metrics_snapshot

        def bad():
            import requests
            resp = requests.Response()
            resp.status_code = 422
            resp.headers = {}
            raise requests.HTTPError("422", response=resp)

        call(bad, site="m3", fallback=lambda e, n: "FB", retries=0)
        snap = metrics_snapshot()["sites"]["m3"]
        assert snap["deterministic_by_code"] == {"http_422": 1}

    def test_metrics_isolated_per_site(self):
        from utils.resilience import call, metrics_snapshot

        call(lambda: "v", site="mA", fallback=lambda e, n: "FB")
        call(lambda: "v", site="mB", fallback=lambda e, n: "FB")
        sites = metrics_snapshot()["sites"]
        assert sites["mA"]["calls"] == 1 and sites["mB"]["calls"] == 1


class TestAlerts:
    def test_alert_rate_limited_per_site_and_code(self):
        from utils.resilience import alert, alert_suppressed, register_alert_hook

        fired = []
        register_alert_hook(lambda site, code, msg, detail: fired.append(code))
        for _ in range(5):
            alert("a1", "auth", "bad key")
        assert len(fired) == 1, "同一 (site, code) 应当被限流"
        assert alert_suppressed() == 4

    def test_distinct_codes_not_limited_together(self):
        from utils.resilience import alert, register_alert_hook

        fired = []
        register_alert_hook(lambda site, code, msg, detail: fired.append(code))
        alert("a2", "auth", "x")
        alert("a2", "http_400", "y")
        assert sorted(fired) == ["auth", "http_400"]

    def test_broken_hook_does_not_break_alerting(self):
        from utils.resilience import alert, register_alert_hook

        register_alert_hook(lambda *a: (_ for _ in ()).throw(RuntimeError("hook broken")))
        fired = []
        register_alert_hook(lambda site, code, msg, detail: fired.append(code))
        alert("a3", "auth", "x")           # 第一个钩子炸了,第二个仍应收到
        assert fired == ["auth"]


class TestRedaction:
    def test_secrets_are_stripped(self):
        from utils.resilience.observe import _redact

        assert "sk-" not in _redact("key=sk-abcdefgh12345678")
        assert "abcdefghijklmnop" not in _redact("Bearer abcdefghijklmnop1234")
        assert "supersecret" not in _redact("api_key=supersecret")
        assert "a" * 40 not in _redact("token " + "a" * 40)

    def test_plain_text_untouched(self):
        from utils.resilience.observe import _redact

        msg = "查询改写失败,已回退原问题"
        assert _redact(msg) == msg
