"""Round-61 pt.47 — walk-forward harness + slippage/commission.

Two related additions to ``backtest_core``:
  1. ``_simulate_symbol`` accepts ``slippage_bps`` + ``commission_per_trade``
     params (default 0 → bit-identical to pt.37 behaviour).
  2. ``run_walk_forward_backtest`` slides a (train, test) window
     forward, picks the best param variant on each train slice and
     evaluates on the immediately-following test slice. Reports
     out-of-sample aggregate + overfit ratio.
"""
from __future__ import annotations

import datetime as _dt

import backtest_core as bc


# ============================================================================
# Helpers — generate synthetic OHLCV bars
# ============================================================================

def _bar(date_str, c, *, h=None, low=None, o=None, v=2_000_000):
    """Flat-ish bar centred on close `c`. Provide explicit high/low
    when the test needs to trigger a stop or target."""
    if o is None:
        o = c
    if h is None:
        h = c * 1.005
    if low is None:
        low = c * 0.995
    return {"date": date_str, "open": o, "high": h, "low": low,
            "close": c, "volume": v}


def _series(start_date, prices, *, vol_mult=None):
    """Build a list of bars from a price series. vol_mult lets one
    bar's volume spike (for breakout signal)."""
    out = []
    d = _dt.date.fromisoformat(start_date)
    for i, p in enumerate(prices):
        v = 2_000_000
        if vol_mult and i == vol_mult[0]:
            v = int(2_000_000 * vol_mult[1])
        out.append(_bar(d.isoformat(), p, v=v))
        d += _dt.timedelta(days=1)
    return out


# ============================================================================
# Slippage helper
# ============================================================================

def test_apply_slippage_long_entry_pays_more():
    out = bc._apply_slippage(100.0, "long", is_entry=True, slippage_bps=10)
    assert abs(out - 100.10) < 1e-9


def test_apply_slippage_long_exit_receives_less():
    out = bc._apply_slippage(100.0, "long", is_entry=False, slippage_bps=10)
    assert abs(out - 99.90) < 1e-9


def test_apply_slippage_short_entry_receives_less():
    out = bc._apply_slippage(100.0, "short", is_entry=True, slippage_bps=10)
    assert abs(out - 99.90) < 1e-9


def test_apply_slippage_short_exit_pays_more():
    out = bc._apply_slippage(100.0, "short", is_entry=False, slippage_bps=10)
    assert abs(out - 100.10) < 1e-9


def test_apply_slippage_zero_passes_through():
    assert bc._apply_slippage(123.45, "long", True, 0) == 123.45
    assert bc._apply_slippage(123.45, "long", False, 0) == 123.45
    assert bc._apply_slippage(123.45, "short", True, 0) == 123.45
    assert bc._apply_slippage(123.45, "short", False, 0) == 123.45


def test_apply_slippage_none_passes_through():
    assert bc._apply_slippage(50.0, "long", True, None) == 50.0


# ============================================================================
# Slippage — integrated through _simulate_symbol
# ============================================================================

def _build_breakout_bars():
    """Stable 100 → breakout day at idx 21 → drift up to target."""
    prices = [100.0] * 21 + [108.0] + [110.0] * 5 + [135.0] * 30
    bars = _series("2026-01-01", prices, vol_mult=(21, 3.0))
    # The breakout day needs higher daily high to register.
    bars[21]["high"] = 109.0
    return bars


def test_slippage_zero_matches_pt37_behavior():
    """slippage_bps=0 + commission=0 must be bit-identical to pt.37."""
    bars = _build_breakout_bars()
    p_no_slip = dict(bc.DEFAULT_PARAMS["breakout"])
    p_no_slip["slippage_bps"] = 0
    p_no_slip["commission_per_trade"] = 0
    trades_no_slip = bc._simulate_symbol("X", bars, "breakout", p_no_slip)

    p_default = dict(bc.DEFAULT_PARAMS["breakout"])
    trades_default = bc._simulate_symbol("X", bars, "breakout", p_default)

    assert len(trades_no_slip) == len(trades_default)
    for a, b in zip(trades_no_slip, trades_default):
        assert a["entry_price"] == b["entry_price"]
        assert a["exit_price"] == b["exit_price"]
        assert a["pnl"] == b["pnl"]


