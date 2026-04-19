"""
Round-12 audit trading-logic fixes — regression tests.

Covers:
  1. Kill-switch deploy abort via threading.Event (cloud_scheduler.
     request_deploy_abort / clear_deploy_abort / deploy_should_abort).
  2. Trade-journal trim advisory lock (fcntl.flock) — concurrent trim
     calls serialize cleanly; if the lock is already held, we don't
     deadlock.
  3. Anomalous share-delta handling in wheel_strategy put-assignment
     detection — pins state when the delta is >= 2x expected (split /
     manual trade) instead of auto-advancing with a wrong cost basis.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest


# ---------- cloud_scheduler deploy abort event ----------


def test_deploy_abort_starts_clear():
    import cloud_scheduler as cs
    # Belt-and-suspenders: make sure any prior test leaked state is gone.
    cs.clear_deploy_abort()
    assert cs.deploy_should_abort() is False


def test_request_deploy_abort_signals_abort():
    import cloud_scheduler as cs
    cs.clear_deploy_abort()
    assert cs.deploy_should_abort() is False
    cs.request_deploy_abort()
    assert cs.deploy_should_abort() is True
    # And clearing re-arms:
    cs.clear_deploy_abort()
    assert cs.deploy_should_abort() is False


def test_deploy_abort_visible_across_threads():
    """The whole point of the threading.Event primitive: a deploy loop
    running in thread A sees the abort set by thread B immediately, not
    after the next disk round-trip."""
    import cloud_scheduler as cs
    cs.clear_deploy_abort()
    saw_abort = []
    barrier = threading.Barrier(2)

    def loop_in_thread():
        barrier.wait()
        # Simulate a tight deploy loop — poll the abort every iteration.
        for _ in range(1000):
            if cs.deploy_should_abort():
                saw_abort.append(True)
                return
            time.sleep(0.001)

    t = threading.Thread(target=loop_in_thread)
    t.start()
    barrier.wait()
    # Give the loop a moment to enter its poll, then signal.
    time.sleep(0.01)
    cs.request_deploy_abort()
    t.join(timeout=2.0)
    cs.clear_deploy_abort()
    assert saw_abort == [True], "deploy loop did not see the cross-thread abort"


# ---------- trade_journal trim lock ----------


def _mktrade(status, days_ago, symbol="X", pnl=None):
    from datetime import datetime, timedelta, timezone
    ET = timezone(timedelta(hours=-4))
    dt = datetime.now(ET) - timedelta(days=days_ago)
    iso = dt.isoformat()
    return {
        "symbol": symbol, "side": "sell" if status == "closed" else "buy",
        "qty": 10, "price": 100.0, "status": status,
        "timestamp": iso,
        "closed_at": iso if status == "closed" else None,
        "pnl": pnl,
    }


def test_trim_journal_concurrent_calls_both_complete(tmp_path):
    """Two threads calling trim_journal on the same path concurrently
    — both should complete without deadlock and without producing a
    corrupt archive. One strictly serializes before the other per the
    flock."""
    import trade_journal

    journal_path = str(tmp_path / "trade_journal.json")
    trades = [
        _mktrade("closed", days_ago=400, pnl=100, symbol="OLD1"),
        _mktrade("closed", days_ago=400, pnl=200, symbol="OLD2"),
        _mktrade("closed", days_ago=30, pnl=50, symbol="FRESH"),
    ]
    with open(journal_path, "w") as f:
        json.dump({"trades": trades}, f)

    results = []

    def worker():
        r = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
        results.append(r)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(timeout=5.0); t2.join(timeout=5.0)
    assert not t1.is_alive() and not t2.is_alive(), "trim deadlocked"
    # Both returned; one moved the trades, the other saw no work to do.
    total_moved = sum(r.get("moved", 0) for r in results)
    assert total_moved == 2, f"expected 2 moved across both calls; got {total_moved}"

    # Archive contents are correct — 2 moved trades.
    with open(trade_journal.archive_path_for(journal_path)) as f:
        arch = json.load(f)
    assert len(arch["trades"]) == 2
    assert {t["symbol"] for t in arch["trades"]} == {"OLD1", "OLD2"}


def test_trim_lock_file_cleans_up_after(tmp_path):
    import trade_journal
    journal_path = str(tmp_path / "trade_journal.json")
    with open(journal_path, "w") as f:
        json.dump({"trades": []}, f)
    trade_journal.trim_journal(journal_path)
    # .trim.lock file is written but the lock itself is released.
    # Another call must not block forever.
    t_start = time.time()
    trade_journal.trim_journal(journal_path)
    elapsed = time.time() - t_start
    assert elapsed < 1.0, f"second trim took {elapsed:.2f}s — lock may have leaked"


# ---------- wheel_strategy anomalous share-delta guard ----------


def test_wheel_anomalous_share_delta_pins_state(tmp_path, monkeypatch):
    """A share delta >= 2x expected (e.g., a 2:1 stock split during the
    put-active window) must NOT auto-advance the wheel to stage 2 with
    a silently-wrong cost basis. Instead: log + return an event warning
    and leave state on stage_1_put_active so the user reconciles."""
    import wheel_strategy as ws

    # Monkeypatch the subprocess to Alpaca so we don't hit the network.
    # We only need _api_get to return a positions list + a fake fetched
    # order for the fill-check path.
    user = {
        "id": 1, "_data_dir": str(tmp_path),
        "_strategies_dir": str(tmp_path / "strategies"),
        "username": "testuser",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_api_key": "k", "_api_secret": "s",
    }
    os.makedirs(user["_strategies_dir"], exist_ok=True)

    # Construct a state that's in stage_1_put_active (an order was
    # filled, put is live, waiting for expiry or assignment).
    past = "2024-01-15"    # explicit past expiry so date.today() > exp
    state = {
        "symbol": "SPLT",
        "strategy": "wheel",
        "stage": "stage_1_put_active",
        "shares_owned": 0,
        "shares_at_open": 0,
        "cycles_completed": 0,
        "total_premium_collected": 100.0,
        "total_realized_pnl": 100.0,
        "active_contract": {
            "contract_symbol": "SPLT240101P00020000",
            "type": "put",
            "strike": 20.0,
            "expiration": past,
            "quantity": 1,
            "premium_received": 100.0,
            "status": "active",
            "open_order_id": None,
            "close_order_id": None,
            "opened_at": "2023-12-01T10:00:00",
        },
        "history": [],
    }
    ws.save_wheel_state(user, state)

    # Fake Alpaca positions call returning 200 shares (split + assignment
    # or just a weird share delta — regardless, 2x expected).
    def _fake_api_get(_u, path, timeout=15):
        if path == "/positions":
            return [{"symbol": "SPLT", "qty": "200"}]
        # Any other path (e.g. /orders/{id}) — return empty to skip
        # the fill-status loop.
        return {"error": "noop"}

    monkeypatch.setattr(ws, "_api_get", _fake_api_get)

    events = ws.advance_wheel_state(user, state)
    # The anomaly guard should emit a WARN event.
    assert any("anomalous share delta" in e or "WARN anomalous" in e for e in events), (
        f"expected WARN event, got: {events}"
    )
    # State should NOT have advanced to stage_2.
    reloaded = ws._load_json(ws.wheel_state_path(user, "SPLT")) or {}
    assert reloaded.get("stage") == "stage_1_put_active", (
        f"state auto-advanced despite anomaly — stage={reloaded.get('stage')}"
    )
    # History should contain the audit event.
    history = reloaded.get("history", [])
    assert any(h.get("event") == "anomalous_share_delta_no_auto_advance" for h in history), (
        f"expected anomaly history entry, got: {history}"
    )


def test_wheel_normal_assignment_still_works(tmp_path, monkeypatch):
    """Sanity: the anomaly guard must NOT fire on a normal assignment
    (exactly expected_delta shares appeared)."""
    import wheel_strategy as ws

    user = {
        "id": 2, "_data_dir": str(tmp_path),
        "_strategies_dir": str(tmp_path / "strategies"),
        "username": "testuser",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_api_key": "k", "_api_secret": "s",
    }
    os.makedirs(user["_strategies_dir"], exist_ok=True)
    past = "2024-01-15"
    state = {
        "symbol": "NORM",
        "strategy": "wheel",
        "stage": "stage_1_put_active",
        "shares_owned": 0,
        "shares_at_open": 0,
        "cycles_completed": 0,
        "total_premium_collected": 100.0,
        "total_realized_pnl": 100.0,
        "active_contract": {
            "contract_symbol": "NORM240101P00020000",
            "type": "put",
            "strike": 20.0,
            "expiration": past,
            "quantity": 1,
            "premium_received": 100.0,
            "status": "active",
            "open_order_id": None,
            "close_order_id": None,
            "opened_at": "2023-12-01T10:00:00",
        },
        "history": [],
    }
    ws.save_wheel_state(user, state)

    def _fake_api_get(_u, path, timeout=15):
        if path == "/positions":
            return [{"symbol": "NORM", "qty": "100"}]   # exactly expected
        return {"error": "noop"}
    monkeypatch.setattr(ws, "_api_get", _fake_api_get)

    events = ws.advance_wheel_state(user, state)
    # Should have advanced to stage_2_shares_owned (normal assignment).
    reloaded = ws._load_json(ws.wheel_state_path(user, "NORM")) or {}
    assert reloaded.get("stage") == "stage_2_shares_owned", (
        f"normal assignment did not advance — stage={reloaded.get('stage')}"
    )
    # Cost basis = 20 - 100/100/1 = 19.0
    assert reloaded.get("cost_basis") == pytest.approx(19.0, abs=0.0001)
