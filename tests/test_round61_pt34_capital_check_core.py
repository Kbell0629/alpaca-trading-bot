"""Round-61 pt.34 — capital_check_core behavioural coverage.

Mirrors the pt.7 pattern (screener_core.py + scorecard_core.py
extracted from update_dashboard.py + update_scorecard.py): the
subprocess-driven entry point in capital_check.py is still
unmeasurable by pytest-cov, but the pure math now lives in
capital_check_core.py and gets full unit coverage here.

Surface tested:
  * `_LAST_RESORT_PRICE_PER_SHARE` constant pinned at $1000 (security
    invariant: prefer over-reservation to under-reservation when a
    last-trade fetch fails).
  * `compute_reserved_by_orders` — full pricing-ladder coverage:
    explicit limit/stop, notional, live last-trade, position avg
    entry fallback, conservative $1000 floor.
  * `position_avg_cost_map` — symbol-uppercase normalization,
    avg_entry_price coercion.
  * `compute_capital_metrics` — every output field, every warning
    threshold, sustainability score branches, recommendation strings,
    error-passthrough.
"""
from __future__ import annotations

import pytest

from capital_check_core import (
    _LAST_RESORT_PRICE_PER_SHARE,
    compute_reserved_by_orders,
    position_avg_cost_map,
    compute_capital_metrics,
)


# ============================================================================
# _LAST_RESORT_PRICE_PER_SHARE — security invariant
# ============================================================================

def test_last_resort_price_pinned_at_1000():
    """Security invariant from round-12 audit: the fallback floor must
    be 1000 (over-reservation) not 100 (under-reservation). Lowering
    this would let a last-trade fetch failure on a $200+ name silently
    authorise an overleveraged deploy."""
    assert _LAST_RESORT_PRICE_PER_SHARE == 1000.0


# ============================================================================
# compute_reserved_by_orders — pricing-ladder branches
# ============================================================================

def test_reserved_skips_sell_orders():
    """Only BUY orders consume free cash. SELL orders are exit
    orders — counting them inflates reserved$ and incorrectly blocks
    new entries."""
    orders = [
        {"side": "sell", "qty": "10", "limit_price": "100", "symbol": "AAPL"},
        {"side": "buy", "qty": "5", "limit_price": "100", "symbol": "AAPL"},
    ]
    assert compute_reserved_by_orders(orders, {}, lambda s: 0) == 500.0


def test_reserved_uses_explicit_limit_price_when_set():
    """Limit orders have a fixed maximum price — use it."""
    orders = [{"side": "buy", "qty": "10", "limit_price": "150",
                "symbol": "X"}]
    assert compute_reserved_by_orders(orders, {}, lambda s: 0) == 1500.0


def test_reserved_uses_explicit_stop_price_when_no_limit():
    """Stop orders have a stop price; same handling as limit."""
    orders = [{"side": "buy", "qty": "10", "stop_price": "120",
                "symbol": "X"}]
    assert compute_reserved_by_orders(orders, {}, lambda s: 0) == 1200.0


def test_reserved_uses_notional_when_no_limit_or_stop():
    """Some Alpaca orders carry only notional (whole-dollar amount).
    Use it directly without multiplying by qty."""
    orders = [{"side": "buy", "qty": "0", "notional": "500",
                "symbol": "X"}]
    assert compute_reserved_by_orders(orders, {}, lambda s: 0) == 500.0


def test_reserved_falls_back_to_live_quote_when_no_price():
    """Pricing ladder rung 1: fetch_last returns the live trade price."""
    orders = [{"side": "buy", "qty": "5", "symbol": "AAPL"}]
    fetch = lambda s: 100.0 if s == "AAPL" else 0
    assert compute_reserved_by_orders(orders, {}, fetch) == 500.0


def test_reserved_falls_back_to_avg_entry_when_no_quote():
    """Pricing ladder rung 2: fetch_last fails → use position's avg
    entry price for the same symbol."""
    orders = [{"side": "buy", "qty": "5", "symbol": "AAPL"}]
    avg = {"AAPL": 90.0}
    assert compute_reserved_by_orders(orders, avg, lambda s: 0) == 450.0


def test_reserved_falls_back_to_1000_floor_when_no_data():
    """Pricing ladder rung 3 (security floor): no live quote, no
    position. Use $1000/share to OVER-reserve. Any change here is a
    security regression — log into round-12 audit history."""
    orders = [{"side": "buy", "qty": "3", "symbol": "X"}]
    assert compute_reserved_by_orders(orders, {}, lambda s: 0) == 3000.0


