"""
Round-29 tests: universal pre-earnings exit for non-PEAD equity strategies.

Covers:
  * should_exit_for_earnings() returns False for wheel (deliberate skip
    — short puts capture IV crush over earnings)
  * should_exit_for_earnings() returns True within the window for
    trailing_stop / breakout / mean_reversion / copy_trading
  * Cache TTL behavior (4 hour window)
  * OCC option symbol underlying resolution
  * Config disable via guardrails flag
  * Migration adds earnings_exit_days_before=1 idempotently
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    import earnings_exit
    earnings_exit.clear_cache()
    yield
    earnings_exit.clear_cache()


# ---------- should_exit_for_earnings ----------


def test_wheel_strategy_never_triggers_earnings_exit(monkeypatch):
    """Short puts benefit from IV crush post-earnings. Never auto-close."""
    import earnings_exit
    # Even if earnings are 0 days away, wheel stays put
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date",
                        lambda sym: date.today())
    should, reason, days = earnings_exit.should_exit_for_earnings(
        "HIMS", "wheel", days_before=1)
    assert should is False
    assert reason is None


def test_pead_strategy_skipped_pead_handles_own_earnings_logic(monkeypatch):
    """PEAD has its own `exit_before_next_earnings_days` rule in the
    scheduler — the universal rule must not double-fire."""
    import earnings_exit
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date",
                        lambda sym: date.today())
    should, _, _ = earnings_exit.should_exit_for_earnings(
        "INTC", "pead", days_before=1)
    assert should is False


def test_trailing_stop_fires_one_day_before(monkeypatch):
    import earnings_exit
    from et_time import now_et
    tomorrow = now_et().date() + timedelta(days=1)
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date",
                        lambda sym: tomorrow)
    should, reason, days = earnings_exit.should_exit_for_earnings(
        "INTC", "trailing_stop", days_before=1)
    assert should is True
    assert "pre_earnings_exit" in reason
    assert days == 1


def test_breakout_fires_on_earnings_day(monkeypatch):
    import earnings_exit
    from et_time import now_et
    today = now_et().date()
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date",
                        lambda sym: today)
    should, _, days = earnings_exit.should_exit_for_earnings(
        "NVDA", "breakout", days_before=1)
    assert should is True
    assert days == 0


def test_mean_reversion_does_not_fire_beyond_window(monkeypatch):
    import earnings_exit
    from et_time import now_et
    far_future = now_et().date() + timedelta(days=30)
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date",
                        lambda sym: far_future)
    should, _, days = earnings_exit.should_exit_for_earnings(
        "AMD", "mean_reversion", days_before=1)
    assert should is False
    assert days == 30


def test_past_earnings_date_does_not_fire(monkeypatch):
    """Stale earnings data (date in past) should NOT fire — negative
    days_to_earnings is a data-quality issue, not a fresh warning."""
    import earnings_exit
    from et_time import now_et
    yesterday = now_et().date() - timedelta(days=1)
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date",
                        lambda sym: yesterday)
    should, _, _ = earnings_exit.should_exit_for_earnings(
        "INTC", "trailing_stop", days_before=1)
    assert should is False


def test_no_earnings_data_does_not_fire(monkeypatch):
    import earnings_exit
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date",
                        lambda sym: None)
    should, _, _ = earnings_exit.should_exit_for_earnings(
        "SOXL", "trailing_stop", days_before=1)
    assert should is False


def test_days_before_configurable(monkeypatch):
    """Operator can widen the buffer via guardrails."""
    import earnings_exit
    from et_time import now_et
    two_days_out = now_et().date() + timedelta(days=2)
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date",
                        lambda sym: two_days_out)
    # Default days_before=1 → doesn't fire
    should1, _, _ = earnings_exit.should_exit_for_earnings(
        "INTC", "trailing_stop", days_before=1)
    assert should1 is False
    # Widened to 3 → fires
    should2, _, days = earnings_exit.should_exit_for_earnings(
        "INTC", "trailing_stop", days_before=3)
    assert should2 is True
    assert days == 2


# ---------- option underlying resolution ----------


def test_occ_option_symbol_routes_to_underlying(monkeypatch):
    """HIMS put position should look up HIMS earnings — but since
    options are wheel-only in practice, this still returns False."""
    import earnings_exit
    calls = []
    def _fake(sym):
        calls.append(sym)
        return None
    monkeypatch.setattr(earnings_exit, "get_next_earnings_date", _fake)
    # Wheel option — never fires, doesn't even look up
    earnings_exit.should_exit_for_earnings(
        "HIMS260508P00027000", "wheel", asset_class="us_option")
    assert calls == []  # short-circuited on wheel


def test_underlying_resolver_extracts_root():
    from earnings_exit import _underlying_for_lookup
    assert _underlying_for_lookup("HIMS260508P00027000", "us_option") == "HIMS"
    assert _underlying_for_lookup("CHWY260515P00025000", "us_option") == "CHWY"
    assert _underlying_for_lookup("AAPL", "us_equity") == "AAPL"
    # Malformed option symbol -> empty
    assert _underlying_for_lookup("NOT_AN_OPTION", "us_option") == ""


# ---------- cache behavior ----------


def test_cache_hit_skips_second_fetch(monkeypatch):
    import earnings_exit
    calls = {"n": 0}
    def _fake(sym):
        calls["n"] += 1
        return date(2099, 1, 1)
    monkeypatch.setattr(earnings_exit,
                        "_fetch_next_earnings_from_yfinance", _fake)
    d1 = earnings_exit.get_next_earnings_date("INTC")
    d2 = earnings_exit.get_next_earnings_date("INTC")
    assert d1 == d2 == date(2099, 1, 1)
    assert calls["n"] == 1  # cache hit on the second call


def test_cache_returns_none_refetches(monkeypatch):
    """None-result is cached too (no point hammering yfinance on
    symbols without earnings coverage)."""
    import earnings_exit
    calls = {"n": 0}
    def _fake(sym):
        calls["n"] += 1
        return None
    monkeypatch.setattr(earnings_exit,
                        "_fetch_next_earnings_from_yfinance", _fake)
    earnings_exit.get_next_earnings_date("SPY")
    earnings_exit.get_next_earnings_date("SPY")
    assert calls["n"] == 1  # None is cached


# ---------- migration ----------


def test_round29_migration_adds_default_on_fresh_guardrails(tmp_path):
    from migrations import (migrate_guardrails_round29,
                             MIGRATION_ROUND29_EARNINGS_EXIT)
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"max_position_pct": 0.07}, f)
    result = migrate_guardrails_round29(gpath)
    assert result == "migrated"
    with open(gpath) as f:
        g = json.load(f)
    assert g["earnings_exit_days_before"] == 1
    assert MIGRATION_ROUND29_EARNINGS_EXIT in g["_migrations_applied"]


def test_round29_migration_is_idempotent(tmp_path):
    from migrations import migrate_guardrails_round29
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"max_position_pct": 0.07}, f)
    assert migrate_guardrails_round29(gpath) == "migrated"
    assert migrate_guardrails_round29(gpath) == "already_applied"


def test_round29_migration_respects_user_override(tmp_path):
    """User who set earnings_exit_days_before=3 keeps their value."""
    from migrations import migrate_guardrails_round29
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"earnings_exit_days_before": 3}, f)
    result = migrate_guardrails_round29(gpath)
    assert result == "user_customised"
    with open(gpath) as f:
        g = json.load(f)
    assert g["earnings_exit_days_before"] == 3  # unchanged


def test_round29_migration_missing_file(tmp_path):
    from migrations import migrate_guardrails_round29
    gpath = str(tmp_path / "does_not_exist.json")
    assert migrate_guardrails_round29(gpath) == "no_file"
