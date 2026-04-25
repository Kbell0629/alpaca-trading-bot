"""Round-61 pt.41 — per-strategy adaptive thresholds.

Self-correcting score multipliers based on each strategy's rolling
30-trade win rate. Cold strategies (< 40% win rate) get demoted so
only the strongest signals deploy; hot strategies (> 60%) get
boosted so the bot deploys more aggressively when the strategy is
working.

Reads the existing trade journal — no new infrastructure.
"""
from __future__ import annotations

from screener_core import (
    compute_strategy_win_rates,
    get_threshold_multiplier,
    apply_adaptive_thresholds,
    ADAPTIVE_LOOKBACK_DEFAULT,
    ADAPTIVE_MIN_SAMPLE,
    ADAPTIVE_COLD_WIN_RATE,
    ADAPTIVE_HOT_WIN_RATE,
    ADAPTIVE_MAX_DEMOTE,
    ADAPTIVE_MAX_BOOST,
)


# ============================================================================
# Constants pinned
# ============================================================================

def test_constants_have_reasonable_defaults():
    assert ADAPTIVE_LOOKBACK_DEFAULT == 30
    assert ADAPTIVE_MIN_SAMPLE == 5
    assert ADAPTIVE_COLD_WIN_RATE == 0.40
    assert ADAPTIVE_HOT_WIN_RATE == 0.60
    assert ADAPTIVE_MAX_DEMOTE == 0.70
    assert ADAPTIVE_MAX_BOOST == 1.30


# ============================================================================
# get_threshold_multiplier
# ============================================================================

def test_multiplier_below_min_sample_is_neutral():
    """< 5 closed trades for the strategy → no adjustment (still
    learning)."""
    assert get_threshold_multiplier(0.0, 0) == 1.0
    assert get_threshold_multiplier(1.0, 4) == 1.0


def test_multiplier_at_min_sample_starts_adjusting():
    """Exactly the min_sample threshold should engage."""
    # Cold strategy at the min sample boundary → demote
    assert get_threshold_multiplier(0.0, 5) == ADAPTIVE_MAX_DEMOTE


def test_multiplier_cold_strategy_is_max_demote():
    """Win rate at or below 40% → 0.70 multiplier (raise threshold)."""
    assert get_threshold_multiplier(0.0, 30) == 0.70
    assert get_threshold_multiplier(0.40, 30) == 0.70
    assert get_threshold_multiplier(0.30, 30) == 0.70


def test_multiplier_hot_strategy_is_max_boost():
    """Win rate at or above 60% → 1.30 multiplier (lower threshold)."""
    assert get_threshold_multiplier(1.0, 30) == 1.30
    assert get_threshold_multiplier(0.60, 30) == 1.30
    assert get_threshold_multiplier(0.75, 30) == 1.30


def test_multiplier_neutral_zone_interpolates():
    """50% (midpoint between 40 and 60) → ~1.00 (midpoint between
    0.70 and 1.30)."""
    mid = get_threshold_multiplier(0.50, 30)
    assert abs(mid - 1.0) < 0.001


def test_multiplier_below_neutral_demotes_more_than_neutral():
    """45% (closer to cold) → multiplier < 1.0."""
    mult = get_threshold_multiplier(0.45, 30)
    assert mult < 1.0
    assert mult > ADAPTIVE_MAX_DEMOTE


def test_multiplier_above_neutral_boosts_more_than_neutral():
    """55% (closer to hot) → multiplier > 1.0."""
    mult = get_threshold_multiplier(0.55, 30)
    assert mult > 1.0
    assert mult < ADAPTIVE_MAX_BOOST


def test_multiplier_clamped_to_range():
    """No matter what we pass, result must be within [demote, boost]."""
    for wr in [-1.0, 0.0, 0.5, 1.0, 2.0]:
        mult = get_threshold_multiplier(wr, 30)
        assert ADAPTIVE_MAX_DEMOTE <= mult <= ADAPTIVE_MAX_BOOST


# ============================================================================
# compute_strategy_win_rates
# ============================================================================

def _close_trade(strategy="breakout", pnl=10.0):
    return {
        "symbol": "X", "strategy": strategy,
        "side": "buy", "qty": 10, "price": 100.0,
        "status": "closed", "pnl": pnl,
        "timestamp": "2026-04-20T09:30:00-04:00",
        "exit_timestamp": "2026-04-22T15:55:00-04:00",
    }


