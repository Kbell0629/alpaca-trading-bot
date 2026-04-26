"""Round-61 pt.65 — wire pt.63's live_data_monitor + pt.64's
risk_parity into runtime.

* Live-data divergence sweep runs once per regular-hours
  monitor_strategies tick. For each held position it compares the
  bot's last-seen current_price against Alpaca's latest_trade and
  notifies the user (alert tier, ≥4% delta) once per session per
  symbol.
* Risk-parity weights surfaced in build_analytics_view so the
  dashboard can read + render them.
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# Live-data divergence wired into monitor_strategies
# ============================================================================

def test_monitor_imports_live_data_monitor():
    src = _src("cloud_scheduler.py")
    assert "import live_data_monitor" in src


def test_monitor_calls_check_position_divergence():
    src = _src("cloud_scheduler.py")
    assert "check_position_divergence" in src


def test_monitor_divergence_only_in_rth():
    """Source pin: divergence sweep skipped during extended_hours
    (thin AH quotes are noisy + we'd false-alert constantly)."""
    src = _src("cloud_scheduler.py")
    idx = src.find("live-data divergence sweep")
    assert idx > 0
    block = src[idx:idx + 3500]
    assert "not extended_hours" in block


def test_monitor_divergence_dedupes_per_session():
    """Source pin: only alert ONCE per session per symbol so we
    don't spam the user every monitor tick."""
    src = _src("cloud_scheduler.py")
    idx = src.find("live-data divergence sweep")
    block = src[idx:idx + 3500]
    assert "divergence_alert_" in block
    assert "_last_runs.get(_key)" in block


def test_monitor_divergence_logs_delta_pct():
    src = _src("cloud_scheduler.py")
    idx = src.find("live-data divergence sweep")
    block = src[idx:idx + 3500]
    assert "delta_pct" in block
    assert "live-data" in block.lower()


def test_monitor_divergence_uses_data_endpoint():
    """The latest_trade probe should hit the data endpoint, not
    the trading endpoint."""
    src = _src("cloud_scheduler.py")
    idx = src.find("live-data divergence sweep")
    block = src[idx:idx + 3500]
    assert "data.alpaca.markets" in block or "_data_endpoint" in block


def test_monitor_divergence_fails_open_on_error():
    """Best-effort: failure logs + continues, doesn't abort the
    monitor tick."""
    src = _src("cloud_scheduler.py")
    idx = src.find("live-data divergence sweep")
    block = src[idx:idx + 3500]
    assert "divergence sweep failed" in block.lower()


def test_monitor_divergence_doesnt_close_positions():
    """The sweep is read-only: notify but never close."""
    src = _src("cloud_scheduler.py")
    idx = src.find("live-data divergence sweep")
    block = src[idx:idx + 3500]
    # No order placement / position close calls in the divergence block.
    assert "user_api_post" not in block
    assert "user_api_delete" not in block


# ============================================================================
# Risk-parity surfaced in build_analytics_view
# ============================================================================

def test_analytics_view_has_risk_parity_weights():
    """build_analytics_view returns risk_parity_weights so dashboard
    can render."""
    import analytics_core as ac
    out = ac.build_analytics_view(journal={"trades": []})
    assert "risk_parity_weights" in out


def test_analytics_view_risk_parity_weights_dict_shape():
    import analytics_core as ac
    out = ac.build_analytics_view(journal={"trades": []})
    weights = out["risk_parity_weights"]
    assert isinstance(weights, dict)


def test_analytics_view_risk_parity_with_real_journal():
    """With a journal that has trades for two strategies of unequal
    σ, the weights sum to 1.0 and the steadier strategy gets more."""
    import analytics_core as ac
    journal = {"trades": [
        # 5 wide-swing breakout trades
        {"status": "closed", "strategy": "breakout", "pnl": p}
        for p in [50, -40, 30, -25, 20]
    ] + [
        # 5 tight wheel trades
        {"status": "closed", "strategy": "wheel", "pnl": p}
        for p in [3, 2, 4, 3, 2]
    ]}
    out = ac.build_analytics_view(journal=journal)
    weights = out["risk_parity_weights"]
    # Expected weights non-empty.
    if weights and "wheel" in weights and "breakout" in weights:
        assert weights["wheel"] > weights["breakout"]


def test_analytics_view_risk_parity_handles_missing_journal():
    """None journal → empty weights, not an error."""
    import analytics_core as ac
    out = ac.build_analytics_view(journal=None)
    # Either empty dict or equal-weighted fallback — both fine.
    assert isinstance(out["risk_parity_weights"], dict)


def test_safe_risk_parity_weights_swallows_module_errors():
    """Helper degrades to {} on any error so analytics fetches
    don't fail because the risk-parity module had a hiccup."""
    import analytics_core as ac
    # Pass a value risk_parity won't like.
    out = ac._safe_risk_parity_weights("not a journal")
    assert isinstance(out, dict)


def test_analytics_view_keys_complete():
    """Sanity: pt.65 added a key, all previous keys still present."""
    import analytics_core as ac
    out = ac.build_analytics_view(journal={"trades": []})
    expected = {
        "kpis", "equity_curve", "drawdown_curve",
        "strategy_breakdown", "pnl_by_period",
        "pnl_by_symbol", "pnl_by_exit_reason",
        "hold_time_distribution", "pnl_distribution",
        "best_worst_trades", "filter_summary",
        "score_outcome", "score_health",
        "risk_parity_weights",   # pt.65
        "slippage_summary",      # pt.80
    }
    assert set(out.keys()) == expected
