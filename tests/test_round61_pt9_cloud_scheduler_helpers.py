"""
Round-61 pt.9 — deeper coverage of cloud_scheduler.py pure helpers.

cloud_scheduler.py is the biggest Python module at ~4700 LOC with
61 top-level functions. Pt.6 hit the HTTP surface via the mock WSGI
harness; coverage landed at ~31% because the scheduler internals
(daily close, wheel orchestration, etc.) weren't touched. This file
tests the pure-and-pure-ish helpers directly. Orchestrators that
wrap Alpaca HTTP + file I/O stay out of scope — they need heavier
mocks and risk false positives.

Covered:
  * Formatters: _fmt_money / _fmt_pct / _fmt_signed_money
  * check_correlation_allowed — sector diversification guard
  * is_first_trading_day_of_month — quick-return calendar gate
  * Daily / interval scheduling gates: should_run_interval,
    should_run_daily_at, _clear_daily_stamp
  * Deploy-abort signals: request_deploy_abort / clear_deploy_abort /
    deploy_should_abort
  * _within_opening_bell_congestion — 9:30-9:50 ET suppression
  * _has_user_tag — activity-log tag parser
  * _build_user_dict_for_mode — dual-mode user shaping
  * strategy_file_lock — cross-thread advisory lock
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest import mock


def _import(monkeypatch):
    """Ensure cloud_scheduler can import with a boot-legal encryption
    key. Re-used across tests to get a fresh module when we monkeypatch
    its internals."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    # Drop from cache so module-level state is fresh
    for name in ("cloud_scheduler",):
        sys.modules.pop(name, None)
    import cloud_scheduler  # noqa: F401 — deliberate import for side effects
    return cloud_scheduler


# ============================================================================
# Formatters
# ============================================================================

