"""
Round-61 user-reported bugs on live SOXL trade (2026-04-24):
  * Today's Closes tagged SOXL close as "[orphan]" even though the
    bot's own trailing stop fired the exit.
  * Positions table labeled a fresh SHORT SOXL position (-29 shares)
    as "TRAILING STOP" strategy instead of "SHORT SELL".

These pin the fixes so both can't silently regress.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch


# ========= Fix 1: error_recovery journals open entries =========

class TestErrorRecoveryJournalsOpen:
    """error_recovery.py used to create a trailing_stop strategy file
    for an orphan Alpaca position but never add a corresponding
    "open" entry to trade_journal.json. When the position later
    closed via stop-trigger, record_trade_close (in cloud_scheduler)
    couldn't find a matching open → fell into the synthetic
    orphan_close branch → close was tagged [orphan] in the
    dashboard's Today's Closes panel. Fix: journal the open alongside
    the strategy file creation."""

    def test_orphan_journal_write_code_exists(self):
        """Pin the journal-append path. A refactor that removes the
        auto-recovered open entry will re-introduce the [orphan] tag
        on every subsequent stop-out of a recovered position."""
        with open("error_recovery.py") as f:
            src = f.read()
        # The journal-path construction
        assert "trade_journal.json" in src, (
            "error_recovery.py must reference trade_journal.json — "
            "fix inserts an open entry so later closes can pair")
        # The append itself, with the auto_recovered sentinel
        assert '"status": "open"' in src, (
            "error_recovery must append an open-status entry")
        assert '"auto_recovered": True' in src, (
            "the appended entry should tag itself auto_recovered=True "
            "so downstream consumers (scorecard, reports) can filter "
            "backfilled opens if needed")
        # The comment explaining WHY
        assert "prevents future [orphan] tag" in src, (
            "the fix comment that ties the journal-write to the "
            "orphan-tag bug must stay so a future refactor doesn't "
            "remove the write thinking it's redundant")

    def test_orphan_journal_write_inside_fix_loop(self):
        """The journal-append must sit inside the `FIXED: Created
        {fname}` block so it runs once per orphan, not somewhere else."""
        with open("error_recovery.py") as f:
            src = f.read()
        # Look for the FIXED line and the journal path nearby
        fixed_idx = src.find("FIXED: Created")
        assert fixed_idx > 0, "FIXED log line moved or removed"
        window = src[fixed_idx:fixed_idx + 2500]
        assert 'trade_journal.json' in window, (
            "journal-write must appear within the same loop as the "
            "strategy-file creation — otherwise recovered orphans can "
            "still close without a matching open")
        assert '"status": "open"' in window
        assert '"auto_recovered": True' in window


# ========= Fix 2: stale strategy files don't claim a symbol =========

class TestMarkAutoDeployedSkipsClosed:
    """server.py._mark_auto_deployed built a symbol_to_strategy map
    by listing strategy files on disk, with priority
    `wheel(3) > trailing_stop(2) > other(1)`. When a long stopped
    out AND the auto-deployer opened a new short on the same
    symbol, BOTH strategy files existed — the stale trailing_stop
    file (status=closed) coexisting with the fresh short_sell file.
    The old priority logic preferred trailing_stop → dashboard
    showed "TRAILING STOP" label on a SHORT -29 share SOXL
    position. Fix: skip strategy files whose status is closed/
    stopped/cancelled so only active files can claim a symbol;
    and put short_sell at the same priority as trailing_stop so
    future ties are broken by file order, not by strategy name."""

    def test_mark_auto_deployed_skips_closed_status(self):
        with open("server.py") as f:
            src = f.read()
        assert '_mark_auto_deployed' in src or 'def _mark_auto_deployed' in src
        # Round-61 pt.21: skip-closed logic delegated to
        # constants.is_closed_status (shared source of truth with
        # error_recovery.list_strategy_files). The literal string-match
        # pin is obsolete; pin the delegation instead.
        assert "is_closed_status" in src, (
            "_mark_auto_deployed must use constants.is_closed_status "
            "to skip stale strategy files — otherwise stale files claim "
            "a symbol and mis-label the active strategy. Pt.21 moved "
            "the closed-status list into constants.py.")
        # Also verify constants.py has the canonical list (single
        # source of truth).
        with open("constants.py") as f:
            cs = f.read()
        for expected in ("closed", "stopped", "cancelled", "canceled",
                         "exited", "filled_and_closed"):
            assert f'"{expected}"' in cs, (
                f"constants.CLOSED_STATUSES missing {expected!r}")

    def test_mark_auto_deployed_short_sell_priority(self):
        """short_sell should get the same priority bucket as
        trailing_stop so a fresh short_sell file wins over a stale
        trailing_stop file that can't be read / is missing status."""
        with open("server.py") as f:
            src = f.read()
        assert '"short_sell": 2' in src, (
            "_mark_auto_deployed priority map must include "
            "short_sell at priority 2 (same tier as trailing_stop) "
            "so short positions get labeled correctly when multiple "
            "strategy files coexist for the same symbol")

    def test_priority_map_still_prefers_wheel(self):
        """Wheel remains top-priority — don't regress the wheel-over-
        trailing-stop relationship (wheel manages options positions
        which have their own exit logic)."""
        with open("server.py") as f:
            src = f.read()
        assert '"wheel": 3' in src, (
            "Wheel must stay at priority 3 (top) — option positions "
            "need the wheel strategy to manage them regardless of "
            "any sibling trailing_stop files")
