#!/usr/bin/env python3
"""
insider_signals.py — SEC EDGAR Form 4 insider buy detection.

Round-11 expansion (item 7). Cluster insider buying (3+ insiders
buying within 30 days) is a well-documented alpha factor. This
module fetches EDGAR's full-text search RSS for Form 4 filings,
counts buys per ticker, and surfaces a +0..+15 score bonus to
the screener.

Free, no API key required. Polite to SEC by:
  - Setting User-Agent with contact email (required by EDGAR)
  - 1-second sleep between requests (SEC rate limit guidance)
  - 24-hour cache so we hit EDGAR at most 1× per day per symbol

Public API:

    fetch_insider_buys(symbol, days=30) -> dict
        {ticker, buy_count, buyer_count, total_value_usd,
         most_recent_date, has_cluster_buy: bool, raw_filings: list}

    insider_score_bonus(insider_data) -> int
        Convert to 0..15 score bonus. Cluster (3+ buyers) → +10,
        each additional buyer → +1, recent (<7d) → +3.

    enrich_picks_with_insiders(picks, top_n=20) -> picks
        Adds `insider_data` and `insider_bonus` to each pick.
        Boosts breakout + pead scores by the bonus.

CONFIG:
    EDGAR_USER_AGENT — required header. Defaults to
        "alpaca-bot kevinbell@example.com" but Kevin should set his
        own (SEC bans persistent abusers).
"""
from __future__ import annotations
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
import re

try:
    from et_time import now_et
except ImportError:
    def now_et():
        return datetime.now()


EDGAR_USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "alpaca-bot/1.0 contact@example.com"
)


def _cache_dir():
    base = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "insider_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(symbol):
    return os.path.join(_cache_dir(), f"{symbol.upper()}.json")


def _read_cache(symbol, max_age_seconds=86400):
    path = _cache_path(symbol)
    try:
        if not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > max_age_seconds:
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # Matches llm_sentiment._read_cache — narrow catch lets genuine
        # code bugs surface while still treating corrupt-file / missing-
        # file / permission as a cache miss.
        return None


