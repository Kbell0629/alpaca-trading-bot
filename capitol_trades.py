#!/usr/bin/env python3
"""
Congressional-trading signal provider for the Alpaca bot.

Role in the system
------------------
Copy Trading is an *entry* strategy that competes in the screener alongside
Breakout, Mean Reversion, and Wheel. It only scores above zero when an
actual member of Congress has filed a recent stock purchase disclosure —
no more fake "big-cap + modest move = copy_trading" signal.

Data provider
-------------
Default: **Quiver Quant** (`/beta/live/congresstrading`). Free tier,
both chambers, official-source-derived. Set `QUIVER_API_KEY` in env
(Bearer token from https://www.quiverquant.com/).

Alternatives kept in for flexibility:
  - Finnhub `/stock/congressional-trading` — requires `FINNHUB_API_KEY`
    AND a paid plan ($99/mo tier). Free tier returns 403.
  - Stock Watcher — decommissioned (domain no longer resolves).
    Kept as a provider stub so old env configs don't break.

Without any key set, every query returns zero — copy_trading silently
drops out of the screener competition. That graceful degrade means
the architecture can deploy before a key lands; once it's set, the
5 AM daily refresh populates the cache and copy_trading starts
winning picks with real signals.

Cache
-----
Disclosures update at most daily on the official side (44-day reporting
window — a politician files within 44 days of the trade). We refresh the
cache once a day via `cloud_scheduler.run_capitol_trades_refresh` and
keep a 90-day rolling window. Re-reading the cache on every screener
run is O(entries) and cheap (<100ms for typical volume).

Scoring philosophy
------------------
A politician's buy signal gets stronger when:
  - the disclosure is recent (days-since-filing < 14 gets a boost)
  - the trade size is large ($50K-$250K tiers give bigger scores)
  - multiple politicians bought the same symbol in the same window
  - the chamber is Senate (more sway than House, per academic literature)
We never score politician SELLS positively — they're harder to read
(tax loss, rebalancing, liquidity event) and create asymmetric risk.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

from et_time import now_et

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
CACHE_FILE = os.path.join(DATA_DIR, "capitol_trades.json")

# How many days back to keep in the cache. Disclosures can be filed up
# to 45 days after the trade, so a 90-day window ensures we see the
# full reporting lag even for older trades that just got filed.
RETENTION_DAYS = 90

# Score boost tiers — applied additively. Max realistic score for a
# single very strong signal is ~25, putting Copy Trading on par with
# Breakout (~20-40) and Wheel (~20-30). Multiple politicians in the
# same window stack beyond that, which is the intent — consensus buys
# are a legitimately stronger signal.
CHAMBER_WEIGHTS = {
    "senate": 1.2,
    "house": 1.0,
}

# Amount-range buckets from disclosure forms. Politicians can't report
# exact amounts — only ranges. We map midpoints to score contributions.
# Source: US House/Senate disclosure forms (same buckets both chambers).
AMOUNT_BUCKETS = [
    ("$1,001 - $15,000", 2.0),
    ("$15,001 - $50,000", 4.0),
    ("$50,001 - $100,000", 7.0),
    ("$100,001 - $250,000", 10.0),
    ("$250,001 - $500,000", 14.0),
    ("$500,001 - $1,000,000", 18.0),
    ("$1,000,001 - $5,000,000", 22.0),
    ("$5,000,001 - $25,000,000", 26.0),
    ("$25,000,001 - $50,000,000", 30.0),
    ("Over $50,000,000", 34.0),
]


def _log(msg: str) -> None:
    print(f"[capitol_trades] {msg}", flush=True)


# ===== Provider: Finnhub =====
def _fetch_finnhub(symbols: list[str] | None = None,
                   days: int = 60) -> list[dict]:
    """Fetch recent congressional trades from Finnhub.

    If `symbols` is provided, only those symbols are queried (efficient
    for a targeted screener pass). If None, falls back to the per-symbol
    approach over a small universe — Finnhub's free tier doesn't offer
    a bulk "recent trades" endpoint, so we accept the per-symbol cost.
    """
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        _log("FINNHUB_API_KEY not set — returning empty signal set")
        return []

    to_date = date.today()
    from_date = to_date - timedelta(days=days)
    out: list[dict] = []
    syms = symbols or _DEFAULT_UNIVERSE

    # Finnhub free tier: 60 calls/min. Space requests to stay well under.
    # For 200 symbols that's ~3.5 min per refresh, acceptable for a
    # nightly task. If Kevin upgrades to paid, we can pull in bulk.
    for sym in syms:
        try:
            url = (f"https://finnhub.io/api/v1/stock/congressional-trading"
                   f"?symbol={urllib.parse.quote(sym)}"
                   f"&from={from_date.isoformat()}"
                   f"&to={to_date.isoformat()}"
                   f"&token={urllib.parse.quote(key)}")
            req = urllib.request.Request(url, headers={
                "User-Agent": "AlpacaBot/1.0 (+https://github.com/Kbell0629/alpaca-trading-bot)"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode()
            payload = json.loads(body) if body else {}
            data = payload.get("data") or []
            for row in data:
                # Finnhub row shape (documented):
                # { symbol, transactionDate, transactionPrice, amountFrom,
                #   amountTo, name, position, ownerType, assetName,
                #   assetType, filingDate, transactionType }
                tx = (row.get("transactionType") or "").lower()
                # Only buys. "purchase", "buy", "p" all seen in the wild.
                if not any(b in tx for b in ("purchase", "buy")):
                    continue
                out.append({
                    "symbol": (row.get("symbol") or sym).upper(),
                    "politician": row.get("name") or "Unknown",
                    "chamber": _infer_chamber(row.get("position")),
                    "position": row.get("position") or "",
                    "transaction_date": row.get("transactionDate") or "",
                    "filing_date": row.get("filingDate") or "",
                    "amount_from": row.get("amountFrom"),
                    "amount_to": row.get("amountTo"),
                    "amount_label": _amount_label(row.get("amountFrom"),
                                                  row.get("amountTo")),
                    "asset_type": row.get("assetType") or "equity",
                })
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _log(f"finnhub 429 — backing off 10s")
                time.sleep(10)
            else:
                _log(f"finnhub HTTPError {e.code} for {sym}: {e.reason}")
        except Exception as e:
            _log(f"finnhub error for {sym}: {type(e).__name__}: {e}")
        # Spread the free-tier 60 rpm budget: ~1s per call is safe.
        time.sleep(1.1)
    return out


# Heuristic to tag chamber when the provider doesn't give it cleanly.
def _infer_chamber(position: str | None) -> str:
    if not position:
        return "house"  # default conservative
    p = position.lower()
    if "senator" in p or "senate" in p:
        return "senate"
    if "representative" in p or "house" in p:
        return "house"
    return "house"


def _amount_label(from_v: Any, to_v: Any) -> str:
    """Turn numeric [from, to] into a disclosure-style bucket label."""
    try:
        f = float(from_v or 0)
        t = float(to_v or 0)
    except (TypeError, ValueError):
        return ""
    if t <= 15000:
        return "$1,001 - $15,000"
    if t <= 50000:
        return "$15,001 - $50,000"
    if t <= 100000:
        return "$50,001 - $100,000"
    if t <= 250000:
        return "$100,001 - $250,000"
    if t <= 500000:
        return "$250,001 - $500,000"
    if t <= 1_000_000:
        return "$500,001 - $1,000,000"
    if t <= 5_000_000:
        return "$1,000,001 - $5,000,000"
    if t <= 25_000_000:
        return "$5,000,001 - $25,000,000"
    if t <= 50_000_000:
        return "$25,000,001 - $50,000,000"
    return "Over $50,000,000"


# Universe for the refresh — symbols we're likely to see in the screener.
# Starts with the liquid S&P 100-ish list; expand as needed. Large but
# not huge because Finnhub's free tier rate-limits us at ~1 call/sec.
_DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK.B",
    "JPM", "V", "UNH", "XOM", "JNJ", "WMT", "LLY", "PG", "MA", "HD",
    "CVX", "MRK", "ABBV", "PEP", "KO", "COST", "AVGO", "BAC", "TMO",
    "MCD", "CSCO", "WFC", "CRM", "ACN", "ABT", "DHR", "ADBE", "TXN",
    "NFLX", "NEE", "NKE", "AMD", "PM", "RTX", "INTC", "HON", "UPS",
    "UNP", "QCOM", "T", "LIN", "LOW", "MS", "SBUX", "IBM", "ORCL",
    "CAT", "GS", "AMGN", "BLK", "MDT", "DE", "INTU", "ELV", "BMY",
    "GE", "AXP", "LMT", "NOW", "PLD", "ISRG", "BKNG", "SYK", "SCHW",
    "C", "CVS", "ADI", "VRTX", "ADP", "MMC", "REGN", "MDLZ", "ZTS",
    "CI", "SPGI", "SO", "DUK", "GILD", "BSX", "EQIX", "MO", "TMUS",
    "SHW", "ICE", "CL", "PYPL", "ETN", "APD", "WM", "AON", "SNPS",
    "CSX", "MMM", "USB", "FDX", "SPY", "QQQ", "IWM", "DIA", "VTI",
    # Heavy-trade small/mid names that show up in politician disclosures
    "PLTR", "SOFI", "NIO", "RIVN", "PATH", "HOOD", "COIN", "DKNG",
    "AFRM", "SNOW", "SHOP", "UBER", "LYFT", "SQ", "ROKU",
]


# ===== Provider: Stock Watcher (free, S3-hosted, both chambers) =====
_STOCK_WATCHER_HOUSE = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/"
    "data/all_transactions.json"
)
_STOCK_WATCHER_SENATE = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/"
    "aggregate/all_transactions.json"
)


def _fetch_stock_watcher(symbols: list[str] | None = None,
                         days: int = 60) -> list[dict]:
    """Pull both House and Senate disclosure datasets from the free
    Stock Watcher S3 buckets, filter to recent BUYs, and normalize the
    shape to match the rest of the module.

    `symbols` is respected if provided — we filter in-memory. If None,
    we keep every row within `days`. The source files are tens of MB
    but a single HTTP GET over S3 is fast and cheap; we re-fetch once
    a day.
    """
    cutoff = date.today() - timedelta(days=days)
    out: list[dict] = []

    def _download(url: str) -> list[dict]:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "AlpacaBot/1.0 (+https://github.com/Kbell0629/alpaca-trading-bot)"
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            _log(f"stock-watcher fetch failed for {url}: {type(e).__name__}: {e}")
            return []

    filter_set = {s.upper() for s in symbols} if symbols else None

    # ----- House -----
    house_rows = _download(_STOCK_WATCHER_HOUSE)
    for r in house_rows:
        try:
            tx_type = (r.get("type") or "").lower()
            if not any(b in tx_type for b in ("purchase", "buy")):
                continue
            sym = (r.get("ticker") or "").upper().strip()
            if not sym or sym == "--":
                continue
            if filter_set and sym not in filter_set:
                continue
            tx_date_str = r.get("transaction_date") or ""
            try:
                tx_date = datetime.fromisoformat(tx_date_str).date()
            except (TypeError, ValueError):
                continue
            if tx_date < cutoff:
                continue
            amount = r.get("amount") or ""
            out.append({
                "symbol": sym,
                "politician": r.get("representative") or "Unknown",
                "chamber": "house",
                "position": "U.S. Representative",
                "transaction_date": tx_date.isoformat(),
                "filing_date": r.get("disclosure_date") or "",
                "amount_from": None,
                "amount_to": None,
                "amount_label": _normalize_amount_label(amount),
                "asset_type": (r.get("asset_description") or "stock").lower(),
            })
        except Exception:
            continue

    # ----- Senate -----
    senate_rows = _download(_STOCK_WATCHER_SENATE)
    for r in senate_rows:
        try:
            tx_type = (r.get("type") or "").lower()
            if not any(b in tx_type for b in ("purchase", "buy")):
                continue
            sym = (r.get("ticker") or "").upper().strip()
            if not sym or sym == "--":
                continue
            if filter_set and sym not in filter_set:
                continue
            tx_date_str = r.get("transaction_date") or ""
            try:
                # Senate dataset uses MM/DD/YYYY rather than ISO.
                try:
                    tx_date = datetime.strptime(tx_date_str, "%m/%d/%Y").date()
                except ValueError:
                    tx_date = datetime.fromisoformat(tx_date_str).date()
            except (TypeError, ValueError):
                continue
            if tx_date < cutoff:
                continue
            amount = r.get("amount") or ""
            out.append({
                "symbol": sym,
                "politician": r.get("senator") or "Unknown",
                "chamber": "senate",
                "position": "U.S. Senator",
                "transaction_date": tx_date.isoformat(),
                "filing_date": r.get("disclosure_date") or "",
                "amount_from": None,
                "amount_to": None,
                "amount_label": _normalize_amount_label(amount),
                "asset_type": (r.get("asset_type") or "stock").lower(),
            })
        except Exception:
            continue

    return out


# Stock Watcher uses slightly different label strings than the canonical
# disclosure-form buckets. Normalize to our internal labels so the
# AMOUNT_BUCKETS scorer doesn't need per-source branches.
def _normalize_amount_label(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip().lower().replace(",", "").replace("$", "")
    # Patterns seen in the wild:
    #   "$1,001 - $15,000", "$1,001 -"  "1001 - 15000", "50001 - 100000"
    import re
    m = re.search(r"(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)", raw)
    if m:
        try:
            to_v = int(m.group(2).replace(",", ""))
            return _amount_label(None, to_v)
        except ValueError:
            pass
    # Fallback: "Over $50,000,000" style
    if "over" in s and "50" in s:
        return "Over $50,000,000"
    return raw.strip()


# ===== Provider: Quiver Quant (free tier, both chambers) =====
def _fetch_quiver(symbols: list[str] | None = None,
                  days: int = 60) -> list[dict]:
    """Pull live congressional trades from Quiver Quant.

    Endpoint returns all recent trades (no per-symbol filtering on the
    free tier), so we filter in memory. Quiver aggregates both House
    and Senate in a single response and tags each row with `Chamber`.
    """
    key = os.environ.get("QUIVER_API_KEY", "").strip()
    if not key:
        _log("QUIVER_API_KEY not set — returning empty signal set")
        return []

    url = "https://api.quiverquant.com/beta/live/congresstrading"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "AlpacaBot/1.0 (+https://github.com/Kbell0629/alpaca-trading-bot)",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        _log(f"quiver HTTPError {e.code}: {e.reason}")
        return []
    except Exception as e:
        _log(f"quiver fetch failed: {type(e).__name__}: {e}")
        return []

    if not isinstance(rows, list):
        _log(f"quiver returned non-list payload: {type(rows).__name__}")
        return []

    cutoff = date.today() - timedelta(days=days)
    filter_set = {s.upper() for s in symbols} if symbols else None
    out: list[dict] = []

    for r in rows:
        try:
            tx_type = (r.get("Transaction") or "").lower()
            if not any(b in tx_type for b in ("purchase", "buy")):
                continue
            sym = (r.get("Ticker") or "").upper().strip()
            if not sym or sym == "--":
                continue
            if filter_set and sym not in filter_set:
                continue
            tx_date_str = r.get("TransactionDate") or r.get("Date") or ""
            try:
                tx_date = datetime.fromisoformat(tx_date_str.split("T")[0]).date()
            except (TypeError, ValueError, AttributeError):
                continue
            if tx_date < cutoff:
                continue
            # Quiver's chamber is in `House` or `Senator` field depending
            # on the row; best we can do is infer from presence of fields.
            if r.get("Chamber"):
                chamber = r["Chamber"].lower()
            elif r.get("Senator"):
                chamber = "senate"
            elif r.get("Representative") or r.get("House"):
                chamber = "house"
            else:
                chamber = "house"
            politician = (r.get("Representative") or r.get("Senator")
                          or r.get("Name") or "Unknown")
            range_s = r.get("Range") or r.get("Amount") or ""
            out.append({
                "symbol": sym,
                "politician": politician,
                "chamber": chamber,
                "position": ("U.S. Senator" if chamber == "senate"
                             else "U.S. Representative"),
                "transaction_date": tx_date.isoformat(),
                "filing_date": (r.get("ReportDate") or r.get("Filed") or ""),
                "amount_from": None,
                "amount_to": None,
                "amount_label": _normalize_amount_label(range_s),
                "asset_type": (r.get("AssetType") or "stock").lower(),
            })
        except Exception:
            continue

    return out


# ===== Provider: Financial Modeling Prep (FMP) =====
def _fetch_fmp(symbols: list[str] | None = None,
               days: int = 60) -> list[dict]:
    """Pull congressional trades from FMP's RSS-feed endpoints (cheap,
    returns ALL recent trades paginated — no per-symbol fan-out needed).

    We fetch Senate + House separately, each paginated until we drop
    below the `days` cutoff. On FMP's free tier (250 req/day) this is
    well within budget: ~3-5 pages per chamber per nightly refresh.
    """
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        _log("FMP_API_KEY not set — returning empty signal set")
        return []

    cutoff = date.today() - timedelta(days=days)
    filter_set = {s.upper() for s in symbols} if symbols else None
    out: list[dict] = []
    max_pages = 10  # safety valve — 100 trades/page * 10 = 1000 recent trades

    def _pull(chamber_endpoint: str, chamber_label: str, politician_key: str,
              office_label: str):
        """Paginate one chamber's RSS feed until we fall off `cutoff`."""
        for page in range(max_pages):
            url = (f"https://financialmodelingprep.com/api/v4/"
                   f"{chamber_endpoint}?page={page}&apikey="
                   f"{urllib.parse.quote(key)}")
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "AlpacaBot/1.0 (+https://github.com/Kbell0629/alpaca-trading-bot)"
                })
                with urllib.request.urlopen(req, timeout=20) as resp:
                    rows = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                _log(f"fmp {chamber_label} HTTPError {e.code}: {e.reason}")
                return
            except Exception as e:
                _log(f"fmp {chamber_label} fetch failed: {type(e).__name__}: {e}")
                return

            if not isinstance(rows, list) or not rows:
                return  # empty page = end of data

            oldest_on_page = None
            for r in rows:
                try:
                    tx_type = (r.get("type") or "").lower()
                    if not any(b in tx_type for b in ("purchase", "buy")):
                        continue
                    sym = (r.get("symbol") or r.get("ticker") or "").upper().strip()
                    if not sym or sym == "--":
                        continue
                    if filter_set and sym not in filter_set:
                        # Still track date for cutoff logic below
                        pass
                    tx_str = (r.get("transactionDate") or r.get("dateReceived")
                              or r.get("date") or "")
                    try:
                        tx_date = datetime.fromisoformat(
                            tx_str.split("T")[0]).date()
                    except (TypeError, ValueError, AttributeError):
                        try:
                            tx_date = datetime.strptime(tx_str, "%m/%d/%Y").date()
                        except Exception:
                            continue
                    if oldest_on_page is None or tx_date < oldest_on_page:
                        oldest_on_page = tx_date
                    if tx_date < cutoff:
                        continue
                    if filter_set and sym not in filter_set:
                        continue
                    amount = r.get("amount") or ""
                    politician = (r.get(politician_key) or r.get("representative")
                                  or r.get("senator") or r.get("firstName", "")
                                  + " " + r.get("lastName", "")).strip()
                    out.append({
                        "symbol": sym,
                        "politician": politician or "Unknown",
                        "chamber": chamber_label,
                        "position": office_label,
                        "transaction_date": tx_date.isoformat(),
                        "filing_date": (r.get("disclosureDate")
                                        or r.get("dateReceived") or ""),
                        "amount_from": None,
                        "amount_to": None,
                        "amount_label": _normalize_amount_label(amount),
                        "asset_type": (r.get("assetDescription")
                                        or r.get("assetType") or "stock").lower(),
                    })
                except Exception:
                    continue

            # Stop paginating once the oldest trade on this page is
            # older than the cutoff — everything beyond is irrelevant.
            if oldest_on_page and oldest_on_page < cutoff:
                return

    _pull("senate-disclosure-rss-feed", "senate", "senator", "U.S. Senator")
    _pull("house-disclosure-rss-feed", "house", "representative",
          "U.S. Representative")
    return out


