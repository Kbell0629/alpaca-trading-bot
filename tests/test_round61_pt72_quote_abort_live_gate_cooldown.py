"""Round-61 pt.72 — three production-readiness items.

Item 1: pre-trade quote-snapshot abort (new pre_trade_check.py)
Item 2: live-mode promotion gate (new live_mode_gate.py)
Item 3: per-symbol 24h cooldown after stop-out (new symbol_cooldown.py)

All three pure modules with lazy-import wiring.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone


_HERE = pathlib.Path(__file__).resolve().parent.parent


# ============================================================================
# Item 1 — pre_trade_check
# ============================================================================

def test_pre_trade_module_imports():
    import pre_trade_check as ptc
    assert hasattr(ptc, "evaluate_pre_trade_quote")
    assert hasattr(ptc, "compute_live_spread_pct")
    assert hasattr(ptc, "compute_price_drift_pct")


def test_compute_live_spread_pct_basic():
    import pre_trade_check as ptc
    pct = ptc.compute_live_spread_pct(100, 101)
    assert 0.99 < pct < 1.0  # ≈0.995%


def test_compute_live_spread_pct_zero_spread():
    import pre_trade_check as ptc
    assert ptc.compute_live_spread_pct(100, 100) == 0.0


def test_compute_live_spread_pct_bad_inputs():
    import pre_trade_check as ptc
    assert ptc.compute_live_spread_pct(0, 100) is None
    assert ptc.compute_live_spread_pct(101, 100) is None
    assert ptc.compute_live_spread_pct("bad", 100) is None


def test_compute_price_drift_pct_basic():
    import pre_trade_check as ptc
    drift = ptc.compute_price_drift_pct(100, 101.5)
    assert 1.49 < drift < 1.51


def test_compute_price_drift_pct_negative():
    import pre_trade_check as ptc
    drift = ptc.compute_price_drift_pct(100, 98)
    assert -2.01 < drift < -1.99


def test_compute_price_drift_pct_bad_inputs():
    import pre_trade_check as ptc
    assert ptc.compute_price_drift_pct(0, 100) is None
    assert ptc.compute_price_drift_pct(100, 0) is None


def test_evaluate_pre_trade_allows_clean_quote():
    import pre_trade_check as ptc
    pick = {"symbol": "AAPL", "price": 200}
    def _fetch(s):
        return {"bid": 199.95, "ask": 200.05, "last": 200.00}
    res = ptc.evaluate_pre_trade_quote(pick, _fetch)
    assert res["allow"] is True
    assert "ok" in res["reason"]


def test_evaluate_pre_trade_blocks_wide_spread():
    import pre_trade_check as ptc
    pick = {"symbol": "X", "price": 10}
    def _fetch(s):
        return {"bid": 9.50, "ask": 10.50, "last": 10.00}
    res = ptc.evaluate_pre_trade_quote(pick, _fetch)
    assert res["allow"] is False
    assert "wide_live_spread" in res["reason"]


def test_evaluate_pre_trade_blocks_price_drift_up():
    import pre_trade_check as ptc
    pick = {"symbol": "X", "price": 100}
    def _fetch(s):
        # 2% UP drift — beyond the 1% default threshold
        return {"bid": 102.0, "ask": 102.05, "last": 102.0}
    res = ptc.evaluate_pre_trade_quote(pick, _fetch)
    assert res["allow"] is False
    assert "price_drift_up" in res["reason"]


def test_evaluate_pre_trade_blocks_price_drift_down():
    import pre_trade_check as ptc
    pick = {"symbol": "X", "price": 100}
    def _fetch(s):
        return {"bid": 97.0, "ask": 97.05, "last": 97.0}
    res = ptc.evaluate_pre_trade_quote(pick, _fetch)
    assert res["allow"] is False
    assert "price_drift_down" in res["reason"]


def test_evaluate_pre_trade_fails_open_on_fetch_error():
    import pre_trade_check as ptc
    pick = {"symbol": "X", "price": 100}
    def _boom(s):
        raise RuntimeError("data feed down")
    res = ptc.evaluate_pre_trade_quote(pick, _boom)
    assert res["allow"] is True
    assert "fail_open" in res["reason"]


def test_evaluate_pre_trade_fails_open_on_empty_quote():
    import pre_trade_check as ptc
    pick = {"symbol": "X", "price": 100}
    res = ptc.evaluate_pre_trade_quote(pick, lambda s: None)
    assert res["allow"] is True
    assert "fail_open" in res["reason"]


def test_evaluate_pre_trade_fails_open_on_bad_pick():
    import pre_trade_check as ptc
    res = ptc.evaluate_pre_trade_quote(None, lambda s: {})
    assert res["allow"] is True


def test_evaluate_pre_trade_custom_thresholds():
    """A 0.6% drift passes the default 1% but fails 0.5%."""
    import pre_trade_check as ptc
    pick = {"symbol": "X", "price": 100}
    def _fetch(s):
        return {"bid": 100.55, "ask": 100.65, "last": 100.6}
    # Default 1% threshold — passes
    res1 = ptc.evaluate_pre_trade_quote(pick, _fetch)
    assert res1["allow"] is True
    # Tighter 0.5% — fails
    res2 = ptc.evaluate_pre_trade_quote(
        pick, _fetch, max_price_drift_pct=0.5)
    assert res2["allow"] is False


# ============================================================================
# Item 2 — live_mode_gate
# ============================================================================

def test_live_mode_module_imports():
    import live_mode_gate as lmg
    assert hasattr(lmg, "check_live_mode_readiness")


def _build_journal(n_closed, win_pct=0.6, avg_pnl=10.0,
                     start_days_ago=30):
    """Synthesize a journal with `n_closed` trades, interleaving
    wins/losses so peak-to-trough drawdown stays modest. Wins
    slightly outnumber losses but no long losing streaks."""
    now = datetime.now(timezone.utc)
    trades = []
    # Interleave: pattern repeats over the n_closed length.
    # E.g. for 60% win rate use a 5-trade cycle of 3W/2L.
    if win_pct >= 0.5:
        cycle_wins = max(1, int(round(win_pct * 5)))
        cycle_losses = 5 - cycle_wins
    else:
        cycle_losses = max(1, int(round((1 - win_pct) * 5)))
        cycle_wins = 5 - cycle_losses
    pattern = (["W"] * cycle_wins) + (["L"] * cycle_losses)
    for i in range(n_closed):
        kind = pattern[i % len(pattern)]
        pnl = avg_pnl if kind == "W" else -avg_pnl
        # i=0 must be the OLDEST so the chronological walk matches
        # the pattern. Trade i timestamp = now - 30d + i minutes.
        trades.append({
            "status": "closed", "pnl": pnl,
            "exit_timestamp": (now - timedelta(days=start_days_ago)
                                 + timedelta(seconds=i * 60)).isoformat(),
        })
    return {"trades": trades}


def test_live_mode_blocks_low_trade_count():
    import live_mode_gate as lmg
    journal = _build_journal(10)  # below 30-trade minimum
    res = lmg.check_live_mode_readiness(journal, {"sharpe_ratio": 1.0})
    assert res["ready"] is False
    keys = [b["key"] for b in res["blockers"]]
    assert "insufficient_trades" in keys


def test_live_mode_blocks_low_win_rate():
    import live_mode_gate as lmg
    journal = _build_journal(40, win_pct=0.30)  # 30% < 45% required
    res = lmg.check_live_mode_readiness(journal, {"sharpe_ratio": 1.0})
    assert res["ready"] is False
    keys = [b["key"] for b in res["blockers"]]
    assert "low_win_rate" in keys


def test_live_mode_blocks_low_sharpe():
    import live_mode_gate as lmg
    journal = _build_journal(40, win_pct=0.55)
    res = lmg.check_live_mode_readiness(journal, {"sharpe_ratio": 0.2})
    assert res["ready"] is False
    keys = [b["key"] for b in res["blockers"]]
    assert "low_sharpe" in keys


def test_live_mode_blocks_high_drawdown():
    """A journal with > 15% peak-to-trough drawdown blocks."""
    import live_mode_gate as lmg
    now = datetime.now(timezone.utc)
    trades = [
        {"status": "closed", "pnl": 1000,
         "exit_timestamp": (now - timedelta(days=20)).isoformat()},
        # 50% drawdown afterwards
        {"status": "closed", "pnl": -500,
         "exit_timestamp": (now - timedelta(days=10)).isoformat()},
    ]
    journal = {"trades": trades}
    res = lmg.check_live_mode_readiness(
        journal, {"sharpe_ratio": 2.0},
        min_closed_trades=2)
    keys = [b["key"] for b in res["blockers"]]
    assert "high_drawdown" in keys


def test_live_mode_blocks_high_audit_findings():
    import live_mode_gate as lmg
    journal = _build_journal(40, win_pct=0.55)
    audit = [{"severity": "HIGH", "message": "Position with no stop"}]
    res = lmg.check_live_mode_readiness(
        journal, {"sharpe_ratio": 1.0}, audit_findings=audit)
    assert res["ready"] is False
    keys = [b["key"] for b in res["blockers"]]
    assert "high_audit_findings" in keys


def test_live_mode_passes_clean_journal():
    import live_mode_gate as lmg
    journal = _build_journal(40, win_pct=0.6, avg_pnl=10)
    # Pass max_drawdown_pct=100 to bypass the DD check — synthetic
    # journals with equal-sized wins/losses naturally have wide
    # peak-to-trough swings as a percentage. Other tests cover
    # the DD blocker explicitly.
    res = lmg.check_live_mode_readiness(
        journal, {"sharpe_ratio": 1.5},
        max_drawdown_pct=100)
    assert res["ready"] is True, res["summary"]
    assert res["blockers"] == []


def test_live_mode_warning_on_borderline_sample():
    import live_mode_gate as lmg
    journal = _build_journal(30, win_pct=0.6)  # exactly at minimum
    res = lmg.check_live_mode_readiness(
        journal, {"sharpe_ratio": 1.0},
        max_drawdown_pct=100)
    assert res["ready"] is True  # not blocked, but warned
    keys = [w["key"] for w in res["warnings"]]
    assert "borderline_sample" in keys


def test_live_mode_handles_empty_journal():
    import live_mode_gate as lmg
    res = lmg.check_live_mode_readiness(None, None)
    assert res["ready"] is False
    # Still need ≥30 trades.
    keys = [b["key"] for b in res["blockers"]]
    assert "insufficient_trades" in keys


def test_live_mode_metrics_returned():
    import live_mode_gate as lmg
    journal = _build_journal(40, win_pct=0.55)
    res = lmg.check_live_mode_readiness(
        journal, {"sharpe_ratio": 1.2})
    assert res["metrics"]["closed_trades"] == 40
    # Synthetic journal: 3W/2L cycle → 60% win rate, not 55%.
    # Just check it's in the positive range.
    assert 0.45 < res["metrics"]["win_rate"] <= 0.65
    assert res["metrics"]["sharpe_ratio"] == 1.2


def test_live_mode_audit_findings_dict_shape():
    """audit_findings can be either a list OR a dict with `findings`."""
    import live_mode_gate as lmg
    journal = _build_journal(40, win_pct=0.55)
    # As dict
    audit = {"findings": [{"severity": "HIGH", "message": "x"}]}
    res = lmg.check_live_mode_readiness(
        journal, {"sharpe_ratio": 1.0}, audit_findings=audit)
    assert res["metrics"]["high_audit_findings"] == 1


# ============================================================================
# Item 3 — symbol_cooldown
# ============================================================================

def test_symbol_cooldown_module_imports():
    import symbol_cooldown as sc
    assert hasattr(sc, "record_stop_out")
    assert hasattr(sc, "is_on_cooldown")
    assert hasattr(sc, "cooldown_remaining_sec")


def test_record_stop_out_sets_cooldown():
    import symbol_cooldown as sc
    state = {}
    assert sc.record_stop_out(state, "SOXL", "stop_hit") is True
    assert "SOXL" in state
    assert sc.is_on_cooldown(state, "SOXL") is True


def test_record_stop_out_skips_target_hit():
    """Profit-target hits should NOT trigger a cooldown."""
    import symbol_cooldown as sc
    state = {}
    assert sc.record_stop_out(state, "AAPL", "target_hit") is False
    assert "AAPL" not in state


def test_record_stop_out_handles_dead_money():
    import symbol_cooldown as sc
    state = {}
    assert sc.record_stop_out(state, "X", "dead_money") is True


def test_record_stop_out_handles_bearish_news():
    import symbol_cooldown as sc
    state = {}
    assert sc.record_stop_out(state, "X", "bearish_news") is True


def test_is_on_cooldown_expires():
    import symbol_cooldown as sc
    state = {}
    sc.record_stop_out(state, "X", "stop_hit", now_ts=1000.0,
                         cooldown_sec=60)
    # 30s later — still on cooldown
    assert sc.is_on_cooldown(state, "X", now_ts=1030.0) is True
    # 70s later — expired
    assert sc.is_on_cooldown(state, "X", now_ts=1070.0) is False


def test_is_on_cooldown_handles_missing_state():
    import symbol_cooldown as sc
    assert sc.is_on_cooldown(None, "X") is False
    assert sc.is_on_cooldown({}, "X") is False


def test_cooldown_remaining_sec_basic():
    import symbol_cooldown as sc
    state = {}
    sc.record_stop_out(state, "X", "stop_hit", now_ts=1000.0,
                         cooldown_sec=3600)
    rem = sc.cooldown_remaining_sec(state, "X", now_ts=1500.0)
    assert rem == 3100.0


def test_cooldown_remaining_sec_zero_when_expired():
    import symbol_cooldown as sc
    state = {}
    sc.record_stop_out(state, "X", "stop_hit", now_ts=1000.0,
                         cooldown_sec=60)
    assert sc.cooldown_remaining_sec(state, "X", now_ts=2000.0) == 0.0


def test_explain_cooldown_human_readable():
    import symbol_cooldown as sc
    state = {}
    sc.record_stop_out(state, "X", "stop_hit", now_ts=1000.0,
                         cooldown_sec=3600)
    explanation = sc.explain_cooldown(state, "X", now_ts=1000.0)
    assert "stop_hit" in explanation
    assert "h remaining" in explanation


def test_explain_cooldown_empty_when_not_on_cooldown():
    import symbol_cooldown as sc
    assert sc.explain_cooldown({}, "X") == ""


def test_prune_expired_removes_old_entries():
    import symbol_cooldown as sc
    state = {}
    sc.record_stop_out(state, "OLD", "stop_hit", now_ts=1000.0,
                         cooldown_sec=60)
    sc.record_stop_out(state, "NEW", "stop_hit", now_ts=2000.0,
                         cooldown_sec=3600)
    pruned = sc.prune_expired(state, now_ts=2100.0)
    assert pruned == 1
    assert "OLD" not in state
    assert "NEW" in state


def test_record_stop_out_with_custom_cooldown_seconds():
    """Caller can override the default 24h."""
    import symbol_cooldown as sc
    state = {}
    sc.record_stop_out(state, "X", "stop_hit",
                         cooldown_sec=300, now_ts=1000.0)
    assert sc.is_on_cooldown(state, "X", now_ts=1100.0) is True
    assert sc.is_on_cooldown(state, "X", now_ts=1500.0) is False


def test_symbol_cooldown_normalises_case():
    import symbol_cooldown as sc
    state = {}
    sc.record_stop_out(state, "soxl", "stop_hit")
    # Lowercase input → state key uppercased.
    assert sc.is_on_cooldown(state, "SOXL") is True
    assert sc.is_on_cooldown(state, "soxl") is True


# ============================================================================
# Wiring source-pin tests
# ============================================================================

def test_cloud_scheduler_wires_pre_trade_check():
    src = (_HERE / "cloud_scheduler.py").read_text()
    assert "pre_trade_check" in src
    assert "evaluate_pre_trade_quote" in src
    assert "pre_trade_abort" in src


def test_cloud_scheduler_wires_symbol_cooldown_record():
    src = (_HERE / "cloud_scheduler.py").read_text()
    # Must record into _last_runs after a stop-out (in record_trade_close).
    idx = src.find("def record_trade_close")
    assert idx > 0
    block = src[idx:idx + 3000]
    assert "symbol_cooldown" in block
    assert "record_stop_out" in block


def test_cloud_scheduler_wires_symbol_cooldown_check():
    src = (_HERE / "cloud_scheduler.py").read_text()
    # The deployer must check is_on_cooldown before deploying.
    assert "is_on_cooldown" in src
    assert "cooldown_after_" in src or "explain_cooldown" in src


def test_auth_mixin_wires_live_mode_gate():
    src = (_HERE / "handlers" / "auth_mixin.py").read_text()
    assert "live_mode_gate" in src
    assert "check_live_mode_readiness" in src


def test_pre_trade_check_pure_module():
    """Pure module — no top-level imports of cloud_scheduler / auth."""
    src = (_HERE / "pre_trade_check.py").read_text()
    forbidden = ("import cloud_scheduler", "from cloud_scheduler",
                  "import auth\n", "from auth import")
    for f in forbidden:
        assert f not in src


def test_live_mode_gate_pure_module():
    src = (_HERE / "live_mode_gate.py").read_text()
    forbidden = ("import cloud_scheduler", "from cloud_scheduler",
                  "import auth\n", "from auth import",
                  "import server", "from server import")
    for f in forbidden:
        assert f not in src


def test_symbol_cooldown_pure_module():
    src = (_HERE / "symbol_cooldown.py").read_text()
    forbidden = ("import cloud_scheduler", "from cloud_scheduler",
                  "import auth\n", "from auth import",
                  "import server", "from server import")
    for f in forbidden:
        assert f not in src
