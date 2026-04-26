"""Round-61 pt.80 — realized vs expected slippage tracking.

The pt.47 backtest harness assumes 10 bps of slippage. This module
reads the journal's per-trade slippage fields, aggregates them,
and reports whether realized matches the assumption.

Tests cover:
  * compute_slippage_bps signed-bps math (long + short)
  * aggregate_realized_slippage summary stats (mean, p50, p90,
    per-strategy breakdown, dollar cost)
  * compare_to_assumption verdict tiers (ok / warn / alert /
    preliminary)
  * annotate_close_with_slippage helper writes the 4 fields onto
    record_trade_close kwargs
  * Wiring source-pin: build_analytics_view returns
    slippage_summary
"""
from __future__ import annotations


# ============================================================================
# compute_slippage_bps
# ============================================================================

def test_compute_slippage_bps_buy_adverse():
    import slippage_tracker as st
    # Bought 0.05% above expected → +5 bps adverse for a buy.
    assert st.compute_slippage_bps(100.0, 100.05, side="buy") == 5.0


def test_compute_slippage_bps_buy_favorable():
    import slippage_tracker as st
    # Bought 0.05% below expected → -5 bps (price improvement).
    assert st.compute_slippage_bps(100.0, 99.95, side="buy") == -5.0


def test_compute_slippage_bps_sell_adverse():
    """For a sell, RECEIVING LESS than expected is adverse → positive bps."""
    import slippage_tracker as st
    assert st.compute_slippage_bps(100.0, 99.95, side="sell") == 5.0


def test_compute_slippage_bps_sell_favorable():
    import slippage_tracker as st
    assert st.compute_slippage_bps(100.0, 100.05, side="sell") == -5.0


def test_compute_slippage_bps_zero():
    import slippage_tracker as st
    assert st.compute_slippage_bps(100.0, 100.0, side="buy") == 0.0


def test_compute_slippage_bps_handles_short_close_alias():
    """A 'short_close' is structurally a buy (covering); slippage
    polarity matches buy."""
    import slippage_tracker as st
    assert st.compute_slippage_bps(100.0, 100.05, side="short_close") == -5.0


def test_compute_slippage_bps_bad_inputs():
    import slippage_tracker as st
    assert st.compute_slippage_bps(0, 100, side="buy") is None
    assert st.compute_slippage_bps(-10, 100, side="buy") is None
    assert st.compute_slippage_bps("bad", 100, side="buy") is None
    assert st.compute_slippage_bps(100, None, side="buy") is None


# ============================================================================
# aggregate_realized_slippage
# ============================================================================

def _make_trade(strategy="breakout", entry_bps=10.0, exit_bps=8.0,
                  qty=10, expected=100.0, filled_entry=None,
                  filled_exit=None):
    """Synthesize a closed trade with slippage fields populated."""
    if filled_entry is None:
        filled_entry = expected * (1 + entry_bps / 10000)
    if filled_exit is None:
        filled_exit = expected * (1 - exit_bps / 10000)
    return {
        "status": "closed", "strategy": strategy, "qty": qty,
        "price": expected,
        "entry_expected_price": expected,
        "entry_filled_price": filled_entry,
        "entry_slippage_bps": entry_bps,
        "exit_price": filled_exit,
        "exit_expected_price": expected * 1.05,
        "exit_filled_price": filled_exit,
        "exit_slippage_bps": exit_bps,
    }


def test_aggregate_empty_journal():
    import slippage_tracker as st
    out = st.aggregate_realized_slippage(None)
    assert out["entry_count"] == 0
    assert out["entry_mean_bps"] == 0.0
    out2 = st.aggregate_realized_slippage({})
    assert out2["entry_count"] == 0


def test_aggregate_single_trade():
    import slippage_tracker as st
    journal = {"trades": [_make_trade(entry_bps=12.0, exit_bps=8.0)]}
    out = st.aggregate_realized_slippage(journal)
    assert out["entry_count"] == 1
    assert out["entry_mean_bps"] == 12.0
    assert out["exit_count"] == 1
    assert out["exit_mean_bps"] == 8.0


def test_aggregate_multiple_trades_mean_correct():
    import slippage_tracker as st
    bps_values = [5, 10, 15, 20, 25]
    journal = {"trades": [_make_trade(entry_bps=b) for b in bps_values]}
    out = st.aggregate_realized_slippage(journal)
    assert out["entry_count"] == 5
    assert out["entry_mean_bps"] == 15.0  # (5+10+15+20+25)/5


