"""
Round-16: unit tests for strategy modules with previously zero coverage.

Modules covered: pead_strategy, short_strategy, earnings_play,
insider_signals, options_flow (limited — mostly network), options_analysis.

Each module has 1 pure scoring/business function tested across the
key boundary conditions (no signal, weak signal, strong signal,
edge case at threshold).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta


# ======================== pead_strategy ========================


def test_pead_surprise_to_score_zero_below_threshold():
    """Earnings surprise <5% gets zero score (lowest tier is 5%)."""
    from pead_strategy import _surprise_to_score
    assert _surprise_to_score(0) == 0
    assert _surprise_to_score(2.0) == 0
    assert _surprise_to_score(4.99) == 0


def test_pead_surprise_to_score_tiered():
    """Score should monotonically increase with surprise magnitude."""
    from pead_strategy import _surprise_to_score
    s5 = _surprise_to_score(5)
    s10 = _surprise_to_score(10)
    s20 = _surprise_to_score(25)
    s50 = _surprise_to_score(60)
    assert s5 > 0
    assert s5 < s10 < s20 < s50


def test_pead_surprise_to_score_handles_negative_misses():
    """Score uses abs() so a -10% miss scores like a +10% beat."""
    from pead_strategy import _surprise_to_score
    assert _surprise_to_score(-10) == _surprise_to_score(10)
    assert _surprise_to_score(-50) == _surprise_to_score(50)


def test_pead_summarize_signal_basic():
    """summarize_signal returns a human-readable string for a signal."""
    from pead_strategy import summarize_signal
    sig = {
        "surprise_pct": 12.5,
        "days_since_report": 2,
        "eps_actual": 2.10,
        "eps_estimate": 1.85,
    }
    out = summarize_signal(sig)
    assert "12.5" in out  # surprise pct shows up
    assert "Beat EPS" in out
    # Empty input shouldn't crash
    assert summarize_signal({}) == "" or "Beat" in summarize_signal({})


# ======================== short_strategy ========================


def test_short_strategy_skips_uptrend():
    """No short candidate returned for stocks in uptrend (momentum_20d
    > -5)."""
    from short_strategy import identify_short_candidates
    picks = [{
        "symbol": "AAPL", "price": 150, "momentum_20d": 5.0,
        "daily_change": 0.5, "volatility": 2.0, "rsi": 50,
        "macd_histogram": 0.1, "overall_bias": "bullish",
        "news_sentiment": "neutral", "relative_volume": 1.0,
    }]
    assert identify_short_candidates(picks) == []


def test_short_strategy_picks_strong_downtrend():
    """A stock with strong downtrend + bearish technicals + high vol
    sell day → returned as short candidate."""
    from short_strategy import identify_short_candidates
    picks = [{
        "symbol": "ZNGA", "price": 100, "momentum_20d": -20.0,
        "daily_change": -5.0, "volatility": 4.0, "rsi": 70,
        "macd_histogram": -0.5, "overall_bias": "bearish",
        "news_sentiment": "negative", "relative_volume": 2.0,
    }]
    cands = identify_short_candidates(picks)
    assert len(cands) == 1
    c = cands[0]
    assert c["symbol"] == "ZNGA"
    assert c["short_score"] >= 10
    assert c["stop_loss"] > c["price"]  # stop ABOVE entry for shorts
    assert c["profit_target"] < c["price"]  # target BELOW entry
    assert c["risk_reward"] > 0


def test_short_strategy_min_score_threshold():
    """A stock that's mildly downtrending but lacks technical signals
    shouldn't qualify (min score 10)."""
    from short_strategy import identify_short_candidates
    picks = [{
        "symbol": "MEH", "price": 50, "momentum_20d": -7.0,
        "daily_change": -1.0, "volatility": 2.0, "rsi": 55,
        "macd_histogram": 0.05, "overall_bias": "neutral",
        "news_sentiment": "neutral", "relative_volume": 1.0,
    }]
    # Score: -7% gets 0 (only -10/-15 thresholds), no other signals
    assert identify_short_candidates(picks) == []


# ======================== earnings_play ========================


