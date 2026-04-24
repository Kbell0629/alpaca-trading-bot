"""
Round-61 pt.5: BEHAVIORAL coverage for scheduler_api.py.

Pre-pt.5 coverage: 41%. Target: 85%+ so this single module contributes
~1-2 points to total coverage.

scheduler_api wraps Alpaca HTTP with:
  * Per-user circuit breaker (trips at 5 failures, 5-min cool-off)
  * Per-user rate limiter (180 tokens / 60s, to stay under 200/min)
  * Per-user/per-mode (paper vs live) auth-failure alerting
  * HTTP 429 retry with Retry-After honor
  * HTTP 5xx retry with exponential backoff

Tests stub urllib.request.urlopen to avoid any network I/O, and
time.sleep to keep the suite fast.
"""
from __future__ import annotations

import io
import json
import sys
import time
from unittest.mock import patch

import pytest
import urllib.error
import urllib.request


def _user(mode="paper", uid=1):
    return {
        "id": uid, "username": f"u{uid}",
        "_api_key": "k", "_api_secret": "s",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_data_endpoint": "https://data.alpaca.markets/v2",
        "_mode": mode,
    }


def _reload(monkeypatch):
    """Fresh scheduler_api module with clean CB/RL state."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for m in ("auth", "scheduler_api"):
        sys.modules.pop(m, None)
    import scheduler_api
    # Make sleep instant for speed
    monkeypatch.setattr(scheduler_api.time, "sleep", lambda *a, **k: None)
    return scheduler_api


class _FakeResponse:
    """File-like urlopen response."""

    def __init__(self, body_bytes, status=200):
        self._body = body_bytes
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ============================================================
# CIRCUIT BREAKER
# ============================================================

class TestCircuitBreaker:
    def test_initial_state_not_blocked(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        assert sa._cb_blocked(u) is False

    def test_failure_records_but_doesnt_trip_below_threshold(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        for _ in range(sa._CB_OPEN_THRESHOLD - 1):
            sa._cb_record_failure(u)
        assert sa._cb_blocked(u) is False

    def test_threshold_failures_trip_breaker(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        for _ in range(sa._CB_OPEN_THRESHOLD):
            sa._cb_record_failure(u)
        assert sa._cb_blocked(u) is True

    def test_success_resets_failure_count(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        for _ in range(sa._CB_OPEN_THRESHOLD - 1):
            sa._cb_record_failure(u)
        sa._cb_record_success(u)
        # Next failure should NOT trip (count was reset)
        sa._cb_record_failure(u)
        assert sa._cb_blocked(u) is False

    def test_cooldown_expires_and_unblocks(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        for _ in range(sa._CB_OPEN_THRESHOLD):
            sa._cb_record_failure(u)
        # Force the open_until into the past
        key = sa._cb_key(u)
        sa._cb_state[key]["open_until"] = time.time() - 1
        # Blocked check should pop state + return False
        assert sa._cb_blocked(u) is False
        assert key not in sa._cb_state

    def test_paper_and_live_use_separate_buckets(self, monkeypatch):
        sa = _reload(monkeypatch)
        paper = _user(mode="paper")
        live = _user(mode="live")
        # Trip paper
        for _ in range(sa._CB_OPEN_THRESHOLD):
            sa._cb_record_failure(paper)
        assert sa._cb_blocked(paper) is True
        # Live is unaffected
        assert sa._cb_blocked(live) is False

    def test_cb_key_shape(self, monkeypatch):
        sa = _reload(monkeypatch)
        paper = _user(mode="paper")
        live = _user(mode="live")
        # Paper keeps plain id for backcompat
        assert sa._cb_key(paper) == 1
        # Live is suffixed with :live
        assert sa._cb_key(live) == "1:live"

    def test_trip_logs_warning(self, monkeypatch, caplog):
        sa = _reload(monkeypatch)
        # Stub cloud_scheduler import inside _cb_record_failure
        fake_cs = sys.modules.get("cloud_scheduler")
        if fake_cs is None:
            import types
            fake_cs = types.ModuleType("cloud_scheduler")
            fake_cs.notify_user = lambda *a, **k: None
            monkeypatch.setitem(sys.modules, "cloud_scheduler", fake_cs)
        else:
            monkeypatch.setattr(fake_cs, "notify_user",
                                 lambda *a, **k: None, raising=False)
        u = _user()
        import logging
        with caplog.at_level(logging.WARNING, logger="scheduler_api"):
            for _ in range(sa._CB_OPEN_THRESHOLD):
                sa._cb_record_failure(u)
        assert any("circuit breaker OPEN" in r.message for r in caplog.records)


# ============================================================
# RATE LIMITER
# ============================================================

class TestRateLimiter:
    def test_acquire_succeeds_when_tokens_available(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        assert sa._rl_acquire(u) is True

    def test_acquire_drains_tokens(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        # Acquire max then force zero to test next call
        for _ in range(sa._RL_MAX):
            sa._rl_acquire(u)
        key = sa._cb_key(u)
        # Manually zero it and freeze time — should fail within deadline
        sa._rl_state[key] = {"tokens": 0, "updated": time.time() + 1000}
        assert sa._rl_acquire(u, wait_max=0.01) is False

    def test_acquire_refills_over_time(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        key = sa._cb_key(u)
        # Drain then simulate time passing
        sa._rl_state[key] = {"tokens": 0, "updated": time.time() - 10}
        assert sa._rl_acquire(u) is True

    def test_isolation_per_user(self, monkeypatch):
        sa = _reload(monkeypatch)
        u1 = _user(uid=1)
        u2 = _user(uid=2)
        # Drain u1
        key1 = sa._cb_key(u1)
        sa._rl_state[key1] = {"tokens": 0, "updated": time.time() + 1000}
        assert sa._rl_acquire(u1, wait_max=0.01) is False
        # u2 unaffected
        assert sa._rl_acquire(u2) is True


# ============================================================
# AUTH FAILURE ALERTING
# ============================================================

class TestAuthAlert:
    def test_first_alert_fires_and_registers_date(self, monkeypatch):
        sa = _reload(monkeypatch)
        calls = []

        import types
        obs = types.ModuleType("observability")
        obs.critical_alert = lambda *a, **k: calls.append((a, k))
        monkeypatch.setitem(sys.modules, "observability", obs)

        u = _user()
        sa._alert_alpaca_auth_failure(u, 401, "Unauthorized")
        assert len(calls) == 1, "first auth failure must fire critical_alert"

    def test_second_same_day_is_deduped(self, monkeypatch):
        sa = _reload(monkeypatch)
        calls = []

        import types
        obs = types.ModuleType("observability")
        obs.critical_alert = lambda *a, **k: calls.append((a, k))
        monkeypatch.setitem(sys.modules, "observability", obs)

        u = _user()
        sa._alert_alpaca_auth_failure(u, 401, "Unauthorized")
        sa._alert_alpaca_auth_failure(u, 401, "Unauthorized")
        assert len(calls) == 1, "same-day repeat must not re-alert"

    def test_paper_and_live_alert_separately_same_day(self, monkeypatch):
        sa = _reload(monkeypatch)
        calls = []

        import types
        obs = types.ModuleType("observability")
        obs.critical_alert = lambda *a, **k: calls.append((a, k))
        monkeypatch.setitem(sys.modules, "observability", obs)

        paper = _user(mode="paper")
        live = _user(mode="live")
        sa._alert_alpaca_auth_failure(paper, 401, "Unauthorized")
        sa._alert_alpaca_auth_failure(live, 401, "Unauthorized")
        assert len(calls) == 2, "paper + live creds rot → two separate alerts"


# ============================================================
# user_api_get
# ============================================================

class TestUserApiGet:
    def test_happy_path_returns_decoded_body(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        payload = {"portfolio_value": "100000"}
        body = json.dumps(payload).encode()
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: _FakeResponse(body))
        assert sa.user_api_get(u, "/account") == payload

    def test_routes_stocks_to_data_endpoint(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["url"] = req.full_url
            return _FakeResponse(b'{"trade":{"p":100}}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_get(u, "/stocks/AAPL/trades/latest?feed=iex")
        assert captured["url"].startswith(u["_data_endpoint"])

    def test_routes_orders_to_api_endpoint(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["url"] = req.full_url
            return _FakeResponse(b"[]")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_get(u, "/orders")
        assert captured["url"].startswith(u["_api_endpoint"])

    def test_circuit_breaker_open_fast_fails(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        # Pre-trip
        for _ in range(sa._CB_OPEN_THRESHOLD):
            sa._cb_record_failure(u)
        # urlopen should NOT be called when CB is open
        calls = []

        def fake_urlopen(*a, **k):
            calls.append(1)
            return _FakeResponse(b"{}")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_get(u, "/account")
        assert result == {"error": "circuit_breaker_open"}
        assert calls == [], "urlopen must NOT fire when CB is open"

    def test_429_retried_then_succeeds(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        attempts = {"n": 0}

        def fake_urlopen(*a, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                err = urllib.error.HTTPError(
                    "http://x", 429, "rate limit",
                    {"Retry-After": "0.01"}, io.BytesIO(b""))
                raise err
            return _FakeResponse(b'{"ok":true}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert sa.user_api_get(u, "/account") == {"ok": True}
        assert attempts["n"] == 2

    def test_5xx_retried_then_succeeds(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        attempts = {"n": 0}

        def fake_urlopen(*a, **k):
            attempts["n"] += 1
            if attempts["n"] < 2:
                err = urllib.error.HTTPError(
                    "http://x", 500, "server error", {}, io.BytesIO(b""))
                raise err
            return _FakeResponse(b'{"ok":true}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert sa.user_api_get(u, "/account") == {"ok": True}

    def test_5xx_exhausts_retries_records_failure(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def always_500(*a, **k):
            err = urllib.error.HTTPError(
                "http://x", 503, "down", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", always_500)
        result = sa.user_api_get(u, "/account")
        assert "error" in result
        assert "503" in result["error"]
        # Failure recorded exactly once (after retries exhausted)
        key = sa._cb_key(u)
        assert sa._cb_state[key]["fails"] == 1

    def test_4xx_not_retried(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        attempts = {"n": 0}

        def fake_urlopen(*a, **k):
            attempts["n"] += 1
            err = urllib.error.HTTPError(
                "http://x", 404, "not found", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_get(u, "/orders/nonexistent")
        # Round-61 pt.29: GET now surfaces ``HTTP <code>: <reason>``
        # (matching POST/DELETE/PATCH shape) so operators see Alpaca's
        # status text, plus any response-body detail when present.
        assert "HTTP 404" in result["error"]
        assert attempts["n"] == 1, "4xx errors must not be retried"

    def test_generic_exception_retried_then_exhausts(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def boom(*a, **k):
            raise ConnectionError("network flap")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        result = sa.user_api_get(u, "/account")
        assert result == {"error": "Request failed"}

    def test_absolute_url_used_as_is(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["url"] = req.full_url
            return _FakeResponse(b"{}")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_get(u, "http://fully.qualified/path")
        assert captured["url"] == "http://fully.qualified/path"


# ============================================================
# user_api_post
# ============================================================

class TestUserApiPost:
    def test_happy_path(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: _FakeResponse(b'{"id":"xyz"}'))
        assert sa.user_api_post(u, "/orders", {"symbol": "AAPL"}) == {"id": "xyz"}

    def test_401_fires_auth_alert(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        alerts = []
        monkeypatch.setattr(sa, "_alert_alpaca_auth_failure",
                             lambda user, code, reason: alerts.append(code))

        def fake_urlopen(*a, **k):
            err = urllib.error.HTTPError(
                "http://x", 401, "Unauthorized", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_post(u, "/orders", {})
        assert "401" in result["error"]
        assert alerts == [401]

    def test_403_fires_auth_alert(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        alerts = []
        monkeypatch.setattr(sa, "_alert_alpaca_auth_failure",
                             lambda user, code, reason: alerts.append(code))

        def fake_urlopen(*a, **k):
            err = urllib.error.HTTPError(
                "http://x", 403, "Forbidden", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_post(u, "/orders", {})
        assert alerts == [403]

    def test_422_validation_does_not_trip_cb(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def fake_urlopen(*a, **k):
            err = urllib.error.HTTPError(
                "http://x", 422, "validation", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_post(u, "/orders", {})
        # No failure recorded — 4xx is user-input issue, not transport
        key = sa._cb_key(u)
        assert sa._cb_state.get(key, {}).get("fails", 0) == 0

    def test_500_records_failure(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def fake_urlopen(*a, **k):
            err = urllib.error.HTTPError(
                "http://x", 500, "down", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_post(u, "/orders", {})
        key = sa._cb_key(u)
        assert sa._cb_state[key]["fails"] == 1

    def test_cb_open_fast_fails(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        for _ in range(sa._CB_OPEN_THRESHOLD):
            sa._cb_record_failure(u)
        result = sa.user_api_post(u, "/orders", {})
        assert result == {"error": "circuit_breaker_open"}


# ============================================================
# user_api_delete
# ============================================================

class TestUserApiDelete:
    def test_happy_path_returns_json(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: _FakeResponse(b'{"status":"canceled"}'))
        assert sa.user_api_delete(u, "/orders/abc") == {"status": "canceled"}

    def test_empty_body_returns_empty_dict(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: _FakeResponse(b""))
        assert sa.user_api_delete(u, "/orders/abc") == {}

    def test_401_fires_alert(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        alerts = []
        monkeypatch.setattr(sa, "_alert_alpaca_auth_failure",
                             lambda user, code, reason: alerts.append(code))

        def fake_urlopen(*a, **k):
            err = urllib.error.HTTPError(
                "http://x", 401, "Unauthorized", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_delete(u, "/orders/abc")
        assert alerts == [401]

    def test_500_records_failure(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def fake_urlopen(*a, **k):
            err = urllib.error.HTTPError(
                "http://x", 500, "down", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_delete(u, "/orders/abc")
        key = sa._cb_key(u)
        assert sa._cb_state[key]["fails"] == 1


# ============================================================
# user_api_patch
# ============================================================

class TestUserApiPatch:
    def test_happy_path(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: _FakeResponse(b'{"id":"stop-1"}'))
        assert sa.user_api_patch(u, "/orders/xxx",
                                   {"stop_price": "95"}) == {"id": "stop-1"}

    def test_401_fires_alert(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        alerts = []
        monkeypatch.setattr(sa, "_alert_alpaca_auth_failure",
                             lambda user, code, reason: alerts.append(code))

        def fake_urlopen(*a, **k):
            err = urllib.error.HTTPError(
                "http://x", 401, "Unauthorized", {}, io.BytesIO(b""))
            raise err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_patch(u, "/orders/x", {"stop_price": "95"})
        assert alerts == [401]

    def test_generic_exception_records_failure(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def boom(*a, **k):
            raise ConnectionError("network down")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        sa.user_api_patch(u, "/orders/x", {"stop_price": "95"})
        key = sa._cb_key(u)
        assert sa._cb_state[key]["fails"] == 1
