"""Round-61 pt.91 — daily_close catchup so missed days self-heal.

User-reported via STALE_SCORECARD audit at Sat 10:36 PM: the
scorecard hadn't updated in 78h with 2 missed daily_close runs.
Root cause: `should_run_daily_at` only fires within
`max_late_seconds` of the target time. For daily_close that's a
4-hour window (16:05–20:05 ET). If a Railway redeploy or
scheduler hiccup pushes the next tick past 20:05 ET, the task
silently skips for that day. The next day's tick checks `last_date
!= today_str` → True, but we're now BEFORE 16:05 → no fire. The
task stays missed forever until the user notices.

Pt.91 adds an `allow_catchup=True` flag. When set, a missed prior
day fires the task immediately (any time after the target on the
NEW calendar day), stamping today's date. The scorecard self-heals
on the next scheduler tick — no human intervention needed.

Catchup is OPT-IN per task. Only daily_close uses it. Trading
actions (auto_deployer, wheel_deploy) keep the strict window
because firing them hours late means trading on stale screener
data.
"""
from __future__ import annotations

from datetime import datetime


def test_catchup_fires_when_prior_day_missed(monkeypatch):
    """Last run was YESTERDAY, today is past target, max_late_seconds
    has long since closed → with catchup, fire."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {"daily_close_x": "2026-04-24"})
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 25, 22, 0))
    fire = cs.should_run_daily_at(
        "daily_close_x", 16, 5,
        max_late_seconds=4 * 3600,
        allow_catchup=True)
    assert fire is True


def test_catchup_disabled_does_not_fire(monkeypatch):
    """Same scenario without catchup → no fire (original behaviour)."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {"daily_close_x": "2026-04-24"})
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 25, 22, 0))
    fire = cs.should_run_daily_at(
        "daily_close_x", 16, 5,
        max_late_seconds=4 * 3600)
    assert fire is False


def test_catchup_no_fire_before_target(monkeypatch):
    """Catchup only fires AFTER today's target time."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {"daily_close_x": "2026-04-24"})
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 25, 10, 0))
    fire = cs.should_run_daily_at(
        "daily_close_x", 16, 5,
        allow_catchup=True)
    assert fire is False


def test_catchup_no_fire_when_first_run_ever(monkeypatch):
    """Empty last_date (never run) → skip catchup; let the normal
    window govern. Prevents firing for tasks that have never been
    set up."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {})
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 25, 22, 0))
    fire = cs.should_run_daily_at(
        "daily_close_x", 16, 5,
        max_late_seconds=4 * 3600,
        allow_catchup=True)
    assert fire is False


def test_catchup_does_not_re_fire_same_day(monkeypatch):
    """Once today's stamp lands, catchup must not re-fire on later
    ticks within the same day."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {"daily_close_x": "2026-04-25"})
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 25, 22, 0))
    fire = cs.should_run_daily_at(
        "daily_close_x", 16, 5,
        max_late_seconds=4 * 3600,
        allow_catchup=True)
    assert fire is False


def test_within_window_still_fires(monkeypatch):
    """Pt.91 must not regress the normal in-window fire path."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {"daily_close_x": "2026-04-24"})
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 25, 17, 0))
    fire = cs.should_run_daily_at(
        "daily_close_x", 16, 5,
        max_late_seconds=4 * 3600,
        allow_catchup=True)
    assert fire is True


def test_catchup_fires_after_multi_day_gap(monkeypatch):
    """User's exact scenario: last run was Wednesday, now is
    Saturday (3 days later). Catchup should still fire."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {"daily_close_x": "2026-04-22"})
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 25, 22, 36))
    fire = cs.should_run_daily_at(
        "daily_close_x", 16, 5,
        max_late_seconds=4 * 3600,
        allow_catchup=True)
    assert fire is True


def test_daily_close_call_site_uses_catchup():
    """Pt.91 source-pin: the scheduler tick that invokes
    daily_close must opt into catchup."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    idx = src.find('should_run_daily_at(f"daily_close_{uid}"')
    assert idx > 0
    block = src[idx:idx + 600]
    assert "allow_catchup=True" in block


def test_should_run_daily_at_signature_has_catchup_kwarg():
    import cloud_scheduler as cs
    import inspect
    sig = inspect.signature(cs.should_run_daily_at)
    assert "allow_catchup" in sig.parameters
    assert sig.parameters["allow_catchup"].default is False


def test_pt91_documented_in_docstring():
    import cloud_scheduler as cs
    doc = cs.should_run_daily_at.__doc__ or ""
    assert "pt.91" in doc.lower() or "catchup" in doc.lower()
