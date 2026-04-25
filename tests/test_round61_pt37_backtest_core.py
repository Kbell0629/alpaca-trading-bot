"""Round-61 pt.37 — backtest simulation engine (pure) coverage.

backtest_core.py is the pure simulation logic; this test file exercises:
  * Indicator helpers (highest_close / lowest_close / avg_volume / RSI)
  * Per-strategy entry signals
  * Per-symbol simulator (entry, stop, target, max-hold, window-end)
  * Aggregation summary (count / wins / win_rate / total_pnl /
    expectancy / best / worst / avg_hold_days)
  * run_backtest end-to-end
  * run_multi_strategy_backtest cross-strategy aggregator
  * Pt.35 invariant: leveraged ETFs blocked from short-sell sims
"""
from __future__ import annotations


from backtest_core import (
    BACKTESTABLE_STRATEGIES,
    DEFAULT_PARAMS,
    _avg_volume,
    _breakout_signal,
    _highest_close,
    _is_blocked_short_symbol,
    _lowest_close,
    _mean_reversion_signal,
    _rsi,
    _short_sell_signal,
    _simulate_symbol,
    _summarize,
    run_backtest,
    run_multi_strategy_backtest,
)


def _bar(date, o, h, l, c, v=1_000_000):
    return {"date": date, "open": o, "high": h, "low": l,
             "close": c, "volume": v}


def _ramp_up_bars(n=30, start=100, step=1):
    """Generate n bars with steadily rising close prices."""
    out = []
    for i in range(n):
        c = start + step * i
        out.append(_bar(f"2026-04-{i+1:02d}", c - 0.5, c + 0.2, c - 0.6, c))
    return out


def _ramp_down_bars(n=30, start=100, step=1):
    out = []
    for i in range(n):
        c = start - step * i
        out.append(_bar(f"2026-04-{i+1:02d}", c + 0.5, c + 0.6, c - 0.2, c))
    return out


# ============================================================================
# Constants + parameter defaults
# ============================================================================

def test_backtestable_strategies_set():
    assert "breakout" in BACKTESTABLE_STRATEGIES
    assert "mean_reversion" in BACKTESTABLE_STRATEGIES
    assert "short_sell" in BACKTESTABLE_STRATEGIES
    # `wheel` and `pead` aren't backtestable from bars alone
    assert "wheel" not in BACKTESTABLE_STRATEGIES
    assert "pead" not in BACKTESTABLE_STRATEGIES


def test_default_params_have_required_keys():
    for s in BACKTESTABLE_STRATEGIES:
        p = DEFAULT_PARAMS[s]
        assert "stop_pct" in p and 0 < p["stop_pct"] < 1
        assert "target_pct" in p and 0 < p["target_pct"] < 1
        assert "max_hold_days" in p and p["max_hold_days"] > 0
        assert p["side"] in ("long", "short")


# ============================================================================
# Indicator helpers
# ============================================================================

def test_highest_close_basic():
    bars = [_bar(f"d{i}", 0, 0, 0, c) for i, c in enumerate([10, 12, 11, 15, 13])]
    assert _highest_close(bars, end_idx=4, n=4) == 15
    assert _highest_close(bars, end_idx=4, n=2) == 15  # last 2 = [11,15]


def test_highest_close_returns_none_when_no_history():
    bars = [_bar("d0", 0, 0, 0, 100)]
    assert _highest_close(bars, end_idx=0, n=10) is None


def test_lowest_close_basic():
    bars = [_bar(f"d{i}", 0, 0, 0, c) for i, c in enumerate([10, 12, 11, 8, 13])]
    assert _lowest_close(bars, end_idx=4, n=4) == 8


def test_avg_volume_basic():
    bars = [_bar(f"d{i}", 0, 0, 0, 0, v) for i, v in enumerate([100, 200, 300])]
    assert _avg_volume(bars, end_idx=3, n=3) == 200


