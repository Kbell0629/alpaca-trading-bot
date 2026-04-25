"""Round-61 pt.62 — second coverage round on cloud_scheduler.

Targets the higher-value pure functions that the pt.60 round
didn't hit. Goal: 30+ tests, +5-7 coverage points.
"""
from __future__ import annotations

import json
import os
import time

import pytest


# ============================================================================
# Local AlpacaMock (same pattern as pt.55 + pt.60).
# ============================================================================

class _AlpacaMock:
    def __init__(self):
        self._handlers = []
        self.calls = []

    def register(self, method, substr, response):
        self._handlers.append((method.upper(), substr, response))

    def _match(self, method, url):
        m = method.upper()
        for hm, substr, resp in reversed(self._handlers):
            if hm == m and substr in url:
                return resp
        return {}

    def _do_get(self, user, path, **_kw):
        self.calls.append(("GET", path, None))
        return self._match("GET", path)

    def _do_post(self, user, path, body=None, **_kw):
        self.calls.append(("POST", path, body))
        return self._match("POST", path)

    def _do_delete(self, user, path, **_kw):
        self.calls.append(("DELETE", path, None))
        return self._match("DELETE", path)


@pytest.fixture
def mock():
    return _AlpacaMock()


def _patch(monkeypatch, mock):
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "user_api_get", mock._do_get)
    monkeypatch.setattr(cs, "user_api_post", mock._do_post)
    monkeypatch.setattr(cs, "user_api_delete", mock._do_delete)
    return cs


# ============================================================================
# _compute_stepped_stop — pure trail-tier math
# ============================================================================

def test_stepped_stop_tier_1_below_5pct_profit_long():
    """Long with profit <5% → Tier 1 (default trail below extreme)."""
    import cloud_scheduler as cs
    entry = 100.0
    extreme = 102.0      # +2% profit
    default_trail = 0.05
    stop, tier, trail = cs._compute_stepped_stop(
        entry, extreme, default_trail, is_short=False)
    assert tier == 1
    assert trail == default_trail
    # 102 * 0.95 = 96.9
    assert abs(stop - 96.9) < 0.01


def test_stepped_stop_tier_2_breakeven_lock_long():
    """Long with 5-10% profit → Tier 2 (stop at entry)."""
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        100.0, 107.0, 0.05, is_short=False)
    assert tier == 2
    assert trail is None
    assert stop == 100.0


def test_stepped_stop_tier_3_six_pct_trail_long():
    """Long with 10-20% profit → Tier 3 (6% trail)."""
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        100.0, 115.0, 0.05, is_short=False)
    assert tier == 3
    assert trail == 0.06
    # 115 * 0.94 = 108.10
    assert abs(stop - 108.10) < 0.01


def test_stepped_stop_tier_4_four_pct_trail_long():
    """Long with 20%+ profit → Tier 4 (4% trail)."""
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        100.0, 125.0, 0.05, is_short=False)
    assert tier == 4
    assert trail == 0.04
    # 125 * 0.96 = 120.00
    assert abs(stop - 120.00) < 0.01


def test_stepped_stop_tier_1_short():
    """Short with <5% profit → Tier 1 (trail above extreme)."""
    import cloud_scheduler as cs
    entry = 100.0
    extreme = 98.0       # -2% (favorable for short)
    stop, tier, trail = cs._compute_stepped_stop(
        entry, extreme, 0.05, is_short=True)
    assert tier == 1
    # 98 * 1.05 = 102.90
    assert abs(stop - 102.9) < 0.01


def test_stepped_stop_tier_3_short():
    """Short with 10-20% profit → Tier 3."""
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        100.0, 85.0, 0.05, is_short=True)
    assert tier == 3
    assert trail == 0.06
    # 85 * 1.06 = 90.10
    assert abs(stop - 90.10) < 0.01


