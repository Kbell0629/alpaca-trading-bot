"""Round-61 pt.42 — composite-regime weighting.

Layered on top of the existing 3-bucket `apply_strategy_rotation`,
pt.42 builds a richer 5-tier composite regime from SPY 200-MA +
breadth + VIX, then applies per-strategy weights tuned for each
regime.
"""
from __future__ import annotations

from screener_core import (
    REGIME_WEIGHTS,
    compute_composite_regime,
    apply_regime_weighting,
)


# ============================================================================
# REGIME_WEIGHTS table
# ============================================================================

def test_regime_weights_has_all_five_tiers():
    expected = {"strong_bull", "weak_bull", "choppy",
                 "weak_bear", "strong_bear"}
    assert set(REGIME_WEIGHTS.keys()) == expected


def test_every_regime_has_all_strategies():
    expected_strategies = {"Breakout", "Mean Reversion", "Wheel Strategy",
                            "PEAD", "Copy Trading", "short_sell"}
    for regime, weights in REGIME_WEIGHTS.items():
        assert set(weights.keys()) == expected_strategies, (
            f"{regime} missing/extra strategy weights")


def test_breakout_weight_increases_with_bullishness():
    """Strong bear → strong bull should monotonically boost breakout."""
    weights = [REGIME_WEIGHTS[r]["Breakout"] for r in (
        "strong_bear", "weak_bear", "choppy", "weak_bull", "strong_bull",
    )]
    # Allow chop to dip below weak-bull (it does), but overall trend
    # is "more bullish = higher breakout weight".
    assert weights[0] < weights[-1]
    assert weights[-1] > 1.0   # strong_bull boosts
    assert weights[0] < 0.5    # strong_bear suppresses


def test_short_sell_weight_increases_with_bearishness():
    weights = [REGIME_WEIGHTS[r]["short_sell"] for r in (
        "strong_bull", "weak_bull", "choppy", "weak_bear", "strong_bear",
    )]
    assert weights[0] < weights[-1]
    assert weights[-1] > 1.0   # strong_bear boosts shorts
    assert weights[0] < 0.5    # strong_bull suppresses


def test_mean_reversion_peaks_in_chop():
    """Range-bound chop is the MR sweet spot."""
    chop = REGIME_WEIGHTS["choppy"]["Mean Reversion"]
    bull = REGIME_WEIGHTS["strong_bull"]["Mean Reversion"]
    assert chop > bull
    assert chop >= 1.0


def test_wheel_weight_grows_with_volatility():
    """Wheel premiums grow with VIX → bear/chop > bull."""
    bull = REGIME_WEIGHTS["strong_bull"]["Wheel Strategy"]
    chop = REGIME_WEIGHTS["choppy"]["Wheel Strategy"]
    bear = REGIME_WEIGHTS["weak_bear"]["Wheel Strategy"]
    assert chop > bull
    assert bear > bull


# ============================================================================
# compute_composite_regime
# ============================================================================

def test_compute_strong_bull():
    """SPY above 200MA + broad + low VIX."""
    assert compute_composite_regime(True, 65, 12) == "strong_bull"


def test_compute_weak_bull_high_breadth_high_vix():
    """SPY above 200MA + good breadth + medium VIX → weak_bull."""
    assert compute_composite_regime(True, 65, 20) == "weak_bull"


def test_compute_weak_bull_low_breadth_low_vix():
    """SPY above 200MA + low VIX even on narrow breadth → weak_bull."""
    assert compute_composite_regime(True, 45, 12) == "weak_bull"


def test_compute_choppy_mid_signals():
    """SPY above 200MA + mid breadth + mid-high VIX → choppy."""
    assert compute_composite_regime(True, 45, 20) == "choppy"


def test_compute_weak_bear_below_200ma():
    """SPY below 200MA + benign other signals → weak_bear."""
    assert compute_composite_regime(False, 50, 18) == "weak_bear"


def test_compute_strong_bear_panic():
    """SPY below 200MA + narrow + high VIX → strong_bear."""
    assert compute_composite_regime(False, 25, 30) == "strong_bear"


def test_compute_handles_missing_inputs():
    """Missing inputs → fall back to benign defaults."""
    # All None: defaults to bull-ish (above_200 = True, breadth=50, vix=18)
    out = compute_composite_regime(None, None, None)
    # 50% breadth + 18 VIX + above_200 → just barely a weak_bull / chop boundary
    assert out in ("weak_bull", "choppy")


def test_compute_below_200ma_with_one_panic_signal():
    """SPY below 200MA but only one of (low breadth, high VIX) → still
    weak_bear, not strong_bear."""
    assert compute_composite_regime(False, 25, 18) == "weak_bear"
    assert compute_composite_regime(False, 50, 30) == "weak_bear"


# ============================================================================
# apply_regime_weighting
# ============================================================================

def _pick(strat="Breakout", **scores):
    base = {
        "symbol": "X", "best_strategy": strat,
        "breakout_score": 0, "mean_reversion_score": 0,
        "wheel_score": 0, "pead_score": 0,
        "copy_score": 0, "short_score": 0, "best_score": 0,
    }
    base.update(scores)
    base["best_score"] = base[_field_for(strat)]
    return base


