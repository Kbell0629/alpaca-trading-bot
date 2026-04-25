"""Round-61 pt.36 — trades analysis core behavioural coverage.

Pure-function module that powers the new /api/trades endpoint + the
Trades dashboard tab. Tests the data-shaping; the HTTP handler is a
thin wrapper covered separately.
"""
from __future__ import annotations


from trades_analysis_core import (
    EXIT_REASON_LABELS,
    enrich_trade,
    filter_trades,
    sort_trades,
    compute_strategy_summary,
    compute_overall_summary,
    build_trades_view,
    _occ_underlying,
    _hold_days,
)


# ============================================================================
# Fixtures
# ============================================================================

def _trade(**overrides):
    """Build a closed trade with sensible defaults; override any field."""
    base = {
        "timestamp": "2026-04-20T09:30:00-04:00",
        "symbol": "AAPL",
        "strategy": "trailing_stop",
        "side": "buy",
        "qty": 10,
        "price": 100.0,
        "reason": "breakout score 85",
        "deployer": "cloud_scheduler",
        "status": "closed",
        "exit_timestamp": "2026-04-22T15:55:00-04:00",
        "exit_price": 110.0,
        "exit_reason": "target_hit",
        "pnl": 100.0,
        "pnl_pct": 10.0,
        "exit_side": "sell",
    }
    base.update(overrides)
    return base


def _open_trade(**overrides):
    base = {
        "timestamp": "2026-04-23T09:35:00-04:00",
        "symbol": "INTC",
        "strategy": "breakout",
        "side": "buy",
        "qty": 63,
        "price": 65.0,
        "reason": "breakout score 78",
        "deployer": "cloud_scheduler",
        "status": "open",
    }
    base.update(overrides)
    return base


# ============================================================================
# enrich_trade
# ============================================================================

def test_enrich_adds_pnl_class_win_for_profit():
    e = enrich_trade(_trade(pnl=50))
    assert e["pnl_class"] == "win"
    assert e["is_winner"] is True
    assert e["is_open"] is False


def test_enrich_pnl_class_loss_for_negative():
    e = enrich_trade(_trade(pnl=-50))
    assert e["pnl_class"] == "loss"
    assert e["is_winner"] is False


def test_enrich_pnl_class_flat_for_near_zero():
    e = enrich_trade(_trade(pnl=0.001))
    assert e["pnl_class"] == "flat"


def test_enrich_open_trade_marked_open():
    e = enrich_trade(_open_trade())
    assert e["is_open"] is True
    assert e["pnl_class"] == "open"
    assert e["hold_days"] is None  # still open


def test_enrich_computes_hold_days():
    """timestamp 2026-04-20 09:30, exit 2026-04-22 15:55 → ~2.27 days."""
    e = enrich_trade(_trade())
    assert e["hold_days"] is not None
    assert 2.0 < e["hold_days"] < 2.5


def test_enrich_handles_missing_exit_timestamp():
    e = enrich_trade(_trade(exit_timestamp=None))
    assert e["hold_days"] is None


def test_enrich_human_exit_reason_known():
    e = enrich_trade(_trade(exit_reason="target_hit"))
    assert e["exit_reason_human"] == "Profit target hit"


def test_enrich_human_exit_reason_unknown_falls_back_to_title_case():
    e = enrich_trade(_trade(exit_reason="some_weird_reason"))
    assert e["exit_reason_human"] == "Some Weird Reason"


def test_enrich_returns_new_dict_not_mutating_input():
    orig = _trade()
    e = enrich_trade(orig)
    assert "pnl_class" in e
    assert "pnl_class" not in orig  # input untouched


def test_enrich_idempotent():
    """Enriching twice should produce the same shape, not double-add."""
    e1 = enrich_trade(_trade())
    e2 = enrich_trade(e1)
    for k in ("pnl_class", "is_winner", "hold_days", "exit_reason_human",
               "is_open", "occ_underlying"):
        assert e1[k] == e2[k]


def test_enrich_handles_non_dict_input():
    assert enrich_trade(None) == {}
    assert enrich_trade("oops") == {}
    assert enrich_trade(42) == {}


def test_enrich_invalid_pnl_string_treated_as_zero():
    e = enrich_trade(_trade(pnl="not-a-number"))
    assert e["pnl_class"] == "flat"


# ============================================================================
# OCC option symbol parsing
# ============================================================================

