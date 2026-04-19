"""
Sentry PII scrub hook — observability._scrub_pii.

Before events leave the process for Sentry, we strip credential-looking
substrings, emails, and auth-header values. Tests pin the redaction
contract so a regression doesn't silently leak API keys into the Sentry
project history.
"""
from __future__ import annotations

import observability as obs


def test_scrub_alpaca_key_in_exception_value():
    event = {"exception": {"values": [
        {"value": "Bad request APCA-API-KEY-ID=PKABCDEF12345678XYZQR failed"}
    ]}}
    out = obs._scrub_pii(event)
    val = out["exception"]["values"][0]["value"]
    assert "PKABCDEF" not in val
    assert "[REDACTED_KEY]" in val


def test_scrub_live_key_prefix():
    event = {"exception": {"values": [{"value": "token AKXYZ987654321QRS42"}]}}
    out = obs._scrub_pii(event)
    assert "AKXYZ" not in out["exception"]["values"][0]["value"]


def test_scrub_email_in_message():
    event = {"message": "failure for user@example.com"}
    out = obs._scrub_pii(event)
    assert "user@example.com" not in out["message"]
    assert "[REDACTED_EMAIL]" in out["message"]


def test_scrub_auth_headers():
    event = {"request": {"headers": {
        "APCA-API-KEY-ID": "PKABCDEF12345678XYZQR",
        "Authorization": "Bearer abc123def456",
        "Cookie": "session=xyz",
        "User-Agent": "Mozilla/5.0",
    }}}
    out = obs._scrub_pii(event)
    h = out["request"]["headers"]
    assert h["APCA-API-KEY-ID"] == "[REDACTED]"
    assert h["Authorization"] == "[REDACTED]"
    assert h["Cookie"] == "[REDACTED]"
    # Non-sensitive headers pass through
    assert h["User-Agent"] == "Mozilla/5.0"


def test_scrub_cookies_wholesale():
    event = {"request": {"cookies": {"session": "xyz", "other": "abc"}}}
    out = obs._scrub_pii(event)
    assert out["request"]["cookies"] == "[REDACTED]"


def test_scrub_breadcrumbs():
    event = {"breadcrumbs": {"values": [
        {"message": "calling with key PKABCDEF12345678XYZQR"},
        {"message": "normal log line"},
    ]}}
    out = obs._scrub_pii(event)
    vals = out["breadcrumbs"]["values"]
    assert "PKABCDEF" not in vals[0]["message"]
    assert vals[1]["message"] == "normal log line"


def test_scrub_returns_none_on_internal_error(monkeypatch):
    """If the scrub itself throws, drop the event — never send unscrubbed."""
    def _boom(s):
        raise RuntimeError("regex died")
    monkeypatch.setattr(obs, "_scrub_text", _boom)
    event = {"exception": {"values": [{"value": "anything"}]}}
    assert obs._scrub_pii(event) is None


def test_scrub_long_base64_secret():
    # 40+ char base64-ish string → treated as credential
    secret = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCd"
    event = {"message": f"leaked: {secret}"}
    out = obs._scrub_pii(event)
    assert secret not in out["message"]
    assert "[REDACTED_SECRET]" in out["message"]


def test_scrub_short_token_unaffected():
    # Short identifiers should NOT be scrubbed — they'd false-positive
    # against normal symbols like ticker codes.
    event = {"message": "ticker AAPL failed"}
    out = obs._scrub_pii(event)
    assert out["message"] == "ticker AAPL failed"


def test_scrub_non_string_values_are_safe():
    """Event shape may have ints / None in unexpected places."""
    event = {"message": None, "exception": {"values": [{"value": 42}]}}
    out = obs._scrub_pii(event)
    assert out is not None  # didn't crash


def test_scrub_query_string():
    event = {"request": {"query_string": "email=user@example.com&x=1"}}
    out = obs._scrub_pii(event)
    assert "user@example.com" not in out["request"]["query_string"]
