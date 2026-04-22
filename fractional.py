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
from contextlib import contextmanager
from typing import Optional

try:
    import fcntl as _fcntl
    _HAS_FLOCK = True
except ImportError:
    _HAS_FLOCK = False


@contextmanager
def _file_lock(path):
    """Round-52: serialize read-modify-write on the cache file to prevent
    concurrent scheduler ticks (paper + live dual-mode, or manual Force
    Deploy overlapping the regular tick) from losing entries. Uses the
    same `<path>.lock` convention as cloud_scheduler.strategy_file_lock.

    On systems without fcntl (Windows), falls back to a no-op — write
    races there are not in our deployment matrix (Railway is Linux).
    """
    if not _HAS_FLOCK:
        yield
        return
    lock_path = path + ".lock"
    fh = None
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fh = open(lock_path, "w")
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        if fh is not None:
            try:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass

FRACTIONAL_CACHE_FILENAME = "fractionable_cache.json"
# Cache lives 24 hours — Alpaca's fractionable list changes rarely
CACHE_TTL_SEC = 24 * 3600
# Minimum fractional position in dollars (Alpaca enforces ~$1)
MIN_FRACTIONAL_DOLLARS = 1.0


def _cache_path(user: dict) -> str:
    """Path to the per-user fractionable cache file.

    Round-52: removed the /tmp fallback. A user dict without _data_dir
    is a programming error — previously we silently wrote to
    /tmp/fractionable_cache.json which would collide across users.
    Raise instead so callers see the bug immediately.
    """
    data_dir = user.get("_data_dir")
    if not data_dir:
        raise ValueError(
            "fractional._cache_path: user dict missing '_data_dir'. "
            "Caller must build a mode-scoped user dict via "
            "auth.user_data_dir + _mode before invoking fractional helpers."
        )
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
    """Atomically write the cache file under a file lock. Round-52:
    added `_file_lock` so two scheduler ticks writing fresh caches
    simultaneously can't race on the tempfile+rename."""
    path = _cache_path(user)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    import time
    tmp = path + ".tmp"
    with _file_lock(path):
        try:
            with open(tmp, "w") as f:
                json.dump({
                    "cached_at": time.time(),
                    "count": len(fractionable_symbols),
                    "symbols": list(fractionable_symbols),
                }, f)
            os.rename(tmp, path)
        except OSError as _e:
            try: os.unlink(tmp)
            except OSError: pass
            # Round-52: surface IO failures via Sentry instead of silently
            # dropping the cache. Cache failure → next deploy re-fetches
            # from Alpaca, which is OK; but repeat failures indicate a
            # disk issue we want visibility on.
            try:
                from observability import capture_exception
                capture_exception(_e, component="fractional._save_cache",
                                    path=path)
            except Exception:
                pass


def refresh_cache(user: dict, api_get_fn) -> Optional[set]:
    """Fetch the fractionable-asset list from Alpaca and cache it.
    `api_get_fn` is a callable like `scheduler_api.user_api_get(user, path)`
    that returns the parsed JSON response.

    Returns the set of fractionable symbols, or None on error.

    Round-52: Alpaca API errors now route through observability.capture_exception
    so systematic failures (auth rot, rate-limit exhaustion) surface in
    Sentry instead of silently returning None on every deploy.
    """
    try:
        # Alpaca endpoint: /v2/assets?status=active&asset_class=us_equity
        # then filter to fractionable=True. This returns ~8k rows.
        resp = api_get_fn(user, "/assets?status=active&asset_class=us_equity")
    except Exception as _e:
        try:
            from observability import capture_exception
            capture_exception(_e, component="fractional.refresh_cache")
        except Exception:
            pass
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

    # Preferred path: fractional.
    # Round-52 fix: if target is below Alpaca's $1 fractional minimum
    # BUT a whole share is affordable, fall through to whole-share
    # sizing instead of rejecting outright.
    if fractional_allowed and fractionable:
        if target_dollars < MIN_FRACTIONAL_DOLLARS:
            # Fall through to whole-share path below — if price ≤ target
            # we can still buy 1 whole share. Stash reason in case
            # whole-share ALSO rejects.
            _frac_reject_reason = (
                f"target ${target_dollars:.2f} below Alpaca fractional "
                f"minimum ${MIN_FRACTIONAL_DOLLARS}")
        else:
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
    else:
        _frac_reject_reason = None

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
    if _frac_reject_reason:
        # Fractional was tried but rejected by min-notional; whole-share
        # also couldn't round to >=1. Surface the fractional reason.
        result["reason"] = _frac_reject_reason + (
            f" AND whole-share needs ${price:.2f} > target ${target_dollars:.2f}")
    elif fractional_allowed and not fractionable:
        result["reason"] = (f"price ${price:.2f} > target ${target_dollars:.2f}; "
                            f"symbol {symbol} is not fractionable")
    else:
        result["reason"] = (f"price ${price:.2f} > target ${target_dollars:.2f}; "
                            "fractional not enabled for this tier")
    return result
