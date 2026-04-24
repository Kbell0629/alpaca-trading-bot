"""Round-61 pt.24 — fix the 3 live audit findings.

User-reported via the 🔍 Audit modal after pt.22 shipped:

  1. LOW · ghost_strategy_file · CORZ
     (breakout_CORZ.json still active but CORZ closed at Alpaca)
  2. LOW · ghost_strategy_file · AXTI
     (same situation)
  3. HIGH · missing_stop · SOXL
     (qty -29 short, NO buy stop at Alpaca despite
     short_sell_SOXL.json being in place)

Root causes:
  - Ghosts: error_recovery.py Check 3 marks stale files closed, but
    only runs inside daily_close (4:05 PM ET) + on-demand via the
    "Adopt MANUAL → AUTO" button path. User clicks Audit, sees
    ghosts, has no direct way to clear them.

  - SOXL: process_short_strategy only places a cover-stop when
    `state.get("cover_order_id")` is falsy. If a prior placement was
    rejected by Alpaca (invalid price, auth, etc.) OR the order got
    canceled later, the stale order-id stays in state and prevents
    re-placement. The position sits unprotected forever.

Fixes:
  1. New `/api/close-ghost-strategies` endpoint + 🧹 Clean Up Ghosts
     button in the audit modal. Runs Check 3's same logic in-request
     (grace period: 10 min; skip if pending sell). User gets
     immediate feedback + modal auto-re-runs audit after cleanup.

  2. `process_short_strategy` verifies the cover_order_id is still
     live at Alpaca before trusting it. If
     canceled/rejected/expired/replaced/done_for_day, resets to None
     so the next block places a fresh stop. Same treatment for
     `process_strategy_file`'s stop_order_id on the long side.
"""
from __future__ import annotations

import json
import os


# ----------------------------------------------------------------------------
# Stale order-id resets in process_short_strategy / process_strategy_file
# ----------------------------------------------------------------------------

def test_short_strategy_resets_stale_cover_order_id():
    """Source-pin: the pre-placement block must query Alpaca for the
    existing cover_order_id and reset it if the order is dead."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    # Find the short-cover placement region.
    idx = src.find("Place initial stop-buy (cover) ABOVE entry")
    assert idx > 0
    block = src[idx:idx + 3500]
    # Must query Alpaca for the existing id.
    assert "user_api_get(user, f\"/orders/{existing_cover_id}\")" in block, (
        "process_short_strategy must verify the persisted "
        "cover_order_id is still live at Alpaca before re-using it.")
    # Must reset on dead statuses.
    for dead in ("canceled", "rejected", "expired"):
        assert f'"{dead}"' in block, (
            f"Dead-status list must include {dead!r}")


def test_long_strategy_resets_stale_stop_order_id():
    """Same treatment for the long stop_order_id in
    process_strategy_file."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    # Find the long initial-stop placement region.
    idx = src.find("Round-61 pt.24: verify stop_order_id is still live")
    assert idx > 0, (
        "pt.24 must add a stop_order_id liveness check before the "
        "long initial-stop placement.")
    block = src[idx:idx + 1500]
    assert "user_api_get(user, f\"/orders/{existing_stop_id}\")" in block
    assert "resetting" in block


# ----------------------------------------------------------------------------
# /api/close-ghost-strategies handler — behavioral via http_harness
# ----------------------------------------------------------------------------

def test_close_ghost_strategies_endpoint_requires_auth(http_harness):
    http_harness.create_user()
    http_harness.logout()
    resp = http_harness.post("/api/close-ghost-strategies", body={})
    assert resp["status"] in (401, 403)


def test_close_ghost_strategies_marks_ghost_file_closed(
        http_harness, monkeypatch, tmp_path):
    """A strategy file for a symbol with no matching Alpaca position
    + >10min old file mtime must get status=closed."""
    http_harness.create_user()
    # Build a per-user strategies dir with a ghost file.
    import auth
    user_dir = auth.user_data_dir(1)
    sdir = os.path.join(user_dir, "strategies")
    os.makedirs(sdir, exist_ok=True)
    ghost_path = os.path.join(sdir, "breakout_CORZ.json")
    with open(ghost_path, "w") as f:
        json.dump({
            "symbol": "CORZ", "strategy": "breakout",
            "status": "active",
            "state": {"entry_fill_price": 10.0},
        }, f)
    # Backdate mtime so grace period (10 min) doesn't skip.
    _past = os.path.getmtime(ghost_path) - 3600
    os.utime(ghost_path, (_past, _past))
    # Mock Alpaca: no positions matching CORZ, no pending orders.
    import server
    monkeypatch.setattr(server, "_fetch_live_alpaca_state",
                        lambda *a, **kw: ({}, [], [], []))
    resp = http_harness.post("/api/close-ghost-strategies", body={})
    assert resp["status"] == 200
    assert "breakout_CORZ.json" in resp["body"]["closed"]
    # File on disk updated.
    after = json.loads(open(ghost_path).read())
    assert after["status"] == "closed"
    assert "closed_reason" in after


