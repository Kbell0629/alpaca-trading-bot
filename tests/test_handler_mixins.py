"""
Unit tests for the pure helpers on the handler mixins.

E2E tests (tests/test_e2e.py) already cover full HTTP flows. These tests
target the logic that lives in the mixin methods themselves — the bits
that take a request context and decide what to do — without spinning up
a subprocess.

Scope:

  * _csrf_ok — double-submit cookie matching / fail-closed on missing
  * _set_session_cookie — Secure flag decision under various
    X-Forwarded-Proto / DEV_MODE / FORCE_SECURE_COOKIE combos
  * Input validation on handlers that return early (regex rejects,
    required-field checks, rate-limit gates)
  * handle_logout — clears session cookie + deletes row

These aren't full route tests; they exercise the decision points that
are pure-enough to unit-test.
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest


class _FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler + DashboardHandler
    surface that the mixins use. Only implements what the method under
    test actually calls — keeps each test isolated.
    """
    def __init__(self, *, cookies="", headers=None, client_ip="1.2.3.4",
                 xfp=None, query_string=""):
        self._resp_status = None
        self._resp_headers = []
        self._body = BytesIO()
        self._sent_json = None
        self._sent_status = 200
        self.client_address = (client_ip, 12345)
        self.wfile = BytesIO()
        base_headers = {"Cookie": cookies}
        if xfp is not None:
            base_headers["X-Forwarded-Proto"] = xfp
        base_headers.update(headers or {})
        self.headers = base_headers
        self.path = "/api/test" + (f"?{query_string}" if query_string else "")
        self.current_user = None

    def send_response(self, status):
        self._resp_status = status

    def send_header(self, key, val):
        self._resp_headers.append((key, val))

    def end_headers(self):
        pass

    def send_json(self, data, status=200):
        self._sent_json = data
        self._sent_status = status

    def header_values(self, key):
        return [v for k, v in self._resp_headers if k == key]


# ---------- _csrf_ok double-submit check ----------


def test_csrf_ok_accepts_matching_cookie_and_header():
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(cookies="csrf=abc123; session=xxx", headers={"X-CSRF-Token": "abc123"})
    assert h._csrf_ok() is True


def test_csrf_ok_rejects_header_mismatch():
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(cookies="csrf=abc123", headers={"X-CSRF-Token": "different"})
    assert h._csrf_ok() is False


def test_csrf_ok_rejects_missing_cookie():
    """Round-11 audit: transitional fail-open for missing cookie
    expired. Missing cookie must now be rejected."""
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(cookies="", headers={"X-CSRF-Token": "abc123"})
    assert h._csrf_ok() is False


def test_csrf_ok_rejects_missing_header():
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(cookies="csrf=abc123", headers={})
    assert h._csrf_ok() is False


def test_csrf_ok_timing_safe_on_same_length_mismatch():
    """hmac.compare_digest guarantees constant-time comparison. The test
    doesn't measure timing (too flaky) but asserts the implementation
    path routes through compare_digest, not `==`."""
    from handlers.auth_mixin import AuthHandlerMixin
    import handlers.auth_mixin as am
    import inspect
    src = inspect.getsource(am.AuthHandlerMixin._csrf_ok)
    assert "hmac.compare_digest" in src, (
        "_csrf_ok must use hmac.compare_digest to prevent timing attacks"
    )


# ---------- _set_session_cookie Secure flag decision ----------


def test_session_cookie_secure_when_xfp_https(monkeypatch):
    """Railway edge sets X-Forwarded-Proto: https on all inbound traffic.
    The mixin must honour this and emit Secure on both cookies."""
    monkeypatch.delenv("FORCE_SECURE_COOKIE", raising=False)
    monkeypatch.delenv("DEV_MODE", raising=False)
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(xfp="https")
    h._set_session_cookie("test-token")
    set_cookies = h.header_values("Set-Cookie")
    assert len(set_cookies) == 2
    for c in set_cookies:
        assert "Secure" in c, f"cookie missing Secure flag: {c}"


def test_session_cookie_secure_via_force_env(monkeypatch):
    """FORCE_SECURE_COOKIE=1 overrides the Xfp check."""
    monkeypatch.setenv("FORCE_SECURE_COOKIE", "1")
    monkeypatch.delenv("DEV_MODE", raising=False)
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(xfp="http")  # deliberately NOT https
    h._set_session_cookie("test-token")
    set_cookies = h.header_values("Set-Cookie")
    assert all("Secure" in c for c in set_cookies)


