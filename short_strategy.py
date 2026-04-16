#!/usr/bin/env python3
"""
Short selling strategy for bear markets.
Identifies stocks breaking down below support on high volume and shorts them.
Alpaca paper trading supports short selling.
"""
import json
import os
from datetime import datetime, timezone

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

def identify_short_candidates(picks):
    """From the screener picks, identify stocks suitable for shorting.

    Good short candidates:
    - Breaking below 20-day low on high volume
    - Bearish RSI (>70 and turning down)
    - Bearish MACD crossover
    - Negative news sentiment
    - In a downtrend (20d momentum < -10%)
    """
    short_candidates = []

    for pick in picks:
        score = 0
        reasons = []

        # Must be in a downtrend
        momentum_20d = pick.get("momentum_20d", 0)
        if momentum_20d > -5:
            continue  # Not bearish enough

        daily_change = pick.get("daily_change", 0)
        volatility = pick.get("volatility", 0)
        rsi = pick.get("rsi", 50)
        macd_hist = pick.get("macd_histogram", 0)
        overall_bias = pick.get("overall_bias", "neutral")
        sentiment = pick.get("news_sentiment", "neutral")
        relative_volume = pick.get("relative_volume", 1)

        # Strong downtrend
        if momentum_20d < -15:
            score += 10
            reasons.append(f"Strong downtrend: {momentum_20d:.1f}% in 20d")
        elif momentum_20d < -10:
            score += 5
            reasons.append(f"Downtrend: {momentum_20d:.1f}% in 20d")

        # Today is a down day
        if daily_change < -3:
            score += 5
            reasons.append(f"Big down day: {daily_change:.1f}%")

        # Bearish technical signals
        if overall_bias == "bearish":
            score += 5
            reasons.append("Bearish technical bias")

        if rsi > 65:
            score += 3
            reasons.append(f"Overbought RSI: {rsi:.0f} (potential reversal)")

        if macd_hist < 0:
            score += 3
            reasons.append("Bearish MACD")

        # Negative sentiment
        if sentiment == "negative":
            score += 5
            reasons.append("Negative news sentiment")

        # High volume on down day (institutional selling)
        if relative_volume > 1.5 and daily_change < 0:
            score += 5
            reasons.append(f"High volume selling: {relative_volume:.1f}x normal")

        # Must have minimum score
        if score < 10:
            continue

        # Calculate short parameters
        price = pick.get("price", 0)
        stop_loss_price = round(price * 1.08, 2)  # 8% stop above entry (tighter than long)
        profit_target = round(price * 0.85, 2)  # 15% profit target

        short_candidates.append({
            "symbol": pick.get("symbol"),
            "price": price,
            "short_score": score,
            "reasons": reasons,
            "stop_loss": stop_loss_price,
            "stop_loss_pct": 8.0,
            "profit_target": profit_target,
            "profit_target_pct": 15.0,
            "risk_reward": round(15.0 / 8.0, 1),  # R:R ratio
            "momentum_20d": momentum_20d,
            "daily_change": daily_change,
            "rsi": rsi,
            "sentiment": sentiment,
            "meme_warning": pick.get("meme_warning", False),
            "meme_note": pick.get("meme_note", ""),
            "social_sentiment": pick.get("social_sentiment", "unknown"),
        })

    short_candidates.sort(key=lambda x: x["short_score"], reverse=True)
    return short_candidates

def create_short_strategy_file(symbol, price, score, reasons):
    """Create a strategy JSON file for a short position."""
    return {
        "symbol": symbol,
        "strategy": "short_sell",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "status": "active",
        "entry_price_estimate": price,
        "rules": {
            "stop_loss_pct": 0.08,
            "profit_target_pct": 0.15,
            "max_hold_days": 14,
            "cover_at_support": True,
        },
        "state": {
            "entry_fill_price": None,
            "entry_order_id": None,
            "stop_order_id": None,
            "total_shares_shorted": 0,
            "highest_pnl": 0,
            "current_stop_price": round(price * 1.08, 2),
        },
        "reasoning": {
            "score": score,
            "reasons": reasons,
        }
    }

if __name__ == "__main__":
    # Test with sample picks
    sample_picks = [
        {"symbol": "XYZ", "price": 50, "momentum_20d": -12, "daily_change": -4,
         "volatility": 5, "rsi": 68, "macd_histogram": -0.5, "overall_bias": "bearish",
         "news_sentiment": "negative", "relative_volume": 2.0},
        {"symbol": "ABC", "price": 100, "momentum_20d": -3, "daily_change": 1,
         "volatility": 2, "rsi": 45, "macd_histogram": 0.2, "overall_bias": "neutral",
         "news_sentiment": "neutral", "relative_volume": 1.0},
    ]
    candidates = identify_short_candidates(sample_picks)
    print(f"Short candidates: {len(candidates)}")
    for c in candidates:
        print(f"  {c['symbol']}: score {c['short_score']}, entry ${c['price']}, stop ${c['stop_loss']}, target ${c['profit_target']} (R:R {c['risk_reward']})")
        for r in c["reasons"]:
            print(f"    - {r}")