def test_reserved_skips_orders_with_invalid_qty():
    """Defensive: a malformed qty (None / non-numeric) shouldn't
    crash the loop — just skip that order."""
    orders = [
        {"side": "buy", "qty": None, "limit_price": "100", "symbol": "X"},
        {"side": "buy", "qty": "not_a_number", "limit_price": "100",
          "symbol": "X"},
        {"side": "buy", "qty": "5", "limit_price": "100", "symbol": "X"},
    ]
    assert compute_reserved_by_orders(orders, {}, lambda s: 0) == 500.0


def test_reserved_handles_fetch_last_exception():
    """fetch_last may raise (network blip, SDK exception). Helper
    must catch and fall through to the next ladder rung."""
    def _raises(sym):
        raise RuntimeError("network down")
    orders = [{"side": "buy", "qty": "2", "symbol": "AAPL"}]
    avg = {"AAPL": 50.0}
    # Should fall through to avg entry (50 * 2 = 100), NOT crash.
    assert compute_reserved_by_orders(orders, avg, _raises) == 100.0


def test_reserved_handles_empty_orders_list():
    assert compute_reserved_by_orders([], {}, lambda s: 0) == 0.0


def test_reserved_handles_none_orders():
    assert compute_reserved_by_orders(None, {}, lambda s: 0) == 0.0


def test_reserved_uppercases_symbol_for_avg_lookup():
    """avg_cost map is keyed UPPER. fetch_last receives the symbol
    as it appears in the order (uppercased)."""
    orders = [{"side": "buy", "qty": "5", "symbol": "aapl"}]
    avg = {"AAPL": 100.0}
    assert compute_reserved_by_orders(orders, avg, lambda s: 0) == 500.0


def test_reserved_invalid_notional_falls_through():
    """Malformed notional shouldn't crash; fall through to ladder."""
    orders = [{"side": "buy", "qty": "2", "notional": "garbage",
                "symbol": "X"}]
    # Falls through to $1000 floor: 2 * 1000 = 2000
    assert compute_reserved_by_orders(orders, {}, lambda s: 0) == 2000.0


def test_reserved_summed_across_multiple_buy_orders():
    """Multiple BUY orders accumulate."""
    orders = [
        {"side": "buy", "qty": "5", "limit_price": "10", "symbol": "A"},
        {"side": "buy", "qty": "3", "limit_price": "20", "symbol": "B"},
        {"side": "buy", "qty": "2", "notional": "100", "symbol": "C"},
    ]
    # 50 + 60 + 100 = 210
    assert compute_reserved_by_orders(orders, {}, lambda s: 0) == 210.0


# ============================================================================
# position_avg_cost_map
# ============================================================================

def test_position_avg_cost_map_uppercases_symbols():
    positions = [
        {"symbol": "aapl", "avg_entry_price": "100"},
        {"symbol": "Msft", "avg_entry_price": "300"},
    ]
    m = position_avg_cost_map(positions)
    assert m == {"AAPL": 100.0, "MSFT": 300.0}


def test_position_avg_cost_map_handles_empty_input():
    assert position_avg_cost_map([]) == {}
    assert position_avg_cost_map(None) == {}


def test_position_avg_cost_map_defaults_missing_avg_entry_to_zero():
    positions = [{"symbol": "X"}]
    assert position_avg_cost_map(positions) == {"X": 0.0}


# ============================================================================
# compute_capital_metrics — error passthrough
# ============================================================================

def test_compute_metrics_passes_through_account_error():
    """If account API returned an error dict, the result is just
    {"error": ...} — no metrics computed."""
    result = compute_capital_metrics(
        account={"error": "HTTP 401: Unauthorized"},
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="2026-04-25T00:00:00",
    )
    assert result == {"error": "HTTP 401: Unauthorized"}


# ============================================================================
# compute_capital_metrics — happy path field shapes
# ============================================================================

def _account(portfolio_value=10000, cash=5000, buying_power=20000):
    return {
        "portfolio_value": str(portfolio_value),
        "cash": str(cash),
        "buying_power": str(buying_power),
        "equity": str(portfolio_value),
    }


def test_compute_metrics_returns_full_schema():
    """All fields the legacy capital_check.check_capital() returned
    must still be present so /api/data + dashboard rendering keep
    working."""
    result = compute_capital_metrics(
        account=_account(),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="2026-04-25T00:00:00",
    )
    expected_keys = {
        "timestamp", "portfolio_value", "cash", "buying_power",
        "total_position_value", "reserved_by_orders", "free_cash",
        "pct_invested", "pct_reserved", "pct_free", "num_positions",
        "max_positions", "additional_trades_possible", "can_trade",
        "sustainability_score", "warnings", "recommendation",
    }
    assert expected_keys.issubset(set(result.keys()))


