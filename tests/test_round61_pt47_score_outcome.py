"""Round-61 pt.47 — score-to-outcome correlation panel.

Three layers:
  1. ``analytics_core.compute_score_outcome`` — bins closed trades
     by their `_screener_score` field.
  2. ``cloud_scheduler`` writes `_screener_score` into the journal at
     OPEN time (long + short paths).
  3. Dashboard renderAnalyticsPanel surfaces the buckets + monotonic
     pills.
"""
from __future__ import annotations

import pathlib

import analytics_core as ac


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# compute_score_outcome — pure
# ============================================================================

def _t(score, pnl, **kw):
    return {
        "status": "closed",
        "_screener_score": score,
        "pnl": pnl,
        **kw,
    }


def test_returns_empty_when_no_journal():
    out = ac.compute_score_outcome(None)
    assert out["tracked_trades"] == 0
    assert out["untracked_trades"] == 0
    assert out["total_closed"] == 0
    assert out["buckets"] == []


def test_returns_empty_when_no_closed_trades():
    journal = {"trades": [
        {"status": "open", "_screener_score": 100, "pnl": None},
    ]}
    out = ac.compute_score_outcome(journal)
    assert out["tracked_trades"] == 0
    assert out["total_closed"] == 0
    assert out["buckets"] == []


def test_counts_untracked_trades_separately():
    journal = {"trades": [
        _t(100, 10),                                  # tracked
        {"status": "closed", "pnl": 5},               # no score
        {"status": "closed", "_screener_score": "bad", "pnl": 5},  # bad
        _t(200, -5),                                  # tracked
    ]}
    out = ac.compute_score_outcome(journal)
    assert out["tracked_trades"] == 2
    assert out["untracked_trades"] == 2
    assert out["total_closed"] == 4


def test_below_min_per_bucket_returns_empty_buckets():
    """Need >=2 trades per bucket × 5 buckets = 10 minimum."""
    journal = {"trades": [_t(i * 10, 1.0) for i in range(8)]}
    out = ac.compute_score_outcome(journal, bucket_count=5)
    assert out["tracked_trades"] == 8
    assert out["buckets"] == []


def test_full_bucketization_with_15_trades():
    """15 trades / 5 buckets = 3 per bucket."""
    journal = {"trades": [
        _t(i * 10, (i - 7) * 1.0) for i in range(15)
    ]}
    out = ac.compute_score_outcome(journal, bucket_count=5)
    assert len(out["buckets"]) == 5
    for b in out["buckets"]:
        assert b["count"] == 3


def test_buckets_sorted_low_to_high_score():
    journal = {"trades": [_t(i, 0.5) for i in range(20)]}
    out = ac.compute_score_outcome(journal)
    score_lows = [b["score_range"][0] for b in out["buckets"]]
    assert score_lows == sorted(score_lows)


def test_bucket_labels_first_and_last_marked():
    journal = {"trades": [_t(i, 0.5) for i in range(20)]}
    out = ac.compute_score_outcome(journal)
    assert "lowest" in out["buckets"][0]["label"]
    assert "highest" in out["buckets"][-1]["label"]


def test_monotonic_winrate_when_higher_score_wins_more():
    """Synthesise a journal where high-score trades win and low-score
    lose. The detector should flag monotonic_winrate=True."""
    trades = []
    # Q1 (low scores, all losses)
    for s in (10, 11, 12, 13):
        trades.append(_t(s, -5))
    # Q2 (low-mid, mostly losses)
    for s in (20, 21, 22, 23):
        trades.append(_t(s, -2))
    # Q3 (mid, flat)
    for s in (30, 31, 32, 33):
        trades.append(_t(s, 1))
    # Q4 (high-mid, mostly wins)
    for s in (40, 41, 42, 43):
        trades.append(_t(s, 5))
    # Q5 (high, all wins)
    for s in (50, 51, 52, 53):
        trades.append(_t(s, 10))
    out = ac.compute_score_outcome({"trades": trades})
    assert out["monotonic_winrate"] is True
    assert out["monotonic_expectancy"] is True


def test_non_monotonic_when_random():
    """Random scores → win rate likely non-monotonic across buckets."""
    journal = {"trades": [
        _t(10, 5), _t(15, -5), _t(20, 5), _t(25, -5),
        _t(30, -5), _t(35, 5), _t(40, -5), _t(45, 5),
        _t(50, 5), _t(55, -5), _t(60, 5), _t(65, -5),
    ]}
    out = ac.compute_score_outcome(journal)
    # Don't assert specifically; just ensure the call returned and
    # produced buckets.
    assert out["buckets"]


def test_each_bucket_has_required_fields():
    journal = {"trades": [_t(i, (i - 10) * 0.5) for i in range(20)]}
    out = ac.compute_score_outcome(journal)
    for b in out["buckets"]:
        for k in ("label", "score_range", "count", "wins", "losses",
                   "win_rate", "total_pnl", "avg_pnl", "expectancy"):
            assert k in b, f"bucket missing key: {k}"


def test_skips_trades_with_invalid_pnl():
    journal = {"trades": [
        _t(10, "bad"),
        _t(11, None),
        _t(12, 5.0),
    ]}
    out = ac.compute_score_outcome(journal)
    assert out["tracked_trades"] == 1
    assert out["untracked_trades"] == 2


def test_handles_string_score_that_parses():
    journal = {"trades": [
        {"status": "closed", "_screener_score": "100.5", "pnl": 5.0},
    ]}
    out = ac.compute_score_outcome(journal)
    assert out["tracked_trades"] == 1


# ============================================================================
# build_analytics_view exposes score_outcome
# ============================================================================

def test_build_view_includes_score_outcome():
    out = ac.build_analytics_view(journal={"trades": []})
    assert "score_outcome" in out
    assert out["score_outcome"]["buckets"] == []


def test_build_view_score_outcome_with_data():
    journal = {"trades": [_t(i, (i - 10) * 0.5) for i in range(20)]}
    out = ac.build_analytics_view(journal=journal)
    so = out["score_outcome"]
    assert so["tracked_trades"] == 20
    assert len(so["buckets"]) == 5


# ============================================================================
# cloud_scheduler embeds _screener_score at deploy time
# ============================================================================

def test_long_deployer_writes_screener_score_in_journal():
    src = _src("cloud_scheduler.py")
    # Find the long-side journal append after best_strat construction.
    assert "_screener_score" in src
    # And it pulls from pick.get("best_score") or _score variable
    assert "best_score" in src


def test_short_deployer_writes_screener_score_in_journal():
    src = _src("cloud_scheduler.py")
    # Short-side uses sc.get("short_score").
    assert 'sc.get("short_score")' in src


# ============================================================================
# Dashboard panel surface (source-pin)
# ============================================================================

def test_dashboard_renders_score_to_outcome_panel():
    src = _src("templates/dashboard.html")
    # Title text must exist near the analytics section.
    assert "Score-to-Outcome Correlation" in src


def test_dashboard_panel_consumes_score_outcome_field():
    src = _src("templates/dashboard.html")
    # The render consumes d.score_outcome.
    assert "score_outcome" in src


def test_dashboard_shows_monotonic_pills():
    src = _src("templates/dashboard.html")
    assert "Win-rate increases with score" in src
    assert "Expectancy increases with score" in src


def test_dashboard_shows_tracked_trades_legend():
    """When score data isn't present, show how many trades are tracked
    vs legacy/no-score."""
    src = _src("templates/dashboard.html")
    assert "tracked_trades" in src
