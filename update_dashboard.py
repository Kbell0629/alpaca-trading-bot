#!/usr/bin/env python3
"""
Alpaca Trading Dashboard Generator — Enhanced Stock Screener + Autonomous Trading Dashboard
Fetches ALL tradeable US equities, scores them across 3 strategies with 10 advanced improvements,
and generates an HTML dashboard + JSON data file.

Improvements:
  1. Multi-timeframe Momentum (5d/20d)
  2. Relative Volume (20d avg)
  3. Sector Diversification (max 2 per sector in top 5)
  4. Earnings Date Avoidance (news-based)
  5. Dynamic Position Sizing (volatility-based)
  6. Profit-Taking Ladder
  7. SPY Correlation / Market Regime
  8. News Sentiment
  9. Daily P&L Tracking
 10. Backtesting Engine (trailing stop sim on 30d bars)

Run: python3 update_dashboard.py
"""

import json
import os
import re
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from et_time import now_et


def load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())
load_dotenv()


def safe_save_json(path, data):
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp_path, path)
    except Exception:
        # Narrow from bare except so KeyboardInterrupt / SystemExit
        # propagate instead of hitting the cleanup+re-raise branch.
        try: os.unlink(tmp_path)
        except OSError: pass
        raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent runtime data lives. On Railway, set to a volume mount
# path (e.g. /data). Locally defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
STRATEGIES_DIR = os.path.join(DATA_DIR, "strategies")
# cloud_scheduler.py may pass per-user dashboard paths via env vars to route output
# into a user-specific data directory. Fall back to the shared DATA_DIR otherwise.
DASHBOARD_PATH = os.environ.get("DASHBOARD_HTML_PATH") or os.path.join(DATA_DIR, "dashboard.html")
DATA_JSON_PATH = os.environ.get("DASHBOARD_DATA_PATH") or os.path.join(DATA_DIR, "dashboard_data.json")

API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
DATA_ENDPOINT = os.environ.get("ALPACA_DATA_ENDPOINT", "https://data.alpaca.markets/v2")
NEWS_ENDPOINT = "https://data.alpaca.markets/v1beta1/news"
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "ARCA"}
BATCH_SIZE = 500
MIN_PRICE = 5.0
# Evolution:
#   100k (original)   → too permissive; 0.1-0.3M names dominated top of
#                       list on volatile days, leading to bad paper fills.
#   500k (first pass) → too strict; filtered 9 of 12 live top picks.
#   300k (current)    → middle ground. Filters the worst thin-float
#                       (< 300k daily volume ≈ < $1-2M dollar volume)
#                       but keeps legitimate small-mid-cap movers.
#                       Combined with the volatility soft-cap on breakout
#                       (below), low-liquidity pumps stay out of the top.
MIN_VOLUME = 300_000

# --- Feature 3: Sector Rotation (sector ETFs + stock-to-ETF mapping) ---
SECTOR_ETFS = {
    "XLK": "Technology", "XLV": "Healthcare", "XLF": "Financials",
    "XLE": "Energy", "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
    "XLI": "Industrials", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Communication"
}

STOCK_TO_ETF = {
    "AAPL": "XLK", "MSFT": "XLK", "GOOG": "XLK", "GOOGL": "XLK", "NVDA": "XLK",
    "AMD": "XLK", "INTC": "XLK", "CRM": "XLK", "ORCL": "XLK", "PLTR": "XLK",
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "NKE": "XLY", "MCD": "XLY",
    "WMT": "XLP", "COST": "XLP", "PG": "XLP", "KO": "XLP",
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF", "WFC": "XLF",
    "COIN": "XLF", "SOFI": "XLF", "HOOD": "XLF",
    "JNJ": "XLV", "UNH": "XLV", "PFE": "XLV", "LLY": "XLV", "MRK": "XLV",
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE",
    "BA": "XLI", "CAT": "XLI", "GE": "XLI", "LMT": "XLI", "RTX": "XLI",
    "META": "XLC", "DIS": "XLC", "NFLX": "XLC",
}

# Sector map, profit ladder, sentiment keywords, earnings regex — all in
# constants.py now. Re-exported here because historically code reads
# these from update_dashboard (and server.py still does via the
# run_screener path). Import-time re-export keeps the old call sites
# working without a flag-day rename.
from constants import (
    SECTOR_MAP,
    PROFIT_LADDER,
    POSITIVE_KEYWORDS,
    NEGATIVE_KEYWORDS,
    EARNINGS_PATTERN,
    Q_PATTERN,
)


# --- Economic Calendar & Social Sentiment (free modules) ---
try:
    from economic_calendar import get_market_risk_level
except ImportError:
    def get_market_risk_level():
        return {"risk_level": "normal", "recommendation": "Economic calendar module not found.", "events": [], "high_impact_count": 0, "medium_impact_count": 0}

try:
    from social_sentiment import get_social_sentiment
except ImportError:
    def get_social_sentiment(symbol):
        return {"symbol": symbol, "overall_sentiment": "unknown", "overall_score": 0, "social_volume": 0, "is_trending": False, "sources": [], "strategy_adjustments": {}, "meme_warning": False}

try:
    from options_analysis import get_wheel_recommendation
except ImportError:
    get_wheel_recommendation = None

try:
    from short_strategy import identify_short_candidates
except ImportError:
    identify_short_candidates = None

try:
    from extended_hours import get_trading_session
except ImportError:
    get_trading_session = None

try:
    from earnings_play import score_earnings_plays
except ImportError:
    score_earnings_plays = None

try:
    from news_scanner import scan_post_market_news
except ImportError:
    scan_post_market_news = None

try:
    from options_flow import scan_options_flow
except ImportError:
    scan_options_flow = None


def api_get(url, timeout=15):
    """Make an authenticated GET request to Alpaca API (no retry, legacy)."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def api_get_with_retry(url, max_retries=3, timeout=15):
    """Make an authenticated GET request with retry logic for 429/5xx."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"error": str(e)}


