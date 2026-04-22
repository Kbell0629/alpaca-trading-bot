"""
Round-50 tests: portfolio auto-calibration across all 6 tiers.

Validates every tier's detection + config + Alpaca-rule compliance,
plus the fractional / PDT / settled-funds integration helpers.

User's requirement: "test every tier to ensure you did not introduce
errors or bugs and is complete." Tiers covered:
  1. cash_micro       ($500-$2k)   — no shorts, no wheel, fractional ON
  2. cash_small       ($2k-$25k)   — no shorts, no wheel, fractional ON
  3. cash_standard    ($25k+)      — no shorts, wheel ON
  4. margin_small     ($2k-$25k)   — SHORTS ON, PDT APPLIES, fractional ON
  5. margin_standard  ($25k-$500k) — all 6 strategies, no PDT
  6. margin_whale     ($500k+)     — single-ticker cap
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pytest


# ========== Fixture helpers ==========


def _account(multiplier, equity, cash=None, cash_withdrawable=None,
              pattern_day_trader=False, shorting_enabled=None,
              day_trades_remaining=None, buying_power=None):
    """Build a fake /v2/account response."""
    if cash is None:
        cash = equity * 0.3  # ~30% cash reserve is typical
    if cash_withdrawable is None:
        cash_withdrawable = cash
    if shorting_enabled is None:
        shorting_enabled = (multiplier >= 2)
    if buying_power is None:
        buying_power = equity * multiplier
    return {
        "multiplier": str(multiplier),
        "equity": str(equity),
        "cash": str(cash),
        "cash_withdrawable": str(cash_withdrawable),
        "pattern_day_trader": pattern_day_trader,
        "shorting_enabled": shorting_enabled,
        "day_trades_remaining": day_trades_remaining,
        "buying_power": str(buying_power),
    }


# ========== Tier detection — one test per tier ==========


def test_tier_cash_micro():
    from portfolio_calibration import detect_tier
    tier = detect_tier(_account(multiplier=1, equity=1500))
    assert tier is not None
    assert tier["name"] == "cash_micro"
    assert tier["account_type"] == "cash"
    assert tier["short_enabled"] is False  # Alpaca rule: no shorts on cash
    assert tier["wheel_enabled"] is False  # too small for CSP
    assert tier["fractional_default"] is True
    assert tier["pdt_applies"] is False  # cash = unlimited day trades
    assert tier["settled_funds_required"] is True
    assert tier["max_positions"] == 2
    assert tier["max_position_pct"] == 0.15
    assert "trailing_stop" in tier["strategies_enabled"]
    assert "short_sell" not in tier["strategies_enabled"]


def test_tier_cash_small():
    from portfolio_calibration import detect_tier
    tier = detect_tier(_account(multiplier=1, equity=10000))
    assert tier["name"] == "cash_small"
    assert tier["account_type"] == "cash"
    assert tier["short_enabled"] is False
    assert tier["wheel_enabled"] is False  # cash < $25k by default
    assert tier["fractional_default"] is True
    assert tier["pdt_applies"] is False
    assert tier["max_positions"] == 5
    assert tier["max_position_pct"] == 0.10
    for s in ("trailing_stop", "breakout", "mean_reversion",
              "pead", "copy_trading"):
        assert s in tier["strategies_enabled"]
    assert "short_sell" not in tier["strategies_enabled"]


def test_tier_cash_standard():
    from portfolio_calibration import detect_tier
    tier = detect_tier(_account(multiplier=1, equity=100000))
    assert tier["name"] == "cash_standard"
    assert tier["wheel_enabled"] is True
    assert tier["short_enabled"] is False
    assert tier["fractional_default"] is False  # whole shares default at $25k+
    assert tier["max_positions"] == 8
    assert tier["max_position_pct"] == 0.07
    assert "wheel" in tier["strategies_enabled"]
    assert "short_sell" not in tier["strategies_enabled"]


def test_tier_margin_small_pdt_applies():
    from portfolio_calibration import detect_tier
    tier = detect_tier(_account(
        multiplier=2, equity=10000,
        shorting_enabled=True,
        day_trades_remaining=3))
    assert tier["name"] == "margin_small"
    assert tier["account_type"] == "margin"
    assert tier["short_enabled"] is True  # margin + ≥$2k = shorts allowed
    assert tier["pdt_applies"] is True  # ← margin <$25k = PDT applies
    assert tier["fractional_default"] is True
    assert tier["settled_funds_required"] is False  # margin doesn't need T+1
    assert "short_sell" in tier["strategies_enabled"]
    assert "wheel" not in tier["strategies_enabled"]  # wheel gated to $25k+
    assert tier["min_stock_price"] == 3  # Alpaca margin <$3 rule


def test_tier_margin_standard():
    from portfolio_calibration import detect_tier
    tier = detect_tier(_account(
        multiplier=4, equity=50000,
        shorting_enabled=True,
        pattern_day_trader=True,
        day_trades_remaining=3))
    assert tier["name"] == "margin_standard"
    assert tier["pdt_applies"] is False  # $25k+ = PDT limits don't bite
    assert tier["short_enabled"] is True
    assert tier["wheel_enabled"] is True
    # Full 6 strategies
    for s in ("trailing_stop", "breakout", "mean_reversion",
              "pead", "copy_trading", "wheel", "short_sell"):
        assert s in tier["strategies_enabled"], f"missing {s} in margin_standard"
    assert tier["max_positions"] == 10
    assert tier["max_position_pct"] == 0.06


def test_tier_margin_whale():
    from portfolio_calibration import detect_tier
    tier = detect_tier(_account(
        multiplier=4, equity=750000,
        shorting_enabled=True))
    assert tier["name"] == "margin_whale"
    assert tier["max_positions"] == 15
    assert tier["max_position_pct"] == 0.04
    assert tier.get("single_ticker_cap_pct") == 0.08


def test_tier_below_micro_returns_none():
    from portfolio_calibration import detect_tier
    assert detect_tier(_account(multiplier=1, equity=100)) is None
    assert detect_tier(_account(multiplier=1, equity=499.99)) is None


def test_tier_invalid_input_returns_none():
    from portfolio_calibration import detect_tier
    assert detect_tier(None) is None
    assert detect_tier("not a dict") is None
    assert detect_tier({}) is None
    # Garbage multiplier
    assert detect_tier({"multiplier": "foo", "equity": "10000"}) is None


# ========== User overrides ==========


def test_user_overrides_merge_correctly():
    from portfolio_calibration import detect_tier, apply_user_overrides
    tier = detect_tier(_account(multiplier=1, equity=10000))
    # User sets custom max_position_pct
    merged = apply_user_overrides(tier, {"max_position_pct": 0.20})
    assert merged["max_position_pct"] == 0.20
    assert merged.get("user_override_max_position_pct") is True
    # Other defaults unchanged
    assert merged["max_positions"] == 5


def test_user_cannot_enable_shorts_on_cash_account():
    """Alpaca rule: shorts only on margin accounts. If user tries to
    override `short_enabled=True` on a cash account, the calibration
    must reject it silently (UI should block earlier too)."""
    from portfolio_calibration import detect_tier, apply_user_overrides
    tier = detect_tier(_account(multiplier=1, equity=10000))
    merged = apply_user_overrides(tier, {"short_enabled": True})
    assert merged["short_enabled"] is False  # cash blocks this
    assert merged.get("short_override_rejected") is True


def test_user_strategies_list_strips_short_on_cash():
    """If user provides strategies_enabled including short_sell but
    they're on a cash account, short_sell must be stripped."""
    from portfolio_calibration import detect_tier, apply_user_overrides
    tier = detect_tier(_account(multiplier=1, equity=10000))
    merged = apply_user_overrides(tier, {
        "strategies_enabled": ["trailing_stop", "short_sell", "breakout"],
    })
    assert "short_sell" not in merged["strategies_enabled"]
    assert "trailing_stop" in merged["strategies_enabled"]
    assert "breakout" in merged["strategies_enabled"]


