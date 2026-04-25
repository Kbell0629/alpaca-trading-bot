"""Round-61 pt.50 — weekend-aware scorecard freshness audit.

The pt.21 staleness check fired any time `last_updated` was >48h
old, false-positive on every Monday morning because Friday's
daily_close → Monday's audit naturally crosses 64+ hours.

Pt.50 measures staleness in TRADING DAYS, not wall-clock hours.
A scorecard is stale only when ≥2 expected daily_close runs were
missed.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import audit_core


# ============================================================================
# _is_trading_day
# ============================================================================

def test_is_trading_day_weekend_false():
    from datetime import date
    # 2026-04-25 is a Saturday; 2026-04-26 is Sunday.
    assert audit_core._is_trading_day(date(2026, 4, 25)) is False
    assert audit_core._is_trading_day(date(2026, 4, 26)) is False


def test_is_trading_day_weekday_true():
    from datetime import date
    # 2026-04-22 is a Wednesday.
    assert audit_core._is_trading_day(date(2026, 4, 22)) is True


def test_is_trading_day_holiday_false():
    from datetime import date
    # 2026-01-01 — New Year's Day.
    assert audit_core._is_trading_day(date(2026, 1, 1)) is False
    # 2026-07-03 — Independence Day observed (3rd is Friday).
    assert audit_core._is_trading_day(date(2026, 7, 3)) is False


def test_is_trading_day_accepts_datetime():
    from datetime import datetime, date
    dt = datetime(2026, 4, 22, 14, 30)
    assert audit_core._is_trading_day(dt) is True


def test_is_trading_day_invalid_input_returns_true():
    """Don't suppress audits on weird input; default to 'is trading'."""
    assert audit_core._is_trading_day("nonsense") is True
    assert audit_core._is_trading_day(42) is True


# ============================================================================
# _trading_closes_between
# ============================================================================

def test_no_closes_when_start_equals_end():
    t = datetime(2026, 4, 22, 16, 5)
    assert audit_core._trading_closes_between(t, t) == 0


def test_no_closes_when_start_after_end():
    a = datetime(2026, 4, 22, 16, 5)
    b = datetime(2026, 4, 21, 16, 5)
    assert audit_core._trading_closes_between(a, b) == 0


def test_one_close_within_a_single_trading_day():
    # Mon 8 AM to Mon 5 PM → one daily_close (4:05 PM) elapsed.
    start = datetime(2026, 4, 27, 8, 0)
    end = datetime(2026, 4, 27, 17, 0)
    assert audit_core._trading_closes_between(start, end) == 1


def test_zero_closes_during_morning_only():
    # Mon 8 AM to Mon 3 PM → 0 closes (close fires at 4:05 PM).
    start = datetime(2026, 4, 27, 8, 0)
    end = datetime(2026, 4, 27, 15, 0)
    assert audit_core._trading_closes_between(start, end) == 0


def test_weekend_gap_yields_zero_closes_friday_close_to_monday_morning():
    """The exact false-positive from the audit screenshot. Friday
    2026-04-24 16:05 → Monday 2026-04-27 09:00 = 65h elapsed but 0
    missed closes because Friday's close already fired and Monday's
    hasn't yet."""
    start = datetime(2026, 4, 24, 16, 5)
    end = datetime(2026, 4, 27, 9, 0)
    # The Mon 16:05 close hasn't fired yet → 0 closes elapsed since
    # start (which equals Fri's close).
    assert audit_core._trading_closes_between(start, end) == 0


def test_weekend_gap_one_missed_close_on_monday_afternoon():
    # Friday 4:05 PM → Monday 5 PM = Mon's close already fired.
    start = datetime(2026, 4, 24, 16, 5)
    end = datetime(2026, 4, 27, 17, 0)
    assert audit_core._trading_closes_between(start, end) == 1


def test_three_day_holiday_weekend_zero_missed():
    """Memorial Day 2026 is May 25 (Mon, holiday). Fri 2026-05-22
    4:05 PM → Tue 2026-05-26 9 AM should be 0 closes (Mon was a
    market holiday + Tue close hasn't fired)."""
    start = datetime(2026, 5, 22, 16, 5)
    end = datetime(2026, 5, 26, 9, 0)
    assert audit_core._trading_closes_between(start, end) == 0


