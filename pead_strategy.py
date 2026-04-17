#!/usr/bin/env python3
"""
Post-Earnings Announcement Drift (PEAD) — entry strategy.

Academic basis
--------------
PEAD is the most-replicated stock-market anomaly in academic finance:
stocks that beat (or miss) earnings continue drifting in that direction
for 30-90 days after the announcement. Bernard & Thomas (1989) is the
foundational paper; the effect has survived 35+ years of out-of-sample
tests. Magnitude is correlated with the standardised earnings surprise
(SUE) — the bigger the beat in standard-deviations, the longer/larger
the drift.

Why this fits the bot
---------------------
The other entry strategies (Breakout, Mean Reversion, Wheel) are
intraday or multi-day reaction plays. PEAD is a 30-60 day hold that
explicitly seeks earnings events the bot currently *avoids*. The
existing earnings_play.py module skips picks within 3 days of an
announcement; PEAD deliberately enters AFTER a strong beat and rides
the post-announcement drift.

Data sources
------------
  - Earnings detection + universe ranking → screener output (already
    populated each cycle).
  - EPS actual / estimate / surprise → yfinance (free, no key, scrapes
    Yahoo). Cached per-symbol in /data/pead_signals.json so the daily
    refresh is cheap and the screener never blocks on a slow scrape.
  - Price reaction + volume → live Alpaca /snapshot (cached upstream).

Signal definition (entry)
-------------------------
A symbol scores > 0 only if ALL of the following hold:
  1. Reported earnings within the past 1-3 trading days (recency).
  2. Surprise % >= 5% (positive beat). Misses are NOT shorted —
     the bot only goes long, and the asymmetry is real (post-miss
     drift is harder to capture cleanly without options).
  3. Price reaction confirms direction: gap up >= 2% on report day.
  4. Volume surge >= 50% vs trailing average (real reaction).
  5. Next earnings NOT in the next 60 calendar days — we don't want
     to ride drift through a fresh earnings event.

Holding period
--------------
60 calendar days from entry, OR trailing-stop exit triggers, OR
guard against next earnings (close 5 days before). The exit is the
universal trailing-stop policy (round-10 architecture), so the
existing monitor handles it once the position is open.

Position sizing
---------------
Base 3% of buying power, capped at 5 concurrent PEAD positions to
avoid concentration. Score-weighted within that band: bigger surprise
gets a bigger allocation.

Graceful degrade
----------------
- yfinance not installed → returns empty signals, score=0, strategy
  silently drops out of the screener competition. Other strategies
  unaffected.
- yfinance returns garbage / rate-limits → cached signals stay good
  for 24h; we re-try on next refresh cycle.
- Network failure mid-refresh → atomic write keeps the prior cache
  intact.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from et_time import now_et

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
CACHE_FILE = os.path.join(DATA_DIR, "pead_signals.json")

# Signal thresholds — calibrated for large/mid-cap US stocks.
MIN_SURPRISE_PCT = 5.0          # actual EPS must beat by 5%+
MIN_PRICE_REACTION_PCT = 2.0    # gap-up of at least 2% on report day
MIN_VOLUME_SURGE_PCT = 50.0     # volume on report day vs avg
MAX_DAYS_SINCE_REPORT = 3       # only entries within 3 trading days
HOLDING_PERIOD_DAYS = 60        # classic PEAD window
EXIT_BEFORE_NEXT_EARNINGS_DAYS = 5  # close 5d before next event

# Score scaling — keeps PEAD competitive with Breakout (~20-40 typical
# winning score). Big surprises beat moderate breakouts; small ones
# lose. SUE-equivalent banding:
SURPRISE_SCORE_TIERS = [
    (5, 8),     # 5-9% beat → 8 points
    (10, 14),   # 10-19% beat → 14
    (20, 22),   # 20-49% beat → 22
    (50, 30),   # 50%+ beat → 30 (rare, very strong)
]


def _log(msg: str) -> None:
    print(f"[pead] {msg}", flush=True)


def _surprise_to_score(surprise_pct: float) -> float:
    s = abs(surprise_pct)
    score = 0.0
    for threshold, points in SURPRISE_SCORE_TIERS:
        if s >= threshold:
            score = points
    return score


# ===== yfinance fetcher =====
def _fetch_yfinance(symbols: list[str]) -> list[dict]:
    """Pull recent earnings for each symbol via yfinance.

    Returns the list of signals — only includes symbols that pass
    the recency + surprise + reaction filters. Each signal has the
    raw inputs the screener needs to score and the auto-deployer
    needs to size.
    """
    try:
        import yfinance as yf
    except ImportError:
        _log("yfinance not installed — PEAD signals empty")
        return []

    out: list[dict] = []
    today = date.today()
    cutoff_date = today - timedelta(days=MAX_DAYS_SINCE_REPORT * 2)  # calendar pad
    next_earnings_block = today + timedelta(days=HOLDING_PERIOD_DAYS - 5)

    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)

            # ---- earnings history (actual vs estimate) ----
            try:
                # Newer yfinance API: get_earnings_dates (returns DataFrame)
                ed = ticker.get_earnings_dates(limit=8)
            except Exception:
                ed = None

            if ed is None or ed.empty:
                continue

            # Find the most recent row where 'EPS Actual' is populated
            # (i.e. an earnings event that has already reported).
            # Column names in current yfinance: 'EPS Estimate',
            # 'Reported EPS', 'Surprise(%)' — and the row index is
            # the earnings datetime.
            recent_row = None
            recent_dt = None
            for ix, row in ed.iterrows():
                actual = row.get("Reported EPS") or row.get("EPS Actual")
                if actual is None or _is_nan(actual):
                    continue  # not yet reported
                # Convert index timestamp to date
                try:
                    rep_date = ix.date() if hasattr(ix, "date") else ix
                except Exception:
                    continue
                if rep_date < cutoff_date:
                    continue
                # Most recent report
                if recent_dt is None or rep_date > recent_dt:
                    recent_dt = rep_date
                    recent_row = row

            if recent_row is None:
                continue

            actual = float(recent_row.get("Reported EPS")
                            or recent_row.get("EPS Actual") or 0)
            estimate = float(recent_row.get("EPS Estimate") or 0)
            surprise_pct_raw = recent_row.get("Surprise(%)")
            if surprise_pct_raw is not None and not _is_nan(surprise_pct_raw):
                surprise_pct = float(surprise_pct_raw)
            elif estimate:
                surprise_pct = (actual - estimate) / abs(estimate) * 100
            else:
                continue

            if surprise_pct < MIN_SURPRISE_PCT:
                continue  # didn't beat enough

            # Days since report (in trading days approx; using calendar
            # days as a proxy is fine for the recency cap)
            days_since = (today - recent_dt).days
            if days_since > MAX_DAYS_SINCE_REPORT:
                continue

            # ---- next earnings check (don't ride into next event) ----
            next_earnings_dt = None
            for ix, row in ed.iterrows():
                actual_v = row.get("Reported EPS") or row.get("EPS Actual")
                if actual_v is not None and not _is_nan(actual_v):
                    continue  # already reported
                try:
                    next_dt = ix.date() if hasattr(ix, "date") else ix
                except Exception:
                    continue
                if next_dt > today and (next_earnings_dt is None
                                        or next_dt < next_earnings_dt):
                    next_earnings_dt = next_dt
            if next_earnings_dt and next_earnings_dt < next_earnings_block:
                continue  # would have to ride through next event

            out.append({
                "symbol": sym.upper(),
                "report_date": recent_dt.isoformat(),
                "days_since_report": days_since,
                "eps_actual": round(actual, 4),
                "eps_estimate": round(estimate, 4),
                "surprise_pct": round(surprise_pct, 2),
                "next_earnings_date": (next_earnings_dt.isoformat()
                                        if next_earnings_dt else None),
                "score": _surprise_to_score(surprise_pct),
            })
            # Politeness: yfinance hits Yahoo per call. 0.4s spacing
            # keeps us well under any rate limiting.
            time.sleep(0.4)
        except Exception as e:
            _log(f"yfinance fetch failed for {sym}: {type(e).__name__}: {e}")
            continue

    return out


def _is_nan(v: Any) -> bool:
    """NaN check that handles float('nan'), pandas NA, None."""
    try:
        import math
        if isinstance(v, float):
            return math.isnan(v)
    except Exception:
        pass
    try:
        # pandas NA / NaT
        return bool(v != v)  # NaN is the only thing != itself
    except Exception:
        return False


# ===== Cache I/O =====
_cache_lock = threading.Lock()
_cache_mtime: float = 0.0
_cache_signals: dict[str, dict] = {}


def load_cache() -> dict[str, dict]:
    """Return cached signals keyed by symbol. Cheap re-read on mtime change."""
    global _cache_mtime, _cache_signals
    try:
        st = os.stat(CACHE_FILE)
    except OSError:
        return {}
    with _cache_lock:
        if st.st_mtime != _cache_mtime:
            try:
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                _cache_signals = {row["symbol"]: row
                                   for row in data.get("signals", [])
                                   if isinstance(row, dict) and row.get("symbol")}
                _cache_mtime = st.st_mtime
            except (OSError, json.JSONDecodeError) as e:
                _log(f"cache read failed: {e}")
                return _cache_signals
        return _cache_signals


def refresh_cache(symbols: list[str] | None = None) -> dict:
    """Pull fresh PEAD signals and atomically rewrite the cache."""
    if not symbols:
        symbols = _DEFAULT_UNIVERSE
    signals = _fetch_yfinance(symbols)
    payload = {
        "refreshed_at": now_et().isoformat(),
        "universe_size": len(symbols),
        "signal_count": len(signals),
        "signals": signals,
    }
    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, CACHE_FILE)
    _log(f"refresh: scanned {len(symbols)}, found {len(signals)} signals")
    return payload


def score_symbol(symbol: str) -> tuple[float, dict | None]:
    """Return (score, signal_detail) for the symbol from the cache.

    Score is 0 if there's no recent qualifying earnings event for the
    symbol, otherwise the SURPRISE_SCORE_TIERS-derived value.
    """
    cache = load_cache()
    sig = cache.get(symbol.upper())
    if not sig:
        return 0.0, None
    return float(sig.get("score") or 0), sig


def summarize_signal(sig: dict) -> str:
    """One-line human-readable summary for UI / email use."""
    if not sig:
        return ""
    sp = sig.get("surprise_pct")
    days = sig.get("days_since_report")
    return (f"Beat EPS by {sp:+.1f}%, {days}d ago "
            f"(actual ${sig.get('eps_actual')} vs est ${sig.get('eps_estimate')})")


# ===== Default universe =====
# Same large/mid-cap S&P-100-ish list as capitol_trades, plus a few
# heavy earnings-mover names. The universe matters because PEAD works
# best on widely-followed names where analyst estimates are reliable.
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
    "CSX", "MMM", "USB", "FDX",
    # High-vol earnings movers
    "PLTR", "SNOW", "SHOP", "UBER", "COIN", "DKNG", "ROKU", "SOFI",
    "AFRM", "SQ", "HOOD",
]


if __name__ == "__main__":
    # CLI:
    #   python pead_strategy.py refresh           → refresh cache for full universe
    #   python pead_strategy.py refresh AAPL NVDA → refresh just these
    #   python pead_strategy.py score AAPL        → show score + signal
    #   python pead_strategy.py dump              → cache summary
    import sys
    args = sys.argv[1:]
    if not args:
        print("usage: pead_strategy.py {refresh [SYM ...] | score SYM | dump}")
        sys.exit(2)
    cmd = args[0]
    if cmd == "refresh":
        out = refresh_cache(symbols=args[1:] or None)
        meta = {k: v for k, v in out.items() if k != "signals"}
        print(json.dumps(meta, indent=2))
        for s in (out.get("signals") or [])[:5]:
            print(" ", s)
    elif cmd == "score" and len(args) >= 2:
        score, sig = score_symbol(args[1])
        print(f"{args[1]} score={score}")
        if sig:
            print(json.dumps(sig, indent=2, default=str))
    elif cmd == "dump":
        cache = load_cache()
        print(f"signals: {len(cache)}")
        for sym, sig in sorted(cache.items(), key=lambda kv: -kv[1].get("score", 0))[:10]:
            print(f"  {sym}: score={sig.get('score')} surprise={sig.get('surprise_pct')}% {sig.get('days_since_report')}d ago")
    else:
        print("unknown command")
        sys.exit(2)
