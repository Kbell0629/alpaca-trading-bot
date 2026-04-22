"""
Round-49 tests: tuned task-staleness watchdog to filter opening-bell
Alpaca-latency false alarms.

Motivating case: user got a "Task monitor for user Kbell0629 is 128s
behind (expected every 60s)" email at 9:34 AM ET — 4 minutes after
market open. The scheduler wasn't stuck; Alpaca was just slow during
the opening-bell crush. The next tick caught up fine, but the alert
had already fired.

Round-49 changes:
  1. Threshold multiplier 2× → 3× (monitor: 120s → 180s)
  2. 2-observation debounce (transient overdue bumps don't page)
  3. Suppress entire check during 9:30-9:50 ET opening-bell window
"""
from __future__ import annotations

import sys as _sys
import time


def _reload(monkeypatch):
    """Reload cloud_scheduler with MASTER_ENCRYPTION_KEY set so auth.py
    import at module load time doesn't raise."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "g" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


# ---------- new threshold multiplier ----------


def test_staleness_threshold_is_3x(monkeypatch):
    cs = _reload(monkeypatch)
    assert cs._STALENESS_MULT == 3, (
        "round-49 raised the multiplier from 2× to 3× to tolerate "
        "opening-bell Alpaca latency")


# ---------- opening-bell suppression ----------


def test_suppressed_during_opening_bell_window(monkeypatch):
    cs = _reload(monkeypatch)
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))  # EDT
    # 9:34 AM ET — within the 9:30-9:50 suppression window
    fake_now = datetime(2026, 4, 22, 9, 34, 0, tzinfo=et)
    monkeypatch.setattr(cs, "now_et", lambda: fake_now)
    assert cs._within_opening_bell_congestion() is True


def test_not_suppressed_after_opening_bell_window(monkeypatch):
    cs = _reload(monkeypatch)
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))
    # 10:15 AM ET — well past the window
    fake_now = datetime(2026, 4, 22, 10, 15, 0, tzinfo=et)
    monkeypatch.setattr(cs, "now_et", lambda: fake_now)
    assert cs._within_opening_bell_congestion() is False


def test_not_suppressed_before_market_open(monkeypatch):
    cs = _reload(monkeypatch)
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))
    # 9:29 AM ET — 1 minute before market open
    fake_now = datetime(2026, 4, 22, 9, 29, 0, tzinfo=et)
    monkeypatch.setattr(cs, "now_et", lambda: fake_now)
    assert cs._within_opening_bell_congestion() is False


def test_check_task_staleness_exits_early_during_opening_bell(monkeypatch):
    """Full suppression during 9:30-9:50 — even if a task is 10 minutes
    overdue, no alert fires."""
    cs = _reload(monkeypatch)
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))
    monkeypatch.setattr(cs, "now_et",
                         lambda: datetime(2026, 4, 22, 9, 40, 0, tzinfo=et))
    cs._last_runs["monitor_1"] = time.time() - 600  # 10 min ago
    cs._staleness_last_alert.clear()
    cs._staleness_overdue_count.clear()
    fired = []
    monkeypatch.setattr(cs, "notify_user",
                         lambda u, m, t: fired.append((u["id"], m)))
    cs._check_task_staleness([{"id": 1, "username": "test"}])
    assert fired == [], (
        "suppression window failed — got notification during 9:30-9:50 ET")


# ---------- 2-observation debounce ----------


def test_first_overdue_observation_does_not_alert(monkeypatch):
    """128s-past-threshold on the first pass must NOT fire. The user's
    original bug was exactly this — one slow tick paged them."""
    cs = _reload(monkeypatch)
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))
    # 10:00 AM ET — past the opening-bell window
    monkeypatch.setattr(cs, "now_et",
                         lambda: datetime(2026, 4, 22, 10, 0, 0, tzinfo=et))
    # Simulate monitor task 200s behind (just past the 180s threshold)
    cs._last_runs["monitor_1"] = time.time() - 200
    cs._staleness_last_alert.clear()
    cs._staleness_overdue_count.clear()
    fired = []
    monkeypatch.setattr(cs, "notify_user",
                         lambda u, m, t: fired.append((u["id"], m)))

    cs._check_task_staleness([{"id": 1, "username": "test"}])
    assert fired == [], (
        "first overdue observation fired an alert — debounce missing")
    # Counter should be bumped to 1
    assert cs._staleness_overdue_count.get("monitor_1") == 1


def test_second_overdue_observation_fires_alert(monkeypatch):
    cs = _reload(monkeypatch)
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))
    monkeypatch.setattr(cs, "now_et",
                         lambda: datetime(2026, 4, 22, 10, 0, 0, tzinfo=et))
    cs._last_runs["monitor_1"] = time.time() - 200
    cs._staleness_last_alert.clear()
    cs._staleness_overdue_count.clear()
    cs._staleness_overdue_count["monitor_1"] = 1  # first strike already
    fired = []
    monkeypatch.setattr(cs, "notify_user",
                         lambda u, m, t: fired.append((u["id"], m)))

    cs._check_task_staleness([{"id": 1, "username": "test"}])
    assert len(fired) == 1
    assert "monitor" in fired[0][1]
    assert "180s" in fired[0][1]  # threshold reported


def test_transient_spike_clears_debounce_on_recovery(monkeypatch):
    """If the task catches up between observations, the debounce counter
    must reset so a later real stall still needs 2 observations."""
    cs = _reload(monkeypatch)
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))
    monkeypatch.setattr(cs, "now_et",
                         lambda: datetime(2026, 4, 22, 10, 0, 0, tzinfo=et))
    cs._last_runs["monitor_1"] = time.time() - 200  # overdue
    cs._staleness_last_alert.clear()
    cs._staleness_overdue_count.clear()
    monkeypatch.setattr(cs, "notify_user", lambda *a, **k: None)

    cs._check_task_staleness([{"id": 1, "username": "test"}])
    assert cs._staleness_overdue_count.get("monitor_1") == 1

    # Task recovers — fresh _last_runs
    cs._last_runs["monitor_1"] = time.time() - 10
    cs._check_task_staleness([{"id": 1, "username": "test"}])
    assert cs._staleness_overdue_count.get("monitor_1") is None, (
        "debounce counter didn't reset after recovery")


def test_128s_lag_no_longer_fires_alert(monkeypatch):
    """Pin the EXACT user-reported scenario: 128s behind a 60s-cadence
    monitor task. With round-48 this paged the user immediately.
    With round-49's 3x threshold (180s), 128s is no longer overdue at
    all — no alert, no debounce bump, just a quiet recovery."""
    cs = _reload(monkeypatch)
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))
    # After the suppression window but still during active trading
    monkeypatch.setattr(cs, "now_et",
                         lambda: datetime(2026, 4, 22, 10, 30, 0, tzinfo=et))
    cs._last_runs["monitor_1"] = time.time() - 128  # user's exact case
    cs._staleness_last_alert.clear()
    cs._staleness_overdue_count.clear()
    fired = []
    monkeypatch.setattr(cs, "notify_user",
                         lambda u, m, t: fired.append(m))

    cs._check_task_staleness([{"id": 1, "username": "test"}])
    assert fired == [], (
        "The 128s-lag case still fires an alert — round-49 threshold "
        "tuning regressed")
    # Counter not bumped because 128 < 180 threshold
    assert cs._staleness_overdue_count.get("monitor_1") is None
