"""
Round-45 tests: dual-mode paper + live parallel trading.

Pins the critical invariants:
  * user_data_dir(user_id, mode="paper") returns users/<id>/ (unchanged
    from pre-round-45 — backward compat for existing state files)
  * user_data_dir(user_id, mode="live") returns users/<id>/live/
  * session.mode column defaults to 'paper' for legacy sessions
  * set_session_mode updates the mode; validate_session surfaces it
  * set_live_parallel_enabled toggles the user flag
  * get_user_alpaca_creds honors explicit mode override
  * scheduler _build_user_dict_for_mode returns mode-scoped dicts
  * get_all_users_for_scheduling expands a user into paper+live when
    live_parallel_enabled=1 AND live keys present (not just one or
    the other)
  * Paper and live state trees never collide
"""
from __future__ import annotations

import importlib
import os
import sys as _sys

import pytest


def _reload_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "b" * 64)
    if "auth" in _sys.modules:
        del _sys.modules["auth"]
    import auth
    importlib.reload(auth)
    auth.init_db()
    return auth


def _make_user(auth, email, username):
    uid, err = auth.create_user(
        email, username, "SecurePass123!",
        "PKTESTPAPER123456", "papersecret1234567")
    assert err is None, err
    return uid


# ---------- user_data_dir mode-awareness ----------


def test_user_data_dir_paper_is_legacy_path(tmp_path, monkeypatch):
    """Pre-round-45 behavior: paper mode returns users/<id>/ unchanged.
    No migration should be required for existing users."""
    auth = _reload_auth(tmp_path, monkeypatch)
    d_default = auth.user_data_dir(42)
    d_paper = auth.user_data_dir(42, mode="paper")
    assert d_default == d_paper
    assert d_paper.endswith("/users/42")


