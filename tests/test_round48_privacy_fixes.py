"""
Round-48 tests: cross-user privacy fixes after user reported seeing
another user's trade emails + activity log entries.

Four real issues, all pinned here:

  1. notify.py had EMAIL_RECIPIENT hardcoded to "se2login@gmail.com".
     Every user's subprocess call wrote to that address, so all users'
     trade alerts piled up in the bootstrap admin's inbox.
  2. notify.py wrote to a SHARED DATA_DIR/email_queue.json — no
     per-user isolation at the writer layer even though the drainer
     supported per-user queues.
  3. email_sender.drain_all happily drained the shared root queue
     (see #2), forwarding cross-user emails to the hardcoded recipient.
  4. /api/scheduler-status's `is_admin=True` callers saw the unfiltered
     activity log for all users. Bootstrap admin (user_id=1) is
     admin by default, so their dashboard silently showed every
     other user's scheduler events.
"""
from __future__ import annotations

import json
import os
import sys as _sys


# ---------- notify.py privacy ----------


def test_notify_queue_email_refuses_without_NOTIFICATION_EMAIL(tmp_path, monkeypatch):
    """queue_email must NOT silently default to a hardcoded recipient
    when NOTIFICATION_EMAIL is unset."""
    monkeypatch.delenv("NOTIFICATION_EMAIL", raising=False)
    if "notify" in _sys.modules:
        del _sys.modules["notify"]
    import notify
    monkeypatch.setattr(notify, "DATA_DIR", str(tmp_path))
    notify.queue_email("subj", "body", "info")
    queue_file = os.path.join(str(tmp_path), "email_queue.json")
    assert not os.path.exists(queue_file), (
        "queue_email wrote to disk even though NOTIFICATION_EMAIL is unset — "
        "that's the bug round-48 fixes. Without a recipient we must drop "
        "the email entirely rather than risk misrouting it.")


def test_notify_queue_email_honors_env_recipient(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_EMAIL", "user-42@example.com")
    if "notify" in _sys.modules:
        del _sys.modules["notify"]
    import notify
    monkeypatch.setattr(notify, "DATA_DIR", str(tmp_path))
    notify.queue_email("subj", "body", "info")
    queue_file = os.path.join(str(tmp_path), "email_queue.json")
    assert os.path.exists(queue_file)
    with open(queue_file) as f:
        q = json.load(f)
    assert len(q) == 1
    assert q[0]["to"] == "user-42@example.com", (
        f"expected user-42@example.com, got {q[0]['to']} — hardcoded "
        "fallback is back.")


def test_notify_no_longer_has_hardcoded_recipient():
    """Grep-level pin: notify.py must not contain the old hardcoded
    address. If someone re-introduces it, this test fires."""
    with open("notify.py") as f:
        src = f.read()
    # Allow the string in comments/docstrings as historical reference,
    # but NOT as code. Simple check: no literal assignment.
    assert 'EMAIL_RECIPIENT = "se2login@gmail.com"' not in src


# ---------- cloud_scheduler.notify_user passes per-user env ----------


def test_notify_user_passes_notification_email_env(tmp_path, monkeypatch):
    """notify_user must set env['NOTIFICATION_EMAIL'] to the user's
    per-user notification_email before spawning the notify.py
    subprocess."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "f" * 64)
    for m in ("cloud_scheduler", "auth", "scheduler_api"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kw):
            captured["env"] = env or {}
            captured["cmd"] = cmd
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def kill(self): pass
        def terminate(self): pass

    monkeypatch.setattr(cloud_scheduler.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(cloud_scheduler, "_track_child", lambda p: None)

    user = {
        "id": 7, "username": "alice",
        "_ntfy_topic": "alpaca-bot-alice",
        "_notification_email": "alice@example.com",
        "_data_dir": str(tmp_path / "users" / "7"),
    }
    cloud_scheduler.notify_user(user, "test trade", "trade")
    assert captured["env"].get("NOTIFICATION_EMAIL") == "alice@example.com"
    assert captured["env"].get("NTFY_TOPIC") == "alpaca-bot-alice"
    # DATA_DIR routed to the user's dir so notify.py writes per-user queue
    assert captured["env"].get("DATA_DIR") == str(tmp_path / "users" / "7")


def test_notify_user_no_notification_email_leaves_env_clean(tmp_path, monkeypatch):
    """If a user has no notification_email set, notify_user must NOT
    leak a stale NOTIFICATION_EMAIL from the parent process env into
    the subprocess (which could misroute to a previous user's address).
    """
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "f" * 64)
    monkeypatch.setenv("NOTIFICATION_EMAIL", "wrong-parent@example.com")
    for m in ("cloud_scheduler", "auth", "scheduler_api"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kw):
            captured["env"] = env or {}
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def kill(self): pass
        def terminate(self): pass

    monkeypatch.setattr(cloud_scheduler.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(cloud_scheduler, "_track_child", lambda p: None)

    # User WITHOUT notification_email
    user = {"id": 9, "username": "bob",
            "_ntfy_topic": "alpaca-bot-bob",
            "_data_dir": str(tmp_path / "users" / "9")}
    cloud_scheduler.notify_user(user, "test", "trade")
    # Must not leak the parent's stale value
    assert "NOTIFICATION_EMAIL" not in captured["env"], (
        "notify_user inherited stale NOTIFICATION_EMAIL from parent env — "
        "this would misroute user 9's emails to 'wrong-parent@example.com'.")


# ---------- email_sender refuses shared root queue ----------


def test_email_sender_quarantines_shared_root_queue(tmp_path, monkeypatch):
    """drain_all must NOT drain DATA_DIR/email_queue.json (the shared
    legacy queue from pre-round-48). Instead it should quarantine it
    to a .dead file so a human can review."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_USER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    # Seed a shared root queue (simulating pre-round-48 leftovers)
    root_q = os.path.join(str(tmp_path), "email_queue.json")
    with open(root_q, "w") as f:
        json.dump([{"to": "se2login@gmail.com", "subject": "leaked",
                    "body": "cross-user leak", "type": "trade",
                    "sent": False, "timestamp": "2026-04-22T10:00:00"}], f)

    # Reload email_sender so it rebinds DATA_DIR
    for m in ("email_sender",):
        _sys.modules.pop(m, None)
    import email_sender

    # Stub the SMTP session open to avoid real network
    class _FakeSession:
        def sendmail(self, *a, **k): pass
        def quit(self): pass
    monkeypatch.setattr(email_sender, "_open_session", lambda: _FakeSession())

    email_sender.drain_all()

    # Root queue must be quarantined, not drained
    assert not os.path.exists(root_q), (
        "Shared root queue still present — drain_all should have "
        "quarantined it to .pre-round48.dead")
    dead = root_q + ".pre-round48.dead"
    assert os.path.exists(dead), "Quarantine marker .pre-round48.dead missing"


# ---------- Activity log: admins filtered by default ----------


def test_scheduler_status_admin_is_filtered_by_default(tmp_path, monkeypatch):
    """Regression pin for the cross-user activity-log bleed the user
    reported. The bootstrap admin (Kbell0629) was seeing
    [godguruselfone] entries in their activity log because round-39's
    filter exempted admins. Round-48 privacy-by-default: admins only
    see their own activity unless they explicitly pass ?all=1.
    """
    import server
    src = open(server.__file__).read()
    # Pin the new behavior — see scheduler-status handler
    assert "_show_unfiltered = bool(_cu.get(\"is_admin\")) and _see_all" in src
    # Pin the opt-in query param
    assert '_see_all = _params.get("all"' in src
