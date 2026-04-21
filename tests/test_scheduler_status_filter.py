"""
Round-39: scheduler_status filters activity log + user roster by
current caller's username for non-admins.

Before round-39, /api/scheduler-status returned the full unfiltered
_recent_logs ring buffer + the full list of usernames. Every
authenticated user could see every other user's scheduler events.
Also pins the `_has_user_tag` helper (distinguishes `[username]`
tags from `[task]` tags like `[scheduler]`).
"""
from __future__ import annotations

import importlib
import sys as _sys

import pytest


@pytest.fixture
def _cs(tmp_path, monkeypatch):
    """Import cloud_scheduler with a tmp DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "0" * 64)
    for m in ("cloud_scheduler", "scheduler_api"):
        if m in _sys.modules:
            del _sys.modules[m]
    import cloud_scheduler
    importlib.reload(cloud_scheduler)
    return cloud_scheduler


# ---------- _has_user_tag helper ----------


def test_has_user_tag_detects_username(_cs):
    assert _cs._has_user_tag("[Kbell0629] Screener completed") is True
    assert _cs._has_user_tag("[monitor] [Kbell0629] stop raised") is True


def test_has_user_tag_ignores_task_tags(_cs):
    # Task tags alone don't count as user tags.
    assert _cs._has_user_tag("[scheduler] heartbeat") is False
    assert _cs._has_user_tag("[screener] starting batch") is False
    assert _cs._has_user_tag("[migration] round20_position_cap applied") is False


def test_has_user_tag_empty_or_none(_cs):
    assert _cs._has_user_tag("") is False
    assert _cs._has_user_tag(None) is False


def test_has_user_tag_handles_mixed(_cs):
    # Bracketed non-task non-user strings (like 6-digit dates) shouldn't
    # match because they're not 3-30 alphanumeric.
    assert _cs._has_user_tag("[scheduler] started at [15:00]") is False


# ---------- get_scheduler_status filtering ----------


def _seed_logs(cs, entries):
    """Helper: push entries into the ring buffer."""
    with cs._logs_lock:
        cs._recent_logs.clear()
        for e in entries:
            cs._recent_logs.append(e)


def test_non_admin_sees_only_own_user_logs(_cs, monkeypatch):
    # Stub get_all_users_for_scheduling so we don't need a real DB
    monkeypatch.setattr(_cs, "get_all_users_for_scheduling",
                        lambda: [
                            {"id": 1, "username": "alice",
                             "_api_endpoint": "paper", "_api_key": "x",
                             "_api_secret": "y", "_data_endpoint": "d"},
                            {"id": 2, "username": "bob",
                             "_api_endpoint": "paper", "_api_key": "x",
                             "_api_secret": "y", "_data_endpoint": "d"},
                        ])
    monkeypatch.setattr(_cs, "user_api_get", lambda u, p: {"is_open": True})
    _seed_logs(_cs, [
        {"ts": "1:00", "ts_iso": "2026-04-21T13:00:00-04:00",
         "task": "scheduler", "msg": "[scheduler] heartbeat"},
        {"ts": "1:01", "ts_iso": "2026-04-21T13:01:00-04:00",
         "task": "screener", "msg": "[screener] [alice] Starting screener..."},
        {"ts": "1:02", "ts_iso": "2026-04-21T13:02:00-04:00",
         "task": "screener", "msg": "[screener] [bob] Starting screener..."},
        {"ts": "1:03", "ts_iso": "2026-04-21T13:03:00-04:00",
         "task": "monitor", "msg": "[monitor] [alice] SOXL: Stop raised"},
    ])
    status = _cs.get_scheduler_status(filter_username="alice", is_admin=False)
    msgs = [e["msg"] for e in status["recent_logs"]]
    # Alice's entries + the generic heartbeat: 3 rows.  Bob's entry
    # filtered out.
    assert "[scheduler] heartbeat" in msgs
    assert "[screener] [alice] Starting screener..." in msgs
    assert "[monitor] [alice] SOXL: Stop raised" in msgs
    assert all("bob" not in m for m in msgs)
    # Users roster trimmed to just Alice
    usernames = [u["username"] for u in status["users"]]
    assert usernames == ["alice"]


def test_admin_sees_everyone(_cs, monkeypatch):
    monkeypatch.setattr(_cs, "get_all_users_for_scheduling",
                        lambda: [
                            {"id": 1, "username": "alice",
                             "_api_endpoint": "paper", "_api_key": "x",
                             "_api_secret": "y", "_data_endpoint": "d"},
                            {"id": 2, "username": "bob",
                             "_api_endpoint": "paper", "_api_key": "x",
                             "_api_secret": "y", "_data_endpoint": "d"},
                        ])
    monkeypatch.setattr(_cs, "user_api_get", lambda u, p: {"is_open": True})
    _seed_logs(_cs, [
        {"ts": "1:00", "ts_iso": "2026-04-21T13:00:00-04:00",
         "task": "scheduler", "msg": "[scheduler] heartbeat"},
        {"ts": "1:01", "ts_iso": "2026-04-21T13:01:00-04:00",
         "task": "screener", "msg": "[screener] [alice] Starting..."},
        {"ts": "1:02", "ts_iso": "2026-04-21T13:02:00-04:00",
         "task": "screener", "msg": "[screener] [bob] Starting..."},
    ])
    status = _cs.get_scheduler_status(filter_username="alice", is_admin=True)
    msgs = [e["msg"] for e in status["recent_logs"]]
    assert any("alice" in m for m in msgs)
    assert any("bob" in m for m in msgs)
    # Admin sees full roster
    usernames = [u["username"] for u in status["users"]]
    assert set(usernames) == {"alice", "bob"}


def test_missing_filter_returns_unfiltered(_cs, monkeypatch):
    """Back-compat: no params = legacy behavior (everything).
    Only used internally / by /healthz; user-facing route always passes
    filter_username."""
    monkeypatch.setattr(_cs, "get_all_users_for_scheduling",
                        lambda: [
                            {"id": 1, "username": "alice",
                             "_api_endpoint": "paper", "_api_key": "x",
                             "_api_secret": "y", "_data_endpoint": "d"},
                        ])
    monkeypatch.setattr(_cs, "user_api_get", lambda u, p: {"is_open": True})
    _seed_logs(_cs, [
        {"ts": "1:00", "ts_iso": "2026-04-21T13:00:00-04:00",
         "task": "scheduler", "msg": "[scheduler] heartbeat"},
        {"ts": "1:01", "ts_iso": "2026-04-21T13:01:00-04:00",
         "task": "screener", "msg": "[screener] [alice] Starting..."},
    ])
    status = _cs.get_scheduler_status()
    assert len(status["recent_logs"]) == 2