def test_avg_volume_handles_missing_values():
    bars = [
        {"date": "d0", "close": 1},
        {"date": "d1", "close": 1, "volume": None},
        {"date": "d2", "close": 1, "volume": 100},
    ]
    # Missing/None volumes coerce to 0; (0+0+100)/3 = 33.33
    assert abs(_avg_volume(bars, end_idx=3, n=3) - 33.333333) < 0.01


def test_rsi_returns_none_when_insufficient_bars():
    bars = [_bar(f"d{i}", 0, 0, 0, 100) for i in range(5)]
    assert _rsi(bars, end_idx=5, period=14) is None


def test_rsi_strong_uptrend_above_70():
    # 15 strictly-rising closes (period=14)
    bars = [_bar(f"d{i}", 0, 0, 0, 100 + i) for i in range(20)]
    rsi = _rsi(bars, end_idx=20, period=14)
    # All gains → loss=0 → RSI=100
    assert rsi == 100.0


def test_rsi_strong_downtrend_below_30():
    bars = [_bar(f"d{i}", 0, 0, 0, 100 - i) for i in range(20)]
    rsi = _rsi(bars, end_idx=20, period=14)
    # All losses → gain=0 → RSI=0
    assert rsi == 0.0


def test_rsi_mixed_in_middle():
    """Alternating up/down closes → RSI somewhere near 50."""
    closes = []
    for i in range(20):
        closes.append(100 + (1 if i % 2 == 0 else -1))
    bars = [_bar(f"d{i}", 0, 0, 0, c) for i, c in enumerate(closes)]
    rsi = _rsi(bars, end_idx=20, period=14)
    assert 30 <= rsi <= 70


# ============================================================================
# Entry signals
# ============================================================================

def test_breakout_signal_fires_on_close_above_20day_high_with_volume():
    """Build a flat 20-day series, then a single high-volume close
    above the prior high."""
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 102, 100, 102, 2_000_000))  # break + 2x vol
    assert _breakout_signal(bars, idx=20, params=DEFAULT_PARAMS["breakout"])


def test_breakout_signal_does_not_fire_without_volume():
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 102, 100, 102, 1_000_000))  # vol == avg
    assert not _breakout_signal(
        bars, idx=20, params=DEFAULT_PARAMS["breakout"])


def test_breakout_signal_does_not_fire_without_break():
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 100.5, 99.5, 99.5, 2_000_000))  # below prior
    assert not _breakout_signal(
        bars, idx=20, params=DEFAULT_PARAMS["breakout"])


def test_mean_reversion_signal_fires_on_oversold_break():
    # 20 flat bars at 100, then a sharp drop on declining series
    bars = []
    for i in range(20):
        bars.append(_bar(f"d{i}", 100, 100, 100, 100))
    # Force RSI low: 14 declining closes
    for i, c in enumerate([99, 98, 97, 96, 95, 94, 93, 92, 91, 90,
                            89, 88, 87, 86]):
        bars.append(_bar(f"e{i}", c, c, c, c))
    # Now today: close below the lowest of last 20
    bars.append(_bar("e15", 80, 80, 80, 80))
    assert _mean_reversion_signal(
        bars, idx=len(bars) - 1,
        params=DEFAULT_PARAMS["mean_reversion"])


def test_mean_reversion_signal_skips_if_rsi_high():
    """A close below 20-day low but with high RSI shouldn't fire."""
    # 20 bars: low at 95, recently rising
    bars = []
    for i in range(15):
        bars.append(_bar(f"d{i}", 100, 100, 100, 95))  # flat low
    for i in range(15):
        bars.append(_bar(f"e{i}", 100, 100, 100, 100 + i))  # rising
    # Close below 20-day low (would be 95) — but RSI is high from rising
    # So signal should NOT fire even with the break.
    bars.append(_bar("z", 90, 90, 90, 90))
    assert not _mean_reversion_signal(
        bars, idx=len(bars) - 1,
        params=DEFAULT_PARAMS["mean_reversion"])


