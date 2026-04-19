"""
trade_journal.py — bounded-growth management for the trade journal.

Background (round-12 audit MEDIUM item):
    Each closed trade is appended to `trades[]` in `trade_journal.json` and
    never removed. For a long-lived deploy this is unbounded — though small
    in practice (~300 bytes/trade, so even 10K trades = 3MB). The concern is
    two-fold:
      1. Every scheduler tick reads the whole journal to snapshot portfolio
         state. O(N) load/save amortised per-tick grows.
      2. update_scorecard.py iterates all trades to compute strategy_breakdown
         on every run; that iteration is already O(N).

Strategy — split live + archive:
    - Trades closed more than `KEEP_CLOSED_YEARS` years ago move from
      `trade_journal.json` to `trade_journal_archive.json` in the same dir.
    - Open trades never move, regardless of age.
    - Recent closed trades stay in the live file for fast lookups.
    - Scorecard aggregation iterates both files combined, so lifetime stats
      (total PnL, per-strategy win rate) are unchanged.

Design choices:
    - Archive lives in the same DATA_DIR as the live journal, so existing
      backup (`backup.py`) captures it automatically.
    - Atomic writes via tempfile+rename (same pattern as server.py.save_json).
    - Idempotent: running trim_journal twice in a row is safe.
    - Conservative default: 2 years of closed-trade history kept live. You
      almost certainly won't notice trimming under normal use.

Public API:

    trim_journal(journal_path: str, *, keep_closed_years: float = 2.0) -> dict
        Side-effect: rewrites journal_path (live) and journal_path + _archive
        so that no closed trade older than the cutoff remains in live.
        Returns a summary dict: {moved, live_count, archive_count, cutoff_iso}.

    load_all_trades(journal_path: str) -> list
        Returns live trades + archived trades (closed+open), in chronological
        order, for callers that need the full history (scorecard, tax_lots).

    archive_path_for(journal_path: str) -> str
        Returns the path of the archive sibling for a given live journal path.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


KEEP_CLOSED_YEARS_DEFAULT = 2.0


def archive_path_for(journal_path: str) -> str:
    """Return the sibling archive-file path for a given live journal path.

    e.g. "/data/users/1/trade_journal.json" -> "/data/users/1/trade_journal_archive.json"
    """
    base, ext = os.path.splitext(journal_path)
    return f"{base}_archive{ext or '.json'}"


def _atomic_write_json(path: str, data: Any) -> None:
    """Write JSON to `path` atomically (tempfile + os.replace).

    Same pattern as server.save_json — kept local here to avoid importing
    server.py from a module that's imported by server.py's dependents.
    """
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _parse_trade_ts(trade: dict) -> Optional[datetime]:
    """Return a tz-aware datetime for a trade's primary timestamp, or None
    if we can't parse one. Ordering: closed_at → exit_time → timestamp.

    Closed-trade timestamps are written by update_scorecard.record_trade_close
    in ISO-8601 ET format. Older rows may use "timestamp" (the entry time).
    We prefer the close time for trimming (that's the "age of the closed row")
    but fall back to the entry time for safety.
    """
    for key in ("closed_at", "exit_time", "timestamp", "entry_time"):
        raw = trade.get(key)
        if not raw:
            continue
        try:
            # datetime.fromisoformat handles both naive and offset-aware ISO
            # strings; our journal writes offset-aware ET timestamps.
            dt = datetime.fromisoformat(str(raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _is_closed(trade: dict) -> bool:
    """True if the trade is a closed row. Defensive — matches how
    update_scorecard + cloud_scheduler mark closure."""
    status = (trade.get("status") or "").lower()
    return status == "closed" or trade.get("pnl") is not None


# Round-12 audit fix: trim_journal races with update_scorecard's append
# path. Scheduler runs trim at 3:15 AM ET and scorecard at 4:05 PM ET so
# they don't overlap in practice, but a future refactor could change the
# timing. Use an OS advisory lock (fcntl.flock) on a sibling .lock file
# to serialize trim against any caller using the same file. update_
# scorecard's append path doesn't currently take this lock (it uses its
# own atomic-write pattern), so simultaneous trim + scorecard writes are
# still theoretically possible — but fcntl.flock at least protects
# concurrent trim calls from corrupting each other.
try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


class _TrimLock:
    """Context manager wrapping flock() on `{journal_path}.trim.lock`.

    No-op on Windows (fcntl unavailable). If flock fails for any reason,
    we fall through to an unlocked trim rather than deadlocking — this
    is belt-and-suspenders, not a hard correctness primitive (the
    scheduler's time-of-day gating already prevents concurrent runs in
    production)."""
    def __init__(self, path: str):
        self.path = path + ".trim.lock"
        self.fh = None

    def __enter__(self):
        if not _HAS_FCNTL:
            return self
        try:
            self.fh = open(self.path, "w")
            _fcntl.flock(self.fh.fileno(), _fcntl.LOCK_EX)
        except Exception as e:
            log.warning("trim_journal: lock acquire failed, proceeding unlocked",
                        extra={"path": self.path, "error": str(e)})
            if self.fh:
                try: self.fh.close()
                except Exception: pass
                self.fh = None
        return self

    def __exit__(self, *a):
        if self.fh:
            try: _fcntl.flock(self.fh.fileno(), _fcntl.LOCK_UN)
            except Exception: pass
            try: self.fh.close()
            except Exception: pass
            self.fh = None


def trim_journal(
    journal_path: str,
    *,
    keep_closed_years: float = KEEP_CLOSED_YEARS_DEFAULT,
) -> dict:
    """Move closed trades older than the cutoff into the archive sibling.

    Does nothing if the live file doesn't exist. Open trades never move.
    Trades missing a parseable timestamp stay in live (conservative).
    Serialized via fcntl.flock on a sibling .trim.lock so concurrent
    trim calls can't corrupt each other.

    Returns: {"moved": N, "live_count": L, "archive_count": A,
              "cutoff_iso": "...", "journal_path": ...}
    """
    if not os.path.exists(journal_path):
        return {"moved": 0, "live_count": 0, "archive_count": 0,
                "cutoff_iso": None, "journal_path": journal_path}

    with _TrimLock(journal_path):
        return _trim_journal_locked(journal_path, keep_closed_years)


def _trim_journal_locked(
    journal_path: str, keep_closed_years: float
) -> dict:
    """Inner — runs with the trim lock held. Factored out to keep the
    public trim_journal() signature clean."""
    try:
        with open(journal_path) as f:
            journal = json.load(f) or {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("trim_journal: unable to load live journal",
                    extra={"path": journal_path, "error": str(e)})
        return {"moved": 0, "live_count": 0, "archive_count": 0,
                "cutoff_iso": None, "journal_path": journal_path,
                "error": str(e)}

    trades = list(journal.get("trades") or [])
    if not trades:
        return {"moved": 0, "live_count": 0, "archive_count": 0,
                "cutoff_iso": None, "journal_path": journal_path}

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_closed_years * 365.25)
    keep_live: list = []
    to_archive: list = []
    for t in trades:
        if not _is_closed(t):
            # Open trades always stay live.
            keep_live.append(t)
            continue
        ts = _parse_trade_ts(t)
        if ts is None:
            # Can't determine age — safer to keep live than to lose history.
            keep_live.append(t)
            continue
        if ts >= cutoff:
            keep_live.append(t)
        else:
            to_archive.append(t)

    if not to_archive:
        return {"moved": 0, "live_count": len(keep_live), "archive_count": 0,
                "cutoff_iso": cutoff.isoformat(),
                "journal_path": journal_path}

    # Merge into existing archive (if any) and sort by best available ts so
    # downstream readers can binary-search by date if they ever want to.
    arch_path = archive_path_for(journal_path)
    archive_doc: dict = {}
    if os.path.exists(arch_path):
        try:
            with open(arch_path) as f:
                archive_doc = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as e:
            # Corrupt archive: don't clobber it — bail out of the trim and
            # log. User can hand-fix then re-run.
            log.error("trim_journal: archive file unreadable; skipping trim",
                      extra={"archive_path": arch_path, "error": str(e)})
            return {"moved": 0, "live_count": len(trades), "archive_count": -1,
                    "cutoff_iso": cutoff.isoformat(),
                    "journal_path": journal_path, "error": str(e)}

    archive_trades: list = list(archive_doc.get("trades") or [])
    archive_trades.extend(to_archive)
    # Stable sort by timestamp; trades without a timestamp sink to the start
    # (they pre-date whatever is already there).
    archive_trades.sort(key=lambda t: (_parse_trade_ts(t) or datetime.min.replace(tzinfo=timezone.utc)))
    archive_doc["trades"] = archive_trades
    archive_doc["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Write archive FIRST. If that fails, live file is untouched and we
    # haven't lost anything. If it succeeds, rewrite live.
    _atomic_write_json(arch_path, archive_doc)

    journal["trades"] = keep_live
    _atomic_write_json(journal_path, journal)

    log.info(
        "trade journal trimmed",
        extra={
            "journal_path": journal_path,
            "moved": len(to_archive),
            "live_count": len(keep_live),
            "archive_count": len(archive_trades),
            "cutoff_iso": cutoff.isoformat(),
        },
    )
    return {
        "moved": len(to_archive),
        "live_count": len(keep_live),
        "archive_count": len(archive_trades),
        "cutoff_iso": cutoff.isoformat(),
        "journal_path": journal_path,
    }


def load_all_trades(journal_path: str) -> list:
    """Concatenate live + archived trades for callers that need full history.

    Returns the union of the two files' `trades` lists. Order: archive first
    (chronological), then live. No deduplication — trim_journal guarantees
    no overlap by design.

    Resilient to either file missing or corrupt: returns whatever is loadable.
    """
    combined: list = []
    arch_path = archive_path_for(journal_path)
    for path in (arch_path, journal_path):
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                doc = json.load(f) or {}
            combined.extend(list(doc.get("trades") or []))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("load_all_trades: skipping unreadable file",
                        extra={"path": path, "error": str(e)})
    return combined
