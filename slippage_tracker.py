"""Round-61 pt.80 — realized vs expected slippage tracking.

The pt.47 backtest harness assumes 10 bps of slippage on entry and
exit. If real fills are systematically wider than that, our
backtest expectancy overstates live performance — every dollar
the strategy "earns" in backtest could be eaten by execution
friction.

This module computes the realized slippage from the trade journal
(per-trade `entry_expected_price` / `entry_filled_price` and
`exit_expected_price` / `exit_filled_price` fields) and reports
whether reality matches the backtest assumption.

Pure module — no I/O. Caller passes the journal dict and gets
back a structured summary.

The journal contract:
  * ``entry_expected_price``: what the screener (or stop/target)
    used to decide to enter
  * ``entry_filled_price``: Alpaca's reported `avg_entry_price`
    after the fill
  * ``entry_slippage_bps``: signed bps; POSITIVE means adverse
    (worse than expected — the trader paid more on a buy or
    received less on a sell)
  * Same trio for the exit side

Use:
    >>> from slippage_tracker import compute_slippage_bps
    >>> compute_slippage_bps(100.0, 100.05, side="buy")
    5.0
    >>> compute_slippage_bps(100.0, 100.05, side="sell")
    -5.0

    >>> from slippage_tracker import aggregate_realized_slippage
    >>> agg = aggregate_realized_slippage(journal)
    >>> print(agg["entry_mean_bps"])
"""
from __future__ import annotations

from typing import Mapping, Optional


# Default backtest assumption (matches pt.47's slippage_bps default
# in run_walk_forward_backtest + production weekly hook).
DEFAULT_ASSUMED_BPS: float = 10.0

# How many trades we need before the comparison is statistically
# meaningful. Below this, surface as "preliminary".
MIN_TRADES_FOR_VERDICT: int = 20


def compute_slippage_bps(expected: float,
                           filled: float,
                           *,
                           side: str = "buy",
                           ) -> Optional[float]:
    """Signed slippage in basis points. POSITIVE = adverse.

    Buy:  filled > expected → positive (paid more)
    Sell: filled < expected → positive (received less)

    Returns None on bad inputs (zero/negative expected, non-numeric).
    """
    try:
        e = float(expected)
        f = float(filled)
    except (TypeError, ValueError):
        return None
    if e <= 0 or f < 0:
        return None
    raw_bps = (f - e) / e * 10000
    side_l = (side or "buy").lower()
    if side_l in ("sell", "short_close"):
        # For a sell, getting LESS than expected is adverse.
        return round(-raw_bps, 2)
    # Default = buy/cover.
    return round(raw_bps, 2)


def _percentile(sorted_values, pct):
    """Inline percentile so we don't need numpy. `sorted_values`
    must already be sorted ascending."""
    if not sorted_values:
        return 0.0
    if pct <= 0:
        return float(sorted_values[0])
    if pct >= 100:
        return float(sorted_values[-1])
    idx = (pct / 100) * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return float(sorted_values[lo]) * (1 - frac) + \
        float(sorted_values[hi]) * frac


def aggregate_realized_slippage(journal: Optional[Mapping]) -> dict:
    """Walk closed trades that have slippage fields populated and
    summarise. Returns:
        {
          "entry_count": int,
          "entry_mean_bps": float,
          "entry_p50_bps": float,
          "entry_p90_bps": float,
          "entry_total_dollars": float,
          "exit_count": int,
          "exit_mean_bps": float,
          "exit_p50_bps": float,
          "exit_p90_bps": float,
          "exit_total_dollars": float,
          "by_strategy": {strategy: {...same shape, entry-side only}},
        }

    Only counts trades that recorded BOTH the expected and filled
    sides. Trades without the slippage fields fall through silently
    (legacy entries from before this module was wired).
    """
    out = {
        "entry_count": 0, "entry_mean_bps": 0.0,
        "entry_p50_bps": 0.0, "entry_p90_bps": 0.0,
        "entry_total_dollars": 0.0,
        "exit_count": 0, "exit_mean_bps": 0.0,
        "exit_p50_bps": 0.0, "exit_p90_bps": 0.0,
        "exit_total_dollars": 0.0,
        "by_strategy": {},
    }
    if not isinstance(journal, Mapping):
        return out
    trades = journal.get("trades") or []
    if not isinstance(trades, list):
        return out
    entry_bps = []
    exit_bps = []
    by_strat = {}
    for t in trades:
        if not isinstance(t, Mapping):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        # Entry side
        try:
            ebps = t.get("entry_slippage_bps")
            if ebps is not None:
                ebps = float(ebps)
                qty = float(t.get("qty") or 0)
                px = float(t.get("entry_filled_price")
                              or t.get("price") or 0)
                dollar_cost = abs(qty) * px * (ebps / 10000)
                entry_bps.append(ebps)
                strat = (t.get("strategy") or "?").lower()
                bucket = by_strat.setdefault(strat, {
                    "count": 0, "sum_bps": 0.0, "values": []})
                bucket["count"] += 1
                bucket["sum_bps"] += ebps
                bucket["values"].append(ebps)
                out["entry_total_dollars"] += dollar_cost
        except (TypeError, ValueError):
            pass
        # Exit side
        try:
            xbps = t.get("exit_slippage_bps")
            if xbps is not None:
                xbps = float(xbps)
                qty = float(t.get("qty") or 0)
                px = float(t.get("exit_filled_price")
                              or t.get("exit_price") or 0)
                dollar_cost = abs(qty) * px * (xbps / 10000)
                exit_bps.append(xbps)
                out["exit_total_dollars"] += dollar_cost
        except (TypeError, ValueError):
            pass
    if entry_bps:
        sorted_e = sorted(entry_bps)
        out["entry_count"] = len(entry_bps)
        out["entry_mean_bps"] = round(sum(entry_bps) / len(entry_bps), 2)
        out["entry_p50_bps"] = round(_percentile(sorted_e, 50), 2)
        out["entry_p90_bps"] = round(_percentile(sorted_e, 90), 2)
    if exit_bps:
        sorted_x = sorted(exit_bps)
        out["exit_count"] = len(exit_bps)
        out["exit_mean_bps"] = round(sum(exit_bps) / len(exit_bps), 2)
        out["exit_p50_bps"] = round(_percentile(sorted_x, 50), 2)
        out["exit_p90_bps"] = round(_percentile(sorted_x, 90), 2)
    out["entry_total_dollars"] = round(out["entry_total_dollars"], 2)
    out["exit_total_dollars"] = round(out["exit_total_dollars"], 2)
    # Per-strategy (entry side only — keeps the panel compact)
    for strat, b in by_strat.items():
        if b["count"] == 0:
            continue
        out["by_strategy"][strat] = {
            "count": b["count"],
            "mean_bps": round(b["sum_bps"] / b["count"], 2),
        }
    return out