# ========== Wheel / PDT / Settled funds helpers ==========


def test_wheel_is_affordable():
    from portfolio_calibration import wheel_is_affordable
    tier = {"_detected_cash_withdrawable": 800}
    # $5 strike × 100 × 1.5 = $750 required
    assert wheel_is_affordable(tier, lowest_strike=5.0) is True
    # $10 strike × 100 × 1.5 = $1500 required, $800 insufficient
    assert wheel_is_affordable(tier, lowest_strike=10.0) is False


def test_pdt_allows_overnight_always():
    """Overnight positions don't count as day trades — always allowed."""
    from portfolio_calibration import pdt_allows_exit
    tier = {"pdt_applies": True, "_detected_day_trades_remaining": 0}
    ok, reason = pdt_allows_exit(tier, is_intraday_exit=False)
    assert ok is True


def test_pdt_blocks_intraday_when_remaining_low():
    from portfolio_calibration import pdt_allows_exit
    tier = {"pdt_applies": True, "_detected_day_trades_remaining": 1}
    ok, reason = pdt_allows_exit(tier, is_intraday_exit=True, buffer=1)
    assert ok is False
    assert "PDT buffer low" in reason


def test_pdt_allows_intraday_when_plenty_remaining():
    from portfolio_calibration import pdt_allows_exit
    tier = {"pdt_applies": True, "_detected_day_trades_remaining": 3}
    ok, reason = pdt_allows_exit(tier, is_intraday_exit=True)
    assert ok is True


