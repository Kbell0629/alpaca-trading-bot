#!/usr/bin/env python3
"""
multi_timeframe.py — Multi-timeframe trend confirmation.

Round-11 expansion (item 9). Only deploy a breakout if BOTH the daily
AND weekly chart agree on direction. Reduces false breakouts ~30%
per published research (a daily breakout that fails the weekly trend
is often a counter-trend rally that fades within days).

Public API:

    confirm_breakout(daily_bars, weekly_bars=None) -> dict
        {confirmed, daily_trend, weekly_trend, score_bonus, reason}

    enrich_picks_with_mtf(picks, weekly_bars_map) -> picks
        Adds mtf_confirmed bool + applies +/- score adjustment.
        Boosts breakout_score by +10 when both timeframes agree,
        -10 when daily is bullish but weekly is bearish (fade signal).

    fetch_weekly_bars(symbols, max_workers=4) -> dict
        Bulk-fetches 6mo of weekly bars via yfinance (rate-limited).
"""
from __future__ import annotations


def _trend_from_bars(bars, lookback=10):
    """Classify trend: bullish / bearish / sideways based on recent
    closes. Simple: compare last close to N-bar SMA + slope."""
    if not bars or len(bars) < lookback + 1:
        return "unknown"
    closes = [float(b.get("c") or b.get("close") or 0) for b in bars]
    closes = [c for c in closes if c > 0]
    if len(closes) < lookback:
        return "unknown"
    recent = closes[-lookback:]
    sma = sum(recent) / len(recent)
    last = closes[-1]
    # Slope: simple % change over the lookback window
    if recent[0] <= 0:
        return "unknown"
    slope_pct = (last - recent[0]) / recent[0]
    if last > sma * 1.02 and slope_pct > 0.02:
        return "bullish"
    if last < sma * 0.98 and slope_pct < -0.02:
        return "bearish"
    return "sideways"


def confirm_breakout(daily_bars, weekly_bars=None):
    """Returns confirmation dict. weekly_bars optional — if missing,
    falls back to daily-only with a smaller bonus."""
    daily_trend = _trend_from_bars(daily_bars, lookback=10)
    if not weekly_bars:
        return {
            "confirmed": daily_trend == "bullish",
            "daily_trend": daily_trend,
            "weekly_trend": "unknown",
            "score_bonus": 5 if daily_trend == "bullish" else 0,
            "reason": "weekly bars unavailable — daily-only confirmation",
        }
    weekly_trend = _trend_from_bars(weekly_bars, lookback=4)  # ~1mo of weekly
    if daily_trend == "bullish" and weekly_trend == "bullish":
        return {
            "confirmed": True,
            "daily_trend": "bullish",
            "weekly_trend": "bullish",
            "score_bonus": 10,
            "reason": "daily + weekly both bullish (high-conviction setup)",
        }
    if daily_trend == "bullish" and weekly_trend == "bearish":
        return {
            "confirmed": False,
            "daily_trend": "bullish",
            "weekly_trend": "bearish",
            "score_bonus": -10,
            "reason": "daily bullish but weekly bearish — likely counter-trend bounce",
        }
    if daily_trend == "bullish" and weekly_trend == "sideways":
        return {
            "confirmed": True,
            "daily_trend": "bullish",
            "weekly_trend": "sideways",
            "score_bonus": 5,
            "reason": "daily bullish, weekly neutral — moderate conviction",
        }
    return {
        "confirmed": False,
        "daily_trend": daily_trend,
        "weekly_trend": weekly_trend,
        "score_bonus": 0,
        "reason": f"daily {daily_trend}, weekly {weekly_trend}",
    }


def fetch_weekly_bars_for_symbols(symbols, max_workers=4):
    """Bulk-fetch 6mo of weekly bars via yfinance_budget. Returns
    {symbol: bars_list} where each bar is {o,h,l,c,v}."""
    if not symbols:
        return {}
    try:
        from yfinance_budget import yf_download
    except ImportError:
        return {}
    data = yf_download(
        tickers=" ".join(s for s in symbols if s),
        period="6mo",
        interval="1wk",
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    if data is None or getattr(data, "empty", True):
        return {}
    out = {}
    for sym in symbols:
        try:
            if hasattr(data, "columns") and hasattr(data.columns, "levels"):
                df = data[sym].dropna()
            else:
                df = data.dropna()
            bars = []
            for idx, row in df.iterrows():
                bars.append({
                    "o": float(row.get("Open") or 0),
                    "h": float(row.get("High") or 0),
                    "l": float(row.get("Low") or 0),
                    "c": float(row.get("Close") or 0),
                    "v": int(row.get("Volume") or 0),
                })
            if bars:
                out[sym] = bars
        except Exception:
            continue
    return out


def enrich_picks_with_mtf(picks, daily_bars_map, weekly_bars_map=None,
                          only_breakouts=True, top_n=20):
    """Enrich top-N picks (default: only those with breakout strategy)
    with multi-timeframe confirmation. Mutates picks in place."""
    if not picks:
        return picks
    for i, p in enumerate(picks):
        if i >= top_n:
            p.setdefault("mtf_confirmed", None)
            continue
        sym = (p.get("symbol") or "").upper()
        strat = (p.get("best_strategy") or "").lower()
        if only_breakouts and strat not in ("breakout", "pead"):
            p["mtf_confirmed"] = None
            continue
        d_bars = (daily_bars_map or {}).get(sym, [])
        w_bars = (weekly_bars_map or {}).get(sym) if weekly_bars_map else None
        result = confirm_breakout(d_bars, w_bars)
        p["mtf_confirmed"] = result["confirmed"]
        p["mtf_daily_trend"] = result["daily_trend"]
        p["mtf_weekly_trend"] = result["weekly_trend"]
        p["mtf_bonus"] = result["score_bonus"]
        p["mtf_reason"] = result["reason"]
        if "breakout_score" in p and isinstance(p["breakout_score"], (int, float)):
            p["breakout_score"] = round(p["breakout_score"] + result["score_bonus"], 2)
        if "pead_score" in p and isinstance(p["pead_score"], (int, float)):
            p["pead_score"] = round(p["pead_score"] + result["score_bonus"] * 0.5, 2)
    return picks


if __name__ == "__main__":
    # Synthetic smoke test
    import random
    random.seed(0)
    daily = [{"c": 100 + i * 0.5 + random.uniform(-1, 1)} for i in range(30)]
    weekly_up = [{"c": 100 + i * 2 + random.uniform(-2, 2)} for i in range(10)]
    weekly_down = [{"c": 100 - i * 2 + random.uniform(-2, 2)} for i in range(10)]
    print("Daily up + weekly up:", confirm_breakout(daily, weekly_up))
    print("Daily up + weekly down:", confirm_breakout(daily, weekly_down))