def test_skips_weekends_and_holidays_in_count():
    """Wednesday 4 PM → next Wednesday 5 PM. That window covers
    Wed close (already past start), Thu/Fri/next-Mon/Tue/Wed
    closes = 5 trading-day closes."""
    start = datetime(2026, 4, 22, 16, 5)   # Wed
    end = datetime(2026, 4, 29, 17, 0)     # next Wed 5 PM
    # Thu Apr 23, Fri Apr 24, Mon Apr 27, Tue Apr 28, Wed Apr 29 → 5
    assert audit_core._trading_closes_between(start, end) == 5


def test_handles_naive_datetimes():
    """Both naive — must still compute correctly."""
    start = datetime(2026, 4, 22, 8, 0)
    end = datetime(2026, 4, 22, 17, 0)
    assert audit_core._trading_closes_between(start, end) == 1


def test_handles_tz_aware_datetimes():
    """Both tz-aware — also fine."""
    start = datetime(2026, 4, 22, 8, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 22, 21, 0, tzinfo=timezone.utc)
    # 21:00 UTC = 17:00 ET → past 16:05 close → 1 close.
    assert audit_core._trading_closes_between(start, end) == 1


def test_returns_zero_for_invalid_inputs():
    assert audit_core._trading_closes_between(None, None) == 0
    assert audit_core._trading_closes_between("a", "b") == 0


# ============================================================================
# Full audit — weekend false-positive prevention
# ============================================================================

def _audit_with_last_updated(last_updated_dt):
    return audit_core.run_audit(
        positions=[],
        orders=[],
        strategy_files={},
        journal={"trades": []},
        scorecard={"last_updated": last_updated_dt.isoformat()},
    )


def test_audit_silent_after_weekend_gap_only():
    """Friday close fired; audit runs Monday morning. Should NOT
    flag stale_scorecard."""
    # Friday 2026-04-24 was a Friday; close at 16:05.
    fri_close = datetime(2026, 4, 24, 16, 5)
    # Monkey-patch et_time.now_et to return Mon 9 AM.
    import et_time
    orig = et_time.now_et
    try:
        et_time.now_et = lambda: datetime(2026, 4, 27, 9, 0)
        report = _audit_with_last_updated(fri_close)
        stale = [f for f in report["findings"]
                  if f["category"] == "stale_scorecard"]
        assert stale == []
    finally:
        et_time.now_et = orig


def test_audit_fires_when_two_closes_missed():
    """Last update Wednesday 4 PM; now is Friday 5 PM → Thu and Fri
    closes both missed. Should flag stale_scorecard."""
    wed_close = datetime(2026, 4, 22, 16, 5)
    import et_time
    orig = et_time.now_et
    try:
        et_time.now_et = lambda: datetime(2026, 4, 24, 17, 0)
        report = _audit_with_last_updated(wed_close)
        stale = [f for f in report["findings"]
                  if f["category"] == "stale_scorecard"]
        assert len(stale) == 1
        assert "missed" in stale[0]["message"]
    finally:
        et_time.now_et = orig


def test_audit_silent_for_three_day_holiday_weekend():
    """Friday before Memorial Day → Tuesday morning after. Mon was
    a holiday so only Tue's close hasn't fired → 0 missed."""
    fri_close = datetime(2026, 5, 22, 16, 5)
    import et_time
    orig = et_time.now_et
    try:
        et_time.now_et = lambda: datetime(2026, 5, 26, 9, 0)
        report = _audit_with_last_updated(fri_close)
        stale = [f for f in report["findings"]
                  if f["category"] == "stale_scorecard"]
        assert stale == []
    finally:
        et_time.now_et = orig


def test_audit_message_includes_missed_closes_count():
    """The new message format mentions the missed-closes count
    rather than a flat hours value."""
    old = datetime(2026, 4, 13, 16, 5)
    import et_time
    orig = et_time.now_et
    try:
        et_time.now_et = lambda: datetime(2026, 4, 24, 17, 0)
        report = _audit_with_last_updated(old)
        stale = [f for f in report["findings"]
                  if f["category"] == "stale_scorecard"]
        assert len(stale) == 1
        assert "expected daily_close runs missed" in stale[0]["message"]
        # Hours value still present for context.
        assert "h ago" in stale[0]["message"]
    finally:
        et_time.now_et = orig
