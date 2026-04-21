"""
Universal earnings-exit rule (round-29).

Before round-29, only the PEAD strategy exited positions before earnings.
Breakout / trailing-stop / mean-reversion positions sat through earnings
and regularly got whipsawed by earnings-surprise moves.

This module adds:
  * A cached yfinance lookup for a symbol's next earnings date
  * A `should_exit_for_earnings()` decision helper used by the scheduler
  * A strategy allow-list — deliberately excludes `wheel` and `pead`

Design choices:
  * **Default 1 day before earnings.** Gives a slippage / after-hours
    buffer without burning too much of the pre-earnings momentum that
    the strategy was deployed to capture.
  * **Full close** (not partial). Partial leaves tail risk into a
    binary event; the whole point of an earnings exit is to sidestep
    it entirely.
  * **Applies to: trailing_stop, breakout, mean_reversion,
    copy_trading.** These are momentum / trend plays where earnings
    is a random-walk shock.
  * **Skips: wheel.** Short puts through earnings capture IV crush,
    which is the wheel's profit engine. Closing early leaves money
    on the table.
  * **Skips: pead.** PEAD already has its own `exit_before_next_
    earnings_days` rule (in `process_strategy_file`) to avoid riding
    into the NEXT earnings event.
  * **4-hour cache** on the earnings-date lookup. The monitor loop
    runs every 30 min per symbol; without the cache we'd hit
    yfinance 48x/day per symbol for data that changes maybe once a
    quarter.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from threading import Lock
from typing import Optional

from et_time import now_et

# Strategies that this rule applies to. Wheel + PEAD excluded (see module docstring).
EXIT_ON_EARNINGS_STRATEGIES = frozenset({
    "trailing_stop",
    "breakout",
    "mean_reversion",
    "copy_trading",
})

# Cache: {symbol: (fetched_at_datetime, next_earnings_date_or_None)}
_CACHE: dict[str, tuple[datetime, Optional[date]]] = {}
_CACHE_TTL = timedelta(hours=4)
_CACHE_LOCK = Lock()


_CACHE_MISS = object()


def _cached_lookup(symbol: str):
    """Return the cached value if fetched within TTL, else _CACHE_MISS.

    We can't use Optional[date] as the return here because the cache
    legitimately stores None (symbols with no earnings coverage) and we
    don't want to re-fetch those on every tick."""
    with _CACHE_LOCK:
        entry = _CACHE.get(symbol)
    if entry is None:
        return _CACHE_MISS
    fetched_at, next_dt = entry
    if (now_et().replace(tzinfo=None) - fetched_at) > _CACHE_TTL:
        return _CACHE_MISS
    return next_dt


def _cache_store(symbol: str, next_dt: Optional[date]) -> None:
    with _CACHE_LOCK:
        _CACHE[symbol] = (now_et().replace(tzinfo=None), next_dt)


def _fetch_next_earnings_from_yfinance(symbol: str) -> Optional[date]:
    """One-shot yfinance call. Returns the next FUTURE earnings date
    (unreported) or None on any error / missing data."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        ticker = yf.Ticker(symbol)
        ed = ticker.get_earnings_dates(limit=8)
    except (ValueError, TypeError, AttributeError, KeyError):
        # Shape drift — don't retry
        return None
    except Exception:
        # Transient network error
        return None
    if ed is None or (hasattr(ed, "empty") and ed.empty):
        return None
    today = now_et().date()
    next_dt: Optional[date] = None
    for ix, row in ed.iterrows():
        actual_v = row.get("Reported EPS")
        if actual_v is None:
            actual_v = row.get("EPS Actual")
        # A row with an actual EPS is already reported — skip it.
        try:
            is_reported = actual_v is not None and not _is_nan(actual_v)
        except Exception:
            is_reported = False
        if is_reported:
            continue
        try:
            row_dt = ix.date() if hasattr(ix, "date") else ix
        except Exception:
            continue
        if row_dt > today and (next_dt is None or row_dt < next_dt):
            next_dt = row_dt
    return next_dt


def _is_nan(v) -> bool:
    try:
        return v != v  # NaN != NaN
    except Exception:
        return False


def get_next_earnings_date(symbol: str) -> Optional[date]:
    """Cached next-earnings-date lookup. Returns a `date` or None."""
    if not symbol:
        return None
    sym = symbol.upper()
    cached = _cached_lookup(sym)
    if cached is not _CACHE_MISS:
        return cached
    next_dt = _fetch_next_earnings_from_yfinance(sym)
    _cache_store(sym, next_dt)
    return next_dt


def should_exit_for_earnings(
    symbol: str,
    strategy_type: str,
    days_before: int = 1,
    asset_class: str = "us_equity",
) -> tuple[bool, Optional[str], Optional[int]]:
    """Decide whether to close a position before its next earnings event.

    Returns (should_exit, reason, days_until_earnings).

    - `symbol`: position symbol. For options we extract the underlying.
    - `strategy_type`: the `_strategy` tag on the position. Only
      strategies in `EXIT_ON_EARNINGS_STRATEGIES` trigger.
    - `days_before`: close when `1 <= days_to_earnings <= days_before`.
    - `asset_class`: 'us_equity' or 'us_option'. Options route through
      underlying (OCC format expected for options).
    """
    if strategy_type not in EXIT_ON_EARNINGS_STRATEGIES:
        return False, None, None
    if days_before < 1:
        return False, None, None
    lookup_sym = _underlying_for_lookup(symbol, asset_class)
    if not lookup_sym:
        return False, None, None
    next_dt = get_next_earnings_date(lookup_sym)
    if next_dt is None:
        return False, None, None
    days_to = (next_dt - now_et().date()).days
    # Window: today (days_to == 0) through days_before days out.
    # We do NOT fire if earnings are in the past (days_to < 0) —
    # that's stale data, not a fresh pre-earnings warning.
    if 0 <= days_to <= days_before:
        reason = f"pre_earnings_exit ({days_to}d to earnings)"
        return True, reason, days_to
    return False, None, days_to


def _underlying_for_lookup(symbol: str, asset_class: str) -> str:
    """Resolve OCC option symbols to their underlying. Non-options pass
    through as-is."""
    if not symbol:
        return ""
    if asset_class != "us_option":
        return symbol.upper()
    # OCC format: root (1-6 chars) + YYMMDD + C/P + strike*1000 (8 digits)
    import re
    m = re.match(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$", symbol.upper())
    return m.group(1) if m else ""


def clear_cache() -> None:
    """Test hook — drop all cached entries."""
    with _CACHE_LOCK:
        _CACHE.clear()