def test_user_data_dir_live_is_subdir(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    d_live = auth.user_data_dir(42, mode="live")
    d_paper = auth.user_data_dir(42, mode="paper")
    assert d_live.endswith("/users/42/live")
    assert d_live != d_paper
    # Live subdir must be INSIDE the paper dir (so each user keeps one root)
    assert d_live.startswith(d_paper + "/") or d_live.startswith(d_paper + os.sep)


def test_user_file_paper_and_live_isolated(tmp_path, monkeypatch):
    """Writing a file in paper mode must not be visible in live mode."""
    auth = _reload_auth(tmp_path, monkeypatch)
    paper_path = auth.user_file(1, "trade_journal.json", mode="paper")
    live_path = auth.user_file(1, "trade_journal.json", mode="live")
    assert paper_path != live_path

    # Write to paper, verify live path doesn't exist yet
    with open(paper_path, "w") as f:
        f.write('{"trades":[]}')
    assert os.path.exists(paper_path)
    assert not os.path.exists(live_path)


# ---------- session.mode + switch-mode ----------


def test_session_defaults_to_paper(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    token = auth.create_session(uid, ip_address="127.0.0.1")
    user = auth.validate_session(token)
    assert user is not None
    assert user.get("session_mode") == "paper"


def test_set_session_mode_switches(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    token = auth.create_session(uid, ip_address="127.0.0.1")

    assert auth.set_session_mode(token, "live") is True
    user = auth.validate_session(token)
    assert user["session_mode"] == "live"

    assert auth.set_session_mode(token, "paper") is True
    user = auth.validate_session(token)
    assert user["session_mode"] == "paper"


def test_set_session_mode_rejects_invalid(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    token = auth.create_session(uid, ip_address="127.0.0.1")
    assert auth.set_session_mode(token, "margin") is False
    assert auth.set_session_mode(token, "") is False
    assert auth.set_session_mode("", "live") is False


def test_legacy_session_mode_null_normalized_to_paper(tmp_path, monkeypatch):
    """Sessions created before the mode column was added have NULL
    mode. validate_session must normalize these to 'paper' so legacy
    logins don't get a surprise empty view."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    token = auth.create_session(uid, ip_address="127.0.0.1")
    # Simulate a pre-round-45 row by setting mode to NULL
    conn = auth._get_db()
    conn.execute("UPDATE sessions SET mode = NULL WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    user = auth.validate_session(token)
    assert user["session_mode"] == "paper"


# ---------- get_user_alpaca_creds mode override ----------


def test_get_creds_explicit_paper_mode(tmp_path, monkeypatch):
    """Pass mode='paper' and we should always get the paper keys,
    regardless of the user's live_mode flag."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    # Save live keys separately
    auth.save_user_alpaca_creds(
        uid, live_key="LIVEKEY1234", live_secret="LIVESECRET1234")
    # Even if we flip the old single-mode toggle on
    auth.set_live_mode(uid, True)
    # Explicit mode='paper' must still return paper
    creds = auth.get_user_alpaca_creds(uid, mode="paper")
    assert creds["key"] == "PKTESTPAPER123456"
    assert "paper-api" in creds["endpoint"]


def test_get_creds_explicit_live_mode(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    auth.save_user_alpaca_creds(
        uid, live_key="LIVEKEY1234", live_secret="LIVESECRET1234")
    # Do NOT flip live_mode — just ask explicitly for live creds
    creds = auth.get_user_alpaca_creds(uid, mode="live")
    assert creds["key"] == "LIVEKEY1234"
    assert "paper-api" not in creds["endpoint"]  # live endpoint


# ---------- set_live_parallel_enabled ----------


def test_set_live_parallel_enabled_toggles(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    user = auth.get_user_by_id(uid)
    assert user.get("live_parallel_enabled") in (0, None)
    auth.set_live_parallel_enabled(uid, True)
    assert auth.get_user_by_id(uid).get("live_parallel_enabled") == 1
    auth.set_live_parallel_enabled(uid, False)
    assert auth.get_user_by_id(uid).get("live_parallel_enabled") == 0


# ---------- scheduler user-dict expansion ----------


def test_scheduler_paper_only_by_default(tmp_path, monkeypatch):
    """A user who hasn't opted into parallel mode gets exactly ONE
    scheduler entry (paper), regardless of whether they have live
    keys set. This prevents the "I saved live keys and now the bot
    is trading live money" footgun."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    # Save live keys (user testing them) but DON'T enable parallel
    auth.save_user_alpaca_creds(
        uid, live_key="LIVEKEY1234", live_secret="LIVESECRET1234")

    # Reload cloud_scheduler so it picks up the fresh auth import
    _sys.modules.pop("cloud_scheduler", None)
    import cloud_scheduler as cs
    entries = cs.get_all_users_for_scheduling()
    my_entries = [e for e in entries if e.get("id") == uid]
    assert len(my_entries) == 1
    assert my_entries[0]["_mode"] == "paper"


def test_scheduler_expands_to_both_when_parallel_enabled(tmp_path, monkeypatch):
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    auth.save_user_alpaca_creds(
        uid, live_key="LIVEKEY1234", live_secret="LIVESECRET1234")
    auth.set_live_parallel_enabled(uid, True)

    _sys.modules.pop("cloud_scheduler", None)
    import cloud_scheduler as cs
    entries = cs.get_all_users_for_scheduling()
    my_entries = [e for e in entries if e.get("id") == uid]
    modes = sorted(e["_mode"] for e in my_entries)
    assert modes == ["live", "paper"]

    # Each entry's data_dir must be isolated
    paper = next(e for e in my_entries if e["_mode"] == "paper")
    live = next(e for e in my_entries if e["_mode"] == "live")
    assert paper["_data_dir"] != live["_data_dir"]
    assert live["_data_dir"].endswith("/live")
    # And each has the correct endpoint
    assert "paper-api" in paper["_api_endpoint"]
    assert "paper-api" not in live["_api_endpoint"]


def test_scheduler_skips_live_when_keys_missing(tmp_path, monkeypatch):
    """Even if live_parallel_enabled=1, if live keys aren't saved we
    must NOT add a live entry — otherwise the scheduler would hit
    Alpaca with blank creds and 401 every tick."""
    auth = _reload_auth(tmp_path, monkeypatch)
    uid = _make_user(auth, "a@x.com", "alice")
    auth.set_live_parallel_enabled(uid, True)
    # Deliberately don't save live keys

    _sys.modules.pop("cloud_scheduler", None)
    import cloud_scheduler as cs
    entries = cs.get_all_users_for_scheduling()
    my_entries = [e for e in entries if e.get("id") == uid]
    assert len(my_entries) == 1  # paper only
    assert my_entries[0]["_mode"] == "paper"
