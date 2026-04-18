#!/usr/bin/env python3
"""
quality_filter.py — Fundamental quality gate + bullish news detection.

Round-11 Tier 2 additions. Two filters that upgrade stock selection:

1) FUNDAMENTAL QUALITY SCORE
   Uses yfinance to fetch Return on Equity, Debt/Equity, and Free Cash
   Flow direction. Converts them into a 0..30 score bonus applied to
   the pick's scores, and flags "low_quality" stocks for the deployer
   to down-weight.

   Scoring:
     ROE > 15%             -> +10
     ROE > 5% to 15%       -> +5
     ROE negative or <5%   -> -5
     Debt/Equity < 1.0     -> +5
     Debt/Equity 1.0-3.0   -> 0
     Debt/Equity > 3.0     -> -5
     Free Cash Flow >0     -> +10
     Free Cash Flow <=0    -> -10
   Final score clamped to [-30, +30], then applied to breakout_score
   (momentum strategies benefit most) and divided by 2 for others.

   Cached 24h per symbol to stay under yfinance rate limits.

2) BULLISH NEWS CATALYST DETECTION
   Symmetric to the existing bearish-news filter: scans the per-pick
   news items for positive catalyst keywords (upgrade, beats, raises,
   contract win, FDA approval) and returns a +0..15 score bonus.

Public API:

    get_quality_score(symbol, data_dir=None, max_age_hours=24) -> dict
        {roe, debt_equity, fcf_positive, quality_score, quality_tier}

    bullish_news_bonus(news_items) -> dict
        {bonus, matched_keywords, has_bullish_catalyst}

    apply_quality_filter(picks, data_dir=None) -> picks
        Mutates picks: adds quality fields + applies score bonuses
        to breakout/pead/mean_reversion/wheel scores.
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime

try:
    from et_time import now_et
except ImportError:
    def now_et():
        return datetime.now()


# -- Bullish news keywords (symmetric to bearish filter in news_scanner) --
# Each keyword carries a weight. Stacking multiple keywords in one news
# item compounds the bonus up to the cap (15).
BULLISH_KEYWORDS = {
    "upgraded": 3, "upgrade": 3,
    "raised target": 4, "price target raise": 4, "pt raise": 4,
    "beats estimates": 4, "beat estimates": 4, "earnings beat": 5,
    "crushed estimates": 6, "strong earnings": 4,
    "guidance raise": 5, "raises guidance": 5, "raised guidance": 5,
    "fda approval": 7, "fda approved": 7, "breakthrough designation": 6,
    "contract win": 4, "contract award": 4, "signed agreement": 3,
    "strategic partnership": 3, "acquisition target": 5,
    "buyout": 5, "takeover bid": 5,
    "beat expectations": 4, "exceeded expectations": 4,
    "record revenue": 4, "record earnings": 4,
    "positive trial": 6, "phase 3 success": 7, "phase iii success": 7,
    "new high": 2, "52-week high": 2,
}


def _cache_path(data_dir, name):
    base = data_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def _load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path, data):
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except Exception as e:
        print(f"[quality_filter] cache save failed: {e}")


def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Fundamental Quality Score
# ---------------------------------------------------------------------------

def _fetch_fundamentals_live(symbol):
    """Pull ROE, Debt/Equity, FCF direction from yfinance. Returns a
    dict with roe, debt_equity, fcf_positive. Any field it can't
    compute becomes None (not 0 — so the scorer knows to skip it)."""
    # Round-11: rate-limited via yfinance_budget (shared across factor modules)
    try:
        from yfinance_budget import yf_ticker_info
        info = yf_ticker_info(symbol)
    except ImportError:
        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            info = t.info or {}
        except Exception as e:
            return {"roe": None, "debt_equity": None, "fcf_positive": None,
                    "error": str(e)}
    if not info:
        return {"roe": None, "debt_equity": None, "fcf_positive": None,
                "error": "yfinance returned empty or rate-limited"}

    # yfinance key variations across versions — try several
    roe = (info.get("returnOnEquity") or info.get("returnOnEquityTTM")
           or info.get("roe"))
    debt_equity = (info.get("debtToEquity") or info.get("debt_to_equity"))
    # Debt/Equity from yfinance is sometimes expressed as % (300 = 300%)
    # and sometimes as ratio (3.0). Normalize: if >10 assume it's a percent.
    if debt_equity is not None:
        try:
            de = float(debt_equity)
            debt_equity = de / 100.0 if de > 10 else de
        except (ValueError, TypeError):
            debt_equity = None
    # FCF — yfinance exposes freeCashflow (can be None for newer IPOs)
    fcf = info.get("freeCashflow")
    fcf_positive = None
    if fcf is not None:
        try:
            fcf_positive = float(fcf) > 0
        except (ValueError, TypeError):
            fcf_positive = None

    return {
        "roe": _safe_float(roe) if roe is not None else None,
        "debt_equity": _safe_float(debt_equity) if debt_equity is not None else None,
        "fcf_positive": fcf_positive,
    }


def _score_fundamentals(data):
    """Convert fundamentals to a -30..+30 score. Missing fields
    contribute 0 (neutral, neither penalty nor bonus)."""
    score = 0
    roe = data.get("roe")
    if roe is not None:
        if roe > 0.15:
            score += 10
        elif roe > 0.05:
            score += 5
        elif roe < 0:
            score -= 5
        else:
            score -= 2
    de = data.get("debt_equity")
    if de is not None:
        if de < 1.0:
            score += 5
        elif de <= 3.0:
            score += 0
        else:
            score -= 5
    fcf = data.get("fcf_positive")
    if fcf is True:
        score += 10
    elif fcf is False:
        score -= 10
    return max(-30, min(30, score))


def _classify_tier(score):
    if score >= 15:
        return "A"    # high quality — boost in breakouts + momentum plays
    if score >= 0:
        return "B"    # neutral
    if score >= -15:
        return "C"    # weak quality — de-prioritize
    return "D"        # avoid


def get_quality_score(symbol, data_dir=None, max_age_hours=24):
    """Returns cached quality data, fetching fresh if older than
    max_age_hours. All fields present even on error so callers can
    proceed. Cached in DATA_DIR/quality_cache.json as a flat map."""
    path = _cache_path(data_dir, "quality_cache.json")
    cache = _load_cache(path) or {}
    entry = cache.get(symbol, {})
    if entry.get("computed_at"):
        try:
            computed = datetime.fromisoformat(entry["computed_at"])
            if computed.tzinfo is not None:
                computed = computed.replace(tzinfo=None)
            age_h = (now_et().replace(tzinfo=None) - computed).total_seconds() / 3600.0
            if age_h < max_age_hours:
                return entry
        except (ValueError, TypeError):
            pass
    # Refresh live
    live = _fetch_fundamentals_live(symbol)
    score = _score_fundamentals(live)
    tier = _classify_tier(score)
    result = {
        "symbol": symbol,
        "roe": live.get("roe"),
        "debt_equity": live.get("debt_equity"),
        "fcf_positive": live.get("fcf_positive"),
        "quality_score": score,
        "quality_tier": tier,
        "computed_at": now_et().isoformat(),
    }
    if "error" in live:
        result["error"] = live["error"]
    cache[symbol] = result
    _save_cache(path, cache)
    return result


# ---------------------------------------------------------------------------
# Bullish News Detection
# ---------------------------------------------------------------------------

def bullish_news_bonus(news_items):
    """Scan news headlines + summaries for bullish catalyst keywords.
    Returns {bonus, matched_keywords, has_bullish_catalyst}. Bonus
    capped at +15 to avoid dominating base scores.

    Args:
        news_items: list of dicts with "headline" + optional "summary"
    """
    if not news_items:
        return {"bonus": 0, "matched_keywords": [], "has_bullish_catalyst": False}

    matched = []
    total = 0
    for item in news_items:
        text = " ".join([
            str(item.get("headline", "") or ""),
            str(item.get("summary", "") or ""),
            str(item.get("title", "") or ""),
        ]).lower()
        if not text.strip():
            continue
        item_matched = []
        for kw, weight in BULLISH_KEYWORDS.items():
            if kw in text:
                item_matched.append(kw)
                total += weight
        matched.extend(item_matched)

    bonus = min(15, total)
    return {
        "bonus": bonus,
        "matched_keywords": list(set(matched)),
        "has_bullish_catalyst": bonus >= 6,  # "strong" bullish signal
    }


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def apply_quality_filter(picks, data_dir=None, news_map=None,
                          only_top_n=30):
    """Mutates picks: adds quality + bullish_news fields and applies
    score adjustments to the existing per-strategy scores.

    Args:
        picks: sorted list of pick dicts (best-scored first)
        data_dir: for cache
        news_map: {symbol: [news_items]} optional — if provided, bullish
                  bonus applied on top
        only_top_n: skip fundamentals lookup beyond this (rate-limit
                    yfinance calls)

    For each top-N pick, adds:
        quality_score, quality_tier, roe, debt_equity, fcf_positive
        bullish_bonus, bullish_keywords, has_bullish_catalyst
    And adjusts:
        breakout_score  += quality_score + bullish_bonus
        pead_score      += quality_score + bullish_bonus
        mean_reversion_score += (quality_score / 2) + (bullish_bonus / 2)
        wheel_score     += (quality_score / 2)  (no news bonus - premium
                          is what matters)
    """
    if not picks:
        return picks
    for i, pick in enumerate(picks):
        sym = (pick.get("symbol") or "").upper()
        if not sym:
            continue
        if i >= only_top_n:
            pick["quality_score"] = 0
            pick["quality_tier"] = "?"
            pick["bullish_bonus"] = 0
            continue
        # Fundamental quality
        try:
            q = get_quality_score(sym, data_dir=data_dir)
            pick["quality_score"] = q.get("quality_score", 0)
            pick["quality_tier"] = q.get("quality_tier", "?")
            pick["roe"] = q.get("roe")
            pick["debt_equity"] = q.get("debt_equity")
            pick["fcf_positive"] = q.get("fcf_positive")
        except Exception as e:
            pick["quality_score"] = 0
            pick["quality_tier"] = "?"
            pick["quality_error"] = str(e)
        # Bullish news (if news feed available)
        news_items = (news_map or {}).get(sym, []) if news_map else []
        nb = bullish_news_bonus(news_items)
        pick["bullish_bonus"] = nb["bonus"]
        pick["bullish_keywords"] = nb["matched_keywords"]
        pick["has_bullish_catalyst"] = nb["has_bullish_catalyst"]
        # Apply adjustments
        qs = pick.get("quality_score", 0)
        bb = pick.get("bullish_bonus", 0)
        for k in ("breakout_score", "pead_score"):
            if k in pick and isinstance(pick[k], (int, float)):
                pick[k] = round(pick[k] + qs + bb, 2)
        for k in ("mean_reversion_score",):
            if k in pick and isinstance(pick[k], (int, float)):
                pick[k] = round(pick[k] + (qs / 2) + (bb / 2), 2)
        for k in ("wheel_score",):
            if k in pick and isinstance(pick[k], (int, float)):
                pick[k] = round(pick[k] + (qs / 2), 2)
    return picks


if __name__ == "__main__":
    # Smoke test
    r = get_quality_score("AAPL")
    print("AAPL quality:", json.dumps(r, indent=2, default=str))
    r2 = bullish_news_bonus([
        {"headline": "Apple upgraded to Buy, raised target to $250"},
        {"headline": "Apple beats Q4 estimates, raises guidance"},
    ])
    print("Bullish news:", r2)