def _write_cache(symbol, data):
    path = _cache_path(symbol)
    tmp = None
    try:
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, default=str)
        os.rename(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        # Silent cache-write failure meant the same EDGAR Form 4 scan
        # would re-run on every refresh, hitting SEC rate limits. Route
        # through observability so we see systematic breakage.
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass
        try:
            from observability import capture_exception
            capture_exception(e, component="insider_signals_cache_write")
        except ImportError:
            pass


_LAST_REQUEST_TIME = 0.0


def _polite_sleep():
    """SEC EDGAR asks for ≤10 req/sec — we go even slower (1/sec)."""
    global _LAST_REQUEST_TIME
    elapsed = time.time() - _LAST_REQUEST_TIME
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _LAST_REQUEST_TIME = time.time()


def _fetch_edgar_form4(symbol, days=30):
    """Hit EDGAR's full-text search for Form 4 filings on this ticker
    in the last `days`. Returns parsed list of {filed_date, filer,
    transaction_type}. Empty on error (caller falls back to neutral)."""
    _polite_sleep()
    end = now_et().date()
    start = end - timedelta(days=days)
    # EDGAR full-text search endpoint
    url = (
        f"https://efts.sec.gov/LATEST/search-index?"
        f"q=%22{urllib.parse.quote(symbol.upper())}%22"
        f"&dateRange=custom"
        f"&startdt={start.isoformat()}"
        f"&enddt={end.isoformat()}"
        f"&forms=4"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": EDGAR_USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return [], str(e)
    hits = (data.get("hits") or {}).get("hits") or []
    parsed = []
    for h in hits:
        src = h.get("_source", {})
        # filer: first display_names entry; date: from `file_date`
        filer = (src.get("display_names") or ["?"])[0]
        filed = src.get("file_date") or src.get("filing_date") or ""
        parsed.append({
            "filer": filer,
            "filed_date": filed,
            "form": src.get("form_type", "4"),
            "accession": h.get("_id", ""),
        })
    return parsed, None


def fetch_insider_buys(symbol, days=30):
    """Returns dict with insider activity summary. 24h cache.

    NOTE: EDGAR's full-text search returns Form 4 hits but doesn't
    distinguish buy vs sell or extract dollar amounts in the search
    layer (that's in the actual filing XML). For a free first pass
    we count filings and assume the heuristic that an insider filing
    Form 4 in the last 30d is interesting signal — clusters of
    multiple insiders especially. Future work: parse the XML for
    real buy/sell + dollar values."""
    cached = _read_cache(symbol)
    if cached is not None:
        return cached
    filings, err = _fetch_edgar_form4(symbol, days)
    if err:
        result = {
            "ticker": symbol.upper(),
            "buy_count": 0,
            "buyer_count": 0,
            # Round-58: `total_value_usd` stays null (not 0) — dashboards
            # were rendering "$0" next to buy_count=12 which read as "$0
            # of insider buying" when the truth was "we count filings but
            # haven't parsed dollar volume yet". See value_parse_status
            # for the real state of the signal.
            "total_value_usd": None,
            "value_parse_status": "not_parsed",
            "most_recent_date": None,
            "has_cluster_buy": False,
            "error": err,
            "raw_filings": [],
        }
    else:
        unique_filers = set(f["filer"] for f in filings if f["filer"] != "?")
        most_recent = max((f["filed_date"] for f in filings if f["filed_date"]),
                           default=None)
        result = {
            "ticker": symbol.upper(),
            "buy_count": len(filings),
            "buyer_count": len(unique_filers),
            "total_value_usd": None,  # Round-58: see note above
            "value_parse_status": "not_parsed",  # enum: not_parsed|parsed|unavailable
            "most_recent_date": most_recent,
            "has_cluster_buy": len(unique_filers) >= 3,
            "raw_filings": filings[:10],  # cap stored to 10
            "computed_at": now_et().isoformat(),
        }
    _write_cache(symbol, result)
    return result


def insider_score_bonus(insider_data):
    """Convert insider activity to 0..15 score bonus.
      cluster (3+ filers in 30d): +10
      additional filer beyond 3:  +1 each (cap +5 → max 15)
      most recent within 7 days:  +3
    Returns int."""
    if not insider_data or insider_data.get("error"):
        return 0
    buyer_count = int(insider_data.get("buyer_count", 0) or 0)
    bonus = 0
    if buyer_count >= 3:
        bonus += 10
    if buyer_count > 3:
        bonus += min(5, buyer_count - 3)
    most_recent = insider_data.get("most_recent_date")
    if most_recent:
        try:
            d = datetime.strptime(most_recent[:10], "%Y-%m-%d").date()
            days_ago = (now_et().date() - d).days
            if days_ago <= 7:
                bonus += 3
        except (ValueError, TypeError):
            pass
    return min(15, bonus)


def enrich_picks_with_insiders(picks, top_n=20):
    """Mutates top-N picks: adds insider_data + insider_bonus + applies
    bonus to breakout_score and pead_score. Skips picks beyond top_n
    to respect EDGAR rate limits."""
    if not picks:
        return picks
    for i, p in enumerate(picks):
        if i >= top_n:
            p["insider_bonus"] = 0
            continue
        sym = p.get("symbol", "").upper()
        if not sym:
            continue
        try:
            data = fetch_insider_buys(sym, days=30)
            p["insider_data"] = data
            bonus = insider_score_bonus(data)
            p["insider_bonus"] = bonus
            for k in ("breakout_score", "pead_score"):
                if k in p and isinstance(p[k], (int, float)):
                    p[k] = round(p[k] + bonus, 2)
        except Exception as e:
            p["insider_bonus"] = 0
            p["insider_error"] = str(e)
    return picks


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"Fetching insider buys for {sym}...")
    r = fetch_insider_buys(sym)
    print(json.dumps(r, indent=2, default=str))
    print(f"Score bonus: {insider_score_bonus(r)}")
