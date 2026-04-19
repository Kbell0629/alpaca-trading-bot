#!/usr/bin/env python3
"""
tax_lots.py — Tax-lot accounting + IRS Form 8949 export.

Round-11 expansion (item 2). Required for live trading because Alpaca
applies FIFO by default and the bot doesn't track which specific lot
got sold. This module walks the trade journal, reconstructs FIFO cost
basis, computes holding period (short-term vs long-term), and emits a
CSV in Form 8949 layout.

Public API:

    compute_tax_lots(journal) -> dict
        {
          "lots": [<closed-lot dicts>],
          "summary": {
            "total_proceeds": float,
            "total_cost_basis": float,
            "total_gain_loss": float,
            "short_term_gain": float,
            "long_term_gain": float,
            "wash_sale_warnings": [<symbol+reason dicts>]
          }
        }

    export_form_8949_csv(lots, output_path) -> str
        Writes the IRS Form 8949 schedule to CSV. Returns the path.

    detect_wash_sales(closed_lots) -> list
        Flags potential wash sales (loss + repurchase within 30 days).
        IRS rule: capital loss disallowed if substantially identical
        security purchased within 30d before/after sale.

Holding period:
    short-term: held < 365 days (taxed as ordinary income)
    long-term:  held >= 365 days (taxed at long-term capital gains rate)
"""
from __future__ import annotations
import csv
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN

# Phase 1 of the float→Decimal migration (see docs/DECIMAL_MIGRATION_PLAN.md).
# tax_lots is read-only w.r.t. trading: it computes cost basis / proceeds /
# gain-loss but never places an order. Moving its internal math to Decimal
# eliminates compounding drift in the numbers the IRS gets, while leaving
# the function signatures and return-dict shape unchanged so downstream
# consumers don't need changes. Serialisation back to float is explicit,
# with banker's rounding at the cent.

# Penny grid used for final quantize. ROUND_HALF_EVEN is IRS-acceptable
# for rounding individual lots; matches Alpaca's own 1099 math.
_CENT = Decimal("0.01")


def _to_decimal(v, default: Decimal = Decimal("0")) -> Decimal:
    """Coerce a trade-field value to Decimal WITHOUT going through float.

    Using Decimal(float_x) is the classic footgun — it preserves the IEEE
    754 imprecision into the Decimal value. Always route through str so
    the human-readable form survives.
    """
    if v is None or v == "":
        return default
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return default


def _round_cent(v: Decimal) -> Decimal:
    """Quantize to the nearest cent using banker's rounding."""
    return v.quantize(_CENT, rounding=ROUND_HALF_EVEN)


def _to_json_money(v: Decimal) -> float:
    """Serialise a Decimal money value to float for the JSON response.

    We round at the cent (Decimal), then cross the boundary to float at
    the very last step. The float has at most 2dp of meaningful precision,
    so the IEEE 754 representation error is bounded well below $0.01.
    """
    return float(_round_cent(v))


