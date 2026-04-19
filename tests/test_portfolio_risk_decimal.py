"""
Tests for phase-3 float→Decimal migration in portfolio_risk.py.

Phase-3 scope (per docs/DECIMAL_MIGRATION_PLAN.md): money-weighted math
in portfolio_beta + beta_adjusted_exposure. Ratios (correlation,
drawdown %, drawdown_size_multiplier) stay as float — they're
proportional, not money.
"""
from __future__ import annotations

import pytest

import portfolio_risk as pr


def _pos(symbol, market_value):
    return {"symbol": symbol, "market_value": market_value}


# ---------- portfolio_beta ----------


def test_portfolio_beta_empty_returns_zero():
    assert pr.portfolio_beta([]) == 0.0


def test_portfolio_beta_single_position_returns_that_beta():
    # SPY beta is not in DEFAULT_BETA_MAP by default — unknown = 1.0.
    assert pr.portfolio_beta([_pos("SPY", 10000)]) == pytest.approx(1.0)


def test_portfolio_beta_weighted_average_is_money_weighted():
    # $10k in TSLA (beta 2.0), $10k in JNJ (beta 0.6).
    # Equal money → avg of 2.0 and 0.6 = 1.3.
    positions = [_pos("TSLA", 10000), _pos("JNJ", 10000)]
    assert pr.portfolio_beta(positions) == pytest.approx(1.3, abs=0.001)


def test_portfolio_beta_heavy_on_high_beta_pulls_upward():
    # $90k TSLA (beta 2.0), $10k JNJ (beta 0.6).
    # Expected: (90k*2.0 + 10k*0.6) / 100k = 1.86
    positions = [_pos("TSLA", 90000), _pos("JNJ", 10000)]
    assert pr.portfolio_beta(positions) == pytest.approx(1.86, abs=0.001)


def test_portfolio_beta_shorts_counted_by_absolute_value():
    # Short $10k TSLA = negative market_value — still contributes to
    # weighted beta via |market_value|.
    positions = [_pos("TSLA", -10000), _pos("JNJ", 10000)]
    assert pr.portfolio_beta(positions) == pytest.approx(1.3, abs=0.001)


def test_portfolio_beta_returns_float_not_decimal():
    positions = [_pos("TSLA", 10000), _pos("JNJ", 10000)]
    assert isinstance(pr.portfolio_beta(positions), float)


def test_portfolio_beta_unknown_symbol_defaults_to_one():
    positions = [_pos("NOT_IN_MAP", 10000)]
    assert pr.portfolio_beta(positions) == pytest.approx(1.0)


# ---------- Drift resistance ----------


def test_portfolio_beta_no_drift_across_100_positions():
    """100 positions with IEEE-754-hostile market values — weighted
    beta should be exactly 1.5 (mean of 1.0 and 2.0) when half are
    beta-1.0 unknowns and half are TSLA."""
    positions = []
    for i in range(50):
        positions.append(_pos("TSLA", 123.45))   # beta 2.0
        positions.append(_pos("UNKNOWN_SYM", 123.45))  # beta 1.0
    # Expected: (50*123.45*2.0 + 50*123.45*1.0) / (100*123.45) = 1.5
    result = pr.portfolio_beta(positions)
    assert result == pytest.approx(1.5, abs=1e-10)


# ---------- beta_adjusted_exposure ----------


def test_beta_adjusted_exposure_zero_portfolio_returns_unknown():
    r = pr.beta_adjusted_exposure([_pos("TSLA", 10000)], 0)
    assert r["regime"] == "unknown"
    assert r["beta_weighted_pct"] == 0


def test_beta_adjusted_exposure_classic_leveraged_etf_example():
    """$50k in 3x ETF with portfolio value $50k → 150% beta-weighted.
    Uses a known beta_map.
    """
    positions = [_pos("TQQQ_LIKE", 50000)]
    beta_map = {"TQQQ_LIKE": 3.0}
    r = pr.beta_adjusted_exposure(positions, 50000, beta_map=beta_map)
    assert r["invested_pct"] == pytest.approx(100.0, abs=0.1)
    assert r["portfolio_beta"] == pytest.approx(3.0, abs=0.01)
    assert r["beta_weighted_pct"] == pytest.approx(300.0, abs=0.1)
    assert r["regime"] == "extreme"
    assert r["block_all"] is True


def test_beta_adjusted_exposure_low_regime():
    # 30% invested at beta 1.0 → 30% beta-weighted. "low".
    positions = [_pos("UNKNOWN_SYM", 30000)]
    r = pr.beta_adjusted_exposure(positions, 100000)
    assert r["regime"] == "low"
    assert r["block_high_beta"] is False
    assert r["block_all"] is False


def test_beta_adjusted_exposure_moderate_regime():
    # 60% invested at beta 1.2 (AAPL) → 72% beta-weighted. "moderate".
    positions = [_pos("AAPL", 60000)]
    r = pr.beta_adjusted_exposure(positions, 100000)
    assert r["regime"] == "moderate"
    assert r["block_high_beta"] is False


def test_beta_adjusted_exposure_high_regime_blocks_high_beta():
    # $80k TSLA (beta 2.0) / $100k pv → 160% beta-weighted → "extreme".
    positions = [_pos("TSLA", 80000)]
    r = pr.beta_adjusted_exposure(positions, 100000)
    # 80% * 2.0 = 160% → extreme
    assert r["regime"] == "extreme"
    assert r["block_high_beta"] is True
    assert r["block_all"] is True


def test_beta_adjusted_exposure_return_types_are_float_not_decimal():
    positions = [_pos("TSLA", 10000)]
    r = pr.beta_adjusted_exposure(positions, 100000)
    for k in ("invested_pct", "portfolio_beta", "beta_weighted_pct"):
        assert isinstance(r[k], float), f"{k} leaked {type(r[k])}"


# ---------- Drift proof for beta_adjusted_exposure ----------


def test_beta_adjusted_exposure_no_drift_on_awkward_market_values():
    """Market values with IEEE-754-hostile decimals (e.g. 10000/3)
    summed across many positions. With Decimal, beta_weighted_pct is
    stable; with pure float, it drifts in the last decimal."""
    # 300 tiny lots adding up to exactly $100,000 invested (at 1/3 each).
    positions = [_pos("UNKNOWN_SYM", 333.33) for _ in range(300)]
    # 300 * 333.33 = 99999.00 invested; pv 100000.
    r = pr.beta_adjusted_exposure(positions, 100000)
    # invested_pct = 99999 / 100000 * 100 = 99.999
    # After round(,1) → 100.0
    assert r["invested_pct"] == 100.0
    # beta = 1.0 (unknown), so beta_weighted_pct = invested_pct (at 100.0
    # rounded — actually 99.999 * 1.0 = 99.999).
    assert r["beta_weighted_pct"] == 100.0
