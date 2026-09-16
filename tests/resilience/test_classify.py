"""错误分类器用例(需求测试项 3/5/6/7/8 的分类面)。

覆盖三类异常形态:openai SDK 层级、requests、urllib,以及标准库传输层异常。
"""
from __future__ import annotations

import json
import socket
import urllib.error

import pytest

from utils.resilience.classify import (
    Kind,
    classify,
    deterministic_status_codes,
    is_transient,
    register_exception_kind,
)


def _http_error(status: int, headers: dict | None = None):
    import requests
    resp = requests.Response()
    resp.status_code = status
    resp.headers = headers or {}
    return requests.HTTPError(f"HTTP {status}", response=resp)


class TestTransient:
    """瞬时:重试可能改变结果。"""

    @pytest.mark.parametrize("exc", [
        TimeoutError("timed out"),
        socket.timeout("timed out"),
        ConnectionResetError("connection reset by peer"),
        socket.gaierror("name resolution failed"),
        json.JSONDecodeError("truncated response", "", 0),
    ])
    def test_stdlib_transport_is_transient(self, exc):
        assert classify(exc).kind is Kind.TRANSIENT

    @pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 507])
    def test_retryable_http_statuses(self, status):
        assert classify(_http_error(status)).kind is Kind.TRANSIENT

    def test_dns_and_connection_have_distinct_codes(self):
        assert classify(socket.gaierror("nope")).code == "dns"
        assert classify(ConnectionResetError("reset")).code == "connection"

    def test_urllib_5xx_is_transient(self):
        exc = urllib.error.HTTPError("u", 503, "busy", None, None)
        assert classify(exc).kind is Kind.TRANSIENT

    def test_transient_never_alerts(self):
        # 瞬时错误是「等一会儿就好」,不该惊动人
        assert classify(TimeoutError("x")).alert is False

    def test_is_transient_helper(self):
        assert is_transient(TimeoutError("x")) is True
        assert is_transient(_http_error(400)) is False


class TestDeterministic:
    """确定性:重试结果一样,直接降级。"""

    @pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 405, 409, 410, 422])
    def test_4xx_is_deterministic(self, status):
        assert classify(_http_error(status)).kind is Kind.DETERMINISTIC

    def test_deterministic_status_codes_exposed(self):
        # retry_util 复用这张表,防止两处判定漂移
        codes = deterministic_status_codes()
        assert 400 in codes and 422 in codes and 402 in codes
        assert 429 not in codes and 500 not in codes

    def test_auth_and_not_found_alert(self):
        # 鉴权/不存在属于「配置或代码问题」,必须告警
        for status in (401, 403, 404, 402):
            c = classify(_http_error(status))
            assert c.alert is True, f"{status} 应当告警"

    def test_bad_request_does_not_alert(self):
        # 400 常见于上游参数问题,不必每次告警
        assert classify(_http_error(400)).alert is False

    @pytest.mark.parametrize("msg", ["余额不足,请充值", "insufficient balance", "quota exhausted"])
    def test_business_refusal_is_deterministic(self, msg):
        c = classify(ValueError(msg))
        assert c.kind is Kind.DETERMINISTIC
        assert c.alert is True

    @pytest.mark.parametrize("msg", [
        "content filter triggered", "内容审核不通过", "data_inspection failed",
    ])
    def test_moderation_is_deterministic(self, msg):
        assert classify(RuntimeError(msg)).kind is Kind.DETERMINISTIC


class TestUnknown:
    """判不出来按不重试处理:把未知当可重试会放大未知故障。"""

    def test_unknown_does_not_retry(self):
        c = classify(ValueError("something odd entirely"))
        assert c.kind is Kind.UNKNOWN

    def test_classifier_never_raises(self):
        # 分类器自己不能成为故障点,哪怕是奇怪对象
        class Hostile(Exception):
            def __str__(self):
                raise RuntimeError("boom")

        c = classify(Hostile())
        assert c.kind in (Kind.UNKNOWN, Kind.TRANSIENT, Kind.DETERMINISTIC)


class TestOpenAI:
    """openai SDK 类型层级优先于状态码猜测。"""

    def _err(self, cls, status, headers=None):
        import httpx
        import openai
        req = httpx.Request("POST", "https://example.invalid")
        resp = httpx.Response(status, request=req, headers=headers or {})
        return getattr(openai, cls)("boom", response=resp, body=None)

    def test_authentication_error(self):
        c = classify(self._err("AuthenticationError", 401))
        assert c.kind is Kind.DETERMINISTIC and c.alert is True and c.status == 401

    def test_rate_limit_carries_retry_after(self):
        c = classify(self._err("RateLimitError", 429, {"Retry-After": "3"}))
        assert c.kind is Kind.TRANSIENT
        assert c.retry_after == 3.0

    def test_internal_server_error(self):
        assert classify(self._err("InternalServerError", 500)).kind is Kind.TRANSIENT

    def test_bad_request(self):
        assert classify(self._err("BadRequestError", 400)).kind is Kind.DETERMINISTIC

    def test_api_timeout(self):
        import httpx
        import openai
        exc = openai.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
        assert classify(exc).kind is Kind.TRANSIENT


class TestRetryAfter:
    def test_delta_seconds(self):
        c = classify(_http_error(429, {"Retry-After": "7"}))
        assert c.retry_after == 7.0

    def test_http_date_form(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        c = classify(_http_error(429, {"Retry-After": format_datetime(when)}))
        # 允许执行耗时带来的少量偏差
        assert c.retry_after is not None and 28 <= c.retry_after <= 30

    def test_garbage_header_is_ignored(self):
        assert classify(_http_error(429, {"Retry-After": "soon"})).retry_after is None

    def test_lowercase_header_key(self):
        assert classify(_http_error(503, {"retry-after": "5"})).retry_after == 5.0


class TestRegistration:
    """调用点可加分类规则而不修改核心(需求:分类器可扩展)。"""

    def test_registered_rule_wins_over_builtin(self):
        class WeirdError(Exception):
            pass

        # 内置会把未知异常判为 UNKNOWN;注册后按我们的规则判
        assert classify(WeirdError("x")).kind is Kind.UNKNOWN
        register_exception_kind(WeirdError, Kind.TRANSIENT, code="weird")
        c = classify(WeirdError("x"))
        assert c.kind is Kind.TRANSIENT and c.code == "weird"

    def test_predicate_matcher(self):
        register_exception_kind(
            lambda e: "flaky" in str(e), Kind.TRANSIENT, code="flaky",
        )
        assert classify(RuntimeError("this is flaky")).kind is Kind.TRANSIENT
        assert classify(RuntimeError("this is solid")).kind is Kind.UNKNOWN

    def test_registration_can_alert(self):
        class ConfigError(Exception):
            pass

        register_exception_kind(ConfigError, Kind.DETERMINISTIC, code="cfg", alert=True)
        assert classify(ConfigError("x")).alert is True