def test_pdt_passthrough_for_cash_accounts():
    """Cash accounts never have PDT — always allow intraday."""
    from portfolio_calibration import pdt_allows_exit
    tier = {"pdt_applies": False}
    ok, reason = pdt_allows_exit(tier, is_intraday_exit=True)
    assert ok is True


def test_settled_funds_passthrough_for_margin():
    """Margin accounts don't have T+1 constraints — always allow."""
    from portfolio_calibration import settled_funds_available
    tier = {"settled_funds_required": False,
            "_detected_cash_withdrawable": 0}
    ok, settled, reason = settled_funds_available(tier, desired_spend=1000)
    assert ok is True


def test_settled_funds_blocks_cash_overspend():
    from portfolio_calibration import settled_funds_available
    tier = {"settled_funds_required": True,
            "_detected_cash_withdrawable": 1000}
    # 95% of $1000 = $950 usable. $1500 > $950 → blocked.
    ok, settled, reason = settled_funds_available(tier, desired_spend=1500)
    assert ok is False
    assert "settled cash" in reason.lower()


# ========== Calibration summary shape ==========


def test_calibration_summary_has_dashboard_keys():
    from portfolio_calibration import detect_tier, calibration_summary
    tier = detect_tier(_account(multiplier=1, equity=10000))
    summary = calibration_summary(tier)
    for key in ("detected", "tier_name", "tier_display", "account_type",
                "equity", "max_positions", "max_position_pct",
                "strategies_enabled", "fractional_default",
                "wheel_enabled", "short_enabled", "pdt_applies"):
        assert key in summary, f"summary missing {key}"


def test_calibration_summary_undetected():
    from portfolio_calibration import calibration_summary
    summary = calibration_summary(None)
    assert summary["detected"] is False
    assert "reason" in summary


# ========== Fractional ==========


def test_fractional_sizing_fractional_tier_fractionable_symbol(tmp_path, monkeypatch):
    from fractional import size_position
    user = {"_data_dir": str(tmp_path)}
    # Pre-seed cache so we don't need to call Alpaca
    import time
    import json as _json
    with open(os.path.join(str(tmp_path), "fractionable_cache.json"), "w") as f:
        _json.dump({"cached_at": time.time(), "symbols": ["TSLA", "AAPL"]}, f)

    tier = {"fractional_default": True}
    # $25 target at TSLA $250 → 0.1 shares fractional
    r = size_position("TSLA", target_dollars=25.0, price=250.0,
                       user=user, tier_cfg=tier)
    assert r["qty"] == 0.1
    assert r["fractional"] is True
    assert r["order_type_hint"] == "market"
    assert r["notional"] == 25.0


def test_fractional_sizing_whole_share_tier():
    from fractional import size_position
    tier = {"fractional_default": False}
    # $250 at $50/share → 5 whole shares
    r = size_position("MSFT", target_dollars=250.0, price=50.0,
                       user={}, tier_cfg=tier)
    assert r["qty"] == 5
    assert r["fractional"] is False
    assert r["order_type_hint"] == "limit"


def test_fractional_sizing_price_too_high_without_fractional():
    from fractional import size_position
    tier = {"fractional_default": False}
    # $100 target, $500 stock — whole share fails, fractional disabled → 0
    r = size_position("BRK", target_dollars=100.0, price=500.0,
                       user={}, tier_cfg=tier)
    assert r["qty"] == 0
    assert "not enabled" in r["reason"].lower() or "too high" in r["reason"].lower()


def test_fractional_sizing_invalid_inputs():
    from fractional import size_position
    r = size_position("", target_dollars=100, price=50, user={}, tier_cfg={})
    assert r["qty"] == 0
    r = size_position("AAPL", target_dollars=0, price=50, user={}, tier_cfg={})
    assert r["qty"] == 0
    r = size_position("AAPL", target_dollars=100, price=0, user={}, tier_cfg={})
    assert r["qty"] == 0


# ========== PDT tracker ==========


def test_pdt_can_day_trade_cash_account():
    from pdt_tracker import can_day_trade
    tier = {"pdt_applies": False}
    ok, reason, remaining = can_day_trade(tier)
    assert ok is True


