#!/usr/bin/env python3
"""
Post-market news scanner.
Scans news for actionable signals: earnings beats, upgrades, contract wins, etc.
Identifies stocks likely to gap up on open.
"""
import json
import os
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from et_time import now_et

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

# Bullish signal patterns (with weights)
BULLISH_SIGNALS = {
    "earnings beat": 10, "beats estimates": 10, "beats expectations": 10,
    "revenue beat": 8, "guidance raised": 10, "raises guidance": 10,
    "fda approval": 12, "fda approves": 12, "fda cleared": 10,
    "contract win": 6, "awarded contract": 6, "wins contract": 6,
    "upgraded to buy": 8, "price target raised": 6, "upgrade": 5,
    "strategic partnership": 5, "acquisition target": 8,
    "record revenue": 7, "record earnings": 7, "all-time high": 5,
    "dividend increase": 4, "buyback": 5,
}

BEARISH_SIGNALS = {
    "earnings miss": -10, "misses estimates": -10, "misses expectations": -10,
    "guidance lowered": -10, "cuts guidance": -10,
    "fda rejection": -12, "fda rejects": -12,
    "downgraded": -7, "price target cut": -6, "downgrade": -5,
    "lawsuit": -6, "investigation": -8, "sec probe": -10,
    "recall": -7, "ceo resign": -8, "ceo steps down": -8,
    "bankruptcy": -15, "going concern": -12,
    "fraud": -15, "delisting": -12,
}

def api_get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def score_news_article(article):
    """Score a news article for actionable signals.

    Uses longest-match precedence so "upgraded to buy" doesn't also trigger "upgrade".
    Patterns are checked in order of length (longest first), and any matched span
    is masked out so shorter overlapping patterns don't double-count.
    """
    text = (article.get("headline","") + " " + article.get("summary","")).lower()
    score = 0
    signals = []

    # Combine bullish + bearish with their direction labels, longest first
    all_patterns = (
        [(p, w, "bullish") for p, w in BULLISH_SIGNALS.items()]
        + [(p, w, "bearish") for p, w in BEARISH_SIGNALS.items()]
    )
    all_patterns.sort(key=lambda x: -len(x[0]))

    # Mask matched spans in working text so shorter overlapping terms don't re-match
    working = text
    for pattern, weight, direction in all_patterns:
        if pattern in working:
            score += weight
            signals.append({"type": direction, "signal": pattern, "weight": weight})
            # Replace with spaces to preserve positions
            working = working.replace(pattern, " " * len(pattern))
    # Cap per-article score to ±15 so a single news item densely packed
    # with bullish/bearish language can't dominate the symbol total and
    # push us into an action purely from one source. Aggregated symbol
    # scoring in scan_post_market_news still sums across articles.
    score = max(-15, min(15, score))
    return score, signals

def scan_post_market_news(hours_back=12, min_score=8):
    """Scan recent news for high-signal articles (post-market earnings, FDA news, etc.)"""
    # Alpaca news API accepts ISO with any offset; ET offset is fine.
    since = (now_et() - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%S%z")
    url = f"https://data.alpaca.markets/v1beta1/news?limit=50&start={since}"
    data = api_get(url)
    news = data.get("news", []) if isinstance(data, dict) else []

    actionable = []
    symbol_scores = {}

    for article in news:
        score, signals = score_news_article(article)
        if abs(score) < min_score:
            continue

        symbols = article.get("symbols", [])
        for sym in symbols:
            if sym not in symbol_scores:
                symbol_scores[sym] = {"score": 0, "articles": [], "signals": []}
            symbol_scores[sym]["score"] += score
            symbol_scores[sym]["articles"].append({
                "headline": article.get("headline", "")[:120],
                "source": article.get("source", ""),
                "created_at": article.get("created_at", ""),
                "score": score
            })
            symbol_scores[sym]["signals"].extend(signals)

    # Format results
    for sym, data in symbol_scores.items():
        total_score = data["score"]
        if abs(total_score) < min_score:
            continue

        direction = "bullish" if total_score > 0 else "bearish"
        action = None
        if total_score >= 15:
            action = "STRONG BUY: Pre-market limit buy recommended"
        elif total_score >= 8:
            action = "BUY: Consider opening position at market open"
        elif total_score <= -15:
            action = "STRONG SELL: Exit any position, consider short"
        elif total_score <= -8:
            action = "SELL: Exit position if held, avoid new long"

        actionable.append({
            "symbol": sym,
            "score": total_score,
            "direction": direction,
            "action": action,
            "article_count": len(data["articles"]),
            "articles": data["articles"][:3],
            "top_signals": list({s["signal"] for s in data["signals"]})[:5],
        })

    actionable.sort(key=lambda x: abs(x["score"]), reverse=True)

    return {
        "scanned_at": now_et().isoformat(),
        "hours_scanned": hours_back,
        "total_articles": len(news),
        "actionable_count": len(actionable),
        "actionable": actionable[:20],
    }

if __name__ == "__main__":
    results = scan_post_market_news()
    print(f"Scanned {results['total_articles']} articles, found {results['actionable_count']} actionable")
    for a in results["actionable"][:5]:
        print(f"  {a['symbol']} score={a['score']} ({a['direction']}): {a['action']}")
        for art in a['articles'][:1]:
            print(f"    - {art['headline']}")
