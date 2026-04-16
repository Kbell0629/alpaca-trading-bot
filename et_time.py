#!/usr/bin/env python3
"""
Shared Eastern Time helper — the ONE canonical timezone for this app.

US stock markets run in ET; the user is in ET; every timestamp this bot
emits (logs, DB rows, JSON files, UI strings) should be ET. Import
`now_et` and `ET_TZ` from this module rather than reaching for
`datetime.now(timezone.utc)` or `datetime.utcnow()`.

zoneinfo handles EDT / EST transitions automatically, so you don't need
to think about daylight savings boundaries.

Stored ISO strings from now_et() include an offset ("-04:00" EDT or
"-05:00" EST) and still compare correctly against any legacy
UTC-offset rows (both are tz-aware).
"""
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — defensive
    ET_TZ = timezone.utc


def now_et():
    """Timezone-aware ET datetime — the canonical 'now' for this app."""
    return datetime.now(ET_TZ)


def et_iso():
    """ET ISO-format string for DB rows / JSON."""
    return now_et().isoformat()


def et_date_str():
    """ET date in YYYY-MM-DD form (for date-keyed state / Alpaca history
    fetch ranges, which are ET for US equities)."""
    return now_et().strftime("%Y-%m-%d")