_providers = {
    "fmp": _fetch_fmp,
    "quiver": _fetch_quiver,
    "stock_watcher": _fetch_stock_watcher,
    "finnhub": _fetch_finnhub,
}


def refresh_cache(symbols: list[str] | None = None, days: int = 60,
                  provider: str | None = None) -> dict:
    """Auto-select the best available provider unless one is explicitly
    named. Preference order: FMP (free tier, both chambers) > Quiver
    (paid) > Finnhub (paid) > Stock Watcher (deprecated).

    Returns an error dict (no cache write) when copy-trading is disabled
    OR no provider key is available. Previously this silently wrote an
    empty cache which made it look like "congress traded nothing today"
    rather than "we didn't ask". A hard-fail here forces the scheduler
    log to show why we have no disclosures, rather than hiding it.
    """
    # Respect the master feature flag. If the scheduler calls us while
    # copy-trading is off, we don't want to hit the free-tier API budget.
    try:
        from update_dashboard import COPY_TRADING_ENABLED
        if not COPY_TRADING_ENABLED:
            return {"error": "COPY_TRADING_ENABLED=False — refresh skipped",
                    "disabled": True, "count": 0}
    except Exception:
        # update_dashboard may fail to import in minimal test contexts.
        # Fall through to the provider check below.
        pass
    has_key = (os.environ.get("FMP_API_KEY") or
               os.environ.get("QUIVER_API_KEY") or
               os.environ.get("FINNHUB_API_KEY"))
    if provider is None and not has_key:
        return {"error": "no provider key set (FMP_API_KEY / QUIVER_API_KEY "
                         "/ FINNHUB_API_KEY) — refresh aborted",
                "disabled": True, "count": 0}
    if provider is None:
        if os.environ.get("FMP_API_KEY"):
            provider = "fmp"
        elif os.environ.get("QUIVER_API_KEY"):
            provider = "quiver"
        elif os.environ.get("FINNHUB_API_KEY"):
            provider = "finnhub"
    """Pull fresh disclosures and atomically rewrite the cache file."""
    fn = _providers.get(provider)
    if not fn:
        return {"error": f"unknown provider: {provider}"}
    rows = fn(symbols=symbols, days=days)
    # Keep only the last RETENTION_DAYS worth of rows (by transaction date).
    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    filtered = []
    for r in rows:
        try:
            tx = datetime.fromisoformat(r.get("transaction_date") or "").date()
            if tx >= cutoff:
                filtered.append(r)
        except (TypeError, ValueError):
            filtered.append(r)  # keep when date is malformed — harmless
    payload = {
        "refreshed_at": now_et().isoformat(),
        "provider": provider,
        "count": len(filtered),
        "rows": filtered,
    }
    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, CACHE_FILE)
    _log(f"refreshed: provider={provider} rows={len(filtered)}")
    return payload


