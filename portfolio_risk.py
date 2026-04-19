#!/usr/bin/env python3
"""
portfolio_risk.py — Beta-adjusted exposure + drawdown sizing + correlation.

Round-11 expansion items 11-13. Three risk-management upgrades that
work together to cap real-money downside:

ITEM 11: BETA-ADJUSTED EXPOSURE
  Current cap is "60% invested." But $50k in SOXL (3× beta) is
  effectively $150k of S&P exposure. Compute portfolio beta-adjusted
  exposure and block new high-beta entries when it gets too high.

ITEM 12: DRAWDOWN-ADAPTIVE SIZING
  Reduce position sizes after losses, increase after wins.
  Not Martingale (risky) — more like:
    drawdown < 2%   → 100% of normal size
    drawdown 2-5%   → 75%
    drawdown 5-10%  → 50%
    drawdown > 10%  → 25%
  Smooths the equity curve in a losing streak.

ITEM 13: CORRELATION-AWARE DEPLOYS
  Already have a sector cap. Better: compute pairwise correlation
  between candidate symbol and each existing position. Block if
  the average correlation > 0.7 — catches "all my positions move
  together" beyond just sector tagging.

Public API:

    portfolio_beta(positions, beta_map=None) -> float
        Weighted-average beta across all positions.
    beta_adjusted_exposure(positions, portfolio_value, beta_map=None) -> dict
        {invested_pct, beta_weighted_pct, regime, block_high_beta}
    drawdown_size_multiplier(equity_history) -> float
        0.25..1.0 multiplier based on current drawdown from peak.
    avg_correlation(symbol, position_symbols, bars_map) -> float
        Average Pearson correlation of symbol's daily returns vs
        each existing position's daily returns over 30 days.
    should_block_correlation(symbol, position_symbols, bars_map,
                              max_avg_corr=0.7) -> tuple
        (block: bool, reason: str)

All functions stdlib-only. Pearson correlation hand-rolled (no scipy).
"""
from __future__ import annotations
import math
from datetime import datetime, timedelta


# Conservative beta defaults for common tickers. Used when we don't
# have live beta data. Leveraged ETFs are explicit (3x beta = 3.0).
DEFAULT_BETA_MAP = {
    "SPY": 1.0, "QQQ": 1.15, "IWM": 1.25, "DIA": 0.95,
    # Leveraged ETFs (3x)
    "SOXL": 3.0, "TQQQ": 3.0, "UPRO": 3.0, "SPXL": 3.0,
    "SOXS": -3.0, "SQQQ": -3.0, "SPXU": -3.0,
    # 2x leveraged
    "SSO": 2.0, "QLD": 2.0, "DDM": 2.0,
    # Sector ETFs (close to 1.0)
    "XLK": 1.15, "XLF": 1.1, "XLE": 1.2, "XLV": 0.85,
    "XLY": 1.1, "XLP": 0.6, "XLI": 1.05, "XLU": 0.4,
    "XLB": 1.1, "XLRE": 0.9, "XLC": 1.0,
    # High-beta tech
    "TSLA": 2.0, "NVDA": 1.7, "META": 1.4, "AMD": 1.8,
    # Stable mega-caps
    "AAPL": 1.2, "MSFT": 1.1, "GOOGL": 1.1, "AMZN": 1.3,
    # Low-beta defensives
    "JNJ": 0.6, "KO": 0.55, "PG": 0.5, "WMT": 0.55, "VZ": 0.45,
    # Crypto-adjacent (high beta)
    "MARA": 4.0, "RIOT": 3.5, "CLSK": 3.5, "MSTR": 2.5, "COIN": 2.5,
}


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


# Phase 3 of the float->Decimal migration (see docs/DECIMAL_MIGRATION_PLAN.md).
# Scope narrower than originally planned: only the money-weighted math in
# portfolio_beta + beta_adjusted_exposure gets Decimal-internal treatment.
# Ratios/statistics elsewhere in this module (correlation, drawdown %,
# Sharpe-like calcs) stay as float — they're proportional, not money, so
# Decimal would add complexity without precision benefit.
from decimal import Decimal as _Decimal


def _dec_mv(v, default=_Decimal("0")):
    """Coerce a market-value field to Decimal via str() so the IEEE-754
    imprecision of the input float doesn't contaminate the aggregate."""
    if v is None or v == "":
        return default
    if isinstance(v, _Decimal):
        return v
    try:
        return _Decimal(str(v))
    except Exception:
        return default


# ============================================================================
# Item 11: Beta-adjusted exposure
# ============================================================================

def portfolio_beta(positions, beta_map=None):
    """Weighted average beta across positions. Uses |market_value|
    so shorts count their beta exposure correctly.

    Phase-3 migration: market-value weights and their sum run in Decimal
    to prevent drift across N positions. Output is a float (beta is a
    dimensionless statistic)."""
    if not positions:
        return 0.0
    bm = beta_map or DEFAULT_BETA_MAP
    total_mv = _Decimal("0")
    weighted_beta = _Decimal("0")
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        mv = abs(_dec_mv(p.get("market_value", 0)))
        if mv <= 0:
            continue
        beta = _dec_mv(bm.get(sym, 1.0))  # unknown defaults to market-beta 1.0
        total_mv += mv
        weighted_beta += mv * beta
    if total_mv <= 0:
        return 0.0
    return float(weighted_beta / total_mv)


