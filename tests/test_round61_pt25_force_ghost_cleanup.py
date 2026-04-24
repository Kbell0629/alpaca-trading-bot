"""Round-61 pt.25 — remove grace period from manual ghost cleanup.

User ran the 🧹 Clean Up Ghosts button after pt.24 shipped. Toast
reported: "Closed 0 ghost file(s), skipped 2 (grace period or
pending sell)". CORZ + AXTI were getting skipped on every click
because `error_recovery.py`'s periodic Check 3 touches the files
(safe_save_json + atomic rename bumps mtime even on no-op), so the
10-min grace window was never satisfied.

Fix: the manual button path is explicit user intent. Remove the
mtime grace-period filter. Scheduled error_recovery.py Check 3
keeps its 10-min grace for autonomous safety (protects fresh-close
races when no human is in the loop), but /api/close-ghost-strategies
honors the click.

Pending-sell check remains — a position mid-exit shouldn't be
force-closed even by user click, because record_trade_close would
then fail to pair the sell with an open journal entry.
"""
from __future__ import annotations

import json
import os


def test_close_ghost_strategies_has_no_grace_period_filter():
    """Source-pin: the pt.25 change removes the mtime check."""
    with open("handlers/actions_mixin.py") as f:
        src = f.read()
    idx = src.find("def handle_close_ghost_strategies")
    assert idx > 0
    body = src[idx:idx + 5000]
    # The old mtime comparison must be gone.
    assert "_time.time() - mtime < 600" not in body, (
        "Pt.25 removes the 10-min grace period from the user-triggered "
        "ghost cleanup. The scheduled error_recovery.py cleanup keeps "
        "its grace period for autonomous safety.")
    # Pending-sell check still present.
    assert "pending_sells" in body, (
        "Pending-sell check must remain — force-closing mid-exit "
        "files would break record_trade_close pairing.")


def test_close_ghost_closes_file_modified_moments_ago(
        http_harness, monkeypatch):
    """Behavioral pin: a file written right now (well within the
    old 10-min grace) MUST be closed by the manual button."""
    http_harness.create_user()
    import auth
    user_dir = auth.user_data_dir(1)
    sdir = os.path.join(user_dir, "strategies")
    os.makedirs(sdir, exist_ok=True)
    path = os.path.join(sdir, "breakout_JUSTNOW.json")
    with open(path, "w") as f:
        json.dump({"symbol": "JUSTNOW", "strategy": "breakout",
                    "status": "active"}, f)
    # mtime = now, inside the old grace window.
    import server
    monkeypatch.setattr(server, "_fetch_live_alpaca_state",
                        lambda *a, **kw: ({}, [], [], []))
    resp = http_harness.post("/api/close-ghost-strategies", body={})
    assert resp["status"] == 200
    assert "breakout_JUSTNOW.json" in resp["body"]["closed"]
    after = json.loads(open(path).read())
    assert after["status"] == "closed"


def test_close_ghost_still_skips_pending_sell(http_harness, monkeypatch):
    """Safety guard that survives pt.25: if a sell order is pending
    on the symbol, DON'T force-close the strategy file. Position
    is mid-exit; closing the file now would make record_trade_close
    fail to pair the fill with an open journal entry."""
    http_harness.create_user()
    import auth
    user_dir = auth.user_data_dir(1)
    sdir = os.path.join(user_dir, "strategies")
    os.makedirs(sdir, exist_ok=True)
    path = os.path.join(sdir, "breakout_EXITING.json")
    with open(path, "w") as f:
        json.dump({"symbol": "EXITING", "strategy": "breakout",
                    "status": "active"}, f)
    import server
    monkeypatch.setattr(server, "_fetch_live_alpaca_state",
                        lambda *a, **kw: ({}, [], [
                            {"symbol": "EXITING", "side": "sell",
                             "type": "limit"},
                        ], []))
    resp = http_harness.post("/api/close-ghost-strategies", body={})
    assert "breakout_EXITING.json" not in resp["body"]["closed"]
    skipped_reasons = [s["reason"] for s in resp["body"]["skipped"]
                       if s["file"] == "breakout_EXITING.json"]
    assert "pending_sell" in skipped_reasons


def test_error_recovery_keeps_its_grace_period():
    """The scheduled error_recovery.py Check 3 still has a grace
    period — autonomous cleanup must not race with the monitor's
    normal close-handling. Only the user-triggered path removed it."""
    with open("error_recovery.py") as f:
        src = f.read()
    # Somewhere in error_recovery, a time-window check with 600s
    # should still exist for the stale-file sweep.
    assert ("time.time() - file_mtime < 600" in src
            or "file_mtime < 600" in src), (
        "error_recovery.py Check 3 must retain its 10-min grace "
        "period — autonomous schedule != user click.")
