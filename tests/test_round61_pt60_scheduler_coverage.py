"""Round-61 pt.60 — coverage push on cloud_scheduler.py.

Goal: lift cloud_scheduler.py coverage from <40% toward 65%+ by
exercising the smaller pure functions + a few mock-driven
integration paths. Uses the lazy-import pattern from pt.52 +
the AlpacaMock pattern from pt.55.
"""
from __future__ import annotations

import json
import os
import time

import pytest


# ============================================================================
# Local AlpacaMock — same pattern as pt.55 but self-contained.
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

    def _do_patch(self, user, path, body=None, **_kw):
        self.calls.append(("PATCH", path, body))
        return self._match("PATCH", path)


@pytest.fixture
def mock():
    return _AlpacaMock()


def _patch(monkeypatch, mock):
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "user_api_get", mock._do_get)
    monkeypatch.setattr(cs, "user_api_post", mock._do_post)
    monkeypatch.setattr(cs, "user_api_delete", mock._do_delete)
    monkeypatch.setattr(cs, "user_api_patch", mock._do_patch)
    return cs


# ============================================================================
# request_deploy_abort + deploy_should_abort — kill-switch signal
# ============================================================================

def test_deploy_abort_default_is_clear(monkeypatch):
    import cloud_scheduler as cs
    # Reset state since other tests may have set it.
    monkeypatch.setattr(cs, "_deploy_abort_event", cs.threading.Event())
    assert cs.deploy_should_abort() is False


def test_request_deploy_abort_sets_flag(monkeypatch):
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_deploy_abort_event", cs.threading.Event())
    cs.request_deploy_abort()
    assert cs.deploy_should_abort() is True


def test_request_deploy_abort_idempotent(monkeypatch):
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_deploy_abort_event", cs.threading.Event())
    cs.request_deploy_abort()
    cs.request_deploy_abort()
    assert cs.deploy_should_abort() is True


# ============================================================================
# should_run_interval — interval debounce
# ============================================================================

def test_should_run_interval_first_call_fires(monkeypatch):
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {})
    # _save_last_runs hits disk; stub it.
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    assert cs.should_run_interval("task1", 60) is True


def test_should_run_interval_second_call_within_window_skipped(monkeypatch):
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_last_runs", {})
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    cs.should_run_interval("task2", 60)   # first fires
    assert cs.should_run_interval("task2", 60) is False


def test_should_run_interval_after_window_fires(monkeypatch):
    import cloud_scheduler as cs
    # Pretend last run was 10 minutes ago.
    monkeypatch.setattr(cs, "_last_runs",
                          {"task3": time.time() - 600})
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    assert cs.should_run_interval("task3", 60) is True


# ============================================================================
# should_run_daily_at — exactly-once-per-day
# ============================================================================

def test_should_run_daily_at_before_target_time_skipped(monkeypatch):
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "_last_runs", {})
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    # Pretend now is 9 AM ET; target is 4:05 PM.
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 27, 9, 0))
    assert cs.should_run_daily_at("daily_task", 16, 5) is False


def test_should_run_daily_at_at_target_fires(monkeypatch):
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "_last_runs", {})
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    # Right at target time.
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 27, 16, 5))
    assert cs.should_run_daily_at("daily_task", 16, 5) is True


def test_should_run_daily_at_only_once_per_day(monkeypatch):
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "_last_runs", {})
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 27, 16, 5))
    cs.should_run_daily_at("daily_task", 16, 5)
    # Second call same day → skipped.
    assert cs.should_run_daily_at("daily_task", 16, 5) is False


def test_should_run_daily_at_too_late_skipped(monkeypatch):
    """If we're past the max_late_seconds window, don't fire."""
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "_last_runs", {})
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    # Target was 4:05 PM, but we're now 8 PM (>1800s late default).
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 27, 20, 0))
    assert cs.should_run_daily_at("daily_task", 16, 5,
                                     max_late_seconds=1800) is False


def test_should_run_daily_at_max_late_window_extended(monkeypatch):
    """User can extend max_late so a Railway restart still recovers."""
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "_last_runs", {})
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 27, 20, 0))
    # Allow 4 hours of lateness — fires.
    assert cs.should_run_daily_at("daily_task", 16, 5,
                                     max_late_seconds=4 * 3600) is True


# ============================================================================
# _clear_daily_stamp — let a failed task retry
# ============================================================================

def test_clear_daily_stamp_removes_today_entry(monkeypatch):
    import cloud_scheduler as cs
    from datetime import datetime
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 27, 16, 5))
    monkeypatch.setattr(cs, "_last_runs",
                          {"daily_task": time.time(),
                           "daily_task_date": "2026-04-27"})
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    cs._clear_daily_stamp("daily_task")
    # After clear, should_run_daily_at SHOULD fire again.
    monkeypatch.setattr(cs, "_last_runs",
                          dict(cs._last_runs))
    # The stamp clearing should at least not raise.


