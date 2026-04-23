"""
Round-61 pt.4: behavioral coverage for PDT / settled-funds / fractional
helpers. Pre-pt.4 coverage on these three modules:
  * pdt_tracker.py ≈ 60%
  * settled_funds.py ≈ 76%
  * fractional.py  ≈ 60%

These modules drive the cash-account risk guardrails: PDT rule awareness
(margin <$25k), T+1 settled-funds enforcement (cash accounts), and
fractional-share sizing. A silent regression here causes Good Faith
Violations (settled-funds), PDT restriction flags (pdt_tracker), or
order rejections (fractional). All are recoverable but annoying and
cost the user real money in opportunity cost.

Tests below exercise the public APIs end-to-end with temp directories
for the per-user JSON state. No Alpaca calls; api_get_fn is stubbed
where needed.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta

import pytest


def _user(tmp):
    d = os.path.join(tmp, "user1")
    os.makedirs(d, exist_ok=True)
    return {"id": 1, "username": "alice", "_data_dir": d}


# =====================================================================
# pdt_tracker
# =====================================================================

class TestPdtTracker:
    def test_is_day_trade_same_day_true(self):
        import pdt_tracker as p
        assert p.is_day_trade("2026-04-23T10:00:00-04:00",
                               "2026-04-23T15:30:00-04:00") is True

    def test_is_day_trade_different_day_false(self):
        import pdt_tracker as p
        assert p.is_day_trade("2026-04-22T15:30:00-04:00",
                               "2026-04-23T10:00:00-04:00") is False

    def test_is_day_trade_empty_inputs_false(self):
        import pdt_tracker as p
        assert p.is_day_trade("", "2026-04-23T10:00:00-04:00") is False
        assert p.is_day_trade("2026-04-23T10:00:00-04:00", "") is False
        assert p.is_day_trade(None, None) is False

    def test_is_day_trade_parse_error_returns_false(self):
        import pdt_tracker as p
        assert p.is_day_trade("garbage", "also-bad") is False

    def test_can_day_trade_empty_tier_allows(self):
        import pdt_tracker as p
        allowed, reason, rem = p.can_day_trade({})
        assert allowed is True
        assert rem is None

    def test_can_day_trade_cash_account_allows(self):
        import pdt_tracker as p
        allowed, _, _ = p.can_day_trade({"pdt_applies": False})
        assert allowed is True

    def test_can_day_trade_margin_25k_allows(self):
        import pdt_tracker as p
        # Margin >$25k: pdt_applies=False even with remaining=0
        allowed, _, _ = p.can_day_trade({
            "pdt_applies": False, "_detected_day_trades_remaining": 0})
        assert allowed is True

    def test_can_day_trade_cash_margin_25k_below_buffer_denies(self):
        import pdt_tracker as p
        allowed, reason, rem = p.can_day_trade(
            {"pdt_applies": True, "_detected_day_trades_remaining": 1},
            buffer=1)
        assert allowed is False
        assert "buffer" in reason.lower()
        assert rem == 1

    def test_can_day_trade_above_buffer_allows(self):
        import pdt_tracker as p
        allowed, _, rem = p.can_day_trade(
            {"pdt_applies": True, "_detected_day_trades_remaining": 3},
            buffer=1)
        assert allowed is True
        assert rem == 3

    def test_can_day_trade_unknown_remaining_allows_conservatively(self):
        import pdt_tracker as p
        allowed, reason, rem = p.can_day_trade(
            {"pdt_applies": True}, buffer=1)  # no _detected_day_trades_remaining
        assert allowed is True
        assert "unknown" in reason.lower()
        assert rem is None

    def test_log_day_trade_creates_entry(self, tmp_path):
        import pdt_tracker as p
        user = {"_data_dir": str(tmp_path)}
        p.log_day_trade(user, "AAPL", "trailing_stop")
        log_file = tmp_path / p.PDT_LOG_FILENAME
        assert log_file.exists()
        entries = json.loads(log_file.read_text())
        assert len(entries) == 1
        assert entries[0]["symbol"] == "AAPL"
        assert entries[0]["strategy"] == "trailing_stop"

    def test_log_day_trade_appends_multiple(self, tmp_path):
        import pdt_tracker as p
        user = {"_data_dir": str(tmp_path)}
        p.log_day_trade(user, "AAPL")
        p.log_day_trade(user, "MSFT")
        entries = json.loads((tmp_path / p.PDT_LOG_FILENAME).read_text())
        assert {e["symbol"] for e in entries} == {"AAPL", "MSFT"}

    def test_log_day_trade_no_data_dir_is_best_effort(self):
        import pdt_tracker as p
        # Must not raise even when _data_dir is missing/bad
        p.log_day_trade({}, "AAPL")  # empty user
        p.log_day_trade({"_data_dir": "/nonexistent/xyz"}, "AAPL")

    def test_log_day_trade_prunes_old_entries(self, tmp_path):
        import pdt_tracker as p
        log_path = tmp_path / p.PDT_LOG_FILENAME
        # Pre-seed with a very old entry
        old_date = (date.today() - timedelta(days=30)).isoformat()
        log_path.write_text(json.dumps([
            {"date": old_date, "symbol": "OLD", "strategy": "x"},
        ]))
        user = {"_data_dir": str(tmp_path)}
        p.log_day_trade(user, "NEW")
        entries = json.loads(log_path.read_text())
        symbols = {e["symbol"] for e in entries}
        assert "OLD" not in symbols, "entries older than LOG_RETENTION_DAYS must be pruned"
        assert "NEW" in symbols

    def test_count_local_day_trades_empty_dir(self):
        import pdt_tracker as p
        assert p.count_local_day_trades_last_5_business_days({}) == 0

    def test_count_local_day_trades_no_file(self, tmp_path):
        import pdt_tracker as p
        user = {"_data_dir": str(tmp_path)}
        assert p.count_local_day_trades_last_5_business_days(user) == 0

    def test_count_local_day_trades_recent_only(self, tmp_path):
        import pdt_tracker as p
        log_path = tmp_path / p.PDT_LOG_FILENAME
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=10)).isoformat()
        log_path.write_text(json.dumps([
            {"date": today, "symbol": "A"},
            {"date": today, "symbol": "B"},
            {"date": old, "symbol": "C"},  # outside the 7-day window
        ]))
        user = {"_data_dir": str(tmp_path)}
        assert p.count_local_day_trades_last_5_business_days(user) == 2

    def test_count_local_day_trades_malformed_file(self, tmp_path):
        import pdt_tracker as p
        (tmp_path / p.PDT_LOG_FILENAME).write_text("not json")
        user = {"_data_dir": str(tmp_path)}
        assert p.count_local_day_trades_last_5_business_days(user) == 0

    def test_count_local_day_trades_non_list_json(self, tmp_path):
        import pdt_tracker as p
        (tmp_path / p.PDT_LOG_FILENAME).write_text('{"not": "a list"}')
        user = {"_data_dir": str(tmp_path)}
        assert p.count_local_day_trades_last_5_business_days(user) == 0


# =====================================================================
# settled_funds
# =====================================================================

class TestSettledFunds:
    def test_ledger_path_requires_data_dir(self):
        import settled_funds as s
        with pytest.raises(ValueError, match="_data_dir"):
            s._ledger_path({})

    def test_record_sale_creates_entry(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        s.record_sale(user, "AAPL", 1000.0, sold_on=date(2026, 4, 22))
        ledger = s._load_ledger(user)
        assert len(ledger) == 1
        assert ledger[0]["symbol"] == "AAPL"
        assert ledger[0]["amount"] == 1000.0
        assert ledger[0]["sold_on"] == "2026-04-22"

    def test_record_sale_zero_proceeds_skipped(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        s.record_sale(user, "AAPL", 0)
        s.record_sale(user, "AAPL", -100)
        s.record_sale(user, "AAPL", None)
        s.record_sale(user, "AAPL", "bad")
        assert s._load_ledger(user) == []

    def test_record_sale_uppercases_symbol(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        s.record_sale(user, "aapl", 500.0)
        assert s._load_ledger(user)[0]["symbol"] == "AAPL"

    def test_next_business_day_skips_weekends(self):
        import settled_funds as s
        # Friday → Monday
        friday = date(2026, 4, 24)
        assert friday.weekday() == 4
        assert s._next_business_day(friday, 1) == date(2026, 4, 27)
        # Thursday → Friday
        thursday = date(2026, 4, 23)
        assert s._next_business_day(thursday, 1) == date(2026, 4, 24)

    def test_unsettled_cash_sums_unsettled(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        today = date.today()
        # Sale today settles tomorrow → still unsettled as of today
        s.record_sale(user, "AAPL", 1000.0, sold_on=today)
        s.record_sale(user, "MSFT", 2500.0, sold_on=today)
        total = s.unsettled_cash(user, as_of=today)
        assert total == 3500.0

    def test_unsettled_cash_ignores_settled(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        # Sold 3 business days ago — already settled
        old = date.today() - timedelta(days=5)
        s.record_sale(user, "AAPL", 1000.0, sold_on=old)
        assert s.unsettled_cash(user) == 0.0

    def test_settled_cash_available_applies_buffer(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        # No sales → entire cash settled, minus 5% buffer
        usable = s.settled_cash_available(user, total_cash=10000.0)
        assert usable == 9500.0  # 95% of 10k

    def test_settled_cash_available_subtracts_unsettled(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        today = date.today()
        s.record_sale(user, "AAPL", 3000.0, sold_on=today)
        usable = s.settled_cash_available(user, total_cash=10000.0,
                                           as_of=today)
        # 10000 - 3000 unsettled = 7000 * 0.95 = 6650
        assert usable == 6650.0

    def test_settled_cash_available_never_negative(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        today = date.today()
        # Huge unsettled entry exceeds cash (data inconsistency)
        s.record_sale(user, "AAPL", 20000.0, sold_on=today)
        usable = s.settled_cash_available(user, total_cash=10000.0,
                                           as_of=today)
        assert usable == 0.0

    def test_settled_cash_available_bad_input(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        assert s.settled_cash_available(user, total_cash="bad") == 0.0
        assert s.settled_cash_available(user, total_cash=None) == 0.0

    def test_can_deploy_margin_bypass(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        ok, usable, reason = s.can_deploy(
            user, desired_spend=100000.0, total_cash=500.0,
            tier_cfg={"settled_funds_required": False})
        assert ok is True
        assert reason == ""

    def test_can_deploy_cash_sufficient(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        ok, usable, reason = s.can_deploy(
            user, desired_spend=1000.0, total_cash=5000.0,
            tier_cfg={"settled_funds_required": True})
        assert ok is True
        assert usable >= 1000.0

    def test_can_deploy_cash_insufficient_has_helpful_reason(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        today = date.today()
        s.record_sale(user, "AAPL", 4000.0, sold_on=today)
        ok, usable, reason = s.can_deploy(
            user, desired_spend=3000.0, total_cash=5000.0,
            tier_cfg={"settled_funds_required": True}, as_of=today)
        assert ok is False
        assert "Good Faith Violation" in reason
        assert "unsettled" in reason
        # Reason should include next-settlement date
        assert "settlement" in reason.lower() or "settles" in reason.lower()

    def test_can_deploy_zero_spend_allowed(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        ok, _, _ = s.can_deploy(user, desired_spend=0,
                                 total_cash=100.0,
                                 tier_cfg={"settled_funds_required": True})
        assert ok is True

    def test_can_deploy_bad_spend(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        ok, _, reason = s.can_deploy(user, desired_spend="bad",
                                      total_cash=100.0,
                                      tier_cfg={"settled_funds_required": True})
        assert ok is False
        assert "invalid" in reason

    def test_load_ledger_prunes_expired_entries(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        path = s._ledger_path(user)
        very_old = (date.today() - timedelta(days=60)).isoformat()
        recent = date.today().isoformat()
        with open(path, "w") as f:
            json.dump([
                {"settles_on": very_old, "amount": 1000, "symbol": "X"},
                {"settles_on": recent, "amount": 500, "symbol": "Y"},
            ], f)
        ledger = s._load_ledger(user)
        assert len(ledger) == 1
        assert ledger[0]["symbol"] == "Y"

    def test_load_ledger_malformed_returns_empty(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        path = s._ledger_path(user)
        with open(path, "w") as f:
            f.write("not json at all")
        assert s._load_ledger(user) == []

    def test_load_ledger_non_list_returns_empty(self, tmp_path):
        import settled_funds as s
        user = _user(str(tmp_path))
        path = s._ledger_path(user)
        with open(path, "w") as f:
            json.dump({"not": "a list"}, f)
        assert s._load_ledger(user) == []


# =====================================================================
# fractional
# =====================================================================

class TestFractional:
    def test_cache_path_requires_data_dir(self):
        import fractional as f
        with pytest.raises(ValueError, match="_data_dir"):
            f._cache_path({})

    def test_load_cache_missing_returns_none(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        assert f._load_cache(user) is None

    def test_load_cache_stale_returns_none(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        path = f._cache_path(user)
        # Stale: cached_at in the distant past
        with open(path, "w") as fh:
            json.dump({"cached_at": 0, "symbols": ["AAPL"]}, fh)
        assert f._load_cache(user) is None

    def test_load_cache_fresh_returns_data(self, tmp_path):
        import fractional as f
        import time
        user = _user(str(tmp_path))
        path = f._cache_path(user)
        with open(path, "w") as fh:
            json.dump({"cached_at": time.time(), "symbols": ["AAPL", "MSFT"]}, fh)
        data = f._load_cache(user)
        assert data is not None
        assert "AAPL" in data["symbols"]

    def test_load_cache_malformed_returns_none(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        path = f._cache_path(user)
        with open(path, "w") as fh:
            fh.write("not json")
        assert f._load_cache(user) is None

    def test_save_cache_writes_atomically(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        f._save_cache(user, ["AAPL", "MSFT", "TSLA"])
        data = f._load_cache(user)
        assert data is not None
        assert set(data["symbols"]) == {"AAPL", "MSFT", "TSLA"}
        # No .tmp litter
        cache_dir = os.path.dirname(f._cache_path(user))
        assert not any(n.endswith(".tmp") for n in os.listdir(cache_dir))

    def test_refresh_cache_non_list_response(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        # Stub returns an error dict instead of a list — must return None
        result = f.refresh_cache(user, lambda u, p: {"error": "boom"})
        assert result is None

    def test_refresh_cache_filters_non_fractionable(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        assets = [
            {"symbol": "AAPL", "fractionable": True, "tradable": True},
            {"symbol": "MSFT", "fractionable": True, "tradable": True},
            {"symbol": "OLD", "fractionable": False, "tradable": True},
            {"symbol": "UNTRADEABLE", "fractionable": True, "tradable": False},
        ]
        result = f.refresh_cache(user, lambda u, p: assets)
        assert result == {"AAPL", "MSFT"}

    def test_refresh_cache_api_exception_returns_none(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        def boom(u, p):
            raise RuntimeError("network")
        assert f.refresh_cache(user, boom) is None

    def test_get_fractionable_symbols_uses_cache(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        f._save_cache(user, ["AAPL", "TSLA"])
        # Calling without api_get_fn should work — cache is fresh
        assert f.get_fractionable_symbols(user) == {"AAPL", "TSLA"}

    def test_get_fractionable_symbols_no_cache_no_fetcher_fails_safe(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        # No cache, no fetcher — must return empty set (fail-safe, not raise)
        assert f.get_fractionable_symbols(user) == set()

    def test_is_fractionable_empty_symbol(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        assert f.is_fractionable("", user) is False
        assert f.is_fractionable(None, user) is False

    def test_is_fractionable_hits_cache(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        f._save_cache(user, ["AAPL", "TSLA"])
        assert f.is_fractionable("AAPL", user) is True
        assert f.is_fractionable("ZZZZ", user) is False
        assert f.is_fractionable("aapl", user) is True  # case-insensitive

    def test_size_position_invalid_inputs(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        tier = {"fractional_default": True}
        r = f.size_position("", 100, 10, user, tier)
        assert r["qty"] == 0
        r = f.size_position("AAPL", 0, 10, user, tier)
        assert r["qty"] == 0
        r = f.size_position("AAPL", 100, 0, user, tier)
        assert r["qty"] == 0
        r = f.size_position("AAPL", 100, -5, user, tier)
        assert r["qty"] == 0

    def test_size_position_fractional_path(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        f._save_cache(user, ["AAPL"])
        tier = {"fractional_default": True}
        r = f.size_position("AAPL", target_dollars=25.0, price=250.0,
                             user=user, tier_cfg=tier)
        assert r["fractional"] is True
        assert r["order_type_hint"] == "market"
        assert r["qty"] == 0.1  # 25/250
        assert r["notional"] == 25.0

    def test_size_position_whole_share_path(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        # No cache; fractional disabled via tier anyway
        tier = {"fractional_default": False}
        r = f.size_position("AAPL", target_dollars=500.0, price=100.0,
                             user=user, tier_cfg=tier)
        assert r["fractional"] is False
        assert r["qty"] == 5
        assert r["notional"] == 500.0
        assert r["order_type_hint"] == "limit"

    def test_size_position_below_fractional_min_falls_back(self, tmp_path):
        """Target below $1 minimum BUT whole share affordable → whole share."""
        import fractional as f
        user = _user(str(tmp_path))
        f._save_cache(user, ["AAPL"])
        tier = {"fractional_default": True}
        # Target $0.50 (below $1 min) but price is only $0.25 (whole share OK)
        r = f.size_position("AAPL", target_dollars=0.50, price=0.25,
                             user=user, tier_cfg=tier)
        assert r["qty"] >= 1
        assert r["fractional"] is False

    def test_size_position_price_above_target_whole_share_fails(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        tier = {"fractional_default": False}
        r = f.size_position("EXPENSIVE", target_dollars=50.0, price=1000.0,
                             user=user, tier_cfg=tier)
        assert r["qty"] == 0
        assert "price" in r["reason"].lower() or "target" in r["reason"].lower()

    def test_size_position_non_fractionable_symbol(self, tmp_path):
        import fractional as f
        user = _user(str(tmp_path))
        f._save_cache(user, ["AAPL"])  # only AAPL is fractionable
        tier = {"fractional_default": True}
        # ZZZZ isn't in cache → not fractionable → whole-share only
        r = f.size_position("ZZZZ", target_dollars=500.0, price=100.0,
                             user=user, tier_cfg=tier)
        assert r["fractional"] is False
        assert r["qty"] == 5
