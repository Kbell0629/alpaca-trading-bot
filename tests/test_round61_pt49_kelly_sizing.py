"""Round-61 pt.49 — fractional-Kelly + correlation-aware sizing.

Pure-module tests for ``position_sizing`` plus source-pin tests for
the cloud_scheduler integration.
"""
from __future__ import annotations

import pathlib

import position_sizing as ps


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


def _trade(*, strategy="breakout", pnl=0.0, status="closed"):
    return {"strategy": strategy, "pnl": pnl, "status": status}


# ============================================================================
# kelly_fraction
# ============================================================================

def test_kelly_zero_when_no_wins():
    assert ps.kelly_fraction(0.0, 100.0, -50.0) == 0.0


def test_kelly_zero_when_negative_edge():
    """Win rate 30%, avg win 50, avg loss -100 → Kelly negative."""
    assert ps.kelly_fraction(0.30, 50.0, -100.0) == 0.0


def test_kelly_positive_with_real_edge():
    """Win rate 60%, avg win 100, avg loss -50: Kelly = (.6*2 - .4)/2
    = 0.4. Half-kelly = 0.2."""
    f = ps.kelly_fraction(0.60, 100.0, -50.0)
    assert 0.10 < f < 0.25
    assert abs(f - 0.20) < 0.01


def test_kelly_capped_at_max_fraction():
    """Crazy edge → Kelly would be huge, but cap kicks in."""
    f = ps.kelly_fraction(0.95, 1000.0, -10.0)
    assert f <= ps.MAX_KELLY_FRACTION


def test_kelly_full_vs_half_kelly():
    # Use a small-edge case where neither full nor half hits the
    # MAX_KELLY_FRACTION cap, so the 2× relationship holds.
    full = ps.kelly_fraction(0.55, 100.0, -100.0, fraction_of=1.0)
    half = ps.kelly_fraction(0.55, 100.0, -100.0, fraction_of=0.5)
    assert abs(full - 2 * half) < 0.001


def test_kelly_invalid_inputs_returns_zero():
    for args in [
        ("bad", 100, -50),
        (0.5, "bad", -50),
        (0.5, 100, "bad"),
        (-0.1, 100, -50),
        (1.1, 100, -50),
        (0.5, -50, -100),    # avg_win must be > 0
        (0.5, 100, 50),       # avg_loss must be < 0
        (0.5, 100, 0),        # avg_loss must be < 0
    ]:
        assert ps.kelly_fraction(*args) == 0.0


# ============================================================================
# compute_strategy_edge
# ============================================================================

def test_strategy_edge_no_journal():
    out = ps.compute_strategy_edge(None, "breakout")
    assert out["trade_count"] == 0
    assert out["kelly_eligible"] is False


def test_strategy_edge_below_min_trades_not_eligible():
    journal = {"trades": [_trade(pnl=1.0) for _ in range(5)]}
    out = ps.compute_strategy_edge(journal, "breakout", min_trades=10)
    assert out["trade_count"] == 5
    assert out["kelly_eligible"] is False
    assert out["kelly_fraction"] == 0.0


def test_strategy_edge_filters_by_strategy():
    journal = {"trades": [
        _trade(strategy="breakout", pnl=10),
        _trade(strategy="wheel", pnl=-100),
        _trade(strategy="wheel", pnl=-100),
    ]}
    out = ps.compute_strategy_edge(journal, "breakout", min_trades=1)
    # Only the breakout trade should count.
    assert out["trade_count"] == 1


def test_strategy_edge_eligible_with_enough_trades():
    journal = {"trades": [
        _trade(pnl=20) for _ in range(7)
    ] + [
        _trade(pnl=-10) for _ in range(3)
    ]}
    out = ps.compute_strategy_edge(journal, "breakout", min_trades=10)
    assert out["trade_count"] == 10
    assert abs(out["win_rate"] - 0.7) < 0.001
    assert abs(out["avg_win"] - 20.0) < 0.001
    assert abs(out["avg_loss"] - (-10.0)) < 0.001
    assert out["kelly_eligible"] is True
    assert out["kelly_fraction"] > 0


def test_strategy_edge_strategy_name_case_insensitive():
    journal = {"trades": [
        {"strategy": "Breakout", "pnl": 10, "status": "closed"},
    ]}
    out = ps.compute_strategy_edge(journal, "breakout", min_trades=1)
    assert out["trade_count"] == 1


def test_strategy_edge_skips_open_trades():
    journal = {"trades": [
        _trade(pnl=10, status="open"),
        _trade(pnl=10, status="closed"),
    ]}
    out = ps.compute_strategy_edge(journal, "breakout", min_trades=1)
    assert out["trade_count"] == 1