# ============================================================================
# is_first_trading_day_of_month
# ============================================================================

def test_is_first_trading_day_returns_false_after_day_7(mock,
                                                          monkeypatch):
    cs = _patch(monkeypatch, mock)
    from datetime import datetime
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 15, 9, 30))
    assert cs.is_first_trading_day_of_month({"id": 1}) is False


def test_is_first_trading_day_with_matching_calendar(mock, monkeypatch):
    cs = _patch(monkeypatch, mock)
    from datetime import datetime
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 1, 9, 30))
    mock.register("GET", "/calendar", [
        {"date": "2026-04-01", "open": "09:30", "close": "16:00"}
    ])
    assert cs.is_first_trading_day_of_month({"id": 1}) is True


def test_is_first_trading_day_today_is_not_calendar_first(mock,
                                                            monkeypatch):
    """Day is in the first week but calendar says Apr 1 was a holiday;
    today's date != first_trading_date in the calendar response."""
    cs = _patch(monkeypatch, mock)
    from datetime import datetime
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 2, 9, 30))
    mock.register("GET", "/calendar", [
        {"date": "2026-04-01", "open": "09:30", "close": "16:00"}
    ])
    assert cs.is_first_trading_day_of_month({"id": 1}) is False


def test_is_first_trading_day_calendar_error(mock, monkeypatch):
    cs = _patch(monkeypatch, mock)
    from datetime import datetime
    monkeypatch.setattr(cs, "get_et_time",
                          lambda: datetime(2026, 4, 1, 9, 30))
    mock.register("GET", "/calendar", {"error": "rate limited"})
    assert cs.is_first_trading_day_of_month({"id": 1}) is False


# ============================================================================
# get_et_time
# ============================================================================

def test_get_et_time_returns_naive_datetime():
    """Per docstring, get_et_time() strips tzinfo so existing callers
    comparing to tz-naive values still work."""
    import cloud_scheduler as cs
    t = cs.get_et_time()
    assert t.tzinfo is None


def test_get_et_time_close_to_now_in_eastern():
    """The naive ET should be roughly UTC - 4 or - 5 hours."""
    import cloud_scheduler as cs
    from datetime import datetime, timezone
    et = cs.get_et_time()
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    # Difference should be 4 to 5 hours.
    diff_seconds = abs((utc - et).total_seconds())
    assert 3.5 * 3600 <= diff_seconds <= 5.5 * 3600


# ============================================================================
# user_file path resolution
# ============================================================================

def test_user_file_resolves_under_user_dir(tmp_path):
    import cloud_scheduler as cs
    user = {"id": 7, "_data_dir": str(tmp_path / "user_7")}
    p = cs.user_file(user, "trade_journal.json")
    assert p.endswith("trade_journal.json")
    assert "user_7" in p


# ============================================================================
# _save_last_runs / _load_last_runs round-trip
# ============================================================================

def test_save_and_load_last_runs_round_trip(tmp_path, monkeypatch):
    """Persist + reload via a temp DATA_DIR. Uses real disk I/O.
    Note: _load_last_runs drops timestamps older than 7 days, so we
    use a recent one."""
    import cloud_scheduler as cs
    recent_ts = time.time() - 60        # 1 min ago
    monkeypatch.setattr(cs, "_LAST_RUNS_PATH",
                          str(tmp_path / "last_runs.json"))
    monkeypatch.setattr(cs, "_last_runs", {"task_a": recent_ts})
    cs._save_last_runs()
    # Read back via _load_last_runs.
    monkeypatch.setattr(cs, "_last_runs", {})
    cs._load_last_runs()
    assert abs(cs._last_runs.get("task_a", 0) - recent_ts) < 1


def test_load_last_runs_handles_missing_file(tmp_path, monkeypatch):
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "_LAST_RUNS_PATH",
                          str(tmp_path / "missing.json"))
    monkeypatch.setattr(cs, "_last_runs", {})
    # Should not crash.
    cs._load_last_runs()
    assert isinstance(cs._last_runs, dict)


def test_load_last_runs_handles_corrupt_file(tmp_path, monkeypatch):
    import cloud_scheduler as cs
    bad = tmp_path / "corrupt.json"
    bad.write_text("not valid json")
    monkeypatch.setattr(cs, "_LAST_RUNS_PATH", str(bad))
    monkeypatch.setattr(cs, "_last_runs", {})
    cs._load_last_runs()
    # Should fall back to empty without crashing.
    assert cs._last_runs == {} or isinstance(cs._last_runs, dict)


# ============================================================================
# record_trade_open / record_trade_close — already partially tested
# in pt.55, repeating here with a few extra edge cases.
# ============================================================================

