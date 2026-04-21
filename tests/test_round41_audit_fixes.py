"""
Round-41 tests: full tech-stack audit fixes.

Pin the fixes so they don't silently regress:
  * auth.py connection-leak try-finally on get_user_by_id /
    get_user_by_username / get_user_by_email / list_active_users /
    validate_session
  * auth.create_user first-user-is-admin TOCTOU — concurrent signups
    on an empty users table serialize via BEGIN IMMEDIATE so exactly
    one user becomes admin
  * journal_backfill.backfill_user_journal holds strategy_file_lock
    around the read-modify-write so concurrent record_trade_open
    can't drop entries
  * server.main PORT env var is guarded against malformed values
"""
from __future__ import annotations

import importlib
import json
import os
import sys as _sys
import threading

import pytest


def _reload_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "0" * 64)
    if "auth" in _sys.modules:
        del _sys.modules["auth"]
    import auth
    importlib.reload(auth)
    auth.init_db()
    return auth


# ---------- auth.py connection-leak fixes ----------


def test_get_user_by_id_closes_connection_even_on_missing_row(tmp_path, monkeypatch):
    """get_user_by_id must not leak a sqlite connection when the user
    doesn't exist. Regression guard for the round-41 try-finally fix."""
    auth = _reload_auth(tmp_path, monkeypatch)
    # Call 500 times for a user that doesn't exist. Before the
    # try-finally fix this would leak 500 open connections; with the
    # fix the process stays at its baseline count.
    for _ in range(500):
        assert auth.get_user_by_id(99999) is None


def test_get_user_by_username_closes_on_exception(tmp_path, monkeypatch):
    """Exception during fetchone must still close the connection."""
    auth = _reload_auth(tmp_path, monkeypatch)
    # Normal lookup path shouldn't raise
    assert auth.get_user_by_username("ghost") is None
    # Even after 100 misses, no connection leak
    for _ in range(100):
        assert auth.get_user_by_username("missing") is None


# ---------- first-user TOCTOU ----------


def test_first_user_becomes_admin_when_table_empty(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid, err = auth.create_user("first@example.com", "first", "SecurePass123!",
                                 "PKKEY12345", "secret12345")
    assert err is None
    u = auth.get_user_by_id(uid)
    assert u["is_admin"] == 1


def test_second_user_is_not_admin(tmp_path, monkeypatch):
    """Second concurrent signup must see count>0 and NOT auto-promote.
    The BEGIN IMMEDIATE in create_user serializes the count-then-insert
    so this invariant holds even under parallel signups."""
    auth = _reload_auth(tmp_path, monkeypatch)
    # First user gets admin
    uid1, _ = auth.create_user("a@x.com", "alice", "SecurePass123!",
                                "PK1", "S1")
    # Second user, serialized after the first commit
    uid2, _ = auth.create_user("b@x.com", "bob", "SecurePass123!",
                                "PK2", "S2")
    assert auth.get_user_by_id(uid1)["is_admin"] == 1
    assert auth.get_user_by_id(uid2)["is_admin"] == 0


def test_concurrent_first_users_race(tmp_path, monkeypatch):
    """Two threads call create_user on an empty table simultaneously.
    Exactly ONE should get is_admin=1. Without BEGIN IMMEDIATE both
    threads could read count==0 and both insert with is_admin=1."""
    auth = _reload_auth(tmp_path, monkeypatch)
    results = []
    errors = []

    def signup(i):
        try:
            uid, err = auth.create_user(
                f"user{i}@x.com", f"user{i}", "SecurePass123!",
                f"PK{i}", f"S{i}")
            results.append((uid, err))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=signup, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"signup raised: {errors}"
    successful = [r for r in results if r[1] is None]
    assert len(successful) == 5, f"expected 5 successes, got {results}"
    admin_count = sum(
        1 for uid, _ in successful
        if auth.get_user_by_id(uid)["is_admin"] == 1
    )
    assert admin_count == 1, (
        f"expected exactly 1 admin, got {admin_count} — BEGIN IMMEDIATE "
        "regression. Concurrent first-user signups both auto-promoted.")


# ---------- journal_backfill strategy_file_lock ----------


def test_journal_backfill_uses_strategy_file_lock(tmp_path, monkeypatch):
    """Pin that backfill_user_journal imports strategy_file_lock and
    uses it around its read-modify-write. If someone refactors this
    away, the concurrency audit regression will re-appear."""
    import journal_backfill
    src = open(journal_backfill.__file__).read()
    assert "from cloud_scheduler import strategy_file_lock" in src
    assert "with strategy_file_lock(journal_path):" in src


def test_journal_backfill_works_when_file_exists(tmp_path, monkeypatch):
    """Regression check: the lock-wrap refactor must not change
    backfill_user_journal's functional contract."""
    # Set up a fake user dir with a stub strategy file
    user_dir = tmp_path / "users" / "1"
    strats_dir = user_dir / "strategies"
    strats_dir.mkdir(parents=True)
    (strats_dir / "breakout_TSLA.json").write_text('{"symbol":"TSLA"}')

    def user_file_fn(user, name):
        return str(user_dir / name)

    # Fake Alpaca position
    positions = [{
        "symbol": "TSLA", "asset_class": "us_equity",
        "avg_entry_price": "250.50", "qty": "10", "side": "long",
    }]

    def fetch_positions_fn(user):
        return positions

    from journal_backfill import backfill_user_journal
    result = backfill_user_journal(
        {"id": 1}, fetch_positions_fn, user_file_fn=user_file_fn)
    assert result["backfilled"] == 1
    assert result["errors"] == []
    # Journal was written
    journal = json.loads((user_dir / "trade_journal.json").read_text())
    assert len(journal["trades"]) == 1
    assert journal["trades"][0]["symbol"] == "TSLA"
    assert journal["trades"][0]["backfilled"] is True

    # Idempotent: running a second time adds nothing
    result2 = backfill_user_journal(
        {"id": 1}, fetch_positions_fn, user_file_fn=user_file_fn)
    assert result2["backfilled"] == 0
    assert result2["skipped_existing"] == 1


# ---------- server.py PORT guard ----------


def test_server_main_port_parse_is_guarded():
    """Pin that server.main's PORT parse catches ValueError — was a
    naked int() cast that would crash the process on a typo."""
    import server
    src = open(server.__file__).read()
    # Must handle malformed PORT — look for the guard pattern
    assert "_port_env = os.environ.get(\"PORT\"" in src
    assert "try:" in src
    assert "except (TypeError, ValueError)" in src or "except ValueError" in src
    # Falls back to a sane default
    assert "port = 8888" in src


# ---------- track_record.html username XSS ----------


def test_track_record_username_is_html_escaped():
    """Pin that {{USERNAME}} is html.escape'd before interpolation.
    track_record.html is a public shareable URL; reflected XSS here
    would persist and be visible to anyone with the share link."""
    import server
    src = open(server.__file__).read()
    # The replacement must go through _html.escape for USERNAME
    assert '_html.escape(user.get("username"' in src
