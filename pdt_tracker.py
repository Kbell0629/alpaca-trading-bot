"""
Round-50: PDT (Pattern Day Trader) rule awareness.

The PDT rule applies to MARGIN accounts with equity < $25k:
  * A "day trade" = buying AND selling the same symbol on the same day
  * Max 3 day trades in a rolling 5-business-day window
  * 4th day trade flags the account as PDT; Alpaca will restrict
    trading unless equity is brought back ≥ $25k for 5 days

Cash accounts are NEVER subject to PDT (unlimited day trades, but
bound by T+1/T+2 settlement — see settled_funds.py for that).

Alpaca provides `day_trades_remaining` on /v2/account which is
authoritative. We wrap that with:
  * `can_day_trade(tier_cfg, buffer=1)` — respects PDT with a reserve
  * `is_day_trade(entry_date, exit_date)` — does a same-day close count
  * `log_day_trade(user, symbol)` — optional local audit trail (for
    debugging when Alpaca's counter disagrees with our view)

The bot uses this BEFORE each intraday exit on margin accounts under
$25k. If `can_day_trade` returns False, the exit is deferred (held
overnight) instead of blindly firing and tripping Alpaca's counter.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional

try:
    import fcntl as _fcntl
    _HAS_FLOCK = True
except ImportError:
    _HAS_FLOCK = False


@contextmanager
def _file_lock(path):
    """Round-52: serialize RMW on pdt_day_trades.json so concurrent
    scheduler ticks (paper + live) can't lose log entries."""
    if not _HAS_FLOCK:
        yield
        return
    lock_path = path + ".lock"
    fh = None
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fh = open(lock_path, "w")
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        if fh is not None:
            try: _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            except OSError: pass
            try: fh.close()
            except OSError: pass

PDT_LOG_FILENAME = "pdt_day_trades.json"
# Alpaca's rolling window is 5 BUSINESS days. We approximate with 7
# calendar days for the local log rotation (errs on the side of
# keeping more history, which is safe).
LOG_RETENTION_DAYS = 14  # keep ~2 weeks for debugging


def is_day_trade(entry_ts: str, exit_ts: str) -> bool:
    """Return True if the position was opened AND closed on the same
    ET calendar date (an intraday round-trip = day trade).

    Robust to various ISO timestamp formats. Returns False on parse
    errors so we don't accidentally flag trades we can't confirm.
    """
    if not entry_ts or not exit_ts:
        return False
    try:
        from datetime import datetime
        e = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
        x = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
        # Convert to ET date (PDT rule is US market date)
        from et_time import now_et
        # Cheap: just compare local dates since both came from now_et
        return e.date() == x.date()
    except (ValueError, TypeError):
        return False


def can_day_trade(tier_cfg: dict, buffer: int = 1) -> tuple:
    """Return (allowed: bool, reason: str, remaining: int | None).

    Rules:
      * Cash account (pdt_applies=False) → always allowed
      * Margin ≥ $25k (pdt_applies=False) → always allowed
      * Margin < $25k (pdt_applies=True):
          - If Alpaca day_trades_remaining > buffer → allowed
          - If remaining <= buffer → DENIED (preserve emergency slot)
          - If remaining is None (unknown) → allow conservatively (Alpaca
            will enforce if over limit)

    `buffer` is the number of day-trade slots we reserve. Default 1
    keeps an emergency slot for kill-switch or critical-exit scenarios.
    Set buffer=0 to use all 3 slots freely (not recommended).
    """
    if not tier_cfg:
        return True, "", None
    if not tier_cfg.get("pdt_applies", False):
        return True, "", None  # cash or margin ≥$25k
    remaining = tier_cfg.get("_detected_day_trades_remaining")
    if remaining is None:
        return True, "day_trades_remaining unknown (allow conservatively)", None
    if remaining <= buffer:
        return False, (
            f"PDT buffer low: day_trades_remaining={remaining}, "
            f"buffer={buffer}. Holding overnight to preserve emergency "
            "day-trade slot."), remaining
    return True, "", remaining


def log_day_trade(user: dict, symbol: str, strategy: str = "") -> None:
    """Append a local day-trade audit entry. Best-effort — never
    raises. Used for debugging when Alpaca's counter disagrees with
    our expectation; not used for enforcement (Alpaca's counter is
    authoritative)."""
    try:
        data_dir = user.get("_data_dir") or ""
        if not data_dir or not os.path.isdir(data_dir):
            return
        path = os.path.join(data_dir, PDT_LOG_FILENAME)
        from et_time import now_et
        # Round-52: lock-wrap the RMW so concurrent log_day_trade calls
        # (e.g., from paper + live scheduler ticks on the same user)
        # can't drop entries via lost-update.
        with _file_lock(path):
            log = []
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        log = json.load(f)
                except (OSError, ValueError):
                    log = []
            if not isinstance(log, list):
                log = []
            # Prune entries older than retention window
            cutoff = (date.today() - timedelta(days=LOG_RETENTION_DAYS)).isoformat()
            log = [e for e in log
                   if isinstance(e, dict) and (e.get("date") or "") >= cutoff]
            log.append({
                "date": date.today().isoformat(),
                "ts": now_et().isoformat(),
                "symbol": (symbol or "").upper(),
                "strategy": strategy,
            })
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(log, f, indent=2)
            os.rename(tmp, path)
    except Exception:
        pass  # never block a trade on a logging failure


def count_local_day_trades_last_5_business_days(user: dict) -> int:
    """Count day-trade entries in our local log for the last 5 business
    days. For debugging only — Alpaca's day_trades_remaining is
    authoritative for enforcement.
    """
    data_dir = user.get("_data_dir") or ""
    if not data_dir:
        return 0
    path = os.path.join(data_dir, PDT_LOG_FILENAME)
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            log = json.load(f)
    except (OSError, ValueError):
        return 0
    if not isinstance(log, list):
        return 0
    # Last 5 business days — approximate with 7 calendar days
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    return sum(1 for e in log
               if isinstance(e, dict) and (e.get("date") or "") >= cutoff)
