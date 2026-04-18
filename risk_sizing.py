#!/usr/bin/env python3
"""
risk_sizing.py — Volatility-aware position sizing + stop-loss helpers.

Round-11 Tier 1 addition. Replaces the bot's fixed-percentage stops with
ATR (Average True Range) based stops so volatile leveraged ETFs (SOXL,
TQQQ) don't get stopped out on normal noise and quiet utility names
don't give up 10% before their stop fires.

Public API:

    compute_atr(bars, period=14) -> float
        Average True Range over N days. Returns absolute $ per share.

    atr_pct(bars, period=14, current_price=None) -> float
        ATR expressed as a percent of current price. 0..1 decimal.

    atr_based_stop_pct(bars, multiplier=2.5, floor_pct=0.05,
                        cap_pct=0.15) -> float
        Returns the stop-loss percent to use for a position. Clamped
        between `floor_pct` (too-tight names) and `cap_pct` (blow-out
        volatility names).

    volatility_position_multiplier(bars, base_atr_pct=0.02) -> float
        For Kelly-lite sizing: returns a 0..1 multiplier to scale
        position size DOWN for high-volatility names (keeps risk dollars
        roughly constant across the book).

All functions are total-paranoia-safe — bars missing or malformed
returns conservative defaults so the caller falls back to the old
fixed 10% stop.
"""
from __future__ import annotations
from typing import Sequence


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def compute_atr(bars: Sequence[dict], period: int = 14) -> float:
    """True Range = max(high-low, |high-prev_close|, |low-prev_close|).
    ATR = simple moving average of TR over `period` bars. Returns 0 on
    insufficient data (caller should fall back to fixed stops)."""
    if not bars or len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        b = bars[i]
        prev = bars[i - 1]
        high = _safe_float(b.get("h"))
        low = _safe_float(b.get("l"))
        prev_close = _safe_float(prev.get("c"))
        if high <= 0 or low <= 0 or prev_close <= 0:
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    # Simple average of the last `period` TRs (classic Wilder uses EMA
    # but SMA is within 5-10% and simpler/more transparent for our use).
    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else 0.0


def atr_pct(bars: Sequence[dict], period: int = 14, current_price: float = None) -> float:
    """ATR as a percent of current price (decimal, 0..1)."""
    atr = compute_atr(bars, period)
    if atr <= 0:
        return 0.0
    if current_price is None or current_price <= 0:
        # Fall back to latest close from bars.
        try:
            current_price = _safe_float(bars[-1].get("c"))
        except (IndexError, AttributeError):
            return 0.0
    if current_price <= 0:
        return 0.0
    return atr / current_price


def atr_based_stop_pct(bars: Sequence[dict],
                        multiplier: float = 2.5,
                        floor_pct: float = 0.05,
                        cap_pct: float = 0.15,
                        current_price: float = None) -> float:
    """Return the stop-loss percent to use for a position.

    - `multiplier × ATR%` is the raw stop distance.
    - `floor_pct` (5% default) prevents absurdly tight stops on ultra-
      low-vol names (utilities, bond ETFs) that would trigger on
      routine bid-ask spread.
    - `cap_pct` (15% default) prevents runaway stops on blow-out
      volatility (leveraged ETFs during a VIX spike, small-caps on
      halt rumors). Anything above 15% is too much capital at risk
      per trade — caller should skip the pick, not widen the stop.

    Returns a decimal (e.g. 0.08 = 8% stop). Returns 0.0 on bad data;
    callers fall back to their strategy's default.
    """
    raw_atr_pct = atr_pct(bars, 14, current_price)
    if raw_atr_pct <= 0:
        return 0.0
    raw_stop = multiplier * raw_atr_pct
    return max(floor_pct, min(cap_pct, raw_stop))


def volatility_position_multiplier(bars: Sequence[dict],
                                     base_atr_pct: float = 0.02) -> float:
    """Kelly-lite position multiplier.

    Scales position size inversely to volatility so a stock with 2×
    the ATR% of the baseline gets half the position size. Keeps
    risk-dollars roughly constant across positions.

    - Returns 1.0 when ATR% == base_atr_pct (the "normal" volatility).
    - Returns <1.0 for higher-vol names (smaller size).
    - Returns >1.0 for lower-vol names, but capped at 1.5 so a boring
      quiet name doesn't eat 15% of the portfolio.
    - Clamped to [0.3, 1.5] so the range stays sane.
    """
    current_atr_pct = atr_pct(bars, 14)
    if current_atr_pct <= 0 or base_atr_pct <= 0:
        return 1.0
    ratio = base_atr_pct / current_atr_pct
    return max(0.3, min(1.5, ratio))


if __name__ == "__main__":
    # Smoke test with synthetic bars
    import random
    random.seed(42)
    price = 100.0
    bars = []
    for _ in range(30):
        c = price + random.uniform(-1.5, 1.5)
        bars.append({"o": price, "h": price + 1.0, "l": c - 1.0, "c": c})
        price = c
    atr = compute_atr(bars)
    print(f"ATR: ${atr:.4f}")
    print(f"ATR%: {atr_pct(bars):.4%}")
    print(f"Stop pct: {atr_based_stop_pct(bars):.4f}")
    print(f"Size mult: {volatility_position_multiplier(bars):.4f}")
