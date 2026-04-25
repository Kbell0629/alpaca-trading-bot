"""Round-61 pt.56 — daily picks-history snapshot.

The pt.49 ``pipeline_backtest`` module replays a sequence of
historical screener picks through the production deploy gates, but
nothing was writing that sequence to disk. Pt.56 plugs the gap:

  1. ``snapshot_picks(date, picks, path)`` appends today's picks to
     a per-user ``picks_history.json`` file, keyed by date.
  2. ``load_picks_history(path)`` reads it back as a list of
     ``{date, picks}`` dicts ready for
     ``pipeline_backtest.run_pipeline_backtest``.
  3. ``trim_to_max_days(history, max_days)`` caps storage.

Pure module — no I/O assumed beyond a path string. Tests use
tmp_path and the round-trip path.

Schema on disk:
    {
      "version": 1,
      "snapshots": {
        "2026-04-25": {
          "picks": [pick_dict, pick_dict, ...]
        },
        "2026-04-26": {...}
      }
    }

Bounded at 90 days by default (~3 months of daily snapshots) so
the file stays small even after a year of trading.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Mapping


DEFAULT_MAX_DAYS: int = 90


def snapshot_picks(date_str: str, picks, path: str,
                    *, max_days: int = DEFAULT_MAX_DAYS) -> None:
    """Append today's picks to the on-disk history file at `path`.
    Idempotent on the same date (overwrites existing entry).
    Atomic write via tempfile + rename.

    `date_str` should be ISO ``YYYY-MM-DD``. `picks` is the list of
    pick dicts the screener produced.
    """
    if not date_str or not isinstance(picks, list):
        return
    existing = _load_raw(path)
    snapshots = existing.get("snapshots") or {}
    snapshots[date_str] = {"picks": picks}
    # Trim oldest beyond max_days when the file grows.
    if max_days and len(snapshots) > max_days:
        # Keep the most-recent max_days entries by sorted date.
        keep = dict(sorted(snapshots.items())[-max_days:])
        snapshots = keep
    out = {"version": 1, "snapshots": snapshots}
    _atomic_write(path, out)


def load_picks_history(path: str) -> list:
    """Return ``[{"date": d, "picks": [...]}, ...]`` sorted by date
    ascending. Empty list if file missing / unreadable."""
    raw = _load_raw(path)
    snapshots = raw.get("snapshots") or {}
    if not isinstance(snapshots, Mapping):
        return []
    out = []
    for d in sorted(snapshots.keys()):
        entry = snapshots.get(d) or {}
        picks = entry.get("picks") or []
        if isinstance(picks, list):
            out.append({"date": d, "picks": picks})
    return out


def trim_to_max_days(history: list, max_days: int) -> list:
    """Trim a loaded history list to the last `max_days` entries."""
    if not isinstance(history, list) or max_days <= 0:
        return []
    return history[-max_days:]


# ============================================================================
# Internal: atomic file IO
# ============================================================================

def _load_raw(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _atomic_write(path: str, data: dict) -> None:
    """Tempfile + rename to avoid partial writes on crash."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".picks_hist_", suffix=".tmp",
                                  dir=parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