def compare_to_assumption(agg: Mapping,
                            assumed_bps: float = DEFAULT_ASSUMED_BPS,
                            ) -> dict:
    """Compare realized slippage to the backtest assumption. Returns
    ``{state, headline, detail, gap_bps, sample_warning}`` where state
    is "ok" / "warn" / "alert" / "preliminary".

    State semantics:
      * "ok": realized ≤ assumption (backtest is conservative or
        matched)
      * "warn": realized exceeds assumption by ≤ 1.5×
      * "alert": realized exceeds assumption by > 1.5× (backtest
        materially overstates live performance)
      * "preliminary": fewer than MIN_TRADES_FOR_VERDICT trades —
        cannot draw conclusions
    """
    if not isinstance(agg, Mapping):
        return {"state": "preliminary",
                "headline": "no slippage data",
                "detail": "", "gap_bps": 0.0,
                "sample_warning": True}
    n = int(agg.get("entry_count") or 0)
    if n < MIN_TRADES_FOR_VERDICT:
        return {
            "state": "preliminary",
            "headline": (f"Realized slippage: {n} trades "
                          f"(need ≥{MIN_TRADES_FOR_VERDICT} for verdict)"),
            "detail": (f"Mean entry slippage so far: "
                        f"{agg.get('entry_mean_bps', 0)} bps. "
                        f"Backtest assumes {assumed_bps} bps."),
            "gap_bps": 0.0,
            "sample_warning": True,
        }
    realized = float(agg.get("entry_mean_bps") or 0)
    gap = realized - float(assumed_bps)
    if realized <= assumed_bps:
        state = "ok"
        headline = (f"Realized {realized} bps ≤ assumed "
                     f"{assumed_bps} bps — backtest is realistic")
    elif realized <= assumed_bps * 1.5:
        state = "warn"
        headline = (f"Realized {realized} bps > assumed "
                     f"{assumed_bps} bps — gap +{gap:.1f} bps")
    else:
        state = "alert"
        headline = (f"Realized {realized} bps >> assumed "
                     f"{assumed_bps} bps — backtest may overstate by "
                     f"{gap:.0f} bps per trade")
    detail = (f"Entry: mean {agg.get('entry_mean_bps', 0)} bps, "
               f"p50 {agg.get('entry_p50_bps', 0)} bps, "
               f"p90 {agg.get('entry_p90_bps', 0)} bps over "
               f"{n} closed trades. "
               f"Total entry cost: ${agg.get('entry_total_dollars', 0):.2f}.")
    return {
        "state": state, "headline": headline, "detail": detail,
        "gap_bps": round(gap, 2),
        "sample_warning": False,
    }


def annotate_close_with_slippage(close_kwargs: dict,
                                    *,
                                    entry_expected_price: Optional[float] = None,
                                    entry_filled_price: Optional[float] = None,
                                    exit_expected_price: Optional[float] = None,
                                    exit_filled_price: Optional[float] = None,
                                    side: str = "buy",
                                    ) -> dict:
    """Helper: when ``record_trade_close(extra=...)`` is being built,
    add the slippage fields if both expected + filled prices are
    available. Mutates and returns ``close_kwargs``.

    ``side`` is the ENTRY side ("buy" for longs, "sell" for shorts).
    The exit side is the opposite.
    """
    extra = close_kwargs.setdefault("extra", {}) if isinstance(
        close_kwargs.get("extra"), dict) else {}
    if entry_expected_price is not None:
        extra["entry_expected_price"] = float(entry_expected_price)
    if entry_filled_price is not None:
        extra["entry_filled_price"] = float(entry_filled_price)
    if (entry_expected_price is not None
            and entry_filled_price is not None):
        bps = compute_slippage_bps(entry_expected_price,
                                     entry_filled_price, side=side)
        if bps is not None:
            extra["entry_slippage_bps"] = bps
    exit_side = "sell" if side == "buy" else "buy"
    if exit_expected_price is not None:
        extra["exit_expected_price"] = float(exit_expected_price)
    if exit_filled_price is not None:
        extra["exit_filled_price"] = float(exit_filled_price)
    if (exit_expected_price is not None
            and exit_filled_price is not None):
        bps = compute_slippage_bps(exit_expected_price,
                                     exit_filled_price, side=exit_side)
        if bps is not None:
            extra["exit_slippage_bps"] = bps
    close_kwargs["extra"] = extra
    return close_kwargs
