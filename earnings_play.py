#!/usr/bin/env python3
"""
Earnings play strategy: capture pre-earnings run-ups without gap risk.
Buys stocks 2-3 days before earnings with positive history, sells morning of.
Uses Alpaca news API to detect upcoming earnings.
"""
import json
import os
import re
import urllib.request
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_dotenv():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())
load_dotenv()

API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

EARNINGS_REGEX = re.compile(
    r'\b(earnings|quarterly results|reports q[1-4]|announces q[1-4]|fiscal q[1-4]|earnings (date|release|call))\b',
    re.IGNORECASE
)

def api_get(url, timeout=10):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def find_upcoming_earnings(symbols, days_ahead=5):
    """Find which symbols have earnings in the next N days based on news keywords."""
    results = []
    for sym in symbols[:50]:  # Limit to avoid API spam
        url = f"https://data.alpaca.markets/v1beta1/news?symbols={sym}&limit=5"
        data = api_get(url)
        news = data.get("news", []) if isinstance(data, dict) else []
        for article in news:
            text = (article.get("headline","") + " " + article.get("summary","")).lower()
            if EARNINGS_REGEX.search(text):
                # Try to detect if upcoming vs already reported
                if "will report" in text or "scheduled" in text or "expected" in text:
                    results.append({
                        "symbol": sym,
                        "headline": article.get("headline", ""),
                        "created_at": article.get("created_at", "")
                    })
                    break
    return results

def score_earnings_plays(picks):
    """From screener picks, identify stocks good for pre-earnings plays.

    Good earnings play candidates:
    - Strong momentum (positive 20d momentum)
    - Tech or consumer stocks (tend to run up into earnings)
    - High volume (liquidity)
    - Not in downtrend
    """
    candidates = []
    for pick in picks:
        momentum_20d = pick.get("momentum_20d", 0)
        momentum_5d = pick.get("momentum_5d", 0)
        volatility = pick.get("volatility", 0)
        sentiment = pick.get("news_sentiment", "neutral")
        daily_volume = pick.get("daily_volume", 0)

        # Only candidates in clear uptrend
        if momentum_20d < 5 or momentum_5d < 0:
            continue
        if daily_volume < 500_000:
            continue
        if sentiment == "negative":
            continue

        # Score: uptrend strength + liquidity bonus + sentiment
        score = (momentum_20d * 0.5) + (momentum_5d * 0.3)
        if sentiment == "positive":
            score += 5
        if volatility < 3:  # Low vol = steady climb into earnings
            score += 3

        if score < 8:
            continue

        candidates.append({
            "symbol": pick.get("symbol"),
            "price": pick.get("price"),
            "earnings_score": round(score, 1),
            "momentum_20d": momentum_20d,
            "momentum_5d": momentum_5d,
            "sentiment": sentiment,
            "strategy_note": "Pre-earnings momentum: buy 2-3 days before, sell morning of earnings",
            "rules": {
                "entry_timing": "2-3 days before expected earnings",
                "exit_timing": "Morning of earnings (NEVER hold through)",
                "stop_loss_pct": 0.05,
                "profit_target_pct": 0.08,
                "max_hold_days": 4,
            }
        })

    candidates.sort(key=lambda x: x["earnings_score"], reverse=True)
    return candidates

if __name__ == "__main__":
    # Test with sample data
    sample_picks = [
        {"symbol": "NVDA", "price": 200, "momentum_20d": 12, "momentum_5d": 3, "volatility": 2.5, "news_sentiment": "positive", "daily_volume": 5000000},
        {"symbol": "XYZ", "price": 50, "momentum_20d": -5, "momentum_5d": -2, "volatility": 4, "news_sentiment": "neutral", "daily_volume": 100000},
    ]
    candidates = score_earnings_plays(sample_picks)
    print(f"Earnings candidates: {len(candidates)}")
    for c in candidates:
        print(f"  {c['symbol']}: score {c['earnings_score']}")
