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
    # Round-10 additions — tickers the bot's 4/17 auto-deployer skipped
    # because they all defaulted to "Other" and blew past the 40% cap.
    # Populating these so correlation is judged on real sector rather
    # than a catch-all bucket.
    # Tech (extended)
    "AVGO": "Tech", "TXN": "Tech", "QCOM": "Tech", "ACN": "Tech", "IBM": "Tech",
    "CSCO": "Tech", "INTU": "Tech", "ADI": "Tech", "SNPS": "Tech",
    "NVTS": "Tech", "PATH": "Tech", "AFRM": "Tech", "HOOD": "Tech", "UBER": "Tech",
    "LYFT": "Tech", "DKNG": "Tech", "PYPL": "Tech", "ROKU": "Tech",
    # Consumer (extended)
    "PEP": "Consumer", "KO": "Consumer", "MDLZ": "Consumer", "BKNG": "Consumer",
    "PM": "Consumer", "MO": "Consumer", "CL": "Consumer", "PG": "Consumer",
    # Healthcare (extended)
    "ABT": "Healthcare", "TMO": "Healthcare", "DHR": "Healthcare", "MDT": "Healthcare",
    "BSX": "Healthcare", "SYK": "Healthcare", "ISRG": "Healthcare", "VRTX": "Healthcare",
    "REGN": "Healthcare", "ELV": "Healthcare", "CI": "Healthcare", "CVS": "Healthcare",
    "ZTS": "Healthcare", "HIMS": "Healthcare", "ACHV": "Healthcare", "SMMT": "Healthcare",
    "ALB": "Healthcare", "CMPS": "Healthcare", "BFLY": "Healthcare", "AMDL": "Healthcare",
    "SIDU": "Healthcare", "CMPX": "Healthcare", "NN": "Healthcare", "SRAD": "Healthcare",
    "TSLR": "Healthcare", "SGML": "Healthcare",
    # Finance (extended)
    "AXP": "Finance", "SPGI": "Finance", "ICE": "Finance", "AON": "Finance",
    "MMC": "Finance", "USB": "Finance", "ADP": "Finance",
    # Crypto / Crypto-adjacent
    "MARA": "Crypto", "RIOT": "Crypto", "CLSK": "Crypto", "HUT": "Crypto",
    "BITF": "Crypto", "CIFR": "Crypto", "WULF": "Crypto",
    # Education
    "TAL": "Education", "EDU": "Education", "GOTU": "Education", "DAO": "Education",
    # Media / Communication
    "TMUS": "Media", "T": "Media", "VZ": "Media", "CMCSA": "Media",
    # REIT
    "PLD": "REIT", "EQIX": "REIT", "AMT": "REIT", "CCI": "REIT", "SPG": "REIT",
    # Utilities
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities", "AEP": "Utilities",
    # Materials / Chemicals
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials", "ECL": "Materials",
    "VSH": "Materials", "ALM": "Materials", "RUM": "Materials",
    # Industrial (extended)
    "UNP": "Industrial", "CSX": "Industrial", "FDX": "Industrial", "NOC": "Industrial",
    "MMM": "Industrial", "ETN": "Industrial", "EMR": "Industrial", "WM": "Industrial",
    # Round-30 additions — tickers bucketed as "Other" in correlation
    # warnings because they were missing from the map. 3x-leveraged
    # semi ETFs (SOXL/SOXS/SOXX/SMH) belong in Tech for concentration
    # purposes since they all move with the semi index.
    "SOXL": "Tech", "SOXS": "Tech", "SOXX": "Tech", "SMH": "Tech",
    "TSDD": "Tech", "TSLQ": "Consumer", "TSLG": "Consumer",
    # Consumer / retail
    "CHWY": "Consumer", "BBWI": "Consumer", "GME": "Consumer",
    "PTON": "Consumer", "RBLX": "Consumer", "MGM": "Consumer",
    "NCLH": "Consumer", "MAIR": "Consumer",
    # Tech additions (small/mid cap)
    "BB": "Tech", "POET": "Tech", "LUMN": "Tech", "SIRI": "Tech",
    "SOUN": "Tech", "TEAM": "Tech", "KVYO": "Tech", "DELL": "Tech",
    "SMCI": "Tech", "GTLB": "Tech", "PINS": "Tech", "TTD": "Tech",
    "CELH": "Consumer",
    # Semiconductor names
    "GFS": "Tech", "MU": "Tech", "AMKR": "Tech", "ON": "Tech",
    "VIAV": "Tech", "SNDK": "Tech",
    # Crypto / crypto-adjacent (MARA / RIOT / WULF / CIFR already in
    # the Crypto bucket above — only add new ones here)
    "MSTR": "Finance", "BMNR": "Finance",
    "BTDR": "Finance", "IREN": "Finance", "GLXY": "Finance",
    # Healthcare / biotech
    "TSHA": "Healthcare", "ENVX": "Healthcare", "OGN": "Healthcare",
    # Quantum / emerging tech
    "IONQ": "Tech", "QBTS": "Tech", "QUBT": "Tech", "RGTI": "Tech",
    "ONDS": "Tech", "SMR": "Industrial", "OKLO": "Industrial",
    "ASPI": "Materials", "EOSE": "Industrial", "UAMY": "Materials",
    # Defense / space / satellite
    "LUNR": "Industrial", "RKLB": "Industrial", "RCAT": "Industrial",
    "SATL": "Industrial", "TRVI": "Healthcare", "RDW": "Industrial",
    # Rare earth / materials
    "USAR": "Materials", "NEXT": "Energy",
    # Finance / trading
    "BULL": "Finance", "CRWV": "Tech", "FIGR": "Finance", "XP": "Finance",
    "SBET": "Finance", "PURR": "Consumer", "NB": "Finance",
    # Energy / oilfield
    "FRO": "Energy", "HAL": "Energy", "SM": "Energy", "AG": "Materials",
    "DOW": "Materials", "BF.B": "Consumer",
    # Apparel / travel
    "VFC": "Consumer", "UAL": "Industrial", "W": "Consumer",
    # Round-58 additions — tickers surfaced as "Other" in the user's
    # 2026-04-22 /api/data dump that have well-known sectors. Adding
    # here closes the gap between the screener's enriched fields and
    # correlation-guard rendering.
    # Tech (semiconductors + SaaS + networking)
    "CRDO": "Tech",    # semiconductor networking (Credo Technology)
    "FSLY": "Tech",    # edge cloud / CDN (Fastly)
    "MRVL": "Tech",    # semiconductors (Marvell)
    "ALAB": "Tech",    # AI semiconductors (Astera Labs)
    "ANET": "Tech",    # data-center networking (Arista)
    "LRCX": "Tech",    # semiconductor equipment (Lam Research)
    "NBIS": "Tech",    # AI cloud (Nebius Group)
    "APH": "Tech",     # connectors / interconnects (Amphenol)
    "APLD": "Tech",    # AI data centers (Applied Digital)
    "SMTC": "Tech",    # mixed-signal semi (Semtech)
    "CTSH": "Tech",    # IT services (Cognizant)
    "RELX": "Tech",    # information/analytics (RELX)
    # Finance
    "COF": "Finance", "CG": "Finance",
    # Industrial (defense / logistics / airlines / heavy eq)
    "LUV": "Industrial", "DAL": "Industrial", "ALK": "Industrial", "CCL": "Consumer",
    "JETS": "Industrial", "CARR": "Industrial",
    # Healthcare
    "ERAS": "Healthcare", "IBRX": "Healthcare",
    # Energy / utilities
    "EQT": "Energy", "XEL": "Utilities",
    # Consumer
    "GAP": "Consumer", "LEVI": "Consumer", "TSCO": "Consumer", "KSS": "Consumer",
    "CPNG": "Consumer", "STUB": "Consumer", "OPEN": "Consumer", "BE": "Industrial",
    # Crypto / fintech
    "RKT": "Finance", "IBIT": "Crypto", "SARO": "Finance",
    # Special situations
    "FRMI": "Finance", "INFQ": "Finance", "AMPX": "Industrial",
    # Satellite / aerospace
    "ASTS": "Industrial", "SATS": "Industrial",
    # Healthcare ETFs / basket
    "MSOS": "Healthcare",  # cannabis ETF
    # Nuclear / uranium
    "NXE": "Energy", "JOBY": "Industrial", "ACHR": "Industrial",
    # Industrial / materials
    "IP": "Materials",
    # Paper / other
    "PBI": "Industrial",  # Pitney Bowes
    "TEL": "Industrial",  # TE Connectivity (sensors/connectors)
    "TE": "Industrial",
    # Finance / insurance
    "BORR": "Energy",  # Borr Drilling (oilfield)
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


# --- Round-24: HTTP timeout defaults ---
# Centralised so one module doesn't wait 30s while another waits 10s for
# the same Alpaca endpoint. Pick the shortest value that still gives the
# upstream time to respond under normal conditions.
#
#   HTTP_TIMEOUT_FAST      — trade-critical paths (orders, quotes). Must
#                            return fast or we'd rather fail and retry.
#   HTTP_TIMEOUT_DEFAULT   — everything not price-sensitive (account,
#                            positions, news, options chains).
#   HTTP_TIMEOUT_SLOW      — explicitly-slow endpoints (yfinance history,
#                            Gemini LLM calls, SEC EDGAR scrapes).
#
# Callers can still override for their specific needs — this is just the
# default so accidental omission doesn't leave a socket-default 300s
# timeout hanging a scheduler thread.
HTTP_TIMEOUT_FAST = 5
HTTP_TIMEOUT_DEFAULT = 10
HTTP_TIMEOUT_SLOW = 20