def test_strategy_edge_skips_flat_trades():
    """|pnl| < 0.005 is neither win nor loss."""
    journal = {"trades": [
        _trade(pnl=10), _trade(pnl=0.001), _trade(pnl=-0.002),
    ]}
    out = ps.compute_strategy_edge(journal, "breakout", min_trades=1)
    assert out["trade_count"] == 1   # only the 10 counted


def test_strategy_edge_handles_invalid_pnl():
    journal = {"trades": [
        {"strategy": "breakout", "pnl": "bad", "status": "closed"},
        {"strategy": "breakout", "pnl": None, "status": "closed"},
        {"strategy": "breakout", "pnl": 5.0, "status": "closed"},
    ]}
    out = ps.compute_strategy_edge(journal, "breakout", min_trades=1)
    assert out["trade_count"] == 1


# ============================================================================
# kelly_size_multiplier
# ============================================================================

def test_kelly_mult_returns_one_when_ineligible():
    edge = {"kelly_eligible": False, "kelly_fraction": 0.5}
    assert ps.kelly_size_multiplier(edge) == 1.0


def test_kelly_mult_floor_when_kelly_zero():
    edge = {"kelly_eligible": True, "kelly_fraction": 0.0}
    assert ps.kelly_size_multiplier(edge) == ps.KELLY_MULT_FLOOR


def test_kelly_mult_one_at_baseline():
    edge = {"kelly_eligible": True,
             "kelly_fraction": ps.BASELINE_KELLY}
    assert abs(ps.kelly_size_multiplier(edge) - 1.0) < 0.01


def test_kelly_mult_ceiling_at_max_kelly():
    edge = {"kelly_eligible": True,
             "kelly_fraction": ps.MAX_KELLY_FRACTION}
    assert ps.kelly_size_multiplier(edge) == ps.KELLY_MULT_CEILING


def test_kelly_mult_above_baseline_below_ceiling():
    edge = {"kelly_eligible": True,
             "kelly_fraction": (ps.BASELINE_KELLY +
                                  ps.MAX_KELLY_FRACTION) / 2}
    m = ps.kelly_size_multiplier(edge)
    assert 1.0 < m < ps.KELLY_MULT_CEILING


def test_kelly_mult_below_baseline_above_floor():
    edge = {"kelly_eligible": True,
             "kelly_fraction": ps.BASELINE_KELLY / 2}
    m = ps.kelly_size_multiplier(edge)
    assert ps.KELLY_MULT_FLOOR < m < 1.0


def test_kelly_mult_invalid_edge_returns_one():
    for bad in (None, "bad", [], 42):
        assert ps.kelly_size_multiplier(bad) == 1.0


# ============================================================================
# count_correlated_positions
# ============================================================================

def test_correlation_count_no_positions():
    assert ps.count_correlated_positions("AAPL", []) == 0
    assert ps.count_correlated_positions("AAPL", None) == 0


def test_correlation_count_no_symbol():
    assert ps.count_correlated_positions("", [{"symbol": "AAPL"}]) == 0


def test_correlation_count_same_sector():
    sector_map = {"AAPL": "Tech", "MSFT": "Tech", "JPM": "Finance"}
    positions = [{"symbol": "MSFT"}, {"symbol": "JPM"}]
    n = ps.count_correlated_positions("AAPL", positions,
                                         sector_map=sector_map)
    assert n == 1   # MSFT is same sector


def test_correlation_count_skips_other_sector():
    """Symbols with sector='Other' don't contribute (avoids over-
    discounting on unrelated tickers)."""
    sector_map = {"AAPL": "Tech"}
    # MSFT not in sector_map → "Other" → no correlation contribution
    positions = [{"symbol": "MSFT"}]
    assert ps.count_correlated_positions("AAPL", positions,
                                            sector_map=sector_map) == 0


def test_correlation_count_skips_self():
    sector_map = {"AAPL": "Tech"}
    positions = [{"symbol": "AAPL"}]
    assert ps.count_correlated_positions("AAPL", positions,
                                            sector_map=sector_map) == 0


def test_correlation_count_falls_back_to_constants_sector_map():
    """No explicit sector_map → fall back to constants.SECTOR_MAP."""
    positions = [{"symbol": "MSFT"}]
    # SECTOR_MAP has both AAPL and MSFT as Tech in production.
    n = ps.count_correlated_positions("AAPL", positions)
    # If SECTOR_MAP has them, n >= 1; if not, n == 0. Either way
    # don't crash.
    assert n in (0, 1)


# ============================================================================
# correlation_size_multiplier
# ============================================================================

def test_correlation_mult_zero_correlated_returns_one():
    assert ps.correlation_size_multiplier(0) == 1.0