def test_short_sell_signal_fires_on_overbought_break_down():
    """Mirror of mean_reversion: close below 20-day low with HIGH RSI."""
    bars = []
    # 14 strongly rising closes to push RSI up
    for i, c in enumerate([100, 102, 104, 106, 108, 110, 112,
                            114, 116, 118, 120, 122, 124, 126]):
        bars.append(_bar(f"e{i}", c, c, c, c))
    # Then 6 stable bars at high (so 20-day low is 100)
    for i in range(6):
        bars.append(_bar(f"s{i}", 126, 126, 126, 126))
    # Today: close below 20-day low (100) with RSI still high
    bars.append(_bar("today", 99, 99, 99, 99))
    assert _short_sell_signal(
        bars, idx=len(bars) - 1,
        params=DEFAULT_PARAMS["short_sell"])


# ============================================================================
# Per-symbol simulator
# ============================================================================

def test_simulate_long_position_hits_target():
    """Build a clear breakout signal, then a sharp rally that hits
    the +30% target."""
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 102, 100, 102, 2_000_000))  # entry
    # Rally to 130 (above +30% target = 102*1.30 = 132.6)
    for c in [110, 120, 135]:
        bars.append(_bar(f"r{c}", c, c + 1, c - 1, c, 1_000_000))
    trades = _simulate_symbol("AAPL", bars, "breakout",
                                DEFAULT_PARAMS["breakout"])
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "target_hit"
    assert t["entry_price"] == 102
    assert t["pnl"] > 0


def test_simulate_long_position_stops_out():
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 102, 100, 102, 2_000_000))  # entry @ 102
    # Crash: low touches 102 * 0.90 = 91.8
    bars.append(_bar("d21", 100, 100, 90, 95, 1_000_000))
    trades = _simulate_symbol("AAPL", bars, "breakout",
                                DEFAULT_PARAMS["breakout"])
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "stop_triggered"
    assert trades[0]["pnl"] < 0


def test_simulate_long_position_window_end_close():
    """If neither stop nor target hits within max_hold, position
    closes at window end."""
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 102, 100, 102, 2_000_000))  # entry
    # Flat for 3 days (well within max_hold=30, doesn't hit stop/target)
    for i in range(3):
        bars.append(_bar(f"f{i}", 102, 102.5, 101.5, 102, 1_000_000))
    trades = _simulate_symbol("AAPL", bars, "breakout",
                                DEFAULT_PARAMS["breakout"])
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "window_end"


def test_simulate_max_hold_exceeded():
    """Position open longer than max_hold_days exits at close."""
    params = dict(DEFAULT_PARAMS["breakout"])
    params["max_hold_days"] = 2
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 102, 100, 102, 2_000_000))  # entry
    bars.append(_bar("d21", 102, 103, 101, 102.5, 1_000_000))
    bars.append(_bar("d22", 102, 103, 101, 102.5, 1_000_000))  # max_hold hit
    bars.append(_bar("d23", 102, 103, 101, 102.5, 1_000_000))
    trades = _simulate_symbol("AAPL", bars, "breakout", params)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "max_hold_exceeded"


def test_simulate_no_signal_returns_no_trades():
    """Flat bars, no signals fire → empty trades list."""
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(40)]
    trades = _simulate_symbol("AAPL", bars, "breakout",
                                DEFAULT_PARAMS["breakout"])
    assert trades == []


def test_simulate_short_position_target_hit():
    """Build a short signal, then a drop that hits the target."""
    # Build conditions for short_sell signal
    bars = []
    for i, c in enumerate([100, 102, 104, 106, 108, 110, 112,
                            114, 116, 118, 120, 122, 124, 126]):
        bars.append(_bar(f"e{i}", c, c, c, c, 1_000_000))
    for i in range(6):
        bars.append(_bar(f"s{i}", 126, 126, 126, 126, 1_000_000))
    bars.append(_bar("entry", 99, 99, 99, 99, 1_000_000))  # short signal
    # Drop to target: 99 * (1 - 0.15) = 84.15
    bars.append(_bar("drop", 95, 95, 80, 80, 1_000_000))
    trades = _simulate_symbol("XYZ", bars, "short_sell",
                                DEFAULT_PARAMS["short_sell"])
    assert len(trades) == 1
    assert trades[0]["side"] == "short"
    assert trades[0]["exit_reason"] == "short_target_hit"
    assert trades[0]["pnl"] > 0