_cache_lock = threading.Lock()
_cache_mtime: float = 0.0
_cache_rows: list[dict] = []


def load_cache() -> list[dict]:
    """Return the cached rows. Cheap: re-reads from disk only when the
    file's mtime has changed since last load.
    """
    global _cache_mtime, _cache_rows
    try:
        st = os.stat(CACHE_FILE)
    except OSError:
        return []
    with _cache_lock:
        if st.st_mtime != _cache_mtime:
            try:
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                _cache_rows = data.get("rows") or []
                _cache_mtime = st.st_mtime
            except (OSError, json.JSONDecodeError) as e:
                _log(f"cache read failed: {e}")
                return _cache_rows
        return _cache_rows


def score_symbol(symbol: str) -> tuple[float, list[dict]]:
    """Return (score, signals) for the symbol based on cached disclosures.

    `signals` lists the disclosures that contributed, so the UI can show
    "Senator X bought $50k-$100k, 5 days ago" next to the pick instead
    of just a numeric score.
    """
    rows = load_cache()
    if not rows:
        return 0.0, []

    sym = symbol.upper()
    today = date.today()
    signals = []
    score = 0.0

    for row in rows:
        if (row.get("symbol") or "").upper() != sym:
            continue
        # Amount contribution
        label = row.get("amount_label") or _amount_label(
            row.get("amount_from"), row.get("amount_to")
        )
        base = 0.0
        for l, s in AMOUNT_BUCKETS:
            if l == label:
                base = s
                break
        # Chamber multiplier
        chamber = (row.get("chamber") or "house").lower()
        base *= CHAMBER_WEIGHTS.get(chamber, 1.0)
        # Recency decay — full credit if within 14 days, linearly fading
        # to zero at 60 days. A 90-day-old trade in the cache contributes
        # nothing so we don't trade on stale signals.
        try:
            tx = datetime.fromisoformat(row.get("transaction_date") or "").date()
            age = (today - tx).days
        except (TypeError, ValueError):
            age = 30  # mid-range if unparseable
        if age <= 14:
            decay = 1.0
        elif age >= 60:
            decay = 0.0
        else:
            decay = (60 - age) / (60 - 14)
        contribution = base * decay
        if contribution <= 0:
            continue
        score += contribution
        signals.append({
            "politician": row.get("politician"),
            "chamber": chamber,
            "position": row.get("position"),
            "transaction_date": row.get("transaction_date"),
            "amount_label": label,
            "age_days": age,
            "contribution": round(contribution, 2),
        })

    # Sort signals by recency (freshest first) for display.
    signals.sort(key=lambda s: s.get("age_days") or 999)
    return round(score, 2), signals