def _open_trade(strategy="breakout"):
    return {
        "symbol": "X", "strategy": strategy,
        "side": "buy", "qty": 10, "price": 100.0,
        "status": "open",
        "timestamp": "2026-04-23T09:30:00-04:00",
    }


def test_win_rates_groups_by_strategy():
    journal = {"trades": [
        _close_trade("breakout", 100),
        _close_trade("breakout", -50),
        _close_trade("wheel", 30),
    ]}
    rates = compute_strategy_win_rates(journal, lookback=30)
    assert "breakout" in rates
    assert "wheel" in rates
    assert rates["breakout"]["count"] == 2
    assert rates["wheel"]["count"] == 1


def test_win_rates_correct_win_loss_count():
    journal = {"trades": [
        _close_trade("breakout", 100),    # win
        _close_trade("breakout", -50),    # loss
        _close_trade("breakout", 200),    # win
        _close_trade("breakout", -30),    # loss
    ]}
    rates = compute_strategy_win_rates(journal, lookback=30)
    assert rates["breakout"]["wins"] == 2
    assert rates["breakout"]["losses"] == 2
    assert rates["breakout"]["count"] == 4
    assert rates["breakout"]["win_rate"] == 0.5


def test_win_rates_skips_open_trades():
    journal = {"trades": [
        _close_trade("breakout", 100),
        _open_trade("breakout"),  # excluded
    ]}
    rates = compute_strategy_win_rates(journal)
    assert rates["breakout"]["count"] == 1


def test_win_rates_skips_unparseable_pnl():
    """orphan_close trades (round-34) can have pnl=None."""
    journal = {"trades": [
        _close_trade("breakout", 100),
        _close_trade("breakout", None),
    ]}
    rates = compute_strategy_win_rates(journal)
    assert rates["breakout"]["count"] == 1


def test_win_rates_lookback_caps_per_strategy():
    """If 50 closed breakout trades exist but lookback=30, only the
    most recent 30 (newest first) are counted."""
    trades = []
    # Add 50 trades — first 20 are losses, last 30 are wins
    for i in range(20):
        trades.append(_close_trade("breakout", -50))
    for i in range(30):
        trades.append(_close_trade("breakout", 100))
    journal = {"trades": trades}
    rates = compute_strategy_win_rates(journal, lookback=30)
    # Walking newest→oldest, first 30 we see are the wins
    assert rates["breakout"]["count"] == 30
    assert rates["breakout"]["wins"] == 30
    assert rates["breakout"]["losses"] == 0
    assert rates["breakout"]["win_rate"] == 1.0


def test_win_rates_empty_journal():
    assert compute_strategy_win_rates(None) == {}
    assert compute_strategy_win_rates({}) == {}
    assert compute_strategy_win_rates({"trades": []}) == {}


def test_win_rates_handles_corrupt_entries():
    journal = {"trades": [
        None,
        "garbage",
        _close_trade("breakout", 100),
    ]}
    rates = compute_strategy_win_rates(journal)
    assert rates["breakout"]["count"] == 1


# ============================================================================
# apply_adaptive_thresholds
# ============================================================================

def _pick(strat="breakout", score=50):
    return {"symbol": "X", "best_strategy": strat, "best_score": score}


def test_apply_demotes_cold_strategy():
    """Picks for a cold strategy get score × 0.70."""
    picks = [_pick("breakout", 100)]
    win_rates = {"breakout": {"wins": 1, "losses": 9, "count": 10,
                                "win_rate": 0.10}}
    apply_adaptive_thresholds(picks, win_rates)
    assert picks[0]["best_score"] == 70.0
    assert picks[0]["adaptive_multiplier"] == 0.70
    assert picks[0]["strategy_win_rate"] == 0.10
    assert picks[0]["strategy_sample_size"] == 10


def test_apply_boosts_hot_strategy():
    """Picks for a hot strategy get score × 1.30."""
    picks = [_pick("breakout", 100)]
    win_rates = {"breakout": {"wins": 8, "losses": 2, "count": 10,
                                "win_rate": 0.80}}
    apply_adaptive_thresholds(picks, win_rates)
    assert picks[0]["best_score"] == 130.0
    assert picks[0]["adaptive_multiplier"] == 1.30


