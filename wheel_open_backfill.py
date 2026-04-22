"""
Round-43: retroactively fix orphan-close entries for wheel contracts
that were deployed BEFORE round-33's wheel-open journaling fix.

Problem: pre-round-33, `wheel_strategy.open_short_put` updated the
wheel state file (history + premium_received) but never journaled
the sell-to-open. When one of those positions finally closes (via
round-42's close-journaling), `record_trade_close` finds no matching
open entry and creates a synthetic "closed" entry flagged
`orphan_close: true`. Dashboard shows `[orphan]` + pnl_pct is missing
because we don't know the entry price.

This backfill walks wheel state files → `history[]` → pulls the
original `sell_to_open_*` / `*_filled` events → finds each
matching orphan_close entry in `trade_journal.json` → patches the
entry with the real entry price + recomputes pnl_pct + clears the
orphan flag.

Idempotent — safe to re-run. Only touches entries flagged
`orphan_close: true`; legitimately-closed entries (with a proper
open pair) are untouched.

Usage:
    from wheel_open_backfill import backfill_wheel_opens
    result = backfill_wheel_opens(user)
    # result = {"patched": 2, "skipped_no_match": 0, "skipped_no_history": 1, "errors": []}
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional


def backfill_wheel_opens(user: dict,
                          user_file_fn: Optional[Callable] = None,
                          list_wheel_fn: Optional[Callable] = None) -> dict:
    """Patch orphan_close entries in trade_journal.json with the original
    entry price recovered from wheel state files.

    Args:
        user: user dict (must have `id` key)
        user_file_fn: callable (user, filename) -> path. Defaults to
            cloud_scheduler.user_file.
        list_wheel_fn: callable (user) -> list of (filename, state_dict).
            Defaults to wheel_strategy.list_wheel_files.

    Returns a summary dict.
    """
    if user_file_fn is None:
        from cloud_scheduler import user_file as _uf
        user_file_fn = _uf
    if list_wheel_fn is None:
        import wheel_strategy as _ws
        list_wheel_fn = _ws.list_wheel_files
    from cloud_scheduler import strategy_file_lock

    result = {"patched": 0, "skipped_no_match": 0,
              "skipped_no_history": 0, "errors": []}

    # Build a map: OCC contract symbol → entry_price
    # Walk every wheel state file's history[] for sell_to_open and
    # *_filled events. The fill event has the authoritative price
    # (actual Alpaca fill_price); the sell_to_open event has the
    # bot's limit price which may differ slightly. Prefer fill.
    entry_by_contract = {}
    try:
        wheels = list_wheel_fn(user)
    except Exception as e:
        result["errors"].append(f"list wheels: {e}")
        return result

    for fname, state in wheels:
        try:
            history = state.get("history", []) if isinstance(state, dict) else []
            for ev in history:
                if not isinstance(ev, dict):
                    continue
                name = ev.get("event") or ""
                detail = ev.get("detail") or {}
                if not isinstance(detail, dict):
                    continue
                # sell_to_open_put / sell_to_open_call — has limit_price
                # and `contract` OCC symbol
                if name.startswith("sell_to_open_"):
                    occ = detail.get("contract")
                    price = detail.get("limit_price")
                    if occ and price is not None:
                        entry_by_contract.setdefault(occ, float(price))
                # put_filled / call_filled — authoritative fill_price.
                # No OCC in detail (the state's active_contract holds it
                # at that moment, but we're walking history after-the-
                # fact). Infer from the active_contract OR from the most
                # recent sell_to_open event.
                elif name.endswith("_filled"):
                    price = detail.get("fill_price")
                    if price is None:
                        continue
                    # Find the sell_to_open event closest before this
                    # one (by event order) to associate the OCC symbol.
                    idx = history.index(ev)
                    occ = None
                    for prior in reversed(history[:idx]):
                        if isinstance(prior, dict) and \
                           (prior.get("event") or "").startswith("sell_to_open_"):
                            pd = prior.get("detail") or {}
                            if isinstance(pd, dict):
                                occ = pd.get("contract")
                            break
                    if occ:
                        # Fill price WINS over limit price (authoritative)
                        entry_by_contract[occ] = float(price)
        except Exception as e:
            result["errors"].append(f"{fname}: {e}")

    if not entry_by_contract:
        # No recoverable opens — nothing to patch. Not an error; just
        # means history has been trimmed past HISTORY_MAX or this user
        # has no wheels yet.
        result["skipped_no_history"] = 1
        return result

    # Patch the journal
    journal_path = user_file_fn(user, "trade_journal.json")
    try:
        with strategy_file_lock(journal_path):
            if not os.path.exists(journal_path):
                return result
            try:
                with open(journal_path) as f:
                    journal = json.load(f)
            except (OSError, ValueError) as e:
                result["errors"].append(f"load journal: {e}")
                return result
            trades = journal.get("trades", [])
            patched_any = False
            for t in trades:
                if not isinstance(t, dict):
                    continue
                if not t.get("orphan_close"):
                    continue
                if t.get("strategy") != "wheel":
                    continue
                occ = t.get("symbol")
                entry_px = entry_by_contract.get(occ)
                if entry_px is None:
                    result["skipped_no_match"] += 1
                    continue
                # Patch in place: fill price field, recompute pnl_pct,
                # clear orphan flag, stamp audit marker.
                t["price"] = round(float(entry_px), 4)
                # Short-cover pnl_pct = (entry/exit - 1) * 100
                try:
                    exit_px = float(t.get("exit_price") or 0)
                    if entry_px > 0 and exit_px > 0:
                        t["pnl_pct"] = round(
                            (float(entry_px) / exit_px - 1) * 100, 2)
                except (TypeError, ValueError):
                    pass
                t.pop("orphan_close", None)
                t["open_backfilled"] = True
                t["backfill_source"] = "wheel_state_history"
                patched_any = True
                result["patched"] += 1

            if patched_any:
                try:
                    tmp = journal_path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(journal, f, indent=2, default=str)
                    os.rename(tmp, journal_path)
                except (OSError, ValueError) as e:
                    result["errors"].append(f"write journal: {e}")
                    result["patched"] = 0  # rollback counter
    except Exception as e:
        result["errors"].append(f"patch journal: {e}")

    return result
