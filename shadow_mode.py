"""Round-61 pt.86 — live-mode dry-run / shadow mode.

Paper validation ends ~May 15. After that the user can flip the
live-mode toggle (with the pt.72 promotion gate as a check), and
real money starts trading. There's currently no way to "watch
what live would do" without committing capital.

Shadow mode is a per-user opt-in flag that runs the auto-deployer
through every gate (pt.65-82 stack) but RECORDS the deploy
intent into a shadow log instead of POSTing the order. The user
can review the log in the dashboard and gain confidence that
live mode would behave as expected.

This module is intentionally pure: caller passes the user dict
(with `_data_dir`) and gets back a boolean `is_shadow_mode_active`
or writes a structured event via `record_shadow_event`. The
shadow log is a per-user JSON file.

Use:
    >>> from shadow_mode import is_shadow_mode_active
    >>> if is_shadow_mode_active(user, guardrails):
    ...     record_shadow_event(user, "would_deploy",
    ...                            symbol="AAPL", strategy="breakout", ...)
    ...     return  # short-circuit the real POST
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Mapping, Optional


SHADOW_LOG_FILENAME = "shadow_log.json"
# Cap the persisted log so it doesn't grow forever. 500 entries
# = ~50 days at 10 deploys/day, well past the typical evaluation
# window.
MAX_LOG_ENTRIES: int = 500


def _shadow_log_path(user: Mapping) -> Optional[str]:
    """Per-user JSON path for the shadow log. None on missing
    `_data_dir` (programming error — caller should always supply it)."""
    if not isinstance(user, Mapping):
        return None
    data_dir = user.get("_data_dir")
    if not data_dir:
        return None
    return os.path.join(data_dir, SHADOW_LOG_FILENAME)


def is_shadow_mode_active(user: Optional[Mapping] = None,
                            guardrails: Optional[Mapping] = None,
                            *,
                            env: Optional[Mapping] = None,
                            ) -> bool:
    """True when shadow mode is enabled for this user. Resolution
    order:
      1. ``guardrails["live_shadow_mode"]`` if present (per-user
         override via Settings → Live Trading).
      2. ``env["LIVE_SHADOW_MODE"]`` falls back to "1"/"true" for
         deployment-wide opt-in (env arg defaults to ``os.environ``).
      3. False.

    Pure: no I/O, just lookups. Safe to call on every deploy.
    """
    if isinstance(guardrails, Mapping):
        v = guardrails.get("live_shadow_mode")
        if v is not None:
            return bool(v)
    e = env if env is not None else os.environ
    raw = (e.get("LIVE_SHADOW_MODE") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def record_shadow_event(user: Mapping, action: str, **fields) -> dict:
    """Append a shadow event to the per-user shadow log. Returns
    the event dict that was written.

    Best-effort: file-write failures don't raise (Sentry capture is
    the caller's responsibility if needed). Caps the log at
    ``MAX_LOG_ENTRIES`` (oldest-first prune).

    Standard fields callers should populate:
      action: "would_deploy" / "would_close" / "would_cancel"
      symbol, strategy, qty, price (when relevant)
      reason: short string explaining WHY (gate, score, etc.)
    """
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
    }
    event.update(fields)
    path = _shadow_log_path(user)
    if not path:
        return event
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        log = []
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    log = json.load(fh)
                    if not isinstance(log, list):
                        log = []
            except (OSError, json.JSONDecodeError):
                log = []
        log.append(event)
        if len(log) > MAX_LOG_ENTRIES:
            log = log[-MAX_LOG_ENTRIES:]
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(log, fh, indent=2)
        os.rename(tmp, path)
    except OSError:
        pass
    return event


def get_shadow_log(user: Mapping, *, limit: int = 50) -> list:
    """Return up to ``limit`` most-recent shadow events (newest
    first). Empty list on missing log or read error."""
    path = _shadow_log_path(user)
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path) as fh:
            log = json.load(fh)
        if not isinstance(log, list):
            return []
        return list(reversed(log))[:max(1, int(limit))]
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return []


def summarize_shadow_log(events) -> dict:
    """Aggregate a shadow log for the dashboard. Returns counts
    + per-action breakdown so the user can see "12 would-deploys
    in the last 24h, all on Tech sector"."""
    out = {"total": 0, "by_action": {}, "by_symbol": {}, "by_strategy": {}}
    if not isinstance(events, list):
        return out
    for e in events:
        if not isinstance(e, Mapping):
            continue
        out["total"] += 1
        action = e.get("action") or "unknown"
        out["by_action"][action] = out["by_action"].get(action, 0) + 1
        sym = (e.get("symbol") or "").upper()
        if sym:
            out["by_symbol"][sym] = out["by_symbol"].get(sym, 0) + 1
        strat = (e.get("strategy") or "")
        if strat:
            out["by_strategy"][strat] = out["by_strategy"].get(strat, 0) + 1
    return out
