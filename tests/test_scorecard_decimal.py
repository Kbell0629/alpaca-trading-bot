"""
Tests for the phase-2 float→Decimal migration in update_scorecard.py.

Goals:

  1. Return-type invariants — no Decimal leaks to the caller. Every money
     field in the scorecard dict is still a plain float; every ratio/pct
     is still a float.
  2. Behaviour parity — classic fixtures produce the hand-computed values.
  3. Drift resistance — accumulating per-strategy pnl across a long chain
     of trades with IEEE-754-hostile prices stays exact to the cent.
  4. Specific places float used to bite: profit_factor, largest_win/loss,
     strategy_breakdown.pnl, peak_value, max_drawdown_pct.
  5. Edge cases: empty journal, single-trade, all-losing, all-winning,
     no snapshots, no positions.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import update_scorecard as us


# ---------- Helpers ----------


def _closed_trade(pnl, pnl_pct=None, strategy="trailing_stop"):
    """Build a minimal closed trade for scorecard tests."""
    return {
        "symbol": "SPY",
        "side": "sell",
        "qty": 10,
        "price": 400,
        "strategy": strategy,
        "status": "closed",
        "pnl": pnl,
        "pnl_pct": pnl_pct if pnl_pct is not None else (pnl / 4000 * 100),
    }


def _account(pv, cash=None):
    return {"portfolio_value": pv, "cash": cash if cash is not None else pv * 0.3}


def _default_scorecard(starting=100000, peak=None):
    sc = {"starting_capital": starting, "start_date": "2024-01-01"}
    if peak is not None:
        sc["peak_value"] = peak
    return sc


# ---------- Return-type invariants ----------


def test_all_money_fields_are_float_in_output():
    journal = {"trades": [_closed_trade(100)], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(101000), [])
    for k in ("current_value", "peak_value", "largest_win", "largest_loss",
              "profit_factor", "max_drawdown_pct", "total_return_pct"):
        assert isinstance(r[k], float), f"{k} is {type(r[k])}, expected float"


def test_strategy_breakdown_pnl_is_float_not_decimal():
    journal = {"trades": [
        _closed_trade(100, strategy="trailing_stop"),
        _closed_trade(-50, strategy="wheel"),
    ], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100050), [])
    for strat_name, row in r["strategy_breakdown"].items():
        assert isinstance(row["pnl"], float), (
            f"{strat_name} pnl leaked {type(row['pnl'])}"
        )
        assert isinstance(row["trades"], int)
        assert isinstance(row["wins"], int)


# ---------- Behaviour parity ----------


def test_empty_journal_zero_metrics():
    r = us.calculate_metrics({"trades": [], "daily_snapshots": []},
                              _default_scorecard(), _account(100000), [])
    assert r["total_trades"] == 0
    assert r["closed_trades"] == 0
    assert r["profit_factor"] == 0
    assert r["largest_win"] == 0.0
    assert r["largest_loss"] == 0.0
    assert r["current_value"] == 100000.00
    assert r["total_return_pct"] == 0.0


def test_single_winning_trade():
    journal = {"trades": [_closed_trade(150.25)], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100150.25), [])
    assert r["closed_trades"] == 1
    assert r["winning_trades"] == 1
    assert r["win_rate_pct"] == 100.0
    assert r["largest_win"] == 150.25
    assert r["largest_loss"] == 0.0
    assert r["profit_factor"] == 150.25   # no losses → total_wins as the factor
    assert r["current_value"] == 100150.25


def test_single_losing_trade():
    journal = {"trades": [_closed_trade(-75.50)], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(99924.50), [])
    assert r["winning_trades"] == 0
    assert r["largest_win"] == 0.0
    assert r["largest_loss"] == -75.50
    assert r["profit_factor"] == 0   # no wins


def test_profit_factor_classic_case():
    journal = {"trades": [
        _closed_trade(100),
        _closed_trade(200),
        _closed_trade(-50),
    ], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100250), [])
    # total_wins = 300, total_losses = 50 -> profit_factor = 6
    assert r["profit_factor"] == 6.0


# ---------- IEEE-754 trip wires ----------


def test_pnl_accumulation_exact_to_the_cent_across_irrational_prices():
    """20 trades each producing $17.10 pnl: 20 * 17.10 = $342.00 exact.
    Under pure float, the final sum drifts by ~1e-13; Decimal makes it
    exact. The cent-quantized output makes this test insensitive to the
    drift difference (both would round to 342.00), so instead we assert
    the aggregate current_value matches hand math."""
    trades = [_closed_trade(17.10) for _ in range(20)]
    journal = {"trades": trades, "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(100000),
                              _account(100342.00), [])
    # per-strategy (default: trailing_stop)
    assert r["strategy_breakdown"]["trailing_stop"]["pnl"] == 342.00
    assert r["strategy_breakdown"]["trailing_stop"]["trades"] == 20


def test_strategy_breakdown_accumulates_per_strategy_exactly():
    trades = [
        _closed_trade(12.34, strategy="trailing_stop"),
        _closed_trade(12.34, strategy="trailing_stop"),
        _closed_trade(-5.67, strategy="wheel"),
        _closed_trade(8.90, strategy="breakout"),
    ]
    journal = {"trades": trades, "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100027.91), [])
    b = r["strategy_breakdown"]
    assert b["trailing_stop"]["pnl"] == 24.68
    assert b["trailing_stop"]["trades"] == 2
    assert b["trailing_stop"]["wins"] == 2
    assert b["wheel"]["pnl"] == -5.67
    assert b["wheel"]["trades"] == 1
    assert b["wheel"]["wins"] == 0
    assert b["breakout"]["pnl"] == 8.90


def test_pead_bucket_counted_not_silently_dropped():
    """Regression guard — round-10 bug where pead trades fell off the
    breakdown because the bucket wasn't initialised."""
    journal = {"trades": [_closed_trade(55.55, strategy="pead")],
               "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100055.55), [])
    assert r["strategy_breakdown"]["pead"]["pnl"] == 55.55
    assert r["strategy_breakdown"]["pead"]["trades"] == 1


def test_strategy_name_normalisation():
    """'Copy Trading', 'trailing-stop', 'TRAILING_STOP' should all bucket
    to the canonical lowercase-underscore form."""
    journal = {"trades": [
        {**_closed_trade(10), "strategy": "Copy Trading"},
        {**_closed_trade(20), "strategy": "trailing-stop"},
        {**_closed_trade(30), "strategy": "TRAILING_STOP"},
    ], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100060), [])
    assert r["strategy_breakdown"]["copy_trading"]["trades"] == 1
    assert r["strategy_breakdown"]["trailing_stop"]["trades"] == 2
    assert r["strategy_breakdown"]["trailing_stop"]["pnl"] == 50.0


