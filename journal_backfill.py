"""
Round-40: one-shot backfill for the per-user trade journal.

Problem: positions deployed BEFORE round-33's journal-undercount fix
don't have an "open" entry in trade_journal.json. When they close,
the orphan-close safety net (round-34) creates a synthetic entry
with `price=None` — so the scorecard's win-rate + P&L% math can't
include those trades accurately.

This module walks a user's active strategy files + current Alpaca
positions and synthesizes missing "open" journal entries. It uses
Alpaca's `avg_entry_price` as the authoritative entry price.

Idempotent — safe to run multiple times. Only appends entries for
symbol+strategy pairs that DON'T already have an open journal entry.

Usage:
    from journal_backfill import backfill_user_journal
    result = backfill_user_journal(user, fetch_positions_fn)
    # result = {"backfilled": 3, "skipped_existing": 2, "errors": []}

The fetch_positions_fn is passed in so tests don't need to mock
Alpaca — callers usually hand in cloud_scheduler.user_api_get or
a test stub that returns a fake positions list.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

from et_time import now_et


def backfill_user_journal(user: dict, fetch_positions_fn: Callable,
                           user_file_fn: Optional[Callable] = None) -> dict:
    """Add missing "open" journal entries for the user's current Alpaca
    positions.

    Args:
        user: user dict (must have `id` key)
        fetch_positions_fn: callable that takes a user and returns
            Alpaca positions list (or an error dict). Usually
            cloud_scheduler.user_api_get bound to "/positions".
        user_file_fn: callable (user, filename) -> path. Defaults to
            the cloud_scheduler function if not passed.

    Returns a summary dict."""
    if user_file_fn is None:
        from cloud_scheduler import user_file as _uf
        user_file_fn = _uf

    result = {"backfilled": 0, "skipped_existing": 0,
              "skipped_no_strategy": 0, "errors": []}

    # Load current journal
    journal_path = user_file_fn(user, "trade_journal.json")
    try:
        if os.path.exists(journal_path):
            with open(journal_path) as f:
                journal = json.load(f)
        else:
            journal = {"trades": [], "daily_snapshots": []}
    except (OSError, ValueError) as e:
        result["errors"].append(f"load journal: {e}")
        return result
    trades = journal.get("trades", [])

    # Build a set of existing OPEN (symbol, strategy) pairs so we
    # don't double-enter
    existing_open = {
        ((t.get("symbol") or "").upper(), t.get("strategy") or "")
        for t in trades
        if isinstance(t, dict) and t.get("status") == "open"
    }

    # Fetch Alpaca positions
    positions = fetch_positions_fn(user) if fetch_positions_fn else []
    if isinstance(positions, dict) and "error" in positions:
        result["errors"].append(f"fetch positions: {positions['error']}")
        return result
    if not isinstance(positions, list):
        result["errors"].append(f"positions not a list: {type(positions).__name__}")
        return result

    strats_dir = os.path.dirname(user_file_fn(user, "strategies/placeholder"))
    now_iso = now_et().isoformat()

    for p in positions:
        try:
            sym = (p.get("symbol") or "").upper()
            if not sym:
                continue
            asset_class = (p.get("asset_class") or "").lower()
            # OCC option symbols route via underlying for strategy lookup
            if asset_class == "us_option":
                m = re.match(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$", sym)
                underlying = m.group(1) if m else sym
            else:
                underlying = sym
            # Find which strategy owns this position
            strategy = _detect_strategy_file(strats_dir, underlying, sym)
            if not strategy:
                result["skipped_no_strategy"] += 1
                continue
            if (sym, strategy) in existing_open:
                result["skipped_existing"] += 1
                continue
            # Pull entry price from Alpaca
            try:
                entry_px = float(p.get("avg_entry_price") or 0)
            except (TypeError, ValueError):
                entry_px = 0
            try:
                qty = float(p.get("qty") or 0)
                qty = int(qty) if abs(qty - int(qty)) < 1e-9 else qty
            except (TypeError, ValueError):
                qty = 0
            # Options are sold short (negative qty), buy-to-open (positive qty
            # for long calls) — use Alpaca's `side` field as source of truth
            side_raw = (p.get("side") or "").lower()
            side = "sell_short" if side_raw == "short" else "buy"
            entry = {
                "timestamp": now_iso,
                "symbol": sym,
                "side": side,
                "qty": qty,
                "price": entry_px or None,
                "strategy": strategy,
                "reason": "Backfilled from Alpaca avg_entry_price — deployed pre-round-33 journaling",
                "deployer": "journal_backfill",
                "status": "open",
                "backfilled": True,
            }
            trades.append(entry)
            existing_open.add((sym, strategy))
            result["backfilled"] += 1
        except Exception as e:
            result["errors"].append(f"{p.get('symbol','?')}: {e}")

    if result["backfilled"] > 0:
        journal["trades"] = trades
        try:
            tmp = journal_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(journal, f, indent=2, default=str)
            os.rename(tmp, journal_path)
        except (OSError, ValueError) as e:
            result["errors"].append(f"write journal: {e}")
            result["backfilled"] = 0  # rollback the counter

    return result


def _detect_strategy_file(strats_dir: str, underlying: str, full_symbol: str) -> str:
    """Scan strategy files to find which one owns this symbol.

    Returns the strategy name (e.g. "trailing_stop", "wheel", "breakout")
    or "" if no matching file. Prefers wheel > trailing_stop > the actual
    entry strategy — same precedence as cloud_scheduler._mark_auto_deployed.
    """
    if not strats_dir or not os.path.isdir(strats_dir):
        return ""
    try:
        files = os.listdir(strats_dir)
    except OSError:
        return ""
    priority = {"wheel": 3, "trailing_stop": 2}
    best = ("", 0)
    for fname in files:
        if not fname.endswith(".json"):
            continue
        stem = fname[:-5]
        if "_" not in stem:
            continue
        strat, _, sym = stem.rpartition("_")
        if not sym or not strat:
            continue
        if sym.upper() == underlying or sym.upper() == full_symbol:
            prio = priority.get(strat, 1)
            if prio >= best[1]:
                best = (strat, prio)
    return best[0]
