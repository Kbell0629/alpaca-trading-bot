"""
Round-43 tests: retroactively fix orphan wheel-close entries by
recovering entry price from wheel state history.

Covers:
  * Happy path: sell_to_open_put + put_filled event in history pairs
    with matching orphan_close entry — patched with fill_price
  * Idempotent: re-running doesn't double-patch
  * Skips if no wheel files exist
  * Skips if no matching OCC symbol in history (e.g., history trimmed)
  * Fill price WINS over limit price (more authoritative)
  * Clears orphan_close flag + stamps open_backfilled marker
  * Computes pnl_pct correctly for short-cover math
"""
from __future__ import annotations

import json
import os

import pytest


def _setup_user(tmp_path):
    """Build minimal user-dir structure mirroring cloud_scheduler.user_file."""
    user_dir = tmp_path / "users" / "1"
    (user_dir / "strategies").mkdir(parents=True)

    def user_file_fn(user, name):
        return str(user_dir / name)

    return user_dir, user_file_fn


def _write_wheel_file(user_dir, symbol: str, history: list):
    """Write a wheel_<SYMBOL>.json with a given history list."""
    path = user_dir / "strategies" / f"wheel_{symbol}.json"
    state = {
        "strategy": "wheel",
        "symbol": symbol,
        "history": history,
        "active_contract": None,
        "stage": "stage_1_searching",
    }
    path.write_text(json.dumps(state))
    return str(path)


def _orphan_entry(contract_sym: str, exit_price: float, pnl: float):
    """Build a journal entry shaped like record_trade_close's synthetic
    orphan (round-34 format)."""
    return {
        "timestamp": "2026-04-22T21:53:00-04:00",
        "symbol": contract_sym,
        "side": "buy",
        "qty": -1,
        "price": None,  # unknown entry — the orphan hallmark
        "strategy": "wheel",
        "reason": "synthetic open entry — original never journaled",
        "deployer": "record_trade_close_orphan",
        "status": "closed",
        "orphan_close": True,
        "exit_timestamp": "2026-04-22T21:53:00-04:00",
        "exit_price": exit_price,
        "exit_reason": "put closed externally",
        "exit_side": "buy",
        "pnl": pnl,
        "pnl_pct": None,
    }


def _list_wheel_files_stub(user_dir):
    def _lw(user):
        out = []
        sdir = user_dir / "strategies"
        for f in os.listdir(str(sdir)):
            if f.startswith("wheel_") and f.endswith(".json"):
                state = json.loads((sdir / f).read_text())
                out.append((f, state))
        return out
    return _lw


# ---------- happy path ----------


def test_backfill_patches_orphan_from_fill_event(tmp_path):
    user_dir, ufn = _setup_user(tmp_path)
    contract = "CHWY260515P00025000"
    _write_wheel_file(user_dir, "CHWY", history=[
        {"event": "sell_to_open_put",
         "detail": {"contract": contract, "limit_price": 0.22}},
        {"event": "put_filled",
         "detail": {"fill_price": 0.25, "premium_received": 25.0}},
        {"event": "put_closed_externally",
         "detail": {"exit_price": 0.35}},
    ])
    journal_path = user_dir / "trade_journal.json"
    journal_path.write_text(json.dumps({
        "trades": [_orphan_entry(contract, exit_price=0.35, pnl=-10.0)],
    }))

    from wheel_open_backfill import backfill_wheel_opens
    result = backfill_wheel_opens(
        {"id": 1}, user_file_fn=ufn,
        list_wheel_fn=_list_wheel_files_stub(user_dir))

    assert result["patched"] == 1
    assert result["errors"] == []

    journal = json.loads(journal_path.read_text())
    t = journal["trades"][0]
    assert t["price"] == 0.25  # fill price (authoritative) wins over limit
    assert "orphan_close" not in t
    assert t["open_backfilled"] is True
    # Short-cover pnl_pct = (entry/exit - 1) * 100 = (0.25/0.35 - 1)*100
    assert t["pnl_pct"] == round((0.25 / 0.35 - 1) * 100, 2)


def test_backfill_falls_back_to_limit_price_when_no_fill_event(tmp_path):
    user_dir, ufn = _setup_user(tmp_path)
    contract = "ABC260101P00050000"
    # Only sell_to_open — no corresponding fill event (history trimmed?)
    _write_wheel_file(user_dir, "ABC", history=[
        {"event": "sell_to_open_put",
         "detail": {"contract": contract, "limit_price": 0.40}},
    ])
    journal_path = user_dir / "trade_journal.json"
    journal_path.write_text(json.dumps({
        "trades": [_orphan_entry(contract, exit_price=0.50, pnl=-10)],
    }))

    from wheel_open_backfill import backfill_wheel_opens
    result = backfill_wheel_opens(
        {"id": 1}, user_file_fn=ufn,
        list_wheel_fn=_list_wheel_files_stub(user_dir))

    assert result["patched"] == 1
    t = json.loads(journal_path.read_text())["trades"][0]
    assert t["price"] == 0.40  # limit price used as fallback


# ---------- idempotency ----------


