"""Round-61 pt.39 — trend filter for long/short entry gating.

Most "fake breakouts" happen on stocks BELOW their longer-term trend
— they look exciting on the daily but are dead-cat-bouncing inside a
downtrend. Mirror problem on the short side: shorting strength inside
an uptrend is just buying the dip in disguise.

Pt.39 adds `apply_trend_filter` in screener_core.py: gates long
strategies (breakout/trailing_stop/mean_reversion/pead/copy_trading)
on price > 50-day SMA, and short strategies (short_sell) on price <
50-day SMA. Picks with insufficient bars pass through (fail open).
"""
from __future__ import annotations

from screener_core import (
    apply_trend_filter,
    _sma_from_closes,
    _LONG_STRATEGIES,
    _SHORT_STRATEGIES,
)


# ============================================================================
# _sma_from_closes
# ============================================================================

def test_sma_basic_50_day():
    closes = list(range(1, 51))  # 1..50
    assert _sma_from_closes(closes, 50) == sum(range(1, 51)) / 50


def test_sma_returns_none_when_too_few_bars():
    assert _sma_from_closes([1, 2, 3], 50) is None
    assert _sma_from_closes([], 50) is None
    assert _sma_from_closes(None, 50) is None


def test_sma_uses_last_period_bars_only():
    """If we have 100 bars and ask for SMA(50), use the last 50."""
    closes = [1] * 50 + [10] * 50
    assert _sma_from_closes(closes, 50) == 10.0


def test_sma_handles_string_numerics():
    """Alpaca bars sometimes carry stringified prices."""
    closes = ["10.0"] * 50
    assert _sma_from_closes(closes, 50) == 10.0


def test_sma_returns_none_on_corrupt_data():
    closes = ["nope"] * 50
    assert _sma_from_closes(closes, 50) is None


# ============================================================================
# apply_trend_filter — long gating
# ============================================================================

def _bars(closes):
    """Build Alpaca-style bar dicts from a closes list."""
    return [{"c": c} for c in closes]