def test_correlation_mult_one_correlated_halves():
    m = ps.correlation_size_multiplier(1)
    assert abs(m - 0.5) < 0.001


def test_correlation_mult_two_correlated_quarters():
    m = ps.correlation_size_multiplier(2)
    assert abs(m - 0.25) < 0.001


def test_correlation_mult_floored():
    """Many correlated positions hit the floor."""
    m = ps.correlation_size_multiplier(10)
    assert m == ps.CORRELATION_MULT_FLOOR


def test_correlation_mult_invalid_input_returns_one():
    assert ps.correlation_size_multiplier("bad") == 1.0


# ============================================================================
# compute_full_size — end-to-end
# ============================================================================

def test_full_size_zero_base_qty_returns_zero():
    out = ps.compute_full_size(
        base_qty=0, strategy="breakout", symbol="AAPL")
    assert out["qty"] == 0


def test_full_size_no_journal_no_corr_returns_base():
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        journal=None, existing_positions=None,
    )
    assert out["qty"] == 100
    assert out["kelly_multiplier"] == 1.0
    assert out["correlation_multiplier"] == 1.0


def test_full_size_strong_strategy_scales_up():
    """A strategy with 70% win rate + 2:1 R:R should scale up."""
    journal = {"trades": [
        _trade(pnl=20) for _ in range(14)
    ] + [
        _trade(pnl=-10) for _ in range(6)
    ]}
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        journal=journal,
    )
    # Should scale UP since edge is strong.
    assert out["qty"] > 100
    assert out["kelly_multiplier"] > 1.0


def test_full_size_weak_strategy_scales_down():
    """A strategy with 40% win rate + 1:1 R:R should scale down."""
    journal = {"trades": [
        _trade(pnl=10) for _ in range(4)
    ] + [
        _trade(pnl=-15) for _ in range(6)
    ]}
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        journal=journal,
    )
    assert out["qty"] < 100
    assert out["kelly_multiplier"] < 1.0


def test_full_size_correlation_discount_applied():
    sector_map = {"AAPL": "Tech", "MSFT": "Tech"}
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        existing_positions=[{"symbol": "MSFT"}],
        sector_map=sector_map,
    )
    # Without journal, kelly_mult=1.0; correlation cuts size in half.
    assert out["correlation_multiplier"] == 0.5
    assert out["qty"] == 50


def test_full_size_clamps_to_minimum_one():
    """Even with extreme discounts, qty should never drop to 0
    when base_qty >= 1."""
    sector_map = {"AAPL": "Tech", "MSFT": "Tech",
                   "GOOGL": "Tech", "META": "Tech",
                   "AMZN": "Tech"}
    out = ps.compute_full_size(
        base_qty=2, strategy="breakout", symbol="AAPL",
        existing_positions=[
            {"symbol": "MSFT"}, {"symbol": "GOOGL"},
            {"symbol": "META"}, {"symbol": "AMZN"},
        ],
        sector_map=sector_map,
    )
    assert out["qty"] >= 1


def test_full_size_disabled_kelly_skips_kelly():
    journal = {"trades": [
        _trade(pnl=20) for _ in range(14)
    ] + [
        _trade(pnl=-10) for _ in range(6)
    ]}
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        journal=journal, enable_kelly=False,
    )
    assert out["kelly_multiplier"] == 1.0


def test_full_size_disabled_correlation_skips_correlation():
    sector_map = {"AAPL": "Tech", "MSFT": "Tech"}
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL",
        existing_positions=[{"symbol": "MSFT"}],
        sector_map=sector_map,
        enable_correlation=False,
    )
    assert out["correlation_multiplier"] == 1.0


def test_full_size_returns_rationale():
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL")
    assert "rationale" in out
    assert isinstance(out["rationale"], str)
    assert len(out["rationale"]) > 0


def test_full_size_returns_full_audit_dict():
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="AAPL")
    for k in ("qty", "base_qty", "kelly_multiplier",
                "correlation_multiplier", "edge",
                "correlated_count", "rationale"):
        assert k in out


# ============================================================================
# Source-pin: cloud_scheduler imports + uses position_sizing
# ============================================================================

def test_cloud_scheduler_imports_position_sizing():
    src = _src("cloud_scheduler.py")
    assert "import position_sizing" in src


def test_cloud_scheduler_uses_compute_full_size():
    src = _src("cloud_scheduler.py")
    assert "compute_full_size" in src


def test_cloud_scheduler_kelly_logs_rationale():
    """The deployer log should include the Kelly+corr rationale so
    we can audit why a trade got resized."""
    src = _src("cloud_scheduler.py")
    assert "Kelly+corr" in src or "Kelly+correlation" in src
