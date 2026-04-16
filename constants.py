#!/usr/bin/env python3
"""
Shared constants used across multiple modules.

Anything that represents a "single source of truth" about the bot's
behaviour — sector classifications, the profit ladder schedule, the
keyword lists used for sentiment scoring — lives here. Previously
these were duplicated across update_dashboard.py, update_scorecard.py,
and cloud_scheduler.py, which risked divergent edits silently changing
the bot's decisions (e.g. adding a symbol to sector map in one file
but not the other would make correlation guards inconsistent with
the scorecard's sector reporting).

stdlib-only by design.
"""
import re

# --- Improvement 3: Sector Map ---
# Used for:
#   - Correlation guard (block 3+ positions in the same sector)
#   - Sector diversification scoring in the screener
#   - Scorecard sector breakdown
# When adding a symbol, update here only — all consumers import from this file.
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
# Scales out of winners at these levels. Each rung sells sell_pct of the
# ORIGINAL position size (not remaining shares), so the math works out to
# 25% left riding after hitting the 50% target.
PROFIT_LADDER = [
    {"gain_pct": 10, "sell_pct": 25, "note": "Lock in early gains"},
    {"gain_pct": 20, "sell_pct": 25, "note": "Take more off the table"},
    {"gain_pct": 30, "sell_pct": 25, "note": "Secure majority profit"},
    {"gain_pct": 50, "sell_pct": 25, "note": "Let the rest ride"},
]


# --- Improvement 8: News Sentiment Keywords ---
# Simple bag-of-keywords classifier. Weighted more heavily toward negative
# signals because missing a "lawsuit" hurts more than missing an "upgrade".
POSITIVE_KEYWORDS = [
    "beats", "record", "growth", "upgrade", "bullish", "raised", "strong",
]
NEGATIVE_KEYWORDS = [
    "misses", "decline", "downgrade", "bearish", "cut", "weak",
    "lawsuit", "investigation", "recall",
]


# --- Improvement 4: Earnings Date Avoidance Patterns ---
# Word-boundary regex so we don't false-match "earning" (present tense in
# unrelated copy). Q_PATTERN catches "Q1", "Q2", etc.
EARNINGS_PATTERN = re.compile(r'\b(earnings|quarterly results|revenue report|guidance)\b', re.IGNORECASE)
Q_PATTERN = re.compile(r'\bQ[1-4]\b')