def test_apply_neutral_strategy_unchanged():
    """50% win rate → multiplier=1.0 → score unchanged."""
    picks = [_pick("breakout", 100)]
    win_rates = {"breakout": {"wins": 5, "losses": 5, "count": 10,
                                "win_rate": 0.50}}
    apply_adaptive_thresholds(picks, win_rates)
    assert picks[0]["best_score"] == 100.0
    assert picks[0]["adaptive_multiplier"] == 1.0


def test_apply_strategy_with_too_few_trades_unchanged():
    """4 trades → below min_sample → no adjustment."""
    picks = [_pick("breakout", 100)]
    win_rates = {"breakout": {"wins": 0, "losses": 4, "count": 4,
                                "win_rate": 0.0}}
    apply_adaptive_thresholds(picks, win_rates)
    assert picks[0]["best_score"] == 100.0
    assert picks[0]["adaptive_multiplier"] == 1.0


def test_apply_strategy_with_no_journal_history_unchanged():
    """A strategy with no journal entries at all gets no adjustment
    (and no annotation tags either)."""
    picks = [_pick("breakout", 100)]
    win_rates = {}
    apply_adaptive_thresholds(picks, win_rates)
    assert picks[0]["best_score"] == 100.0
    assert "adaptive_multiplier" not in picks[0]


def test_apply_resorts_by_adjusted_score():
    """After adjustment, picks should be re-sorted by their NEW scores."""
    picks = [
        _pick("breakout", 100),   # cold → 70
        _pick("wheel", 80),        # hot  → 104
    ]
    win_rates = {
        "breakout": {"wins": 1, "losses": 9, "count": 10, "win_rate": 0.1},
        "wheel": {"wins": 8, "losses": 2, "count": 10, "win_rate": 0.8},
    }
    apply_adaptive_thresholds(picks, win_rates)
    # wheel (104) should come before breakout (70)
    assert picks[0]["best_strategy"] == "wheel"
    assert picks[1]["best_strategy"] == "breakout"


def test_apply_handles_strategy_name_case_normalisation():
    """Picks may have mixed-case names ("Breakout"), journal stores
    lowercase ("breakout"). Match should still work."""
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 100}]
    win_rates = {"breakout": {"wins": 8, "losses": 2, "count": 10,
                                "win_rate": 0.80}}
    apply_adaptive_thresholds(picks, win_rates)
    assert picks[0]["best_score"] == 130.0


def test_apply_handles_space_to_underscore():
    """Pick "Mean Reversion" should match journal "mean_reversion"."""
    picks = [{"symbol": "X", "best_strategy": "Mean Reversion",
               "best_score": 100}]
    win_rates = {"mean_reversion": {"wins": 1, "losses": 9, "count": 10,
                                       "win_rate": 0.10}}
    apply_adaptive_thresholds(picks, win_rates)
    assert picks[0]["best_score"] == 70.0


def test_apply_empty_picks():
    assert apply_adaptive_thresholds([], {}) == []
    assert apply_adaptive_thresholds(None, {}) is None


def test_apply_no_win_rates_passes_through():
    picks = [_pick("breakout", 100)]
    apply_adaptive_thresholds(picks, None)
    apply_adaptive_thresholds(picks, {})
    assert picks[0]["best_score"] == 100.0


# ============================================================================
# Source-pin: wired into update_dashboard
# ============================================================================

def test_update_dashboard_calls_adaptive_thresholds():
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    assert "compute_strategy_win_rates" in src
    assert "apply_adaptive_thresholds" in src


def test_update_dashboard_reads_journal_path_env():
    """Round-9 isolation: should respect JOURNAL_PATH env var so each
    user's screener subprocess sees their own journal, not the
    shared one."""
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    # Find the adaptive threshold block
    idx = src.find("apply_adaptive_thresholds")
    body = src[max(0, idx - 1500):idx + 500]
    assert 'os.environ.get(\n                    "JOURNAL_PATH"' in body or \
           'JOURNAL_PATH' in body


def test_adaptive_thresholds_runs_after_factor_scoring():
    """Order: factor scoring → trend filter → adaptive thresholds.
    Adaptive thresholds need final best_strategy to lookup win rate."""
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    factor_idx = src.find("apply_factor_scores(top_candidates")
    adaptive_idx = src.find("apply_adaptive_thresholds(top_candidates")
    assert factor_idx > 0
    assert adaptive_idx > 0
    assert adaptive_idx > factor_idx
