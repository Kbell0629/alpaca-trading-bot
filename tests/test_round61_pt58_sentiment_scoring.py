"""Round-61 pt.58 — confluence multiplier + pre-market gap penalty.

Three layers:
  1. ``position_sizing`` adds a confluence multiplier (counts
     positive signals on a pick: high score, news sentiment,
     LLM sentiment, insider cluster buy, multi-TF alignment).
  2. ``screener_core.apply_gap_penalty`` demotes picks with a
     3-8% intraday move (the grey zone between "real breakout"
     and "we'd be chasing").
  3. Both are wired into the auto-deployer / screener pipeline.
"""
from __future__ import annotations


# ============================================================================
# count_confluence_signals
# ============================================================================

def test_count_confluence_no_signals():
    import position_sizing as ps
    assert ps.count_confluence_signals({}) == 0
    assert ps.count_confluence_signals(None) == 0


def test_count_confluence_high_score_alone():
    import position_sizing as ps
    pick = {"best_score": 95}
    assert ps.count_confluence_signals(pick) == 1


def test_count_confluence_high_score_below_threshold():
    """`best_score < 80` must NOT count."""
    import position_sizing as ps
    assert ps.count_confluence_signals({"best_score": 79}) == 0


def test_count_confluence_news_sentiment_positive():
    import position_sizing as ps
    pick = {"news_sentiment": "positive"}
    assert ps.count_confluence_signals(pick) == 1


def test_count_confluence_negative_sentiment_doesnt_count():
    import position_sizing as ps
    assert ps.count_confluence_signals(
        {"news_sentiment": "negative"}) == 0
    assert ps.count_confluence_signals(
        {"news_sentiment": "neutral"}) == 0


def test_count_confluence_llm_sentiment():
    import position_sizing as ps
    assert ps.count_confluence_signals(
        {"llm_sentiment_score": 5}) == 1
    assert ps.count_confluence_signals(
        {"llm_sentiment_score": 3}) == 0   # at threshold, not over
    assert ps.count_confluence_signals(
        {"llm_sentiment_score": -5}) == 0


def test_count_confluence_insider_cluster_buy():
    import position_sizing as ps
    pick = {"insider_data": {"has_cluster_buy": True}}
    assert ps.count_confluence_signals(pick) == 1
    pick = {"insider_data": {"has_cluster_buy": False}}
    assert ps.count_confluence_signals(pick) == 0


def test_count_confluence_mtf_alignment():
    import position_sizing as ps
    assert ps.count_confluence_signals(
        {"mtf_alignment": "aligned"}) == 1
    assert ps.count_confluence_signals(
        {"mtf_alignment": "disaligned"}) == 0


def test_count_confluence_all_five_signals():
    import position_sizing as ps
    pick = {
        "best_score": 90,
        "news_sentiment": "positive",
        "llm_sentiment_score": 6,
        "insider_data": {"has_cluster_buy": True},
        "mtf_alignment": "aligned",
    }
    assert ps.count_confluence_signals(pick) == 5


def test_count_confluence_handles_invalid_types():
    import position_sizing as ps
    assert ps.count_confluence_signals(
        {"best_score": "high", "llm_sentiment_score": "bullish"}) == 0


# ============================================================================
# confluence_size_multiplier
# ============================================================================

def test_confluence_mult_zero_signals():
    import position_sizing as ps
    assert ps.confluence_size_multiplier(0) == ps.CONFLUENCE_MULT_FLOOR


def test_confluence_mult_one_signal_still_floor():
    """0 OR 1 signals → floor (0.7×) — single weak signal isn't
    enough to justify base size."""
    import position_sizing as ps
    assert ps.confluence_size_multiplier(1) == ps.CONFLUENCE_MULT_FLOOR


def test_confluence_mult_two_signals_neutral():
    import position_sizing as ps
    assert ps.confluence_size_multiplier(2) == 1.0


def test_confluence_mult_three_signals():
    import position_sizing as ps
    assert ps.confluence_size_multiplier(3) == 1.2


def test_confluence_mult_four_signals_ceiling():
    import position_sizing as ps
    assert ps.confluence_size_multiplier(4) == ps.CONFLUENCE_MULT_CEILING


def test_confluence_mult_five_signals_ceiling():
    import position_sizing as ps
    assert ps.confluence_size_multiplier(5) == ps.CONFLUENCE_MULT_CEILING


def test_confluence_mult_invalid_returns_one():
    import position_sizing as ps
    assert ps.confluence_size_multiplier("bad") == 1.0


# ============================================================================
# compute_full_size with confluence
# ============================================================================

def test_compute_full_size_confluence_scales_up_strong_pick():
    import position_sizing as ps
    pick = {
        "best_score": 95,
        "news_sentiment": "positive",
        "llm_sentiment_score": 8,
        "insider_data": {"has_cluster_buy": True},
        "mtf_alignment": "aligned",
    }
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        pick=pick,
    )
    assert out["confluence_count"] == 5
    assert out["confluence_multiplier"] == ps.CONFLUENCE_MULT_CEILING
    assert out["qty"] >= 100   # multiplier >= 1.5 means up-sized


