"""
Round-61 pt.9 — calibration endpoint error message differentiation.

User report (2026-04-24): Account Settings → Calibration tab shows the
yellow warning "Equity below $500 or Alpaca /account returned no data"
even though the paper account has $103k equity. Root cause: the
calibration handler collapsed three distinct failure modes into one
error string, so the user couldn't tell whether it was their creds,
their funding level, or an API outage.

Fix differentiates:
  * Alpaca API error (bad creds, network) → "Alpaca /account call
    failed: <error>"
  * Equity < $500 → "Account equity $X below $500 minimum"
  * Missing equity field → "Alpaca /account returned no equity data"

Pin the three messages so a future refactor doesn't collapse them back.
"""
from __future__ import annotations


def test_calibration_handler_differentiates_api_error():
    """When Alpaca returns an error dict, the message must include the
    error string + a pointer to Settings."""
    with open("server.py") as f:
        src = f.read()
    assert "Alpaca /account call failed:" in src, (
        "Calibration handler must surface the Alpaca error specifically "
        "instead of the generic 'Equity below $500' catch-all.")
    assert "Settings → Alpaca API" in src or "Settings -> Alpaca API" in src, (
        "Error message must point the user at where to fix their keys.")


def test_calibration_handler_differentiates_low_equity():
    """When equity < $500, the message must say so + include the actual
    amount so the user can confirm the detection."""
    with open("server.py") as f:
        src = f.read()
    assert "below the " in src and "$500 minimum" in src, (
        "Low-equity branch must render the specific $500 minimum + "
        "actual equity value.")


def test_calibration_handler_handles_missing_equity_field():
    """If the call succeeds but the equity field isn't there, the
    message must be distinct from the API-error case."""
    with open("server.py") as f:
        src = f.read()
    # Strip Python string-concatenation line breaks so the assembled
    # message is what we assert on (not the on-disk layout).
    import re
    flat = re.sub(r'"\s*\n\s*"', '', src)
    assert "returned no equity data" in flat, (
        "Missing-equity-field branch must have its own message.")


def test_handler_behaviorally_returns_api_error_branch(http_harness, monkeypatch):
    """End-to-end: stub user_api_get to return an error dict and assert
    the handler surfaces the exact string."""
    http_harness.create_user()
    from handlers import auth_mixin  # noqa

    import server as _server
    # Patch the handler's bound method so the request path uses our stub
    orig = _server.DashboardHandler.user_api_get
    def fake_user_api_get(self, url, timeout=15):
        return {"error": "HTTP 401: invalid API key"}
    monkeypatch.setattr(_server.DashboardHandler, "user_api_get", fake_user_api_get)
    try:
        resp = http_harness.get("/api/calibration")
        assert resp["status"] == 200
        body = resp["body"]
        assert body["detected"] is False
        assert "Alpaca /account call failed" in body["reason"]
        assert "invalid API key" in body["reason"]
    finally:
        monkeypatch.setattr(_server.DashboardHandler, "user_api_get", orig)


def test_handler_behaviorally_returns_low_equity_branch(http_harness, monkeypatch):
    """End-to-end: stub Alpaca response to return equity=$100 and assert
    the low-equity-specific message + the actual amount."""
    http_harness.create_user()
    import server as _server
    orig = _server.DashboardHandler.user_api_get
    def fake_user_api_get(self, url, timeout=15):
        return {"equity": "100.00", "multiplier": "1", "cash": "100.00"}
    monkeypatch.setattr(_server.DashboardHandler, "user_api_get", fake_user_api_get)
    try:
        resp = http_harness.get("/api/calibration")
        assert resp["status"] == 200
        body = resp["body"]
        assert body["detected"] is False
        assert "$500 minimum" in body["reason"]
        assert "100.00" in body["reason"]
    finally:
        monkeypatch.setattr(_server.DashboardHandler, "user_api_get", orig)
