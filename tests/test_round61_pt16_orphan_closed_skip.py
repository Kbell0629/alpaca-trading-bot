"""Round-61 pt.16 — error_recovery must ignore closed strategy files
so its orphan-detection agrees with the dashboard's MANUAL labels.

User-reported: after pt.15 shipped, clicking "🤖 Adopt MANUAL -> AUTO"
returned "No MANUAL positions found that need adoption" — but the
dashboard still showed the position as MANUAL. The two code paths had
different definitions of "has a strategy file":

- `server.py::_mark_auto_deployed` (dashboard labeling — round-61 #110)
  SKIPS files whose status is closed / stopped / cancelled / etc. So
  a stale `trailing_stop_SOXL.json` (status=closed) doesn't claim the
  SOXL symbol, and the position shows MANUAL.

- `error_recovery.py::list_strategy_files` returned EVERY file in
  STRATEGIES_DIR regardless of status. So the same stale file DID
  claim the symbol, `strategy_symbol_map` included it, and the
  orphan-detection loop skipped the position ("already has a
  strategy file"). User sees MANUAL in UI but "no orphans" from the
  adopt button.

Fix: `list_strategy_files` now filters closed statuses using the same
set as `_mark_auto_deployed`. `monitor_strategies` already filters on
the active side (`status in ("active", "awaiting_fill")` at
cloud_scheduler.py:1246), so no risk of double-managing when the new
active file coexists with the stale closed one.
"""
from __future__ import annotations

import json
import os


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# Source pins
# ----------------------------------------------------------------------------

def test_list_strategy_files_documents_closed_filter():
    # Round-61 pt.21: the closed-status list moved into constants.py
    # so error_recovery.py and server.py share a single source of
    # truth. Pin the delegation at call-site + the canonical list in
    # constants.py (not the local variable inside error_recovery
    # anymore).
    src = _src("error_recovery.py")
    assert "is_closed_status" in src, (
        "list_strategy_files must import constants.is_closed_status "
        "for the filter — pt.21 moved the list there to prevent drift "
        "with server._mark_auto_deployed.")
    cs = _src("constants.py")
    for s in ("closed", "stopped", "cancelled", "canceled",
              "exited", "filled_and_closed"):
        assert f'"{s}"' in cs, (
            f"constants.CLOSED_STATUSES must include {s!r} — that's "
            "the single source of truth both error_recovery and "
            "server._mark_auto_deployed read from.")


def test_list_strategy_files_references_dashboard_contract():
    """The regression happens when the two filters drift. Keep a
    pointer in the comment so any future refactor of the statuses
    list updates both places."""
    src = _src("error_recovery.py")
    assert "_mark_auto_deployed" in src, (
        "error_recovery.py must reference _mark_auto_deployed in a "
        "comment so readers know the two code paths must stay in "
        "sync on the closed-status filter.")


# ----------------------------------------------------------------------------
# Behavioral: list_strategy_files skips closed files
# ----------------------------------------------------------------------------

def test_list_strategy_files_skips_closed_file(tmp_path, monkeypatch):
    """A strategy file with status=closed must NOT appear in the
    returned dict — matches _mark_auto_deployed's behavior."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    # Stale closed file (pre-existing from a prior session)
    (sdir / "trailing_stop_SOXL.json").write_text(json.dumps({
        "symbol": "SOXL", "strategy": "trailing_stop", "status": "closed",
    }))
    # Active file for a different symbol
    (sdir / "breakout_CRDO.json").write_text(json.dumps({
        "symbol": "CRDO", "strategy": "breakout", "status": "active",
    }))
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    result = er.list_strategy_files()
    # Only the active CRDO file should be returned.
    assert "breakout_CRDO.json" in result
    assert "trailing_stop_SOXL.json" not in result, (
        "Stale closed file must be filtered — otherwise it masquerades "
        "as an active strategy and suppresses orphan adoption.")


def test_list_strategy_files_keeps_all_non_closed_statuses(tmp_path, monkeypatch):
    """Active, awaiting_fill, paused, and any custom status must
    still appear — we ONLY filter the explicit closed set."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "a_AAA.json").write_text(json.dumps({
        "symbol": "AAA", "strategy": "a", "status": "active"}))
    (sdir / "b_BBB.json").write_text(json.dumps({
        "symbol": "BBB", "strategy": "b", "status": "awaiting_fill"}))
    (sdir / "c_CCC.json").write_text(json.dumps({
        "symbol": "CCC", "strategy": "c", "status": "paused"}))
    (sdir / "d_DDD.json").write_text(json.dumps({
        "symbol": "DDD", "strategy": "d"}))  # no status field
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    result = er.list_strategy_files()
    assert set(result.keys()) == {"a_AAA.json", "b_BBB.json",
                                   "c_CCC.json", "d_DDD.json"}


def test_list_strategy_files_filters_all_closed_aliases(tmp_path, monkeypatch):
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    for status in ("closed", "stopped", "cancelled", "canceled",
                   "exited", "filled_and_closed"):
        fname = f"x_{status.upper()}.json"
        (sdir / fname).write_text(json.dumps({
            "symbol": status.upper(), "strategy": "x", "status": status}))
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    result = er.list_strategy_files()
    assert result == {}, (
        "All six closed-status aliases must be filtered, got: "
        f"{list(result.keys())}")


def test_list_strategy_files_closed_filter_is_case_insensitive(tmp_path, monkeypatch):
    """Users / migrations may have written statuses with mixed case.
    We lowercase before comparing to catch "Closed" / "CLOSED" /
    "Cancelled" etc."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "trailing_stop_SOXL.json").write_text(json.dumps({
        "symbol": "SOXL", "strategy": "trailing_stop", "status": "CLOSED"}))
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    assert er.list_strategy_files() == {}
