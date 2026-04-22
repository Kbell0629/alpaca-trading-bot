"""
Round-50: fractional-share support.

Alpaca supports fractional quantities (e.g. 0.1234 shares) for most
liquid US equities. This unlocks every stock for small accounts —
a $500 account can hold a $25 slice of TSLA even at $250/share.

Constraints Alpaca enforces:
  * Only symbols where `assets.fractionable == True` accept fractional qty
  * Fractional orders must be market or notional (limit support is
    spotty; we'll use market for fractional, limit for whole-share)
  * Minimum notional per fractional order is typically $1
  * Not all strategies/order-types support fractional (e.g., bracket
    OCO orders require whole shares)

This module:
  * Caches the fractionable-asset list (per-user; refreshed daily)
  * Provides `is_fractionable(symbol, user)` check
  * Provides `size_position(symbol, target_dollars, price, user, tier_cfg)`
    that returns a qty respecting fractional / whole-share per tier + asset
"""
from __future__ import annotations

import json
import os
from typing import Optional

FRACTIONAL_CACHE_FILENAME = "fractionable_cache.json"
# Cache lives 24 hours — Alpaca's fractionable list changes rarely
CACHE_TTL_SEC = 24 * 3600
# Minimum fractional position in dollars (Alpaca enforces ~$1)
MIN_FRACTIONAL_DOLLARS = 1.0


def _cache_path(user: dict) -> str:
    """Path to the per-user fractionable cache file."""
    data_dir = user.get("_data_dir")
    if not data_dir:
        import tempfile
        data_dir = tempfile.gettempdir()
    return os.path.join(data_dir, FRACTIONAL_CACHE_FILENAME)


def _load_cache(user: dict) -> Optional[dict]:
    """Return cached fractionable list + timestamp, or None if missing/stale."""
    path = _cache_path(user)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    import time
    if not isinstance(data, dict):
        return None
    if (time.time() - data.get("cached_at", 0)) > CACHE_TTL_SEC:
        return None  # stale
    return data


def _save_cache(user: dict, fractionable_symbols: list) -> None:
    """Atomically write the cache file."""
    path = _cache_path(user)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    import time
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({
                "cached_at": time.time(),
                "count": len(fractionable_symbols),
                "symbols": list(fractionable_symbols),
            }, f)
        os.rename(tmp, path)
    except OSError:
        try: os.unlink(tmp)
        except OSError: pass


def refresh_cache(user: dict, api_get_fn) -> Optional[set]:
    """Fetch the fractionable-asset list from Alpaca and cache it.
    `api_get_fn` is a callable like `scheduler_api.user_api_get(user, path)`
    that returns the parsed JSON response.

    Returns the set of fractionable symbols, or None on error.
    """
    try:
        # Alpaca endpoint: /v2/assets?status=active&asset_class=us_equity
        # then filter to fractionable=True. This returns ~8k rows.
        resp = api_get_fn(user, "/assets?status=active&asset_class=us_equity")
    except Exception:
        return None
    if not isinstance(resp, list):
        return None
    symbols = {
        (a.get("symbol") or "").upper()
        for a in resp
        if isinstance(a, dict) and a.get("fractionable") is True
        and a.get("tradable") is True
    }
    _save_cache(user, sorted(symbols))
    return symbols


def get_fractionable_symbols(user: dict, api_get_fn=None) -> set:
    """Return the set of fractionable symbols for this user. Uses cache
    if fresh; refreshes via api_get_fn if stale/missing. Returns an
    empty set on any error so callers fail safe (treat as non-
    fractionable)."""
    cache = _load_cache(user)
    if cache and isinstance(cache.get("symbols"), list):
        return {s.upper() for s in cache["symbols"]}
    if api_get_fn is None:
        return set()  # no cache, no fetcher → fail safe
    fresh = refresh_cache(user, api_get_fn)
    return fresh or set()


def is_fractionable(symbol: str, user: dict, api_get_fn=None) -> bool:
    """Quick lookup — is this symbol fractionable for this user?"""
    if not symbol:
        return False
    return symbol.upper() in get_fractionable_symbols(user, api_get_fn)


def size_position(symbol: str, target_dollars: float, price: float,
                   user: dict, tier_cfg: dict,
                   api_get_fn=None) -> dict:
    """Compute the order quantity for a target-dollar position.

    Returns a dict:
      {
        "qty": float or int,   # the quantity to order
        "notional": float,     # the actual $ committed
        "fractional": bool,    # True if qty is non-integer
        "order_type_hint": "market" | "limit",
        "reason": str,         # human-readable explanation
      }

    Logic:
      * If price or target_dollars <= 0 → qty=0, reason="invalid inputs"
      * If tier allows fractional AND symbol is fractionable →
          fractional qty (rounded 4dp), market order, min $1
      * Otherwise whole-share: floor(target / price), limit order OK
      * If whole-share rounds to 0 (price > target):
          - If fractional allowed + symbol eligible: fall back to fractional
          - Else: qty=0, reason="price too high for this account size"
    """
    result = {"qty": 0, "notional": 0.0, "fractional": False,
              "order_type_hint": "limit", "reason": ""}
    if not symbol or target_dollars <= 0 or price <= 0:
        result["reason"] = "invalid inputs (symbol/target/price ≤ 0)"
        return result
    # User's fractional preference: tier default OR user override
    fractional_allowed = bool(tier_cfg.get("fractional_default", False))
    if tier_cfg.get("user_override_fractional_enabled"):
        fractional_allowed = bool(tier_cfg.get("fractional_default", False))
    # Check symbol eligibility if fractional is even a possibility
    fractionable = False
    if fractional_allowed:
        fractionable = is_fractionable(symbol, user, api_get_fn)

    # Preferred path: fractional
    if fractional_allowed and fractionable:
        if target_dollars < MIN_FRACTIONAL_DOLLARS:
            result["reason"] = (f"target ${target_dollars:.2f} below Alpaca "
                                f"fractional minimum ${MIN_FRACTIONAL_DOLLARS}")
            return result
        qty = round(target_dollars / price, 4)
        if qty <= 0:
            result["reason"] = "computed qty rounds to zero"
            return result
        result.update({
            "qty": qty,
            "notional": round(qty * price, 2),
            "fractional": True,
            "order_type_hint": "market",  # fractional → market only
            "reason": f"fractional {qty} × ${price:.2f}",
        })
        return result

    # Whole-share path
    qty = int(target_dollars // price)
    if qty >= 1:
        result.update({
            "qty": qty,
            "notional": round(qty * price, 2),
            "fractional": False,
            "order_type_hint": "limit",
            "reason": f"whole-share {qty} × ${price:.2f}",
        })
        return result

    # Whole-share rounds to zero — can we salvage via fractional?
    if fractional_allowed:
        # Fractional was allowed but symbol not eligible, OR min-notional
        if fractionable:
            # Shouldn't reach here (fractional path above should have hit)
            pass
        result["reason"] = (f"price ${price:.2f} > target ${target_dollars:.2f}; "
                            f"symbol {symbol} is not fractionable")
    else:
        result["reason"] = (f"price ${price:.2f} > target ${target_dollars:.2f}; "
                            "fractional not enabled for this tier")
    return result