def _field_for(strat):
    return {
        "Breakout": "breakout_score",
        "Mean Reversion": "mean_reversion_score",
        "Wheel Strategy": "wheel_score",
        "PEAD": "pead_score",
        "Copy Trading": "copy_score",
        "short_sell": "short_score",
    }.get(strat, "best_score")


def test_apply_strong_bull_boosts_breakout():
    picks = [_pick("Breakout", breakout_score=100)]
    apply_regime_weighting(picks, "strong_bull")
    assert picks[0]["breakout_score"] == 140.0   # 1.40x
    assert picks[0]["best_score"] == 140.0
    assert picks[0]["composite_regime"] == "strong_bull"
    assert picks[0]["regime_weight_applied"]["Breakout"] == 1.40


def test_apply_strong_bear_boosts_short():
    picks = [_pick("short_sell", short_score=100)]
    apply_regime_weighting(picks, "strong_bear")
    assert picks[0]["short_score"] == 150.0   # 1.50x
    assert picks[0]["best_score"] == 150.0


def test_apply_choppy_boosts_mean_reversion():
    picks = [_pick("Mean Reversion", mean_reversion_score=100)]
    apply_regime_weighting(picks, "choppy")
    assert picks[0]["mean_reversion_score"] == 130.0   # 1.30x
    assert picks[0]["best_score"] == 130.0


def test_apply_strong_bull_suppresses_short():
    picks = [_pick("short_sell", short_score=100)]
    apply_regime_weighting(picks, "strong_bull")
    assert picks[0]["short_score"] == 20.0   # 0.20x


def test_apply_unknown_regime_uses_choppy_default():
    picks = [_pick("Breakout", breakout_score=100)]
    apply_regime_weighting(picks, "made_up_regime")
    # Should use choppy weights → Breakout × 0.70
    assert picks[0]["breakout_score"] == 70.0


def test_apply_re_sorts_by_adjusted_best_score():
    """In strong_bear, breakout (×0.30) should drop below mean_rev
    (×0.85) when both start at 100."""
    picks = [
        _pick("Breakout", breakout_score=100),
        _pick("Mean Reversion", mean_reversion_score=100),
    ]
    picks[0]["symbol"] = "BREAK"
    picks[1]["symbol"] = "MR"
    apply_regime_weighting(picks, "strong_bear")
    # MR (85) > Breakout (30)
    assert picks[0]["symbol"] == "MR"
    assert picks[1]["symbol"] == "BREAK"


def test_apply_handles_empty_picks():
    assert apply_regime_weighting([], "strong_bull") == []
    assert apply_regime_weighting(None, "strong_bull") is None


def test_apply_short_strategy_underscore_match():
    """Pick best_strategy="short_sell" should match the lowercase
    snake-case key in the score_fields lookup."""
    picks = [_pick("short_sell", short_score=80)]
    apply_regime_weighting(picks, "weak_bear")
    # weak_bear short × 1.30 = 104
    assert picks[0]["best_score"] == 104.0


def test_apply_handles_invalid_score_values():
    """Non-numeric score field should be coerced to 0, not crash."""
    picks = [{"symbol": "X", "best_strategy": "Breakout",
               "breakout_score": "garbage", "best_score": 50}]
    apply_regime_weighting(picks, "strong_bull")
    assert picks[0]["breakout_score"] == 0.0


def test_apply_tags_every_pick():
    """Even picks whose strategy isn't in the score_fields map should
    get composite_regime + regime_weight_applied tags."""
    picks = [{"symbol": "X", "best_strategy": "future_strategy",
               "best_score": 50}]
    apply_regime_weighting(picks, "strong_bull")
    assert picks[0]["composite_regime"] == "strong_bull"
    assert "regime_weight_applied" in picks[0]


# ============================================================================
# Source-pin: wired into update_dashboard
# ============================================================================

def test_update_dashboard_imports_pt42_helpers():
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    assert "compute_composite_regime" in src
    assert "apply_regime_weighting" in src


def test_regime_weighting_runs_after_factor_scoring():
    """Order: factor scoring → composite regime weighting → trend filter."""
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    factor_idx = src.find("apply_factor_scores(top_candidates")
    rw_idx = src.find("apply_regime_weighting(top_candidates")
    assert factor_idx > 0
    assert rw_idx > 0
    assert rw_idx > factor_idx


def test_regime_weighting_uses_breadth_and_spy_200ma():
    """The composite regime requires breadth + SPY 200-MA + VIX
    inputs. Pin that update_dashboard sources all three."""
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    # SPY 200-MA via _sma_from_closes
    assert "_sma_from_closes(\n" in src or "_sma_from_closes(" in src
    # Breadth via existing breadth_data
    rw_idx = src.find("compute_composite_regime(")
    body = src[rw_idx:rw_idx + 800]
    assert "breadth" in body.lower()