def test_slippage_reduces_long_pnl():
    """With slippage_bps=50 (0.5%) on entry + exit, P&L must be lower
    than the no-slippage case."""
    bars = _build_breakout_bars()
    p_no_slip = dict(bc.DEFAULT_PARAMS["breakout"])
    trades_no_slip = bc._simulate_symbol("X", bars, "breakout", p_no_slip)

    p_slip = dict(bc.DEFAULT_PARAMS["breakout"])
    p_slip["slippage_bps"] = 50
    trades_slip = bc._simulate_symbol("X", bars, "breakout", p_slip)

    assert len(trades_no_slip) == len(trades_slip) == 1
    assert trades_slip[0]["pnl"] < trades_no_slip[0]["pnl"]


def test_commission_subtracts_from_pnl():
    bars = _build_breakout_bars()
    p_no_comm = dict(bc.DEFAULT_PARAMS["breakout"])
    trades_no_comm = bc._simulate_symbol("X", bars, "breakout", p_no_comm)

    p_comm = dict(bc.DEFAULT_PARAMS["breakout"])
    p_comm["commission_per_trade"] = 1.50
    trades_comm = bc._simulate_symbol("X", bars, "breakout", p_comm)

    assert len(trades_no_comm) == 1 and len(trades_comm) == 1
    diff = trades_no_comm[0]["pnl"] - trades_comm[0]["pnl"]
    assert abs(diff - 1.50) < 0.01


def test_slippage_metadata_in_trade_record():
    bars = _build_breakout_bars()
    p = dict(bc.DEFAULT_PARAMS["breakout"])
    p["slippage_bps"] = 25
    p["commission_per_trade"] = 0.50
    trades = bc._simulate_symbol("X", bars, "breakout", p)
    assert trades, "expected at least one trade"
    t = trades[0]
    assert t["slippage_bps"] == 25
    assert t["commission"] == 0.50


# ============================================================================
# Walk-forward harness
# ============================================================================

def _build_long_bars(n=120, base=100.0):
    """Build n bars with periodic breakout opportunities so the
    walk-forward harness has signal to work with on every fold."""
    prices = []
    for i in range(n):
        if i % 21 == 20:
            # Breakout day every 21 bars
            prices.append(base + 8.0 + (i * 0.05))
        else:
            prices.append(base + (i * 0.05))
    bars = _series("2026-01-01", prices)
    # Bump volume + high on breakout days
    for i in range(20, n, 21):
        bars[i]["high"] = bars[i]["close"] + 1.0
        bars[i]["volume"] = 6_000_000
    return bars


def test_walk_forward_rejects_bad_strategy():
    out = bc.run_walk_forward_backtest({}, "nope")
    assert "error" in out


def test_walk_forward_requires_minimum_bars():
    bars_by_symbol = {"X": _build_long_bars(20)}
    out = bc.run_walk_forward_backtest(
        bars_by_symbol, "breakout", train_days=30, test_days=30)
    assert "error" in out
    assert "30" in out["error"] or "60" in out["error"]


def test_walk_forward_rejects_invalid_window_sizes():
    bars_by_symbol = {"X": _build_long_bars(120)}
    for bad in [(0, 30, 7), (30, 0, 7), (30, 30, 0)]:
        train, test, step = bad
        out = bc.run_walk_forward_backtest(
            bars_by_symbol, "breakout",
            train_days=train, test_days=test, step_days=step)
        assert "error" in out


def test_walk_forward_returns_expected_shape():
    bars_by_symbol = {"X": _build_long_bars(120)}
    out = bc.run_walk_forward_backtest(
        bars_by_symbol, "breakout",
        train_days=30, test_days=30, step_days=15)
    assert out.get("strategy") == "breakout"
    assert "folds" in out
    assert "fold_count" in out
    assert "aggregate_test_summary" in out
    assert "aggregate_train_expectancy" in out
    assert "aggregate_test_expectancy" in out
    assert "overfit_ratio" in out
    assert isinstance(out["folds"], list)


def test_walk_forward_produces_multiple_folds():
    """120 bars / step 15 → enough room for several (train+test=60d)
    folds. With train+test=60 and step=15 that's (120-60)/15+1 = 5
    folds."""
    bars_by_symbol = {"X": _build_long_bars(120)}
    out = bc.run_walk_forward_backtest(
        bars_by_symbol, "breakout",
        train_days=30, test_days=30, step_days=15)
    assert out["fold_count"] >= 2


def test_walk_forward_each_fold_has_required_fields():
    bars_by_symbol = {"X": _build_long_bars(120)}
    out = bc.run_walk_forward_backtest(
        bars_by_symbol, "breakout",
        train_days=30, test_days=30, step_days=15)
    for f in out["folds"]:
        assert "fold_idx" in f
        assert "train_window" in f and len(f["train_window"]) == 2
        assert "test_window" in f and len(f["test_window"]) == 2
        assert "best_params" in f
        assert "train_summary" in f
        assert "test_summary" in f


