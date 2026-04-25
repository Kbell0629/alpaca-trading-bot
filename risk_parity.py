"""Round-61 pt.64 — risk-parity strategy weight allocator.

Currently every strategy in the bot competes on equal footing —
each gets a slot in the auto-deployer's `top_n` and a slot in the
sector-cap regardless of how volatile its realised P&L has been.
A high-variance strategy can hog deploys with the same headcount
as a steady-Eddie one, blowing up portfolio variance.

Risk-parity inverts the relationship: weight each strategy by
1 / σ(realised_pnl) so steadier strategies get bigger weight.

Pure module — caller passes a journal-shaped dict and gets back
``{strategy: weight}`` summing to 1.0. Designed for the dashboard
to surface (read-only) and a future weighted-deploy mode.
"""
from __future__ import annotations

from typing import Mapping, Optional

import math


# Strategies that we allocate weights for. Anything outside this set
# is ignored (legacy / one-off strategies don't need risk-parity).
RISK_PARITY_STRATEGIES = (
    "breakout",
    "mean_reversion",
    "trailing_stop",
    "wheel",
    "pead",
    "short_sell",
    "copy_trading",
)

MIN_TRADES_FOR_PARITY: int = 5
DEFAULT_FALLBACK_VOL: float = 1.0     # σ assumption for strategies
                                       # below MIN_TRADES_FOR_PARITY


def _stdev(values):
    """Sample stdev. Returns 0.0 if <2 values, else math.sqrt of
    population variance / (n-1)."""
    if not values:
        return 0.0
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def compute_strategy_volatility(journal: Optional[Mapping]) -> dict:
    """Return ``{strategy: {"trade_count": int, "stdev_pnl": float,
    "mean_pnl": float}}`` for each strategy with at least
    MIN_TRADES_FOR_PARITY closed trades.

    Strategies not in ``RISK_PARITY_STRATEGIES`` are skipped.
    """
    out = {}
    if not isinstance(journal, Mapping):
        return out
    trades = journal.get("trades") or []
    if not isinstance(trades, list):
        return out
    by_strategy = {}
    for t in trades:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        strat = (t.get("strategy") or "").lower()
        if strat not in RISK_PARITY_STRATEGIES:
            continue
        try:
            pnl = float(t.get("pnl"))
        except (TypeError, ValueError):
            continue
        by_strategy.setdefault(strat, []).append(pnl)
    for strat, pnls in by_strategy.items():
        out[strat] = {
            "trade_count": len(pnls),
            "stdev_pnl": round(_stdev(pnls), 4),
            "mean_pnl": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        }
    return out


def compute_risk_parity_weights(journal: Optional[Mapping],
                                  *,
                                  strategies=RISK_PARITY_STRATEGIES,
                                  min_trades: int = MIN_TRADES_FOR_PARITY,
                                  fallback_vol: float = DEFAULT_FALLBACK_VOL,
                                  ) -> dict:
    """Return ``{strategy: weight}`` for the requested ``strategies``,
    weighted inversely by σ(P&L). Weights sum to 1.0.

    Strategies with < ``min_trades`` closed trades use ``fallback_vol``
    so they get a default slice rather than dropping out — gives new
    strategies a chance to accumulate samples.

    Empty / no-data → equal-weighted fallback so callers can still
    call this safely and get a sensible default.
    """
    vols = compute_strategy_volatility(journal)
    inv_vols = {}
    for strat in strategies:
        info = vols.get(strat) or {}
        cnt = int(info.get("trade_count") or 0)
        sigma = float(info.get("stdev_pnl") or 0)
        if cnt < min_trades or sigma <= 0:
            sigma = fallback_vol
        if sigma <= 0:
            continue
        inv_vols[strat] = 1.0 / sigma
    total = sum(inv_vols.values())
    if total <= 0:
        # Fallback: equal weights across `strategies`.
        n = len(strategies)
        if n == 0:
            return {}
        eq = round(1.0 / n, 4)
        return {s: eq for s in strategies}
    return {s: round(iv / total, 4) for s, iv in inv_vols.items()}


def explain_weights(weights: Mapping) -> str:
    """Human-readable one-liner for the dashboard / logs."""
    if not weights:
        return "(no weights computed)"
    parts = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    return ", ".join(f"{s}={w:.2%}" for s, w in parts)