def test_aggregate_p50_and_p90():
    import slippage_tracker as st
    bps_values = list(range(1, 11))   # 1..10
    journal = {"trades": [_make_trade(entry_bps=b) for b in bps_values]}
    out = st.aggregate_realized_slippage(journal)
    # P50 of 1..10 = midpoint of 5,6 = 5.5
    assert 5.0 <= out["entry_p50_bps"] <= 6.0
    # P90 ≈ 9.1
    assert 8.5 <= out["entry_p90_bps"] <= 10.0


def test_aggregate_skips_open_trades():
    import slippage_tracker as st
    closed = _make_trade()
    open_t = dict(closed)
    open_t["status"] = "open"
    journal = {"trades": [closed, open_t]}
    out = st.aggregate_realized_slippage(journal)
    assert out["entry_count"] == 1


def test_aggregate_skips_trades_without_slippage_fields():
    """Legacy entries (pre-pt.80) without slippage_bps are silently
    skipped."""
    import slippage_tracker as st
    legacy = {"status": "closed", "strategy": "breakout",
                "price": 100, "exit_price": 110, "qty": 10}
    new = _make_trade(entry_bps=15.0)
    journal = {"trades": [legacy, new]}
    out = st.aggregate_realized_slippage(journal)
    assert out["entry_count"] == 1
    assert out["entry_mean_bps"] == 15.0


def test_aggregate_per_strategy_breakdown():
    import slippage_tracker as st
    journal = {"trades": [
        _make_trade(strategy="breakout", entry_bps=10),
        _make_trade(strategy="breakout", entry_bps=20),
        _make_trade(strategy="wheel", entry_bps=5),
    ]}
    out = st.aggregate_realized_slippage(journal)
    assert "breakout" in out["by_strategy"]
    assert out["by_strategy"]["breakout"]["count"] == 2
    assert out["by_strategy"]["breakout"]["mean_bps"] == 15.0
    assert out["by_strategy"]["wheel"]["mean_bps"] == 5.0


def test_aggregate_dollar_cost_accumulates():
    import slippage_tracker as st
    # 10 shares @ $100 with 10 bps slippage → ~$1.00 cost per side
    journal = {"trades": [_make_trade(qty=10, expected=100.0,
                                          entry_bps=10.0)]}
    out = st.aggregate_realized_slippage(journal)
    assert 0.5 < out["entry_total_dollars"] < 1.5


# ============================================================================
# compare_to_assumption
# ============================================================================

def test_compare_below_minimum_sample_returns_preliminary():
    import slippage_tracker as st
    agg = {"entry_count": 5, "entry_mean_bps": 12.0,
            "entry_p50_bps": 12.0, "entry_p90_bps": 18.0,
            "entry_total_dollars": 5.0}
    out = st.compare_to_assumption(agg)
    assert out["state"] == "preliminary"
    assert out["sample_warning"] is True


def test_compare_realized_under_assumption_returns_ok():
    import slippage_tracker as st
    agg = {"entry_count": 30, "entry_mean_bps": 7.0,
            "entry_p50_bps": 6.0, "entry_p90_bps": 12.0,
            "entry_total_dollars": 50.0}
    out = st.compare_to_assumption(agg, assumed_bps=10.0)
    assert out["state"] == "ok"
    assert out["gap_bps"] < 0


def test_compare_realized_just_over_returns_warn():
    import slippage_tracker as st
    agg = {"entry_count": 30, "entry_mean_bps": 13.0,
            "entry_p50_bps": 12.0, "entry_p90_bps": 18.0,
            "entry_total_dollars": 90.0}
    out = st.compare_to_assumption(agg, assumed_bps=10.0)
    assert out["state"] == "warn"
    assert out["gap_bps"] == 3.0


def test_compare_realized_far_over_returns_alert():
    import slippage_tracker as st
    agg = {"entry_count": 30, "entry_mean_bps": 25.0,
            "entry_p50_bps": 24.0, "entry_p90_bps": 35.0,
            "entry_total_dollars": 200.0}
    out = st.compare_to_assumption(agg, assumed_bps=10.0)
    assert out["state"] == "alert"
    assert out["gap_bps"] == 15.0
    assert "overstate" in out["headline"].lower()


