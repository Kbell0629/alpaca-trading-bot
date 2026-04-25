"""Round-61 pt.48 — high-impact macro event calendar.

Pure module — no I/O, no external deps. Encodes the dates of US
market-moving macro events so the auto-deployer can either skip
new entries on those days or raise the score threshold.

Events tracked:
  * **FOMC** — Fed rate decisions. Two-day meetings; the second
    day at 2pm ET is when the statement + dot plot drop and the
    market makes its move.
  * **CPI** — Bureau of Labor Statistics monthly inflation print,
    released ~13th of each month at 8:30 AM ET.
  * **NFP / Jobs Report** — first Friday of each month at 8:30 AM
    ET. Includes nonfarm payrolls, unemployment rate, average
    hourly earnings.
  * **PCE** — Personal Consumption Expenditures (the Fed's
    preferred inflation measure), late month at 8:30 AM ET.

Why this matters: a bot trading through FOMC at 2pm with the same
risk model as a quiet Tuesday will buy false breakouts when the
chairman pivots, and stop out on the recovery. Pre-event volatility
is also non-information-bearing — entering 30 minutes before NFP
is buying a coin flip.

Use:
  >>> from event_calendar import is_high_impact_event_day
  >>> hit, reason = is_high_impact_event_day(date(2026, 4, 29))
  >>> print(hit, reason)  # True, "FOMC"

  >>> from event_calendar import next_high_impact_event
  >>> next_high_impact_event(date(2026, 4, 28))
  ('FOMC', date(2026, 4, 29))
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, Tuple


# ============================================================================
# 2026 FOMC meeting schedule — second day of each meeting (rate decision day).
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
# ============================================================================

FOMC_2026 = (
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 16),
)

# 2027 — extend so the bot doesn't go blind at year-end.
FOMC_2027 = (
    date(2027, 1, 27),
    date(2027, 3, 17),
    date(2027, 4, 28),
    date(2027, 6, 16),
    date(2027, 7, 28),
    date(2027, 9, 22),
    date(2027, 10, 27),
    date(2027, 12, 15),
)

FOMC_DATES: frozenset = frozenset(FOMC_2026 + FOMC_2027)


# ============================================================================
# CPI release calendar — BLS publishes ~13th of each month at 8:30 ET.
# Source: bls.gov/schedule/news_release/cpi.htm
# ============================================================================

CPI_2026 = (
    date(2026, 1, 14),  # Dec 2025 data
    date(2026, 2, 11),
    date(2026, 3, 11),
    date(2026, 4, 14),
    date(2026, 5, 12),
    date(2026, 6, 10),
    date(2026, 7, 14),
    date(2026, 8, 12),
    date(2026, 9, 10),
    date(2026, 10, 14),
    date(2026, 11, 12),
    date(2026, 12, 10),
)

CPI_2027 = (
    date(2027, 1, 13),
    date(2027, 2, 10),
    date(2027, 3, 10),
    date(2027, 4, 13),
    date(2027, 5, 12),
    date(2027, 6, 10),
    date(2027, 7, 14),
    date(2027, 8, 11),
    date(2027, 9, 14),
    date(2027, 10, 13),
    date(2027, 11, 10),
    date(2027, 12, 9),
)

CPI_DATES: frozenset = frozenset(CPI_2026 + CPI_2027)


# ============================================================================
# NFP / Jobs Report — first Friday of each month at 8:30 ET. Computed
# rather than hardcoded since the rule is mechanical.
# ============================================================================

def _first_friday_of_month(year: int, month: int) -> date:
    """Return the date of the first Friday in the given month."""
    d = date(year, month, 1)
    # weekday(): Mon=0..Sun=6; Friday=4.
    offset = (4 - d.weekday()) % 7
    return d + timedelta(days=offset)


def _all_first_fridays(start_year: int = 2026, end_year: int = 2027):
    out = set()
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            out.add(_first_friday_of_month(y, m))
    return frozenset(out)


NFP_DATES: frozenset = _all_first_fridays(2026, 2027)


# ============================================================================
# PCE — last Friday of each month (Personal Income & Outlays release).
# Source: bea.gov scheduling.
# ============================================================================

def _last_friday_of_month(year: int, month: int) -> date:
    """Return the date of the last Friday in the given month."""
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    # Walk back to the last Friday.
    last_day = next_month_first - timedelta(days=1)
    offset = (last_day.weekday() - 4) % 7
    return last_day - timedelta(days=offset)


def _all_last_fridays(start_year: int = 2026, end_year: int = 2027):
    out = set()
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            out.add(_last_friday_of_month(y, m))
    return frozenset(out)


PCE_DATES: frozenset = _all_last_fridays(2026, 2027)


# ============================================================================
# Public API
# ============================================================================

# Order matters when multiple events overlap on a single day — the
# higher-impact one wins.
_PRIORITY = ("FOMC", "CPI", "NFP", "PCE")


def _coerce_date(d) -> Optional[date]:
    """Accept date, datetime, or ISO-format string. Returns None on
    invalid input."""
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


def is_high_impact_event_day(d) -> Tuple[bool, Optional[str]]:
    """Return (True, "FOMC"|"CPI"|"NFP"|"PCE") if `d` is a known
    high-impact macro event day, else (False, None).

    Accepts ``date``, ``datetime``, or ISO string. Out-of-range
    dates (before 2026 or after 2027) return (False, None).
    """
    parsed = _coerce_date(d)
    if parsed is None:
        return False, None
    # FOMC > CPI > NFP > PCE on collisions.
    if parsed in FOMC_DATES:
        return True, "FOMC"
    if parsed in CPI_DATES:
        return True, "CPI"
    if parsed in NFP_DATES:
        return True, "NFP"
    if parsed in PCE_DATES:
        return True, "PCE"
    return False, None


def next_high_impact_event(d) -> Optional[Tuple[str, date]]:
    """Return (event_name, event_date) for the next high-impact
    event on or after `d`. Returns None if no event in the next
    365 days (i.e., we've outrun our static calendar).
    """
    parsed = _coerce_date(d)
    if parsed is None:
        return None
    horizon = parsed + timedelta(days=365)
    candidates = []
    for label, dates in (("FOMC", FOMC_DATES), ("CPI", CPI_DATES),
                          ("NFP", NFP_DATES), ("PCE", PCE_DATES)):
        for ed in dates:
            if parsed <= ed <= horizon:
                candidates.append((ed, label))
    if not candidates:
        return None
    candidates.sort()
    ed, label = candidates[0]
    return label, ed


def event_score_multiplier(event_label: Optional[str]) -> float:
    """Risk multiplier applied to score thresholds on event days.
    Higher = bot more reluctant to enter. FOMC 2.0 ⇒ a pick needs
    twice the normal score to deploy on FOMC day. Returns 1.0 (no
    adjustment) for non-event days or unknown labels.
    """
    if not event_label:
        return 1.0
    return {
        "FOMC": 2.0,
        "CPI": 1.5,
        "NFP": 1.5,
        "PCE": 1.3,
    }.get(event_label, 1.0)