def beta_adjusted_exposure(positions, portfolio_value, beta_map=None):
    """Compute beta-weighted exposure as % of portfolio. The intuition:
    100% in a 3× leveraged ETF = 300% beta-weighted exposure.

    Phase-3 migration: invested and portfolio_value held as Decimal for
    the core aggregate; percentages emit as float (proportional)."""
    pv_d = _dec_mv(portfolio_value)
    if pv_d <= 0:
        return {"invested_pct": 0, "beta_weighted_pct": 0,
                "portfolio_beta": 0, "regime": "unknown",
                "block_high_beta": False}
    invested_d = sum(
        (abs(_dec_mv(p.get("market_value", 0))) for p in positions),
        _Decimal("0"),
    )
    pf_beta = portfolio_beta(positions, beta_map)   # float already
    # beta_pct and invested_pct are percentages — compute as float for
    # downstream display consistency but with Decimal invested/pv so the
    # ratio is drift-free.
    beta_pct = float((invested_d * _dec_mv(pf_beta) / pv_d)) * 100
    invested_pct = float(invested_d / pv_d) * 100
    # Regime classification
    if beta_pct < 50:
        regime, block = "low", False
    elif beta_pct < 100:
        regime, block = "moderate", False
    elif beta_pct < 150:
        regime, block = "high", True  # block new high-beta
    else:
        regime, block = "extreme", True  # block ALL new positions
    return {
        "invested_pct": round(invested_pct, 1),
        "portfolio_beta": round(pf_beta, 2),
        "beta_weighted_pct": round(beta_pct, 1),
        "regime": regime,
        "block_high_beta": block,
        "block_all": regime == "extreme",
    }


def is_high_beta_candidate(symbol, beta_map=None, threshold=1.5):
    """True if this candidate has beta > threshold and would push
    portfolio further into high-beta territory."""
    bm = beta_map or DEFAULT_BETA_MAP
    return bm.get(symbol.upper(), 1.0) >= threshold


# ============================================================================
# Item 12: Drawdown-adaptive sizing
# ============================================================================

def current_drawdown_pct(equity_history):
    """Compute current drawdown from recent peak. equity_history is a
    list of {date, equity} dicts in chronological order. Returns
    drawdown as positive percent (5.2 = down 5.2% from peak)."""
    if not equity_history or len(equity_history) < 2:
        return 0.0
    equities = [_safe_float(e.get("equity") or e.get("portfolio_value"))
                for e in equity_history]
    equities = [e for e in equities if e > 0]
    if not equities:
        return 0.0
    peak = max(equities)
    current = equities[-1]
    if peak <= 0:
        return 0.0
    dd = (peak - current) / peak * 100
    return max(0.0, dd)


def drawdown_size_multiplier(equity_history):
    """Scale position size by current drawdown:
      < 2%   → 1.00 (full size)
      2-5%   → 0.75
      5-10%  → 0.50
      > 10%  → 0.25
    Returns 1.0 if no history (cold start)."""
    dd = current_drawdown_pct(equity_history)
    if dd < 2:
        return 1.0
    if dd < 5:
        return 0.75
    if dd < 10:
        return 0.5
    return 0.25


# ============================================================================
# Item 13: Correlation-aware deploys
# ============================================================================

def _pearson(a, b):
    """Pearson correlation of two equal-length lists."""
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    a = a[-n:]
    b = b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a = math.sqrt(sum((a[i] - mean_a) ** 2 for i in range(n)))
    den_b = math.sqrt(sum((b[i] - mean_b) ** 2 for i in range(n)))
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def _bars_to_returns(bars):
    """Daily log returns from a bars list."""
    if not bars or len(bars) < 2:
        return []
    closes = [_safe_float(b.get("c") or b.get("close")) for b in bars]
    closes = [c for c in closes if c > 0]
    if len(closes) < 2:
        return []
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    return rets


def avg_correlation(symbol, position_symbols, bars_map):
    """Average pairwise correlation of `symbol`'s returns vs each of
    `position_symbols` over the bars in bars_map.

    Returns 0..1 (or negative for inverse). bars_map: {sym: bars_list}.
    Symbols missing from bars_map are skipped."""
    sym_rets = _bars_to_returns(bars_map.get(symbol, []))
    if not sym_rets:
        return 0.0
    corrs = []
    for ps in position_symbols:
        if ps == symbol:
            continue
        ps_rets = _bars_to_returns(bars_map.get(ps, []))
        if not ps_rets:
            continue
        c = _pearson(sym_rets, ps_rets)
        corrs.append(c)
    return sum(corrs) / len(corrs) if corrs else 0.0


def should_block_correlation(symbol, position_symbols, bars_map,
                              max_avg_corr=0.7):
    """Returns (block: bool, reason: str). Blocks if avg correlation
    with existing positions > max_avg_corr."""
    if not position_symbols:
        return False, "no positions to compare"
    avg = avg_correlation(symbol, position_symbols, bars_map)
    if avg > max_avg_corr:
        return True, (f"avg correlation {avg:.2f} > {max_avg_corr} "
                      f"with {len(position_symbols)} existing positions")
    return False, f"avg correlation {avg:.2f} acceptable"


if __name__ == "__main__":
    # Smoke test
    positions = [
        {"symbol": "SOXL", "market_value": 10000},
        {"symbol": "AAPL", "market_value": 5000},
    ]
    print("Beta exposure:", beta_adjusted_exposure(positions, 100000))
    print("Drawdown mult (5% DD):", drawdown_size_multiplier(
        [{"equity": 100000}, {"equity": 95000}]
    ))
    bars_a = [{"c": 100 + i * 0.1} for i in range(30)]
    bars_b = [{"c": 100 + i * 0.12} for i in range(30)]  # highly correlated
    print("Pearson:", _pearson(_bars_to_returns(bars_a), _bars_to_returns(bars_b)))
