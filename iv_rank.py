#!/usr/bin/env python3
"""
iv_rank.py — Implied Volatility Rank for wheel-strategy gating.

Round-11 Tier 2 addition. Professional option sellers only sell premium
when it's RICH — measured by IV Rank (where current IV sits within its
own 52-week range). Selling puts when IV Rank < 30 leaves money on
the table; selling when IV Rank > 50 captures the meaningful edge.

IV Rank = (current_IV - 52w_low_IV) / (52w_high_IV - 52w_low_IV) * 100

Using historical volatility (HV) as a proxy when implied isn't easily
available — HV rank correlates ~0.8 with IV rank and is computable from
the same daily bars we already fetch. This is the free alternative to
paid options-data providers.

Public API:

    compute_hv_rank(bars, window=20, lookback=252) -> float
        0..100 score. Historical volatility of recent `window` days
        ranked against trailing `lookback` days.

    get_hv_rank_for_symbol(symbol, data_dir=None, max_age_hours=24) -> dict
        {hv_rank, current_hv, low_hv, high_hv}
        Fetches 1y of daily bars via yfinance and computes.

    should_sell_put(hv_rank, threshold=40) -> bool
        True if HV Rank high enough that put premium is "rich".

    iv_rank_score_bonus(hv_rank) -> float
        0..15 score bonus applied to wheel_score. Strong rank adds
        more premium edge.
"""
from __future__ import annotations
import json
import math
import os
import tempfile
from datetime import datetime

try:
    from et_time import now_et
except ImportError:
    def now_et():
        return datetime.now()


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def compute_hv(closes, window=20):
    """Annualized historical volatility over the last `window` closes.
    sqrt(252) * stdev(daily log returns). Returns 0 on bad data."""
    if not closes or len(closes) < window + 1:
        return 0.0
    rets = []
    for i in range(-window, 0):
        prev = _safe_float(closes[i - 1])
        cur = _safe_float(closes[i])
        if prev <= 0 or cur <= 0:
            continue
        rets.append(math.log(cur / prev))
    if not rets or len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    return math.sqrt(var) * math.sqrt(252)  # annualized


def compute_hv_rank(closes, window=20, lookback=252):
    """Where today's HV sits in its trailing 1y range. Returns 0..100.

    - Computes rolling HV(window) for each day in the lookback.
    - Finds the min / max over the lookback.
    - Ranks today's HV in that range.

    Returns 50 on insufficient data (neutral fallback, caller can
    check for None if they want stricter behavior)."""
    if not closes or len(closes) < window + 5:
        return 50.0
    hvs = []
    for i in range(window, len(closes)):
        window_closes = closes[i - window: i + 1]
        hv = compute_hv(window_closes, window=window)
        if hv > 0:
            hvs.append(hv)
    if not hvs or len(hvs) < 10:
        return 50.0
    current_hv = hvs[-1]
    # Use up to lookback days of history for ranking
    lookback_hvs = hvs[-lookback:] if len(hvs) > lookback else hvs
    low = min(lookback_hvs)
    high = max(lookback_hvs)
    if high <= low:
        return 50.0
    rank = (current_hv - low) / (high - low) * 100
    return max(0.0, min(100.0, rank))


def _cache_path(data_dir):
    base = data_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "iv_rank_cache.json")


def _load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path, data):
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except Exception as e:
        print(f"[iv_rank] cache save failed: {e}")


def get_hv_rank_for_symbol(symbol, data_dir=None, max_age_hours=24):
    """Returns cached HV rank for a symbol, refreshing via yfinance
    if older than max_age_hours. Always returns a dict — neutral
    defaults on error."""
    path = _cache_path(data_dir)
    cache = _load_cache(path) or {}
    entry = cache.get(symbol, {})
    if entry.get("computed_at"):
        try:
            computed = datetime.fromisoformat(entry["computed_at"])
            if computed.tzinfo is not None:
                computed = computed.replace(tzinfo=None)
            age = (now_et().replace(tzinfo=None) - computed).total_seconds() / 3600.0
            if age < max_age_hours:
                return entry
        except (ValueError, TypeError):
            pass

    # Round-11: rate-limited via yfinance_budget
    try:
        from yfinance_budget import yf_history
        hist = yf_history(symbol, period="1y", interval="1d", auto_adjust=True)
    except ImportError:
        try:
            import yfinance as yf
            hist = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
        except ImportError:
            return {"hv_rank": 50.0, "current_hv": 0, "error": "yfinance missing"}

    if hist is None:
        # yfinance rate-limited or circuit-open. Previously returned a
        # neutral hv_rank=50, which callers can't distinguish from a real
        # neutral reading — they might open a put thinking IV is average
        # when actually we have zero data. Keep the neutral value (so the
        # dashboard card still renders) but mark rate_limited=True so
        # decision paths can skip, and ping telemetry so we see it.
        try:
            from observability import capture_message
            capture_message(f"iv_rank yfinance rate-limited for {symbol}",
                            level="warning", component="iv_rank")
        except Exception:
            pass
        return {"hv_rank": 50.0, "current_hv": 0,
                "rate_limited": True,
                "error": "yfinance returned None (rate-limited?)"}
    try:
        closes = hist["Close"].dropna().tolist()
        if len(closes) < 50:
            return {"hv_rank": 50.0, "current_hv": 0,
                    "error": "insufficient history"}
        hv_rank = compute_hv_rank(closes)
        current_hv = compute_hv(closes, window=20)
        # low / high over the lookback for transparency
        hvs = [compute_hv(closes[i - 20: i + 1], 20)
               for i in range(20, len(closes))]
        hvs = [h for h in hvs if h > 0]
        low_hv = min(hvs) if hvs else 0
        high_hv = max(hvs) if hvs else 0
        result = {
            "symbol": symbol,
            "hv_rank": round(hv_rank, 1),
            "current_hv": round(current_hv, 4),
            "low_hv": round(low_hv, 4),
            "high_hv": round(high_hv, 4),
            "computed_at": now_et().isoformat(),
        }
        cache[symbol] = result
        _save_cache(path, cache)
        return result
    except Exception as e:
        return {"hv_rank": 50.0, "current_hv": 0, "error": str(e)}


def should_sell_put(hv_rank, threshold=40):
    """Policy gate: True if HV rank >= threshold (premium rich enough
    to bother selling). Default threshold 40 = "above 40th percentile
    of its own 1y vol range." Tune higher for stricter filter."""
    return hv_rank is not None and hv_rank >= threshold


def iv_rank_score_bonus(hv_rank):
    """Convert HV rank to a score bonus for wheel candidates.
    Piecewise linear: rank 30 -> 0, rank 60 -> 10, rank 80+ -> 15.
    Ranks below 30 get a penalty (premium too thin, skip the trade)."""
    if hv_rank is None:
        return 0
    if hv_rank < 20:
        return -10  # skip territory
    if hv_rank < 30:
        return -5
    if hv_rank < 50:
        return 3
    if hv_rank < 70:
        return 10
    return 15


if __name__ == "__main__":
    # Smoke test with synthetic data
    import random
    random.seed(1)
    closes = []
    price = 100.0
    for i in range(300):
        price = price * (1 + random.gauss(0, 0.02))
        closes.append(price)
    print(f"Synthetic HV(20): {compute_hv(closes, 20):.4f}")
    print(f"Synthetic HV Rank: {compute_hv_rank(closes):.1f}")
    # Try a live fetch
    r = get_hv_rank_for_symbol("SPY")
    print("SPY:", json.dumps(r, indent=2, default=str))