def test_pdt_can_day_trade_margin_plenty_remaining():
    from pdt_tracker import can_day_trade
    tier = {"pdt_applies": True, "_detected_day_trades_remaining": 3}
    ok, reason, remaining = can_day_trade(tier)
    assert ok is True
    assert remaining == 3


def test_pdt_can_day_trade_margin_buffer_hit():
    from pdt_tracker import can_day_trade
    tier = {"pdt_applies": True, "_detected_day_trades_remaining": 1}
    ok, reason, remaining = can_day_trade(tier, buffer=1)
    assert ok is False  # 1 remaining with buffer=1 → deny
    assert "PDT buffer low" in reason


def test_pdt_can_day_trade_unknown_allows_conservatively():
    from pdt_tracker import can_day_trade
    tier = {"pdt_applies": True, "_detected_day_trades_remaining": None}
    ok, _, _ = can_day_trade(tier)
    assert ok is True


def test_pdt_is_day_trade_same_date():
    from pdt_tracker import is_day_trade
    assert is_day_trade("2026-04-22T09:40:00-04:00",
                        "2026-04-22T15:30:00-04:00") is True


def test_pdt_is_day_trade_different_dates():
    from pdt_tracker import is_day_trade
    assert is_day_trade("2026-04-22T09:40:00-04:00",
                        "2026-04-23T09:45:00-04:00") is False


# ========== Settled funds ==========


def test_settled_funds_record_and_query(tmp_path):
    from settled_funds import record_sale, unsettled_cash
    user = {"_data_dir": str(tmp_path)}
    record_sale(user, "AAPL", proceeds=500.0, sold_on=date.today())
    record_sale(user, "TSLA", proceeds=300.0, sold_on=date.today())
    # Today → all proceeds still unsettled (T+1)
    assert unsettled_cash(user, as_of=date.today()) == 800.0


def test_settled_funds_drop_after_settlement_date(tmp_path):
    from settled_funds import record_sale, unsettled_cash
    user = {"_data_dir": str(tmp_path)}
    # Sale from 3 business days ago — should be settled already
    three_days_ago = date.today() - timedelta(days=5)
    record_sale(user, "AAPL", proceeds=500.0, sold_on=three_days_ago)
    # Today: sale has settled, unsettled should be 0
    assert unsettled_cash(user, as_of=date.today()) == 0.0


def test_settled_funds_can_deploy_margin_passthrough(tmp_path):
    from settled_funds import can_deploy
    user = {"_data_dir": str(tmp_path)}
    tier = {"settled_funds_required": False}
    ok, usable, reason = can_deploy(user, desired_spend=10000,
                                     total_cash=5000, tier_cfg=tier)
    assert ok is True  # margin: no T+1


def test_settled_funds_can_deploy_cash_blocked(tmp_path):
    from settled_funds import record_sale, can_deploy
    user = {"_data_dir": str(tmp_path)}
    # Simulate a recent sale — proceeds unsettled
    record_sale(user, "AAPL", proceeds=500.0, sold_on=date.today())
    tier = {"settled_funds_required": True}
    # Total cash includes the unsettled $500; settled should be much less
    ok, usable, reason = can_deploy(user, desired_spend=600,
                                     total_cash=500, tier_cfg=tier)
    assert ok is False
    assert "settled" in reason.lower()


def test_settled_funds_can_deploy_cash_allowed(tmp_path):
    from settled_funds import can_deploy
    user = {"_data_dir": str(tmp_path)}
    tier = {"settled_funds_required": True}
    # No pending sales, plenty of cash
    ok, usable, reason = can_deploy(user, desired_spend=500,
                                     total_cash=1000, tier_cfg=tier)
    assert ok is True
    # 95% buffer applied
    assert usable == 950.0


# ========== End-to-end: full calibration flow per tier ==========


@pytest.mark.parametrize("equity,multiplier,expected_tier", [
    (800,     1, "cash_micro"),
    (10000,   1, "cash_small"),
    (50000,   1, "cash_standard"),
    (10000,   2, "margin_small"),
    (50000,   4, "margin_standard"),
    (750000,  4, "margin_whale"),
])
def test_end_to_end_tier_detection(equity, multiplier, expected_tier):
    from portfolio_calibration import detect_tier, apply_user_overrides, calibration_summary
    tier = detect_tier(_account(multiplier=multiplier, equity=equity))
    assert tier is not None, f"tier detection failed for equity=${equity} multiplier={multiplier}"
    assert tier["name"] == expected_tier
    # Apply empty overrides — should round-trip cleanly
    merged = apply_user_overrides(tier, {})
    summary = calibration_summary(merged)
    assert summary["detected"] is True
    assert summary["tier_name"] == expected_tier