def test_occ_underlying_parses_short_put():
    assert _occ_underlying("HIMS260508P00027000") == "HIMS"


def test_occ_underlying_parses_short_call():
    assert _occ_underlying("DKNG260515C00021000") == "DKNG"


def test_occ_underlying_returns_none_for_normal_stock():
    assert _occ_underlying("AAPL") is None
    assert _occ_underlying("BRK.B") is None


def test_occ_underlying_returns_none_for_too_short():
    assert _occ_underlying("AAPL230101") is None


def test_occ_underlying_handles_lowercase():
    """Some sources lowercase the right-letter — accept both."""
    assert _occ_underlying("HIMS260508p00027000") == "HIMS"


def test_enrich_sets_occ_underlying_for_options():
    e = enrich_trade(_trade(symbol="HIMS260508P00027000"))
    assert e["occ_underlying"] == "HIMS"


def test_enrich_occ_underlying_falls_back_to_symbol_for_stocks():
    e = enrich_trade(_trade(symbol="AAPL"))
    assert e["occ_underlying"] == "AAPL"


# ============================================================================
# filter_trades
# ============================================================================

def test_filter_no_filters_returns_all():
    trades = [_trade(symbol="AAPL"), _trade(symbol="MSFT"),
               _open_trade(symbol="INTC")]
    assert len(filter_trades(trades, None)) == 3
    assert len(filter_trades(trades, {})) == 3


def test_filter_status_closed_excludes_open():
    trades = [_trade(), _open_trade()]
    out = filter_trades(trades, {"status": "closed"})
    assert len(out) == 1
    assert out[0]["status"] == "closed"


def test_filter_status_open_excludes_closed():
    trades = [_trade(), _open_trade()]
    out = filter_trades(trades, {"status": "open"})
    assert len(out) == 1
    assert out[0]["status"] == "open"


def test_filter_strategy_includes_only_listed():
    trades = [_trade(strategy="breakout"), _trade(strategy="wheel"),
               _trade(strategy="mean_reversion")]
    out = filter_trades(trades, {"strategy": ["breakout", "wheel"]})
    strategies = sorted(t["strategy"] for t in out)
    assert strategies == ["breakout", "wheel"]


def test_filter_win_loss_win():
    trades = [_trade(pnl=100), _trade(pnl=-50), _trade(pnl=0)]
    out = filter_trades([enrich_trade(t) for t in trades],
                          {"win_loss": "win"})
    assert len(out) == 1
    assert out[0]["pnl"] == 100


def test_filter_win_loss_loss():
    trades = [_trade(pnl=100), _trade(pnl=-50), _trade(pnl=0)]
    out = filter_trades([enrich_trade(t) for t in trades],
                          {"win_loss": "loss"})
    assert len(out) == 1
    assert out[0]["pnl"] == -50


def test_filter_win_loss_excludes_open_trades():
    """Open trades have no W/L yet — filter must drop them when
    win_loss is set."""
    trades = [_trade(pnl=100), _open_trade()]
    out = filter_trades([enrich_trade(t) for t in trades],
                          {"win_loss": "win"})
    assert len(out) == 1


def test_filter_symbol_substring_case_insensitive():
    trades = [_trade(symbol="AAPL"), _trade(symbol="META"),
               _trade(symbol="MSFT")]
    out = filter_trades(trades, {"symbol": "ms"})
    assert [t["symbol"] for t in out] == ["MSFT"]


def test_filter_symbol_matches_occ_underlying():
    """A user filtering by 'HIMS' should see options on HIMS."""
    trades = [_trade(symbol="HIMS260508P00027000"),
               _trade(symbol="AAPL")]
    out = filter_trades(trades, {"symbol": "HIMS"})
    assert len(out) == 1
    assert out[0]["symbol"] == "HIMS260508P00027000"


def test_filter_exit_reason():
    trades = [_trade(exit_reason="target_hit"),
               _trade(exit_reason="stop_triggered"),
               _trade(exit_reason="pead_window_complete")]
    out = filter_trades(trades, {"exit_reason": ["target_hit",
                                                    "pead_window_complete"]})
    assert len(out) == 2


def test_filter_side_long():
    trades = [_trade(side="buy"), _trade(side="sell")]
    out = filter_trades(trades, {"side": "long"})
    assert len(out) == 1
    assert out[0]["side"] == "buy"


