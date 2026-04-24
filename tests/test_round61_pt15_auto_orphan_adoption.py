"""Round-61 pt.15 — autonomous orphan-position adoption.

User-reported: dashboard shows MANUAL on a short (SOXL) that SHOULD
have been auto-managed. The bot's error_recovery.py already knows how
to synthesize a strategy file for any Alpaca position without one,
but it was only invoked once per day inside run_daily_close. Between
one 4:05 PM ET close and the next, any position opened outside the
bot (or whose strategy file got cleaned up) sat unmanaged for up to
23.5 hours — no stop placed, no bot-driven exit.

This file pins:
  1. cloud_scheduler.run_orphan_adoption(user) — per-user wrapper
     around error_recovery.py (subprocess, same pattern as
     run_daily_close). Returns {"created": N, ...} so callers can
     surface the adoption count to the user.
  2. Periodic invocation every 600s during market hours in the main
     scheduler loop.
  3. handlers.actions_mixin.handle_force_orphan_adoption — the
     on-demand path triggered by the "Adopt MANUAL -> AUTO" button.
  4. server.py routes /api/adopt-orphans -> the handler.
  5. Dashboard button + JS handler posts to the new endpoint, surfaces
     the result via toast, refreshes so new AUTO labels appear.
"""
from __future__ import annotations


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# cloud_scheduler wrapper + schedule
# ----------------------------------------------------------------------------

def test_cloud_scheduler_has_run_orphan_adoption():
    src = _src("cloud_scheduler.py")
    assert "def run_orphan_adoption(user)" in src, (
        "cloud_scheduler must expose run_orphan_adoption(user) as "
        "the per-user entry point that handlers + the periodic loop "
        "both call.")


def test_run_orphan_adoption_invokes_error_recovery_subprocess():
    src = _src("cloud_scheduler.py")
    # The function body must call error_recovery.py in a subprocess
    # (same isolation pattern as run_daily_close uses for
    # update_scorecard.py + error_recovery.py).
    idx = src.find("def run_orphan_adoption(user)")
    assert idx > 0
    body = src[idx:idx + 2500]
    assert "error_recovery.py" in body, (
        "run_orphan_adoption must subprocess error_recovery.py — "
        "keeps the scheduler heartbeat alive + reuses the existing "
        "orphan-detection logic per-user via env vars.")
    assert "ALPACA_API_KEY" in body and "STRATEGIES_DIR" in body, (
        "Subprocess env must include per-user ALPACA creds and "
        "STRATEGIES_DIR (same pattern as run_daily_close).")


def test_orphan_adoption_scheduled_every_10min_in_market_hours():
    """The periodic scheduler loop must call run_orphan_adoption once
    every 10 min during market hours, gated by should_run_interval."""
    src = _src("cloud_scheduler.py")
    assert "should_run_interval(f\"adopt_orphans_{uid}\", 600)" in src, (
        "Orphan adoption must be gated by "
        "should_run_interval(f'adopt_orphans_{uid}', 600) so it runs "
        "every 10 min during market hours (not every 60s which would "
        "spam subprocess starts, not once per day which caused the "
        "original gap).")
    # And it must sit inside the market_open_flag branch.
    idx = src.find("should_run_interval(f\"adopt_orphans_{uid}\", 600)")
    assert idx > 0
    surrounding = src[max(0, idx - 400):idx + 200]
    assert "market_open_flag" in surrounding, (
        "Orphan adoption must only run during market hours — "
        "no point synthesizing strategy files while the bot "
        "can't place the corresponding Alpaca stop orders.")


# ----------------------------------------------------------------------------
# Handler + route
# ----------------------------------------------------------------------------

def test_actions_mixin_has_handle_force_orphan_adoption():
    src = _src("handlers/actions_mixin.py")
    assert "def handle_force_orphan_adoption" in src
    assert "cs.run_orphan_adoption" in src, (
        "Handler must delegate to cloud_scheduler.run_orphan_adoption "
        "so on-demand + scheduled invocations share the same code path.")


def test_server_routes_adopt_orphans_endpoint():
    src = _src("server.py")
    assert '"/api/adopt-orphans"' in src
    assert "handle_force_orphan_adoption" in src


# ----------------------------------------------------------------------------
# UI: button + JS handler
# ----------------------------------------------------------------------------

def test_dashboard_has_adopt_orphans_button():
    src = _src("templates/dashboard.html")
    assert 'onclick="adoptOrphanPositions()"' in src, (
        "Dashboard must have an 'Adopt MANUAL -> AUTO' button that "
        "calls the adoptOrphanPositions JS handler.")


def test_dashboard_has_adoptOrphanPositions_js_handler():
    src = _src("templates/dashboard.html")
    assert "async function adoptOrphanPositions" in src
    assert "/api/adopt-orphans" in src


# ----------------------------------------------------------------------------
# Behavioral: via http_harness
# ----------------------------------------------------------------------------

def test_adopt_orphans_endpoint_requires_auth(http_harness):
    http_harness.create_user()
    http_harness.logout()
    resp = http_harness.post("/api/adopt-orphans", body={})
    assert resp["status"] in (401, 403), (
        f"Unauthed /api/adopt-orphans should 401/403, got {resp['status']}")


def test_adopt_orphans_endpoint_calls_run_orphan_adoption(http_harness, monkeypatch):
    """Endpoint must delegate to cloud_scheduler.run_orphan_adoption
    and surface the 'adopted' count to the UI."""
    http_harness.create_user()
    import cloud_scheduler as cs
    called = {"n": 0}
    def _fake(user):
        called["n"] += 1
        return {"created": 3, "lines": ["FIXED: Created short_sell_SOXL.json"]}
    monkeypatch.setattr(cs, "run_orphan_adoption", _fake)
    resp = http_harness.post("/api/adopt-orphans", body={})
    assert resp["status"] == 200
    assert called["n"] == 1
    assert resp["body"].get("adopted") == 3
    assert "3" in resp["body"].get("message", "")


def test_adopt_orphans_endpoint_handles_subprocess_error(http_harness, monkeypatch):
    """When run_orphan_adoption returns {'error': ...}, endpoint
    must 500 with the error message (not silently succeed)."""
    http_harness.create_user()
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "run_orphan_adoption",
                        lambda user: {"error": "timeout"})
    resp = http_harness.post("/api/adopt-orphans", body={})
    assert resp["status"] == 500
    assert "timeout" in resp["body"].get("error", "").lower()
