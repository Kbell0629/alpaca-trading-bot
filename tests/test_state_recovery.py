"""
Round-16 state-recovery validator tests.

state_recovery surfaces consistency drift between wheel state files,
trade journal, and Alpaca-reported positions. It NEVER auto-fixes —
it only logs/captures so the operator notices before drift becomes
real-money damage.
"""
from __future__ import annotations

import json
import os


# ---------- reconcile_wheel_vs_positions ----------


def test_no_discrepancy_when_state_matches_positions():
    from state_recovery import reconcile_wheel_vs_positions
    states = {"AAPL": {"shares_owned": 100}}
    positions = [{"symbol": "AAPL", "qty": "100"}]
    assert reconcile_wheel_vs_positions(states, positions) == []


def test_no_discrepancy_when_state_says_zero():
    """Wheel state during stage_1 (pre-assignment) shows zero shares;
    not a discrepancy even if Alpaca shows zero too."""
    from state_recovery import reconcile_wheel_vs_positions
    states = {"AAPL": {"shares_owned": 0}}
    positions = []
    assert reconcile_wheel_vs_positions(states, positions) == []


def test_warning_when_state_says_owned_but_alpaca_zero():
    """Wheel says we own 100 but Alpaca shows none → manual close
    or stop-out. WARNING severity."""
    from state_recovery import reconcile_wheel_vs_positions
    states = {"AAPL": {"shares_owned": 100}}
    positions = []
    out = reconcile_wheel_vs_positions(states, positions)
    assert len(out) == 1
    assert out[0]["severity"] == "warning"
    assert out[0]["expected_shares"] == 100
    assert out[0]["actual_shares"] == 0


def test_warning_when_alpaca_has_fewer_shares():
    """Manual partial sale → wheel state out of sync."""
    from state_recovery import reconcile_wheel_vs_positions
    states = {"AAPL": {"shares_owned": 200}}
    positions = [{"symbol": "AAPL", "qty": "100"}]
    out = reconcile_wheel_vs_positions(states, positions)
    assert len(out) == 1
    assert out[0]["severity"] == "warning"
    assert out[0]["delta"] == -100


def test_info_severity_for_likely_split():
    """Alpaca shows 2× expected → likely 2:1 split, info severity
    (round-13 auto-resolver handles this)."""
    from state_recovery import reconcile_wheel_vs_positions
    states = {"NVDA": {"shares_owned": 100}}
    positions = [{"symbol": "NVDA", "qty": "200"}]
    out = reconcile_wheel_vs_positions(states, positions)
    assert len(out) == 1
    assert out[0]["severity"] == "info"


def test_handles_missing_or_malformed_qty():
    from state_recovery import reconcile_wheel_vs_positions
    states = {"AAPL": {"shares_owned": 100}}
    # qty is None → treated as 0
    positions = [{"symbol": "AAPL", "qty": None}]
    out = reconcile_wheel_vs_positions(states, positions)
    assert len(out) == 1
    assert out[0]["actual_shares"] == 0


def test_case_insensitive_symbol_match():
    from state_recovery import reconcile_wheel_vs_positions
    states = {"aapl": {"shares_owned": 100}}
    positions = [{"symbol": "AAPL", "qty": "100"}]
    assert reconcile_wheel_vs_positions(states, positions) == []


def test_empty_inputs_safe():
    from state_recovery import reconcile_wheel_vs_positions
    assert reconcile_wheel_vs_positions(None, None) == []
    assert reconcile_wheel_vs_positions({}, []) == []


# ---------- reconcile_journal_vs_positions ----------


def test_journal_no_orphan_when_matched():
    from state_recovery import reconcile_journal_vs_positions
    open_trades = [{"symbol": "AAPL", "status": "open", "id": "t1"}]
    positions = [{"symbol": "AAPL", "qty": "100"}]
    assert reconcile_journal_vs_positions(open_trades, positions) == []


def test_journal_orphan_when_position_missing():
    from state_recovery import reconcile_journal_vs_positions
    open_trades = [{"symbol": "AAPL", "status": "open", "id": "t1"}]
    positions = []
    out = reconcile_journal_vs_positions(open_trades, positions)
    assert len(out) == 1
    assert out[0]["symbol"] == "AAPL"
    assert out[0]["severity"] == "warning"