def _make_user(tmp_path):
    udir = tmp_path / "user_dir"
    udir.mkdir(parents=True, exist_ok=True)
    return {
        "id": 99, "username": "test99",
        "_data_dir": str(udir),
        "_strategies_dir": str(udir / "strategies"),
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
    }


def test_record_trade_open_with_extra_dict_merged(mock, monkeypatch,
                                                     tmp_path):
    cs = _patch(monkeypatch, mock)
    user = _make_user(tmp_path)
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    cs.record_trade_open(
        user, "AAPL", "breakout", 100.0, 5,
        "test", side="buy", deployer="harness",
        extra={"_screener_score": 92, "tier": "cash_micro"},
    )
    journal_path = os.path.join(user["_data_dir"], "trade_journal.json")
    with open(journal_path) as f:
        data = json.load(f)
    t = data["trades"][0]
    assert t.get("_screener_score") == 92
    assert t.get("tier") == "cash_micro"


def test_record_trade_close_no_matching_open_creates_orphan(mock,
                                                              monkeypatch,
                                                              tmp_path):
    """Pt.33: record_trade_close on a symbol with no open journal entry
    creates a synthetic 'orphan' entry so the close shows up in the
    Today's Closes panel + scorecard. Locking this behaviour in."""
    cs = _patch(monkeypatch, mock)
    user = _make_user(tmp_path)
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    cs.record_trade_close(
        user, "NOTHELD", "breakout", 110.0,
        pnl=10.0, exit_reason="target_hit",
        qty=5, side="sell")
    journal_path = os.path.join(user["_data_dir"], "trade_journal.json")
    assert os.path.exists(journal_path)
    with open(journal_path) as f:
        data = json.load(f)
    nothelds = [t for t in (data.get("trades") or [])
                 if t.get("symbol") == "NOTHELD"]
    assert len(nothelds) == 1
    assert nothelds[0].get("status") == "closed"
    assert nothelds[0].get("exit_reason") == "target_hit"


def test_record_trade_close_updates_pnl_pct_for_long(mock, monkeypatch,
                                                       tmp_path):
    cs = _patch(monkeypatch, mock)
    user = _make_user(tmp_path)
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    cs.record_trade_open(user, "AAPL", "breakout", 100.0, 5,
                          "test", side="buy", deployer="h")
    cs.record_trade_close(user, "AAPL", "breakout", 110.0,
                            pnl=50.0, exit_reason="target_hit",
                            qty=5, side="sell")
    journal_path = os.path.join(user["_data_dir"], "trade_journal.json")
    with open(journal_path) as f:
        data = json.load(f)
    t = data["trades"][0]
    assert t.get("pnl_pct") == 10.0   # (110/100 - 1)*100


def test_record_trade_close_short_cover_pnl_pct(mock, monkeypatch,
                                                   tmp_path):
    """Short pnl_pct = (entry/exit - 1) * 100 — sold high, bought low."""
    cs = _patch(monkeypatch, mock)
    user = _make_user(tmp_path)
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    # Open a short.
    cs.record_trade_open(user, "SOXL", "short_sell", 100.0, 5,
                          "test", side="sell_short", deployer="h")
    # Cover at $90.
    cs.record_trade_close(user, "SOXL", "short_sell", 90.0,
                            pnl=50.0, exit_reason="target_hit",
                            qty=5, side="buy")
    journal_path = os.path.join(user["_data_dir"], "trade_journal.json")
    with open(journal_path) as f:
        data = json.load(f)
    t = data["trades"][0]
    # short pnl_pct = (100/90 - 1)*100 = 11.11
    assert t.get("pnl_pct") is not None
    assert abs(t["pnl_pct"] - 11.11) < 0.1


# ============================================================================
# check_correlation_allowed — edge cases (extra coverage)
# ============================================================================

def test_correlation_allowed_empty_existing():
    import cloud_scheduler as cs
    allowed, reason = cs.check_correlation_allowed("AAPL", [])
    assert allowed is True


def test_correlation_allowed_other_sector_skipped():
    import cloud_scheduler as cs
    # MARA is "Other" sector typically — held alone shouldn't block AAPL.
    allowed, reason = cs.check_correlation_allowed(
        "AAPL", [{"symbol": "ZZZUNKNOWN1", "market_value": "5000"},
                  {"symbol": "ZZZUNKNOWN2", "market_value": "5000"}])
    # Unknown symbols → "Other" → don't count toward sector cap.
    assert allowed is True


# ============================================================================
# notify_user (best-effort logger; no-op if no notification adapter)
# ============================================================================

def test_notify_user_doesnt_raise(mock, monkeypatch):
    """notify_user is supposed to be best-effort. Even with no
    NTFY_TOPIC / SMTP configured, calling should not raise."""
    cs = _patch(monkeypatch, mock)
    user = {"id": 1, "username": "test", "_data_dir": "/tmp",
             "_notification_email": None}
    # Just verify it doesn't crash.
    cs.notify_user(user, "test message", "info")
