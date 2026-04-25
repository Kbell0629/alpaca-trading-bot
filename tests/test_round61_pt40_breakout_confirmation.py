"""Round-61 pt.40 — multi-day breakout confirmation.

Single-day breakouts (one close above 20-day high) have a well-known
"Tuesday-fake-Wednesday-collapse" failure mode. Academic momentum
research shows two-bar confirmation lifts win rate ~10-15 points at
the cost of ~20% fewer entries — net positive.

Pt.40 adds `apply_breakout_confirmation` in screener_core.py: per
Breakout-strategy pick, require both today's close AND yesterday's
close to be above their respective 20-day highs. Unconfirmed picks
are demoted (score halved + tagged), not eliminated, so they can
still rank if nothing else qualifies.
"""
from __future__ import annotations

from screener_core import (
    apply_breakout_confirmation,
    _max_high_window,
    _BREAKOUT_LOOKBACK_DAYS,
)


# ============================================================================
# _max_high_window
# ============================================================================

def _bar(c, h=None):
    """Build an Alpaca-style bar dict. ``h`` defaults to ``c`` when
    not given (typical for synthesized test bars)."""
    return {"c": c, "h": h if h is not None else c}


def test_max_high_window_basic():
    """20 bars, max high should equal the largest h in the window."""
    bars = [_bar(c=100, h=h) for h in range(1, 21)]
    assert _max_high_window(bars, len(bars), 20) == 20.0


def test_max_high_window_excludes_end_idx():
    """Slice end is exclusive — passing end_idx=len(bars)-1 should
    exclude the LAST bar (used to compute the breakout LEVEL before
    today)."""
    bars = [_bar(c=100, h=h) for h in [10, 20, 30, 40, 50]]
    # Window of last 4 bars BEFORE the final bar = highs [10,20,30,40]
    assert _max_high_window(bars, 4, 4) == 40.0


def test_max_high_window_returns_none_on_short_history():
    bars = [_bar(c=100, h=100) for _ in range(5)]
    assert _max_high_window(bars, 5, 20) is None


def test_max_high_window_handles_string_highs():
    bars = [_bar(c="100", h="100") for _ in range(20)]
    assert _max_high_window(bars, 20, 20) == 100.0


def test_max_high_window_returns_none_on_corrupt():
    bars = [_bar(c="garbage", h="garbage") for _ in range(20)]
    assert _max_high_window(bars, 20, 20) is None


def test_max_high_window_handles_none_input():
    assert _max_high_window(None, 20, 20) is None
    assert _max_high_window([], 20, 20) is None


# ============================================================================
# apply_breakout_confirmation — confirmed case
# ============================================================================

def _two_day_breakout_bars():
    """20 days at 100 + day at 105 (clears prior 20-day-high) +
    day at 108 (clears today's 20-day-high which now includes the
    105 day)."""
    bars = [_bar(c=100, h=100) for _ in range(20)]   # bars[0..19]
    bars.append(_bar(c=105, h=106))                    # bars[20] — yesterday
    bars.append(_bar(c=108, h=109))                    # bars[21] — today
    return bars


def test_confirmed_breakout_keeps_score():
    """Both yesterday and today above their respective 20-day highs."""
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 50,
               "breakout_score": 50}]
    bars_map = {"X": _two_day_breakout_bars()}
    apply_breakout_confirmation(picks, bars_map, lookback=20)
    p = picks[0]
    assert p["breakout_confirmed"] is True
    assert p["breakout_today_above"] is True
    assert p["breakout_yesterday_above"] is True
    assert p["best_score"] == 50  # unchanged
    assert "_breakout_unconfirmed" not in p


def test_unconfirmed_breakout_demoted_score_halved():
    """Today closes above 20-day high but yesterday did NOT — single-
    day breakout. Demote (halve score), tag, but keep in list."""
    bars = [_bar(c=100, h=100) for _ in range(20)]   # baseline
    bars.append(_bar(c=99, h=100))                    # yesterday — below high
    bars.append(_bar(c=110, h=111))                   # today — above high
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 80,
               "breakout_score": 80}]
    apply_breakout_confirmation(picks, {"X": bars}, lookback=20)
    p = picks[0]
    assert p["breakout_confirmed"] is False
    assert p["breakout_today_above"] is True
    assert p["breakout_yesterday_above"] is False
    assert p["_breakout_unconfirmed"] is True
    assert p["best_score"] == 40.0  # halved
    assert p["breakout_score"] == 40.0  # halved


def test_neither_day_above_demoted():
    """Both yesterday AND today below 20-day high — fully unconfirmed."""
    bars = [_bar(c=100, h=120) for _ in range(20)]   # 20-day high = 120
    bars.append(_bar(c=110, h=115))                   # yesterday — below
    bars.append(_bar(c=115, h=119))                   # today — below
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 50}]
    apply_breakout_confirmation(picks, {"X": bars}, lookback=20)
    assert picks[0]["_breakout_unconfirmed"] is True
    assert picks[0]["breakout_today_above"] is False
    assert picks[0]["breakout_yesterday_above"] is False
    assert picks[0]["best_score"] == 25.0


def test_confirmed_yesterday_only_still_demoted():
    """Yesterday above its high but today NOT — current setup is no
    longer a breakout. Demote."""
    bars = [_bar(c=100, h=100) for _ in range(20)]
    bars.append(_bar(c=110, h=111))   # yesterday above 100 high
    bars.append(_bar(c=105, h=108))   # today — NOT above today's high (which now includes the 111)
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 60}]
    apply_breakout_confirmation(picks, {"X": bars}, lookback=20)
    assert picks[0]["_breakout_unconfirmed"] is True


# ============================================================================
# Strategy gating
# ============================================================================