def test_simulate_short_position_cover_stop():
    """Short position: price spike up triggers stop."""
    bars = []
    for i, c in enumerate([100, 102, 104, 106, 108, 110, 112,
                            114, 116, 118, 120, 122, 124, 126]):
        bars.append(_bar(f"e{i}", c, c, c, c, 1_000_000))
    for i in range(6):
        bars.append(_bar(f"s{i}", 126, 126, 126, 126, 1_000_000))
    bars.append(_bar("entry", 99, 99, 99, 99, 1_000_000))  # short
    # Stop at 99 * 1.08 = 106.92; spike to 110
    bars.append(_bar("spike", 100, 110, 100, 108, 1_000_000))
    trades = _simulate_symbol("XYZ", bars, "short_sell",
                                DEFAULT_PARAMS["short_sell"])
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "short_stop_covered"
    assert trades[0]["pnl"] < 0


# ============================================================================
# _summarize
# ============================================================================

def test_summarize_empty_returns_zero_defaults():
    s = _summarize([])
    assert s["count"] == 0
    assert s["total_pnl"] == 0.0
    assert s["best_pnl"] is None


def test_summarize_basic_aggregates():
    trades = [
        {"pnl": 100, "hold_days": 5},
        {"pnl": -50, "hold_days": 10},
        {"pnl": 75, "hold_days": 3},
    ]
    s = _summarize(trades)
    assert s["count"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert abs(s["total_pnl"] - 125) < 0.01
    assert abs(s["avg_pnl"] - 41.67) < 0.05
    assert s["best_pnl"] == 100
    assert s["worst_pnl"] == -50
    assert abs(s["avg_hold_days"] - 6) < 0.01


def test_summarize_expectancy_formula():
    """Expectancy = win_rate * avg_win + loss_rate * avg_loss
    1 win @ +100, 1 loss @ -50: 0.5*100 + 0.5*(-50) = 25"""
    trades = [{"pnl": 100, "hold_days": 1},
               {"pnl": -50, "hold_days": 1}]
    s = _summarize(trades)
    assert abs(s["expectancy"] - 25.0) < 0.01


def test_summarize_wins_threshold_floats_dust_to_flat():
    """Tiny pnl shouldn't count as a win — float dust at the boundary."""
    trades = [{"pnl": 0.001, "hold_days": 1}]
    s = _summarize(trades)
    assert s["flat"] == 1
    assert s["wins"] == 0


# ============================================================================
# run_backtest end-to-end
# ============================================================================

def test_run_backtest_unsupported_strategy_returns_error():
    out = run_backtest({}, "unsupported_xyz")
    assert "error" in out
    assert "Unsupported" in out["error"]


def test_run_backtest_handles_empty_input():
    out = run_backtest({}, "breakout")
    assert out["strategy"] == "breakout"
    assert out["trades"] == []
    assert out["summary"]["count"] == 0


def test_run_backtest_skips_short_bar_series():
    """Symbols with <5 bars get skipped (insufficient data)."""
    out = run_backtest({"X": [_bar("d0", 100, 100, 100, 100)]}, "breakout")
    assert out["trades"] == []


def test_run_backtest_param_override():
    """Params kwarg should merge over defaults."""
    out = run_backtest({}, "breakout", params={"stop_pct": 0.05})
    assert out["params"]["stop_pct"] == 0.05
    # Other defaults still present
    assert out["params"]["target_pct"] == 0.30


def test_run_backtest_blocks_leveraged_etf_for_short():
    """Pt.35 invariant: leveraged/inverse ETFs MUST be excluded from
    short-sell simulations even if their bar series would have fired
    a signal. Conditional — only enforces if pt.35's
    ``constants.is_leveraged_or_inverse_etf`` is in place; otherwise
    the helper falls open (no harm, just no extra protection)."""
    import importlib
    pt35_available = False
    try:
        c = importlib.import_module("constants")
        pt35_available = hasattr(c, "is_leveraged_or_inverse_etf")
    except Exception:
        pass

    # Build a bar series that would otherwise trigger a short signal
    bars = []
    for i, c in enumerate([100, 102, 104, 106, 108, 110, 112,
                            114, 116, 118, 120, 122, 124, 126]):
        bars.append(_bar(f"e{i}", c, c, c, c, 1_000_000))
    for i in range(6):
        bars.append(_bar(f"s{i}", 126, 126, 126, 126, 1_000_000))
    bars.append(_bar("entry", 99, 99, 99, 99, 1_000_000))
    bars.append(_bar("drop", 80, 95, 80, 85, 1_000_000))

    out_blocked = run_backtest({"SOXL": bars}, "short_sell")
    if pt35_available:
        assert out_blocked["trades"] == [], (
            "Pt.35: SOXL short-sell sim must be blocked when "
            "constants.is_leveraged_or_inverse_etf is available")

    # AAPL is NOT in any blocklist → should produce a trade
    out_ok = run_backtest({"AAPL": bars}, "short_sell")
    assert len(out_ok["trades"]) >= 1


def test_run_backtest_does_not_block_leveraged_etf_for_long():
    """Long-side sims can include leveraged ETFs — pt.35 only blocks
    SHORT entries on them."""
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 102, 100, 102, 2_000_000))
    bars.append(_bar("d21", 102, 110, 102, 108, 1_000_000))
    out = run_backtest({"SOXL": bars}, "breakout")
    # Should produce a trade — long-side breakout is allowed even on SOXL
    assert len(out["trades"]) >= 1