def summarize_signal(signals: list[dict]) -> str:
    """One-line human-readable summary for use in notifications / emails /
    screener tooltips.
    """
    if not signals:
        return ""
    s = signals[0]
    who = s.get("politician") or "Unknown"
    chamber = (s.get("chamber") or "").title()
    amt = s.get("amount_label") or ""
    age = s.get("age_days")
    age_s = f"{age}d ago" if isinstance(age, int) else "recent"
    more = f" (+{len(signals) - 1} more)" if len(signals) > 1 else ""
    return f"{who} ({chamber}) bought {amt}, {age_s}{more}"


if __name__ == "__main__":
    # CLI:
    #   python capitol_trades.py refresh           → pull universe, rewrite cache
    #   python capitol_trades.py refresh AAPL MSFT → pull only listed symbols
    #   python capitol_trades.py score AAPL        → show score + signals
    #   python capitol_trades.py dump              → print cache summary
    import sys
    args = sys.argv[1:]
    if not args:
        print("usage: capitol_trades.py {refresh [SYM ...] | score SYM | dump}")
        sys.exit(2)
    cmd = args[0]
    if cmd == "refresh":
        syms = args[1:] or None
        out = refresh_cache(symbols=syms)
        print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=2))
        print(f"first 3 rows:")
        for r in (out.get("rows") or [])[:3]:
            print(" ", r)
    elif cmd == "score" and len(args) >= 2:
        score, signals = score_symbol(args[1])
        print(f"{args[1]} score={score}")
        for s in signals[:10]:
            print(" ", s)
    elif cmd == "dump":
        rows = load_cache()
        print(f"cache rows: {len(rows)}")
        from collections import Counter
        c = Counter((r.get("symbol") or "?") for r in rows)
        for sym, n in c.most_common(10):
            print(f"  {sym}: {n}")
    else:
        print("unknown command")
        sys.exit(2)
