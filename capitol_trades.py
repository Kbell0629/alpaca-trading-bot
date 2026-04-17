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
Default: Finnhub `/stock/congressional-trading` (free tier, both chambers).
Set `FINNHUB_API_KEY` in the environment. Without it, every query returns
zero — copy_trading silently drops out of the screener competition. That
graceful degrade means the architecture can deploy before the key lands.

The module is intentionally provider-agnostic: add a `house-stock-watcher`
or `quiver` provider later by implementing `_fetch_<provider>()` and
wiring it in `_providers`.

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


_providers = {
    "finnhub": _fetch_finnhub,
}


def refresh_cache(symbols: list[str] | None = None, days: int = 60,
                  provider: str = "finnhub") -> dict:
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
