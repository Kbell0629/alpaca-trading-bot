"""
Tests for trade_journal.py — trim + load_all_trades.

Coverage goals (per the PR description that audited this):
  - Empty journal: no-op, no files created.
  - All-open journal: no trades moved, regardless of age.
  - Mixed-age closed trades: only those past the cutoff move.
  - Missing timestamp: stays live (conservative).
  - Running twice: idempotent.
  - Pre-existing archive: new trades appended + sorted correctly.
  - load_all_trades: returns archive + live concatenated, resilient to
    a missing file or corrupt JSON on either side.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

import trade_journal


ET = timezone(timedelta(hours=-4))  # EDT; tests don't care about DST correctness


def _iso(days_ago: float) -> str:
    """ISO-8601 ET string for `days_ago` days before now."""
    dt = datetime.now(ET) - timedelta(days=days_ago)
    return dt.isoformat()


def _mktrade(status: str, days_ago: float, symbol="AAPL", pnl=None, strategy="trailing_stop"):
    return {
        "symbol": symbol,
        "side": "buy" if status == "open" else "sell",
        "qty": 10,
        "price": 150.0,
        "status": status,
        "strategy": strategy,
        "timestamp": _iso(days_ago),
        "closed_at": _iso(days_ago) if status == "closed" else None,
        "pnl": pnl,
    }


@pytest.fixture
def tmpdir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def journal_path(tmpdir):
    return os.path.join(tmpdir, "trade_journal.json")


def _write(path, doc):
    with open(path, "w") as f:
        json.dump(doc, f)


def _read(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


# ---------- trim_journal ----------


def test_trim_noop_when_file_missing(journal_path):
    result = trade_journal.trim_journal(journal_path)
    assert result["moved"] == 0
    assert not os.path.exists(journal_path)
    assert not os.path.exists(trade_journal.archive_path_for(journal_path))


def test_trim_noop_when_no_trades(journal_path):
    _write(journal_path, {"trades": [], "daily_snapshots": []})
    result = trade_journal.trim_journal(journal_path)
    assert result["moved"] == 0
    assert result["live_count"] == 0


def test_open_trades_never_move_regardless_of_age(journal_path):
    # An "open" trade from 5 years ago shouldn't move, even with an
    # aggressive 1-year cutoff. We could argue that's a stale open, but
    # that's a data-quality problem for the caller, not trim_journal's.
    trades = [_mktrade("open", days_ago=365 * 5)]
    _write(journal_path, {"trades": trades})
    result = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    assert result["moved"] == 0
    live = _read(journal_path)
    assert len(live["trades"]) == 1


def test_closed_within_cutoff_stays_live(journal_path):
    trades = [_mktrade("closed", days_ago=100, pnl=50)]
    _write(journal_path, {"trades": trades})
    result = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    assert result["moved"] == 0
    live = _read(journal_path)
    assert len(live["trades"]) == 1


def test_closed_past_cutoff_moves_to_archive(journal_path):
    trades = [
        _mktrade("closed", days_ago=400, pnl=100, symbol="OLD1"),
        _mktrade("closed", days_ago=30, pnl=50, symbol="FRESH"),
        _mktrade("open", days_ago=400, symbol="OPEN_STALE"),
    ]
    _write(journal_path, {"trades": trades})
    result = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    assert result["moved"] == 1
    assert result["live_count"] == 2
    assert result["archive_count"] == 1

    live = _read(journal_path)
    assert {t["symbol"] for t in live["trades"]} == {"FRESH", "OPEN_STALE"}

    archive = _read(trade_journal.archive_path_for(journal_path))
    assert {t["symbol"] for t in archive["trades"]} == {"OLD1"}
    assert "last_updated" in archive


def test_trim_is_idempotent(journal_path):
    trades = [
        _mktrade("closed", days_ago=400, pnl=100, symbol="OLD"),
        _mktrade("closed", days_ago=30, pnl=50, symbol="FRESH"),
    ]
    _write(journal_path, {"trades": trades})
    first = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    second = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    # Second run has nothing left to move.
    assert first["moved"] == 1
    assert second["moved"] == 0
    # Archive hasn't grown.
    archive = _read(trade_journal.archive_path_for(journal_path))
    assert len(archive["trades"]) == 1


def test_trade_without_timestamp_stays_live(journal_path):
    # Conservative behaviour: if we can't tell how old it is, don't move it.
    trades = [
        {"symbol": "NO_TS", "status": "closed", "pnl": 25},
        _mktrade("closed", days_ago=500, pnl=10, symbol="OLD"),
    ]
    _write(journal_path, {"trades": trades})
    result = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    assert result["moved"] == 1

    live = _read(journal_path)
    assert any(t["symbol"] == "NO_TS" for t in live["trades"])
    archive = _read(trade_journal.archive_path_for(journal_path))
    assert all(t["symbol"] != "NO_TS" for t in archive["trades"])


def test_existing_archive_is_extended_not_replaced(journal_path):
    arch_path = trade_journal.archive_path_for(journal_path)
    existing_archive_trades = [
        _mktrade("closed", days_ago=800, pnl=200, symbol="VERY_OLD"),
    ]
    _write(arch_path, {"trades": existing_archive_trades, "last_updated": "prev"})

    new_trades = [
        _mktrade("closed", days_ago=400, pnl=100, symbol="OLD"),
        _mktrade("closed", days_ago=30, pnl=50, symbol="FRESH"),
    ]
    _write(journal_path, {"trades": new_trades})

    result = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    assert result["moved"] == 1
    assert result["archive_count"] == 2  # pre-existing + new

    archive = _read(arch_path)
    symbols = [t["symbol"] for t in archive["trades"]]
    # Sorted oldest-first (VERY_OLD 800d < OLD 400d).
    assert symbols == ["VERY_OLD", "OLD"]
    # last_updated was refreshed.
    assert archive["last_updated"] != "prev"


def test_closed_at_is_preferred_over_timestamp_for_age(journal_path):
    """A trade entered 500 days ago but closed 30 days ago counts as 30d old."""
    trade = _mktrade("closed", days_ago=30, pnl=10)
    trade["timestamp"] = _iso(500)      # entry time
    trade["closed_at"] = _iso(30)       # close time
    _write(journal_path, {"trades": [trade]})

    result = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    assert result["moved"] == 0  # Age derived from closed_at, not timestamp.


def test_preserves_non_trade_fields(journal_path):
    """daily_snapshots and arbitrary top-level keys must survive trimming."""
    trades = [
        _mktrade("closed", days_ago=400, pnl=100, symbol="OLD"),
    ]
    _write(journal_path, {
        "trades": trades,
        "daily_snapshots": [{"date": "2025-01-01", "pnl": 100}],
        "custom_field": {"nested": True},
    })
    trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    live = _read(journal_path)
    assert live["daily_snapshots"] == [{"date": "2025-01-01", "pnl": 100}]
    assert live["custom_field"] == {"nested": True}


def test_corrupt_archive_aborts_without_data_loss(journal_path):
    """If the existing archive is unreadable, trim_journal must NOT clobber
    either file — user's data is safer left untouched until they investigate."""
    arch_path = trade_journal.archive_path_for(journal_path)
    with open(arch_path, "w") as f:
        f.write("{ not valid json")

    original_trades = [
        _mktrade("closed", days_ago=400, pnl=100, symbol="OLD"),
    ]
    _write(journal_path, {"trades": original_trades})

    result = trade_journal.trim_journal(journal_path, keep_closed_years=1.0)
    assert result["moved"] == 0
    assert result["archive_count"] == -1
    # Live file untouched.
    live = _read(journal_path)
    assert len(live["trades"]) == 1
    # Archive still corrupt (we didn't rewrite it).
    with open(arch_path) as f:
        assert "not valid json" in f.read()


