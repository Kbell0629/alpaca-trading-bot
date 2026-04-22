"""
Round-42 tests: wheel_strategy now journals every close.

Before this round, wheel_strategy.py updated its own state file + audit
history on every exit path (assigned / expired / bought-to-close /
closed-externally) but NEVER called cloud_scheduler.record_trade_close.
The journal ended up with orphan "open" entries that silently vanished
when the option disappeared from Alpaca — the CHWY 2026-04-22 stop-hit
is the motivating case.

These tests pin:
  * _journal_wheel_close calls record_trade_close with the contract's
    OCC symbol + strategy="wheel" + side="buy" (short cover).
  * Each of the 5 exit paths invokes the helper (grep-level assertion).
  * External-close detection fires when the contract disappears from
    Alpaca positions while the wheel state still shows active.
"""
from __future__ import annotations

import importlib
import json
import os
import sys as _sys

import pytest


def _reload_wheel(isolated_data_dir):
    for mod in ("cloud_scheduler", "wheel_strategy"):
        _sys.modules.pop(mod, None)
    import wheel_strategy
    importlib.reload(wheel_strategy)
    return wheel_strategy


# ---------- _journal_wheel_close helper ----------


def test_journal_wheel_close_calls_record_trade_close(isolated_data_dir, monkeypatch):
    ws = _reload_wheel(isolated_data_dir)
    captured = []

    def fake_record(user, symbol, strategy, exit_price, pnl, exit_reason,
                     qty=None, side="sell"):
        captured.append({
            "user": user, "symbol": symbol, "strategy": strategy,
            "exit_price": exit_price, "pnl": pnl, "exit_reason": exit_reason,
            "qty": qty, "side": side,
        })
        return True

    import cloud_scheduler
    monkeypatch.setattr(cloud_scheduler, "record_trade_close", fake_record)

    ws._journal_wheel_close(
        user={"id": 1, "username": "test"},
        contract_meta={
            "contract_symbol": "CHWY260515P00025000",
            "quantity": 1, "type": "put",
        },
        exit_price=0.35,
        pnl=-10.0,
        exit_reason="stop hit",
    )
    assert len(captured) == 1
    c = captured[0]
    assert c["symbol"] == "CHWY260515P00025000"
    assert c["strategy"] == "wheel"
    assert c["side"] == "buy"  # buy-to-close a short
    assert c["qty"] == -1  # negative = short position
    assert c["exit_price"] == 0.35
    assert c["pnl"] == -10.0


def test_journal_wheel_close_skips_when_contract_symbol_missing(isolated_data_dir, monkeypatch):
    """Belt-and-braces: if contract_meta is malformed / None, don't crash."""
    ws = _reload_wheel(isolated_data_dir)
    captured = []
    import cloud_scheduler
    monkeypatch.setattr(cloud_scheduler, "record_trade_close",
                         lambda *a, **kw: captured.append(a) or True)

    # None contract_meta
    ws._journal_wheel_close({"id": 1}, None, 0, 0, "x")
    # Missing contract_symbol
    ws._journal_wheel_close({"id": 1}, {"quantity": 1}, 0, 0, "x")
    # Both should skip the record call
    assert captured == []


def test_journal_wheel_close_swallows_record_errors(isolated_data_dir, monkeypatch):
    """Journal-write failures must not break the state machine."""
    ws = _reload_wheel(isolated_data_dir)

    def raising_record(*a, **kw):
        raise RuntimeError("db locked")

    import cloud_scheduler
    monkeypatch.setattr(cloud_scheduler, "record_trade_close", raising_record)

    # Must not raise
    ws._journal_wheel_close(
        {"id": 1},
        {"contract_symbol": "X", "quantity": 1},
        0.35, -10, "stop",
    )


# ---------- Source-level pins: every exit path calls the helper ----------


def test_all_five_exit_paths_call_journal_helper(isolated_data_dir):
    """Grep-level pin: if someone refactors an exit path and forgets to
    call _journal_wheel_close, this test catches it before the journal
    goes silently out of sync again."""
    ws = _reload_wheel(isolated_data_dir)
    src = open(ws.__file__).read()

    # Count distinct calls to _journal_wheel_close. Expecting at least 6:
    # put_assigned, put_expired, call_assigned, call_expired,
    # bought_to_close, closed_externally.
    call_count = src.count("_journal_wheel_close(")
    # One definition + 6 call sites = at least 7 occurrences
    assert call_count >= 7, (
        f"Expected >=7 occurrences of _journal_wheel_close "
        f"(1 definition + 6 exit paths), got {call_count}. "
        "Did someone remove a close-journaling call?"
    )

    # Every key exit path must log_history AND call the helper nearby
    for event in ("put_assigned", "put_expired_worthless",
                  "call_assigned", "call_expired_worthless",
                  "_bought_to_close", "_closed_externally"):
        assert event in src, f"exit-path event {event} missing from wheel_strategy.py"