def test_close_ghost_strategies_closes_recently_modified_files(
        http_harness, monkeypatch, tmp_path):
    """Round-61 pt.25: removed the 10-min grace period from the
    user-triggered cleanup path. User clicking the button is
    asserting intent — they've seen the audit and want these
    specific files retired. The scheduled error_recovery.py Check 3
    keeps its grace period for autonomous safety, but the manual
    button honors the click."""
    http_harness.create_user()
    import auth
    user_dir = auth.user_data_dir(1)
    sdir = os.path.join(user_dir, "strategies")
    os.makedirs(sdir, exist_ok=True)
    recent_path = os.path.join(sdir, "breakout_FRESH.json")
    with open(recent_path, "w") as f:
        json.dump({"symbol": "FRESH", "strategy": "breakout",
                    "status": "active"}, f)
    # mtime is "now" — pt.25 ignores this on the manual path.
    import server
    monkeypatch.setattr(server, "_fetch_live_alpaca_state",
                        lambda *a, **kw: ({}, [], [], []))
    resp = http_harness.post("/api/close-ghost-strategies", body={})
    assert resp["status"] == 200
    # Pt.25: file should be closed despite fresh mtime.
    assert "breakout_FRESH.json" in resp["body"]["closed"]
    # And it should NOT appear in skipped.
    skipped_files = [s["file"] for s in resp["body"]["skipped"]]
    assert "breakout_FRESH.json" not in skipped_files


def test_close_ghost_strategies_skips_files_with_matching_position(
        http_harness, monkeypatch):
    """A strategy file whose symbol IS in the current position list
    is NOT a ghost — must not be touched."""
    http_harness.create_user()
    import auth
    user_dir = auth.user_data_dir(1)
    sdir = os.path.join(user_dir, "strategies")
    os.makedirs(sdir, exist_ok=True)
    live_path = os.path.join(sdir, "breakout_CRDO.json")
    with open(live_path, "w") as f:
        json.dump({"symbol": "CRDO", "strategy": "breakout",
                    "status": "active"}, f)
    _past = os.path.getmtime(live_path) - 3600
    os.utime(live_path, (_past, _past))
    import server
    # Alpaca shows CRDO as an open position.
    monkeypatch.setattr(server, "_fetch_live_alpaca_state",
                        lambda *a, **kw: ({}, [
                            {"symbol": "CRDO", "qty": "18",
                             "current_price": "197.00",
                             "asset_class": "us_equity"},
                        ], [], []))
    resp = http_harness.post("/api/close-ghost-strategies", body={})
    assert "breakout_CRDO.json" not in resp["body"]["closed"]


def test_close_ghost_strategies_skips_pending_sell(http_harness, monkeypatch):
    """If there's a pending SELL order for the ghost symbol, skip —
    position may be mid-exit, premature close would confuse the
    monitor."""
    http_harness.create_user()
    import auth
    user_dir = auth.user_data_dir(1)
    sdir = os.path.join(user_dir, "strategies")
    os.makedirs(sdir, exist_ok=True)
    path = os.path.join(sdir, "breakout_EXITING.json")
    with open(path, "w") as f:
        json.dump({"symbol": "EXITING", "strategy": "breakout",
                    "status": "active"}, f)
    _past = os.path.getmtime(path) - 3600
    os.utime(path, (_past, _past))
    import server
    monkeypatch.setattr(server, "_fetch_live_alpaca_state",
                        lambda *a, **kw: ({}, [], [
                            {"symbol": "EXITING", "side": "sell",
                             "type": "limit"},
                        ], []))
    resp = http_harness.post("/api/close-ghost-strategies", body={})
    assert "breakout_EXITING.json" not in resp["body"]["closed"]


# ----------------------------------------------------------------------------
# UI pins
# ----------------------------------------------------------------------------

def test_audit_modal_has_clean_up_ghosts_button():
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert 'onclick="closeGhostStrategies()"' in src
    assert "🧹 Clean Up Ghosts" in src


def test_dashboard_has_close_ghost_strategies_js_handler():
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "async function closeGhostStrategies" in src
    assert "/api/close-ghost-strategies" in src


def test_server_routes_close_ghost_strategies_endpoint():
    with open("server.py") as f:
        src = f.read()
    assert '"/api/close-ghost-strategies"' in src
    assert "handle_close_ghost_strategies" in src