# ---------- Peak / drawdown ----------


def test_peak_value_is_max_of_snapshots_current_and_prior_peak():
    snapshots = [
        {"portfolio_value": 100000},
        {"portfolio_value": 105000},  # peak
        {"portfolio_value": 98000},
    ]
    journal = {"trades": [], "daily_snapshots": snapshots}
    r = us.calculate_metrics(journal, _default_scorecard(100000, peak=102000),
                              _account(99500), [])
    # peak = max(105000, 99500, 102000) = 105000
    assert r["peak_value"] == 105000.00


def test_max_drawdown_captures_worst_snapshot_drop():
    snapshots = [
        {"portfolio_value": 100000},
        {"portfolio_value": 110000},   # peak
        {"portfolio_value": 88000},    # -20% drawdown from peak
    ]
    journal = {"trades": [], "daily_snapshots": snapshots}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(88000), [])
    # Expected dd: (110000 - 88000) / 110000 * 100 = 20.00
    assert r["max_drawdown_pct"] == 20.00


def test_drawdown_current_value_against_peak():
    """Current portfolio value below an all-time peak produces drawdown too."""
    snapshots = [{"portfolio_value": 100000}, {"portfolio_value": 120000}]
    journal = {"trades": [], "daily_snapshots": snapshots}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100000), [])
    # peak is 120000, current 100000 → dd = 16.67
    assert r["max_drawdown_pct"] == pytest.approx(16.67, abs=0.01)


# ---------- Total return ----------


def test_total_return_pct_exact_from_current_vs_starting():
    journal = {"trades": [], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(100000),
                              _account(112345.67), [])
    # (112345.67 - 100000) / 100000 * 100 = 12.34567
    assert r["total_return_pct"] == pytest.approx(12.35, abs=0.01)


def test_zero_starting_capital_does_not_divide_by_zero():
    journal = {"trades": [], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(0), _account(100000), [])
    assert r["total_return_pct"] == 0


# ---------- Interaction with live account values ----------


def test_portfolio_value_falls_back_to_starting_when_account_zero():
    """If Alpaca returns 0 portfolio_value (flaky API), fall back."""
    journal = {"trades": [], "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(100000),
                              _account(0), [])
    assert r["current_value"] == 100000.00


# ---------- Drift proof of concept ----------


def test_long_chain_strategy_breakdown_no_compounding_drift():
    """500 trades of $0.10 pnl each in one bucket → $50.00 exact. Under
    pure float this drifts by ~1e-12; Decimal makes it exact."""
    trades = [_closed_trade(0.10) for _ in range(500)]
    journal = {"trades": trades, "daily_snapshots": []}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100050), [])
    assert r["strategy_breakdown"]["trailing_stop"]["pnl"] == 50.00


def test_sharpe_ratio_unchanged_behaviour_float_ok_on_stats():
    """Statistical ratios (Sharpe/Sortino) stay as float per the migration
    plan. We're not asserting a specific value — just that the field exists
    and is a float, not a Decimal leak."""
    snapshots = [{"portfolio_value": 100000 + i * 100} for i in range(10)]
    journal = {"trades": [], "daily_snapshots": snapshots}
    r = us.calculate_metrics(journal, _default_scorecard(), _account(100900), [])
    assert isinstance(r["sharpe_ratio"], (int, float))
    assert isinstance(r["sortino_ratio"], (int, float))


# ---------- Helper sanity ----------


def test_dec_rejects_none_gracefully():
    assert us._dec(None) == Decimal("0")


def test_dec_avoids_float_double_conversion():
    assert us._dec(0.1) == Decimal("0.1")   # via str(), not Decimal(float)


def test_to_cents_float_banker_rounding():
    # 0.005 rounds to even: 0.00 (0 is even)
    assert us._to_cents_float(Decimal("0.005")) == 0.00
    # 0.015 rounds to even: 0.02 (2 is even)
    assert us._to_cents_float(Decimal("0.015")) == 0.02
    # 0.125 rounds to even: 0.12 (last digit 2 is even)
    assert us._to_cents_float(Decimal("0.125")) == 0.12