def test_walk_forward_best_params_from_grid():
    """The chosen best_params for each fold must come from the
    supplied grid."""
    grid = [
        {**bc.DEFAULT_PARAMS["breakout"], "stop_pct": 0.05},
        {**bc.DEFAULT_PARAMS["breakout"], "stop_pct": 0.10},
        {**bc.DEFAULT_PARAMS["breakout"], "stop_pct": 0.15},
    ]
    valid_stops = {p["stop_pct"] for p in grid}
    bars_by_symbol = {"X": _build_long_bars(120)}
    out = bc.run_walk_forward_backtest(
        bars_by_symbol, "breakout",
        train_days=30, test_days=30, step_days=15,
        param_grid=grid)
    for f in out["folds"]:
        assert f["best_params"]["stop_pct"] in valid_stops


def test_walk_forward_train_test_windows_sequential():
    """test_window starts immediately after train_window."""
    bars_by_symbol = {"X": _build_long_bars(120)}
    out = bc.run_walk_forward_backtest(
        bars_by_symbol, "breakout",
        train_days=30, test_days=30, step_days=15)
    for f in out["folds"]:
        # train_end_date < test_start_date
        train_end = f["train_window"][1]
        test_start = f["test_window"][0]
        assert train_end < test_start


def test_walk_forward_base_params_passed_through():
    """base_params (e.g. slippage_bps) should be applied to every
    grid variant."""
    bars_by_symbol = {"X": _build_long_bars(120)}
    base = {"slippage_bps": 50, "commission_per_trade": 1.0}
    out = bc.run_walk_forward_backtest(
        bars_by_symbol, "breakout",
        train_days=30, test_days=30, step_days=15,
        base_params=base)
    for f in out["folds"]:
        assert f["best_params"].get("slippage_bps") == 50
        assert f["best_params"].get("commission_per_trade") == 1.0


def test_walk_forward_overfit_ratio_computed_when_test_nonzero():
    bars_by_symbol = {"X": _build_long_bars(120)}
    out = bc.run_walk_forward_backtest(
        bars_by_symbol, "breakout",
        train_days=30, test_days=30, step_days=15)
    # Either we get a numeric ratio, or test was zero (None).
    if out["aggregate_test_expectancy"] != 0:
        assert out["overfit_ratio"] is not None
    else:
        assert out["overfit_ratio"] is None


def test_default_param_grid_includes_baseline():
    grid = bc._default_param_grid("breakout")
    base = bc.DEFAULT_PARAMS["breakout"]
    found_baseline = False
    for v in grid:
        if (v["stop_pct"] == base["stop_pct"]
                and v["target_pct"] == base["target_pct"]):
            found_baseline = True
            break
    assert found_baseline


def test_default_param_grid_size():
    """Grid should have 9 entries: baseline + 8 perturbations."""
    grid = bc._default_param_grid("breakout")
    assert len(grid) == 9


def test_slice_bars_handles_oob():
    bars = [{"date": "d1"}, {"date": "d2"}, {"date": "d3"}]
    assert bc._slice_bars(bars, -5, 100) == bars
    assert bc._slice_bars(bars, 5, 10) == []
    assert bc._slice_bars(bars, 1, 1) == []
    assert bc._slice_bars([], 0, 5) == []


def test_slice_universe_drops_empty_slices():
    bars_by_symbol = {
        "A": [{"date": "d1"}, {"date": "d2"}],
        "B": [],
        "C": [{"date": "d1"}],
    }
    out = bc._slice_universe(bars_by_symbol, 0, 1)
    assert "A" in out
    assert "B" not in out  # empty input
    assert "C" in out


def test_max_universe_len_picks_longest():
    bars_by_symbol = {
        "A": [{}] * 30,
        "B": [{}] * 100,
        "C": [{}] * 50,
    }
    assert bc._max_universe_len(bars_by_symbol) == 100


def test_max_universe_len_empty():
    assert bc._max_universe_len({}) == 0
    assert bc._max_universe_len(None) == 0


def test_window_dates_picks_min_max():
    universe = {
        "A": [{"date": "2026-01-01"}, {"date": "2026-01-30"}],
        "B": [{"date": "2026-01-05"}, {"date": "2026-02-10"}],
    }
    assert bc._window_dates(universe) == ["2026-01-01", "2026-02-10"]


def test_window_dates_empty():
    assert bc._window_dates({}) == [None, None]
