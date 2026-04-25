"""Round-61 pt.66 — sector-momentum filter.

A pick can score 700 on a breakout setup, but if its sector is
trending DOWN 15% MoM the long entry is fighting both intra-sector
selling pressure and a regime headwind. The auto-deployer's existing
``regime`` filter looks at SPY breadth, not per-sector rotation.

This module bridges the gap: take a per-sector month-over-month
return map and tag any pick whose sector is trending below
``threshold_pct`` (default −10%). Tagged picks get
``_sector_downtrend=True`` and a ``filter_reasons`` entry so the
dashboard chip pattern surfaces the WHY.

Pure module — caller fetches sector-level returns (e.g. from a
sector-ETF lookup like XLE/XLF/XLK) and passes the dict in. No I/O.

Use:
    >>> from sector_momentum import apply_sector_momentum_filter
    >>> picks = [{"symbol": "XOM", "sector": "Energy", ...}, ...]
    >>> sector_returns = {"Energy": -12.5, "Technology": 8.0}
    >>> apply_sector_momentum_filter(picks, sector_returns)
    # XOM gets _sector_downtrend=True, will_deploy=False
"""
from __future__ import annotations

from typing import Mapping, Optional


# Default threshold: a sector down >10% MoM is a meaningful regime
# headwind. Anything tighter trips on normal pullbacks.
DEFAULT_THRESHOLD_PCT: float = -10.0


# Mapping of sector ETFs to their canonical sector name. Callers can
# fetch the ETFs (cheap, liquid) and translate via this map.
SECTOR_ETF_MAP: dict = {
    "XLE": "Energy",
    "XLF": "Financial Services",
    "XLK": "Technology",
    "XLV": "Healthcare",
    "XLY": "Consumer Cyclical",
    "XLP": "Consumer Defensive",
    "XLI": "Industrials",
    "XLB": "Basic Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}


def compute_pct_return(start_price: float, end_price: float) -> Optional[float]:
    """Return ``(end - start) / start * 100`` in percent. None on bad
    inputs (zero / negative start, non-numeric)."""
    try:
        s = float(start_price)
        e = float(end_price)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    return (e / s - 1) * 100


def build_sector_returns(etf_prices: Mapping) -> dict:
    """Translate ``{etf_symbol: {"start": p1, "end": p2}}`` into
    ``{sector_name: pct_return}`` using ``SECTOR_ETF_MAP``.

    Unknown ETFs are silently ignored. Bad-data entries are skipped.
    """
    out = {}
    if not isinstance(etf_prices, Mapping):
        return out
    for etf, info in etf_prices.items():
        sector = SECTOR_ETF_MAP.get(etf)
        if not sector:
            continue
        if not isinstance(info, Mapping):
            continue
        ret = compute_pct_return(info.get("start"), info.get("end"))
        if ret is None:
            continue
        out[sector] = round(ret, 2)
    return out


def is_sector_in_downtrend(sector: Optional[str],
                            sector_returns: Mapping,
                            threshold_pct: float = DEFAULT_THRESHOLD_PCT,
                            ) -> bool:
    """Return True if `sector` has a return BELOW `threshold_pct`
    (default −10%) in `sector_returns`. Unknown / missing sectors
    return False (fail-open)."""
    if not sector or not isinstance(sector_returns, Mapping):
        return False
    ret = sector_returns.get(sector)
    if ret is None:
        return False
    try:
        return float(ret) < threshold_pct
    except (TypeError, ValueError):
        return False


def apply_sector_momentum_filter(picks: list,
                                    sector_returns: Mapping,
                                    *,
                                    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
                                    only_long: bool = True,
                                    ) -> list:
    """Tag picks whose sector is trending below ``threshold_pct`` MoM.

    Args:
      picks: list of pick dicts (mutated in place).
      sector_returns: ``{sector_name: pct_return}``.
      threshold_pct: cutoff. Default −10% (sector must be DOWN >10%).
      only_long: if True (default), only flag long picks. Short picks
        in down-trending sectors are GOOD setups, leave alone.

    Mutates each affected pick:
      * ``_sector_downtrend = True``
      * ``_sector_return_pct = <return>``
      * ``will_deploy = False``
      * Appends ``"sector_downtrend"`` to ``filter_reasons``.

    Returns ``picks`` for chaining.
    """
    if not picks or not isinstance(sector_returns, Mapping):
        return picks or []
    for p in picks:
        if not isinstance(p, dict):
            continue
        sector = p.get("sector")
        if not is_sector_in_downtrend(sector, sector_returns,
                                        threshold_pct=threshold_pct):
            continue
        # Direction-aware: short picks BENEFIT from sector downtrends.
        if only_long:
            best_strat = (p.get("best_strategy") or "").lower()
            if best_strat == "short_sell":
                continue
        try:
            ret = float(sector_returns.get(sector))
        except (TypeError, ValueError):
            ret = None
        p["_sector_downtrend"] = True
        if ret is not None:
            p["_sector_return_pct"] = round(ret, 2)
        p["will_deploy"] = False
        reasons = p.get("filter_reasons")
        if not isinstance(reasons, list):
            reasons = []
        if "sector_downtrend" not in reasons:
            reasons.append("sector_downtrend")
        p["filter_reasons"] = reasons
    return picks


def explain_sector_returns(sector_returns: Mapping,
                            *,
                            top_n: int = 5) -> str:
    """Human-readable one-liner for the dashboard / logs.

    Returns the top ``top_n`` strongest and weakest sectors. Empty
    input returns "(no sector data)"."""
    if not sector_returns:
        return "(no sector data)"
    items = sorted(sector_returns.items(), key=lambda kv: kv[1], reverse=True)
    strongest = items[:top_n]
    weakest = items[-top_n:][::-1]
    s = ", ".join(f"{k}={v:+.1f}%" for k, v in strongest)
    w = ", ".join(f"{k}={v:+.1f}%" for k, v in weakest)
    return f"Strongest: {s} | Weakest: {w}"
