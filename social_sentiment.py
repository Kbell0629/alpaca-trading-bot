#!/usr/bin/env python3
"""
Social sentiment analysis for the trading bot.
Uses free APIs — StockTwits (no auth) and basic web searches.
"""
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

def get_stocktwits_sentiment(symbol):
    """Get sentiment from StockTwits (free, no API key needed)."""
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        messages = data.get("messages", [])
        if not messages:
            return {"source": "stocktwits", "sentiment": "neutral", "score": 0, "volume": 0, "error": None}

        # Recency filter: drop messages older than STALE_THRESHOLD. For a
        # low-chatter symbol, StockTwits' 30 most recent may still stretch
        # back days — treating those as "current sentiment" is misleading.
        # If the freshest message is >30min old OR fewer than 5 messages
        # are within window, mark the reading stale and return neutral.
        STALE_MINUTES = 30
        MIN_FRESH_MSGS = 5
        try:
            now = datetime.now(timezone.utc)
            fresh = []
            for msg in messages:
                ts = msg.get("created_at")
                if not ts:
                    continue
                try:
                    # StockTwits format: "2026-04-19T12:34:56Z"
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if (now - dt).total_seconds() <= STALE_MINUTES * 60:
                    fresh.append(msg)
            if len(fresh) < MIN_FRESH_MSGS:
                return {"source": "stocktwits", "sentiment": "neutral",
                        "score": 0, "volume": len(messages),
                        "stale": True,
                        "error": f"stale: only {len(fresh)} fresh msgs (<{MIN_FRESH_MSGS})"}
            messages = fresh
        except Exception as e:
            # Recency filter failed (shape drift) — fall back to raw list
            # rather than blocking sentiment entirely. The existing path
            # handled the last 12 months of StockTwits fine.
            # Still route through observability so a systematic shape
            # change (StockTwits renaming "created_at") doesn't silently
            # corrupt every sentiment reading.
            try:
                from observability import capture_exception
                capture_exception(e, component="stocktwits_recency_filter",
                                  symbol=symbol)
            except ImportError:
                pass

        bullish = 0
        bearish = 0
        total = len(messages)

        for msg in messages:
            entities = msg.get("entities", {})
            sentiment = entities.get("sentiment", {})
            if sentiment:
                if sentiment.get("basic") == "Bullish":
                    bullish += 1
                elif sentiment.get("basic") == "Bearish":
                    bearish += 1

        if bullish + bearish == 0:
            score = 0
            sentiment_label = "neutral"
        else:
            score = (bullish - bearish) / (bullish + bearish) * 100  # -100 to +100
            if score > 20:
                sentiment_label = "bullish"
            elif score < -20:
                sentiment_label = "bearish"
            else:
                sentiment_label = "neutral"

        return {
            "source": "stocktwits",
            "sentiment": sentiment_label,
            "score": round(score, 1),
            "bullish": bullish,
            "bearish": bearish,
            "total_messages": total,
            "volume": total,  # message volume as a proxy for buzz
            "is_trending": total > 20,  # if lots of messages, it's buzzing
            "error": None,
        }
    except Exception as e:
        return {"source": "stocktwits", "sentiment": "unknown", "score": 0, "volume": 0, "error": str(e)}

def get_social_sentiment(symbol):
    """Get combined social sentiment for a stock."""
    stocktwits = get_stocktwits_sentiment(symbol)

    # Combine into a unified result
    result = {
        "symbol": symbol,
        "overall_sentiment": stocktwits["sentiment"],
        "overall_score": stocktwits["score"],
        "social_volume": stocktwits["volume"],
        "is_trending": stocktwits.get("is_trending", False),
        "sources": [stocktwits],
        "strategy_adjustments": {},
    }

    # Strategy adjustments based on social sentiment
    if stocktwits["sentiment"] == "bullish" and stocktwits["score"] > 50:
        result["strategy_adjustments"] = {
            "trailing_stop": 3,
            "copy_trading": 0,
            "breakout": 5,  # Social buzz + breakout = meme potential
            "mean_reversion": -3,  # Don't buy dips on hyped stocks
            "wheel": -5,  # Don't sell puts on meme stocks (too volatile)
        }
    elif stocktwits["sentiment"] == "bearish" and stocktwits["score"] < -50:
        result["strategy_adjustments"] = {
            "trailing_stop": -3,
            "copy_trading": 0,
            "mean_reversion": 3,  # Oversold + bearish sentiment = contrarian opportunity
            "breakout": 0,
            "wheel": -3,
        }

    # Meme stock warning
    if stocktwits["volume"] > 30 and abs(stocktwits["score"]) > 60:
        result["meme_warning"] = True
        result["meme_note"] = f"High social buzz ({stocktwits['volume']} msgs, {stocktwits['score']:+.0f} sentiment). Increased volatility risk."
    else:
        result["meme_warning"] = False

    return result

if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "TSLA"
    print(f"Social sentiment for {symbol}:")
    result = get_social_sentiment(symbol)
    print(f"  Sentiment: {result['overall_sentiment']} ({result['overall_score']:+.1f})")
    print(f"  Social volume: {result['social_volume']} messages")
    print(f"  Trending: {result['is_trending']}")
    print(f"  Meme warning: {result['meme_warning']}")
    if result.get("meme_note"):
        print(f"  Note: {result['meme_note']}")
    print(f"  Strategy adjustments: {result['strategy_adjustments']}")