def test_filter_side_short():
    trades = [_trade(side="buy"), _trade(side="sell")]
    out = filter_trades(trades, {"side": "short"})
    assert len(out) == 1
    assert out[0]["side"] == "sell"


def test_filter_date_from_excludes_older():
    trades = [
        _trade(timestamp="2026-04-15T09:30:00-04:00"),
        _trade(timestamp="2026-04-22T09:30:00-04:00"),
    ]
    out = filter_trades(trades, {"date_from": "2026-04-20T00:00:00-04:00"})
    assert len(out) == 1
    assert out[0]["timestamp"] == "2026-04-22T09:30:00-04:00"


def test_filter_date_to_excludes_newer():
    trades = [
        _trade(timestamp="2026-04-15T09:30:00-04:00"),
        _trade(timestamp="2026-04-22T09:30:00-04:00"),
    ]
    out = filter_trades(trades, {"date_to": "2026-04-20T00:00:00-04:00"})
    assert len(out) == 1
    assert out[0]["timestamp"] == "2026-04-15T09:30:00-04:00"


def test_filter_pnl_bounds():
    trades = [_trade(pnl=10), _trade(pnl=50), _trade(pnl=-20)]
    out = filter_trades(trades, {"min_pnl": 0, "max_pnl": 30})
    assert len(out) == 1
    assert out[0]["pnl"] == 10


def test_filter_combined_strategy_plus_winloss():
    trades = [
        _trade(strategy="breakout", pnl=100),
        _trade(strategy="breakout", pnl=-50),
        _trade(strategy="wheel", pnl=20),
    ]
    out = filter_trades([enrich_trade(t) for t in trades],
                          {"strategy": ["breakout"], "win_loss": "win"})
    assert len(out) == 1
    assert out[0]["strategy"] == "breakout"
    assert out[0]["pnl"] == 100


def test_filter_handles_non_dict_entries():
    trades = [_trade(), None, "garbage", _trade()]
    out = filter_trades(trades, {})
    assert len(out) == 2


# ============================================================================
# sort_trades
# ============================================================================

def test_sort_by_exit_timestamp_descending_default():
    trades = [
        _trade(exit_timestamp="2026-04-20T15:00:00-04:00"),
        _trade(exit_timestamp="2026-04-22T15:00:00-04:00"),
        _trade(exit_timestamp="2026-04-21T15:00:00-04:00"),
    ]
    out = sort_trades(trades)
    dates = [t["exit_timestamp"] for t in out]
    assert dates[0] > dates[1] > dates[2]


def test_sort_by_pnl_ascending():
    trades = [_trade(pnl=100), _trade(pnl=-50), _trade(pnl=20)]
    out = sort_trades(trades, sort_by="pnl", descending=False)
    assert [t["pnl"] for t in out] == [-50, 20, 100]


def test_sort_by_pnl_descending():
    trades = [_trade(pnl=100), _trade(pnl=-50), _trade(pnl=20)]
    out = sort_trades(trades, sort_by="pnl", descending=True)
    assert [t["pnl"] for t in out] == [100, 20, -50]


def test_sort_missing_values_land_last_descending():
    """Missing pnl values must NOT win the descending sort by being
    treated as a high value."""
    trades = [_trade(pnl=10), _trade(pnl=None), _trade(pnl=50)]
    out = sort_trades(trades, sort_by="pnl", descending=True)
    # Order: 50, 10, None
    pnls = [t.get("pnl") for t in out]
    assert pnls[0] == 50
    assert pnls[1] == 10
    assert pnls[2] is None


def test_sort_missing_values_land_last_ascending():
    """Missing values land last in ASCENDING too — never first."""
    trades = [_trade(pnl=10), _trade(pnl=None), _trade(pnl=-5)]
    out = sort_trades(trades, sort_by="pnl", descending=False)
    pnls = [t.get("pnl") for t in out]
    assert pnls[0] == -5
    assert pnls[1] == 10
    assert pnls[2] is None


def test_sort_empty_returns_empty():
    assert sort_trades([]) == []
    assert sort_trades(None) == []


def test_sort_by_string_field():
    trades = [_trade(symbol="MSFT"), _trade(symbol="AAPL"),
               _trade(symbol="GOOG")]
    out = sort_trades(trades, sort_by="symbol", descending=False)
    assert [t["symbol"] for t in out] == ["AAPL", "GOOG", "MSFT"]


