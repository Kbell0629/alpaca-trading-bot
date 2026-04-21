"""
Round-34: extract the "today's closes" scanner into its own module so
tests can import it without dragging in server.py's auth / sqlite init.

Before this split, `server._scan_todays_closes` was testable only by
reloading the whole server module, which broke when DATA_DIR pointed
at a tmp path without the auth DB seeded.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from et_time import now_et


def scan_todays_closes(journal_or_path) -> list:
    """Return trades closed TODAY (ET), newest-first.

    Accepts either:
      * a dict (the loaded journal), or
      * a filesystem path to a trade_journal.json

    Tolerates malformed entries (non-dict, missing exit_timestamp,
    unparseable ISO timestamps) without raising.
    """
    journal = _load(journal_or_path)
    trades = journal.get("trades", []) if isinstance(journal, dict) else []
    today = now_et().date()
    out = []
    for t in trades:
        if not isinstance(t, dict) or t.get("status") != "closed":
            continue
        exit_ts = t.get("exit_timestamp")
        if not exit_ts:
            continue
        try:
            exit_dt = datetime.fromisoformat(exit_ts)
        except (ValueError, TypeError):
            continue
        try:
            exit_date = exit_dt.date()
        except Exception:
            continue
        if exit_date != today:
            continue
        out.append({
            "symbol": t.get("symbol"),
            "strategy": t.get("strategy"),
            "exit_timestamp": exit_ts,
            "exit_price": t.get("exit_price"),
            "exit_reason": t.get("exit_reason"),
            "pnl": t.get("pnl"),
            "pnl_pct": t.get("pnl_pct"),
            "qty": t.get("qty"),
            "entry_price": t.get("price"),
            "orphan_close": bool(t.get("orphan_close")),
        })
    out.sort(key=lambda x: x.get("exit_timestamp") or "", reverse=True)
    return out


def _load(journal_or_path) -> Optional[dict]:
    if isinstance(journal_or_path, dict):
        return journal_or_path
    if not isinstance(journal_or_path, str):
        return None
    if not os.path.exists(journal_or_path):
        return None
    try:
        import json
        with open(journal_or_path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
