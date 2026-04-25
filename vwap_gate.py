"""Round-61 pt.59 — VWAP-relative entry gate for breakouts.

Pt.45 + pt.58 added gates against chasing (chase_block 8%, gap
penalty 3-8%). VWAP-relative is a different angle: even when
daily_change is moderate, a stock that has rallied above today's
VWAP is "trading rich" intraday. Buying above VWAP on a 30-min
cycle systematically captures the worst entry of the session.

This gate blocks breakouts where ``price > vwap * (1 + tolerance)``.
For other strategies (mean_reversion, pead, wheel) the gate is a
no-op — they have different entry rationale.

Pure module. Caller fetches today's intraday bars (1-min or 5-min)
via Alpaca and computes VWAP via ``indicators.vwap`` (existing
helper). Then passes the latest VWAP value to ``evaluate_vwap_gate``.
"""
from __future__ import annotations

from typing import Optional


# Tolerance: how far above VWAP we'll still allow the entry. 0.5%
# leaves room for normal intraday wiggle without forcing the bot
# to chase down to VWAP exactly.
DEFAULT_TOLERANCE_PCT: float = 0.5


def compute_vwap_offset_pct(price: float, vwap: float) -> Optional[float]:
    """Return (price - vwap) / vwap * 100 in percent. None on bad
    inputs (zero or negative VWAP, non-numeric)."""
    try:
        p = float(price)
        v = float(vwap)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return (p / v - 1) * 100


def evaluate_vwap_gate(*,
                        strategy: str,
                        price: float,
                        vwap: Optional[float],
                        tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
                        ) -> dict:
    """Decide whether to BLOCK an entry based on the VWAP offset.

    Args:
      strategy: lowercase strategy name. Only ``breakout`` is gated;
        all others return ``allowed=True`` with a "not_breakout"
        skip reason (so callers can log the no-op).
      price: candidate entry price.
      vwap: today's session VWAP (from caller). May be None when the
        data fetch failed — the gate fails OPEN (allowed=True) so
        a flaky data feed never blocks deploys.
      tolerance_pct: how far above VWAP we'll allow. Default 0.5%.

    Returns:
      {
        "allowed": bool,
        "reason": str,        # short label
        "vwap_offset_pct": float | None,
        "vwap": float | None,
      }
    """
    out = {
        "allowed": True, "reason": "ok",
        "vwap_offset_pct": None, "vwap": vwap,
    }
    if (strategy or "").lower() != "breakout":
        out["reason"] = "not_breakout"
        return out
    if vwap is None:
        out["reason"] = "no_vwap_data_fail_open"
        return out
    offset = compute_vwap_offset_pct(price, vwap)
    out["vwap_offset_pct"] = offset
    if offset is None:
        out["reason"] = "bad_vwap_or_price"
        return out
    if offset > tolerance_pct:
        out["allowed"] = False
        out["reason"] = (f"price_above_vwap: {offset:.2f}% > "
                          f"{tolerance_pct}%")
    else:
        out["reason"] = (f"price_within_vwap_tolerance: "
                          f"{offset:.2f}% <= {tolerance_pct}%")
    return out