def test_compare_handles_bad_input():
    import slippage_tracker as st
    out = st.compare_to_assumption(None)
    assert out["state"] == "preliminary"
    assert out["sample_warning"] is True


def test_compare_custom_assumption():
    """Caller can pass a different assumption (e.g. 25 bps for
    illiquid small caps)."""
    import slippage_tracker as st
    agg = {"entry_count": 30, "entry_mean_bps": 22.0,
            "entry_p50_bps": 22.0, "entry_p90_bps": 30.0,
            "entry_total_dollars": 100.0}
    out = st.compare_to_assumption(agg, assumed_bps=25.0)
    assert out["state"] == "ok"


# ============================================================================
# annotate_close_with_slippage
# ============================================================================

def test_annotate_close_writes_entry_fields():
    import slippage_tracker as st
    kw = {}
    st.annotate_close_with_slippage(
        kw,
        entry_expected_price=100.0,
        entry_filled_price=100.05,
        side="buy",
    )
    assert kw["extra"]["entry_expected_price"] == 100.0
    assert kw["extra"]["entry_filled_price"] == 100.05
    assert kw["extra"]["entry_slippage_bps"] == 5.0


def test_annotate_close_writes_exit_fields():
    import slippage_tracker as st
    kw = {}
    # Pick numbers that produce a clean integer bps so the assertion
    # doesn't depend on float-rounding precision.
    st.annotate_close_with_slippage(
        kw,
        exit_expected_price=100.0,
        exit_filled_price=99.90,
        side="buy",   # entry side = buy; exit side inferred = sell
    )
    # 99.90 vs 100.0 on a sell → received 10 bps less → +10 bps adverse.
    assert kw["extra"]["exit_expected_price"] == 100.0
    assert kw["extra"]["exit_filled_price"] == 99.90
    assert kw["extra"]["exit_slippage_bps"] == 10.0


def test_annotate_close_short_position():
    """For a short, entry side is sell, exit is buy-to-cover.
    Slippage signs reflect that."""
    import slippage_tracker as st
    kw = {}
    st.annotate_close_with_slippage(
        kw,
        entry_expected_price=100.0,
        entry_filled_price=100.05,
        side="sell",  # entry is a sell (short)
    )
    # Selling at 100.05 vs expected 100 → received MORE → favorable.
    assert kw["extra"]["entry_slippage_bps"] == -5.0


def test_annotate_close_skips_when_data_missing():
    import slippage_tracker as st
    kw = {}
    st.annotate_close_with_slippage(kw, side="buy")
    # No fields written if neither expected nor filled are passed.
    assert kw.get("extra", {}) == {}


# ============================================================================
# build_analytics_view wiring
# ============================================================================

def test_analytics_view_includes_slippage_summary():
    import analytics_core
    out = analytics_core.build_analytics_view(
        journal={"trades": []},
        scorecard={},
        account=None, picks=[],
    )
    assert "slippage_summary" in out
    summary = out["slippage_summary"]
    # Empty journal → preliminary verdict, no aggregate counts.
    assert "aggregate" in summary
    assert "verdict" in summary


def test_analytics_view_slippage_summary_with_data():
    import analytics_core
    journal = {"trades": [
        {"status": "closed", "strategy": "breakout", "qty": 10,
         "price": 100, "exit_price": 110,
         "entry_expected_price": 100,
         "entry_filled_price": 100.10,
         "entry_slippage_bps": 10.0,
         "exit_expected_price": 110,
         "exit_filled_price": 109.92,
         "exit_slippage_bps": 8.0},
    ]}
    out = analytics_core.build_analytics_view(
        journal=journal, scorecard={}, account=None, picks=[])
    s = out["slippage_summary"]
    assert s["aggregate"]["entry_count"] == 1
    assert s["aggregate"]["entry_mean_bps"] == 10.0
    # 1 trade < MIN_TRADES_FOR_VERDICT → preliminary
    assert s["verdict"]["state"] == "preliminary"


# ============================================================================
# Pure-module discipline
# ============================================================================

def test_slippage_tracker_pure_module():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "slippage_tracker.py").read_text()
    forbidden = ("import cloud_scheduler", "from cloud_scheduler",
                  "import auth\n", "from auth import",
                  "import server", "from server import")
    for f in forbidden:
        assert f not in src
