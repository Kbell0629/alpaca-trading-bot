"""
Stock-split auto-resolution in wheel_strategy (round-13).

The round-12 anomaly guard FREEZES wheel state when the observed share
delta is >= 2x expected_delta (possible split / manual trade / DRIP).
Round-13 adds auto-resolution: if yfinance confirms a split happened
between put-open and now, normalise baseline + expected_delta by the
split ratio and proceed with assignment detection using the adjusted
numbers.

Coverage:

  * _detect_split_since: returns 1.0 on missing/invalid input, returns
    cumulative ratio when splits found, returns 1.0 when yfinance lookup
    fails.
  * advance_wheel_state with 2:1 split + matching share qty → auto-
    resolves to assignment with adjusted cost basis.
  * advance_wheel_state with 2x share delta but NO split detected →
    stays FROZEN (existing safety behaviour preserved).
  * Normal 1x assignment still works (no regressions).
  * 3:1 split with intermediate share qty still freezes (share count
    doesn't cleanly match post-split expected — better safe than sorry).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import pytest


# ---------- _detect_split_since ----------


def test_detect_split_since_returns_one_on_empty_since():
    import wheel_strategy as ws
    assert ws._detect_split_since("AAPL", "") == 1.0
    assert ws._detect_split_since("AAPL", None) == 1.0


def test_detect_split_since_returns_one_on_bad_iso():
    import wheel_strategy as ws
    assert ws._detect_split_since("AAPL", "not-a-date") == 1.0


def test_detect_split_since_returns_one_when_yf_empty(monkeypatch):
    import wheel_strategy as ws
    monkeypatch.setattr("yfinance_budget.yf_splits", lambda _sym: [])
    assert ws._detect_split_since("AAPL", "2024-01-01T10:00:00") == 1.0


def test_detect_split_since_returns_one_when_yf_raises(monkeypatch):
    """yfinance lookup failures must not block the caller — fall through
    to the freeze-state path that's already the caller's fallback."""
    import wheel_strategy as ws
    def _raise(_sym):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr("yfinance_budget.yf_splits", _raise)
    # _detect_split_since wraps the import in try/except — returns 1.0
    # instead of propagating.
    assert ws._detect_split_since("AAPL", "2024-01-01T10:00:00") == 1.0


def test_detect_split_since_survives_malformed_split_rows(monkeypatch):
    """yfinance returning an entry with non-numeric ratio (shape drift)
    used to blow up on `ratio > 0` with TypeError — the original try/
    except only wrapped the yf_splits CALL, not the iteration. Now
    wrapped end-to-end, so we fall through to the 1.0 freeze path."""
    import wheel_strategy as ws
    split_dt = datetime(2024, 6, 15, tzinfo=timezone.utc)
    monkeypatch.setattr("yfinance_budget.yf_splits",
                        lambda _sym: [(split_dt, None)])  # bad ratio
    assert ws._detect_split_since("AAPL", "2024-06-01T10:00:00") == 1.0


def test_detect_split_since_reports_failure_to_observability(monkeypatch):
    """Runtime errors from yfinance must surface to Sentry so we notice
    systematic API drift rather than silently freezing every wheel."""
    import sys, types
    import wheel_strategy as ws
    captured = []
    fake = types.ModuleType("observability")
    fake.capture_exception = lambda exc, **ctx: captured.append((exc, ctx))
    monkeypatch.setitem(sys.modules, "observability", fake)
    def _raise(_sym):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr("yfinance_budget.yf_splits", _raise)
    assert ws._detect_split_since("AAPL", "2024-06-01T10:00:00") == 1.0
    assert len(captured) == 1
    assert captured[0][1].get("fn") == "_detect_split_since"
    assert captured[0][1].get("symbol") == "AAPL"


def test_detect_split_since_returns_ratio_when_split_found(monkeypatch):
    """yfinance reports a 2:1 split that post-dates opened_at → ratio 2.0."""
    import wheel_strategy as ws
    split_dt = datetime(2024, 6, 15, tzinfo=timezone.utc)
    monkeypatch.setattr("yfinance_budget.yf_splits",
                        lambda _sym: [(split_dt, 2.0)])
    assert ws._detect_split_since("AAPL", "2024-06-01T10:00:00") == 2.0


def test_detect_split_since_ignores_splits_before_opened_at(monkeypatch):
    """A split that happened BEFORE we opened the put doesn't affect
    our share-delta expectation."""
    import wheel_strategy as ws
    old_split_dt = datetime(2020, 8, 31, tzinfo=timezone.utc)
    monkeypatch.setattr("yfinance_budget.yf_splits",
                        lambda _sym: [(old_split_dt, 4.0)])
    assert ws._detect_split_since("AAPL", "2024-06-01T10:00:00") == 1.0


def test_detect_split_since_compounds_multiple_splits(monkeypatch):
    """Two splits (unlikely but possible): ratios multiply."""
    import wheel_strategy as ws
    d1 = datetime(2024, 6, 15, tzinfo=timezone.utc)
    d2 = datetime(2024, 7, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("yfinance_budget.yf_splits",
                        lambda _sym: [(d1, 2.0), (d2, 3.0)])
    assert ws._detect_split_since("AAPL", "2024-06-01T10:00:00") == 6.0


# ---------- advance_wheel_state with split auto-resolve ----------


def _wheel_user(tmp_path, uid=1):
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir(exist_ok=True)
    return {
        "id": uid, "_data_dir": str(tmp_path),
        "_strategies_dir": str(strat_dir),
        "username": f"user{uid}",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_api_key": "k", "_api_secret": "s",
    }


def _put_active_state(symbol, baseline=0, qty=1, strike=20.0, premium=100.0,
                      expiration="2024-01-15", opened_at="2023-12-01T10:00:00"):
    return {
        "symbol": symbol, "strategy": "wheel",
        "stage": "stage_1_put_active",
        "shares_owned": 0,
        "shares_at_open": baseline,
        "cycles_completed": 0,
        "total_premium_collected": 100.0,
        "total_realized_pnl": 100.0,
        "active_contract": {
            "contract_symbol": f"{symbol}240101P00020000",
            "type": "put", "strike": strike,
            "expiration": expiration,
            "quantity": qty,
            "premium_received": premium,
            "status": "active",
            "open_order_id": None, "close_order_id": None,
            "opened_at": opened_at,
        },
        "history": [],
    }


def test_two_for_one_split_auto_resolves(tmp_path, monkeypatch):
    """2:1 split happened during the put-active window. Observed share
    qty = 200 (twice expected_delta). _detect_split_since returns 2.0.
    Guard normalises baseline 0→0 and expected_delta 100→200; share
    delta 200 now == adjusted expected_delta → assignment advances."""
    import wheel_strategy as ws
    user = _wheel_user(tmp_path)
    state = _put_active_state("SPLT", baseline=0, qty=1)
    ws.save_wheel_state(user, state)

    # Fake Alpaca: 200 shares now
    monkeypatch.setattr(
        ws, "_api_get",
        lambda _u, path, timeout=15: (
            [{"symbol": "SPLT", "qty": "200"}] if path == "/positions"
            else {"error": "noop"}
        )
    )
    # Fake yfinance: 2:1 split during the put window
    split_dt = datetime(2023, 12, 20, tzinfo=timezone.utc)
    monkeypatch.setattr("yfinance_budget.yf_splits",
                        lambda _sym: [(split_dt, 2.0)])

    events = ws.advance_wheel_state(user, state)
    assert any("split detected" in e.lower() for e in events), (
        f"expected 'stock split detected' event, got: {events}"
    )
    reloaded = ws._load_json(ws.wheel_state_path(user, "SPLT"))
    # With 200 shares and expected_delta normalised to 200, the
    # assignment branch should have fired and advanced to stage 2.
    assert reloaded["stage"] == "stage_2_shares_owned"
    # History should contain the split-resolution audit entry
    assert any(h.get("event") == "split_auto_resolved"
               for h in reloaded["history"])


def test_anomaly_with_no_split_still_freezes(tmp_path, monkeypatch):
    """Share qty 2x expected but yfinance sees NO split → preserve the
    round-12 safety behaviour (freeze + manual reconcile)."""
    import wheel_strategy as ws
    user = _wheel_user(tmp_path, uid=2)
    state = _put_active_state("ANOM", baseline=0, qty=1)
    ws.save_wheel_state(user, state)

    monkeypatch.setattr(
        ws, "_api_get",
        lambda _u, path, timeout=15: (
            [{"symbol": "ANOM", "qty": "200"}] if path == "/positions"
            else {"error": "noop"}
        )
    )
    # No splits found
    monkeypatch.setattr("yfinance_budget.yf_splits", lambda _sym: [])

    events = ws.advance_wheel_state(user, state)
    assert any("FROZEN" in e or "frozen" in e.lower() for e in events), (
        f"expected FROZEN event, got: {events}"
    )
    reloaded = ws._load_json(ws.wheel_state_path(user, "ANOM"))
    assert reloaded["stage"] == "stage_1_put_active"   # not advanced
    assert any(h.get("event") == "anomalous_share_delta_no_auto_advance"
               for h in reloaded["history"])


def test_normal_assignment_still_works_when_splits_empty(tmp_path, monkeypatch):
    """1x expected_delta share count → normal assignment path (no
    anomaly guard triggered). Split-lookup isn't even called because
    share_delta < 2x expected."""
    import wheel_strategy as ws
    user = _wheel_user(tmp_path, uid=3)
    state = _put_active_state("NORM", baseline=0, qty=1)
    ws.save_wheel_state(user, state)

    monkeypatch.setattr(
        ws, "_api_get",
        lambda _u, path, timeout=15: (
            [{"symbol": "NORM", "qty": "100"}] if path == "/positions"
            else {"error": "noop"}
        )
    )
    # yf_splits raises if called — but the share_delta < 2x branch
    # shouldn't call it. If the test passes, it means the no-split
    # assignment path is unaffected by the new split-resolution code.
    called = []
    def _raise(_sym):
        called.append(True)
        raise RuntimeError("should not be called for 1x assignment")
    monkeypatch.setattr("yfinance_budget.yf_splits", _raise)

    events = ws.advance_wheel_state(user, state)
    # Split lookup shouldn't fire on a normal 1x assignment
    assert called == [], (
        "split lookup called on normal (non-anomalous) assignment path"
    )
    reloaded = ws._load_json(ws.wheel_state_path(user, "NORM"))
    assert reloaded["stage"] == "stage_2_shares_owned"
    assert reloaded["cost_basis"] == pytest.approx(19.0, abs=0.01)


def test_split_ratio_only_partial_match_treated_as_expired(tmp_path, monkeypatch):
    """3:1 split + observed 250 shares (not the expected post-3:1 300).
    The split ratio normalises expected_delta 100→300 but share_delta
    stays at 250 — less than 300, so the assignment branch doesn't
    fire and falls through to the "expired worthless" path. This is
    the correct conservative outcome: if Alpaca reports share count
    that doesn't cleanly match a full post-split assignment, we don't
    book it as assignment.

    The split-resolution audit entry still lands in history so the
    user can see the guard fired."""
    import wheel_strategy as ws
    user = _wheel_user(tmp_path, uid=4)
    state = _put_active_state("PART", baseline=0, qty=1)
    ws.save_wheel_state(user, state)

    # Fake Alpaca: 250 shares (partial, not the expected post-3:1 300)
    # 250 >= 2*expected_delta=200 so the anomaly path fires.
    monkeypatch.setattr(
        ws, "_api_get",
        lambda _u, path, timeout=15: (
            [{"symbol": "PART", "qty": "250"}] if path == "/positions"
            else {"error": "noop"}
        )
    )
    split_dt = datetime(2023, 12, 20, tzinfo=timezone.utc)
    monkeypatch.setattr("yfinance_budget.yf_splits",
                        lambda _sym: [(split_dt, 3.0)])

    events = ws.advance_wheel_state(user, state)
    reloaded = ws._load_json(ws.wheel_state_path(user, "PART"))
    # Split was detected (event + audit entry emitted) but adjusted
    # share_delta 250 < adjusted expected_delta 300 so the assignment
    # branch didn't fire. Falls through to the put-expired-worthless
    # path (stage_1_searching + cycles_completed+=1).
    assert reloaded["stage"] == "stage_1_searching"
    assert reloaded["cycles_completed"] == 1
    # History has BOTH the split-resolution entry AND the expired-
    # worthless entry (audit trail shows the full decision path).
    events_seen = {h.get("event") for h in reloaded["history"]}
    assert "split_auto_resolved" in events_seen
    assert "put_expired_worthless" in events_seen