def test_compute_metrics_total_position_value_sums_market_value():
    positions = [
        {"symbol": "A", "market_value": "1000", "avg_entry_price": "100"},
        {"symbol": "B", "market_value": "2500", "avg_entry_price": "50"},
    ]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000),
        positions=positions, orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["total_position_value"] == 3500.0
    assert result["pct_invested"] == 35.0


def test_compute_metrics_free_cash_subtracts_reserved():
    """free_cash = cash - reserved_by_orders. A pending BUY for 5x$100
    on $5000 cash yields free_cash=$4500."""
    orders = [{"side": "buy", "qty": "5", "limit_price": "100", "symbol": "X"}]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=5000),
        positions=[], orders=orders, guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["reserved_by_orders"] == 500.0
    assert result["free_cash"] == 4500.0


def test_compute_metrics_pct_zero_when_portfolio_value_zero():
    """Defensive: portfolio_value=0 must not cause /0. All pct fields
    become 0."""
    result = compute_capital_metrics(
        account=_account(portfolio_value=0, cash=0),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["pct_invested"] == 0
    assert result["pct_reserved"] == 0
    assert result["pct_free"] == 0


def test_compute_metrics_uses_default_max_positions_when_missing():
    result = compute_capital_metrics(
        account=_account(),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["max_positions"] == 5  # default


def test_compute_metrics_respects_guardrails_max_positions():
    result = compute_capital_metrics(
        account=_account(),
        positions=[], orders=[], guardrails={"max_positions": 8},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["max_positions"] == 8


def test_compute_metrics_can_trade_true_when_room():
    """can_trade requires free_cash >= min_trade_size AND fewer
    positions than max_positions."""
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=10000),
        positions=[], orders=[], guardrails={"max_positions": 5},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["can_trade"] is True


def test_compute_metrics_can_trade_false_when_low_cash():
    """min_trade_size = 3% of portfolio. cash<3% → can_trade=False."""
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=100),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["can_trade"] is False


def test_compute_metrics_can_trade_false_at_max_positions():
    """At max_positions, can_trade must be False even with cash."""
    positions = [{"symbol": f"S{i}", "market_value": "100",
                   "avg_entry_price": "10"} for i in range(5)]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=10000),
        positions=positions, orders=[],
        guardrails={"max_positions": 5},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["can_trade"] is False


def test_compute_metrics_additional_trades_capped_by_max_positions():
    """Even with $10k free cash and a 10% per-position cap (=$1000),
    you can't deploy 10 trades if max_positions is 5 and you already
    hold 3."""
    positions = [{"symbol": f"S{i}", "market_value": "0",
                   "avg_entry_price": "10"} for i in range(3)]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=10000),
        positions=positions, orders=[],
        guardrails={"max_positions": 5, "max_position_pct": 0.10},
        fetch_last=lambda s: 0, now_iso="t",
    )
    # avg_position = 1000; free_cash/avg = 10; capped by 5-3 = 2
    assert result["additional_trades_possible"] == 2


def test_compute_metrics_additional_trades_zero_when_no_pct():
    """max_position_pct=0 means avg_position_size=0; division-by-zero
    must yield 0, not crash."""
    result = compute_capital_metrics(
        account=_account(),
        positions=[], orders=[],
        guardrails={"max_position_pct": 0},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["additional_trades_possible"] == 0


# ============================================================================
# Warnings — every threshold branch
# ============================================================================

def test_warning_high_exposure_above_80pct():
    positions = [{"symbol": "X", "market_value": "8500",
                   "avg_entry_price": "100"}]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=1500),
        positions=positions, orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert any("HIGH EXPOSURE" in w for w in result["warnings"])


def test_warning_moderate_exposure_between_60_and_80():
    positions = [{"symbol": "X", "market_value": "7000",
                   "avg_entry_price": "100"}]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=3000),
        positions=positions, orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    msgs = [w for w in result["warnings"] if "MODERATE EXPOSURE" in w]
    assert msgs
    # Mutually exclusive with HIGH EXPOSURE
    assert not any("HIGH EXPOSURE" in w for w in result["warnings"])


def test_warning_low_capital_when_cash_below_min_trade():
    """Cash below 3% min_trade_size triggers LOW CAPITAL warning."""
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=100),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert any("LOW CAPITAL" in w for w in result["warnings"])