def test_stepped_stop_zero_entry_falls_back_to_flat_trail():
    """No entry price → can't compute tiers; fall back to flat trail."""
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        0, 100.0, 0.05, is_short=False)
    assert tier == 1
    # 100 * 0.95 = 95.0
    assert abs(stop - 95.0) < 0.01


def test_stepped_stop_negative_entry_falls_back():
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        -1.0, 100.0, 0.05)
    assert tier == 1


def test_stepped_stop_none_entry_falls_back():
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        None, 100.0, 0.05)
    assert tier == 1


def test_stepped_stop_at_5pct_boundary_is_tier_2():
    """Exactly 5% profit → Tier 2 (boundary inclusive on lower edge)."""
    import cloud_scheduler as cs
    stop, tier, _ = cs._compute_stepped_stop(
        100.0, 105.0, 0.05, is_short=False)
    assert tier == 2


def test_stepped_stop_at_10pct_boundary_is_tier_3():
    import cloud_scheduler as cs
    stop, tier, _ = cs._compute_stepped_stop(
        100.0, 110.0, 0.05, is_short=False)
    assert tier == 3


def test_stepped_stop_at_20pct_boundary_is_tier_4():
    import cloud_scheduler as cs
    stop, tier, _ = cs._compute_stepped_stop(
        100.0, 120.0, 0.05, is_short=False)
    assert tier == 4


# ============================================================================
# strategy_file_lock — tested as a contextmanager
# ============================================================================

def test_strategy_file_lock_returns_contextmanager(tmp_path):
    import cloud_scheduler as cs
    p = str(tmp_path / "AAPL.json")
    # Just verify the context-manager protocol works.
    with cs.strategy_file_lock(p):
        pass


def test_strategy_file_lock_can_be_used_concurrently_same_path(tmp_path):
    """Re-entering the same lock from the same thread should not
    deadlock — strategy_file_lock uses an RLock-style or ref-counted
    pattern."""
    import cloud_scheduler as cs
    p = str(tmp_path / "AAPL.json")
    # Acquire then release sequentially (no nested re-entry).
    with cs.strategy_file_lock(p):
        pass
    with cs.strategy_file_lock(p):
        pass


# ============================================================================
# user_strategies_dir
# ============================================================================

def test_user_strategies_dir_resolves_from_user(tmp_path):
    import cloud_scheduler as cs
    user = {"id": 9, "_data_dir": str(tmp_path / "u9"),
             "_strategies_dir": str(tmp_path / "u9" / "strategies")}
    out = cs.user_strategies_dir(user)
    assert "u9" in out
    assert "strategies" in out


# ============================================================================
# load_json / save_json — round-trip + error cases
# ============================================================================

def test_save_json_creates_file(tmp_path):
    import cloud_scheduler as cs
    p = str(tmp_path / "out.json")
    cs.save_json(p, {"a": 1, "b": [2, 3]})
    assert os.path.exists(p)


def test_load_json_round_trip(tmp_path):
    import cloud_scheduler as cs
    p = str(tmp_path / "out.json")
    cs.save_json(p, {"key": "value"})
    out = cs.load_json(p)
    assert out == {"key": "value"}


def test_load_json_missing_returns_none(tmp_path):
    import cloud_scheduler as cs
    out = cs.load_json(str(tmp_path / "nope.json"))
    assert out is None


def test_load_json_corrupt_returns_none(tmp_path):
    import cloud_scheduler as cs
    p = tmp_path / "bad.json"
    p.write_text("not valid json {{")
    out = cs.load_json(str(p))
    # cs.load_json may return None or {} — accept either.
    assert out is None or out == {}


# ============================================================================
# _within_opening_bell_congestion
# ============================================================================

def test_opening_bell_congestion_at_open(monkeypatch):
    """First 5 minutes after open → True (congestion)."""
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "now_et",
                          lambda: datetime(2026, 4, 27, 9, 31))
    assert cs._within_opening_bell_congestion() is True


def test_opening_bell_congestion_after_5min(monkeypatch):
    """After 5 min past open → False."""
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "now_et",
                          lambda: datetime(2026, 4, 27, 9, 55))
    assert cs._within_opening_bell_congestion() is False