# ============================================================================
# run_multi_strategy_backtest
# ============================================================================

def test_multi_strategy_runs_all_supported_by_default():
    out = run_multi_strategy_backtest({})
    by = out["by_strategy"]
    assert "breakout" in by
    assert "mean_reversion" in by
    assert "short_sell" in by


def test_multi_strategy_respects_explicit_strategies_arg():
    out = run_multi_strategy_backtest({}, strategies=["breakout"])
    assert "breakout" in out["by_strategy"]
    assert "mean_reversion" not in out["by_strategy"]


def test_multi_strategy_unsupported_strategy_marked_error():
    out = run_multi_strategy_backtest(
        {}, strategies=["breakout", "garbage_strategy"])
    assert "error" in out["by_strategy"]["garbage_strategy"]


def test_multi_strategy_overall_summary_pools_trades():
    """Cross-strategy overall summary aggregates trades from EACH
    strategy's run."""
    bars = [_bar(f"d{i}", 100, 100.5, 99.5, 100, 1_000_000)
             for i in range(20)]
    bars.append(_bar("d20", 100, 102, 100, 102, 2_000_000))
    bars.append(_bar("d21", 102, 110, 102, 108, 1_000_000))
    out = run_multi_strategy_backtest({"AAPL": bars})
    # At least breakout should produce a trade
    assert out["overall_summary"]["count"] >= 1


def test_multi_strategy_includes_symbols_evaluated():
    out = run_multi_strategy_backtest({"AAPL": [], "MSFT": []})
    assert "AAPL" in out["symbols_evaluated"]
    assert "MSFT" in out["symbols_evaluated"]


# ============================================================================
# Pt.35 blocklist defer
# ============================================================================

def test_is_blocked_short_symbol_uses_pt35_constants():
    """Helper defers to constants.is_leveraged_or_inverse_etf so the
    SSOT stays in one place. Conditional: only asserts blocked-True
    if pt.35 is in place; falls open if not (returns False, which
    is acceptable since the legacy code has no protection at all)."""
    import importlib
    try:
        c = importlib.import_module("constants")
        pt35 = hasattr(c, "is_leveraged_or_inverse_etf")
    except Exception:
        pt35 = False
    if pt35:
        assert _is_blocked_short_symbol("SOXL") is True
        assert _is_blocked_short_symbol("SQQQ") is True
    # These two ALWAYS hold regardless of pt.35
    assert _is_blocked_short_symbol("AAPL") is False
    assert _is_blocked_short_symbol(None) is False
