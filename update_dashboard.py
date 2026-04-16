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
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_DIR = os.path.join(BASE_DIR, "strategies")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
DATA_JSON_PATH = os.path.join(BASE_DIR, "dashboard_data.json")

API_ENDPOINT = "https://paper-api.alpaca.markets/v2"
DATA_ENDPOINT = "https://data.alpaca.markets/v2"
NEWS_ENDPOINT = "https://data.alpaca.markets/v1beta1/news"
API_KEY = ""
API_SECRET = ""

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "ARCA"}
BATCH_SIZE = 500
MIN_PRICE = 5.0
MIN_VOLUME = 100_000

# --- Improvement 3: Sector Map ---
SECTOR_MAP = {
    # Tech
    "AAPL": "Tech", "MSFT": "Tech", "GOOG": "Tech", "GOOGL": "Tech", "META": "Tech",
    "NVDA": "Tech", "AMD": "Tech", "INTC": "Tech", "CRM": "Tech", "ORCL": "Tech",
    "ADBE": "Tech", "NOW": "Tech", "SHOP": "Tech", "SQ": "Tech", "PLTR": "Tech",
    "NET": "Tech", "SNOW": "Tech", "DDOG": "Tech", "MDB": "Tech", "CRWD": "Tech",
    # Consumer
    "AMZN": "Consumer", "TSLA": "Consumer", "NKE": "Consumer", "SBUX": "Consumer",
    "MCD": "Consumer", "HD": "Consumer", "LOW": "Consumer", "TGT": "Consumer",
    "COST": "Consumer", "WMT": "Consumer", "DIS": "Consumer", "NFLX": "Consumer",
    # Finance
    "JPM": "Finance", "BAC": "Finance", "GS": "Finance", "MS": "Finance",
    "WFC": "Finance", "C": "Finance", "BLK": "Finance", "SCHW": "Finance",
    "COIN": "Finance", "SOFI": "Finance", "V": "Finance", "MA": "Finance",
    # Healthcare
    "JNJ": "Healthcare", "UNH": "Healthcare", "PFE": "Healthcare", "ABBV": "Healthcare",
    "MRK": "Healthcare", "LLY": "Healthcare", "BMY": "Healthcare", "AMGN": "Healthcare",
    "MRNA": "Healthcare", "GILD": "Healthcare",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy", "OXY": "Energy",
    # Industrial
    "BA": "Industrial", "CAT": "Industrial", "DE": "Industrial", "GE": "Industrial",
    "HON": "Industrial", "LMT": "Industrial", "RTX": "Industrial", "UPS": "Industrial",
}

# --- Improvement 6: Profit-Taking Ladder ---
PROFIT_LADDER = [
    {"gain_pct": 10, "sell_pct": 25, "note": "Lock in early gains"},
    {"gain_pct": 20, "sell_pct": 25, "note": "Take more off the table"},
    {"gain_pct": 30, "sell_pct": 25, "note": "Secure majority profit"},
    {"gain_pct": 50, "sell_pct": 25, "note": "Let the rest ride"},
]

# --- Improvement 8: Sentiment keywords ---
POSITIVE_KEYWORDS = ["beats", "record", "growth", "upgrade", "bullish", "raised", "strong"]
NEGATIVE_KEYWORDS = ["misses", "decline", "downgrade", "bearish", "cut", "weak", "lawsuit", "investigation", "recall"]

# --- Improvement 4: Earnings keywords ---
EARNINGS_KEYWORDS = ["earnings", "quarterly results", "q1", "q2", "q3", "q4", "revenue report", "guidance"]


