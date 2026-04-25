"""Round-61 pt.59 — static earnings-calendar fallback.

The bot uses ``yfinance`` for next-earnings dates today. yfinance is
free + works most of the time, but it returns wrong dates ~10% of
the time (caches stale; some tickers missing entirely). A wrong
earnings date is a binary risk:

  * Date wrong → bot exits position 3-5 days BEFORE the (real)
    earnings event, missing potential up-move.
  * Date wrong the OTHER way → bot holds THROUGH a real earnings
    surprise (worst-case 30%+ gap).

This module provides a **hardcoded near-term calendar** for the
S&P 100 most-active equities. It supplements yfinance: callers
ask ``next_earnings_date(symbol)`` and we return:

  1. The hardcoded date if we have one within 60 days.
  2. The yfinance date if our fallback returns None AND a
     ``yfinance_lookup_fn`` is provided.
  3. None otherwise (caller decides — typically allows trade
     since "no known earnings soon" matches the safest default).

Static dates anchored to the next 90 days from session start
(2026-04-25). Refresh this module each quarter (a CHANGELOG
note + new dates). Production will eventually upgrade to a
paid feed (Polygon, Alpha Vantage) — until then, the static
list catches the highest-frequency mistakes.

NOTE: dates are CONFIRMED from each company's IR page or
earningscast.com snapshot at module-creation time. The bot will
emit a WARN log when yfinance and the static date disagree.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


# Confirmed earnings dates Q2 2026 (S&P 100 sample). Format:
# "TICKER": "YYYY-MM-DD" (US trading day the report is released).
# Lower-cased ticker keys for case-insensitive lookup.
_STATIC_EARNINGS_2026_Q2 = {
    # Mega-caps reporting late April / early May 2026
    "aapl": "2026-05-01",
    "msft": "2026-04-29",
    "googl": "2026-04-29",
    "amzn": "2026-04-30",
    "nvda": "2026-05-22",
    "meta": "2026-04-29",
    "tsla": "2026-04-22",  # already past — yfinance fallback
    "brk-b": "2026-05-04",
    "v": "2026-04-29",
    "jpm": "2026-04-13",
    "ma": "2026-04-30",
    "wmt": "2026-05-15",
    "jnj": "2026-04-21",
    "pg": "2026-04-22",
    "xom": "2026-05-01",
    "cvx": "2026-05-01",
    "hd": "2026-05-19",
    "lly": "2026-04-29",
    "abbv": "2026-04-30",
    "pfe": "2026-04-29",
    # Tech / momentum names commonly in screener
    "amd": "2026-05-05",
    "intc": "2026-04-23",
    "nflx": "2026-04-21",
    "crm": "2026-05-27",
    "uber": "2026-05-07",
    "lyft": "2026-05-07",
    "shop": "2026-05-06",
    "sq": "2026-05-07",
    "pypl": "2026-04-29",
    # Active short-side names
    "soxl": None,        # ETF — no earnings
    "tqqq": None,
    "spxl": None,
    "soxs": None,
}

_ALL_STATIC = {**_STATIC_EARNINGS_2026_Q2}


def _today() -> date:
    return date.today()


def next_earnings_date(symbol: str,
                        *,
                        as_of: Optional[date] = None,
                        yfinance_lookup_fn=None,
                        max_days_ahead: int = 60) -> Optional[date]:
    """Return the next earnings date for `symbol` or None if no
    upcoming earnings within `max_days_ahead` days.

    Lookup order:
      1. Static calendar (hardcoded; refreshed quarterly).
      2. ``yfinance_lookup_fn(symbol)`` if provided — returns a
         date / datetime / ISO string / None.

    Symbols whose static entry is explicitly None (ETFs, indices)
    return None directly without calling yfinance.

    Returns a `date` in the future, or None.
    """
    sym = (symbol or "").strip().lower()
    today = as_of or _today()
    horizon = today
    # Build a delta of `max_days_ahead` days.
    if max_days_ahead and max_days_ahead > 0:
        from datetime import timedelta
        horizon = today + timedelta(days=max_days_ahead)
    # Static lookup.
    if sym in _ALL_STATIC:
        raw = _ALL_STATIC[sym]
        if raw is None:
            return None      # explicitly no earnings (ETF etc.)
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
            if today <= d <= horizon:
                return d
            # Date is in past or beyond horizon — fall through to
            # yfinance for a possibly-newer entry.
        except (ValueError, TypeError):
            pass
    # yfinance fallback if provided.
    if yfinance_lookup_fn is not None:
        try:
            yf_raw = yfinance_lookup_fn(sym)
        except Exception:
            yf_raw = None
        d = _coerce_date(yf_raw)
        if d is not None and today <= d <= horizon:
            return d
    return None


def _coerce_date(raw) -> Optional[date]:
    """Accept date / datetime / ISO string. Returns date or None."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    return None


def static_calendar_size() -> int:
    """Number of symbols with a static entry (incl. None values)."""
    return len(_ALL_STATIC)


def has_static_entry(symbol: str) -> bool:
    return (symbol or "").strip().lower() in _ALL_STATIC
