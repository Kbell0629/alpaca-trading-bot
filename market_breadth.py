#!/usr/bin/env python3
"""
market_breadth.py — % of S&P 500 stocks trading above their 50-day MA.

Round-11 Tier 1 addition. Provides a breadth gate for breakout + PEAD
auto-deploys: if fewer than 40% of S&P 500 components are above their
50dma, the market is too weak to trust breakouts (they fail ~80% of
the time in weak-breadth regimes).

Uses yfinance for bulk data fetching (already a project dependency).
Results are cached to disk for 24h so we don't hammer Yahoo every
scheduler tick.

Public API:

    get_breadth_pct(data_dir=None, max_age_hours=24) -> dict
        Returns {"breadth_pct": float, "above_50dma": int,
                 "total": int, "computed_at": iso8601, "regime": str}
        regime: "strong" (>60), "healthy" (40-60), "weak" (<40)

    should_block_breakouts(breadth_threshold=40) -> bool
        Convenience wrapper for the scheduler: True if breadth is too
        weak to deploy new breakouts.
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime, timedelta

try:
    from et_time import now_et
except ImportError:
    def now_et():
        return datetime.now()

# Top 100 S&P 500 names by weight as a fast proxy for breadth. Full
# 503-ticker list is slower to fetch and doesn't change the signal
# meaningfully (top 100 are ~70% of index weight). The "breadth" in
# classic studies is equal-weighted, so we treat these equally too.
# This list is intentionally static — weekly rebalance not required
# for a crude breadth gauge (we only care about the >40% threshold
# with significant margin).
SP500_TOP_100 = [
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "BRK.B", "TSLA", "LLY",
    "V", "UNH", "XOM", "JPM", "JNJ", "WMT", "MA", "PG", "AVGO", "HD",
    "CVX", "MRK", "ABBV", "KO", "PEP", "COST", "ADBE", "BAC", "CRM", "MCD",
    "TMO", "CSCO", "NFLX", "ACN", "ABT", "DHR", "LIN", "WFC", "DIS", "TXN",
    "VZ", "CMCSA", "PM", "ORCL", "NEE", "BMY", "INTC", "NKE", "RTX", "QCOM",
    "UPS", "HON", "AMGN", "IBM", "SBUX", "LOW", "CAT", "GS", "INTU", "MS",
    "UNP", "DE", "ELV", "AMAT", "BLK", "SPGI", "GILD", "AXP", "ISRG", "MDT",
    "BKNG", "SCHW", "LMT", "PLD", "SYK", "TJX", "ADI", "CVS", "MDLZ", "MMC",
    "C", "VRTX", "ADP", "CB", "BSX", "REGN", "ZTS", "CI", "BDX", "DUK",
    "PGR", "PANW", "ETN", "SO", "T", "SLB", "NOW", "FI", "ITW", "EOG",
]


def _cache_path(data_dir):
    base = data_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "market_breadth.json")


def _load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(path, data):
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except Exception as e:
        print(f"[breadth] cache save failed: {e}")


def _classify_regime(pct):
    if pct >= 60:
        return "strong"
    if pct >= 40:
        return "healthy"
    return "weak"


def compute_breadth_live(symbols=None):
    """Compute breadth percent by checking each symbol's current price
    vs its 50-day simple moving average via yfinance. Returns the full
    result dict. This is the expensive path — most callers should use
    `get_breadth_pct()` which caches for 24h."""
    # Round-11: use the shared rate-limit + retry wrapper so we don't
    # compete with other factor modules for Yahoo's per-minute budget.
    try:
        from yfinance_budget import yf_download
    except ImportError:
        yf_download = None

    if yf_download is None:
        try:
            import yfinance as yf
            yf_download = lambda **kw: yf.download(**kw)
        except ImportError:
            return {"error": "yfinance not installed", "breadth_pct": 50.0,
                    "above_50dma": 0, "total": 0, "regime": "healthy",
                    "computed_at": now_et().isoformat()}

    tickers = symbols or SP500_TOP_100
    above_count = 0
    checked = 0
    # Bulk download 60 trading days (enough for 50dma); period='3mo'
    # covers even after holidays. Use threads=True for parallel fetch.
    data = yf_download(
        tickers=" ".join(tickers),
        period="3mo",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    if data is None or getattr(data, "empty", True):
        return {"error": "yfinance returned empty or rate-limited", "breadth_pct": 50.0,
                "above_50dma": 0, "total": 0, "regime": "healthy",
                "computed_at": now_et().isoformat()}

    for t in tickers:
        try:
            # When downloading >1 ticker, yfinance returns a multi-index
            # dataframe. Handle both single and multi formats.
            if hasattr(data, "columns") and hasattr(data.columns, "levels"):
                closes = data[t]["Close"].dropna()
            else:
                closes = data["Close"].dropna()
            if len(closes) < 50:
                continue
            sma50 = closes.tail(50).mean()
            current = closes.iloc[-1]
            if current and sma50 and float(current) > float(sma50):
                above_count += 1
            checked += 1
        except Exception:
            continue

    if checked == 0:
        return {"error": "no tickers resolved", "breadth_pct": 50.0,
                "above_50dma": 0, "total": 0, "regime": "healthy",
                "computed_at": now_et().isoformat()}

    pct = round(above_count / checked * 100, 1)
    return {
        "breadth_pct": pct,
        "above_50dma": above_count,
        "total": checked,
        "regime": _classify_regime(pct),
        "computed_at": now_et().isoformat(),
    }


def get_breadth_pct(data_dir=None, max_age_hours=24):
    """Returns cached breadth data, refreshing from yfinance if the
    cache is older than `max_age_hours`. Always returns a dict — on
    any error a conservative default ({breadth_pct: 50, regime:
    healthy}) is returned so the caller doesn't crash."""
    path = _cache_path(data_dir)
    cached = _load_cache(path)
    if cached and cached.get("computed_at"):
        try:
            computed = datetime.fromisoformat(cached["computed_at"])
            # Strip tz if present — comparing naive to aware fails
            if computed.tzinfo is not None:
                computed = computed.replace(tzinfo=None)
            now_naive = now_et().replace(tzinfo=None)
            age_hours = (now_naive - computed).total_seconds() / 3600.0
            if age_hours < max_age_hours:
                return cached
        except (ValueError, TypeError):
            pass  # bad timestamp — refresh
    result = compute_breadth_live()
    if "error" not in result:
        _save_cache(path, result)
    return result


def should_block_breakouts(data_dir=None, breadth_threshold=40):
    """Convenience for scheduler: True if current breadth is below
    threshold, meaning breakout deploys should be paused."""
    data = get_breadth_pct(data_dir)
    return data.get("breadth_pct", 50.0) < breadth_threshold


if __name__ == "__main__":
    print("Computing market breadth (live)...")
    r = compute_breadth_live()
    print(json.dumps(r, indent=2, default=str))
