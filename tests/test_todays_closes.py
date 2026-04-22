"""
Round-34 tests: Today's Closes surfacing + orphan-close synthetic entry.

Before round-34, `record_trade_close` silently returned False when no
matching open journal entry existed (e.g. pre-round-33 wheel puts or
manual deploys that never got journaled).  The close disappeared —
nothing in the journal, no scorecard update, nothing surfaced to the
dashboard.  User had to guess from activity-log scrolling what actually
happened.

Covered:
  * record_trade_close creates a synthetic orphan_close entry when no
    matching open trade exists (was: silently returned False)
  * record_trade_close with matching open entry still works as before
    (pin pre-existing behavior against regression)
  * scan_todays_closes picks up closes with today's exit_timestamp
  * scan_todays_closes ignores yesterday's closes
  * scan_todays_closes sorts newest-first
  * scan_todays_closes tolerates malformed entries without crashing
  * scan_todays_closes accepts either a dict or a file path
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest


@pytest.fixture
def _tmp_user(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Round-46 CI-safety: reloading cloud_scheduler transitively
    # imports auth, which requires MASTER_ENCRYPTION_KEY to be set or
    # it raises at module load. Set it here so this fixture is
    # self-sufficient (same pattern as test_round46_dual_mode_fixes'
    # _reload helper and conftest.py's isolated_data_dir fixture).
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    import importlib
    import sys as _sys
    for m in ("cloud_scheduler", "scheduler_api", "auth"):
        if m in _sys.modules:
            del _sys.modules[m]
    import cloud_scheduler
    importlib.reload(cloud_scheduler)
    monkeypatch.setattr(cloud_scheduler, "user_file",
                        lambda user, fn: os.path.join(str(tmp_path), fn))
    return {"user": {"id": 1, "username": "test"}, "cs": cloud_scheduler,
            "path": str(tmp_path)}


# ---------- record_trade_close orphan fix ----------


def test_orphan_close_creates_synthetic_entry(_tmp_user):
    """Before round-34: close with no matching open entry silently
    returned False.  Now: creates a synthetic entry flagged orphan_close=True."""
    cs = _tmp_user["cs"]
    user = _tmp_user["user"]
    result = cs.record_trade_close(
        user, "HIMS260508P00027000", "wheel",
        exit_price=1.00, pnl=105.0,
        exit_reason="stop_triggered", qty=-1, side="buy")
    assert result is True
    with open(os.path.join(_tmp_user["path"], "trade_journal.json")) as f:
        journal = json.load(f)
    trades = journal["trades"]
    assert len(trades) == 1
    t = trades[0]
    assert t["symbol"] == "HIMS260508P00027000"
    assert t["strategy"] == "wheel"
    assert t["status"] == "closed"
    assert t["orphan_close"] is True
    assert t["exit_price"] == 1.00
    assert t["exit_reason"] == "stop_triggered"
    assert t["pnl"] == 105.0
    assert t["deployer"] == "record_trade_close_orphan"


def test_normal_close_still_matches_open_entry(_tmp_user):
    """Regression pin: with a matching open entry, record_trade_close
    updates it in place and does NOT create a synthetic entry."""
    cs = _tmp_user["cs"]
    user = _tmp_user["user"]
    cs.record_trade_open(user, "INTC", "breakout",
                          price=66.66, qty=63, reason="deploy")
    cs.record_trade_close(
        user, "INTC", "breakout",
        exit_price=62.04, pnl=-290.0,
        exit_reason="stop_triggered", qty=63, side="sell")
    with open(os.path.join(_tmp_user["path"], "trade_journal.json")) as f:
        journal = json.load(f)
    trades = journal["trades"]
    assert len(trades) == 1
    t = trades[0]
    assert t["status"] == "closed"
    assert t["exit_price"] == 62.04
    assert t.get("orphan_close") is None or t["orphan_close"] is False


# ---------- scan_todays_closes (pure module, no server reload) ----------


def test_scan_todays_closes_picks_up_today():
    from todays_closes import scan_todays_closes
    from et_time import now_et
    today_iso = now_et().replace(microsecond=0).isoformat()
    journal = {
        "trades": [
            {"symbol": "INTC", "strategy": "breakout", "status": "closed",
             "exit_timestamp": today_iso, "exit_price": 62.04,
             "exit_reason": "stop_triggered", "pnl": -290.0,
             "pnl_pct": -6.9, "qty": 63, "price": 66.66},
        ],
    }
    closes = scan_todays_closes(journal)
    assert len(closes) == 1
    assert closes[0]["symbol"] == "INTC"
    assert closes[0]["exit_reason"] == "stop_triggered"
    assert closes[0]["pnl"] == -290.0


def test_scan_todays_closes_ignores_yesterday():
    from todays_closes import scan_todays_closes
    from et_time import now_et
    yesterday = (now_et() - timedelta(days=1)).replace(microsecond=0).isoformat()
    journal = {
        "trades": [
            {"symbol": "STALE", "strategy": "breakout", "status": "closed",
             "exit_timestamp": yesterday, "exit_price": 10.0,
             "exit_reason": "stop_triggered", "pnl": -50.0, "qty": 10},
        ],
    }
    assert scan_todays_closes(journal) == []


def test_scan_todays_closes_newest_first():
    from todays_closes import scan_todays_closes
    from et_time import now_et
    # Anchor the test timestamps to noon ET so `now - 3 hours` doesn't
    # cross midnight when the CI runner executes between 00:00-03:00 ET.
    # Before this anchor, running the test at ~midnight ET filtered out
    # the "3 hours ago" trade as yesterday's date and the assertion
    # `== ["THIRD", "SECOND", "FIRST"]` saw only ["THIRD"].
    today = now_et().date()
    noon = now_et().replace(
        year=today.year, month=today.month, day=today.day,
        hour=12, minute=0, second=0, microsecond=0,
    )
    journal = {
        "trades": [
            {"symbol": "FIRST", "status": "closed",
             "exit_timestamp": (noon - timedelta(hours=3)).isoformat(),
             "exit_price": 10.0, "exit_reason": "x", "pnl": 1.0},
            {"symbol": "SECOND", "status": "closed",
             "exit_timestamp": (noon - timedelta(hours=1)).isoformat(),
             "exit_price": 20.0, "exit_reason": "x", "pnl": 2.0},
            {"symbol": "THIRD", "status": "closed",
             "exit_timestamp": noon.isoformat(),
             "exit_price": 30.0, "exit_reason": "x", "pnl": 3.0},
        ],
    }
    closes = scan_todays_closes(journal)
    assert [c["symbol"] for c in closes] == ["THIRD", "SECOND", "FIRST"]


def test_scan_todays_closes_tolerates_malformed_entries():
    from todays_closes import scan_todays_closes
    from et_time import now_et
    today_iso = now_et().replace(microsecond=0).isoformat()
    journal = {
        "trades": [
            {"symbol": "OPEN_TRADE", "strategy": "breakout", "status": "open"},
            {"symbol": "BAD_TS", "status": "closed", "exit_timestamp": "not-a-date"},
            "not_a_dict",
            {"symbol": "GOOD", "strategy": "breakout", "status": "closed",
             "exit_timestamp": today_iso, "exit_price": 10.0,
             "exit_reason": "stop_triggered", "pnl": 5.0},
        ],
    }
    closes = scan_todays_closes(journal)
    assert len(closes) == 1
    assert closes[0]["symbol"] == "GOOD"


def test_scan_todays_closes_missing_journal_returns_empty(tmp_path):
    from todays_closes import scan_todays_closes
    missing_path = str(tmp_path / "does_not_exist.json")
    assert scan_todays_closes(missing_path) == []
    # Also accepts an empty dict
    assert scan_todays_closes({}) == []
    # Also accepts None / invalid types
    assert scan_todays_closes(None) == []


def test_scan_todays_closes_accepts_file_path(tmp_path):
    """Helper accepts either a dict or a filesystem path."""
    from todays_closes import scan_todays_closes
    from et_time import now_et
    today_iso = now_et().replace(microsecond=0).isoformat()
    path = str(tmp_path / "journal.json")
    with open(path, "w") as f:
        json.dump({
            "trades": [
                {"symbol": "X", "status": "closed",
                 "exit_timestamp": today_iso, "exit_price": 1.0,
                 "exit_reason": "x", "pnl": 1.0},
            ],
        }, f)
    closes = scan_todays_closes(path)
    assert len(closes) == 1
    assert closes[0]["symbol"] == "X"
