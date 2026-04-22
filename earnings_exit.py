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


# Round-58: track the last fetch failure reason so the
# should_exit_for_earnings caller can emit a LOUD Sentry breadcrumb
# instead of silently swallowing a yfinance error. Before this, an
# `ImportError` / shape drift / network hiccup would make the exit
# rule silently fail-open — INTC sat through its earnings on
# 2026-04-23 because the fetch returned None and nobody knew.
_LAST_FETCH_ERR: dict[str, str] = {}


def get_last_fetch_error(symbol: str) -> Optional[str]:
    return _LAST_FETCH_ERR.get(symbol.upper())


def _fetch_next_earnings_from_yfinance(symbol: str) -> Optional[date]:
    """One-shot yfinance call. Returns the next FUTURE earnings date
    (unreported) or None on any error / missing data.

    Round-58: now records the failure reason in `_LAST_FETCH_ERR[symbol]`
    so the caller can distinguish "really no upcoming earnings" (no
    fetch error) from "fetch failed and we're silently holding through
    earnings" (fetch error surfaced)."""
    sym_u = symbol.upper() if symbol else ""
    try:
        import yfinance as yf
    except ImportError:
        _LAST_FETCH_ERR[sym_u] = "yfinance_not_installed"
        return None
    try:
        ticker = yf.Ticker(symbol)
        ed = ticker.get_earnings_dates(limit=8)
    except (ValueError, TypeError, AttributeError, KeyError) as e:
        _LAST_FETCH_ERR[sym_u] = f"shape_drift:{type(e).__name__}"
        return None
    except Exception as e:
        _LAST_FETCH_ERR[sym_u] = f"network:{type(e).__name__}"
        return None
    if ed is None or (hasattr(ed, "empty") and ed.empty):
        _LAST_FETCH_ERR[sym_u] = "empty_result"
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
    # Clear prior error on success. Distinguish "no upcoming earnings
    # in the next 8 scheduled events" (returned dataframe, no future
    # unreported row) vs. fetch failure — the former is legitimate.
    if next_dt is None:
        _LAST_FETCH_ERR[sym_u] = "no_future_unreported"
    else:
        _LAST_FETCH_ERR.pop(sym_u, None)
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
        # Round-58: LOUD on silent-skip. When the lookup fails for a
        # real fetch reason (network / shape drift / import error),
        # surface to Sentry so the operator knows the earnings gate
        # isn't actually gating. Legitimate "no upcoming earnings in
        # the next 8 scheduled events" ("no_future_unreported") stays
        # quiet — that's a valid None, not a silent failure.
        err = _LAST_FETCH_ERR.get(lookup_sym)
        if err and err != "no_future_unreported":
            try:
                from observability import capture_message
                capture_message(
                    f"earnings_exit fetch failed: {lookup_sym} ({err})",
                    level="warning",
                    event="earnings_exit_fetch_failed",
                    symbol=lookup_sym,
                    strategy=strategy_type,
                    error=err,
                )
            except Exception:
                pass
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
    _LAST_FETCH_ERR.clear()


def force_refresh(symbol: str) -> Optional[date]:
    """Round-58: operator tool. Bust the 4-hour cache for this symbol
    and re-fetch from yfinance immediately. Returns the freshly-fetched
    next earnings date (or None on fetch failure — check
    `get_last_fetch_error(symbol)` for diagnosis). Used by the admin
    "verify earnings rule is working" button and by tests."""
    if not symbol:
        return None
    sym = symbol.upper()
    with _CACHE_LOCK:
        _CACHE.pop(sym, None)
    _LAST_FETCH_ERR.pop(sym, None)
    next_dt = _fetch_next_earnings_from_yfinance(sym)
    _cache_store(sym, next_dt)
    return next_dt
