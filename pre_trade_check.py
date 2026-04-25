"""Round-61 pt.72 — pre-trade quote-snapshot abort.

The screener picks a stock at 9:35 AM with price=$100, spread=0.05.
The order goes to Alpaca at 9:35:08. In those 8 seconds the spread
might have widened to $0.50 (10× worse fill) or the price might
have gapped 1.5% on a fresh news drop. The deploy fires anyway,
locking in the bad fill.

This module adds a last-millisecond sanity check: fetch a fresh
``latestQuote`` right before placing the order, abort if either:
  * spread widened materially (default: > 0.5%) from screener time
  * price gapped > 1% from screener-time price

Pure module. Caller injects:
  * ``fetch_quote_fn(symbol) -> {"bid": ..., "ask": ..., "last": ...}``
  * pick dict with screener-time `price`, `bid`, `ask`

Returns ``{"allow": bool, "reason": str, "live_quote": {...}}``.

Use:
    >>> from pre_trade_check import evaluate_pre_trade_quote
    >>> result = evaluate_pre_trade_quote(pick, fetch_quote_fn)
    >>> if not result["allow"]:
    ...     log(f"Aborting deploy: {result['reason']}")
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional


# How wide the live spread can be before we abort. Mirrors
# spread_filter.DEFAULT_MAX_SPREAD_PCT but applies to the
# moment-of-execution quote, not the screener-time quote.
DEFAULT_MAX_LIVE_SPREAD_PCT: float = 0.5

# Maximum allowed price drift from screener time. >1% means a
# meaningful microstructure event happened in the seconds since
# the screener picked this — wait for the next cycle.
DEFAULT_MAX_PRICE_DRIFT_PCT: float = 1.0

# Minimum allowed bid/ask values. Sub-$0.01 quotes are usually a
# data hiccup; treat as "no live quote" and fail-open.
MIN_VALID_PRICE: float = 0.01


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def compute_live_spread_pct(bid, ask) -> Optional[float]:
    """Return ``(ask - bid) / mid * 100``. None on bad inputs."""
    b = _safe_float(bid)
    a = _safe_float(ask)
    if b is None or a is None or a < b:
        return None
    mid = (a + b) / 2
    if mid <= 0:
        return None
    return (a - b) / mid * 100


def compute_price_drift_pct(screener_price, live_price) -> Optional[float]:
    """Signed % drift: ``(live - screener) / screener * 100``."""
    s = _safe_float(screener_price)
    live = _safe_float(live_price)
    if s is None or live is None:
        return None
    return (live - s) / s * 100


def evaluate_pre_trade_quote(
        pick: Mapping,
        fetch_quote_fn: Callable,
        *,
        max_live_spread_pct: float = DEFAULT_MAX_LIVE_SPREAD_PCT,
        max_price_drift_pct: float = DEFAULT_MAX_PRICE_DRIFT_PCT,
        side: str = "long",
        ) -> dict:
    """Return ``{"allow": bool, "reason": str, "live_quote": dict,
    "spread_pct": float | None, "drift_pct": float | None}``.

    Args:
      pick: pick dict with at minimum ``symbol`` and ``price``
        (screener-time price).
      fetch_quote_fn: callable ``(symbol) -> {"bid": ..., "ask":
        ..., "last": ...}`` or None on failure.
      max_live_spread_pct: abort if live spread > this. Default 0.5%.
      max_price_drift_pct: abort if abs(price drift) > this. Default 1.0%.
      side: "long" (gate against UP drift) or "short" (gate against
        DOWN drift). Either side aborts on spread.

    Fail-open: any error in the fetch or any missing field returns
    ``allow=True`` with a "no_live_quote_fail_open" reason. The
    screener-time gates already applied; this is microstructure
    insurance, not a hard prerequisite.
    """
    out = {
        "allow": True, "reason": "ok",
        "live_quote": None, "spread_pct": None, "drift_pct": None,
    }
    if not isinstance(pick, Mapping):
        out["reason"] = "no_live_quote_fail_open: bad pick"
        return out
    sym = (pick.get("symbol") or "").upper()
    if not sym:
        out["reason"] = "no_live_quote_fail_open: no symbol"
        return out
    try:
        quote = fetch_quote_fn(sym)
    except Exception as e:
        out["reason"] = f"no_live_quote_fail_open: fetch error {e}"
        return out
    if not isinstance(quote, Mapping):
        out["reason"] = "no_live_quote_fail_open: empty quote"
        return out
    out["live_quote"] = dict(quote)
    bid = quote.get("bid")
    ask = quote.get("ask")
    last = quote.get("last") or quote.get("price")
    # Spread check
    spread_pct = compute_live_spread_pct(bid, ask)
    out["spread_pct"] = spread_pct
    if spread_pct is not None and spread_pct > max_live_spread_pct:
        out["allow"] = False
        out["reason"] = (f"wide_live_spread: {spread_pct:.2f}% > "
                          f"{max_live_spread_pct}%")
        return out
    # Drift check
    screener_price = pick.get("price") or pick.get("current_price")
    live_price = last
    drift_pct = compute_price_drift_pct(screener_price, live_price)
    out["drift_pct"] = drift_pct
    if drift_pct is not None:
        # For longs, abort on UP drift (we'd be chasing). For shorts,
        # abort on DOWN drift (the move already played out).
        # Either side aborts on excessive opposite drift too — a
        # pre-trade gap usually means breaking news.
        if abs(drift_pct) > max_price_drift_pct:
            direction = "up" if drift_pct > 0 else "down"
            out["allow"] = False
            out["reason"] = (f"price_drift_{direction}: "
                              f"{drift_pct:+.2f}% (|drift| > "
                              f"{max_price_drift_pct}%)")
            return out
    out["reason"] = (f"ok: spread {spread_pct or 0:.2f}%, "
                      f"drift {drift_pct or 0:+.2f}%")
    return out
