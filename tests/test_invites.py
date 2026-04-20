"""
Tests for round-26 single-use signup invites.

Covers:
  * create_invite generates a plaintext token + stores hash only
  * check_invite accepts unused / unexpired; rejects missing / used /
    expired tokens
  * consume_invite is atomic — second concurrent caller returns
    "already used" via rowcount==0 (simulated by calling twice)
  * list_invites returns creator's invites newest-first
"""
from __future__ import annotations

import importlib
import sys
import time

import pytest


def _reload_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "0" * 64)
    if "auth" in sys.modules:
        del sys.modules["auth"]
    import auth
    importlib.reload(auth)
    auth.init_db()
    return auth


def _make_admin(auth):
    uid, err = auth.create_user(
        "admin@example.com", "admin", "SecurePass123!",
        "PKTESTKEY1234567890", "testsecret-1234567890")
    assert err is None, err
    return uid


# ---------- create_invite ----------


def test_create_invite_returns_plaintext_token(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_admin(auth)
    token = auth.create_invite(uid, note="friend1")
    # Plaintext tokens are ~43 chars of urlsafe base64
    assert len(token) >= 30
    # DB should have the HASH, not the plaintext
    conn = auth._get_db()
    row = conn.execute(
        "SELECT token_hash, note FROM invites WHERE created_by_user_id = ?",
        (uid,)).fetchone()
    conn.close()
    assert row is not None
    assert row["token_hash"] != token  # hashed
    assert row["token_hash"] == auth._hash_invite_token(token)
    assert row["note"] == "friend1"


# ---------- check_invite ----------


def test_check_invite_accepts_unused_unexpired(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_admin(auth)
    token = auth.create_invite(uid)
    ok, reason = auth.check_invite(token)
    assert ok, reason
    assert reason is None


def test_check_invite_rejects_missing_token(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    ok, reason = auth.check_invite("not-a-real-token")
    assert not ok
    assert "not found" in reason


def test_check_invite_rejects_expired(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_admin(auth)
    token = auth.create_invite(uid)
    # Backdate expires_at via raw SQL
    from datetime import timedelta
    from et_time import now_et
    past = (now_et() - timedelta(hours=1)).isoformat()
    conn = auth._get_db()
    conn.execute(
        "UPDATE invites SET expires_at = ? WHERE token_hash = ?",
        (past, auth._hash_invite_token(token)))
    conn.commit()
    conn.close()
    ok, reason = auth.check_invite(token)
    assert not ok
    assert "expired" in reason


def test_check_invite_rejects_used(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_admin(auth)
    # Create a second user who'll consume the invite
    uid2, err = auth.create_user(
        "friend@example.com", "friend", "SecurePass123!",
        "PKTESTKEY1234567890", "testsecret-1234567890")
    assert err is None
    token = auth.create_invite(uid)
    ok, _ = auth.consume_invite(token, uid2)
    assert ok
    ok2, reason = auth.check_invite(token)
    assert not ok2
    assert "already used" in reason


# ---------- consume_invite ----------


def test_consume_invite_marks_used_and_second_call_fails(tmp_path, monkeypatch):
    """Atomicity: the second consume_invite call must fail because the
    first already set used_at. WHERE used_at IS NULL on the UPDATE
    means only one caller can succeed."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_admin(auth)
    uid2, _ = auth.create_user(
        "f2@example.com", "friend2", "SecurePass123!",
        "PKTESTKEY1234567890", "testsecret-1234567890")
    uid3, _ = auth.create_user(
        "f3@example.com", "friend3", "SecurePass123!",
        "PKTESTKEY1234567890", "testsecret-1234567890")
    token = auth.create_invite(uid)
    ok1, _ = auth.consume_invite(token, uid2)
    ok2, reason = auth.consume_invite(token, uid3)
    assert ok1
    assert not ok2
    assert "already used" in reason


def test_consume_invite_rejects_expired(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_admin(auth)
    uid2, _ = auth.create_user(
        "f@example.com", "friend_e", "SecurePass123!",
        "PKTESTKEY1234567890", "testsecret-1234567890")
    token = auth.create_invite(uid)
    from datetime import timedelta
    from et_time import now_et
    past = (now_et() - timedelta(hours=1)).isoformat()
    conn = auth._get_db()
    conn.execute(
        "UPDATE invites SET expires_at = ? WHERE token_hash = ?",
        (past, auth._hash_invite_token(token)))
    conn.commit()
    conn.close()
    ok, reason = auth.consume_invite(token, uid2)
    assert not ok
    assert "expired" in reason


# ---------- list_invites ----------


def test_list_invites_filters_by_creator_and_orders_newest_first(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_admin(auth)
    # Second admin
    uid2, _ = auth.create_user(
        "admin2@example.com", "admin2", "SecurePass123!",
        "PKTESTKEY1234567890", "testsecret-1234567890")
    # Create 2 from admin, 1 from admin2
    auth.create_invite(uid, note="first")
    time.sleep(0.01)  # ensure ordering by created_at is stable
    auth.create_invite(uid, note="second")
    auth.create_invite(uid2, note="other-admin-invite")
    rows = auth.list_invites(uid)
    assert len(rows) == 2
    # Newest first
    assert rows[0]["note"] == "second"
    assert rows[1]["note"] == "first"
    # The other admin's invite isn't in this creator's list
    assert all(r["note"] != "other-admin-invite" for r in rows)