def load_json(path):
    """Load a JSON file, returning None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def fetch_tradeable_symbols():
    """Fetch all tradeable US equity symbols from major exchanges."""
    print("Fetching all tradeable US equity assets...")
    url = f"{API_ENDPOINT}/assets?status=active&asset_class=us_equity"
    data = api_get_with_retry(url, timeout=30)
    if isinstance(data, dict) and "error" in data:
        print(f"  Error fetching assets: {data['error']}")
        return []

    symbols = []
    for asset in data:
        if (
            asset.get("tradable")
            and asset.get("exchange") in MAJOR_EXCHANGES
            and asset.get("status") == "active"
        ):
            symbols.append(asset["symbol"])

    print(f"  Found {len(symbols)} tradeable symbols on {', '.join(MAJOR_EXCHANGES)}")
    return symbols


def fetch_snapshots_batch(symbols):
    """Fetch snapshots for a batch of symbols using the bulk endpoint."""
    symbols_str = ",".join(symbols)
    url = f"{DATA_ENDPOINT}/stocks/snapshots?symbols={urllib.parse.quote(symbols_str)}&feed=iex"
    return api_get_with_retry(url, timeout=20)


def fetch_all_snapshots(symbols):
    """Fetch snapshots for all symbols in batches of BATCH_SIZE (parallelized)."""
    all_snapshots = {}
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    total_batches = len(batches)
    print(f"  Fetching {total_batches} batches in parallel (6 workers)...")

    def fetch_one(idx_batch):
        idx, batch = idx_batch
        result = fetch_snapshots_batch(batch)
        if isinstance(result, dict) and "error" not in result:
            return idx, result, None
        else:
            err = result.get("error", "unknown") if isinstance(result, dict) else "bad response"
            return idx, {}, err

    completed = 0
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_one, (i, b)): i for i, b in enumerate(batches)}
        for future in as_completed(futures):
            try:
                idx, data, err = future.result()
                completed += 1
                if err:
                    print(f"    Batch {idx+1} failed: {err[:80]}")
                else:
                    all_snapshots.update(data)
                if completed % 5 == 0 or completed == total_batches:
                    print(f"    Progress: {completed}/{total_batches} batches ({len(all_snapshots)} symbols)")
            except Exception as e:
                print(f"    Batch error: {e}")

    print(f"  Got snapshot data for {len(all_snapshots)} symbols")
    return all_snapshots


# ---------------------------------------------------------------------------
# Strategy competition
# ---------------------------------------------------------------------------
# Only these strategies compete for best_strategy. Trailing Stop is an EXIT
# policy applied by the auto-deployer and the live monitor — never an entry.
# This keeps the screener competition honest after the round-10
# architecture change ("Option C"): if Trailing Stop ever reappears in a
# scores dict (e.g. regime rotation sets it), it still can't win because
# we pick the max from ENTRY_STRATEGIES only.
# Copy Trading is disabled in 2026 — free providers (Stock Watcher)
# shut down, paid tiers start at $30/mo (Quiver) or $99/mo (Finnhub) and
# their endpoints 403 on free tiers. Infrastructure in capitol_trades.py
# is preserved; flip the flag below to re-enable once a working data
# source is available.
COPY_TRADING_ENABLED = False

# PEAD (Post-Earnings Announcement Drift) — entry strategy that scores
# stocks which just beat earnings and rides the academically-validated
# 30-60 day post-earnings drift. Data via yfinance (free, no key).
# See pead_strategy.py.
PEAD_ENABLED = True

# ENTRY_STRATEGIES is the white-list for the screener competition.
# Compose dynamically so flag flips don't require list-edit churn.
_ENTRY_BASE = ["Breakout", "Mean Reversion", "Wheel Strategy"]
if COPY_TRADING_ENABLED:
    _ENTRY_BASE.append("Copy Trading")
if PEAD_ENABLED:
    _ENTRY_BASE.append("PEAD")
ENTRY_STRATEGIES = tuple(_ENTRY_BASE)


def pick_best_entry_strategy(scores):
    """Round-61 pt.7 — thin compat wrapper. Real implementation lives
    in screener_core.py so pytest-cov can see it."""
    from screener_core import pick_best_entry_strategy as _impl
    return _impl(scores, ENTRY_STRATEGIES)


def _trading_day_fraction_elapsed():
    """Round-61 pt.7 — thin compat wrapper around
    screener_core.trading_day_fraction_elapsed."""
    try:
        from et_time import get_et_time
        now = get_et_time()
    except Exception:
        from datetime import datetime
        now = datetime.now()
    from screener_core import trading_day_fraction_elapsed as _impl
    return _impl(now)


def score_stocks(snapshots):
    """Round-61 pt.7 — thin compat wrapper. Real scoring math is in
    screener_core.score_stocks, fully unit-tested. The local flags
    (COPY_TRADING_ENABLED, PEAD_ENABLED) and SECTOR_MAP are passed
    through as parameters."""
    from screener_core import score_stocks as _impl

    # Build the score-fn callbacks here so the core module doesn't
    # have to import capitol_trades / pead_strategy.
    def _copy_fn(symbol):
        import capitol_trades
        return capitol_trades.score_symbol(symbol)

    def _pead_fn(symbol):
        import pead_strategy
        return pead_strategy.score_symbol(symbol)

    return _impl(
        snapshots,
        entry_strategies=ENTRY_STRATEGIES,
        sector_map=SECTOR_MAP,
        min_price=MIN_PRICE,
        min_volume=MIN_VOLUME,
        copy_trading_enabled=COPY_TRADING_ENABLED,
        pead_enabled=PEAD_ENABLED,
        copy_score_fn=_copy_fn if COPY_TRADING_ENABLED else None,
        pead_score_fn=_pead_fn if PEAD_ENABLED else None,
    )



# ---------------------------------------------------------------------------
# Improvement 1 & 2: Multi-timeframe Momentum + Relative Volume from 20-day bars
# ---------------------------------------------------------------------------

def fetch_historical_bars(symbol, days=20):
    """Fetch daily bars for a symbol over the last N calendar days."""
    end_date = now_et().strftime("%Y-%m-%d")
    start_date = (now_et() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    url = (
        f"{DATA_ENDPOINT}/stocks/{urllib.parse.quote(symbol)}/bars"
        f"?timeframe=1Day&start={start_date}&end={end_date}&limit={days}&feed=iex"
    )
    result = api_get_with_retry(url, timeout=10)
    if isinstance(result, dict) and "bars" in result:
        return result["bars"]
    if isinstance(result, list):
        return result
    return []


def fetch_bars_for_picks(picks, days=20, max_workers=6):
    """Fetch historical bars for multiple picks in parallel. Returns {symbol: bars}."""
    def fetch_one(pick):
        try:
            bars = fetch_historical_bars(pick["symbol"], days)
            return pick["symbol"], bars
        except Exception:
            return pick["symbol"], []

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, p): p for p in picks}
        for future in as_completed(futures):
            try:
                sym, bars = future.result()
                results[sym] = bars
            except Exception:
                pass
    return results


def fetch_bars_for_symbols(symbols, days=20, max_workers=6):
    """Fetch historical bars for a list of symbols in parallel. Returns {symbol: bars}."""
    def fetch_one(sym):
        try:
            return sym, fetch_historical_bars(sym, days)
        except Exception:
            return sym, []

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, s): s for s in symbols}
        for future in as_completed(futures):
            try:
                sym, bars = future.result()
                results[sym] = bars
            except Exception:
                pass
    return results


def enrich_with_momentum(pick, bars):
    """Add momentum and relative volume data from historical bars."""
    if not bars or len(bars) < 5:
        pick["momentum_5d"] = 0.0
        pick["momentum_20d"] = 0.0
        pick["relative_volume"] = 1.0
        return

    closes = [b.get("c", 0) for b in bars]
    volumes = [b.get("v", 0) for b in bars]

    # 5-day momentum
    if len(closes) >= 5 and closes[-5] > 0:
        pick["momentum_5d"] = (closes[-1] / closes[-5] - 1) * 100
    else:
        pick["momentum_5d"] = 0.0

    # 20-day momentum
    if len(closes) >= 20 and closes[-20] > 0:
        pick["momentum_20d"] = (closes[-1] / closes[-20] - 1) * 100
    elif len(closes) >= 2 and closes[0] > 0:
        pick["momentum_20d"] = (closes[-1] / closes[0] - 1) * 100
    else:
        pick["momentum_20d"] = 0.0

    # Relative volume (Improvement 2)
    avg_vol = sum(volumes) / len(volumes) if volumes else 1
    today_vol = pick.get("daily_volume", 0)
    pick["relative_volume"] = today_vol / avg_vol if avg_vol > 0 else 1.0

    # Add momentum to trailing score (Improvement 1)
    pick["trailing_score"] += pick["momentum_20d"] * 0.3 + pick["momentum_5d"] * 0.2

    # Relative volume bonus (Improvement 2)
    if pick["relative_volume"] > 2.0:
        pick["trailing_score"] += 5
        pick["copy_score"] += 5
        pick["wheel_score"] += 5
        pick["mean_reversion_score"] += 3
        pick["breakout_score"] += 5

    # Enrich mean reversion with momentum (big 5d drop = higher MR score)
    if pick["momentum_5d"] < -10:
        pick["mean_reversion_score"] += abs(pick["momentum_5d"]) * 1.5
    elif pick["momentum_5d"] < -5:
        pick["mean_reversion_score"] += abs(pick["momentum_5d"]) * 0.8

    # Enrich breakout with momentum (strong 5d up + high rvol = breakout confirmation)
    if pick["momentum_5d"] > 5 and pick["relative_volume"] > 1.5:
        pick["breakout_score"] += pick["momentum_5d"] * 1.0 + (pick["relative_volume"] - 1) * 10

    # Feature 7: Volume Profile Breakouts -- apply volume quality multiplier
    # using ACTUAL 20-day relative volume (not snapshot approximation).
    # Only boost if this is a real breakout candidate (daily_change > 3).
    rvol = pick.get("relative_volume", 1.0)
    daily_change = pick.get("daily_change", 0)
    if pick.get("breakout_score", 0) > 0 and daily_change > 3:
        if rvol >= 3.0:
            pick["breakout_score"] *= 1.5
            pick["breakout_note"] = "3x_volume_confirmed"
        elif rvol >= 2.0:
            pick["breakout_score"] *= 1.2
            pick["breakout_note"] = "2x_volume_confirmed"
        else:
            pick.setdefault("breakout_note", "standard_breakout")

    # Recalculate best with ALL 5 strategies
    pick["scores"] = {
        "Trailing Stop": pick["trailing_score"],
        "Copy Trading": pick["copy_score"],
        "Wheel Strategy": pick["wheel_score"],
        "Mean Reversion": pick["mean_reversion_score"],
        "Breakout": pick["breakout_score"],
        "PEAD": pick.get("pead_score", 0),
    }
    pick["best_strategy"] = pick_best_entry_strategy(pick["scores"])
    pick["best_score"] = pick["scores"][pick["best_strategy"]]


# ---------------------------------------------------------------------------
# Improvement 7: SPY Correlation / Market Regime
# ---------------------------------------------------------------------------

def fetch_market_regime():
    """Fetch SPY 20-day bars and determine market regime."""
    print("Fetching SPY bars for market regime analysis...")
    bars = fetch_historical_bars("SPY", days=20)
    if not bars or len(bars) < 2:
        print("  Could not fetch SPY bars -- defaulting to neutral regime")
        return {"spy_momentum_20d": 0.0, "market_regime": "neutral"}

    closes = [b.get("c", 0) for b in bars]
    if len(closes) >= 20 and closes[-20] > 0:
        spy_mom = (closes[-1] / closes[-20] - 1) * 100
    elif closes[0] > 0:
        spy_mom = (closes[-1] / closes[0] - 1) * 100
    else:
        spy_mom = 0.0

    # Round-10 audit: add a hysteresis band so SPY hovering near ±5%
    # doesn't flap regime between screener cycles. Entering bull/bear
    # needs a stronger signal than staying in it — neutral-to-bull
    # requires >6%, bull-to-neutral requires <4% (same for bear side).
    # Prior regime read from a small cache file so hysteresis survives
    # restarts; absence → assume neutral.
    _regime_cache = os.path.join(DATA_DIR, "market_regime.json")
    # Round-11: hold an fcntl lock across the read + write so two
    # concurrent update_dashboard runs (scheduler tick + handler
    # Refresh) don't race and lose a regime-transition decision.
    import fcntl as _fcntl_r
    _lock_fd = None
    try:
        _lock_fd = open(_regime_cache + ".lock", "w")
        _fcntl_r.flock(_lock_fd, _fcntl_r.LOCK_EX)
    except Exception:
        _lock_fd = None
    try:
        _prior = (load_json(_regime_cache) or {}).get("market_regime", "neutral")
    except Exception:
        _prior = "neutral"
    if _prior == "bull":
        regime = "bull" if spy_mom > 4 else ("bear" if spy_mom < -5 else "neutral")
    elif _prior == "bear":
        regime = "bear" if spy_mom < -4 else ("bull" if spy_mom > 5 else "neutral")
    else:
        if spy_mom > 6:
            regime = "bull"
        elif spy_mom < -6:
            regime = "bear"
        else:
            regime = "neutral"
    try:
        safe_save_json(_regime_cache, {"market_regime": regime,
                                        "spy_momentum_20d": round(spy_mom, 2),
                                        "updated_at": now_et().isoformat()})
    except Exception:
        pass
    finally:
        if _lock_fd:
            try:
                _fcntl_r.flock(_lock_fd, _fcntl_r.LOCK_UN)
                _lock_fd.close()
            except Exception:
                pass

    print(f"  SPY 20d momentum: {spy_mom:+.1f}% -- Market regime: {regime} (prior: {_prior})")
    return {"spy_momentum_20d": round(spy_mom, 2), "market_regime": regime}


def apply_market_regime(picks, regime):
    """Adjust scores based on market regime (Improvement 7)."""
    if regime == "bull":
        print("  Bull market regime: adjusting scores (+5 trailing, +3 breakout)")
        for p in picks:
            p["trailing_score"] += 5
            p["breakout_score"] += 3
            p["scores"]["Trailing Stop"] = p["trailing_score"]
            p["scores"]["Breakout"] = p["breakout_score"]
            p["best_strategy"] = pick_best_entry_strategy(p["scores"])
            p["best_score"] = p["scores"][p["best_strategy"]]
    elif regime == "bear":
        print("  Bear market regime: adjusting scores (-5 trailing, +3 wheel)")
        for p in picks:
            p["trailing_score"] -= 5
            p["wheel_score"] += 3
            p["scores"]["Trailing Stop"] = p["trailing_score"]
            p["scores"]["Wheel Strategy"] = p["wheel_score"]
            p["best_strategy"] = pick_best_entry_strategy(p["scores"])
            p["best_score"] = p["scores"][p["best_strategy"]]


# ---------------------------------------------------------------------------
# Feature 1: Dynamic Strategy Rotation by Market Regime
# ---------------------------------------------------------------------------

def apply_strategy_rotation(picks, market_regime, vix_estimate=None):
    """Dynamically weight ENTRY strategies based on market conditions.
    Bull: boost breakout. Reduce mean_reversion.
    Bear: boost mean_reversion and short_sell. Reduce breakout.
    Neutral/choppy: boost wheel (premium income).

    Trailing Stop is NOT weighted here — it's an exit policy, not an
    entry strategy (round-10 architecture change). Keeping
    `trailing_score` as a dimmed reference on each pick, but its value
    never influences `best_strategy`.
    """
    regime_weights = {
        'bull': {
            'breakout': 1.4,
            'mean_reversion': 0.6,
            'copy_trading': 1.1,   # dormant — kept for when COPY_TRADING_ENABLED flips
            'wheel': 0.8,
            'short_sell': 0.3,  # Almost never short in bull
            'pead': 1.2,  # Beats drift longer in bull markets
        },
        'neutral': {
            'breakout': 1.0,
            'mean_reversion': 1.0,
            'copy_trading': 1.0,
            'wheel': 1.3,  # Range-bound = premium
            'short_sell': 0.7,
            'pead': 1.0,  # Standard PEAD — academic baseline
        },
        'bear': {
            'breakout': 0.5,  # False breakouts
            'mean_reversion': 1.2,  # Oversold bounces
            'copy_trading': 0.9,
            'wheel': 1.4,  # High vol = fat premiums
            'short_sell': 1.5,  # Bear market shorts
            'pead': 0.8,  # Beats get sold into in bear markets
        }
    }
    weights = regime_weights.get(market_regime, regime_weights['neutral'])
    for pick in picks:
        pick['breakout_score'] = pick.get('breakout_score', 0) * weights['breakout']
        pick['mean_reversion_score'] = pick.get('mean_reversion_score', 0) * weights['mean_reversion']
        if COPY_TRADING_ENABLED:
            pick['copy_score'] = pick.get('copy_score', 0) * weights['copy_trading']
        else:
            pick['copy_score'] = 0
        pick['wheel_score'] = pick.get('wheel_score', 0) * weights['wheel']
        if PEAD_ENABLED:
            pick['pead_score'] = pick.get('pead_score', 0) * weights.get('pead', 1.0)
        else:
            pick['pead_score'] = 0
        # Trailing Stop intentionally NOT weighted — it's an exit policy.
        scores = {
            'Trailing Stop': 0,
            'Copy Trading': pick.get('copy_score', 0),
            'Wheel Strategy': pick.get('wheel_score', 0),
            'Mean Reversion': pick.get('mean_reversion_score', 0),
            'Breakout': pick.get('breakout_score', 0),
            'PEAD': pick.get('pead_score', 0),
        }
        pick['scores'] = scores
        pick['best_strategy'] = pick_best_entry_strategy(scores)
        pick['best_score'] = scores[pick['best_strategy']]
        pick['regime_weights_applied'] = weights
    return picks, weights


# ---------------------------------------------------------------------------
# Feature 3: Sector Rotation Signal
# ---------------------------------------------------------------------------

_sector_cache = {"data": None, "timestamp": 0}
SECTOR_CACHE_TTL = 3600  # 1 hour


def calculate_sector_rotation():
    """Fetch sector ETF 20-day performance and rank vs SPY (parallel + 1h cached)."""
    now = time.time()
    if _sector_cache["data"] and (now - _sector_cache["timestamp"] < SECTOR_CACHE_TTL):
        print("  Using cached sector rotation (age: {:.0f}s)".format(now - _sector_cache["timestamp"]))
        return _sector_cache["data"]

    results = {}
    # Fetch SPY + all sector ETF bars in parallel
    all_symbols = ["SPY"] + list(SECTOR_ETFS.keys())
    bars_map = fetch_bars_for_symbols(all_symbols, days=20, max_workers=6)

    spy_bars = bars_map.get("SPY", [])
    spy_return = 0
    if spy_bars and len(spy_bars) >= 20:
        spy_return = (spy_bars[-1].get("c", 0) / spy_bars[-20].get("c", 1) - 1) * 100

    for etf, name in SECTOR_ETFS.items():
        bars = bars_map.get(etf, [])
        if bars and len(bars) >= 20:
            etf_return = (bars[-1].get("c", 0) / bars[-20].get("c", 1) - 1) * 100
            relative = etf_return - spy_return
            results[etf] = {
                "name": name,
                "etf_return_20d": round(etf_return, 2),
                "relative_to_spy": round(relative, 2),
                "strength": "strong" if relative > 2 else "weak" if relative < -2 else "neutral"
            }
    result = {"sectors": results, "spy_return_20d": round(spy_return, 2)}
    _sector_cache["data"] = result
    _sector_cache["timestamp"] = now
    return result


def apply_sector_rotation_filter(picks, sector_data):
    """Penalize picks in weak sectors, boost picks in strong sectors."""
    if not sector_data or "sectors" not in sector_data:
        return picks
    for pick in picks:
        etf = STOCK_TO_ETF.get(pick.get("symbol"))
        if etf and etf in sector_data["sectors"]:
            s = sector_data["sectors"][etf]
            strength = s["strength"]
            if strength == "strong":
                # Boost all scores 15%
                for key in ["trailing_score", "breakout_score", "mean_reversion_score", "copy_score", "wheel_score", "pead_score"]:
                    if key in pick:
                        pick[key] *= 1.15
                pick["sector_signal"] = f"Strong sector ({s['name']}, +{s['relative_to_spy']:.1f}% vs SPY)"
            elif strength == "weak":
                # Reduce scores 20%
                for key in ["trailing_score", "breakout_score", "mean_reversion_score", "copy_score", "wheel_score", "pead_score"]:
                    if key in pick:
                        pick[key] *= 0.80
                pick["sector_signal"] = f"Weak sector ({s['name']}, {s['relative_to_spy']:.1f}% vs SPY)"
            pick["sector"] = s["name"]
            pick["sector_etf"] = etf
    return picks


# ---------------------------------------------------------------------------
# Feature 9: Market Breadth Filter
# ---------------------------------------------------------------------------

def calculate_market_breadth(all_snapshots):
    """Calculate market breadth from the snapshot data we already have.
    Returns % of stocks advancing on the day.
    """
    advancing = 0
    declining = 0
    for sym, snap in all_snapshots.items():
        daily = snap.get("dailyBar", {}) or {}
        prev = snap.get("prevDailyBar", {}) or {}
        if daily and prev:
            today_close = daily.get("c", 0)
            prev_close = prev.get("c", 0)
            if today_close > prev_close:
                advancing += 1
            elif today_close < prev_close:
                declining += 1
    total = advancing + declining
    if total == 0:
        return {"breadth_pct": 50, "advancing": 0, "declining": 0, "signal": "unknown"}
    breadth = advancing / total * 100
    signal = "strong" if breadth > 60 else "weak" if breadth < 40 else "neutral"
    return {
        "breadth_pct": round(breadth, 1),
        "advancing": advancing,
        "declining": declining,
        "signal": signal,
        "note": f"{round(breadth,1)}% of stocks advancing today"
    }


def apply_breadth_filter(picks, breadth_data):
    """Reduce scores if market breadth is weak (divergence warning)."""
    if not breadth_data:
        return picks
    signal = breadth_data.get("signal", "neutral")
    if signal == "weak":
        # Weak breadth = reduce long scores (market is narrow = risky)
        for pick in picks:
            for key in ["trailing_score", "breakout_score", "mean_reversion_score", "wheel_score", "pead_score"]:
                if key in pick:
                    pick[key] *= 0.85  # 15% reduction
            pick["breadth_warning"] = True
    elif signal == "strong":
        # Strong breadth = broad market strength = boost
        for pick in picks:
            for key in ["trailing_score", "breakout_score", "mean_reversion_score", "wheel_score", "pead_score"]:
                if key in pick:
                    pick[key] *= 1.10
    return picks


# ---------------------------------------------------------------------------
# Improvement 4 & 8: Earnings Avoidance + News Sentiment
# ---------------------------------------------------------------------------

def fetch_news_for_symbol(symbol, limit=5):
    """Fetch recent news for a symbol."""
    url = f"{NEWS_ENDPOINT}?symbols={urllib.parse.quote(symbol)}&limit={limit}"
    result = api_get_with_retry(url, timeout=10)
    if isinstance(result, dict) and "news" in result:
        return result["news"]
    if isinstance(result, list):
        return result
    return []


def analyze_news(pick, news_items):
    """Check for earnings warnings and compute sentiment (Improvements 4 & 8)."""
    if not news_items:
        pick["earnings_warning"] = False
        pick["news_sentiment"] = "neutral"
        pick["sentiment_score"] = 0
        return

    headlines_text = " ".join(
        (item.get("headline", "") + " " + item.get("summary", ""))
        for item in news_items
    )

    # Improvement 4: Earnings avoidance (word-boundary matching)
    has_earnings = bool(EARNINGS_PATTERN.search(headlines_text) or Q_PATTERN.search(headlines_text))
    pick["earnings_warning"] = has_earnings
    # Round-10 audit: PEAD wants exactly these stocks (post-earnings
    # drift). Don't penalize other strategies' scores either when PEAD
    # is the likely winner — let PEAD compete on its merits.
    if has_earnings and pick.get("pead_score", 0) <= 0:
        pick["trailing_score"] -= 10
        pick["copy_score"] -= 10
        pick["wheel_score"] -= 10

    # Improvement 8: Sentiment (case-insensitive search on lowered text)
    headlines_lower = headlines_text.lower()
    pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in headlines_lower)
    neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in headlines_lower)
    sentiment_score = pos_count - neg_count

    pick["sentiment_score"] = sentiment_score
    if sentiment_score > 0:
        pick["news_sentiment"] = "positive"
    elif sentiment_score < 0:
        pick["news_sentiment"] = "negative"
    else:
        pick["news_sentiment"] = "neutral"

    # Add sentiment to all scores
    bonus = sentiment_score * 2
    pick["trailing_score"] += bonus
    pick["copy_score"] += bonus
    pick["wheel_score"] += bonus
    pick["mean_reversion_score"] += bonus
    pick["breakout_score"] += bonus

    # Recalculate best with ALL 5 strategies
    pick["scores"] = {
        "Trailing Stop": pick["trailing_score"],
        "Copy Trading": pick["copy_score"],
        "Wheel Strategy": pick["wheel_score"],
        "Mean Reversion": pick["mean_reversion_score"],
        "Breakout": pick["breakout_score"],
        "PEAD": pick.get("pead_score", 0),
    }
    pick["best_strategy"] = pick_best_entry_strategy(pick["scores"])
    pick["best_score"] = pick["scores"][pick["best_strategy"]]


# ---------------------------------------------------------------------------
# Improvement 5: Dynamic Position Sizing
# ---------------------------------------------------------------------------

def calc_position_size(price, volatility, portfolio_value, max_risk_pct=0.02):
    """Size position so max loss (1 ATR move) = max_risk_pct of portfolio.

    Phase 5 of the float->Decimal migration (plan: docs/DECIMAL_MIGRATION_PLAN.md).
    This is THE share-count computation — the value that flows to Alpaca's
    order-placement endpoint. Float drift at the int() boundary can tip the
    integer qty by ±1 share at the rounding edge. Paper-trading users don't
    notice; live-trading users would (wrong qty = wrong capital deployed).

    Compute the two caps in Decimal, int-truncate each independently (same
    as the pre-migration impl — preserves behaviour where "1 more share
    would exceed max_risk" rounds DOWN, not up). Output is plain int.
    """
    if price <= 0 or volatility <= 0 or portfolio_value <= 0:
        return 1
    from decimal import Decimal as _D
    def _d(v):
        return _D(str(v)) if not isinstance(v, _D) else v
    price_d = _d(price)
    vol_d = _d(volatility)
    pv_d = _d(portfolio_value)
    risk_pct_d = _d(max_risk_pct)
    risk_per_share_d = price_d * (vol_d / _d(100))
    max_risk_dollars_d = pv_d * risk_pct_d
    # int() on Decimal does ToIntegralValue with TRUNC semantics, matching
    # the old int(float) behaviour.
    shares = max(1, int(max_risk_dollars_d / risk_per_share_d))
    max_by_value = max(1, int(pv_d * _d("0.10") / price_d))  # max 10% in one stock
    return min(shares, max_by_value)


# ---------------------------------------------------------------------------
# Improvement 3: Sector Diversification
# ---------------------------------------------------------------------------

def apply_sector_diversification(picks, max_per_sector=2, top_n=5):
    """Select top N picks with no more than max_per_sector from same sector."""
    selected = []
    sector_counts = {}
    for p in picks:
        sector = p.get("sector", "Other")
        if sector_counts.get(sector, 0) >= max_per_sector:
            continue
        selected.append(p)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= top_n:
            break
    return selected


# ---------------------------------------------------------------------------
# Improvement 10: Backtesting Engine
# ---------------------------------------------------------------------------

def backtest_trailing_stop(bars, stop_pct=0.10, trail_activation=0.10, trail_distance=0.05):
    """Simulate trailing stop on historical data. Return dict with results including equity curve."""
    if not bars or len(bars) < 3:
        return {"entry": 0, "exit": 0, "return_pct": 0, "stopped_out": False, "days": 0,
                "equity_curve": [], "stop_levels": []}

    # Enter at next day's open to avoid look-ahead bias
    entry = bars[1].get("o", bars[0].get("c", 0))
    if entry <= 0:
        return {"entry": 0, "exit": 0, "return_pct": 0, "stopped_out": False, "days": 0,
                "equity_curve": [], "stop_levels": []}

    highest = entry
    stop_price = entry * (1 - stop_pct)
    trailing_active = False

    equity_curve = [round(entry, 2)]
    stop_levels = [round(stop_price, 2)]

    for i, bar in enumerate(bars[2:], start=2):
        low = bar.get("l", 0)
        high = bar.get("h", 0)
        close = bar.get("c", 0)

        # Check if stopped out — use bar low as worst-case exit
        if low > 0 and low <= stop_price:
            exit_price = bar.get("l", stop_price)  # worst-case exit
            equity_curve.append(round(exit_price, 2))
            stop_levels.append(round(stop_price, 2))
            return {
                "entry": round(entry, 2),
                "exit": round(exit_price, 2),
                "return_pct": round((exit_price / entry - 1) * 100, 2),
                "stopped_out": True,
                "days": i - 1,
                "equity_curve": equity_curve,
                "stop_levels": stop_levels,
            }

        if high > highest:
            highest = high

        if not trailing_active and highest >= entry * (1 + trail_activation):
            trailing_active = True

        if trailing_active:
            new_stop = highest * (1 - trail_distance)
            if new_stop > stop_price:
                stop_price = new_stop

        equity_curve.append(round(close, 2))
        stop_levels.append(round(stop_price, 2))

    final = bars[-1].get("c", 0)
    return {
        "entry": round(entry, 2),
        "exit": round(final, 2),
        "return_pct": round((final / entry - 1) * 100, 2) if entry > 0 else 0,
        "stopped_out": False,
        "days": len(bars) - 2,
        "equity_curve": equity_curve,
        "stop_levels": stop_levels,
    }


# ---------------------------------------------------------------------------
# Improvement 9: Daily P&L Tracking
# ---------------------------------------------------------------------------

def compute_portfolio_pnl(positions, portfolio_value):
    """Compute daily P&L from positions."""
    total_unrealized = 0.0
    for p in positions:
        try:
            total_unrealized += float(p.get("unrealized_intraday_pl", 0))
        except (ValueError, TypeError):
            pass

    daily_pnl_pct = (total_unrealized / portfolio_value * 100) if portfolio_value > 0 else 0.0
    alert_triggered = daily_pnl_pct < -3.0

    return {
        "total_portfolio_pnl": round(total_unrealized, 2),
        "daily_pnl_pct": round(daily_pnl_pct, 2),
        "alert_triggered": alert_triggered,
    }


# ---------------------------------------------------------------------------
# Main data fetch with all improvements
# ---------------------------------------------------------------------------

def fetch_all_data():
    """Fetch all data needed for the dashboard with all 10 improvements."""
    start_time = time.time()

    # Account, positions, orders
    print("Fetching account data...")
    account = api_get_with_retry(f"{API_ENDPOINT}/account")
    positions = api_get_with_retry(f"{API_ENDPOINT}/positions")
    orders = api_get_with_retry(f"{API_ENDPOINT}/orders?status=open&limit=50")

    # Normalize
    account = account if isinstance(account, dict) and "error" not in account else {}
    positions_list = positions if isinstance(positions, list) else []
    orders_list = orders if isinstance(orders, list) else []

    portfolio_value = float(account.get("portfolio_value", 0))

    # Strategy files
    trailing = load_json(os.path.join(STRATEGIES_DIR, "trailing_stop.json"))
    copy_trading = load_json(os.path.join(STRATEGIES_DIR, "copy_trading.json"))
    wheel = load_json(os.path.join(STRATEGIES_DIR, "wheel_strategy.json"))

    # Improvement 7: Market regime (fetch early so we can apply to scores)
    market_info = fetch_market_regime()

    # Full stock screener -- initial fast pass
    print("Running full stock screener...")
    symbols = fetch_tradeable_symbols()
    snapshots = fetch_all_snapshots(symbols) if symbols else {}

    # Feature 9: Market Breadth -- compute from snapshots we already have
    print("Calculating market breadth (advance/decline)...")
    breadth_data = calculate_market_breadth(snapshots)
    print(f"  Breadth: {breadth_data.get('note', 'unknown')} -- signal: {breadth_data.get('signal')}")

    # Feature 3: Sector Rotation -- fetch sector ETF performance
    print("Calculating sector rotation (sector ETFs vs SPY)...")
    sector_data = calculate_sector_rotation()
    if sector_data.get("sectors"):
        strong = [f"{etf}({v['name']}) +{v['relative_to_spy']:.1f}%"
                  for etf, v in sector_data["sectors"].items() if v["strength"] == "strong"]
        weak = [f"{etf}({v['name']}) {v['relative_to_spy']:.1f}%"
                for etf, v in sector_data["sectors"].items() if v["strength"] == "weak"]
        if strong:
            print(f"  Strong sectors: {', '.join(strong)}")
        if weak:
            print(f"  Weak sectors: {', '.join(weak)}")

    print("Scoring stocks (initial pass)...")
    picks = score_stocks(snapshots)
    print(f"  Scored {len(picks)} stocks after filtering (price >= ${MIN_PRICE}, volume >= {MIN_VOLUME:,})")

    # Feature 9: Apply breadth filter to all picks
    print("Applying market breadth filter...")
    apply_breadth_filter(picks, breadth_data)

    # Feature 3: Apply sector rotation filter to all picks
    print("Applying sector rotation filter...")
    apply_sector_rotation_filter(picks, sector_data)

    # Feature 1: Apply dynamic strategy rotation weights by market regime
    # (replaces the old static apply_market_regime additive adjustments)
    print(f"Applying strategy rotation weights for '{market_info['market_regime']}' regime...")
    picks, regime_weights = apply_strategy_rotation(picks, market_info["market_regime"])

    # Re-sort picks after all weighting
    picks.sort(key=lambda x: x["best_score"], reverse=True)

    # --- Economic Calendar: check risk level ONCE (not per stock) ---
    print("Checking economic calendar for market-moving events...")
    econ_risk = get_market_risk_level()
    econ_risk_level = econ_risk["risk_level"]
    print(f"  Economic risk level: {econ_risk_level.upper()} -- {econ_risk['recommendation']}")
    if econ_risk["events"]:
        for ev in econ_risk["events"][:5]:
            print(f"    [{ev['impact'].upper()}] {ev['date']}: {ev['event']}")

    # --- Enrich top 50 candidates (reduced from 100 for speed) ---
    ENRICH_TOP_N = 50
    top_candidates = picks[:ENRICH_TOP_N]
    if top_candidates:
        # Improvement 1 & 2: Fetch 20-day bars for momentum + relative volume (PARALLEL)
        print(f"Enriching top {len(top_candidates)} candidates with 20-day historical bars (parallel)...")
        from indicators import analyze_stock

        # Fetch all bars in parallel first. Need at least 26 trading days
        # for the MACD-26 EMA; fetching 60 calendar days ensures ≥40
        # trading days of history which comfortably covers RSI-14,
        # MACD-12/26/9, and the 20-day momentum / volatility computations
        # below. The previous days=20 meant len(bars_20d) < 26 always
        # short-circuited the technical-indicator branch into neutral
        # defaults (RSI=50, MACD=0, BIAS=neutral) for every pick.
        bars_map = fetch_bars_for_picks(top_candidates, days=60, max_workers=6)
        print(f"  Fetched bars for {len(bars_map)} symbols in parallel")

        # Round-11 Tier 1: ATR-based stop sizing. Attach atr_14 and
        # atr_pct (ATR as % of current price) to every enriched pick.
        # Auto-deployer consumes these to set volatility-sized stops
        # instead of a fixed 10%.
        try:
            from risk_sizing import compute_atr, atr_pct as _atr_pct
        except ImportError:
            compute_atr = None
            _atr_pct = None
        for pick in top_candidates:
            sym = pick["symbol"]
            try:
                bars_20d = bars_map.get(sym, [])
                enrich_with_momentum(pick, bars_20d)

                # Technical Indicators (uses the same bars we already fetched)
                if bars_20d and len(bars_20d) >= 26:
                    tech = analyze_stock(bars_20d)
                    pick["technical"] = tech
                    pick["rsi"] = tech.get("rsi", 50)
                    pick["macd_histogram"] = tech.get("macd_histogram", 0)
                    pick["overall_bias"] = tech.get("overall_bias", "neutral")

                    # Apply indicator score adjustments to all strategies
                    for strat, adj in tech.get("strategy_adjustments", {}).items():
                        key = f"{strat}_score"
                        if key in pick:
                            pick[key] += adj
                else:
                    pick["technical"] = {}
                    pick["rsi"] = 50
                    pick["macd_histogram"] = 0
                    pick["overall_bias"] = "neutral"

                # ATR attachment (used by cloud_scheduler for volatility-aware stops)
                if compute_atr and bars_20d and len(bars_20d) >= 15:
                    try:
                        _price = pick.get("price", 0) or (bars_20d[-1].get("c", 0) if bars_20d else 0)
                        pick["atr_14"] = round(compute_atr(bars_20d, period=14), 4)
                        pick["atr_pct"] = round(_atr_pct(bars_20d, period=14,
                                                          current_price=_price), 4)
                    except Exception:
                        pick["atr_14"] = 0
                        pick["atr_pct"] = 0
                else:
                    pick["atr_14"] = 0
                    pick["atr_pct"] = 0
            except Exception as e:
                print(f"    Error processing bars for {sym}: {e}")
                pick["momentum_5d"] = 0.0
                pick["momentum_20d"] = 0.0
                pick["relative_volume"] = 1.0
                pick["technical"] = {}
                pick["rsi"] = 50
                pick["macd_histogram"] = 0
                pick["overall_bias"] = "neutral"
                pick["atr_14"] = 0
                pick["atr_pct"] = 0

        # Round-11 Tier 1: Relative Strength + Sector Rotation factors.
        # RS: rank each pick's 3m/6m return vs SPY; boost momentum
        # strategies (breakout, pead) by RS score (±25 clamp).
        # Sector Rotation: multiply all entry-strategy scores by the
        # pick's sector-ETF multiplier (0.85 for weak sectors, 1.20
        # for leading sectors). Both factors mutate pick dicts in place;
        # downstream ranking then picks the best strategy organically.
        factor_bars = {}  # declared outside try so Kelly sizing can read it
        try:
            from factor_enrichment import apply_factor_scores
            # Fetch SPY 6-month bars once for RS baseline
            print("Fetching SPY 6-month bars for Relative Strength ranking...")
            spy_long_bars = fetch_historical_bars("SPY", days=130) or []
            print(f"  SPY bars: {len(spy_long_bars)} days")
            # Also need 3m/6m bars for each pick. Fetch now if missing.
            factor_bars = {}
            for p in top_candidates:
                sym = p.get("symbol", "")
                existing = bars_map.get(sym, [])
                if len(existing) >= 100:
                    factor_bars[sym] = existing
            if len(factor_bars) < len(top_candidates):
                # Re-fetch longer bars for top 20 only (full 50 would
                # be too many API calls). RS needs ≥60 days for 3m,
                # ≥120 for 6m. Degrades gracefully if bars come up short.
                print("Fetching extended (6m) bars for top 20 for RS ranking...")
                need_long = [p for p in top_candidates[:20] if p["symbol"] not in factor_bars]
                if need_long:
                    long_bars_map = fetch_bars_for_picks(need_long, days=130, max_workers=6)
                    factor_bars.update(long_bars_map)
                # Picks 21-50 fall back to the 20-day bars; RS will be
                # weaker-signal for those but still better than nothing.
                for p in top_candidates[20:]:
                    sym = p.get("symbol", "")
                    if sym not in factor_bars:
                        factor_bars[sym] = bars_map.get(sym, [])
            apply_factor_scores(top_candidates, spy_long_bars, bars_map=factor_bars,
                                 data_dir=DATA_DIR)
            print("  RS + Sector factors applied")

            # Round-61 pt.42: composite-regime weighting. Layer on top
            # of the 3-bucket apply_strategy_rotation. Compute a
            # 5-tier composite regime from SPY 200-MA + breadth + VIX,
            # then weight per-strategy scores accordingly. Strong-bull
            # boosts breakout/PEAD, suppresses MR/short. Choppy boosts
            # MR/wheel, suppresses breakout. Strong-bear boosts short
            # & wheel, suppresses breakout/PEAD.
            try:
                from screener_core import (
                    compute_composite_regime,
                    apply_regime_weighting,
                    _sma_from_closes,
                )
                # SPY trend: close vs 200-day MA from spy_long_bars
                spy_above_200 = None
                if spy_long_bars and len(spy_long_bars) >= 100:
                    spy_closes = [b.get("c") for b in spy_long_bars
                                   if isinstance(b, dict)]
                    spy_sma = _sma_from_closes(
                        spy_closes,
                        min(200, len(spy_closes) - 1),
                    )
                    spy_last = spy_closes[-1] if spy_closes else None
                    if spy_sma and spy_last:
                        spy_above_200 = float(spy_last) > spy_sma
                # Breadth from market_breadth signal computed earlier
                _breadth_pct = breadth_data.get("breadth_pct") if breadth_data else None
                # VIX: not directly fetched; use the estimate the
                # screener already derives from market_info if present
                _vix = (market_info or {}).get("vix_estimate") or None
                composite = compute_composite_regime(
                    spy_above_200, _breadth_pct, _vix)
                apply_regime_weighting(top_candidates, composite)
                print(f"  Composite regime: {composite} "
                      f"(SPY>200MA={spy_above_200}, "
                      f"breadth={_breadth_pct}, vix={_vix})")
            except Exception as _rw_err:
                print(f"  Regime weighting failed: {_rw_err}. Continuing.")

            # Round-61 pt.39: trend filter. Block LONG entries whose
            # price is below the 50-day SMA (dead-cat bounces inside a
            # downtrend) and SHORT entries above the 50-day SMA
            # (chasing weakness in an uptrend). Uses the same
            # factor_bars we already fetched for RS ranking — no
            # extra API calls.
            try:
                from screener_core import apply_trend_filter
                before = sum(1 for p in top_candidates
                              if p.get("will_deploy") is not False
                              and p.get("best_strategy"))
                apply_trend_filter(top_candidates, bars_map=factor_bars,
                                    period=50)
                after = sum(1 for p in top_candidates
                             if p.get("will_deploy") is not False
                             and p.get("best_strategy"))
                filtered = before - after
                if filtered > 0:
                    print(f"  Trend filter: blocked {filtered} picks "
                          f"(below 50-MA longs / above 50-MA shorts)")
            except Exception as _tf_err:
                print(f"  Trend filter failed: {_tf_err}. Continuing.")

            # Round-61 pt.40: multi-day breakout confirmation. Require
            # today's close AND yesterday's close to BOTH be above their
            # respective 20-day highs before treating a Breakout pick
            # as real. Single-day breakouts get score halved (still
            # rankable but won't be deploy-top). Uses the same
            # factor_bars already fetched for RS ranking — zero extra
            # API calls.
            try:
                from screener_core import apply_breakout_confirmation
                apply_breakout_confirmation(top_candidates,
                                              bars_map=factor_bars,
                                              lookback=20)
                demoted = sum(1 for _p in top_candidates
                               if _p.get("_breakout_unconfirmed"))
                if demoted > 0:
                    print(f"  Breakout confirmation: {demoted} picks "
                          f"demoted (single-day breakouts, score halved)")
            except Exception as _bc_err:
                print(f"  Breakout confirmation failed: {_bc_err}. Continuing.")

            # Round-61 pt.41: adaptive thresholds based on rolling
            # 30-trade win rate per strategy. Cold strategies (win
            # rate < 40%) get scores demoted (×0.70). Hot strategies
            # (> 60%) get scores boosted (×1.30). Self-correcting:
            # when breakout is on a losing streak, the threshold
            # raises and only the strongest signals deploy. When it's
            # winning, the bot deploys more aggressively. Reads the
            # trade journal we already maintain.
            try:
                import json as _json
                from screener_core import (
                    compute_strategy_win_rates,
                    apply_adaptive_thresholds,
                )
                _journal_path = os.environ.get(
                    "JOURNAL_PATH",
                    os.path.join(DATA_DIR, "trade_journal.json"),
                )
                if os.path.exists(_journal_path):
                    with open(_journal_path) as _jf:
                        _journal = _json.load(_jf) or {}
                    _win_rates = compute_strategy_win_rates(_journal,
                                                              lookback=30)
                    if _win_rates:
                        apply_adaptive_thresholds(top_candidates,
                                                    _win_rates)
                        # Log the adjustments for operator visibility
                        for _strat, _stats in _win_rates.items():
                            if _stats["count"] >= 5:
                                print(f"  Adaptive threshold {_strat}: "
                                      f"{_stats['wins']}/{_stats['count']} "
                                      f"({_stats['win_rate']*100:.0f}% win rate)")
            except Exception as _at_err:
                print(f"  Adaptive thresholds failed: {_at_err}. Continuing.")
        except Exception as _factor_err:
            print(f"  Factor enrichment failed: {_factor_err}. Continuing without RS/sector factors.")

        # Improvement 4 & 8: News sentiment + earnings avoidance for top 20 (PARALLEL)
        print("Fetching news for top 20 candidates in parallel (sentiment + earnings check)...")
        news_top = top_candidates[:20]

        def fetch_news_one(pick):
            try:
                items = fetch_news_for_symbol(pick["symbol"], limit=5)
                return pick["symbol"], items, None
            except Exception as e:
                return pick["symbol"], [], str(e)

        news_map = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_news_one, p): p for p in news_top}
            for future in as_completed(futures):
                try:
                    sym, items, err = future.result()
                    news_map[sym] = (items, err)
                except Exception:
                    pass

        for pick in news_top:
            sym = pick["symbol"]
            items, err = news_map.get(sym, ([], None))
            if err:
                print(f"    Error fetching news for {sym}: {err}")
                pick["earnings_warning"] = False
                pick["news_sentiment"] = "neutral"
                pick["sentiment_score"] = 0
                continue
            try:
                analyze_news(pick, items)
                if pick.get("earnings_warning"):
                    print(f"    {sym}: EARNINGS WARNING detected -- score penalized")
                if pick.get("news_sentiment") != "neutral":
                    print(f"    {sym}: Sentiment = {pick['news_sentiment']} (score: {pick['sentiment_score']})")
            except Exception as e:
                print(f"    Error analyzing news for {sym}: {e}")
                pick["earnings_warning"] = False
                pick["news_sentiment"] = "neutral"
                pick["sentiment_score"] = 0

        # Set defaults for candidates 20-N that didn't get news
        for pick in top_candidates[20:]:
            pick.setdefault("earnings_warning", False)
            pick.setdefault("news_sentiment", "neutral")
            pick.setdefault("sentiment_score", 0)

        # Round-11 Tier 2: Fundamental quality + Bullish news boost.
        # Applied to top 30 picks only (yfinance rate limit + cache TTL
        # 24h). Low-quality names (tier D: neg ROE + high debt + neg FCF)
        # get penalized; bullish news catalysts add up to +15.
        try:
            from quality_filter import apply_quality_filter
            # Reuse the news_map built in the news-fetch loop above.
            # news_map keys to (items, err) tuples; extract just items.
            news_map_for_quality = {}
            for _sym, _pair in (news_map or {}).items():
                if isinstance(_pair, tuple) and _pair[0]:
                    news_map_for_quality[_sym] = _pair[0]
                elif isinstance(_pair, list):
                    news_map_for_quality[_sym] = _pair
            print("Applying fundamental quality + bullish news filters...")
            apply_quality_filter(top_candidates, data_dir=DATA_DIR,
                                  news_map=news_map_for_quality, only_top_n=30)
            _tier_counts = {}
            for p in top_candidates[:30]:
                _t = p.get("quality_tier", "?")
                _tier_counts[_t] = _tier_counts.get(_t, 0) + 1
            print(f"  Quality tiers in top 30: {_tier_counts}")
        except Exception as _qerr:
            print(f"  Quality filter failed: {_qerr}. Continuing.")

        # Round-11 expansion item 7: SEC EDGAR insider buys.
        # Cluster insider buying (3+ insiders in 30d) = +10..+15 score
        # bonus on breakout + pead. Cached 24h per symbol; respects
        # SEC's 1-req/sec polite-bot guidance.
        try:
            from insider_signals import enrich_picks_with_insiders
            print("Fetching SEC EDGAR Form 4 insider activity for top 20...")
            enrich_picks_with_insiders(top_candidates, top_n=20)
            _ins_count = sum(1 for p in top_candidates[:20]
                              if (p.get("insider_data") or {}).get("has_cluster_buy"))
            print(f"  {_ins_count} stocks with cluster insider buys (3+ filers in 30d)")
        except Exception as _ie:
            print(f"  Insider signals failed: {_ie}. Continuing.")

        # Round-11 expansion item 8: LLM-powered news sentiment.
        # Re-scores the news items already fetched above using Gemini
        # 1.5 Flash (or whichever provider env var is set). Catches
        # nuance the keyword scanner misses ("lawsuit dismissed" =
        # bullish despite "lawsuit"). Bonus -10..+15 added to score.
        try:
            from llm_sentiment import score_news, _detect_provider
            _provider = _detect_provider()
            if _provider:
                print(f"LLM news scoring via {_provider} for top 15 picks...")
                _llm_count = 0
                for p in top_candidates[:15]:
                    sym = p.get("symbol", "")
                    items = (news_map or {}).get(sym, ([],))[0] or []
                    if not items:
                        continue
                    # Score the most recent headline
                    first = items[0]
                    result = score_news(
                        headline=first.get("headline", ""),
                        summary=first.get("summary", ""),
                        symbol=sym,
                    )
                    p["llm_sentiment_score"] = result.get("score", 0)
                    p["llm_reasoning"] = result.get("reasoning", "")
                    p["llm_provider"] = result.get("provider")
                    # Apply: ±15 bonus to breakout + pead, half on others
                    bonus = result.get("score", 0) * 1.5  # -10..+10 → -15..+15
                    for k in ("breakout_score", "pead_score"):
                        if k in p and isinstance(p[k], (int, float)):
                            p[k] = round(p[k] + bonus, 2)
                    for k in ("mean_reversion_score", "wheel_score"):
                        if k in p and isinstance(p[k], (int, float)):
                            p[k] = round(p[k] + bonus * 0.5, 2)
                    _llm_count += 1
                print(f"  LLM-scored {_llm_count} headlines")
            else:
                print("  LLM sentiment skipped — no provider env var set "
                      "(GEMINI_API_KEY / OPENAI_API_KEY / GROQ_API_KEY)")
        except Exception as _le:
            print(f"  LLM sentiment failed: {_le}. Continuing.")

        # Round-11 expansion item 9: Multi-timeframe confirmation.
        # Fetch 6mo of WEEKLY bars for top 20 breakout/PEAD picks and
        # confirm the daily breakout has weekly trend agreement.
        # +10 bonus when both timeframes bullish; -10 when daily up
        # but weekly down (counter-trend bounce → likely fade).
        try:
            from multi_timeframe import (
                fetch_weekly_bars_for_symbols, enrich_picks_with_mtf
            )
            mtf_top = [p for p in top_candidates[:20]
                        if (p.get("best_strategy") or "").lower() in ("breakout", "pead")]
            if mtf_top:
                print(f"Fetching weekly bars for {len(mtf_top)} breakout/PEAD picks (multi-TF)...")
                weekly_map = fetch_weekly_bars_for_symbols(
                    [p["symbol"] for p in mtf_top]
                )
                enrich_picks_with_mtf(top_candidates, bars_map, weekly_map,
                                       only_breakouts=True, top_n=20)
                _conf = sum(1 for p in mtf_top if p.get("mtf_confirmed"))
                print(f"  Multi-TF confirmed: {_conf}/{len(mtf_top)}")
        except Exception as _me:
            print(f"  Multi-TF confirmation failed: {_me}. Continuing.")

        # Round-11 Tier 2: IV Rank for wheel candidates. Only score up
        # picks whose historical volatility is in the upper half of its
        # 1y range — rich premium is where the wheel's edge lives.
        try:
            from iv_rank import get_hv_rank_for_symbol, iv_rank_score_bonus
            # Only top wheel candidates to stay under yfinance rate limits
            _wheel_sorted = sorted(top_candidates[:30],
                                    key=lambda p: p.get("wheel_score", 0),
                                    reverse=True)[:15]
            print("Computing IV rank (HV proxy) for top 15 wheel candidates...")
            for p in _wheel_sorted:
                try:
                    hv = get_hv_rank_for_symbol(p["symbol"], data_dir=DATA_DIR)
                    p["hv_rank"] = hv.get("hv_rank", 50.0)
                    p["current_hv"] = hv.get("current_hv", 0)
                    bonus = iv_rank_score_bonus(p["hv_rank"])
                    p["iv_rank_bonus"] = bonus
                    if "wheel_score" in p and isinstance(p["wheel_score"], (int, float)):
                        p["wheel_score"] = round(p["wheel_score"] + bonus, 2)
                except Exception:
                    p["hv_rank"] = 50.0
                    p["iv_rank_bonus"] = 0
            # Defaults for wheel candidates we didn't score
            for p in top_candidates:
                p.setdefault("hv_rank", 50.0)
                p.setdefault("iv_rank_bonus", 0)
        except Exception as _iverr:
            print(f"  IV rank enrichment failed: {_iverr}. Continuing.")

        # --- Social Sentiment for top 10 candidates (PARALLEL, 4 workers — StockTwits rate-limited) ---
        print("Fetching social sentiment for top 10 candidates in parallel (4 workers)...")
        social_top = top_candidates[:10]

        def fetch_social_one(pick):
            try:
                return pick["symbol"], get_social_sentiment(pick["symbol"]), None
            except Exception as e:
                return pick["symbol"], None, str(e)

        social_map = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_social_one, p): p for p in social_top}
            for future in as_completed(futures):
                try:
                    sym, social, err = future.result()
                    social_map[sym] = (social, err)
                except Exception:
                    pass

        for pick in social_top:
            sym = pick["symbol"]
            social, err = social_map.get(sym, (None, None))
            if err or social is None:
                if err:
                    print(f"    Error fetching social sentiment for {sym}: {err}")
                pick["social_sentiment"] = "unknown"
                pick["social_score"] = 0
                pick["social_volume"] = 0
                pick["social_trending"] = False
                pick["meme_warning"] = False
                pick["meme_note"] = ""
                continue

            pick["social_sentiment"] = social.get("overall_sentiment", "unknown")
            pick["social_score"] = social.get("overall_score", 0)
            pick["social_volume"] = social.get("social_volume", 0)
            pick["social_trending"] = social.get("is_trending", False)
            pick["meme_warning"] = social.get("meme_warning", False)
            pick["meme_note"] = social.get("meme_note", "")

            # Apply strategy adjustments from social sentiment
            adj = social.get("strategy_adjustments", {})
            if adj:
                pick["trailing_score"] += adj.get("trailing_stop", 0)
                pick["copy_score"] += adj.get("copy_trading", 0)
                pick["wheel_score"] += adj.get("wheel", 0)
                pick["mean_reversion_score"] += adj.get("mean_reversion", 0)
                pick["breakout_score"] += adj.get("breakout", 0)

                # Recalculate best strategy after social adjustments
                pick["scores"] = {
                    "Trailing Stop": pick["trailing_score"],
                    "Copy Trading": pick["copy_score"],
                    "Wheel Strategy": pick["wheel_score"],
                    "Mean Reversion": pick["mean_reversion_score"],
                    "Breakout": pick["breakout_score"],
                    "PEAD": pick.get("pead_score", 0),
                }
                pick["best_strategy"] = pick_best_entry_strategy(pick["scores"])
                pick["best_score"] = pick["scores"][pick["best_strategy"]]

            if social.get("overall_sentiment") != "unknown":
                print(f"    {sym}: social={social['overall_sentiment']} ({social['overall_score']:+.1f}), "
                      f"vol={social['social_volume']}, trending={social['is_trending']}"
                      + (f" MEME WARNING" if social.get("meme_warning") else ""))

        # Set social defaults for candidates 10-N that didn't get social data
        for pick in top_candidates[10:]:
            pick.setdefault("social_sentiment", "unknown")
            pick.setdefault("social_score", 0)
            pick.setdefault("social_volume", 0)
            pick.setdefault("social_trending", False)
            pick.setdefault("meme_warning", False)
            pick.setdefault("meme_note", "")

    # --- Apply learned weights from self-learning engine ---
    # Round-36 fix: learn.py writes to LEARNED_WEIGHTS_PATH (per-user
    # path), but this code was reading from DATA_DIR/learned_weights.json
    # (the shared path). End result: the screener NEVER saw the per-user
    # weekly-learning output. Honor the same env-var override learn.py
    # uses so the read + write paths match.
    _learned_path = os.environ.get(
        "LEARNED_WEIGHTS_PATH",
        os.path.join(DATA_DIR, "learned_weights.json"),
    )
    learned = load_json(_learned_path)
    if learned:
        multipliers = learned.get("strategy_multipliers", {})
        boost_signals = learned.get("boost_signals", [])
        penalty_signals = learned.get("penalty_signals", [])
        confidence = learned.get("confidence", "low")
        print(f"Applying learned weights (confidence: {confidence}, "
              f"{learned.get('total_trades_analyzed', 0)} trades analyzed)...")

        for pick in top_candidates:
            # Apply strategy multipliers
            pick["trailing_score"] *= multipliers.get("trailing_stop", 1.0)
            pick["copy_score"] *= multipliers.get("copy_trading", 1.0)
            pick["wheel_score"] *= multipliers.get("wheel", 1.0)
            pick["mean_reversion_score"] *= multipliers.get("mean_reversion", 1.0)
            pick["breakout_score"] *= multipliers.get("breakout", 1.0)

            # Apply boost/penalty signals
            for signal in boost_signals:
                if signal == "high_momentum" and pick.get("momentum_20d", 0) > 10:
                    pick["trailing_score"] += 3
                    pick["copy_score"] += 3
                    pick["wheel_score"] += 3
                    pick["mean_reversion_score"] += 3
                    pick["breakout_score"] += 3
                elif signal == "high_volume" and pick.get("relative_volume", 1) > 2:
                    pick["trailing_score"] += 3
                    pick["copy_score"] += 3
                    pick["wheel_score"] += 3
                    pick["mean_reversion_score"] += 3
                    pick["breakout_score"] += 3
                elif signal == "positive_sentiment" and pick.get("news_sentiment") == "positive":
                    pick["trailing_score"] += 3
                    pick["copy_score"] += 3
                    pick["wheel_score"] += 3
                    pick["mean_reversion_score"] += 3
                    pick["breakout_score"] += 3
                elif signal == "low_volatility" and pick.get("volatility", 3) < 3:
                    pick["trailing_score"] += 3
                    pick["copy_score"] += 3
                    pick["wheel_score"] += 3
                    pick["mean_reversion_score"] += 3
                    pick["breakout_score"] += 3
                elif signal == "high_volatility" and pick.get("volatility", 3) > 5:
                    pick["trailing_score"] += 3
                    pick["copy_score"] += 3
                    pick["wheel_score"] += 3
                    pick["mean_reversion_score"] += 3
                    pick["breakout_score"] += 3
                elif signal == "bull_market" and market_info.get("market_regime") == "bull":
                    pick["trailing_score"] += 3
                    pick["copy_score"] += 3
                    pick["wheel_score"] += 3
                    pick["mean_reversion_score"] += 3
                    pick["breakout_score"] += 3
                elif signal == "bear_market" and market_info.get("market_regime") == "bear":
                    pick["trailing_score"] += 3
                    pick["copy_score"] += 3
                    pick["wheel_score"] += 3
                    pick["mean_reversion_score"] += 3
                    pick["breakout_score"] += 3

            for signal in penalty_signals:
                if signal == "high_momentum" and pick.get("momentum_20d", 0) > 10:
                    pick["trailing_score"] -= 3
                    pick["copy_score"] -= 3
                    pick["wheel_score"] -= 3
                    pick["mean_reversion_score"] -= 3
                    pick["breakout_score"] -= 3
                elif signal == "high_volume" and pick.get("relative_volume", 1) > 2:
                    pick["trailing_score"] -= 3
                    pick["copy_score"] -= 3
                    pick["wheel_score"] -= 3
                    pick["mean_reversion_score"] -= 3
                    pick["breakout_score"] -= 3
                elif signal == "positive_sentiment" and pick.get("news_sentiment") == "positive":
                    pick["trailing_score"] -= 3
                    pick["copy_score"] -= 3
                    pick["wheel_score"] -= 3
                    pick["mean_reversion_score"] -= 3
                    pick["breakout_score"] -= 3
                elif signal == "low_volatility" and pick.get("volatility", 3) < 3:
                    pick["trailing_score"] -= 3
                    pick["copy_score"] -= 3
                    pick["wheel_score"] -= 3
                    pick["mean_reversion_score"] -= 3
                    pick["breakout_score"] -= 3
                elif signal == "high_volatility" and pick.get("volatility", 3) > 5:
                    pick["trailing_score"] -= 3
                    pick["copy_score"] -= 3
                    pick["wheel_score"] -= 3
                    pick["mean_reversion_score"] -= 3
                    pick["breakout_score"] -= 3
                elif signal == "bull_market" and market_info.get("market_regime") == "bull":
                    pick["trailing_score"] -= 3
                    pick["copy_score"] -= 3
                    pick["wheel_score"] -= 3
                    pick["mean_reversion_score"] -= 3
                    pick["breakout_score"] -= 3
                elif signal == "bear_market" and market_info.get("market_regime") == "bear":
                    pick["trailing_score"] -= 3
                    pick["copy_score"] -= 3
                    pick["wheel_score"] -= 3
                    pick["mean_reversion_score"] -= 3
                    pick["breakout_score"] -= 3

            # Recalculate best strategy after learned weight adjustments
            pick["scores"] = {
                "Trailing Stop": pick["trailing_score"],
                "Copy Trading": pick["copy_score"],
                "Wheel Strategy": pick["wheel_score"],
                "Mean Reversion": pick["mean_reversion_score"],
                "Breakout": pick["breakout_score"],
                "PEAD": pick.get("pead_score", 0),
            }
            pick["best_strategy"] = pick_best_entry_strategy(pick["scores"])
            pick["best_score"] = pick["scores"][pick["best_strategy"]]
    else:
        print("No learned_weights.json found -- skipping learned weight adjustments.")

    # Re-sort top candidates after enrichment
    top_candidates.sort(key=lambda x: x["best_score"], reverse=True)

    # Improvement 10: Backtest top 10 (PARALLEL)
    print("Running backtests on top 10 candidates in parallel (30-day trailing stop sim)...")
    backtest_top = top_candidates[:10]
    bt_bars_map = fetch_bars_for_picks(backtest_top, days=30, max_workers=6)
    for pick in backtest_top:
        sym = pick["symbol"]
        try:
            bars_30d = bt_bars_map.get(sym, [])
            bt = backtest_trailing_stop(bars_30d)
            pick["backtest_return"] = bt["return_pct"]
            pick["backtest_detail"] = bt
            print(f"    {sym}: backtest return = {bt['return_pct']:+.1f}% ({'stopped out' if bt['stopped_out'] else 'held'} over {bt['days']}d)")
        except Exception as e:
            print(f"    Error backtesting {sym}: {e}")
            pick["backtest_return"] = 0.0
            pick["backtest_detail"] = {}

    # Set defaults for candidates without backtest
    for pick in top_candidates[10:]:
        pick.setdefault("backtest_return", None)
        pick.setdefault("backtest_detail", None)

    # Improvement 5: Position sizing for top candidates
    # If economic calendar risk is high/extreme, reduce position sizes by 50%
    econ_size_multiplier = 0.5 if econ_risk_level in ("high", "extreme") else 1.0
    if econ_size_multiplier < 1.0:
        print(f"Calculating position sizes (REDUCED 50% due to {econ_risk_level} economic risk)...")
    else:
        print("Calculating position sizes...")

    # Round-11 Tier 3: Kelly-lite volatility-parity sizing. Scales
    # position size inversely to ATR% so each position risks roughly
    # the same DOLLARS at the stop. Also boosts strong-signal picks
    # (high best_score) and caps everything at 10% of portfolio.
    try:
        from risk_sizing import volatility_position_multiplier
        _vol_parity_available = True
    except ImportError:
        _vol_parity_available = False

    # Rank picks by score to compute the signal-strength multiplier
    _scores_for_rank = sorted(
        (p.get("best_score", 0) or 0 for p in top_candidates),
        reverse=True
    )
    _top_score = max(_scores_for_rank) if _scores_for_rank else 1
    for pick in top_candidates:
        base_shares = calc_position_size(
            pick["price"], pick["volatility"], portfolio_value
        )
        size_shares = base_shares * econ_size_multiplier

        # Volatility-parity: shrink size on high-ATR names
        if _vol_parity_available:
            # Build bars dict for this symbol from the factor_bars or
            # bars_map computed earlier.
            _bars = (factor_bars or {}).get(pick["symbol"]) or bars_map.get(pick["symbol"], [])
            try:
                vol_mult = volatility_position_multiplier(_bars, base_atr_pct=0.02)
            except Exception:
                vol_mult = 1.0
            size_shares *= vol_mult
            pick["vol_parity_mult"] = round(vol_mult, 3)
        else:
            pick["vol_parity_mult"] = 1.0

        # Signal-strength boost: top-score pick gets +20%, bottom -20%
        score = pick.get("best_score", 0) or 0
        if _top_score > 0:
            rel_score = score / _top_score
            # Clamp signal multiplier to [0.8, 1.2]
            signal_mult = 0.8 + min(0.4, max(0.0, rel_score * 0.4))
        else:
            signal_mult = 1.0
        size_shares *= signal_mult
        pick["signal_size_mult"] = round(signal_mult, 3)

        # Hard cap: no single position > 10% of portfolio
        max_shares_by_cap = int((portfolio_value * 0.10) / max(pick["price"], 0.01))
        size_shares = min(size_shares, max_shares_by_cap)
        pick["recommended_shares"] = max(1, int(size_shares))

    # Improvement 6: Add profit ladder to all top candidates
    for pick in top_candidates:
        pick["profit_ladder"] = PROFIT_LADDER

    # Round-58: annotate each enriched pick with `filter_reasons` so the
    # dashboard can hide (or demote) picks the deployer will reject. Before
    # this, picks like MSOS (daily +21%, vol 25%) sat at the top of the
    # list with score 281 even though run_auto_deployer's Gate A / Gate B
    # (cloud_scheduler:2341) would skip them. Source-of-truth is still
    # those gates; this mirrors them so the UI doesn't lie. We also tag
    # will_deploy=False so the dashboard can dim violators.
    #
    # Round-61 pt.45: bridged additional filter reasons into the unified
    # `filter_reasons` list so the screener table can show WHY a high-
    # scored pick was demoted. New reasons:
    #   * `already_held`            — symbol is in the user's open positions
    #   * `below_50ma` / `above_50ma` — pt.39 trend filter (long/short)
    #   * `breakout_unconfirmed`    — pt.40 single-day breakout
    # Existing chase_block + volatility_block tags retained.
    held_symbols = {(p.get("symbol") or "").upper()
                    for p in positions_list if isinstance(p, dict)}
    for p in top_candidates:
        reasons = p.get("filter_reasons") or []
        try:
            _dc = float(p.get("daily_change", 0) or 0)
        except (TypeError, ValueError):
            _dc = 0.0
        try:
            _vol = float(p.get("volatility", 0) or 0)
        except (TypeError, ValueError):
            _vol = 0.0
        _best_strat = (p.get("best_strategy") or "").lower().replace(" ", "_")
        # Map display names to deployer keys
        _strat_key = {
            "breakout": "breakout",
            "pead": "pead",
            "mean_reversion": "mean_reversion",
            "wheel_strategy": "wheel",
            "trailing_stop": "trailing_stop",
            "copy_trading": "copy_trading",
        }.get(_best_strat, _best_strat)
        if _strat_key in ("breakout", "pead") and _dc > 8.0:
            reasons.append(f"chase_block (+{_dc:.1f}% intraday)")
        if _strat_key in ("breakout", "pead") and _vol > 20.0:
            reasons.append(f"volatility_block ({_vol:.1f}% > 20%)")
        # Pt.45: already-held filter. The auto-deployer skips symbols
        # the user already has a position in (avoid double-deploy);
        # surface this in the screener so users see WHY a strong pick
        # didn't appear in the Top 3.
        _sym = (p.get("symbol") or "").upper()
        if _sym and _sym in held_symbols:
            reasons.append("already_held")
        # Pt.45: bridge pt.39 trend-filter tag into filter_reasons.
        # apply_trend_filter sets `_filtered_by_trend` to "below_sma"
        # (long-side filter) or "above_sma" (short-side filter).
        _trend = p.get("_filtered_by_trend")
        if _trend == "below_sma":
            reasons.append("below_50ma")
        elif _trend == "above_sma":
            reasons.append("above_50ma")
        # Pt.45: bridge pt.40 breakout-confirmation tag.
        if p.get("_breakout_unconfirmed"):
            reasons.append("breakout_unconfirmed")
        p["filter_reasons"] = reasons
        p["will_deploy"] = not reasons

    # Improvement 3: Sector-diversified top 5. Round-58: prefer picks
    # that will actually deploy — violators drop to the bottom so the
    # "top 5" reflects what the deployer will pick up tomorrow.
    _deployable = [p for p in top_candidates if p.get("will_deploy")]
    _blocked = [p for p in top_candidates if not p.get("will_deploy")]
    top_candidates = _deployable + _blocked
    diversified_top5 = apply_sector_diversification(top_candidates, max_per_sector=2, top_n=5)
    print(f"  Diversified top 5: {', '.join(p['symbol'] + ' (' + p['sector'] + ')' for p in diversified_top5)}")

    # Merge enriched top candidates back into picks list
    enriched_symbols = {p["symbol"] for p in top_candidates}
    remaining = [p for p in picks if p["symbol"] not in enriched_symbols]
    # Set defaults for non-enriched picks
    for p in remaining:
        p.setdefault("momentum_5d", 0.0)
        p.setdefault("momentum_20d", 0.0)
        p.setdefault("relative_volume", 1.0)
        p.setdefault("earnings_warning", False)
        p.setdefault("news_sentiment", "neutral")
        p.setdefault("sentiment_score", 0)
        p.setdefault("backtest_return", None)
        p.setdefault("backtest_detail", None)
        p.setdefault("recommended_shares", 0)
        p.setdefault("profit_ladder", None)
        p.setdefault("social_sentiment", "unknown")
        p.setdefault("social_score", 0)
        p.setdefault("social_volume", 0)
        p.setdefault("social_trending", False)
        p.setdefault("meme_warning", False)
        p.setdefault("meme_note", "")

    all_picks = top_candidates + remaining

    # Improvement 9: Daily P&L
    pnl_data = compute_portfolio_pnl(positions_list, portfolio_value)
    if pnl_data["alert_triggered"]:
        print(f"  ALERT: Daily loss of {pnl_data['daily_pnl_pct']:.1f}% exceeds -3% threshold!")

    # --- Short Selling Candidates ---
    short_candidates = []
    if identify_short_candidates:
        print("Identifying short selling candidates...")
        short_candidates = identify_short_candidates(top_candidates[:50])
        if short_candidates:
            print(f"  Found {len(short_candidates)} short candidates")
            for sc in short_candidates[:3]:
                print(f"    {sc['symbol']}: score {sc['short_score']}, entry ${sc['price']}, "
                      f"stop ${sc['stop_loss']}, target ${sc['profit_target']} (R:R {sc['risk_reward']})")
        else:
            print("  No short candidates found (market may not be bearish enough)")

    # --- Trading Session ---
    trading_session = get_trading_session() if get_trading_session else "unknown"
    print(f"Current trading session: {trading_session}")

    # --- Options Analysis for top wheel candidate ---
    options_data = None
    if get_wheel_recommendation:
        # Find the top wheel candidate
        wheel_candidates = sorted(top_candidates, key=lambda x: x.get("wheel_score", 0), reverse=True)
        if wheel_candidates:
            top_wheel = wheel_candidates[0]
            print(f"Fetching options data for top wheel candidate: {top_wheel['symbol']}...")
            try:
                options_data = get_wheel_recommendation(top_wheel["symbol"], top_wheel["price"])
                put = options_data.get("put_analysis", {})
                if put.get("best"):
                    b = put["best"]
                    print(f"  Best put: Strike ${b['strike']} exp {b['expiration']} ({b['dte']} DTE, score {b['score']})")
                else:
                    print(f"  No puts found: {put.get('message', 'N/A')}")
            except Exception as e:
                print(f"  Error fetching options data: {e}")
                options_data = None

    # Feature 2: Earnings play candidates
    if score_earnings_plays:
        try:
            earnings_candidates = score_earnings_plays(picks[:50])[:10]
            print(f"Earnings play candidates: {len(earnings_candidates)}")
        except Exception as e:
            print(f"  Earnings play scoring failed: {e}")
            earnings_candidates = []
    else:
        earnings_candidates = []

    # Feature 4: Post-market news scan
    if scan_post_market_news:
        try:
            news_signals = scan_post_market_news(hours_back=12, min_score=8)
            print(f"Post-market news scan: {news_signals.get('actionable_count', 0)} actionable signals "
                  f"from {news_signals.get('total_articles', 0)} articles")
        except Exception as e:
            print(f"  News scan failed: {e}")
            news_signals = {"actionable": [], "error": str(e)}
    else:
        news_signals = {"actionable": []}

    # Feature 5: Options flow for top 20 picks (PARALLELIZED)
    if scan_options_flow:
        try:
            from options_flow import analyze_options_flow as _ofa
            top_syms = [p["symbol"] for p in picks[:20]]

            def _run_one(sym):
                try:
                    return _ofa(sym)
                except Exception:
                    return {"symbol": sym, "signal": "no_data"}

            options_flow_all = []
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {executor.submit(_run_one, s): s for s in top_syms}
                for future in as_completed(futures):
                    try:
                        options_flow_all.append(future.result())
                    except Exception:
                        pass
            options_flow = [a for a in options_flow_all
                            if a.get("signal") in ("bullish", "bearish") and a.get("confidence") != "low"]
            options_flow.sort(key=lambda x: abs(x.get("call_put_ratio", 1) - 1), reverse=True)
            print(f"Options flow: {len(options_flow)} symbols with unusual activity")
        except Exception as e:
            print(f"  Options flow scan failed: {e}")
            options_flow = []
    else:
        options_flow = []

    elapsed = time.time() - start_time
    print(f"Data fetch + enrichment complete in {elapsed:.1f}s")

    return {
        "account": account,
        "positions": positions_list,
        "open_orders": orders_list,
        "trailing": trailing,
        "copy_trading": copy_trading,
        "wheel": wheel,
        "picks": all_picks,
        "diversified_top5": diversified_top5,
        "earnings_candidates": earnings_candidates,
        "news_signals": news_signals,
        "options_flow": options_flow,
        "total_screened": len(snapshots),
        "total_passed": len(all_picks),
        "market_regime": market_info["market_regime"],
        "spy_momentum_20d": market_info["spy_momentum_20d"],
        "market_breadth": breadth_data,
        "sector_rotation": sector_data,
        "regime_weights": regime_weights,
        "economic_calendar": {
            "risk_level": econ_risk_level,
            "recommendation": econ_risk["recommendation"],
            "events": econ_risk["events"],
            "high_impact_count": econ_risk["high_impact_count"],
            "medium_impact_count": econ_risk["medium_impact_count"],
            "position_size_multiplier": econ_size_multiplier,
        },
        "pnl": pnl_data,
        "short_candidates": short_candidates,
        "trading_session": trading_session,
        "options_data": options_data,
        "updated_at": now_et().strftime("%Y-%m-%d %I:%M:%S %p ET"),
    }


def generate_html(data):
    """Generate the full HTML dashboard (backward compatible)."""
    acct = data["account"]
    equity = float(acct.get("equity", 0))
    cash = float(acct.get("cash", 0))
    buying_power = float(acct.get("buying_power", 0))
    portfolio_value = float(acct.get("portfolio_value", 0))
    long_market_value = float(acct.get("long_market_value", 0))

    # ---- Trailing Stop Strategy ----
    ts = data["trailing"] or {}
    ts_state = ts.get("state", {})
    ts_rules = ts.get("rules", {})
    ts_symbol = ts.get("symbol", ts_rules.get("symbol", "N/A"))
    ts_entry = ts_state.get("entry_fill_price") or ts.get("entry_price_estimate", 0)
    ts_shares = ts_state.get("total_shares_held", 0)
    ts_stop = ts_state.get("current_stop_price", "Not set")
    ts_trailing = ts_state.get("trailing_activated", False)
    ts_highest = ts_state.get("highest_price_seen", "N/A")
    trailing_badge = '<span class="badge-active">ACTIVE</span>' if ts_trailing else '<span class="badge-inactive">WAITING</span>'

    # Ladder buys
    ladders = ts_rules.get("ladder_in", [])
    ladder_html = ""
    for l in ladders:
        filled = l.get("order_id") in [f.get("order_id") for f in ts_state.get("ladder_fills", [])]
        status = "Filled" if filled else "Pending"
        status_class = "status-filled" if filled else "status-pending"
        ladder_html += f'<tr><td>-{int(l.get("drop_pct",0)*100)}%</td><td>${l.get("price","N/A")}</td><td>{l.get("qty",0)}</td><td><span class="{status_class}">{status}</span></td><td class="note">{l.get("note","")}</td></tr>'
    if not ladder_html:
        ladder_html = '<tr><td colspan="5" class="empty">No ladder buys configured</td></tr>'

    # ---- Copy Trading ----
    ct = data["copy_trading"] or {}
    ct_state = ct.get("state", {})
    ct_politician = ct_state.get("selected_politician") or "Not selected yet"
    ct_trades = ct_state.get("trades_copied", [])
    ct_pnl = ct_state.get("total_realized_pnl", 0)
    ct_status_badge = '<span class="badge-active">ACTIVE</span>' if ct_trades else '<span class="badge-pending">SETUP NEEDED</span>'

    # ---- Wheel Strategy ----
    ws = data["wheel"] or {}
    ws_state = ws.get("state", {})
    ws_stage = ws_state.get("current_stage", "stage_1_sell_puts")
    ws_stage_label = "Stage 1: Selling Puts" if "stage_1" in ws_stage else "Stage 2: Selling Calls"
    ws_premiums = ws_state.get("total_premiums_collected", 0)
    ws_cycles = ws_state.get("cycles_completed", 0)
    ws_status_badge = '<span class="badge-active">ACTIVE</span>' if ws_cycles > 0 else '<span class="badge-pending">SETUP NEEDED</span>'

    # ---- Open Orders ----
    open_orders_html = ""
    for o in data["open_orders"]:
        side_class = "buy" if o.get("side") == "buy" else "sell"
        price = o.get("limit_price") or o.get("stop_price") or "Market"
        open_orders_html += f'<tr><td>{o.get("symbol","")}</td><td><span class="side-{side_class}">{o.get("side","").upper()}</span></td><td>{o.get("type","")}</td><td>{o.get("qty","")}</td><td>${price}</td><td>{o.get("status","")}</td></tr>'
    if not open_orders_html:
        open_orders_html = '<tr><td colspan="6" class="empty">No open orders</td></tr>'

    # ---- Positions ----
    positions_html = ""
    for p in data["positions"]:
        unrealized = float(p.get("unrealized_pl", 0))
        pc = "positive" if unrealized >= 0 else "negative"
        positions_html += f'<tr><td>{p.get("symbol","")}</td><td>{p.get("qty","")}</td><td>${float(p.get("avg_entry_price",0)):.2f}</td><td>${float(p.get("current_price",0)):.2f}</td><td class="{pc}">${unrealized:.2f}</td><td class="{pc}">{float(p.get("unrealized_plpc",0))*100:.1f}%</td></tr>'
    if not positions_html:
        positions_html = '<tr><td colspan="6" class="empty">No open positions</td></tr>'

    # ---- Top 3 Picks Cards ----
    picks = data.get("picks", [])
    top3 = picks[:3]
    top50 = picks[:50]

    strategy_colors = {"Trailing Stop": "#3b82f6", "Copy Trading": "#10b981", "Wheel Strategy": "#8b5cf6", "Mean Reversion": "#f59e0b", "Breakout": "#ef4444"}
    rank_labels = ["TOP PICK", "RUNNER UP", "STRONG OPTION"]

    picks_cards = ""
    max_score = max((abs(p["best_score"]) for p in picks), default=1)
    if max_score <= 0:
        max_score = 1

    for i, p in enumerate(top3):
        color = strategy_colors.get(p["best_strategy"], "#3b82f6")
        chg_class = "positive" if p["daily_change"] >= 0 else "negative"
        vs_class = "positive" if p["volume_surge"] > 0 else "negative"
        rank = rank_labels[i] if i < 3 else ""

        t_pct = max(5, min(100, abs(p["trailing_score"]) / max_score * 100))
        c_pct = max(5, min(100, abs(p["copy_score"]) / max_score * 100))
        w_pct = max(5, min(100, abs(p["wheel_score"]) / max_score * 100))

        picks_cards += f"""
        <div class="pick-card" style="border-top: 3px solid {color}">
            <div class="pick-header">
                <div>
                    <span class="pick-rank" style="color:{color}">{rank}</span>
                    <div class="pick-symbol">{p['symbol']}</div>
                    <div class="pick-price">${p['price']:,.2f}</div>
                </div>
                <div class="pick-strategy" style="background:{color}20;color:{color}">
                    Deploy: {p['best_strategy']}
                </div>
            </div>
            <div class="pick-stats">
                <div class="pick-stat">
                    <span class="pick-stat-label">Daily Change</span>
                    <span class="pick-stat-value {chg_class}">{p['daily_change']:+.1f}%</span>
                </div>
                <div class="pick-stat">
                    <span class="pick-stat-label">Volatility</span>
                    <span class="pick-stat-value">{p['volatility']:.1f}%</span>
                </div>
                <div class="pick-stat">
                    <span class="pick-stat-label">Volume</span>
                    <span class="pick-stat-value">{p['daily_volume']/1e6:.1f}M</span>
                </div>
                <div class="pick-stat">
                    <span class="pick-stat-label">Vol Surge</span>
                    <span class="pick-stat-value {vs_class}">{p['volume_surge']:+.0f}%</span>
                </div>
            </div>
            <div class="pick-scores">
                <div class="score-row"><span class="score-label" style="color:#3b82f6">Trailing</span><div class="score-bar-bg"><div class="score-bar" style="width:{t_pct}%;background:#3b82f6"></div></div><span class="score-val">{p['trailing_score']:.0f}</span></div>
                <div class="score-row"><span class="score-label" style="color:#10b981">Copy</span><div class="score-bar-bg"><div class="score-bar" style="width:{c_pct}%;background:#10b981"></div></div><span class="score-val">{p['copy_score']:.0f}</span></div>
                <div class="score-row"><span class="score-label" style="color:#8b5cf6">Wheel</span><div class="score-bar-bg"><div class="score-bar" style="width:{w_pct}%;background:#8b5cf6"></div></div><span class="score-val">{p['wheel_score']:.0f}</span></div>
            </div>
        </div>"""

    # ---- Full Screener Table (Top 50) ----
    screener_html = ""
    for i, p in enumerate(top50):
        color = strategy_colors.get(p["best_strategy"], "#3b82f6")
        chg_class = "positive" if p["daily_change"] >= 0 else "negative"
        vs_class = "positive" if p["volume_surge"] > 0 else "negative"
        highlight = ' style="background:rgba(59,130,246,0.05)"' if i < 3 else ""
        screener_html += f'<tr{highlight}><td>{i+1}</td><td><strong>{p["symbol"]}</strong></td><td>${p["price"]:,.2f}</td><td class="{chg_class}">{p["daily_change"]:+.1f}%</td><td>{p["volatility"]:.1f}%</td><td>{p["daily_volume"]/1e6:.1f}M</td><td class="{vs_class}">{p["volume_surge"]:+.0f}%</td><td style="color:{color};font-weight:600">{p["best_strategy"]}</td><td style="font-weight:700">{p["best_score"]:.0f}</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Trading Bot Dashboard</title>
<style>
:root {{
    --bg: #0a0e17; --card: #111827; --border: #1e293b;
    --text: #e2e8f0; --text-dim: #94a3b8; --accent: #3b82f6;
    --green: #10b981; --red: #ef4444; --orange: #f59e0b; --purple: #8b5cf6;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text); padding: 24px; line-height: 1.5;
}}
.header {{
    display:flex; justify-content:space-between; align-items:center;
    margin-bottom:24px; padding-bottom:16px; border-bottom:1px solid var(--border);
}}
.header h1 {{
    font-size:24px; font-weight:700;
    background: linear-gradient(135deg, var(--accent), var(--purple));
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
}}
.header .updated {{ color:var(--text-dim); font-size:13px; }}
.paper-badge {{
    background:var(--orange); color:#000; padding:4px 12px; border-radius:20px;
    font-size:11px; font-weight:700; letter-spacing:1px;
}}
.account-bar {{ display:grid; grid-template-columns:repeat(5,1fr); gap:16px; margin-bottom:24px; }}
.metric {{
    background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px;
}}
.metric .label {{ font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px; }}
.metric .value {{ font-size:22px; font-weight:700; }}

/* Stock Picks */
.section-title {{
    font-size:18px; font-weight:700; margin-bottom:16px;
    display:flex; align-items:center; gap:8px;
}}
.section-title .subtitle {{ font-size:13px; color:var(--text-dim); font-weight:400; }}
.picks {{ display:grid; grid-template-columns:repeat(3,1fr); gap:20px; margin-bottom:24px; }}
.pick-card {{
    background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px;
}}
.pick-header {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:16px; }}
.pick-rank {{ font-size:10px; font-weight:700; letter-spacing:1px; }}
.pick-symbol {{ font-size:28px; font-weight:800; margin-top:2px; }}
.pick-price {{ font-size:16px; color:var(--text-dim); }}
.pick-strategy {{
    padding:6px 12px; border-radius:8px; font-size:11px; font-weight:700;
    text-transform:uppercase; letter-spacing:0.5px;
}}
.pick-stats {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:16px; }}
.pick-stat {{
    display:flex; justify-content:space-between; padding:6px 0;
    border-bottom:1px solid rgba(30,41,59,0.5);
}}
.pick-stat-label {{ font-size:11px; color:var(--text-dim); }}
.pick-stat-value {{ font-size:13px; font-weight:600; }}
.pick-scores {{ display:flex; flex-direction:column; gap:6px; }}
.score-row {{ display:flex; align-items:center; gap:8px; }}
.score-label {{ font-size:10px; font-weight:600; width:50px; text-transform:uppercase; }}
.score-bar-bg {{ flex:1; height:6px; background:rgba(30,41,59,0.8); border-radius:3px; overflow:hidden; }}
.score-bar {{ height:100%; border-radius:3px; transition:width 0.5s; }}
.score-val {{ font-size:11px; color:var(--text-dim); width:30px; text-align:right; }}

/* Strategies */
.strategies {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:20px; margin-bottom:24px; }}
.strategy-card {{
    background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:20px; position:relative; overflow:hidden;
}}
.strategy-card::before {{
    content:''; position:absolute; top:0; left:0; right:0; height:3px;
}}
.strategy-card.trailing::before {{ background:linear-gradient(90deg,var(--accent),#60a5fa); }}
.strategy-card.copy::before {{ background:linear-gradient(90deg,var(--green),#34d399); }}
.strategy-card.wheel::before {{ background:linear-gradient(90deg,var(--purple),#a78bfa); }}
.strategy-card h2 {{ font-size:16px; margin-bottom:4px; }}
.strategy-card .subtitle {{ font-size:12px; color:var(--text-dim); margin-bottom:16px; }}
.stat-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.stat {{ padding:8px 0; }}
.stat .stat-label {{ font-size:10px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; }}
.stat .stat-value {{ font-size:16px; font-weight:600; margin-top:2px; }}
.badge-active {{ background:rgba(16,185,129,0.15); color:var(--green); padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; }}
.badge-inactive {{ background:rgba(245,158,11,0.15); color:var(--orange); padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; }}
.badge-pending {{ background:rgba(148,163,184,0.15); color:var(--text-dim); padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; }}
.strategy-visual {{
    background:rgba(59,130,246,0.05); border:1px solid rgba(59,130,246,0.2);
    border-radius:8px; padding:12px; margin-top:12px; font-size:12px;
}}

/* Tables */
.tables {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:24px; }}
.table-card {{
    background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:20px; overflow-x:auto;
}}
.table-card h3 {{ font-size:14px; margin-bottom:12px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ text-align:left; padding:8px 12px; border-bottom:1px solid var(--border); color:var(--text-dim); font-size:10px; text-transform:uppercase; letter-spacing:0.5px; font-weight:600; }}
td {{ padding:8px 12px; border-bottom:1px solid rgba(30,41,59,0.5); }}
.positive {{ color:var(--green); }}
.negative {{ color:var(--red); }}
.side-buy {{ color:var(--green); font-weight:600; }}
.side-sell {{ color:var(--red); font-weight:600; }}
.status-filled {{ color:var(--green); font-weight:600; }}
.status-pending {{ color:var(--orange); }}
.empty {{ text-align:center; color:var(--text-dim); padding:20px; }}
.note {{ color:var(--text-dim); font-size:11px; }}

.screener {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:24px; }}
.ladder-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:24px; }}
.ladder-card h3, .screener h3 {{ font-size:14px; margin-bottom:12px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; }}

.how-to {{
    background: linear-gradient(135deg, rgba(59,130,246,0.08), rgba(139,92,246,0.08));
    border: 1px solid rgba(59,130,246,0.2); border-radius:12px;
    padding:20px; margin-bottom:24px;
}}
.how-to h3 {{ font-size:14px; margin-bottom:8px; }}
.how-to code {{ background:rgba(59,130,246,0.15); padding:2px 6px; border-radius:4px; font-size:12px; }}
.how-to ul {{ padding-left:20px; font-size:13px; color:var(--text-dim); }}
.how-to li {{ margin-bottom:4px; }}

.screener-stats {{
    display:flex; gap:24px; margin-bottom:16px; font-size:12px; color:var(--text-dim);
}}
.screener-stats span {{ font-weight:600; color:var(--text); }}

.footer {{ text-align:center; padding:16px; color:var(--text-dim); font-size:11px; border-top:1px solid var(--border); }}
@media (max-width:1200px) {{
    .strategies,.picks {{ grid-template-columns:1fr; }}
    .tables {{ grid-template-columns:1fr; }}
    .account-bar {{ grid-template-columns:repeat(3,1fr); }}
}}
</style>
</head>
<body>

<div class="header">
    <div>
        <h1>Stock Trading Bot Dashboard</h1>
        <div class="updated">Last updated: {data['updated_at']}</div>
    </div>
    <span class="paper-badge">PAPER TRADING</span>
</div>

<!-- Account Overview -->
<div class="account-bar">
    <div class="metric"><div class="label">Portfolio Value</div><div class="value">${portfolio_value:,.2f}</div></div>
    <div class="metric"><div class="label">Cash</div><div class="value">${cash:,.2f}</div></div>
    <div class="metric"><div class="label">Buying Power</div><div class="value">${buying_power:,.2f}</div></div>
    <div class="metric"><div class="label">Long Market Value</div><div class="value">${long_market_value:,.2f}</div></div>
    <div class="metric"><div class="label">Equity</div><div class="value">${equity:,.2f}</div></div>
</div>

<!-- How to Use -->
<div class="how-to">
    <h3>How to Use This Dashboard</h3>
    <ul>
        <li>Tell Claude: <code>/stock-bot</code> to launch the bot and see picks</li>
        <li>Or just say: <code>"Run trailing stop on NVDA"</code>, <code>"Start wheel on SOFI"</code>, <code>"Set up copy trading"</code></li>
        <li>Strategies run autonomously once deployed — check back anytime for status</li>
    </ul>
</div>

<!-- Top 3 Picks -->
<div class="section-title">Top 3 Stock Picks <span class="subtitle">-- Screened {data['total_screened']:,} stocks, scored {data['total_passed']:,} after filtering</span></div>
<div class="picks">
    {picks_cards if picks_cards else '<div class="empty">No picks available -- market may be closed</div>'}
</div>

<!-- Active Strategies -->
<div class="section-title">Active Strategies</div>
<div class="strategies">
    <div class="strategy-card trailing">
        <h2>1. Trailing Stop</h2>
        <div class="subtitle">{ts_symbol} -- Auto stop-loss with ratcheting floor</div>
        <div class="stat-grid">
            <div class="stat"><div class="stat-label">Entry Price</div><div class="stat-value">${ts_entry if ts_entry else 'Pending'}</div></div>
            <div class="stat"><div class="stat-label">Shares Held</div><div class="stat-value">{ts_shares}</div></div>
            <div class="stat"><div class="stat-label">Stop Price</div><div class="stat-value">{ts_stop}</div></div>
            <div class="stat"><div class="stat-label">Trailing</div><div class="stat-value">{trailing_badge}</div></div>
            <div class="stat"><div class="stat-label">Highest Seen</div><div class="stat-value">{ts_highest}</div></div>
        </div>
        <div class="strategy-visual"><strong>Rules:</strong> 10% stop-loss | +10% activates trailing | 5% trail distance | Floor only goes up</div>
    </div>
    <div class="strategy-card copy">
        <h2>2. Copy Trading</h2>
        <div class="subtitle">Track &amp; copy politician trades via Capitol Trades</div>
        <div class="stat-grid">
            <div class="stat"><div class="stat-label">Tracking</div><div class="stat-value">{ct_politician}</div></div>
            <div class="stat"><div class="stat-label">Status</div><div class="stat-value">{ct_status_badge}</div></div>
            <div class="stat"><div class="stat-label">Trades Copied</div><div class="stat-value">{len(ct_trades)}</div></div>
            <div class="stat"><div class="stat-label">Realized P&L</div><div class="stat-value">${ct_pnl:,.2f}</div></div>
        </div>
        <div class="strategy-visual"><strong>Rules:</strong> 5% position size | Max 10 positions | Skip if moved 15%+ | 10% stop-loss</div>
    </div>
    <div class="strategy-card wheel">
        <h2>3. Wheel Strategy</h2>
        <div class="subtitle">Sell puts &rarr; Assigned &rarr; Sell calls &rarr; Repeat</div>
        <div class="stat-grid">
            <div class="stat"><div class="stat-label">Current Stage</div><div class="stat-value" style="font-size:13px">{ws_stage_label}</div></div>
            <div class="stat"><div class="stat-label">Status</div><div class="stat-value">{ws_status_badge}</div></div>
            <div class="stat"><div class="stat-label">Premiums</div><div class="stat-value positive">${ws_premiums:,.2f}</div></div>
            <div class="stat"><div class="stat-label">Cycles</div><div class="stat-value">{ws_cycles}</div></div>
        </div>
        <div class="strategy-visual"><strong>Rules:</strong> Strike 10% OTM | 2-4 week exp | Close at 50% profit | Check every 15 min</div>
    </div>
</div>

<!-- Ladder Buys -->
<div class="ladder-card">
    <h3>Trailing Stop -- Ladder Buy Levels</h3>
    <table>
        <thead><tr><th>Drop</th><th>Trigger Price</th><th>Shares</th><th>Status</th><th>Note</th></tr></thead>
        <tbody>{ladder_html}</tbody>
    </table>
</div>

<!-- Orders & Positions -->
<div class="tables">
    <div class="table-card">
        <h3>Open Orders</h3>
        <table>
            <thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>Status</th></tr></thead>
            <tbody>{open_orders_html}</tbody>
        </table>
    </div>
    <div class="table-card">
        <h3>Positions</h3>
        <table>
            <thead><tr><th>Symbol</th><th>Qty</th><th>Avg Entry</th><th>Current</th><th>P&L</th><th>P&L %</th></tr></thead>
            <tbody>{positions_html}</tbody>
        </table>
    </div>
</div>

<!-- Full Screener -->
<div class="screener">
    <h3>Full Stock Screener -- Top 50</h3>
    <div class="screener-stats">
        Screened: <span>{data['total_screened']:,}</span> stocks &nbsp;|&nbsp;
        Passed filters: <span>{data['total_passed']:,}</span> &nbsp;|&nbsp;
        Showing: <span>Top 50</span>
    </div>
    <table>
        <thead><tr><th>#</th><th>Symbol</th><th>Price</th><th>Daily Chg</th><th>Volatility</th><th>Volume</th><th>Vol Surge</th><th>Best Strategy</th><th>Score</th></tr></thead>
        <tbody>{screener_html if screener_html else '<tr><td colspan="9" class="empty">No screener data -- market may be closed</td></tr>'}</tbody>
    </table>
</div>

<div class="footer">
    Stock Trading Bot -- Strategies: Trailing Stop | Copy Trading | Wheel -- Full market screener across NYSE, NASDAQ, ARCA<br>
    Tell Claude: "/stock-bot" to launch | "Run trailing stop on [SYMBOL]" | "Start wheel on [SYMBOL]" | "Set up copy trading"
</div>

</body>
</html>"""
    return html


def save_data_json(data):
    """Save all enhanced data as JSON for the new dashboard server."""
    # Make a JSON-serializable copy
    output = {
        "account": data["account"],
        "positions": data["positions"],
        "open_orders": data["open_orders"],
        "picks": data["picks"][:100],  # Top 100 with full enrichment data
        "diversified_top5": data["diversified_top5"],
        "total_screened": data["total_screened"],
        "total_passed": data["total_passed"],
        "market_regime": data["market_regime"],
        "spy_momentum_20d": data["spy_momentum_20d"],
        "market_breadth": data.get("market_breadth", {}),
        "sector_rotation": data.get("sector_rotation", {}),
        "regime_weights": data.get("regime_weights", {}),
        "economic_calendar": data.get("economic_calendar", {}),
        "pnl": data["pnl"],
        "updated_at": data["updated_at"],
        "trailing_strategy": data["trailing"],
        "copy_trading_strategy": data["copy_trading"],
        "wheel_strategy": data["wheel"],
        "short_candidates": data.get("short_candidates", []),
        "trading_session": data.get("trading_session", "unknown"),
        "options_data": data.get("options_data"),
        "earnings_candidates": data.get("earnings_candidates", []),
        "news_signals": data.get("news_signals", {"actionable": []}),
        "options_flow": data.get("options_flow", []),
    }
    safe_save_json(DATA_JSON_PATH, output)
    print(f"Data JSON saved: {DATA_JSON_PATH}")

    # Round-61 pt.56: snapshot today's picks into picks_history.json
    # so the pipeline-backtest harness has a sequence to replay.
    # Best-effort — never block the screener cycle.
    try:
        import picks_history as _ph
        from datetime import date as _date_cls
        _hist_path = os.environ.get("PICKS_HISTORY_PATH") or \
                      os.path.join(DATA_DIR, "picks_history.json")
        _today = _date_cls.today().isoformat()
        _picks_for_history = output.get("picks") or []
        _ph.snapshot_picks(_today, _picks_for_history, _hist_path)
    except Exception as _ph_e:
        print(f"picks_history snapshot failed: {_ph_e}")


def main():
    start = time.time()
    data = fetch_all_data()

    print("Generating dashboard HTML...")
    html = generate_html(data)
    # Atomic write for dashboard HTML — temp file must live on same filesystem
    # as DASHBOARD_PATH so os.rename() is atomic.
    _html_dir = os.path.dirname(DASHBOARD_PATH) or DATA_DIR
    os.makedirs(_html_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=_html_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(html)
        os.rename(tmp_path, DASHBOARD_PATH)
    except Exception:
        try: os.unlink(tmp_path)
        except OSError: pass
        raise
    print(f"Dashboard saved: {DASHBOARD_PATH}")

    print("Saving enhanced data JSON...")
    save_data_json(data)

    elapsed = time.time() - start
    print(f"\nTotal runtime: {elapsed:.1f}s")

    # Summary
    print(f"\nMarket regime: {data['market_regime']} (SPY 20d: {data['spy_momentum_20d']:+.1f}%)")
    econ = data.get("economic_calendar", {})
    if econ:
        print(f"Economic calendar: {econ.get('risk_level', 'N/A').upper()} risk -- {econ.get('recommendation', '')}")
    pnl = data["pnl"]
    print(f"Portfolio P&L: ${pnl['total_portfolio_pnl']:,.2f} ({pnl['daily_pnl_pct']:+.1f}%)")
    if pnl["alert_triggered"]:
        print("*** ALERT: Daily loss exceeds -3% threshold! ***")
        # Round-15: actually notify the operator. Previously this was a
        # print + dashboard flag with no push / email wiring, so a real
        # -3% intraday drawdown wouldn't notify anyone off-dashboard.
        # critical_alert routes through ntfy.sh + email + Sentry.
        # Dedupe by ET-day stamp so the 30-min-refresh screener doesn't
        # re-fire the alert every cycle.
        try:
            from observability import critical_alert
            from et_time import now_et
            today = now_et().strftime("%Y-%m-%d")
            stamp_path = os.path.join(DATA_DIR, "daily_loss_alert_stamp.txt")
            last = ""
            try:
                with open(stamp_path) as _f:
                    last = _f.read().strip()
            except Exception:
                pass
            if last != today:
                # The scheduler passes ntfy_topic + notification_email via
                # env vars when invoking this subprocess (cloud_scheduler
                # lines 797/871). Build a minimal user dict so critical_
                # alert's per-user ntfy + email paths work.
                _alert_user = {
                    "ntfy_topic": os.environ.get("NTFY_TOPIC") or None,
                    "notification_email": os.environ.get("NOTIFICATION_EMAIL") or None,
                }
                critical_alert(
                    f"Daily loss alert: {pnl['daily_pnl_pct']:+.1f}%",
                    f"Portfolio P&L ${pnl['total_portfolio_pnl']:,.2f} "
                    f"({pnl['daily_pnl_pct']:+.1f}%) — exceeds the -3% "
                    f"daily-loss threshold. Review positions. Kill switch "
                    f"is available in Settings if the downtrend continues.",
                    tags={"daily_pnl_pct": round(pnl["daily_pnl_pct"], 2)},
                    user=_alert_user,
                )
                with open(stamp_path, "w") as _f:
                    _f.write(today)
        except Exception as _e:
            print(f"[daily-loss alert] failed to notify: {type(_e).__name__}")

    if data["diversified_top5"]:
        print(f"\nDiversified Top 5 Picks:")
        for i, p in enumerate(data["diversified_top5"]):
            extras = []
            if p.get("momentum_5d") is not None:
                extras.append(f"5d:{p['momentum_5d']:+.1f}%")
            if p.get("momentum_20d") is not None:
                extras.append(f"20d:{p['momentum_20d']:+.1f}%")
            if p.get("relative_volume") is not None:
                extras.append(f"rvol:{p['relative_volume']:.1f}x")
            if p.get("backtest_return") is not None:
                extras.append(f"bt:{p['backtest_return']:+.1f}%")
            if p.get("news_sentiment", "neutral") != "neutral":
                extras.append(f"news:{p['news_sentiment']}")
            if p.get("earnings_warning"):
                extras.append("EARNINGS!")
            if p.get("social_sentiment", "unknown") not in ("unknown", "neutral"):
                extras.append(f"social:{p['social_sentiment']}")
            if p.get("meme_warning"):
                extras.append("MEME!")
            extra_str = f" [{', '.join(extras)}]" if extras else ""
            print(f"  {i+1}. {p['symbol']} ({p['sector']}) - {p['best_strategy']}, score {p['best_score']:.0f}, {p['recommended_shares']} shares{extra_str}")
    elif data["picks"]:
        print(f"\nTop 3 picks: {', '.join(p['symbol'] + ' (' + p['best_strategy'] + ', score ' + str(round(p['best_score'])) + ')' for p in data['picks'][:3])}")
    else:
        print("No stock picks available (market may be closed or data unavailable)")


if __name__ == "__main__":
    main()
