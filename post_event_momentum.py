"""Round-61 pt.66 — post-event momentum lean-in.

Pt.48 added the high-impact event calendar (FOMC/CPI/NFP/PCE) and
pt.48 raised the score threshold ON those days (don't enter into the
binary uncertainty). What it didn't do: capture the EDGE that often
exists in the days IMMEDIATELY AFTER. Once the news is digested the
post-event drift is a documented effect:

  * Post-FOMC: 1-3 day continuation in the dovish/hawkish direction.
  * Post-CPI: 1-2 day continuation in the inflation-surprise direction.
  * Post-NFP: 1 day continuation; mostly fades by Tuesday.
  * Post-PCE: 1 day mild continuation.

This module exposes ``post_event_boost(today, lookback_days=5)``
returning ``(multiplier, label)``. The auto-deployer can use the
multiplier as a >1.0 boost to the score threshold's *inverse* —
i.e. lower the bar slightly so the bot can capture more of the
post-event drift trades.

Pure module — no I/O, no globals. Calendar lookup goes through
``event_calendar`` (already pure).

Use:
    >>> from post_event_momentum import post_event_boost
    >>> from datetime import date
    >>> # Day after the 2026-04-29 FOMC.
    >>> mult, label = post_event_boost(date(2026, 4, 30))
    >>> print(mult, label)  # e.g. 1.15, "post-FOMC day 1"
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, Tuple

import event_calendar as _ec


# Per-event boost schedule. Each entry maps day-after to a
# multiplier > 1.0. Higher = more aggressive post-event lean-in
# (lower effective score threshold).
#
# Empirical defaults — conservative, prefer slightly-larger boost on
# day 1 and trail off by day 3.
POST_EVENT_BOOST: dict = {
    "FOMC": {1: 1.20, 2: 1.10, 3: 1.05},
    "CPI":  {1: 1.15, 2: 1.05},
    "NFP":  {1: 1.10},
    "PCE":  {1: 1.05},
}

# How far back to look. 5 days covers a full trading week including
# weekend gaps after a Friday event.
DEFAULT_LOOKBACK_DAYS: int = 5


def _coerce_date(d) -> Optional[date]:
    """Accept date / datetime / ISO string. None on invalid."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        try:
            return datetime.fromisoformat(d.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            try:
                return date.fromisoformat(d)
            except (ValueError, AttributeError):
                return None
    return None


def _trading_days_between(start: date, end: date) -> int:
    """Count Mon-Fri days strictly BETWEEN start and end (exclusive
    on both sides not exclusive — actually inclusive of `end`,
    exclusive of `start`). Holidays not subtracted: this is a
    pragmatic approximation used to determine "day 1 after event".
    Negative or zero result if `end <= start`.
    """
    if end <= start:
        return 0
    n = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:  # Mon=0..Fri=4
            n += 1
        cur += timedelta(days=1)
    return n


def find_recent_event(today,
                        *,
                        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                        ) -> Optional[Tuple[str, date, int]]:
    """Look back up to ``lookback_days`` calendar days for a
    high-impact event. Return ``(event_label, event_date, day_after)``
    or None.

    ``day_after`` is the count of TRADING days between the event and
    ``today`` (1 = next trading day).
    """
    parsed = _coerce_date(today)
    if parsed is None:
        return None
    for delta in range(1, lookback_days + 1):
        candidate = parsed - timedelta(days=delta)
        hit, label = _ec.is_high_impact_event_day(candidate)
        if hit and label:
            day_after = _trading_days_between(candidate, parsed)
            if day_after <= 0:
                continue
            return label, candidate, day_after
    return None


def post_event_boost(today,
                       *,
                       lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                       schedule: Optional[dict] = None,
                       ) -> Tuple[float, Optional[str]]:
    """Return ``(multiplier, label)`` capturing the post-event drift
    boost for ``today``. ``multiplier`` is 1.0 (no boost) when no
    recent event is in range, else a value > 1.0.

    Args:
      today: date / datetime / ISO string.
      lookback_days: how many calendar days back to scan. Default 5.
      schedule: override ``POST_EVENT_BOOST`` for testing.

    The label format is ``"post-<EVENT> day <N>"`` (e.g.
    ``"post-FOMC day 1"``). Returned alongside the multiplier so
    callers can include it in log lines or dashboard chips.
    """
    sched = schedule if schedule is not None else POST_EVENT_BOOST
    found = find_recent_event(today, lookback_days=lookback_days)
    if not found:
        return 1.0, None
    label, _event_date, day_after = found
    mults = sched.get(label) or {}
    mult = mults.get(day_after)
    if mult is None or mult <= 1.0:
        return 1.0, None
    return float(mult), f"post-{label} day {day_after}"


def adjust_score_threshold(base_threshold: float, today,
                              *,
                              lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                              ) -> Tuple[float, Optional[str]]:
    """Lower the score threshold by the post-event boost.

    A multiplier of 1.20 means the effective threshold is
    ``base / 1.20`` — a 20% lower bar so the bot leans in.

    Returns ``(adjusted_threshold, label_or_None)``. When no event
    is in range, returns ``(base_threshold, None)``.
    """
    mult, label = post_event_boost(today, lookback_days=lookback_days)
    if mult <= 1.0 or label is None:
        return base_threshold, None
    return base_threshold / mult, label