def test_warning_max_positions_reached():
    positions = [{"symbol": f"S{i}", "market_value": "0",
                   "avg_entry_price": "10"} for i in range(5)]
    result = compute_capital_metrics(
        account=_account(),
        positions=positions, orders=[],
        guardrails={"max_positions": 5},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert any("MAX POSITIONS REACHED" in w for w in result["warnings"])


def test_warning_heavy_order_book_when_reserved_above_half_cash():
    """Reserved > 50% of cash triggers HEAVY ORDER BOOK warning."""
    orders = [{"side": "buy", "qty": "1", "limit_price": "3000",
                "symbol": "X"}]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=5000),
        positions=[], orders=orders, guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert any("HEAVY ORDER BOOK" in w for w in result["warnings"])


def test_warning_no_warnings_when_healthy():
    """Lots of cash, low exposure, room for trades → empty warnings."""
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=8000),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["warnings"] == []


# ============================================================================
# Sustainability score
# ============================================================================

def test_sustainability_100_when_healthy():
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=8000),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["sustainability_score"] == 100


def test_sustainability_decreases_above_50pct_invested():
    """For each percentage point above 50% invested, score drops by 1."""
    positions = [{"symbol": "X", "market_value": "7000",
                   "avg_entry_price": "100"}]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=3000),
        positions=positions, orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    # 70% invested -> 100 - (70-50) = 80
    assert result["sustainability_score"] == 80


def test_sustainability_drops_20_at_max_positions():
    positions = [{"symbol": f"S{i}", "market_value": "0",
                   "avg_entry_price": "10"} for i in range(5)]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=10000),
        positions=positions, orders=[],
        guardrails={"max_positions": 5},
        fetch_last=lambda s: 0, now_iso="t",
    )
    # 0% invested but at max: 100 - 20 = 80
    assert result["sustainability_score"] == 80


def test_sustainability_drops_30_when_low_cash():
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=100),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    # cash < min_trade_size: 100 - 30 = 70
    assert result["sustainability_score"] == 70


def test_sustainability_floor_at_zero():
    """Score can't go negative even with all penalties + extreme
    over-exposure."""
    positions = [{"symbol": "X", "market_value": "20000",
                   "avg_entry_price": "100"}]
    extra = [{"symbol": f"S{i}", "market_value": "0",
               "avg_entry_price": "10"} for i in range(5)]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=10),
        positions=positions + extra, orders=[],
        guardrails={"max_positions": 5},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert result["sustainability_score"] >= 0
    assert result["sustainability_score"] <= 100


# ============================================================================
# Recommendation strings
# ============================================================================

def test_recommendation_healthy_above_80():
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=8000),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert "Healthy" in result["recommendation"]
    assert "more trades possible" in result["recommendation"]


def test_recommendation_caution_between_50_and_80():
    """50 ≤ score < 80 → Caution string."""
    positions = [{"symbol": "X", "market_value": "7000",
                   "avg_entry_price": "100"}]
    # 70% invested → score 80; need slightly more to drop below
    positions = [{"symbol": "X", "market_value": "7500",
                   "avg_entry_price": "100"}]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=2500),
        positions=positions, orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    # 75% → score 75 → Caution
    assert result["sustainability_score"] == 75
    assert "Caution" in result["recommendation"]


def test_recommendation_critical_below_50():
    positions = [{"symbol": "X", "market_value": "9500",
                   "avg_entry_price": "100"}]
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=10),
        positions=positions, orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert "Critical" in result["recommendation"]


def test_recommendation_includes_free_cash_dollars():
    """All three recommendation tiers must include the free-cash $."""
    result = compute_capital_metrics(
        account=_account(portfolio_value=10000, cash=4000),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0, now_iso="t",
    )
    assert "$4,000.00" in result["recommendation"]


# ============================================================================
# now_iso passthrough
# ============================================================================

def test_compute_metrics_uses_provided_now_iso():
    """timestamp must match the caller's now_iso. This decouples the
    function from et_time so tests don't drift on clock skew."""
    result = compute_capital_metrics(
        account=_account(),
        positions=[], orders=[], guardrails={},
        fetch_last=lambda s: 0,
        now_iso="2026-04-25T12:34:56-04:00",
    )
    assert result["timestamp"] == "2026-04-25T12:34:56-04:00"


# ============================================================================
# Backwards-compat: capital_check.py still exposes the helper
# ============================================================================

def test_legacy_capital_check_compute_reserved_alias_still_works():
    """capital_check.py kept the underscore-prefix alias for any
    caller that imported it directly. Pin so a refactor doesn't
    break that contract silently."""
    import capital_check
    assert capital_check._compute_reserved_by_orders is compute_reserved_by_orders
    assert capital_check._LAST_RESORT_PRICE_PER_SHARE == 1000.0
    # check_capital still callable (signature unchanged)
    assert callable(capital_check.check_capital)
