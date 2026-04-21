"""
Round-40 tests: delete_user, export_user_data, journal_backfill.

Covers the three helpers that landed in this round:
  * auth.delete_user — cascades through data dir, invites, sessions;
    refuses last-admin, refuses self-delete
  * auth.export_user_data — ZIP bundle with sanitized profile + audit
    log + strategies + journal (GDPR-style)
  * journal_backfill.backfill_user_journal — adds missing "open"
    entries for pre-round-33 positions; idempotent
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys as _sys
import zipfile

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


def _make_user(auth, email, username, is_admin=False):
    uid, err = auth.create_user(
        email, username, "SecurePass123!",
        "PKTESTKEY1234567890", "testsecret-1234567890")
    assert err is None, err
    if is_admin:
        ok, _ = auth.set_user_admin(uid, True)
        assert ok
    return uid


# ---------- delete_user guard rails ----------


def test_delete_user_refuses_self_deletion(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "admin@example.com", "admin")
    ok, err = auth.delete_user(uid, actor_user_id=uid)
    assert ok is False
    assert "your own account" in err.lower()


def test_delete_user_refuses_last_active_admin(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    # admin is the only admin
    uid = _make_user(auth, "admin@example.com", "admin")
    # Different actor — someone else trying to delete the last admin
    ok, err = auth.delete_user(uid, actor_user_id=None)
    assert ok is False
    assert "last active admin" in err.lower()


def test_delete_user_allows_removing_secondary_admin(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")  # admin #1
    uid2 = _make_user(auth, "admin2@example.com", "admin2", is_admin=True)
    ok, err = auth.delete_user(uid2, actor_user_id=None)
    assert ok is True
    assert err is None
    assert auth.get_user_by_id(uid2) is None


def test_delete_user_allows_removing_regular_user(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")
    uid2 = _make_user(auth, "friend@example.com", "friend")
    ok, err = auth.delete_user(uid2, actor_user_id=None)
    assert ok is True
    assert auth.get_user_by_id(uid2) is None


def test_delete_user_rejects_missing_user_id(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")
    ok, err = auth.delete_user(0)
    assert ok is False
    ok2, err2 = auth.delete_user(None)
    assert ok2 is False
    ok3, err3 = auth.delete_user("not-a-number")
    assert ok3 is False


def test_delete_user_rejects_unknown_user(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")
    ok, err = auth.delete_user(9999)
    assert ok is False
    assert "not found" in err.lower()


def test_delete_user_cascades_data_dir(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")
    uid2 = _make_user(auth, "friend@example.com", "friend")
    # Create a per-user data dir with a file in it
    udir = os.path.join(str(tmp_path), "users", str(uid2))
    os.makedirs(udir, exist_ok=True)
    with open(os.path.join(udir, "trade_journal.json"), "w") as f:
        json.dump({"trades": []}, f)
    ok, _ = auth.delete_user(uid2, actor_user_id=None)
    assert ok is True
    # Data dir should be gone
    assert not os.path.exists(udir)


def test_delete_user_cascades_invites(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    admin_id = _make_user(auth, "admin@example.com", "admin")
    uid2 = _make_user(auth, "friend@example.com", "friend")
    # friend created an invite (implausible but valid — normally only
    # admins create invites, but we're just testing cascade)
    auth.create_invite(uid2, note="test")
    ok, _ = auth.delete_user(uid2, actor_user_id=admin_id)
    assert ok is True
    # Invite gone
    invites = auth.list_invites(created_by_user_id=uid2)
    assert invites == []


# ---------- export_user_data ----------


def test_export_user_data_produces_valid_zip(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "myname")
    zip_bytes, err = auth.export_user_data(uid)
    assert err is None
    assert zip_bytes is not None
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = z.namelist()
    assert "README.txt" in names
    assert "profile.json" in names
    assert "sessions.json" in names
    assert "audit_log.json" in names
    assert "invites.json" in names


def test_export_user_data_sanitizes_sensitive_fields(tmp_path, monkeypatch):
    """Password hash + encrypted Alpaca keys should never appear in
    the exported profile."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "myname")
    zip_bytes, err = auth.export_user_data(uid)
    assert err is None
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    profile = json.loads(z.read("profile.json"))
    assert "password_hash" not in profile
    assert "password_salt" not in profile
    assert "alpaca_key_encrypted" not in profile
    assert "alpaca_secret_encrypted" not in profile
    # But the non-sensitive fields are there
    assert profile["username"] == "myname"
    assert profile["email"] == "me@example.com"


def test_export_user_data_includes_per_user_files(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "myname")
    # Seed a strategy file + journal
    udir = os.path.join(str(tmp_path), "users", str(uid))
    os.makedirs(os.path.join(udir, "strategies"), exist_ok=True)
    with open(os.path.join(udir, "strategies", "trailing_stop_TSLA.json"), "w") as f:
        json.dump({"symbol": "TSLA"}, f)
    with open(os.path.join(udir, "trade_journal.json"), "w") as f:
        json.dump({"trades": [{"symbol": "TSLA"}]}, f)
    zip_bytes, err = auth.export_user_data(uid)
    assert err is None
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = z.namelist()
    # Files from the user dir should be included (relative paths)
    assert "trade_journal.json" in names
    assert "strategies/trailing_stop_TSLA.json" in names


def test_export_user_data_rejects_unknown_user(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "me@example.com", "myname")
    zip_bytes, err = auth.export_user_data(9999)
    assert zip_bytes is None
    assert "not found" in err.lower()