# ---------- load_all_trades ----------


def test_load_all_trades_empty(journal_path):
    assert trade_journal.load_all_trades(journal_path) == []


def test_load_all_trades_live_only(journal_path):
    trades = [_mktrade("closed", days_ago=10, pnl=5)]
    _write(journal_path, {"trades": trades})
    result = trade_journal.load_all_trades(journal_path)
    assert len(result) == 1


def test_load_all_trades_archive_only(journal_path):
    arch_path = trade_journal.archive_path_for(journal_path)
    _write(arch_path, {"trades": [_mktrade("closed", days_ago=500, pnl=5)]})
    result = trade_journal.load_all_trades(journal_path)
    assert len(result) == 1


def test_load_all_trades_combined(journal_path):
    arch_path = trade_journal.archive_path_for(journal_path)
    _write(arch_path, {"trades": [_mktrade("closed", days_ago=500, pnl=5, symbol="OLD")]})
    _write(journal_path, {"trades": [_mktrade("closed", days_ago=10, pnl=7, symbol="NEW")]})
    result = trade_journal.load_all_trades(journal_path)
    symbols = [t["symbol"] for t in result]
    # Archive comes first (older).
    assert symbols == ["OLD", "NEW"]


def test_load_all_trades_resilient_to_corrupt_file(journal_path):
    arch_path = trade_journal.archive_path_for(journal_path)
    with open(arch_path, "w") as f:
        f.write("BROKEN")
    _write(journal_path, {"trades": [_mktrade("closed", days_ago=10, pnl=7, symbol="GOOD")]})
    # Archive is corrupt, live is fine — we should still get the live trades.
    result = trade_journal.load_all_trades(journal_path)
    assert [t["symbol"] for t in result] == ["GOOD"]