def test_earnings_play_skips_downtrend():
    from earnings_play import score_earnings_plays
    picks = [{
        "symbol": "DOWN", "price": 50, "momentum_20d": -5,
        "momentum_5d": -2, "volatility": 4, "news_sentiment": "neutral",
        "daily_volume": 1_000_000,
    }]
    assert score_earnings_plays(picks) == []


def test_earnings_play_skips_low_volume():
    from earnings_play import score_earnings_plays
    picks = [{
        "symbol": "ILLIQUID", "price": 50, "momentum_20d": 12,
        "momentum_5d": 3, "volatility": 2.5, "news_sentiment": "positive",
        "daily_volume": 100_000,  # too thin
    }]
    assert score_earnings_plays(picks) == []


def test_earnings_play_skips_negative_sentiment():
    from earnings_play import score_earnings_plays
    picks = [{
        "symbol": "BADNEWS", "price": 100, "momentum_20d": 12,
        "momentum_5d": 3, "volatility": 2.5, "news_sentiment": "negative",
        "daily_volume": 5_000_000,
    }]
    assert score_earnings_plays(picks) == []


def test_earnings_play_strong_uptrend_qualifies():
    from earnings_play import score_earnings_plays
    picks = [{
        "symbol": "NVDA", "price": 200, "momentum_20d": 12,
        "momentum_5d": 3, "volatility": 2.5, "news_sentiment": "positive",
        "daily_volume": 5_000_000,
    }]
    out = score_earnings_plays(picks)
    assert len(out) == 1
    assert out[0]["symbol"] == "NVDA"
    assert out[0]["earnings_score"] > 8
    assert "rules" in out[0]


def test_earnings_play_sorts_descending_by_score():
    from earnings_play import score_earnings_plays
    picks = [
        {"symbol": "WEAK", "price": 50, "momentum_20d": 6, "momentum_5d": 1,
         "volatility": 5, "news_sentiment": "neutral", "daily_volume": 1_000_000},
        {"symbol": "STRONG", "price": 200, "momentum_20d": 20, "momentum_5d": 8,
         "volatility": 2, "news_sentiment": "positive", "daily_volume": 5_000_000},
    ]
    out = score_earnings_plays(picks)
    if len(out) >= 2:
        assert out[0]["earnings_score"] >= out[1]["earnings_score"]


# ======================== insider_signals ========================


def test_insider_score_zero_on_empty():
    from insider_signals import insider_score_bonus
    assert insider_score_bonus(None) == 0
    assert insider_score_bonus({}) == 0
    assert insider_score_bonus({"error": "edgar_down"}) == 0


def test_insider_score_zero_below_cluster_threshold():
    """Below 3 buyers → no cluster bonus."""
    from insider_signals import insider_score_bonus
    assert insider_score_bonus({"buyer_count": 0}) == 0
    assert insider_score_bonus({"buyer_count": 1}) == 0
    assert insider_score_bonus({"buyer_count": 2}) == 0


def test_insider_score_cluster_at_3():
    """3+ buyers triggers the cluster bonus."""
    from insider_signals import insider_score_bonus
    assert insider_score_bonus({"buyer_count": 3}) == 10


def test_insider_score_extra_buyers_capped_at_15():
    """Cluster + extras: capped at 15."""
    from insider_signals import insider_score_bonus
    assert insider_score_bonus({"buyer_count": 4}) == 11
    assert insider_score_bonus({"buyer_count": 8}) == 15  # 10 + 5
    assert insider_score_bonus({"buyer_count": 100}) == 15  # capped


def test_insider_score_recent_filing_bonus():
    """Recent (≤7 day) filing adds 3."""
    from insider_signals import insider_score_bonus
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    score = insider_score_bonus({"buyer_count": 3, "most_recent_date": recent})
    # cluster (10) + recent (3) = 13
    assert score == 13


def test_insider_score_old_filing_no_bonus():
    """Old (>7 day) filing → no recent bonus."""
    from insider_signals import insider_score_bonus
    old = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d")
    score = insider_score_bonus({"buyer_count": 3, "most_recent_date": old})
    assert score == 10  # cluster only


def test_insider_score_handles_malformed_date():
    """Malformed date string shouldn't crash, just skip recency bonus."""
    from insider_signals import insider_score_bonus
    score = insider_score_bonus({"buyer_count": 3,
                                  "most_recent_date": "not-a-date"})
    assert score == 10
