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
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def compute_tax_lots(journal, basis_method="FIFO"):
    """Walk trades chronologically and apply lot-matching.

    Args:
        journal: dict with "trades" list (from trade_journal.json)
        basis_method: "FIFO" or "LIFO" — defaults FIFO (Alpaca's default)

    Returns dict described in module docstring."""
    trades = sorted(
        journal.get("trades", []) if journal else [],
        key=lambda t: t.get("timestamp", t.get("entry_date", ""))
    )

    # Per-symbol queue of open buy lots.
    # Each lot: {qty, cost_basis_per_share, entry_date, entry_trade_id}
    open_lots = defaultdict(deque)
    closed_lots = []  # list of dicts with all matching info

    for t in trades:
        symbol = t.get("symbol", "")
        if not symbol:
            continue
        side = (t.get("side") or "").lower()
        try:
            qty = int(_safe_float(t.get("qty", 0)))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        price = _safe_float(t.get("price"))
        ts = t.get("timestamp") or t.get("entry_date") or ""
        d = _parse_date(ts)
        if not d:
            continue

        if side == "buy":
            # Open a new lot
            open_lots[symbol].append({
                "qty_remaining": qty,
                "cost_basis_per_share": price,
                "entry_date": d,
                "entry_timestamp": ts,
                "strategy": t.get("strategy", ""),
            })
        elif side == "sell":
            # Match against open lots using basis_method
            qty_to_sell = qty
            exit_price = price
            exit_date = d
            while qty_to_sell > 0 and open_lots[symbol]:
                if basis_method == "LIFO":
                    lot = open_lots[symbol][-1]
                else:
                    lot = open_lots[symbol][0]
                matched_qty = min(qty_to_sell, lot["qty_remaining"])
                proceeds = matched_qty * exit_price
                cost_basis = matched_qty * lot["cost_basis_per_share"]
                gain_loss = proceeds - cost_basis
                holding_days = (exit_date - lot["entry_date"]).days
                term = "long" if holding_days >= 365 else "short"
                closed_lots.append({
                    "symbol": symbol,
                    "qty": matched_qty,
                    "acquired_date": lot["entry_date"].isoformat(),
                    "sold_date": exit_date.isoformat(),
                    "holding_days": holding_days,
                    "term": term,
                    "cost_basis": round(cost_basis, 2),
                    "proceeds": round(proceeds, 2),
                    "gain_loss": round(gain_loss, 2),
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

    # Summary
    short_term = sum(l["gain_loss"] for l in closed_lots if l["term"] == "short")
    long_term = sum(l["gain_loss"] for l in closed_lots if l["term"] == "long")
    total_proceeds = sum(l["proceeds"] for l in closed_lots)
    total_basis = sum(l["cost_basis"] for l in closed_lots)
    total_gl = sum(l["gain_loss"] for l in closed_lots)

    return {
        "lots": closed_lots,
        "summary": {
            "lot_count": len(closed_lots),
            "total_proceeds": round(total_proceeds, 2),
            "total_cost_basis": round(total_basis, 2),
            "total_gain_loss": round(total_gl, 2),
            "short_term_gain": round(short_term, 2),
            "long_term_gain": round(long_term, 2),
            "wash_sale_warnings": wash_sales,
        },
        "open_lots_remaining": {
            sym: [
                {
                    "qty": l["qty_remaining"],
                    "cost_basis_per_share": l["cost_basis_per_share"],
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
