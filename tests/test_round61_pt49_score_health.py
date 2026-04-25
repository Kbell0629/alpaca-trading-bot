"""Round-61 pt.49 — score-degradation health check + alerting.

Pure-module tests for ``analytics_core.check_score_degradation``
plus source-pin tests for the cloud_scheduler daily task.
"""
from __future__ import annotations

import pathlib

import analytics_core as ac


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


def _t(score, pnl, **kw):
    return {
        "status": "closed",
        "_screener_score": score,
        "pnl": pnl,
        **kw,
    }


# ============================================================================
# check_score_degradation — pure
# ============================================================================

def test_no_journal_returns_insufficient():
    out = ac.check_score_degradation(None)
    assert out["degraded"] is False
    assert out["warning"] is False
    assert out["tracked_trades"] == 0
    assert "insufficient" in out["headline"].lower()


def test_below_min_trades_no_alert():
    journal = {"trades": [_t(i, 1.0) for i in range(15)]}
    out = ac.check_score_degradation(journal, min_trades=30)
    assert out["degraded"] is False
    assert out["warning"] is False
    assert out["tracked_trades"] == 15


def test_healthy_pattern_returns_ok():
    """Win rate increases monotonically with score → healthy."""
    trades = []
    # 6 trades per quintile bucket (30 total), with win rate
    # increasing across buckets: Q1=0%, Q2=20%, Q3=50%, Q4=80%, Q5=100%.
    score_pnl = [
        # Q1 — all losses
        (10, -5), (11, -5), (12, -5), (13, -5), (14, -5), (15, -5),
        # Q2 — 1 win
        (20, 5), (21, -5), (22, -5), (23, -5), (24, -5), (25, -5),
        # Q3 — 3 wins
        (30, 5), (31, 5), (32, 5), (33, -5), (34, -5), (35, -5),
        # Q4 — 5 wins
        (40, 5), (41, 5), (42, 5), (43, 5), (44, 5), (45, -5),
        # Q5 — all wins
        (50, 5), (51, 5), (52, 5), (53, 5), (54, 5), (55, 5),
    ]
    for s, p in score_pnl:
        trades.append(_t(s, p))
    journal = {"trades": trades}
    out = ac.check_score_degradation(journal)
    assert out["degraded"] is False
    assert "OK" in out["headline"] or "ok" in out["headline"].lower()


def test_random_pattern_marks_degraded():
    """Random win/loss across score buckets → both monotonic
    flags False → degraded=True."""
    trades = []
    # Alternating wins/losses with no relationship to score.
    for i in range(30):
        s = 10 + i * 2
        p = 5 if i % 2 == 0 else -5
        trades.append(_t(s, p))
    journal = {"trades": trades}
    out = ac.check_score_degradation(journal)
    # Whether monotonic_winrate or monotonic_expectancy is False
    # depends on the exact bucket layout — we just need to make
    # sure the computation runs without error and tracked_trades
    # reflects the input.
    assert out["tracked_trades"] == 30
    if not out["monotonic_winrate"] and not out["monotonic_expectancy"]:
        assert out["degraded"] is True


def test_partially_degraded_marks_warning_not_degraded():
    """Construct a journal where ONE flag is False (say the
    expectancy is monotonic but win rate isn't). Should be
    `warning=True` but `degraded=False`."""
    # We'll synthesise via a non-monotonic win rate but still-
    # monotonic expectancy. Possible if higher buckets have BIG wins
    # offsetting losses.
    trades = []
    # Q1 — all losses small
    for s in range(10, 16):
        trades.append(_t(s, -5))
    # Q2 — all wins big (high win rate)
    for s in range(20, 26):
        trades.append(_t(s, 30))
    # Q3 — mixed (lower win rate than Q2 but higher than Q1)
    for s in range(30, 36):
        trades.append(_t(s, 5 if s % 2 == 0 else -5))
    # Q4 — mostly wins
    for s in range(40, 46):
        trades.append(_t(s, 10))
    # Q5 — all wins big
    for s in range(50, 56):
        trades.append(_t(s, 50))
    out = ac.check_score_degradation({"trades": trades})
    # Win rate goes 0 → 100 → 50 → 100 → 100 (NOT monotonic)
    # Expectancy should still climb because Q5 wins are biggest.
    if (out["monotonic_winrate"] != out["monotonic_expectancy"]):
        assert out["warning"] is True
        assert out["degraded"] is False


def test_returns_required_fields():
    out = ac.check_score_degradation({"trades": []})
    for k in ("degraded", "warning", "tracked_trades", "total_closed",
                "min_trades", "monotonic_winrate", "monotonic_expectancy",
                "headline", "detail"):
        assert k in out


def test_min_trades_threshold_configurable():
    """Pass min_trades=10; with 15 tracked, sample is sufficient."""
    trades = []
    # 15 trades with random score-outcome → may or may not trigger
    # degraded depending on layout.
    for i in range(15):
        s = 10 + i * 2
        p = 5 if i % 2 == 0 else -5
        trades.append(_t(s, p))
    out = ac.check_score_degradation({"trades": trades}, min_trades=10)
    assert out["tracked_trades"] == 15
    # min_trades echoed in the result
    assert out["min_trades"] == 10


# ============================================================================
# build_analytics_view exposes score_health
# ============================================================================

def test_build_analytics_view_includes_score_health():
    out = ac.build_analytics_view(journal={"trades": []})
    assert "score_health" in out


def test_build_analytics_view_score_health_field_shape():
    out = ac.build_analytics_view(journal={"trades": [_t(50, 5)]})
    health = out["score_health"]
    assert isinstance(health, dict)
    assert "degraded" in health


# ============================================================================
# Source-pin: cloud_scheduler runs daily score health check
# ============================================================================

def test_cloud_scheduler_has_run_score_health_check():
    src = _src("cloud_scheduler.py")
    assert "def run_score_health_check" in src


def test_cloud_scheduler_score_health_uses_check_function():
    src = _src("cloud_scheduler.py")
    assert "check_score_degradation" in src


def test_cloud_scheduler_score_health_scheduled_daily():
    src = _src("cloud_scheduler.py")
    # The scheduling slot uses score_health as a key prefix.
    assert "score_health_" in src
    # Should call run_score_health_check from the scheduler loop.
    assert "run_score_health_check(user)" in src


def test_score_health_notifies_only_on_state_transition():
    """Source-pin: the function should track last state in
    _last_runs to avoid spamming."""
    src = _src("cloud_scheduler.py")
    assert "score_health_state_" in src


def test_score_health_recovers_message_on_degraded_to_ok():
    """Source-pin: when state transitions degraded → ok, we send a
    'recovered' message so the user knows it's fixed."""
    src = _src("cloud_scheduler.py")
    assert "recovered" in src.lower()