def test_journal_skips_closed_trades():
    """A trade marked closed in the journal shouldn't be flagged."""
    from state_recovery import reconcile_journal_vs_positions
    open_trades = [{"symbol": "AAPL", "status": "closed", "id": "t1"}]
    positions = []
    assert reconcile_journal_vs_positions(open_trades, positions) == []


# ---------- reconcile_user end-to-end ----------


def test_reconcile_user_with_no_files_returns_empty(tmp_path):
    from state_recovery import reconcile_user
    user = {"username": "test"}
    result = reconcile_user(
        user,
        wheel_states_path=str(tmp_path / "strategies"),
        journal_path=str(tmp_path / "journal.json"),
        fetch_positions=lambda: [],
    )
    assert result["wheel_discrepancies"] == []
    assert result["journal_discrepancies"] == []
    assert result["fetch_failed"] is False


def test_reconcile_user_loads_wheel_files(tmp_path):
    """Place a wheel_FOO.json and confirm it gets picked up."""
    from state_recovery import reconcile_user
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir()
    with open(strat_dir / "wheel_FOO.json", "w") as f:
        json.dump({"shares_owned": 100, "stage": "stage_2_shares_owned"}, f)
    result = reconcile_user(
        {"username": "test"},
        wheel_states_path=str(strat_dir),
        journal_path=str(tmp_path / "journal.json"),
        fetch_positions=lambda: [],  # Alpaca shows zero → discrepancy
    )
    assert len(result["wheel_discrepancies"]) == 1
    assert result["wheel_discrepancies"][0]["symbol"] == "FOO"
    assert result["wheel_discrepancies"][0]["severity"] == "warning"


def test_reconcile_user_handles_fetch_failure(tmp_path):
    """If Alpaca fetch raises, return empty discrepancies + flag."""
    from state_recovery import reconcile_user
    def _raise():
        raise ConnectionError("alpaca down")
    result = reconcile_user(
        {"username": "test"},
        wheel_states_path=str(tmp_path),
        journal_path=str(tmp_path / "journal.json"),
        fetch_positions=_raise,
    )
    assert result["fetch_failed"] is True
    assert result["wheel_discrepancies"] == []


def test_reconcile_user_journal_orphan_detection(tmp_path):
    """Open trade in journal + no Alpaca position → orphan."""
    from state_recovery import reconcile_user
    journal_path = str(tmp_path / "journal.json")
    with open(journal_path, "w") as f:
        json.dump({"open_trades": [
            {"symbol": "ORPHAN", "status": "open", "id": "trade-1"}
        ]}, f)
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir()
    result = reconcile_user(
        {"username": "test"},
        wheel_states_path=str(strat_dir),
        journal_path=journal_path,
        fetch_positions=lambda: [],
    )
    assert len(result["journal_discrepancies"]) == 1
    assert result["journal_discrepancies"][0]["symbol"] == "ORPHAN"


# ---------- report_to_observability ----------


def test_report_to_observability_does_not_raise(monkeypatch):
    """Even when Sentry isn't configured / capture_message blows up,
    the reporter should never propagate."""
    from state_recovery import report_to_observability
    user = {"username": "u"}
    result = {
        "wheel_discrepancies": [{
            "symbol": "X", "expected_shares": 100, "actual_shares": 0,
            "delta": -100, "severity": "warning", "hint": "h",
        }],
        "journal_discrepancies": [{
            "symbol": "Y", "trade_id": "t1",
            "severity": "warning", "hint": "h",
        }],
    }
    # Should not raise even with no observability available
    report_to_observability(user, result)


def test_report_to_observability_calls_capture_message(monkeypatch):
    """Verify capture_message is called per discrepancy."""
    from state_recovery import report_to_observability
    captured = []
    import sys
    import types
    fake_obs = types.ModuleType("observability")
    fake_obs.capture_message = lambda msg, level=None, **kw: captured.append(
        (msg, level, kw)
    )
    monkeypatch.setitem(sys.modules, "observability", fake_obs)
    user = {"username": "alice"}
    result = {
        "wheel_discrepancies": [{
            "symbol": "AAPL", "expected_shares": 100, "actual_shares": 0,
            "delta": -100, "severity": "warning", "hint": "h",
        }],
        "journal_discrepancies": [],
    }
    report_to_observability(user, result)
    assert len(captured) == 1
    msg, level, kw = captured[0]
    assert "alice/AAPL" in msg
    assert level == "warning"
    assert kw["component"] == "state_recovery"