def test_session_cookie_plain_in_dev_mode(monkeypatch):
    """DEV_MODE=1 + no https + no FORCE_SECURE_COOKIE → plain cookie so
    localhost http testing works without forcing HTTPS."""
    monkeypatch.delenv("FORCE_SECURE_COOKIE", raising=False)
    monkeypatch.setenv("DEV_MODE", "1")
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(xfp="http")
    h._set_session_cookie("test-token")
    set_cookies = h.header_values("Set-Cookie")
    for c in set_cookies:
        assert "Secure" not in c, f"Secure set unexpectedly in DEV_MODE: {c}"


def test_session_cookie_default_secure_without_dev_mode(monkeypatch):
    """Production default: no DEV_MODE + no Xfp + no FORCE → still
    Secure. Fail-closed on cookie security when env is ambiguous."""
    monkeypatch.delenv("FORCE_SECURE_COOKIE", raising=False)
    monkeypatch.delenv("DEV_MODE", raising=False)
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(xfp=None)   # no Xfp header at all
    h._set_session_cookie("test-token")
    set_cookies = h.header_values("Set-Cookie")
    assert all("Secure" in c for c in set_cookies), (
        "Production default should be Secure; missing DEV_MODE must not "
        "drop the flag"
    )


def test_session_cookie_samesite_strict():
    """SameSite=Strict on both session and csrf cookies."""
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(xfp="https")
    h._set_session_cookie("test-token")
    set_cookies = h.header_values("Set-Cookie")
    for c in set_cookies:
        assert "SameSite=Strict" in c, f"Missing SameSite=Strict: {c}"


def test_session_cookie_httponly_on_session_not_csrf():
    """Session cookie MUST be HttpOnly so JS can't exfiltrate it.
    CSRF cookie MUST NOT be HttpOnly — the dashboard JS reads it to
    echo the double-submit header."""
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H(xfp="https")
    h._set_session_cookie("test-token")
    set_cookies = h.header_values("Set-Cookie")
    session_cookie = next(c for c in set_cookies if c.startswith("session="))
    csrf_cookie = next(c for c in set_cookies if c.startswith("csrf="))
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie


# ---------- handle_change_password input validation ----------


def test_change_password_rejects_empty_fields():
    from handlers.auth_mixin import AuthHandlerMixin

    class H(AuthHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.current_user = {"id": 1, "username": "testuser"}
    h.handle_change_password({"old_password": "", "new_password": ""})
    assert h._sent_status == 400
    assert "required" in h._sent_json["error"].lower()


# ---------- actions mixin: handle_cancel_order validation ----------


def test_cancel_order_rejects_missing_id():
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_cancel_order({})
    assert h._sent_status == 400
    assert "order_id" in h._sent_json["error"].lower()


def test_cancel_order_rejects_non_uuid():
    """Path-traversal guard: order_id must be a UUID regex."""
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_cancel_order({"order_id": "../etc/passwd"})
    assert h._sent_status == 400
    assert "format" in h._sent_json["error"].lower()


def test_cancel_order_rejects_sql_injection_attempt():
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_cancel_order({"order_id": "'; DROP TABLE orders--"})
    assert h._sent_status == 400


def test_close_position_rejects_missing_symbol():
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_close_position({})
    assert h._sent_status == 400


def test_close_position_rejects_bad_symbol():
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_close_position({"symbol": "../foo"})
    assert h._sent_status == 400


def test_close_position_rejects_lowercase_symbol_after_upper_normalise():
    """handle_close_position uppercases the symbol; "abc123" becomes
    "ABC123" which fails the [A-Z]{1,10} regex because of the digits."""
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_close_position({"symbol": "abc123"})
    assert h._sent_status == 400


def test_sell_rejects_invalid_qty():
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_sell({"symbol": "AAPL", "qty": "not a number"})
    assert h._sent_status == 400
    assert "quantity" in h._sent_json["error"].lower()


def test_sell_rejects_out_of_range_qty():
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_sell({"symbol": "AAPL", "qty": 99999})
    assert h._sent_status == 400


def test_sell_rejects_zero_qty():
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _FakeHandler):
        pass

    h = H()
    h.handle_sell({"symbol": "AAPL", "qty": 0})
    assert h._sent_status == 400