def test_non_breakout_strategy_passes_through():
    """Mean reversion / wheel / etc. are not gated by breakout
    confirmation — they have different signal patterns."""
    bars = [_bar(c=100, h=100) for _ in range(22)]
    picks = [
        {"symbol": "A", "best_strategy": "Mean Reversion", "best_score": 50},
        {"symbol": "B", "best_strategy": "Wheel Strategy", "best_score": 50},
        {"symbol": "C", "best_strategy": "PEAD", "best_score": 50},
        {"symbol": "D", "best_strategy": "short_sell", "best_score": 50},
    ]
    bars_map = {p["symbol"]: bars for p in picks}
    apply_breakout_confirmation(picks, bars_map, lookback=20)
    for p in picks:
        assert "_breakout_unconfirmed" not in p
        assert p["best_score"] == 50


def test_strategy_name_case_insensitive():
    """Strategy name matching is case-insensitive — "Breakout",
    "breakout", "BREAKOUT" all gated."""
    bars = [_bar(c=100, h=100) for _ in range(20)]
    bars += [_bar(c=99, h=100), _bar(c=110, h=111)]
    for name in ("Breakout", "breakout", "BREAKOUT"):
        picks = [{"symbol": "X", "best_strategy": name, "best_score": 50}]
        apply_breakout_confirmation(picks, {"X": bars}, lookback=20)
        assert picks[0]["_breakout_unconfirmed"] is True, (
            f"strategy name {name!r} should be gated")


# ============================================================================
# Fail-open behaviours
# ============================================================================

def test_no_bars_passes_through():
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 50}]
    apply_breakout_confirmation(picks, {}, lookback=20)
    assert "_breakout_unconfirmed" not in picks[0]
    assert picks[0]["best_score"] == 50


def test_too_few_bars_passes_through():
    """Need lookback+2 bars (20 + today + yesterday). Anything less:
    fail open."""
    bars = [_bar(c=100, h=100) for _ in range(21)]  # only 21
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 50}]
    apply_breakout_confirmation(picks, {"X": bars}, lookback=20)
    assert "_breakout_unconfirmed" not in picks[0]


def test_corrupt_bar_passes_through():
    """Today's close is None → can't confirm → fail open."""
    bars = [_bar(c=100, h=100) for _ in range(20)]
    bars.append(_bar(c=110, h=111))
    bars.append({"c": None, "h": None})
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 50}]
    apply_breakout_confirmation(picks, {"X": bars}, lookback=20)
    assert "_breakout_unconfirmed" not in picks[0]


def test_zero_close_passes_through():
    bars = [_bar(c=100, h=100) for _ in range(20)]
    bars.append(_bar(c=0, h=0))
    bars.append(_bar(c=110, h=111))
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 50}]
    apply_breakout_confirmation(picks, {"X": bars}, lookback=20)
    assert "_breakout_unconfirmed" not in picks[0]


def test_no_picks_does_not_crash():
    assert apply_breakout_confirmation([], {}, lookback=20) == []
    assert apply_breakout_confirmation(None, {}, lookback=20) is None


# ============================================================================
# Sort order + multi-pick
# ============================================================================

def test_demoted_picks_re_sorted_to_bottom():
    """After confirmation, surviving picks should be highest-score-first
    with demoted picks below them."""
    confirmed_bars = _two_day_breakout_bars()
    unconfirmed = [_bar(c=100, h=100) for _ in range(20)]
    unconfirmed += [_bar(c=99, h=100), _bar(c=110, h=111)]

    picks = [
        {"symbol": "DEMOTE", "best_strategy": "Breakout", "best_score": 100},
        {"symbol": "KEEP", "best_strategy": "Breakout", "best_score": 60},
    ]
    apply_breakout_confirmation(picks, {
        "DEMOTE": unconfirmed,    # 100 → 50
        "KEEP": confirmed_bars,    # 60 stays
    }, lookback=20)
    # KEEP (60) > DEMOTE (50)
    assert picks[0]["symbol"] == "KEEP"
    assert picks[1]["symbol"] == "DEMOTE"
    assert picks[1]["_breakout_unconfirmed"] is True


# ============================================================================
# Constants + source-pin
# ============================================================================

def test_breakout_lookback_default_is_20():
    """Pin the canonical 20-day window — Donchian/Turtle standard."""
    assert _BREAKOUT_LOOKBACK_DAYS == 20


def test_breakout_score_field_also_halved():
    """The legacy `breakout_score` field (set by score_stocks) should
    be halved alongside `best_score`. Some downstream code reads
    breakout_score directly for display."""
    bars = [_bar(c=100, h=100) for _ in range(20)]
    bars += [_bar(c=99, h=100), _bar(c=110, h=111)]
    picks = [{"symbol": "X", "best_strategy": "Breakout", "best_score": 50,
               "breakout_score": 50}]
    apply_breakout_confirmation(picks, {"X": bars}, lookback=20)
    assert picks[0]["best_score"] == 25
    assert picks[0]["breakout_score"] == 25


# ============================================================================
# Wired into update_dashboard
# ============================================================================

def test_update_dashboard_calls_apply_breakout_confirmation():
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    assert "apply_breakout_confirmation" in src
    assert "from screener_core import apply_breakout_confirmation" in src


def test_breakout_confirmation_runs_after_factor_scores():
    """Order matters: confirmation runs AFTER apply_factor_scores so
    `best_strategy` is finalized before gating."""
    import pathlib
    src = pathlib.Path("update_dashboard.py").read_text()
    factor_idx = src.find("apply_factor_scores(top_candidates")
    bc_idx = src.find("apply_breakout_confirmation(top_candidates")
    assert factor_idx > 0
    assert bc_idx > 0
    assert bc_idx > factor_idx