def test_export_user_data_rejects_missing_id(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    zb, err = auth.export_user_data(0)
    assert zb is None
    zb2, err2 = auth.export_user_data(None)
    assert zb2 is None


# ---------- journal_backfill ----------


@pytest.fixture
def _cs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "0" * 64)
    for m in ("cloud_scheduler", "scheduler_api", "journal_backfill"):
        if m in _sys.modules:
            del _sys.modules[m]
    import cloud_scheduler
    importlib.reload(cloud_scheduler)
    import journal_backfill
    importlib.reload(journal_backfill)
    monkeypatch.setattr(cloud_scheduler, "user_file",
                        lambda user, fn: os.path.join(str(tmp_path), fn))
    return {"cs": cloud_scheduler, "bf": journal_backfill,
            "path": str(tmp_path),
            "user": {"id": 1, "username": "test"}}


def test_backfill_adds_missing_open_entry(_cs):
    bf = _cs["bf"]
    user = _cs["user"]
    # Seed a strategy file so _detect_strategy_file matches
    sdir = os.path.join(_cs["path"], "strategies")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "trailing_stop_SOXL.json"), "w") as f:
        json.dump({"symbol": "SOXL"}, f)
    # No existing journal
    def _fetch(u):
        return [{"symbol": "SOXL", "asset_class": "us_equity",
                 "qty": "117", "avg_entry_price": "85.11", "side": "long"}]
    monkeypatch_user_file = lambda u, fn: os.path.join(_cs["path"], fn)
    result = bf.backfill_user_journal(user, _fetch,
                                        user_file_fn=monkeypatch_user_file)
    assert result["backfilled"] == 1
    assert result["skipped_existing"] == 0
    with open(os.path.join(_cs["path"], "trade_journal.json")) as f:
        journal = json.load(f)
    entry = journal["trades"][0]
    assert entry["symbol"] == "SOXL"
    assert entry["strategy"] == "trailing_stop"
    assert entry["price"] == 85.11
    assert entry["qty"] == 117
    assert entry["side"] == "buy"
    assert entry["backfilled"] is True


def test_backfill_is_idempotent(_cs):
    """Running backfill twice should not duplicate entries."""
    bf = _cs["bf"]
    user = _cs["user"]
    sdir = os.path.join(_cs["path"], "strategies")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "trailing_stop_SOXL.json"), "w") as f:
        json.dump({"symbol": "SOXL"}, f)
    def _fetch(u):
        return [{"symbol": "SOXL", "asset_class": "us_equity",
                 "qty": "117", "avg_entry_price": "85.11", "side": "long"}]
    ufn = lambda u, fn: os.path.join(_cs["path"], fn)
    r1 = bf.backfill_user_journal(user, _fetch, user_file_fn=ufn)
    assert r1["backfilled"] == 1
    r2 = bf.backfill_user_journal(user, _fetch, user_file_fn=ufn)
    assert r2["backfilled"] == 0
    assert r2["skipped_existing"] == 1


def test_backfill_handles_option_underlying(_cs):
    """A HIMS put should match the wheel strategy file named with
    the underlying HIMS, not the OCC symbol."""
    bf = _cs["bf"]
    user = _cs["user"]
    sdir = os.path.join(_cs["path"], "strategies")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "wheel_HIMS.json"), "w") as f:
        json.dump({"symbol": "HIMS"}, f)
    def _fetch(u):
        return [{"symbol": "HIMS260508P00027000",
                 "asset_class": "us_option",
                 "qty": "-1", "avg_entry_price": "2.05", "side": "short"}]
    ufn = lambda u, fn: os.path.join(_cs["path"], fn)
    result = bf.backfill_user_journal(user, _fetch, user_file_fn=ufn)
    assert result["backfilled"] == 1
    with open(os.path.join(_cs["path"], "trade_journal.json")) as f:
        j = json.load(f)
    entry = j["trades"][0]
    assert entry["symbol"] == "HIMS260508P00027000"
    assert entry["strategy"] == "wheel"
    assert entry["side"] == "sell_short"
    assert entry["qty"] == -1


def test_backfill_skips_positions_without_strategy_file(_cs):
    """If no strategy file matches, don't synthesize a journal entry —
    we'd be guessing at the strategy."""
    bf = _cs["bf"]
    user = _cs["user"]
    # No strategy files at all
    os.makedirs(os.path.join(_cs["path"], "strategies"), exist_ok=True)
    def _fetch(u):
        return [{"symbol": "ORPHAN", "asset_class": "us_equity",
                 "qty": "10", "avg_entry_price": "50.00", "side": "long"}]
    ufn = lambda u, fn: os.path.join(_cs["path"], fn)
    result = bf.backfill_user_journal(user, _fetch, user_file_fn=ufn)
    assert result["backfilled"] == 0
    assert result["skipped_no_strategy"] == 1


def test_backfill_tolerates_fetch_error(_cs):
    bf = _cs["bf"]
    user = _cs["user"]
    def _fetch(u):
        return {"error": "401 Unauthorized"}
    ufn = lambda u, fn: os.path.join(_cs["path"], fn)
    result = bf.backfill_user_journal(user, _fetch, user_file_fn=ufn)
    assert result["backfilled"] == 0
    assert result["errors"]


def test_backfill_tolerates_non_list_positions(_cs):
    bf = _cs["bf"]
    user = _cs["user"]
    ufn = lambda u, fn: os.path.join(_cs["path"], fn)
    result = bf.backfill_user_journal(user, lambda u: "not a list",
                                        user_file_fn=ufn)
    assert result["backfilled"] == 0
    assert result["errors"]
