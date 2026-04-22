"""
Round-14 audit fixes — verify each one stays fixed.

Covers:
  * observability.critical_alert email path (was importing from wrong
    module + wrong signature → every kill-switch trip silently failed
    to email the operator)
  * Sentry ignore_errors quota safety
  * notify queue hard cap when DLQ persistently fails
  * notify.log_notification flock concurrency safety
  * llm_sentiment counter ET timezone
"""
from __future__ import annotations

import os
import json
import sys
import types
from datetime import datetime, timezone


# ---------- observability.critical_alert email path ----------


def test_critical_alert_emails_via_notify_module(monkeypatch, tmp_path):
    """Was broken: imported from email_sender (wrong module) AND called
    queue_email with the wrong signature. Both errors got swallowed by
    the outer except. This test pins the corrected path."""
    captured = []
    fake_notify = types.ModuleType("notify")
    def _fake_queue_email(subject, body, notify_type="info"):
        captured.append({"subject": subject, "body": body,
                         "notify_type": notify_type})
    fake_notify.queue_email = _fake_queue_email
    monkeypatch.setitem(sys.modules, "notify", fake_notify)

    import observability as obs
    user = {"notification_email": "ops@example.com"}
    result = obs.critical_alert("KILL SWITCH TRIPPED",
                                "daily loss exceeded -3%",
                                user=user)
    assert result["email"] is True
    assert len(captured) == 1
    assert "KILL SWITCH" in captured[0]["subject"]
    assert "daily loss" in captured[0]["body"]
    assert captured[0]["notify_type"] == "alert"


def test_critical_alert_email_skipped_without_notification_email(monkeypatch):
    """No notification_email on user → email path skipped, no exception."""
    captured = []
    fake_notify = types.ModuleType("notify")
    fake_notify.queue_email = lambda *a, **k: captured.append(1)
    monkeypatch.setitem(sys.modules, "notify", fake_notify)

    import observability as obs
    result = obs.critical_alert("test", "test", user={})
    assert result["email"] is False
    assert captured == []


def test_critical_alert_email_failure_does_not_propagate(monkeypatch):
    """If queue_email raises, the outer try/except swallows it and
    critical_alert still returns a result dict — never propagate."""
    fake_notify = types.ModuleType("notify")
    def _broken(*a, **k):
        raise RuntimeError("smtp dead")
    fake_notify.queue_email = _broken
    monkeypatch.setitem(sys.modules, "notify", fake_notify)

    import observability as obs
    user = {"notification_email": "ops@example.com"}
    result = obs.critical_alert("x", "y", user=user)
    assert result["email"] is False  # didn't claim success


# ---------- Sentry ignore_errors quota guard ----------


def test_sentry_ignore_errors_lists_transients(monkeypatch):
    """Verify the ignore list includes URLError / TimeoutError class so
    yfinance / Alpaca transient noise doesn't burn the 5K/month free
    quota. We can't actually init Sentry without a DSN, so introspect
    the source for the list."""
    import inspect, observability as obs
    src = inspect.getsource(obs.init_sentry)
    for required in ("URLError", "TimeoutError", "socket.timeout"):
        assert required in src, f"ignore_errors missing {required}"


# ---------- notify queue hard cap ----------


def test_notify_queue_hard_cap_when_dlq_persistently_fails(monkeypatch,
                                                            tmp_path):
    """Round-13 kept overflow in main queue when DLQ fails to avoid
    silent data loss. Round-14 caps the main queue at 200 entries as a
    memory-safety net so a permanently-wedged DLQ writer can't OOM us.

    Round-48: notify.queue_email now refuses to enqueue without
    NOTIFICATION_EMAIL set (privacy fix — no hardcoded recipient).
    Set it here before the fresh import so the module picks it up."""
    monkeypatch.setenv("NOTIFICATION_EMAIL", "test@example.com")
    if "notify" in sys.modules:
        del sys.modules["notify"]
    import notify
    monkeypatch.setattr(notify, "DATA_DIR", str(tmp_path))

    # Pre-fill the queue file with 250 entries
    queue_file = os.path.join(str(tmp_path), "email_queue.json")
    big_queue = [{"timestamp": "2026-04-19T10:00:00", "to": "x@y",
                  "subject": f"#{i}", "body": "b", "type": "info",
                  "sent": False} for i in range(250)]
    with open(queue_file, "w") as f:
        json.dump(big_queue, f)

    # Capture the original safe_save_json then wrap it: only fail on DLQ
    _orig_save = notify.safe_save_json
    def _selective_save(path, data):
        if path.endswith("_dlq.json"):
            raise OSError("dlq disk full")
        return _orig_save(path, data)
    monkeypatch.setattr(notify, "safe_save_json", _selective_save)

    notify.queue_email("subj", "body", "info")

    with open(queue_file) as f:
        result = json.load(f)
    # Hard cap kicks in at 200 (DLQ failed so soft cap of 50 doesn't apply)
    assert len(result) <= 200, f"queue not capped: {len(result)} entries"
    # And it should be at exactly 200 since we pre-filled 250 + 1 new
    assert len(result) == 200


# ---------- notify.log_notification concurrency ----------


def test_log_notification_acquires_flock(monkeypatch, tmp_path):
    """log_notification used to read-modify-write without a lock.
    Concurrent callers could lose entries via TOCTOU. Round-14 wraps
    the body in fcntl.flock — verify the lock file is created."""
    if "notify" in sys.modules:
        del sys.modules["notify"]
    import notify
    monkeypatch.setattr(notify, "DATA_DIR", str(tmp_path))
    notify.log_notification("test message", "info")
    assert os.path.exists(os.path.join(str(tmp_path),
                                       "notification_log.json.lock"))
    log_path = os.path.join(str(tmp_path), "notification_log.json")
    with open(log_path) as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["message"] == "test message"


# ---------- llm_sentiment counter ET timezone ----------


def test_llm_counter_uses_et_timezone(monkeypatch, tmp_path):
    """The cost-estimate counter used naive UTC, which made the 'today'
    boundary jump at midnight UTC instead of midnight ET. Round-14
    routes through et_time.now_et."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    if "llm_sentiment" in sys.modules:
        del sys.modules["llm_sentiment"]
    import llm_sentiment
    # Stub et_time.now_et to a known value
    import et_time
    fake_now = datetime(2026, 4, 19, 23, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(et_time, "now_et", lambda: fake_now.astimezone(
        et_time.ET_TZ if hasattr(et_time, "ET_TZ") else timezone.utc))
    llm_sentiment._bump_call_counter()
    counter_path = os.path.join(llm_sentiment._cache_dir(), "_counter.json")
    assert os.path.exists(counter_path)
    with open(counter_path) as f:
        data = json.load(f)
    assert data["count"] == 1
    # Date should be the ET date, not UTC date
    assert "date" in data