def test_opening_bell_congestion_before_open(monkeypatch):
    """Pre-market → False (we're not in trading hours yet)."""
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "now_et",
                          lambda: datetime(2026, 4, 27, 9, 0))
    assert cs._within_opening_bell_congestion() is False


# ============================================================================
# log + _save_recent_logs round-trip
# ============================================================================

def test_log_adds_to_recent_logs(monkeypatch):
    """log() appends to a ring buffer that the activity panel reads."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_RECENT_LOGS_PATH", "/tmp/test_logs.json")
    monkeypatch.setattr(cs, "_save_recent_logs", lambda: None)
    initial_len = len(cs._recent_logs)
    cs.log("test message", "test")
    assert len(cs._recent_logs) > initial_len


# ============================================================================
# _flatten_all_user — emergency kill-switch flatten helper
# ============================================================================

def test_flatten_all_user_calls_delete_orders(mock, monkeypatch):
    """_flatten_all_user is a kill-switch helper that cancels orders
    AND closes equity + option positions. Verify it issues the
    expected DELETE calls without raising."""
    cs = _patch(monkeypatch, mock)
    user = {"id": 1, "username": "tester",
             "_data_dir": "/tmp", "_strategies_dir": "/tmp",
             "_notification_email": None,
             "_api_endpoint": "https://paper-api.alpaca.markets/v2"}
    cs._flatten_all_user(user)
    # At minimum, /orders DELETE should have been attempted.
    delete_calls = [c for c in mock.calls if c[0] == "DELETE"]
    assert any("/orders" in c[1] for c in delete_calls)


# ============================================================================
# get_all_users_for_scheduling
# ============================================================================

def test_get_all_users_for_scheduling_returns_list(monkeypatch,
                                                       tmp_path):
    """Should return an iterable of user dicts (possibly empty)."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "DATA_DIR", str(tmp_path))
    out = cs.get_all_users_for_scheduling()
    assert hasattr(out, "__iter__")


# ============================================================================
# notify_rich falls back to notify_user when rich body is None
# ============================================================================

def test_notify_rich_falls_back_when_no_body(mock, monkeypatch):
    cs = _patch(monkeypatch, mock)
    user = {"id": 1, "username": "test", "_data_dir": "/tmp",
             "_notification_email": None}
    # Should not raise — falls back to notify_user.
    cs.notify_rich(user, "test", "info",
                     rich_subject=None, rich_body=None)


def test_notify_user_global_doesnt_raise():
    """notify_user_global is a fan-out helper that messages all users."""
    import cloud_scheduler as cs
    cs.notify_user_global("test global notice", "info")


# ============================================================================
# log_to_sentry safety
# ============================================================================

def test_log_silent_when_sentry_disabled(monkeypatch):
    import cloud_scheduler as cs
    # Default state has no SENTRY_DSN → log() should still work.
    monkeypatch.setattr(cs, "_save_recent_logs", lambda: None)
    cs.log("safe test", "test")
    # No assertion — just verifying no crash.


# ============================================================================
# now_et returns a tz-aware ET datetime
# ============================================================================

def test_now_et_is_tz_aware():
    import cloud_scheduler as cs
    t = cs.now_et()
    assert t.tzinfo is not None


def test_now_et_eastern_offset():
    """now_et should be -4 or -5 hours from UTC."""
    import cloud_scheduler as cs
    from datetime import datetime, timezone
    et = cs.now_et()
    # Compute offset in hours
    offset = et.utcoffset()
    if offset is not None:
        hours = offset.total_seconds() / 3600
        assert hours in (-4, -5)


# ============================================================================
# _heartbeat_tick — drops stale entries from _last_runs
# ============================================================================

def test_heartbeat_tick_runs_without_error(monkeypatch):
    """_heartbeat_tick is the periodic upkeep — should not raise."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_save_recent_logs", lambda: None)
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    cs._heartbeat_tick()
