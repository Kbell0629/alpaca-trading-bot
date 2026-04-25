"""Round-61 pt.68 — bid-ask spread filter.

The screener checks volume to filter "illiquid" stocks but volume
alone misses one important class of bad fills: tickers with high
volume *and* a wide bid-ask spread. A small-cap with $10M ADV but
a 5% spread costs you 5% on entry alone — before the strategy even
plays out.

This module rejects any pick whose ``(ask - bid) / mid`` exceeds
``max_spread_pct`` (default 0.5%). Pure: caller supplies a
``{symbol: {"bid": x, "ask": y}}`` map.

Use:
    >>> from spread_filter import apply_spread_filter
    >>> picks = [{"symbol": "AAPL"}, {"symbol": "ILLIQ"}]
    >>> quotes = {"AAPL": {"bid": 200.00, "ask": 200.05},
    ...           "ILLIQ": {"bid": 5.00, "ask": 5.30}}  # 6% spread
    >>> apply_spread_filter(picks, quotes)
    # ILLIQ tagged _wide_spread=True, will_deploy=False
"""
from __future__ import annotations

from typing import Mapping, Optional


# Default cutoff: a spread under 0.5% of mid is "tight" by retail
# standards. Anything wider is a liquidity tax we don't want to pay.
DEFAULT_MAX_SPREAD_PCT: float = 0.5


def compute_spread_pct(bid: float, ask: float) -> Optional[float]:
    """Return ``(ask - bid) / mid * 100`` in percent.
    None on bad inputs (zero/negative bid or ask, ask <= bid,
    non-numeric)."""
    try:
        b = float(bid)
        a = float(ask)
    except (TypeError, ValueError):
        return None
    if b <= 0 or a <= 0 or a < b:
        return None
    mid = (a + b) / 2
    if mid <= 0:
        return None
    return (a - b) / mid * 100


def is_spread_tight(bid: float, ask: float,
                     max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
                     ) -> bool:
    """Return True if the spread is at-or-below ``max_spread_pct``.
    Bad inputs return True (fail-open) so a flaky quote feed
    doesn't drop every pick."""
    pct = compute_spread_pct(bid, ask)
    if pct is None:
        return True
    return pct <= max_spread_pct


def apply_spread_filter(picks: list,
                          quotes: Mapping,
                          *,
                          max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
                          ) -> list:
    """Tag picks whose spread exceeds ``max_spread_pct`` and mark
    them ``will_deploy=False``.

    Args:
      picks: list of pick dicts (mutated in place).
      quotes: ``{symbol: {"bid": float, "ask": float}}``.
      max_spread_pct: cutoff. Default 0.5%.

    Mutates each affected pick:
      * ``_wide_spread = True``
      * ``_spread_pct = <pct>``
      * ``will_deploy = False``
      * Appends ``"wide_spread"`` to ``filter_reasons``.

    Returns ``picks`` for chaining.
    """
    if not picks:
        return picks or []
    if not isinstance(quotes, Mapping):
        return picks
    for p in picks:
        if not isinstance(p, dict):
            continue
        sym = (p.get("symbol") or "").upper()
        if not sym:
            continue
        q = quotes.get(sym)
        if not isinstance(q, Mapping):
            continue
        pct = compute_spread_pct(q.get("bid"), q.get("ask"))
        if pct is None or pct <= max_spread_pct:
            continue
        p["_wide_spread"] = True
        p["_spread_pct"] = round(pct, 3)
        p["will_deploy"] = False
        reasons = p.get("filter_reasons")
        if not isinstance(reasons, list):
            reasons = []
        if "wide_spread" not in reasons:
            reasons.append("wide_spread")
        p["filter_reasons"] = reasons
    return picks
