"""
Round-36 tests: admin invite-revoke + admin-toggle helpers.

Covers:
  * revoke_invite: sets expires_at to the past → check_invite returns "expired"
  * revoke_invite: returns False for already-used invite (no-op, no silent change)
  * revoke_invite: returns False for non-existent token_hash
  * set_user_admin: promotes / demotes a user's is_admin flag
  * set_user_admin: REFUSES to demote the last active admin (guard rail)
  * set_user_admin: inactive admins do not count toward the "last admin" check
"""
from __future__ import annotations

import importlib
import sys as _sys

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
    # The first create_user auto-promotes to admin; for subsequent users
    # we use set_user_admin where requested.
    if is_admin:
        ok, _ = auth.set_user_admin(uid, True)
        assert ok
    return uid


# ---------- revoke_invite ----------


def test_revoke_invite_makes_active_invite_expired(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "admin@example.com", "admin")
    token = auth.create_invite(uid, note="friend1")
    # Active before revoke
    ok, reason = auth.check_invite(token)
    assert ok
    # Revoke it
    hashed = auth._hash_invite_token(token)
    assert auth.revoke_invite(hashed) is True
    # Now reads as expired
    ok2, reason2 = auth.check_invite(token)
    assert not ok2
    assert "expired" in reason2


def test_revoke_invite_rejects_already_used(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "admin@example.com", "admin")
    uid2 = _make_user(auth, "friend@example.com", "friend")
    token = auth.create_invite(uid)
    ok, _ = auth.consume_invite(token, uid2)
    assert ok
    # Now try to revoke — should return False because it's USED not ACTIVE
    hashed = auth._hash_invite_token(token)
    assert auth.revoke_invite(hashed) is False


def test_revoke_invite_rejects_unknown_hash(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")
    assert auth.revoke_invite("not-a-real-hash") is False
    assert auth.revoke_invite("") is False


def test_revoke_invite_rejects_none(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")
    assert auth.revoke_invite(None) is False


# ---------- set_user_admin ----------


def test_set_user_admin_promotes_regular_user(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")  # auto-promoted first user
    uid2 = _make_user(auth, "friend@example.com", "friend")
    # Friend starts as non-admin
    row = auth.get_user_by_id(uid2)
    assert row["is_admin"] == 0
    # Promote
    ok, err = auth.set_user_admin(uid2, True)
    assert ok is True
    assert err is None
    row2 = auth.get_user_by_id(uid2)
    assert row2["is_admin"] == 1


def test_set_user_admin_demotes_secondary_admin(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")  # admin #1 (auto)
    uid2 = _make_user(auth, "admin2@example.com", "admin2",
                      is_admin=True)  # admin #2
    # Demote admin #2 → should succeed because admin #1 still exists
    ok, err = auth.set_user_admin(uid2, False)
    assert ok is True
    assert err is None
    row = auth.get_user_by_id(uid2)
    assert row["is_admin"] == 0


def test_set_user_admin_refuses_to_demote_last_admin(tmp_path, monkeypatch):
    """Guard rail: if you demote the only active admin, no one can
    access the admin panel anymore. Return a clear error."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "admin@example.com", "admin")  # lone admin
    ok, err = auth.set_user_admin(uid, False)
    assert ok is False
    assert err is not None
    assert "last admin" in err.lower()
    # Still admin
    row = auth.get_user_by_id(uid)
    assert row["is_admin"] == 1


def test_set_user_admin_ignores_inactive_admins_in_last_check(tmp_path, monkeypatch):
    """Inactive admins don't count as a safety net — if the only other
    admin is deactivated, demoting the last ACTIVE admin is still blocked."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid1 = _make_user(auth, "admin@example.com", "admin")
    uid2 = _make_user(auth, "admin2@example.com", "admin2", is_admin=True)
    # Deactivate admin #2 using a direct DB update (helper API is outside scope)
    conn = auth._get_db()
    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid2,))
    conn.commit()
    conn.close()
    # Now try to demote admin #1 — should fail
    ok, err = auth.set_user_admin(uid1, False)
    assert ok is False
    assert "last admin" in err.lower()


def test_set_user_admin_handles_unknown_user(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")
    # User id 999 doesn't exist
    ok, err = auth.set_user_admin(999, True)
    # Returns False (no row updated), but doesn't raise
    assert ok is False


def test_set_user_admin_rejects_missing_user_id(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "admin@example.com", "admin")
    ok, err = auth.set_user_admin(0, True)
    assert ok is False
    ok2, err2 = auth.set_user_admin(None, True)
    assert ok2 is False