# ============================================================================
# compute_strategy_summary
# ============================================================================

def test_strategy_summary_groups_by_strategy():
    trades = [
        _trade(strategy="breakout", pnl=100),
        _trade(strategy="breakout", pnl=-50),
        _trade(strategy="wheel", pnl=20),
    ]
    s = compute_strategy_summary(trades)
    assert "breakout" in s
    assert "wheel" in s
    assert s["breakout"]["count"] == 2
    assert s["wheel"]["count"] == 1


def test_strategy_summary_win_rate():
    trades = [
        _trade(strategy="breakout", pnl=100),
        _trade(strategy="breakout", pnl=-50),
        _trade(strategy="breakout", pnl=200),
        _trade(strategy="breakout", pnl=-30),
    ]
    s = compute_strategy_summary(trades)
    assert s["breakout"]["count"] == 4
    assert s["breakout"]["wins"] == 2
    assert s["breakout"]["losses"] == 2
    assert s["breakout"]["win_rate"] == 0.5


def test_strategy_summary_total_pnl():
    trades = [_trade(pnl=100), _trade(pnl=-30), _trade(pnl=50)]
    s = compute_strategy_summary(trades)
    strat = next(iter(s.values()))
    assert abs(strat["total_pnl"] - 120) < 0.01
    assert abs(strat["avg_pnl"] - 40) < 0.01


def test_strategy_summary_best_worst():
    trades = [_trade(pnl=100), _trade(pnl=-50), _trade(pnl=200),
               _trade(pnl=-30)]
    s = compute_strategy_summary(trades)
    strat = next(iter(s.values()))
    assert strat["best_pnl"] == 200
    assert strat["worst_pnl"] == -50


def test_strategy_summary_skips_open_trades():
    trades = [_trade(pnl=100), _open_trade()]
    s = compute_strategy_summary(trades)
    # Only the closed trade's strategy should appear
    assert sum(v["count"] for v in s.values()) == 1


def test_strategy_summary_skips_unparseable_pnl():
    """Round-34 orphan_close entries can have pnl=None — must not
    crash the summary."""
    trades = [_trade(pnl=100), _trade(pnl=None), _trade(pnl="bad")]
    s = compute_strategy_summary(trades)
    assert sum(v["count"] for v in s.values()) == 1


def test_strategy_summary_expectancy_basic():
    """Expectancy = win_rate * avg_win + loss_rate * avg_loss
    For 1 win at +100 and 1 loss at -50: 0.5*100 + 0.5*-50 = 25."""
    trades = [_trade(pnl=100), _trade(pnl=-50)]
    s = compute_strategy_summary(trades)
    strat = next(iter(s.values()))
    assert abs(strat["expectancy"] - 25.0) < 0.01


def test_strategy_summary_avg_hold_days():
    trades = [
        _trade(timestamp="2026-04-20T10:00:00-04:00",
                exit_timestamp="2026-04-21T10:00:00-04:00"),  # 1 day
        _trade(timestamp="2026-04-20T10:00:00-04:00",
                exit_timestamp="2026-04-23T10:00:00-04:00"),  # 3 days
    ]
    s = compute_strategy_summary(trades)
    strat = next(iter(s.values()))
    assert abs(strat["avg_hold_days"] - 2.0) < 0.01


def test_strategy_summary_empty_returns_empty():
    assert compute_strategy_summary([]) == {}
    assert compute_strategy_summary(None) == {}


def test_strategy_summary_drops_internal_running_sums():
    """Public schema should NOT leak internal running-sum keys."""
    trades = [_trade(pnl=10)]
    s = compute_strategy_summary(trades)
    strat = next(iter(s.values()))
    for leak in ("win_pnl_sum", "loss_pnl_sum",
                  "hold_days_sum", "hold_days_count"):
        assert leak not in strat


# ============================================================================
# compute_overall_summary
# ============================================================================

def test_overall_summary_empty_has_zero_defaults():
    s = compute_overall_summary([])
    assert s["count"] == 0
    assert s["wins"] == 0
    assert s["total_pnl"] == 0.0
    assert s["best_pnl"] is None