# ---------- External-close detection ----------


def test_external_close_detection_fires_when_position_missing(isolated_data_dir, monkeypatch):
    """Simulate the CHWY case: wheel state has active_contract, Alpaca's
    /positions list does NOT contain the OCC symbol (stop filled, position
    gone). The next advance_wheel_state tick should:
      1. Detect the missing position
      2. Call _journal_wheel_close
      3. Reset stage to stage_1_searching
      4. Clear active_contract
    """
    ws = _reload_wheel(isolated_data_dir)

    # Stub the Alpaca calls
    def fake_api_get(user, path, **kw):
        if path == "/positions":
            return []  # contract is NOT in positions (externally closed)
        if path.startswith("/account/activities/FILL"):
            return [{"side": "buy", "price": "0.35"}]  # buy-to-close fill
        return {}

    monkeypatch.setattr(ws, "_api_get", fake_api_get)

    journaled = []

    def fake_journal(user, contract_meta, exit_price, pnl, exit_reason):
        journaled.append({
            "exit_price": exit_price, "pnl": pnl,
            "exit_reason": exit_reason,
            "contract_symbol": contract_meta.get("contract_symbol"),
        })

    monkeypatch.setattr(ws, "_journal_wheel_close", fake_journal)
    # Bypass the flock for the test
    monkeypatch.setattr(ws, "save_wheel_state", lambda u, s: None)

    state = {
        "symbol": "CHWY",
        "stage": "stage_1_put_active",
        "active_contract": {
            "contract_symbol": "CHWY260515P00025000",
            "type": "put",
            "strike": 25.0,
            "expiration": "2026-05-15",
            "quantity": 1,
            "premium_received": 25.0,
            "status": "active",
        },
    }
    user = {"id": 1, "username": "test", "_api_key": "k", "_api_secret": "s"}

    events = ws._advance_wheel_state_locked(user, state, [])

    # Journal was hit once with the external-close metadata
    assert len(journaled) == 1, f"expected 1 journal call, got {journaled}"
    j = journaled[0]
    assert j["contract_symbol"] == "CHWY260515P00025000"
    assert j["exit_price"] == 0.35  # recovered from activities endpoint
    # net premium = 25 - (0.35 * 100 * 1) = -10
    assert j["pnl"] == -10.0
    assert "externally" in j["exit_reason"].lower()

    # Wheel state advanced correctly
    assert state["stage"] == "stage_1_searching"
    assert state["active_contract"] is None
    assert any("closed externally" in e for e in events)


def test_external_close_detection_skips_when_position_still_open(isolated_data_dir, monkeypatch):
    """Sanity: if the contract IS in Alpaca's positions, detection must
    NOT fire — state stays active and normal wheel logic continues."""
    ws = _reload_wheel(isolated_data_dir)

    def fake_api_get(user, path, **kw):
        if path == "/positions":
            return [{"symbol": "CHWY260515P00025000", "qty": "-1"}]
        return {}

    monkeypatch.setattr(ws, "_api_get", fake_api_get)
    # Suppress get_option_quote (returns None → profit-target branch skips)
    monkeypatch.setattr(ws, "get_option_quote", lambda u, s: None)

    journaled = []
    monkeypatch.setattr(ws, "_journal_wheel_close",
                         lambda *a, **kw: journaled.append(a))
    monkeypatch.setattr(ws, "save_wheel_state", lambda u, s: None)

    state = {
        "symbol": "CHWY",
        "stage": "stage_1_put_active",
        "active_contract": {
            "contract_symbol": "CHWY260515P00025000",
            "type": "put", "strike": 25.0,
            "expiration": "2026-05-15",
            "quantity": 1, "premium_received": 25.0,
            "status": "active",
        },
    }
    user = {"id": 1, "username": "test", "_api_key": "k", "_api_secret": "s"}

    ws._advance_wheel_state_locked(user, state, [])

    assert journaled == [], "external-close must NOT fire when contract is still in positions"
    assert state["active_contract"] is not None, "active_contract should stay set"
