"""
Round-37 tests: admin edit of username / email.

Covers:
  * update_user with only email → username untouched
  * update_user with only username → email untouched
  * update_user with both → both updated
  * rejects missing user_id
  * rejects invalid username regex
  * rejects invalid email (no @)
  * rejects duplicate email (another user has it)
  * rejects duplicate username (another user has it)
  * allows the same user to "update" to their own email/username
    (no-op, not flagged as duplicate)
  * rejects unknown user_id
  * rejects empty update (both fields None)
"""
from __future__ import annotations

import importlib
import sys as _sys


def _reload_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "0" * 64)
    if "auth" in _sys.modules:
        del _sys.modules["auth"]
    import auth
    importlib.reload(auth)
    auth.init_db()
    return auth


def _make_user(auth, email, username):
    uid, err = auth.create_user(
        email, username, "SecurePass123!",
        "PKTESTKEY1234567890", "testsecret-1234567890")
    assert err is None, err
    return uid


# ---------- happy paths ----------


def test_update_user_changes_email_only(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "old@example.com", "myname")
    ok, err = auth.update_user(uid, email="new@example.com")
    assert ok is True
    assert err is None
    row = auth.get_user_by_id(uid)
    assert row["email"] == "new@example.com"
    assert row["username"] == "myname"  # untouched


def test_update_user_changes_username_only(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "oldname")
    ok, err = auth.update_user(uid, username="newname")
    assert ok is True
    row = auth.get_user_by_id(uid)
    assert row["username"] == "newname"
    assert row["email"] == "me@example.com"  # untouched


def test_update_user_changes_both(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "old@example.com", "oldname")
    ok, err = auth.update_user(
        uid, email="new@example.com", username="newname")
    assert ok is True
    row = auth.get_user_by_id(uid)
    assert row["email"] == "new@example.com"
    assert row["username"] == "newname"


def test_update_user_same_values_is_noop_success(tmp_path, monkeypatch):
    """A no-op 'update' to the user's own current values should succeed,
    not trigger the uniqueness-conflict path."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "myname")
    ok, err = auth.update_user(uid, email="me@example.com", username="myname")
    assert ok is True
    assert err is None


# ---------- validation failures ----------


def test_update_user_rejects_missing_user_id(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "me@example.com", "myname")
    ok, err = auth.update_user(0, email="x@y.com")
    assert ok is False
    ok2, err2 = auth.update_user(None, email="x@y.com")
    assert ok2 is False


def test_update_user_rejects_empty_update(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "myname")
    ok, err = auth.update_user(uid)  # no fields
    assert ok is False
    assert "no fields" in err.lower()


def test_update_user_rejects_bad_username_format(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "myname")
    # Too short
    ok, err = auth.update_user(uid, username="ab")
    assert ok is False
    # Invalid chars
    ok2, err2 = auth.update_user(uid, username="bad name!")
    assert ok2 is False
    # Too long
    ok3, err3 = auth.update_user(uid, username="a" * 31)
    assert ok3 is False


def test_update_user_rejects_bad_email(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "myname")
    ok, err = auth.update_user(uid, email="not-an-email")
    assert ok is False
    ok2, err2 = auth.update_user(uid, email="")
    assert ok2 is False


def test_update_user_rejects_unknown_user(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "me@example.com", "myname")
    ok, err = auth.update_user(9999, email="x@y.com")
    assert ok is False
    assert "not found" in err.lower()


# ---------- uniqueness ----------


def test_update_user_rejects_duplicate_email(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "alice@example.com", "alice")
    bob_id = _make_user(auth, "bob@example.com", "bob")
    # Bob tries to take alice's email
    ok, err = auth.update_user(bob_id, email="alice@example.com")
    assert ok is False
    assert "email" in err.lower()


def test_update_user_rejects_duplicate_username(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "alice@example.com", "alice")
    bob_id = _make_user(auth, "bob@example.com", "bob")
    ok, err = auth.update_user(bob_id, username="alice")
    assert ok is False
    assert "username" in err.lower()


def test_update_user_allows_reclaiming_own_values(tmp_path, monkeypatch):
    """Edge case: admin re-submits the form without changing anything.
    The uniqueness check filters on `id != target_id` so the target's
    own current row doesn't trip the duplicate path."""
    auth = _reload_auth(tmp_path, monkeypatch)
    _make_user(auth, "alice@example.com", "alice")
    bob_id = _make_user(auth, "bob@example.com", "bob")
    ok, err = auth.update_user(
        bob_id, email="bob@example.com", username="bob")
    assert ok is True


def test_update_user_trims_whitespace(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "me@example.com", "myname")
    ok, err = auth.update_user(
        uid, email="  trimmed@example.com  ", username="  newname  ")
    assert ok is True
    row = auth.get_user_by_id(uid)
    assert row["email"] == "trimmed@example.com"
    assert row["username"] == "newname"
