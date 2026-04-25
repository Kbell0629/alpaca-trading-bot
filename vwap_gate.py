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


def detect_vwap_retest(*,
                        price: float,
                        vwap: Optional[float],
                        prev_price: Optional[float] = None,
                        session_low: Optional[float] = None,
                        ) -> bool:
    """Round-61 pt.66: VWAP retest is the high-quality long pattern
    where price spent the morning UNDER VWAP, then crossed UP through
    it on volume. Different from "chase above VWAP" — the buyer was
    waiting for confirmation, not paying up.

    Heuristic:
      * Current price is at-or-just-above VWAP (within 0.5% above)
      * Either prev_price was BELOW VWAP (cross detected) OR
        session_low was BELOW VWAP (previously underwater)
    """
    try:
        p = float(price)
        v = float(vwap)
    except (TypeError, ValueError):
        return False
    if v <= 0:
        return False
    offset = (p / v - 1) * 100
    # Only consider "at" VWAP (not far below, not chased far above).
    if not (-0.25 <= offset <= 0.5):
        return False
    if prev_price is not None:
        try:
            pp = float(prev_price)
            if pp < v and p >= v:
                return True
        except (TypeError, ValueError):
            pass
    if session_low is not None:
        try:
            sl = float(session_low)
            if sl < v * 0.995:  # session traded materially below VWAP
                return True
        except (TypeError, ValueError):
            pass
    return False


def evaluate_vwap_gate(*,
                        strategy: str,
                        price: float,
                        vwap: Optional[float],
                        tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
                        prev_price: Optional[float] = None,
                        session_low: Optional[float] = None,
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
      prev_price, session_low: optional. When supplied, used by
        ``detect_vwap_retest`` to flag a high-quality cross-up
        pattern. Retests are explicitly ALLOWED with a positive
        signal even if they would otherwise be blocked.

    Returns:
      {
        "allowed": bool,
        "reason": str,        # short label
        "vwap_offset_pct": float | None,
        "vwap": float | None,
        "is_retest": bool,    # pt.66
      }
    """
    out = {
        "allowed": True, "reason": "ok",
        "vwap_offset_pct": None, "vwap": vwap,
        "is_retest": False,
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
    # Pt.66: prefer-retest. If we can detect a recent cross-up
    # through VWAP, allow with a positive label even if the offset
    # is marginally above tolerance.
    if detect_vwap_retest(price=price, vwap=vwap,
                            prev_price=prev_price,
                            session_low=session_low):
        out["is_retest"] = True
        out["allowed"] = True
        out["reason"] = (f"vwap_retest_cross_up: {offset:.2f}% "
                          "(crossed from below)")
        return out
    if offset > tolerance_pct:
        out["allowed"] = False
        out["reason"] = (f"price_above_vwap: {offset:.2f}% > "
                          f"{tolerance_pct}%")
    else:
        out["reason"] = (f"price_within_vwap_tolerance: "
                          f"{offset:.2f}% <= {tolerance_pct}%")
    return out