def test_long_above_50ma_passes_through():
    """Price 110, SMA 100 → above MA → long strategy keeps its score."""
    picks = [{"symbol": "AAPL", "price": 110.0, "best_strategy": "breakout",
               "best_score": 50, "will_deploy": True}]
    bars_map = {"AAPL": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["best_strategy"] == "breakout"
    assert picks[0]["best_score"] == 50
    assert picks[0]["will_deploy"] is True
    assert picks[0]["above_sma_50"] is True
    assert picks[0]["sma_50"] == 100.0
    assert "_filtered_by_trend" not in picks[0]


def test_long_below_50ma_filtered():
    """Price 90, SMA 100 → below MA → long strategy gets filtered."""
    picks = [{"symbol": "ZZZ", "price": 90.0, "best_strategy": "breakout",
               "best_score": 50, "will_deploy": True}]
    bars_map = {"ZZZ": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["_filtered_by_trend"] == "below_sma"
    assert picks[0]["best_score"] == 0
    assert picks[0]["will_deploy"] is False
    assert picks[0]["best_strategy"] is None
    assert picks[0]["_filtered_strategy"] == "breakout"


def test_long_AT_50ma_filtered():
    """Price exactly == SMA is NOT above; should be filtered.
    Strict above comparison prevents marginal/tied cases from sneaking
    through."""
    picks = [{"symbol": "AAPL", "price": 100.0, "best_strategy": "trailing_stop",
               "best_score": 50}]
    bars_map = {"AAPL": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["_filtered_by_trend"] == "below_sma"


def test_all_long_strategies_get_filtered():
    """Every strategy in _LONG_STRATEGIES should be subject to the
    filter — not just breakout."""
    bars_map = {"X": _bars([100] * 50)}
    for strat in _LONG_STRATEGIES:
        picks = [{"symbol": "X", "price": 50.0, "best_strategy": strat,
                   "best_score": 50, "will_deploy": True}]
        apply_trend_filter(picks, bars_map, period=50)
        assert picks[0]["_filtered_by_trend"] == "below_sma", (
            f"strategy {strat} should be filtered when below SMA")


# ============================================================================
# apply_trend_filter — short gating
# ============================================================================

def test_short_below_50ma_passes_through():
    """Price 90, SMA 100 → below MA → short strategy is on-trend, keep."""
    picks = [{"symbol": "ZZZ", "price": 90.0, "best_strategy": "short_sell",
               "best_score": 30, "will_deploy": True}]
    bars_map = {"ZZZ": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["best_strategy"] == "short_sell"
    assert picks[0]["best_score"] == 30
    assert picks[0]["will_deploy"] is True
    assert picks[0]["above_sma_50"] is False


def test_short_above_50ma_filtered():
    """Price 110, SMA 100 → above MA → shorting strength inside an
    uptrend → filter."""
    picks = [{"symbol": "AAPL", "price": 110.0, "best_strategy": "short_sell",
               "best_score": 30, "will_deploy": True}]
    bars_map = {"AAPL": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["_filtered_by_trend"] == "above_sma"
    assert picks[0]["best_score"] == 0
    assert picks[0]["best_strategy"] is None


def test_short_AT_50ma_filtered():
    """Price == SMA → not strictly below → filter (mirror of long
    boundary case)."""
    picks = [{"symbol": "X", "price": 100.0, "best_strategy": "short_sell",
               "best_score": 30}]
    bars_map = {"X": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["_filtered_by_trend"] == "above_sma"


# ============================================================================
# Fail-open behaviours
# ============================================================================

def test_no_bars_passes_through():
    """If we don't have bars for the symbol, pass through unchanged.
    Never block a deploy on missing data."""
    picks = [{"symbol": "AAPL", "price": 50.0, "best_strategy": "breakout",
               "best_score": 50}]
    apply_trend_filter(picks, {}, period=50)
    assert picks[0]["best_strategy"] == "breakout"
    assert picks[0]["best_score"] == 50
    assert "_filtered_by_trend" not in picks[0]


def test_too_few_bars_passes_through():
    """20 bars but we asked for SMA(50) → fail open."""
    picks = [{"symbol": "AAPL", "price": 50.0, "best_strategy": "breakout",
               "best_score": 50}]
    bars_map = {"AAPL": _bars([100] * 20)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["best_strategy"] == "breakout"
    assert "_filtered_by_trend" not in picks[0]


def test_zero_price_passes_through():
    """Defensive: a pick with price=0 (corrupt snapshot) shouldn't
    crash the filter or get flagged."""
    picks = [{"symbol": "AAPL", "price": 0, "best_strategy": "breakout",
               "best_score": 50}]
    bars_map = {"AAPL": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["best_strategy"] == "breakout"
    assert "_filtered_by_trend" not in picks[0]


def test_no_best_strategy_passes_through():
    """A pick that wasn't selected for any strategy isn't gated by
    the filter — there's nothing to filter."""
    picks = [{"symbol": "AAPL", "price": 50.0, "best_strategy": None,
               "best_score": 0}]
    bars_map = {"AAPL": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    # Trend tag still added for visibility
    assert picks[0]["sma_50"] == 100.0
    assert picks[0]["above_sma_50"] is False
    # But no filter tag
    assert "_filtered_by_trend" not in picks[0]


def test_unknown_strategy_passes_through():
    """A pick with a strategy name not in the long/short sets
    (e.g., a future strategy not yet wired) passes through."""
    picks = [{"symbol": "X", "price": 50.0, "best_strategy": "future_strategy",
               "best_score": 50}]
    bars_map = {"X": _bars([100] * 50)}
    apply_trend_filter(picks, bars_map, period=50)
    assert picks[0]["best_strategy"] == "future_strategy"


# ============================================================================
# Mixed cases + sort-after-filter
# ============================================================================

def test_filter_resorts_so_filtered_picks_drop_to_bottom():
    """After filtering, the surviving picks should be sorted highest-
    score-first, with filtered (score=0) picks at the bottom."""
    picks = [
        {"symbol": "A", "price": 50.0, "best_strategy": "breakout",
          "best_score": 100, "will_deploy": True},  # filtered
        {"symbol": "B", "price": 110.0, "best_strategy": "breakout",
          "best_score": 80, "will_deploy": True},   # passes
        {"symbol": "C", "price": 105.0, "best_strategy": "breakout",
          "best_score": 90, "will_deploy": True},   # passes
    ]
    bars_map = {
        "A": _bars([100] * 50),
        "B": _bars([100] * 50),
        "C": _bars([100] * 50),
    }
    apply_trend_filter(picks, bars_map, period=50)
    # Order should be C (90) -> B (80) -> A (0, filtered)
    assert [p["symbol"] for p in picks] == ["C", "B", "A"]
    assert picks[2]["best_score"] == 0


def test_long_and_short_picks_filter_independently():
    """A pick set with both long and short candidates: long below MA
    gets filtered, short below MA stays, and vice versa."""
    picks = [
        {"symbol": "AAPL", "price": 110.0, "best_strategy": "breakout",
          "best_score": 50},   # long, above MA → keep
        {"symbol": "ZZZ", "price": 90.0, "best_strategy": "breakout",
          "best_score": 50},   # long, below MA → filter
        {"symbol": "MSFT", "price": 110.0, "best_strategy": "short_sell",
          "best_score": 30},   # short, above MA → filter
        {"symbol": "QQQ", "price": 90.0, "best_strategy": "short_sell",
          "best_score": 30},   # short, below MA → keep
    ]
    bars_map = {sym: _bars([100] * 50)
                 for sym in ("AAPL", "ZZZ", "MSFT", "QQQ")}
    apply_trend_filter(picks, bars_map, period=50)
    by_sym = {p["symbol"]: p for p in picks}
    assert by_sym["AAPL"]["best_strategy"] == "breakout"
    assert by_sym["QQQ"]["best_strategy"] == "short_sell"
    assert by_sym["ZZZ"]["_filtered_by_trend"] == "below_sma"
    assert by_sym["MSFT"]["_filtered_by_trend"] == "above_sma"


def test_filter_handles_empty_picks_list():
    """No picks → return same list, don't crash."""
    assert apply_trend_filter([], {}, period=50) == []
    assert apply_trend_filter(None, {}, period=50) is None


def test_filter_period_argument_respected():
    """SMA(20) check on 20 bars works (longer SMAs need more bars).
    Caller (update_dashboard) defaults to 50; tests exercise other
    period values to confirm flexibility."""
    picks = [{"symbol": "X", "price": 90.0, "best_strategy": "breakout",
               "best_score": 50}]
    bars_map = {"X": _bars([100] * 20)}
    apply_trend_filter(picks, bars_map, period=20)
    assert picks[0]["_filtered_by_trend"] == "below_sma"
    assert picks[0]["sma_20"] == 100.0
    assert picks[0]["above_sma_20"] is False


# ============================================================================
# Source-pin: wired into update_dashboard
# ============================================================================

def test_update_dashboard_calls_apply_trend_filter():
    """Pin that the screener pipeline actually invokes the filter.
    Without this wiring, the helper would exist but be dead code."""
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    assert "apply_trend_filter" in src
    assert "from screener_core import apply_trend_filter" in src


def test_update_dashboard_runs_trend_filter_after_factor_scores():
    """Order matters: trend filter must run AFTER factor scoring
    (so the strategy choice is final) and uses the same factor_bars
    that RS ranking uses (no extra API calls)."""
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    factor_idx = src.find("apply_factor_scores(top_candidates")
    trend_idx = src.find("apply_trend_filter(top_candidates")
    assert factor_idx > 0
    assert trend_idx > 0
    assert trend_idx > factor_idx, (
        "trend filter must run AFTER apply_factor_scores so the "
        "best_strategy choice is finalized before gating")