def test_backfill_is_idempotent(tmp_path):
    user_dir, ufn = _setup_user(tmp_path)
    contract = "CHWY260515P00025000"
    _write_wheel_file(user_dir, "CHWY", history=[
        {"event": "sell_to_open_put",
         "detail": {"contract": contract, "limit_price": 0.25}},
    ])
    journal_path = user_dir / "trade_journal.json"
    journal_path.write_text(json.dumps({
        "trades": [_orphan_entry(contract, exit_price=0.35, pnl=-10)],
    }))

    from wheel_open_backfill import backfill_wheel_opens
    lwf = _list_wheel_files_stub(user_dir)

    r1 = backfill_wheel_opens({"id": 1}, user_file_fn=ufn, list_wheel_fn=lwf)
    r2 = backfill_wheel_opens({"id": 1}, user_file_fn=ufn, list_wheel_fn=lwf)

    assert r1["patched"] == 1
    # Second run: no more orphans to patch (round-1 cleared the flag)
    assert r2["patched"] == 0

    # Journal was only patched once — not double-modified
    t = json.loads(journal_path.read_text())["trades"][0]
    assert t["price"] == 0.25
    assert t["open_backfilled"] is True


# ---------- skip conditions ----------


def test_backfill_skips_no_matching_contract(tmp_path):
    """Orphan exists for contract X; wheel history has data for
    contract Y only. Must skip without error and not touch the orphan."""
    user_dir, ufn = _setup_user(tmp_path)
    _write_wheel_file(user_dir, "OTHER", history=[
        {"event": "sell_to_open_put",
         "detail": {"contract": "OTHER260101P00010000", "limit_price": 0.10}},
    ])
    journal_path = user_dir / "trade_journal.json"
    # Orphan for a DIFFERENT contract with no history match
    journal_path.write_text(json.dumps({
        "trades": [_orphan_entry("MISSING260515P00025000",
                                  exit_price=0.35, pnl=-10)],
    }))

    from wheel_open_backfill import backfill_wheel_opens
    result = backfill_wheel_opens(
        {"id": 1}, user_file_fn=ufn,
        list_wheel_fn=_list_wheel_files_stub(user_dir))

    assert result["patched"] == 0
    assert result["skipped_no_match"] == 1
    # Orphan still orphan
    t = json.loads(journal_path.read_text())["trades"][0]
    assert t.get("orphan_close") is True
    assert t.get("price") is None


def test_backfill_skips_when_no_wheel_files(tmp_path):
    user_dir, ufn = _setup_user(tmp_path)
    # Journal has an orphan but NO wheel files at all
    journal_path = user_dir / "trade_journal.json"
    journal_path.write_text(json.dumps({
        "trades": [_orphan_entry("X260515P00025000",
                                  exit_price=0.35, pnl=-10)],
    }))

    from wheel_open_backfill import backfill_wheel_opens
    result = backfill_wheel_opens(
        {"id": 1}, user_file_fn=ufn,
        list_wheel_fn=_list_wheel_files_stub(user_dir))

    assert result["patched"] == 0
    assert result["skipped_no_history"] == 1


def test_backfill_skips_non_orphan_entries(tmp_path):
    """A wheel close that already has a matching open (no orphan_close
    flag) must NOT be modified even if history has the contract."""
    user_dir, ufn = _setup_user(tmp_path)
    contract = "CHWY260515P00025000"
    _write_wheel_file(user_dir, "CHWY", history=[
        {"event": "sell_to_open_put",
         "detail": {"contract": contract, "limit_price": 0.25}},
    ])
    journal_path = user_dir / "trade_journal.json"
    # Existing close (properly paired; no orphan_close flag)
    clean = _orphan_entry(contract, exit_price=0.35, pnl=-10)
    clean.pop("orphan_close")
    clean["price"] = 0.25  # already has entry price
    journal_path.write_text(json.dumps({"trades": [clean]}))

    from wheel_open_backfill import backfill_wheel_opens
    result = backfill_wheel_opens(
        {"id": 1}, user_file_fn=ufn,
        list_wheel_fn=_list_wheel_files_stub(user_dir))

    assert result["patched"] == 0
    t = json.loads(journal_path.read_text())["trades"][0]
    assert "open_backfilled" not in t  # wasn't touched


def test_backfill_skips_non_wheel_strategies(tmp_path):
    """Orphan close for a non-wheel strategy (e.g. HTZ breakout) must
    not be patched even if the OCC-symbol-keyed entry map happens to
    match. Only wheel orphans are in scope."""
    user_dir, ufn = _setup_user(tmp_path)
    _write_wheel_file(user_dir, "HTZ", history=[])  # no history either
    journal_path = user_dir / "trade_journal.json"
    orphan = _orphan_entry("HTZ", exit_price=8.5, pnl=-120.0)
    orphan["strategy"] = "breakout"
    journal_path.write_text(json.dumps({"trades": [orphan]}))

    from wheel_open_backfill import backfill_wheel_opens
    result = backfill_wheel_opens(
        {"id": 1}, user_file_fn=ufn,
        list_wheel_fn=_list_wheel_files_stub(user_dir))

    assert result["patched"] == 0
    t = json.loads(journal_path.read_text())["trades"][0]
    assert t.get("orphan_close") is True  # still orphan
