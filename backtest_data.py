"""Round-61 pt.37 — backtest data layer (OHLCV fetcher + on-disk cache).

Pure simulation lives in ``backtest_core.py``; this module is the
I/O boundary that pulls historical daily bars from yfinance (free,
no auth, no rate-limit headache) and caches them on disk so repeat
runs don't re-fetch.

Cache layout: ``$DATA_DIR/backtest_cache/<SYMBOL>.json`` containing:
    {
        "symbol": "AAPL",
        "fetched_at": "2026-04-25T10:00:00Z",
        "bars": [
            {"date": "2026-03-26", "open": ..., "high": ...,
             "low": ..., "close": ..., "volume": ...},
            ...
        ]
    }

Cache freshness: bars cached within ``CACHE_TTL_HOURS`` (default 12)
are reused. Older entries are refetched. The cache is per-symbol so
adding one new symbol to a backtest doesn't invalidate the rest.

Graceful degrade: if yfinance import fails OR the network is down,
``fetch_bars`` returns the cached bars (even if stale) rather than
raising — backtest is best-effort, not load-bearing.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone


CACHE_DIRNAME = "backtest_cache"
CACHE_TTL_HOURS = 12
DEFAULT_LOOKBACK_DAYS = 60  # plenty for indicator warm-up + 30-day window


# ============================================================================
# Cache I/O
# ============================================================================

def _cache_dir(data_dir):
    """Return the per-instance cache dir; create on first use."""
    p = os.path.join(data_dir, CACHE_DIRNAME)
    os.makedirs(p, exist_ok=True)
    return p


def _cache_path(data_dir, symbol):
    return os.path.join(_cache_dir(data_dir), f"{symbol.upper()}.json")


def _atomic_write(path, payload):
    """tempfile + os.rename — same pattern used elsewhere in the bot."""
    dir_name = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_cached_bars(data_dir, symbol):
    """Return the cached entry for `symbol` (or None if not cached).

    Returned dict shape: {"symbol", "fetched_at", "bars": [...]}.
    """
    path = _cache_path(data_dir, symbol)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_cached_bars(data_dir, symbol, bars):
    """Persist `bars` to the cache with a current `fetched_at` stamp."""
    payload = {
        "symbol": symbol.upper(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "bars": bars,
    }
    _atomic_write(_cache_path(data_dir, symbol), payload)
    return payload


def is_cache_fresh(entry, ttl_hours=CACHE_TTL_HOURS):
    """True if the cache entry was fetched within the TTL."""
    if not entry or not entry.get("fetched_at"):
        return False
    try:
        fetched = datetime.fromisoformat(
            entry["fetched_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - fetched
    return age < timedelta(hours=ttl_hours)


# ============================================================================
# Network fetch (yfinance)
# ============================================================================

def _yfinance_fetch(symbol, days):
    """Pull `days` of daily bars for `symbol` via yfinance. Returns
    a bars list or None on failure. Pure-function w.r.t. the network
    response (caller decides whether to cache).
    """
    try:
        import yfinance as yf  # local import — yfinance is heavy
    except Exception:
        return None
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(days * 1.5) + 5)  # padding for weekends
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start.date(), end=end.date(),
                                interval="1d", auto_adjust=False)
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    bars = []
    for ts, row in hist.iterrows():
        try:
            d = ts.date().isoformat()
        except Exception:
            d = str(ts)[:10]
        bars.append({
            "date": d,
            "open": float(row.get("Open") or 0),
            "high": float(row.get("High") or 0),
            "low": float(row.get("Low") or 0),
            "close": float(row.get("Close") or 0),
            "volume": float(row.get("Volume") or 0),
        })
    # Trim to requested days from the end
    bars = bars[-days:] if days < len(bars) else bars
    return bars


# ============================================================================
# Public API
# ============================================================================

def fetch_bars(data_dir, symbol, days=DEFAULT_LOOKBACK_DAYS,
                ttl_hours=CACHE_TTL_HOURS, force_refresh=False,
                fetcher=None):
    """Get `days` worth of daily bars for `symbol`. Cache-first.

    ``data_dir`` — root dir for the per-user cache (or shared DATA_DIR).
    ``days`` — minimum bar count to return. Cached entries with FEWER
        bars trigger a refetch.
    ``ttl_hours`` — cache freshness window. Older entries refetch.
    ``force_refresh`` — bypass the cache check entirely.
    ``fetcher`` — optional callable (symbol, days) -> bars or None.
        Defaults to ``_yfinance_fetch``. Tests inject fakes here.

    Returns the bars list (possibly stale on network failure) or
    None if no cache and no network.
    """
    fetcher = fetcher or _yfinance_fetch
    cached = load_cached_bars(data_dir, symbol)
    if (not force_refresh
            and is_cache_fresh(cached, ttl_hours=ttl_hours)
            and cached
            and len(cached.get("bars") or []) >= days):
        return cached["bars"][-days:]
    # Need to fetch
    fresh = fetcher(symbol, days)
    if fresh:
        save_cached_bars(data_dir, symbol, fresh)
        return fresh[-days:]
    # Network failed — return whatever we have, even if stale
    if cached and cached.get("bars"):
        return cached["bars"][-days:]
    return None


def fetch_bars_for_symbols(data_dir, symbols, days=DEFAULT_LOOKBACK_DAYS,
                             ttl_hours=CACHE_TTL_HOURS,
                             force_refresh=False, fetcher=None):
    """Bulk wrapper: returns ``{symbol: bars or None}``."""
    out = {}
    for sym in symbols or []:
        if not sym:
            continue
        bars = fetch_bars(
            data_dir, sym, days=days, ttl_hours=ttl_hours,
            force_refresh=force_refresh, fetcher=fetcher,
        )
        out[sym] = bars
    return out


def universe_from_journal(journal):
    """Pull a backtest symbol universe from a user's trade journal.

    Returns a sorted list of unique symbols (OCC option contracts
    resolved to underlyings via the round-22 multi-contract pattern).
    Useful for "what would the bot have done on the symbols it
    actually traded" backtests.
    """
    seen = set()
    for t in (journal or {}).get("trades") or []:
        sym = (t.get("symbol") or "").upper()
        if not sym:
            continue
        # Strip OCC option suffix (round-22 pattern):
        #   HIMS260508P00027000 → HIMS
        if (len(sym) >= 15 and sym[-15:-9].isdigit()
                and sym[-9] in ("P", "C")
                and sym[-8:].isdigit()):
            sym = sym[:-15]
        seen.add(sym)
    return sorted(seen)


def universe_from_dashboard_data(data):
    """Pull a backtest symbol universe from a dashboard /api/data
    snapshot — uses the screener's top picks. Useful for "what
    would each strategy have done on the current top 50?" runs.
    """
    syms = []
    for pick in (data or {}).get("picks") or []:
        sym = (pick.get("symbol") or "").upper()
        if sym and sym not in syms:
            syms.append(sym)
    return syms