def test_compute_full_size_confluence_scales_down_weak_pick():
    import position_sizing as ps
    pick = {
        "best_score": 50,           # below 80 threshold
        "news_sentiment": "neutral",
        "llm_sentiment_score": 0,
    }
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        pick=pick,
    )
    assert out["confluence_count"] == 0
    assert out["confluence_multiplier"] == ps.CONFLUENCE_MULT_FLOOR
    assert out["qty"] < 100


def test_compute_full_size_confluence_skipped_when_no_pick():
    """Backwards-compat: callers who don't pass `pick` get
    confluence_multiplier=1.0 (no change vs pt.49)."""
    import position_sizing as ps
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
    )
    assert out["confluence_multiplier"] == 1.0
    assert out["confluence_count"] == 0


def test_compute_full_size_confluence_disabled():
    import position_sizing as ps
    pick = {"best_score": 95, "mtf_alignment": "aligned"}
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        pick=pick, enable_confluence=False,
    )
    assert out["confluence_multiplier"] == 1.0


def test_compute_full_size_zero_base_qty_skips_all():
    import position_sizing as ps
    out = ps.compute_full_size(
        base_qty=0, strategy="breakout", symbol="AAPL",
        pick={"best_score": 95},
    )
    assert out["qty"] == 0
    assert "no sizing" in out["rationale"]


def test_compute_full_size_rationale_includes_confluence():
    import position_sizing as ps
    pick = {"best_score": 95, "mtf_alignment": "aligned",
             "news_sentiment": "positive"}
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        pick=pick,
    )
    assert "confluence" in out["rationale"]


# ============================================================================
# apply_gap_penalty (screener_core)
# ============================================================================

def test_apply_gap_penalty_no_picks():
    import screener_core as sc
    assert sc.apply_gap_penalty([]) == []


def test_apply_gap_penalty_below_threshold_unchanged():
    import screener_core as sc
    p = {"symbol": "X", "best_score": 100, "daily_change": 1.5}
    sc.apply_gap_penalty([p])
    assert p["best_score"] == 100
    assert "_gap_penalty_applied" not in p


def test_apply_gap_penalty_in_grey_zone_demoted():
    import screener_core as sc
    p = {"symbol": "X", "best_score": 100, "daily_change": 5.0}
    sc.apply_gap_penalty([p])
    # 100 * 0.85 = 85
    assert p["best_score"] == 85.0
    assert p["_gap_penalty_applied"] is True
    assert p["_gap_penalty_pct"] == 5.0


def test_apply_gap_penalty_above_block_threshold_unchanged():
    """8%+ gaps left alone — chase_block hard-blocks them at deploy."""
    import screener_core as sc
    p = {"symbol": "X", "best_score": 100, "daily_change": 12.0}
    sc.apply_gap_penalty([p])
    assert p["best_score"] == 100
    assert "_gap_penalty_applied" not in p


def test_apply_gap_penalty_resorts_by_score():
    import screener_core as sc
    picks = [
        {"symbol": "A", "best_score": 100, "daily_change": 5.0},  # → 85
        {"symbol": "B", "best_score": 90, "daily_change": 1.0},   # → 90
    ]
    out = sc.apply_gap_penalty(picks)
    assert out[0]["symbol"] == "B"   # B is now higher (90 > 85)
    assert out[1]["symbol"] == "A"


def test_apply_gap_penalty_handles_invalid_picks():
    import screener_core as sc
    picks = [
        None,
        "not a dict",
        {"symbol": "A", "best_score": 100, "daily_change": "bad"},
        {"symbol": "B", "best_score": "bad", "daily_change": 5.0},
    ]
    sc.apply_gap_penalty(picks)
    # No crash; nothing modified.


def test_apply_gap_penalty_threshold_configurable():
    import screener_core as sc
    p = {"symbol": "X", "best_score": 100, "daily_change": 4.0}
    sc.apply_gap_penalty([p], threshold_pct=5.0)
    # 4% < 5% threshold → not demoted
    assert p["best_score"] == 100


def test_apply_gap_penalty_negative_daily_change_unaffected():
    """Pt.58 only penalises UP gaps — gappers down are mean-reversion
    candidates, not chase candidates."""
    import screener_core as sc
    p = {"symbol": "X", "best_score": 100, "daily_change": -5.0}
    sc.apply_gap_penalty([p])
    assert p["best_score"] == 100
    assert "_gap_penalty_applied" not in p


# ============================================================================
# Source pins — wired into the pipeline
# ============================================================================

def test_cloud_scheduler_passes_pick_to_compute_full_size():
    """Long path AND short path both must pass `pick=` to the
    sizing helper so confluence runs on real deploys."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    # Long-side passes pick=pick.
    assert "pick=pick" in src
    # Short-side passes pick=sc (sc is the short candidate dict
    # in the deploy loop).
    assert "pick=sc" in src


def test_update_dashboard_calls_apply_gap_penalty():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "update_dashboard.py").read_text()
    assert "apply_gap_penalty" in src
    assert "from screener_core import apply_gap_penalty" in src