def test_overall_summary_aggregates_across_strategies():
    trades = [
        _trade(strategy="breakout", pnl=100),
        _trade(strategy="wheel", pnl=-30),
        _trade(strategy="trailing_stop", pnl=50),
    ]
    s = compute_overall_summary(trades)
    assert s["count"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert abs(s["total_pnl"] - 120) < 0.01


def test_overall_summary_win_rate():
    trades = [_trade(pnl=100), _trade(pnl=-30), _trade(pnl=20),
               _trade(pnl=-5)]
    s = compute_overall_summary(trades)
    assert s["count"] == 4
    assert s["wins"] == 2
    assert s["losses"] == 2
    assert s["win_rate"] == 0.5


# ============================================================================
# build_trades_view (end-to-end)
# ============================================================================

def test_build_view_returns_full_payload_shape():
    journal = {"trades": [_trade(), _trade(pnl=-30), _open_trade()]}
    out = build_trades_view(journal)
    assert "trades" in out
    assert "strategy_summary" in out
    assert "overall_summary" in out
    assert "filters_applied" in out
    assert "sort_by" in out
    assert "descending" in out
    assert out["total_count"] == 3
    assert out["filtered_count"] == 3


def test_build_view_filtered_count_reflects_filters():
    journal = {"trades": [
        _trade(strategy="breakout"),
        _trade(strategy="wheel"),
        _trade(strategy="breakout"),
    ]}
    out = build_trades_view(journal, filters={"strategy": ["wheel"]})
    assert out["total_count"] == 3
    assert out["filtered_count"] == 1
    assert out["trades"][0]["strategy"] == "wheel"


def test_build_view_summary_reflects_filtered_set_not_total():
    """When the user filters to just 'breakout', the strategy-summary
    cards should reflect ONLY the filtered breakout stats (not the
    full journal)."""
    journal = {"trades": [
        _trade(strategy="breakout", pnl=-50),
        _trade(strategy="breakout", pnl=-100),
        _trade(strategy="wheel", pnl=200),
    ]}
    out = build_trades_view(journal, filters={"strategy": ["breakout"]})
    # Should only see breakout in summary
    assert "breakout" in out["strategy_summary"]
    assert "wheel" not in out["strategy_summary"]
    # Overall reflects the filtered view only
    assert out["overall_summary"]["count"] == 2


def test_build_view_handles_missing_journal():
    out = build_trades_view(None)
    assert out["trades"] == []
    assert out["total_count"] == 0
    assert out["overall_summary"]["count"] == 0


def test_build_view_handles_journal_without_trades_key():
    out = build_trades_view({"daily_snapshots": []})
    assert out["trades"] == []
    assert out["total_count"] == 0


def test_build_view_default_sort_is_exit_timestamp_desc():
    """Most recent close at the top — that's the default user sees."""
    journal = {"trades": [
        _trade(exit_timestamp="2026-04-15T15:00:00-04:00"),
        _trade(exit_timestamp="2026-04-22T15:00:00-04:00"),
        _trade(exit_timestamp="2026-04-19T15:00:00-04:00"),
    ]}
    out = build_trades_view(journal)
    dates = [t["exit_timestamp"] for t in out["trades"]]
    assert dates[0] > dates[1] > dates[2]


def test_build_view_enriches_trades():
    """Trades in the output should be enriched (have pnl_class etc)."""
    journal = {"trades": [_trade(pnl=100)]}
    out = build_trades_view(journal)
    assert out["trades"][0]["pnl_class"] == "win"
    assert out["trades"][0]["hold_days"] is not None


# ============================================================================
# EXIT_REASON_LABELS coverage
# ============================================================================

def test_exit_reason_labels_covers_all_known_codes():
    """Every exit_reason string written by cloud_scheduler should
    have a human-readable label here. Catch new codes that get added
    without dashboard-side rendering by greppping for known callers."""
    expected_codes = {
        "target_hit", "short_target_hit", "short_stop_covered",
        "stop_triggered", "pead_window_complete", "pre_earnings_exit",
        "max_hold_exceeded", "closed_externally", "manual_close",
        "orphan_close", "wheel_assigned", "wheel_called_away",
        "wheel_btc_50pct", "wheel_expired", "kill_switch_flatten",
        "monthly_rebalance", "friday_risk_reduction",
        "ladder_10pct", "ladder_20pct", "ladder_30pct", "ladder_50pct",
    }
    missing = expected_codes - set(EXIT_REASON_LABELS.keys())
    assert not missing, (
        f"Missing human labels for exit_reason codes: {missing}")
