#!/usr/bin/env python3
"""
Economic calendar awareness for the trading bot.
Tracks Fed meetings, options expiration, and market-moving events.
Free — no paid APIs needed.
"""
import json
import os
import urllib.request
from datetime import datetime, date, timedelta, timezone

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

# 2026 FOMC Meeting Dates (Federal Reserve interest rate decisions)
# These are the most market-moving events of the year
FOMC_DATES_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-10",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

# Monthly options expiration (3rd Friday of each month)
def get_monthly_opex(year, month):
    """Get the 3rd Friday of the given month."""
    first_day = date(year, month, 1)
    # Find first Friday
    days_until_friday = (4 - first_day.weekday()) % 7
    first_friday = first_day + timedelta(days=days_until_friday)
    third_friday = first_friday + timedelta(weeks=2)
    return third_friday.isoformat()

# Quad witching dates (3rd Friday of Mar, Jun, Sep, Dec) — extra volatile
QUAD_WITCHING_MONTHS = [3, 6, 9, 12]

# Major market events keywords to watch for in news
EVENT_KEYWORDS = {
    "high_impact": ["federal reserve", "fomc", "interest rate", "rate decision", "cpi", "inflation data",
                     "jobs report", "nonfarm payroll", "gdp", "unemployment"],
    "medium_impact": ["consumer confidence", "retail sales", "housing starts", "pmi",
                       "trade balance", "oil inventory", "fed chair", "treasury"],
}

def api_get(url, timeout=10, max_retries=2):
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < max_retries - 1:
                import time
                time.sleep(1)
                continue
            return {"error": str(e)}

def check_upcoming_events(days_ahead=3):
    """Check for market-moving events in the next N days."""
    today = date.today()
    events = []

    # Pre-calculate opex dates for the months we'll check (avoids recalculating per day)
    fomc_set = set(FOMC_DATES_2026)
    opex_cache = {}
    for i in range(days_ahead + 1):
        check_date = today + timedelta(days=i)
        key = (check_date.year, check_date.month)
        if key not in opex_cache:
            opex_cache[key] = get_monthly_opex(check_date.year, check_date.month)

    for i in range(days_ahead + 1):
        check_date = today + timedelta(days=i)
        date_str = check_date.isoformat()
        days_away = i

        # FOMC meetings
        if date_str in fomc_set:
            events.append({
                "date": date_str,
                "days_away": days_away,
                "event": "FOMC Meeting / Rate Decision",
                "impact": "high",
                "action": "Tighten stops to 5%, avoid new entries within 24hrs"
            })

        # Options expiration
        opex = opex_cache[(check_date.year, check_date.month)]
        if date_str == opex:
            is_quad = check_date.month in QUAD_WITCHING_MONTHS
            events.append({
                "date": date_str,
                "days_away": days_away,
                "event": "Quad Witching" if is_quad else "Monthly Options Expiration",
                "impact": "high" if is_quad else "medium",
                "action": "Expect increased volatility. Avoid new wheel entries." if is_quad else "Slightly higher volume expected."
            })

    return events

def check_news_for_events():
    """Check recent Alpaca news for market-moving event headlines."""
    url = f"https://data.alpaca.markets/v1beta1/news?limit=10"
    result = api_get(url)

    events = []
    if isinstance(result, dict) and "news" in result:
        for article in result["news"]:
            headline = (article.get("headline", "") + " " + article.get("summary", "")).lower()

            for kw in EVENT_KEYWORDS["high_impact"]:
                if kw in headline:
                    events.append({
                        "date": article.get("created_at", "")[:10],
                        "days_away": 0,
                        "event": f"News: {article.get('headline', '')[:80]}",
                        "impact": "high",
                        "source": article.get("source", ""),
                        "action": "High-impact news detected. Consider tightening stops."
                    })
                    break
            else:
                for kw in EVENT_KEYWORDS["medium_impact"]:
                    if kw in headline:
                        events.append({
                            "date": article.get("created_at", "")[:10],
                            "days_away": 0,
                            "event": f"News: {article.get('headline', '')[:80]}",
                            "impact": "medium",
                            "source": article.get("source", ""),
                            "action": "Medium-impact economic news."
                        })
                        break

    return events

def get_market_risk_level():
    """Calculate overall market risk level based on upcoming events."""
    calendar_events = check_upcoming_events(3)
    news_events = check_news_for_events()
    all_events = calendar_events + news_events

    high_count = sum(1 for e in all_events if e["impact"] == "high")
    medium_count = sum(1 for e in all_events if e["impact"] == "medium")

    if high_count >= 2:
        risk_level = "extreme"
        recommendation = "Multiple high-impact events. Consider pausing all new trades and tightening stops to 5%."
    elif high_count >= 1:
        risk_level = "high"
        recommendation = "High-impact event approaching. Tighten stops, reduce position sizes by 50%."
    elif medium_count >= 2:
        risk_level = "elevated"
        recommendation = "Several medium-impact events. Use normal position sizes but set tighter stops."
    else:
        risk_level = "normal"
        recommendation = "No major events detected. Normal trading conditions."

    return {
        "risk_level": risk_level,
        "recommendation": recommendation,
        "events": all_events,
        "high_impact_count": high_count,
        "medium_impact_count": medium_count,
    }

if __name__ == "__main__":
    risk = get_market_risk_level()
    print(f"Market Risk Level: {risk['risk_level'].upper()}")
    print(f"Recommendation: {risk['recommendation']}")
    print(f"Events found: {len(risk['events'])}")
    for e in risk["events"]:
        print(f"  [{e['impact'].upper()}] {e['date']}: {e['event']}")