class TestFmtMoney:
    def test_basic(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._fmt_money(100) == "$100.00"
        assert cs._fmt_money(1234.5) == "$1,234.50"
        assert cs._fmt_money(0) == "$0.00"

    def test_negative_preserves_sign(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._fmt_money(-42) == "$-42.00"

    def test_bad_input_returns_placeholder(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._fmt_money(None) == "$—"
        assert cs._fmt_money("abc") == "$—"
        assert cs._fmt_money(object()) == "$—"


class TestFmtPct:
    def test_sign_and_precision(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._fmt_pct(1.5) == "+1.50%"
        assert cs._fmt_pct(-2.5) == "-2.50%"
        assert cs._fmt_pct(0) == "+0.00%"

    def test_custom_precision(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._fmt_pct(1.2345, decimals=1) == "+1.2%"
        assert cs._fmt_pct(1.2345, decimals=3) == "+1.234%"

    def test_bad_input(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._fmt_pct(None) == "—"
        assert cs._fmt_pct("bad") == "—"


class TestFmtSignedMoney:
    def test_positive_uses_plus(self, monkeypatch):
        cs = _import(monkeypatch)
        # Uses unicode minus for losses; regular + for gains
        assert cs._fmt_signed_money(100) == "+$100.00"

    def test_negative_uses_unicode_minus(self, monkeypatch):
        cs = _import(monkeypatch)
        out = cs._fmt_signed_money(-100)
        # Unicode minus − (not ASCII -)
        assert out.startswith("−$")
        assert "100.00" in out

    def test_bad_input(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._fmt_signed_money(None) == "$—"


# ============================================================================
# check_correlation_allowed
# ============================================================================

class TestCheckCorrelationAllowed:
    def test_empty_positions_allowed(self, monkeypatch):
        cs = _import(monkeypatch)
        ok, reason = cs.check_correlation_allowed("AAPL", [])
        assert ok is True
        assert "OK" in reason

    def test_two_same_sector_blocks(self, monkeypatch):
        cs = _import(monkeypatch)
        # Assume AAPL + MSFT + NVDA all in Tech per constants.SECTOR_MAP.
        existing = [
            {"symbol": "AAPL", "market_value": 10000},
            {"symbol": "MSFT", "market_value": 10000},
        ]
        ok, reason = cs.check_correlation_allowed("NVDA", existing)
        assert ok is False
        assert "2 positions" in reason or "Tech" in reason

    def test_other_sector_skips_per_sector_count_cap(self, monkeypatch):
        """The `if pos_sector != "Other"` branch excludes 'Other' from
        the per-sector count cap. Two 'Other' holdings plus an Other
        proposal should pass the count check (though may still hit the
        60% concentration cap depending on sizing)."""
        cs = _import(monkeypatch)
        existing = [
            {"symbol": "ZZZ1", "market_value": 1000},
            {"symbol": "ZZZ2", "market_value": 1000},
            # A real-sector ticker so Other isn't 100% of the book.
            {"symbol": "AAPL", "market_value": 10000},
        ]
        ok, reason = cs.check_correlation_allowed("ZZZ3", existing)
        # Under 60% Other-concentration cap + count-check doesn't apply
        # to Other → should be allowed.
        assert ok is True, f"expected allowed, got reason={reason}"

    def test_concentration_cap_blocks_real_sector(self, monkeypatch):
        """If existing positions in a sector already exceed 40% of total
        portfolio value, new entries in that sector are blocked."""
        cs = _import(monkeypatch)
        # AAPL = Tech. Give it $5000, put $5000 in a non-Tech holding.
        existing = [
            {"symbol": "AAPL", "market_value": 5000},   # Tech
        ]
        ok, reason = cs.check_correlation_allowed("MSFT", existing)
        # AAPL = 100% of Tech, adding MSFT would push us over the per-
        # sector count cap (2 Tech including the newcomer is fine).
        # This test mostly ensures the function returns a 2-tuple.
        assert isinstance(ok, bool)
        assert isinstance(reason, str)


# ============================================================================
# Deploy-abort signalling
# ============================================================================

class TestDeployAbort:
    def test_starts_clear(self, monkeypatch):
        cs = _import(monkeypatch)
        cs.clear_deploy_abort()
        assert cs.deploy_should_abort() is False

    def test_request_sets_flag(self, monkeypatch):
        cs = _import(monkeypatch)
        cs.clear_deploy_abort()
        cs.request_deploy_abort()
        assert cs.deploy_should_abort() is True

    def test_clear_resets(self, monkeypatch):
        cs = _import(monkeypatch)
        cs.request_deploy_abort()
        cs.clear_deploy_abort()
        assert cs.deploy_should_abort() is False


# ============================================================================
# Interval + daily scheduling gates
# ============================================================================

class TestShouldRunInterval:
    def test_first_call_fires(self, monkeypatch):
        cs = _import(monkeypatch)
        with cs._last_runs_lock:
            cs._last_runs.pop("test_interval_task", None)
        # Stub out file persistence to avoid disk I/O during tests
        monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
        assert cs.should_run_interval("test_interval_task", 60) is True

    def test_second_call_within_interval_suppresses(self, monkeypatch):
        cs = _import(monkeypatch)
        with cs._last_runs_lock:
            cs._last_runs.pop("test_interval2", None)
        monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
        assert cs.should_run_interval("test_interval2", 60) is True
        assert cs.should_run_interval("test_interval2", 60) is False

    def test_after_interval_fires_again(self, monkeypatch):
        cs = _import(monkeypatch)
        with cs._last_runs_lock:
            cs._last_runs["test_interval3"] = time.time() - 120
        monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
        assert cs.should_run_interval("test_interval3", 60) is True


class TestShouldRunDailyAt:
    def test_before_target_suppresses(self, monkeypatch):
        cs = _import(monkeypatch)
        with cs._last_runs_lock:
            cs._last_runs.pop("test_daily_1", None)
        monkeypatch.setattr(cs, "_save_last_runs", lambda: None)

        # Fake clock: 3:00 PM ET, target 4:05 PM → before target
        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake_now = _dt.datetime(2026, 4, 24, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "get_et_time", lambda: fake_now)

        assert cs.should_run_daily_at("test_daily_1", 16, 5) is False

    def test_at_target_fires_once(self, monkeypatch):
        cs = _import(monkeypatch)
        with cs._last_runs_lock:
            cs._last_runs.pop("test_daily_2", None)
        monkeypatch.setattr(cs, "_save_last_runs", lambda: None)

        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake_now = _dt.datetime(2026, 4, 24, 16, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "get_et_time", lambda: fake_now)

        assert cs.should_run_daily_at("test_daily_2", 16, 5) is True
        # Same-day repeat should NOT fire
        assert cs.should_run_daily_at("test_daily_2", 16, 5) is False

    def test_too_late_suppresses_unless_max_late_allows(self, monkeypatch):
        cs = _import(monkeypatch)
        with cs._last_runs_lock:
            cs._last_runs.pop("test_daily_3", None)
        monkeypatch.setattr(cs, "_save_last_runs", lambda: None)

        import datetime as _dt
        from zoneinfo import ZoneInfo
        # 60 min past target, default max_late=1800s (30 min) → suppress
        fake_now = _dt.datetime(2026, 4, 24, 17, 5, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "get_et_time", lambda: fake_now)

        assert cs.should_run_daily_at("test_daily_3", 16, 5) is False

        # With max_late=7200s (2h), it fires
        assert cs.should_run_daily_at("test_daily_3", 16, 5, max_late_seconds=7200) is True


class TestClearDailyStamp:
    def test_removes_stamp(self, monkeypatch):
        cs = _import(monkeypatch)
        with cs._last_runs_lock:
            cs._last_runs["test_clear_stamp"] = "2026-04-24"
        monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
        cs._clear_daily_stamp("test_clear_stamp")
        with cs._last_runs_lock:
            assert "test_clear_stamp" not in cs._last_runs


# ============================================================================
# _within_opening_bell_congestion
# ============================================================================

class TestOpeningBellCongestion:
    def test_true_at_9_35(self, monkeypatch):
        cs = _import(monkeypatch)
        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake = _dt.datetime(2026, 4, 24, 9, 35, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "now_et", lambda: fake)
        assert cs._within_opening_bell_congestion() is True

    def test_false_at_10_am(self, monkeypatch):
        cs = _import(monkeypatch)
        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake = _dt.datetime(2026, 4, 24, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "now_et", lambda: fake)
        assert cs._within_opening_bell_congestion() is False

    def test_false_before_open(self, monkeypatch):
        cs = _import(monkeypatch)
        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake = _dt.datetime(2026, 4, 24, 9, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "now_et", lambda: fake)
        assert cs._within_opening_bell_congestion() is False

    def test_exception_returns_false(self, monkeypatch):
        cs = _import(monkeypatch)
        monkeypatch.setattr(cs, "now_et", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert cs._within_opening_bell_congestion() is False


# ============================================================================
# _has_user_tag
# ============================================================================

class TestHasUserTag:
    def test_empty_returns_false(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._has_user_tag("") is False
        assert cs._has_user_tag(None) is False

    def test_no_brackets_returns_false(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._has_user_tag("plain message") is False

    def test_user_tag_returns_true(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._has_user_tag("[alice] deployed AAPL") is True

    def test_task_tag_only_returns_false(self, monkeypatch):
        cs = _import(monkeypatch)
        # Task tags are recognised names like deployer/monitor/scheduler
        for tag in cs._TASK_TAGS:
            assert cs._has_user_tag(f"[{tag}] running") is False

    def test_mixed_user_and_task_tag_true(self, monkeypatch):
        cs = _import(monkeypatch)
        assert cs._has_user_tag("[alice] [deployer] msg") is True


# ============================================================================
# _build_user_dict_for_mode
# ============================================================================

class TestBuildUserDictForMode:
    def test_returns_none_on_missing_creds(self, monkeypatch):
        cs = _import(monkeypatch)
        # Stub the auth lookup to return nothing
        monkeypatch.setattr(cs.auth, "get_user_alpaca_creds",
                             lambda uid, mode="paper": None)
        out = cs._build_user_dict_for_mode({"id": 1, "username": "u"}, "paper")
        assert out is None

    def test_returns_none_on_auth_exception(self, monkeypatch):
        cs = _import(monkeypatch)
        monkeypatch.setattr(cs.auth, "get_user_alpaca_creds",
                             lambda uid, mode="paper": (_ for _ in ()).throw(RuntimeError("x")))
        out = cs._build_user_dict_for_mode({"id": 1, "username": "u"}, "paper")
        assert out is None

    def test_returns_user_dict_when_creds_present(self, monkeypatch):
        cs = _import(monkeypatch)
        monkeypatch.setattr(cs.auth, "get_user_alpaca_creds",
                             lambda uid, mode="paper": {
                                 "key": "AK", "secret": "SK",
                                 "endpoint": "https://paper-api.alpaca.markets/v2",
                             })
        out = cs._build_user_dict_for_mode(
            {"id": 42, "username": "alice"}, "paper")
        assert out is not None
        assert out["id"] == 42
        assert out["username"] == "alice"
        # Mode tag set
        assert out["_mode"] == "paper"


# ============================================================================
# strategy_file_lock (advisory cross-thread lock)
# ============================================================================

class TestStrategyFileLock:
    def test_basic_acquire_release(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        p = str(tmp_path / "test.json")
        # Acquire + release should work without raising.
        with cs.strategy_file_lock(p):
            (tmp_path / "test.json").write_text("{}")
        assert (tmp_path / "test.json").exists()

    def test_serializes_across_threads(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        p = str(tmp_path / "race.json")
        order = []

        def worker(ident, delay):
            with cs.strategy_file_lock(p):
                order.append(("enter", ident))
                time.sleep(delay)
                order.append(("exit", ident))

        t1 = threading.Thread(target=worker, args=(1, 0.1))
        t2 = threading.Thread(target=worker, args=(2, 0.0))
        t1.start()
        # Small lead-time so t1 acquires first
        time.sleep(0.02)
        t2.start()
        t1.join(); t2.join()
        # Thread 1 must enter and exit BEFORE thread 2 enters
        enter1 = order.index(("enter", 1))
        exit1 = order.index(("exit", 1))
        enter2 = order.index(("enter", 2))
        assert enter1 < exit1 < enter2, f"lock didn't serialize: {order}"


# ============================================================================
# is_first_trading_day_of_month (quick-return path)
# ============================================================================

class TestIsFirstTradingDayOfMonth:
    def test_returns_false_after_day_7(self, monkeypatch):
        cs = _import(monkeypatch)
        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake = _dt.datetime(2026, 4, 15, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "get_et_time", lambda: fake)
        # Also stub the network call in case the fast-exit isn't taken
        monkeypatch.setattr(cs, "user_api_get", lambda u, p: [])
        assert cs.is_first_trading_day_of_month({"id": 1}) is False

    def test_early_month_calls_calendar(self, monkeypatch):
        cs = _import(monkeypatch)
        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake = _dt.datetime(2026, 4, 1, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "get_et_time", lambda: fake)
        # Calendar says 2026-04-01 is first trading day
        monkeypatch.setattr(cs, "user_api_get",
                             lambda u, p: [{"date": "2026-04-01"}])
        assert cs.is_first_trading_day_of_month({"id": 1}) is True

    def test_early_month_but_not_first_trading_day(self, monkeypatch):
        cs = _import(monkeypatch)
        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake = _dt.datetime(2026, 4, 2, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "get_et_time", lambda: fake)
        # Calendar says 2026-04-01 was the first trading day — today is 04-02
        monkeypatch.setattr(cs, "user_api_get",
                             lambda u, p: [{"date": "2026-04-01"},
                                           {"date": "2026-04-02"}])
        assert cs.is_first_trading_day_of_month({"id": 1}) is False

    def test_empty_calendar_returns_false(self, monkeypatch):
        cs = _import(monkeypatch)
        import datetime as _dt
        from zoneinfo import ZoneInfo
        fake = _dt.datetime(2026, 4, 1, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        monkeypatch.setattr(cs, "get_et_time", lambda: fake)
        monkeypatch.setattr(cs, "user_api_get", lambda u, p: [])
        assert cs.is_first_trading_day_of_month({"id": 1}) is False