def _parse_date(s):
    """Parse various ISO/datetime formats to a date object."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s.date()
    s = str(s).split("T")[0].split(" ")[0]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _safe_float(v, default=0.0):
    """Legacy helper. Kept for any external caller; internal compute paths
    use _to_decimal exclusively after the phase-1 migration."""
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def compute_tax_lots(journal, basis_method="FIFO"):
    """Walk trades chronologically and apply lot-matching.

    Args:
        journal: dict with "trades" list (from trade_journal.json)
        basis_method: "FIFO" or "LIFO" — defaults FIFO (Alpaca's default)

    Returns dict described in module docstring.

    Phase-1 migration note: internal math is Decimal. The return dict keeps
    the same keys + float types for cost_basis / proceeds / gain_loss so
    every caller (server.py handlers, CSV export, JSON responses) is
    unaffected. The only behavioural change is fewer floating-point
    rounding errors in the last 2-3 decimal places — which now won't
    compound across long chains of partial fills.
    """
    trades = sorted(
        journal.get("trades", []) if journal else [],
        key=lambda t: t.get("timestamp", t.get("entry_date", ""))
    )

    # Per-symbol queue of open buy lots.
    # Each lot: {qty_remaining: int, cost_basis_per_share: Decimal,
    #            entry_date, entry_timestamp, strategy}
    open_lots: dict[str, deque] = defaultdict(deque)
    closed_lots: list = []

    for t in trades:
        symbol = t.get("symbol", "")
        if not symbol:
            continue
        side = (t.get("side") or "").lower()
        # Share counts stay integer — Alpaca supports fractional shares but
        # the tax-lot report rounds to whole-share lines for 8949 clarity.
        try:
            qty = int(_safe_float(t.get("qty", 0)))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        price = _to_decimal(t.get("price"))
        ts = t.get("timestamp") or t.get("entry_date") or ""
        d = _parse_date(ts)
        if not d:
            continue

        if side == "buy":
            # Open a new lot. cost_basis_per_share is Decimal from here on.
            open_lots[symbol].append({
                "qty_remaining": qty,
                "cost_basis_per_share": price,
                "entry_date": d,
                "entry_timestamp": ts,
                "strategy": t.get("strategy", ""),
            })
        elif side == "sell":
            qty_to_sell = qty
            exit_price = price
            exit_date = d
            while qty_to_sell > 0 and open_lots[symbol]:
                if basis_method == "LIFO":
                    lot = open_lots[symbol][-1]
                else:
                    lot = open_lots[symbol][0]
                matched_qty = min(qty_to_sell, lot["qty_remaining"])
                # Decimal arithmetic. matched_qty is int; mixing int with
                # Decimal is exact (no float involved).
                proceeds_d = matched_qty * exit_price
                cost_basis_d = matched_qty * lot["cost_basis_per_share"]
                gain_loss_d = proceeds_d - cost_basis_d
                holding_days = (exit_date - lot["entry_date"]).days
                term = "long" if holding_days >= 365 else "short"
                closed_lots.append({
                    "symbol": symbol,
                    "qty": matched_qty,
                    "acquired_date": lot["entry_date"].isoformat(),
                    "sold_date": exit_date.isoformat(),
                    "holding_days": holding_days,
                    "term": term,
                    # Serialise at the boundary. Quantize first, then cross
                    # to float for JSON compatibility.
                    "cost_basis": _to_json_money(cost_basis_d),
                    "proceeds": _to_json_money(proceeds_d),
                    "gain_loss": _to_json_money(gain_loss_d),
                    "strategy": lot.get("strategy", ""),
                    "method": basis_method,
                })
                lot["qty_remaining"] -= matched_qty
                qty_to_sell -= matched_qty
                if lot["qty_remaining"] <= 0:
                    if basis_method == "LIFO":
                        open_lots[symbol].pop()
                    else:
                        open_lots[symbol].popleft()
            # Note: if qty_to_sell > 0 here, it's an unmatched sell
            # (short or data-skew). We silently drop the residual.

    # Wash-sale detection (basic: same symbol + loss + repurchase within 30d)
    wash_sales = detect_wash_sales(closed_lots, all_trades=trades)

    # Summary math — sum Decimals directly to avoid accumulating float
    # error across thousands of lots. Each closed_lots[i]["gain_loss"] is
    # a rounded float for the caller; summing Decimal pre-round values
    # would give a slightly more accurate total, but to stay consistent
    # with what the dashboard sees lot-by-lot we sum the rounded forms.
    short_term = sum(
        (_to_decimal(l["gain_loss"]) for l in closed_lots if l["term"] == "short"),
        Decimal("0"),
    )
    long_term = sum(
        (_to_decimal(l["gain_loss"]) for l in closed_lots if l["term"] == "long"),
        Decimal("0"),
    )
    total_proceeds = sum(
        (_to_decimal(l["proceeds"]) for l in closed_lots),
        Decimal("0"),
    )
    total_basis = sum(
        (_to_decimal(l["cost_basis"]) for l in closed_lots),
        Decimal("0"),
    )
    total_gl = sum(
        (_to_decimal(l["gain_loss"]) for l in closed_lots),
        Decimal("0"),
    )

    return {
        "lots": closed_lots,
        "summary": {
            "lot_count": len(closed_lots),
            "total_proceeds": _to_json_money(total_proceeds),
            "total_cost_basis": _to_json_money(total_basis),
            "total_gain_loss": _to_json_money(total_gl),
            "short_term_gain": _to_json_money(short_term),
            "long_term_gain": _to_json_money(long_term),
            "wash_sale_warnings": wash_sales,
        },
        "open_lots_remaining": {
            sym: [
                {
                    "qty": l["qty_remaining"],
                    # Serialise the Decimal cost basis at the boundary
                    # — preserve 2dp for display; the internal lot still
                    # holds the full-precision Decimal.
                    "cost_basis_per_share": _to_json_money(l["cost_basis_per_share"]),
                    "entry_date": l["entry_date"].isoformat(),
                    "strategy": l["strategy"],
                }
                for l in lots if l["qty_remaining"] > 0
            ]
            for sym, lots in open_lots.items()
            if any(l["qty_remaining"] > 0 for l in lots)
        },
    }


def detect_wash_sales(closed_lots, all_trades=None):
    """IRS wash-sale rule: a capital loss is disallowed if the same
    or substantially identical security was purchased within 30 days
    before OR after the sale.

    Returns a list of warnings: [{symbol, sale_date, loss_amount,
    repurchase_date, days_apart, reason}]
    """
    warnings = []
    losses = [l for l in closed_lots if l["gain_loss"] < 0]
    # Build a per-symbol list of buy dates from all trades
    buys_by_symbol = defaultdict(list)
    if all_trades:
        for t in all_trades:
            if (t.get("side") or "").lower() != "buy":
                continue
            d = _parse_date(t.get("timestamp") or t.get("entry_date"))
            if d:
                buys_by_symbol[t.get("symbol", "")].append(d)

    for loss_lot in losses:
        sym = loss_lot["symbol"]
        sold = _parse_date(loss_lot["sold_date"])
        if not sold:
            continue
        # Check buys within 30 days before/after the sale
        window_start = sold - timedelta(days=30)
        window_end = sold + timedelta(days=30)
        for buy_date in buys_by_symbol.get(sym, []):
            if window_start <= buy_date <= window_end and buy_date != sold:
                warnings.append({
                    "symbol": sym,
                    "sale_date": loss_lot["sold_date"],
                    "loss_amount": loss_lot["gain_loss"],
                    "repurchase_date": buy_date.isoformat(),
                    "days_apart": (buy_date - sold).days,
                    "reason": "Same security re-purchased within 30 days — wash-sale loss disallowed",
                })
                break  # one warning per loss lot is enough
    return warnings


def export_form_8949_csv(lots, output_path):
    """Write a CSV in IRS Form 8949 layout. Columns:
        Description, Date Acquired, Date Sold, Proceeds, Cost Basis,
        Gain/Loss, Term, Strategy

    Lots are split by term so the user can paste short-term and
    long-term sections into the correct 8949 box. Returns the path."""
    out = os.path.dirname(output_path) or "."
    os.makedirs(out, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "Description (a)",
            "Date Acquired (b)",
            "Date Sold (c)",
            "Proceeds (d)",
            "Cost Basis (e)",
            "Gain/Loss (h)",
            "Term",
            "Strategy",
        ])
        for term in ("short", "long"):
            for lot in [l for l in lots if l["term"] == term]:
                w.writerow([
                    f"{lot['qty']} sh {lot['symbol']}",
                    lot["acquired_date"],
                    lot["sold_date"],
                    f"{lot['proceeds']:.2f}",
                    f"{lot['cost_basis']:.2f}",
                    f"{lot['gain_loss']:.2f}",
                    "Long-term" if term == "long" else "Short-term",
                    lot.get("strategy", ""),
                ])
    return output_path


if __name__ == "__main__":
    # Smoke test
    fixture = {
        "trades": [
            {"symbol": "AAPL", "side": "buy", "qty": 100, "price": 150,
             "timestamp": "2024-01-15T10:00:00", "strategy": "breakout"},
            {"symbol": "AAPL", "side": "sell", "qty": 50, "price": 160,
             "timestamp": "2024-02-15T10:00:00", "strategy": "breakout"},
            {"symbol": "AAPL", "side": "sell", "qty": 50, "price": 145,
             "timestamp": "2025-02-15T10:00:00", "strategy": "breakout"},
        ]
    }
    result = compute_tax_lots(fixture)
    import json as _j
    print(_j.dumps(result, indent=2, default=str))
