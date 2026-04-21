"""
Round-33 tests: trade-journal undercount fix + scroll-hint wrapper.

Before round-33, only cloud_scheduler.run_auto_deployer's main path
appended to trade_journal.json. Wheel puts (sold via
wheel_strategy.open_put) and manual deploys (via the dashboard Deploy
button → handlers/strategy_mixin.py) never wrote entries, so the
scorecard's total_trades counter undercounted reality.

Covered:
  * record_trade_open appends an 'open' entry with the expected shape
  * record_trade_open is idempotent-per-call (writes one entry)
  * record_trade_open survives missing journal file (creates it)
  * Second open entry for the same symbol coexists with the first (we
    support multiple sequential opens, each gets closed independently)
  * record_trade_close still matches the NEWEST open entry (pre-existing
    invariant — pinned so round-33 didn't regress it)
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def _tmp_user(tmp_path, monkeypatch):
    """Stub out user_file to use tmp_path so journal writes land there."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import sys as _sys
    # Force fresh imports that pick up DATA_DIR
    for m in ("cloud_scheduler", "scheduler_api"):
        if m in _sys.modules:
            del _sys.modules[m]
    import cloud_scheduler
    importlib.reload(cloud_scheduler)
    monkeypatch.setattr(cloud_scheduler, "user_file",
                        lambda user, fn: os.path.join(str(tmp_path), fn))
    return {"user": {"id": 1, "username": "test"}, "cs": cloud_scheduler,
            "path": str(tmp_path)}


def test_record_trade_open_creates_journal_with_open_entry(_tmp_user):
    cs = _tmp_user["cs"]
    user = _tmp_user["user"]
    cs.record_trade_open(user, "SOXL", "trailing_stop",
                          price=85.11, qty=117,
                          reason="Manual deploy", deployer="manual_dashboard")
    with open(os.path.join(_tmp_user["path"], "trade_journal.json")) as f:
        journal = json.load(f)
    trades = journal["trades"]
    assert len(trades) == 1
    t = trades[0]
    assert t["symbol"] == "SOXL"
    assert t["strategy"] == "trailing_stop"
    assert t["side"] == "buy"
    assert t["qty"] == 117
    assert t["price"] == 85.11
    assert t["status"] == "open"
    assert t["deployer"] == "manual_dashboard"
    assert "timestamp" in t


def test_record_trade_open_appends_to_existing_journal(_tmp_user):
    cs = _tmp_user["cs"]
    user = _tmp_user["user"]
    # Seed an existing trade
    seed = {"trades": [{"symbol": "OLD", "status": "closed"}],
            "daily_snapshots": []}
    with open(os.path.join(_tmp_user["path"], "trade_journal.json"), "w") as f:
        json.dump(seed, f)
    cs.record_trade_open(user, "INTC", "breakout", price=66.66, qty=63,
                          reason="Manual deploy")
    with open(os.path.join(_tmp_user["path"], "trade_journal.json")) as f:
        journal = json.load(f)
    assert len(journal["trades"]) == 2
    assert journal["trades"][0]["symbol"] == "OLD"  # preserved
    assert journal["trades"][1]["symbol"] == "INTC"
    assert journal["trades"][1]["status"] == "open"


def test_record_trade_open_handles_short_side_for_wheel(_tmp_user):
    """Wheel puts are SHORT positions — helper must accept sell_short
    side without mangling the price or qty."""
    cs = _tmp_user["cs"]
    user = _tmp_user["user"]
    cs.record_trade_open(
        user, "HIMS260508P00027000", "wheel",
        price=2.05, qty=-1, side="sell_short",
        reason="Sold-to-open put",
        deployer="wheel_auto_deploy",
        extra={"underlying": "HIMS", "option_side": "put"})
    with open(os.path.join(_tmp_user["path"], "trade_journal.json")) as f:
        journal = json.load(f)
    t = journal["trades"][0]
    assert t["side"] == "sell_short"
    assert t["qty"] == -1
    assert t["underlying"] == "HIMS"
    assert t["option_side"] == "put"


def test_record_trade_open_survives_journal_write_failure(_tmp_user, monkeypatch):
    """A broken journal write must NOT raise — the trade already
    happened in Alpaca; we can't undo it. Log + continue."""
    cs = _tmp_user["cs"]
    user = _tmp_user["user"]

    def _boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(cs, "save_json", _boom)

    # Should NOT raise
    cs.record_trade_open(user, "SOXL", "trailing_stop",
                          price=85.11, qty=117, reason="test")


def test_record_trade_close_still_matches_newest_open(_tmp_user):
    """Pin the pre-existing invariant: record_trade_close picks the
    NEWEST open entry for that symbol+strategy. Round-33 added a new
    append path; must not regress the close-matching logic."""
    cs = _tmp_user["cs"]
    user = _tmp_user["user"]
    # Two opens on the same symbol (e.g. two consecutive positions)
    cs.record_trade_open(user, "SOXL", "trailing_stop",
                          price=80.0, qty=50, reason="first")
    cs.record_trade_open(user, "SOXL", "trailing_stop",
                          price=85.0, qty=100, reason="second")
    # Close one — should match the newer entry (price 85)
    cs.record_trade_close(user, "SOXL", "trailing_stop",
                           exit_price=90.0, pnl=500.0,
                           exit_reason="test_close", qty=100)
    with open(os.path.join(_tmp_user["path"], "trade_journal.json")) as f:
        journal = json.load(f)
    trades = journal["trades"]
    assert len(trades) == 2
    # Newer entry should be closed, older still open
    assert trades[0]["status"] == "open"
    assert trades[0]["price"] == 80.0
    assert trades[1]["status"] == "closed"
    assert trades[1]["price"] == 85.0
    assert trades[1]["exit_reason"] == "test_close"