def api_get(url, timeout=15):
    """Make an authenticated GET request to Alpaca API."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
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
    data = api_get(url, timeout=30)
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
    return api_get(url, timeout=20)


def fetch_all_snapshots(symbols):
    """Fetch snapshots for all symbols in batches of BATCH_SIZE."""
    all_snapshots = {}
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    total_batches = len(batches)

    for idx, batch in enumerate(batches):
        print(f"  Fetching batch {idx + 1}/{total_batches}... ({len(batch)} symbols)")
        result = fetch_snapshots_batch(batch)
        if isinstance(result, dict) and "error" not in result:
            all_snapshots.update(result)
        elif isinstance(result, dict) and "error" in result:
            print(f"    Batch {idx + 1} failed: {result['error']} -- skipping")
        # Small delay to avoid rate limiting
        if idx < total_batches - 1:
            time.sleep(0.3)

    print(f"  Got snapshot data for {len(all_snapshots)} symbols")
    return all_snapshots


def score_stocks(snapshots):
    """Score each stock across all 3 strategies using snapshot data (initial fast pass)."""
    results = []

    for symbol, snap in snapshots.items():
        try:
            daily_bar = snap.get("dailyBar") or {}
            prev_bar = snap.get("prevDailyBar") or {}
            latest_trade = snap.get("latestTrade") or {}

            price = latest_trade.get("p", 0)
            daily_close = daily_bar.get("c", 0)
            prev_close = prev_bar.get("c", 0)
            daily_high = daily_bar.get("h", 0)
            daily_low = daily_bar.get("l", 0)
            daily_volume = daily_bar.get("v", 0)
            prev_volume = prev_bar.get("v", 0)

            # Skip if missing critical data
            if not price or not prev_close or not daily_low:
                continue

            # Filter: penny stocks and illiquid
            if price < MIN_PRICE:
                continue
            if daily_volume < MIN_VOLUME:
                continue

            # Calculate metrics
            daily_change = (daily_close / prev_close - 1) * 100 if prev_close else 0
            volatility = (daily_high - daily_low) / daily_low * 100 if daily_low else 0
            volume_surge = (daily_volume / prev_volume - 1) * 100 if prev_volume else 0

            # --- Strategy Scores ---
            # Trailing Stop Score
            trailing_score = daily_change * 0.5 + volatility * 0.3
            if volume_surge > 50:
                trailing_score += 5

            # Copy Trading Score
            copy_score = daily_change * 0.3
            if price > 100 and daily_volume > 1_000_000:
                copy_score += 15

            # Wheel Strategy Score
            wheel_score = volatility * 3
            if 20 <= price <= 500:
                wheel_score += 5
            if -5 <= daily_change <= 5:
                wheel_score += 5

            # Mean Reversion Score: rewards oversold stocks (big drop + high volume = bounce candidate)
            mean_reversion_score = 0
            if daily_change < -5:  # stock dropped significantly today
                mean_reversion_score = abs(daily_change) * 1.5 + volatility * 0.3
                if volume_surge > 50:
                    mean_reversion_score += 5  # high volume selloff = more likely to bounce
            elif daily_change < -2:
                mean_reversion_score = abs(daily_change) * 0.8

            # Breakout Score: rewards stocks breaking up on high volume
            breakout_score = 0
            if daily_change > 3 and volume_surge > 50:  # up big on high volume
                breakout_score = daily_change * 1.5 + (volume_surge / 20)
            elif daily_change > 2 and volume_surge > 30:
                breakout_score = daily_change * 1.0 + (volume_surge / 30)

            # Best strategy
            scores = {
                "Trailing Stop": trailing_score,
                "Copy Trading": copy_score,
                "Wheel Strategy": wheel_score,
                "Mean Reversion": mean_reversion_score,
                "Breakout": breakout_score,
            }
            best_strategy = max(scores, key=scores.get)
            best_score = scores[best_strategy]

            # Sector (Improvement 3)
            sector = SECTOR_MAP.get(symbol, "Other")

            results.append({
                "symbol": symbol,
                "price": price,
                "daily_change": daily_change,
                "volatility": volatility,
                "daily_volume": daily_volume,
                "volume_surge": volume_surge,
                "best_strategy": best_strategy,
                "best_score": best_score,
                "scores": scores,
                "trailing_score": trailing_score,
                "copy_score": copy_score,
                "wheel_score": wheel_score,
                "mean_reversion_score": mean_reversion_score,
                "breakout_score": breakout_score,
                "sector": sector,
            })
        except Exception:
            continue

    # Sort by highest best-strategy score
    results.sort(key=lambda x: x["best_score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Improvement 1 & 2: Multi-timeframe Momentum + Relative Volume from 20-day bars
# ---------------------------------------------------------------------------

def fetch_historical_bars(symbol, days=20):
    """Fetch daily bars for a symbol over the last N calendar days."""
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    url = (
        f"{DATA_ENDPOINT}/stocks/{urllib.parse.quote(symbol)}/bars"
        f"?timeframe=1Day&start={start_date}&end={end_date}&limit={days}&feed=iex"
    )
    result = api_get(url, timeout=10)
    if isinstance(result, dict) and "bars" in result:
        return result["bars"]
    if isinstance(result, list):
        return result
    return []


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

    # Recalculate best with ALL 5 strategies
    pick["scores"] = {
        "Trailing Stop": pick["trailing_score"],
        "Copy Trading": pick["copy_score"],
        "Wheel Strategy": pick["wheel_score"],
        "Mean Reversion": pick["mean_reversion_score"],
        "Breakout": pick["breakout_score"],
    }
    pick["best_strategy"] = max(pick["scores"], key=pick["scores"].get)
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

    if spy_mom > 5:
        regime = "bull"
    elif spy_mom < -5:
        regime = "bear"
    else:
        regime = "neutral"

    print(f"  SPY 20d momentum: {spy_mom:+.1f}% -- Market regime: {regime}")
    return {"spy_momentum_20d": round(spy_mom, 2), "market_regime": regime}


def apply_market_regime(picks, regime):
    """Adjust scores based on market regime (Improvement 7)."""
    if regime == "bear":
        print("  Bear market regime: adjusting scores (-5 trailing, +3 wheel)")
        for p in picks:
            p["trailing_score"] -= 5
            p["wheel_score"] += 3
            p["scores"]["Trailing Stop"] = p["trailing_score"]
            p["scores"]["Wheel Strategy"] = p["wheel_score"]
            p["best_strategy"] = max(p["scores"], key=p["scores"].get)
            p["best_score"] = p["scores"][p["best_strategy"]]


# ---------------------------------------------------------------------------
# Improvement 4 & 8: Earnings Avoidance + News Sentiment
# ---------------------------------------------------------------------------

def fetch_news_for_symbol(symbol, limit=5):
    """Fetch recent news for a symbol."""
    url = f"{NEWS_ENDPOINT}?symbols={urllib.parse.quote(symbol)}&limit={limit}"
    result = api_get(url, timeout=10)
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
        (item.get("headline", "") + " " + item.get("summary", "")).lower()
        for item in news_items
    )

    # Improvement 4: Earnings avoidance
    earnings_found = any(kw in headlines_text for kw in EARNINGS_KEYWORDS)
    pick["earnings_warning"] = earnings_found
    if earnings_found:
        pick["trailing_score"] -= 10
        pick["copy_score"] -= 10
        pick["wheel_score"] -= 10

    # Improvement 8: Sentiment
    pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in headlines_text)
    neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in headlines_text)
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
    }
    pick["best_strategy"] = max(pick["scores"], key=pick["scores"].get)
    pick["best_score"] = pick["scores"][pick["best_strategy"]]


# ---------------------------------------------------------------------------
# Improvement 5: Dynamic Position Sizing
# ---------------------------------------------------------------------------

def calc_position_size(price, volatility, portfolio_value, max_risk_pct=0.02):
    """Size position so max loss (1 ATR move) = max_risk_pct of portfolio."""
    if price <= 0 or volatility <= 0 or portfolio_value <= 0:
        return 1
    risk_per_share = price * (volatility / 100)
    max_risk_dollars = portfolio_value * max_risk_pct
    shares = max(1, int(max_risk_dollars / risk_per_share))
    max_by_value = max(1, int(portfolio_value * 0.10 / price))  # max 10% in one stock
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
    """Simulate trailing stop on historical data. Return dict with results."""
    if not bars or len(bars) < 2:
        return {"entry": 0, "exit": 0, "return_pct": 0, "stopped_out": False, "days": 0}

    entry = bars[0].get("c", 0)
    if entry <= 0:
        return {"entry": 0, "exit": 0, "return_pct": 0, "stopped_out": False, "days": 0}

    highest = entry
    stop_price = entry * (1 - stop_pct)
    trailing_active = False

    for i, bar in enumerate(bars[1:], start=1):
        low = bar.get("l", 0)
        high = bar.get("h", 0)
        close = bar.get("c", 0)

        # Check if stopped out
        if low > 0 and low <= stop_price:
            exit_price = stop_price
            return {
                "entry": round(entry, 2),
                "exit": round(exit_price, 2),
                "return_pct": round((exit_price / entry - 1) * 100, 2),
                "stopped_out": True,
                "days": i,
            }

        if high > highest:
            highest = high

        if not trailing_active and highest >= entry * (1 + trail_activation):
            trailing_active = True

        if trailing_active:
            new_stop = highest * (1 - trail_distance)
            if new_stop > stop_price:
                stop_price = new_stop

    final = bars[-1].get("c", 0)
    return {
        "entry": round(entry, 2),
        "exit": round(final, 2),
        "return_pct": round((final / entry - 1) * 100, 2) if entry > 0 else 0,
        "stopped_out": False,
        "days": len(bars) - 1,
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
    account = api_get(f"{API_ENDPOINT}/account")
    positions = api_get(f"{API_ENDPOINT}/positions")
    orders = api_get(f"{API_ENDPOINT}/orders?status=open&limit=50")

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
    print("Scoring stocks (initial pass)...")
    picks = score_stocks(snapshots)
    print(f"  Scored {len(picks)} stocks after filtering (price >= ${MIN_PRICE}, volume >= {MIN_VOLUME:,})")

    # Apply market regime to all picks (Improvement 7)
    apply_market_regime(picks, market_info["market_regime"])

    # --- Enrich top 100 candidates ---
    top_candidates = picks[:100]
    if top_candidates:
        # Improvement 1 & 2: Fetch 20-day bars for momentum + relative volume
        print(f"Enriching top {len(top_candidates)} candidates with 20-day historical bars...")
        for i, pick in enumerate(top_candidates):
            sym = pick["symbol"]
            if (i + 1) % 20 == 0 or i == 0:
                print(f"  Fetching bars {i + 1}/{len(top_candidates)}: {sym}...")
            try:
                bars_20d = fetch_historical_bars(sym, days=20)
                enrich_with_momentum(pick, bars_20d)
            except Exception as e:
                print(f"    Error fetching bars for {sym}: {e}")
                pick["momentum_5d"] = 0.0
                pick["momentum_20d"] = 0.0
                pick["relative_volume"] = 1.0
            time.sleep(0.15)  # rate limit

        # Improvement 4 & 8: News sentiment + earnings avoidance for top 20
        print("Fetching news for top 20 candidates (sentiment + earnings check)...")
        for i, pick in enumerate(top_candidates[:20]):
            sym = pick["symbol"]
            try:
                news_items = fetch_news_for_symbol(sym, limit=5)
                analyze_news(pick, news_items)
                if pick.get("earnings_warning"):
                    print(f"    {sym}: EARNINGS WARNING detected -- score penalized")
                if pick.get("news_sentiment") != "neutral":
                    print(f"    {sym}: Sentiment = {pick['news_sentiment']} (score: {pick['sentiment_score']})")
            except Exception as e:
                print(f"    Error fetching news for {sym}: {e}")
                pick["earnings_warning"] = False
                pick["news_sentiment"] = "neutral"
                pick["sentiment_score"] = 0
            time.sleep(0.15)

        # Set defaults for candidates 20-100 that didn't get news
        for pick in top_candidates[20:]:
            pick.setdefault("earnings_warning", False)
            pick.setdefault("news_sentiment", "neutral")
            pick.setdefault("sentiment_score", 0)

    # --- Apply learned weights from self-learning engine ---
    learned = load_json(os.path.join(BASE_DIR, "learned_weights.json"))
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
            }
            pick["best_strategy"] = max(pick["scores"], key=pick["scores"].get)
            pick["best_score"] = pick["scores"][pick["best_strategy"]]
    else:
        print("No learned_weights.json found -- skipping learned weight adjustments.")

    # Re-sort top candidates after enrichment
    top_candidates.sort(key=lambda x: x["best_score"], reverse=True)

    # Improvement 10: Backtest top 10
    print("Running backtests on top 10 candidates (30-day trailing stop sim)...")
    for i, pick in enumerate(top_candidates[:10]):
        sym = pick["symbol"]
        try:
            bars_30d = fetch_historical_bars(sym, days=30)
            bt = backtest_trailing_stop(bars_30d)
            pick["backtest_return"] = bt["return_pct"]
            pick["backtest_detail"] = bt
            print(f"    {sym}: backtest return = {bt['return_pct']:+.1f}% ({'stopped out' if bt['stopped_out'] else 'held'} over {bt['days']}d)")
        except Exception as e:
            print(f"    Error backtesting {sym}: {e}")
            pick["backtest_return"] = 0.0
            pick["backtest_detail"] = {}
        time.sleep(0.15)

    # Set defaults for candidates without backtest
    for pick in top_candidates[10:]:
        pick.setdefault("backtest_return", None)
        pick.setdefault("backtest_detail", None)

    # Improvement 5: Position sizing for top candidates
    print("Calculating position sizes...")
    for pick in top_candidates:
        pick["recommended_shares"] = calc_position_size(
            pick["price"], pick["volatility"], portfolio_value
        )

    # Improvement 6: Add profit ladder to all top candidates
    for pick in top_candidates:
        pick["profit_ladder"] = PROFIT_LADDER

    # Improvement 3: Sector-diversified top 5
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

    all_picks = top_candidates + remaining

    # Improvement 9: Daily P&L
    pnl_data = compute_portfolio_pnl(positions_list, portfolio_value)
    if pnl_data["alert_triggered"]:
        print(f"  ALERT: Daily loss of {pnl_data['daily_pnl_pct']:.1f}% exceeds -3% threshold!")

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
        "total_screened": len(snapshots),
        "total_passed": len(all_picks),
        "market_regime": market_info["market_regime"],
        "spy_momentum_20d": market_info["spy_momentum_20d"],
        "pnl": pnl_data,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
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
    max_score = max((p["best_score"] for p in picks), default=1) or 1

    for i, p in enumerate(top3):
        color = strategy_colors.get(p["best_strategy"], "#3b82f6")
        chg_class = "positive" if p["daily_change"] >= 0 else "negative"
        vs_class = "positive" if p["volume_surge"] > 0 else "negative"
        rank = rank_labels[i] if i < 3 else ""

        t_pct = max(5, min(100, p["trailing_score"] / max_score * 100))
        c_pct = max(5, min(100, p["copy_score"] / max_score * 100))
        w_pct = max(5, min(100, p["wheel_score"] / max_score * 100))

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
        "pnl": data["pnl"],
        "updated_at": data["updated_at"],
        "trailing_strategy": data["trailing"],
        "copy_trading_strategy": data["copy_trading"],
        "wheel_strategy": data["wheel"],
    }
    with open(DATA_JSON_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"Data JSON saved: {DATA_JSON_PATH}")


def main():
    start = time.time()
    data = fetch_all_data()

    print("Generating dashboard HTML...")
    html = generate_html(data)
    with open(DASHBOARD_PATH, "w") as f:
        f.write(html)
    print(f"Dashboard saved: {DASHBOARD_PATH}")

    print("Saving enhanced data JSON...")
    save_data_json(data)

    elapsed = time.time() - start
    print(f"\nTotal runtime: {elapsed:.1f}s")

    # Summary
    print(f"\nMarket regime: {data['market_regime']} (SPY 20d: {data['spy_momentum_20d']:+.1f}%)")
    pnl = data["pnl"]
    print(f"Portfolio P&L: ${pnl['total_portfolio_pnl']:,.2f} ({pnl['daily_pnl_pct']:+.1f}%)")
    if pnl["alert_triggered"]:
        print("*** ALERT: Daily loss exceeds -3% threshold! ***")

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
            extra_str = f" [{', '.join(extras)}]" if extras else ""
            print(f"  {i+1}. {p['symbol']} ({p['sector']}) - {p['best_strategy']}, score {p['best_score']:.0f}, {p['recommended_shares']} shares{extra_str}")
    elif data["picks"]:
        print(f"\nTop 3 picks: {', '.join(p['symbol'] + ' (' + p['best_strategy'] + ', score ' + str(round(p['best_score'])) + ')' for p in data['picks'][:3])}")
    else:
        print("No stock picks available (market may be closed or data unavailable)")


if __name__ == "__main__":
    main()
