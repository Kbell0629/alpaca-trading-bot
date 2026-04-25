"""Round-61 pt.46 — analytics core tests.

Pure-function module that powers the new Analytics Hub dashboard tab.
Every aggregator is tested in isolation; the HTTP handler is a thin
wrapper covered in the endpoint tests file.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analytics_core import (
    compute_headline_kpis,
    compute_equity_curve,
    compute_drawdown_curve,
    compute_pnl_by_period,
    compute_pnl_by_symbol,
    compute_pnl_by_exit_reason,
    compute_hold_time_distribution,
    compute_pnl_distribution,
    compute_best_worst_trades,
    compute_filter_summary,
    compute_strategy_breakdown,
    build_analytics_view,
)


# ============================================================================
# Fixtures
# ============================================================================

def _utc(year, month, day, hour=12):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _trade(symbol="AAPL", strategy="breakout", pnl=10.0, status="closed",
            entry="2026-04-15T09:30:00+00:00",
            exit="2026-04-17T15:55:00+00:00",
            exit_reason="target_hit", qty=10, price=100.0,
            exit_price=110.0):
    return {
        "symbol": symbol, "strategy": strategy,
        "side": "buy", "qty": qty, "price": price,
        "status": status, "pnl": pnl,
        "exit_price": exit_price, "exit_reason": exit_reason,
        "timestamp": entry, "exit_timestamp": exit,
        "pnl_pct": ((exit_price - price) / price * 100)
                    if price else None,
    }


def _open_trade(symbol="MSFT", strategy="wheel"):
    return {"symbol": symbol, "strategy": strategy,
             "side": "buy", "qty": 5, "price": 400.0,
             "status": "open",
             "timestamp": "2026-04-23T09:30:00+00:00"}


# ============================================================================
# Headline KPIs
# ============================================================================

def test_kpis_empty_journal_zero_defaults():
    out = compute_headline_kpis({"trades": []})
    assert out["total_trades"] == 0
    assert out["closed_trades"] == 0
    assert out["wins"] == 0
    assert out["win_rate"] == 0.0
    assert out["total_realized_pnl"] == 0.0
    assert out["best_trade_pnl"] == 0.0


def test_kpis_basic_win_loss_counts():
    journal = {"trades": [
        _trade(pnl=100), _trade(pnl=-50), _trade(pnl=200),
        _trade(pnl=-30), _open_trade(),
    ]}
    out = compute_headline_kpis(journal)
    assert out["total_trades"] == 5
    assert out["closed_trades"] == 4
    assert out["open_trades"] == 1
    assert out["wins"] == 2
    assert out["losses"] == 2
    assert out["win_rate"] == 0.5
    assert out["total_realized_pnl"] == 220.0
    assert out["best_trade_pnl"] == 200.0
    assert out["worst_trade_pnl"] == -50.0


def test_kpis_expectancy_calculation():
    """Expectancy = win_rate * avg_win + loss_rate * avg_loss
    For 1 win at 100 + 1 loss at -50: 0.5*100 + 0.5*-50 = 25"""
    journal = {"trades": [_trade(pnl=100), _trade(pnl=-50)]}
    out = compute_headline_kpis(journal)
    assert abs(out["expectancy"] - 25.0) < 0.01


def test_kpis_avg_hold_days():
    """1 day hold + 3 day hold = avg 2 days."""
    journal = {"trades": [
        _trade(entry="2026-04-15T09:30:00+00:00",
                exit="2026-04-16T09:30:00+00:00"),
        _trade(entry="2026-04-15T09:30:00+00:00",
                exit="2026-04-18T09:30:00+00:00"),
    ]}
    out = compute_headline_kpis(journal)
    assert abs(out["avg_hold_days"] - 2.0) < 0.01


def test_kpis_paper_validation_days_elapsed():
    """Paper window started 2026-04-15. From 2026-04-25 = 10 days."""
    out = compute_headline_kpis({"trades": []}, now=_utc(2026, 4, 25))
    assert out["paper_validation_days_elapsed"] == 10


def test_kpis_first_last_trade_dates():
    journal = {"trades": [
        _trade(entry="2026-04-15T09:30:00+00:00",
                exit="2026-04-17T15:55:00+00:00"),
        _trade(entry="2026-04-20T09:30:00+00:00",
                exit="2026-04-22T15:55:00+00:00"),
    ]}
    out = compute_headline_kpis(journal)
    assert out["first_trade_date"] == "2026-04-15"
    assert out["last_trade_date"] == "2026-04-20"


def test_kpis_includes_account_data_when_provided():
    out = compute_headline_kpis(
        {"trades": []},
        account={"portfolio_value": "100000",
                  "positions": [{"unrealized_pl": "50.0"},
                                {"unrealized_pl": "-25.0"}]},
    )
    assert out["portfolio_value"] == 100000.0
    assert out["total_unrealized_pnl"] == 25.0


def test_kpis_uses_scorecard_sharpe_and_drawdown():
    out = compute_headline_kpis(
        {"trades": []},
        scorecard={"sharpe_ratio": 1.5, "max_drawdown_pct": -8.5},
    )
    assert out["sharpe_ratio"] == 1.5
    assert out["max_drawdown_pct"] == -8.5


def test_kpis_handles_unparseable_pnl():
    """Synthetic orphan_close entries can have pnl=None — must not
    crash."""
    journal = {"trades": [_trade(pnl=100), _trade(pnl=None)]}
    out = compute_headline_kpis(journal)
    assert out["closed_trades"] == 2
    # None pnl coerces to 0 in computation
    assert out["total_realized_pnl"] == 100.0


# ============================================================================
# Equity curve
# ============================================================================

def test_equity_curve_empty_returns_empty():
    assert compute_equity_curve({}) == []
    assert compute_equity_curve(None) == []
    assert compute_equity_curve({"daily_snapshots": []}) == []


def test_equity_curve_extracts_date_value_pairs():
    sc = {"daily_snapshots": [
        {"date": "2026-04-15", "portfolio_value": 100000},
        {"date": "2026-04-16", "portfolio_value": 100500},
        {"date": "2026-04-17", "portfolio_value": 99800},
    ]}
    out = compute_equity_curve(sc)
    assert len(out) == 3
    assert out[0] == {"date": "2026-04-15", "value": 100000.0}
    assert out[2] == {"date": "2026-04-17", "value": 99800.0}


def test_equity_curve_skips_malformed_entries():
    sc = {"daily_snapshots": [
        {"date": "2026-04-15", "portfolio_value": 100000},
        {"date": "2026-04-16"},   # missing value
        {"portfolio_value": 100500},  # missing date
        "garbage",
        {"date": "2026-04-17", "portfolio_value": "not-numeric"},
    ]}
    out = compute_equity_curve(sc)
    assert len(out) == 1


def test_equity_curve_sorts_by_date():
    sc = {"daily_snapshots": [
        {"date": "2026-04-17", "portfolio_value": 100},
        {"date": "2026-04-15", "portfolio_value": 200},
        {"date": "2026-04-16", "portfolio_value": 150},
    ]}
    out = compute_equity_curve(sc)
    assert [p["date"] for p in out] == ["2026-04-15",
                                            "2026-04-16",
                                            "2026-04-17"]


# ============================================================================
# Drawdown curve
# ============================================================================

def test_drawdown_curve_empty():
    assert compute_drawdown_curve([]) == []
    assert compute_drawdown_curve(None) == []


def test_drawdown_curve_tracks_peak_and_drawdown():
    """Equity goes 100 → 110 → 105 → 95 → 120.
    Peak progression: 100, 110, 110, 110, 120.
    Drawdown:  0, 0, -4.55%, -13.64%, 0."""
    eq = [
        {"date": "d1", "value": 100},
        {"date": "d2", "value": 110},
        {"date": "d3", "value": 105},
        {"date": "d4", "value": 95},
        {"date": "d5", "value": 120},
    ]
    out = compute_drawdown_curve(eq)
    assert out[0]["drawdown_pct"] == 0.0
    assert out[1]["drawdown_pct"] == 0.0
    assert abs(out[2]["drawdown_pct"] - (-4.55)) < 0.01
    assert abs(out[3]["drawdown_pct"] - (-13.64)) < 0.01
    assert out[4]["drawdown_pct"] == 0.0
    assert out[4]["peak"] == 120.0


def test_drawdown_curve_handles_missing_values():
    eq = [{"date": "d1", "value": None},
           {"date": "d2", "value": 100},
           {"date": "d3", "value": 90}]
    out = compute_drawdown_curve(eq)
    # None entry skipped, others process normally
    assert len(out) == 2


# ============================================================================
# P&L by period
# ============================================================================

def test_pnl_by_period_today_window():
    """Trade exited today goes in `today` AND `7d` AND `30d` AND
    `90d` AND `all`."""
    now = _utc(2026, 4, 25, 18)
    journal = {"trades": [_trade(pnl=100,
                                    exit="2026-04-25T15:55:00+00:00")]}
    out = compute_pnl_by_period(journal, now=now)
    assert out["today"]["pnl"] == 100
    assert out["today"]["count"] == 1
    assert out["7d"]["pnl"] == 100
    assert out["all"]["pnl"] == 100


def test_pnl_by_period_7d_excludes_older():
    now = _utc(2026, 4, 25)
    journal = {"trades": [
        _trade(pnl=50, exit="2026-04-20T15:00:00+00:00"),  # 5d
        _trade(pnl=100, exit="2026-04-10T15:00:00+00:00"),  # 15d
    ]}
    out = compute_pnl_by_period(journal, now=now)
    assert out["7d"]["pnl"] == 50
    assert out["7d"]["count"] == 1
    assert out["30d"]["pnl"] == 150
    assert out["30d"]["count"] == 2


def test_pnl_by_period_skips_open_trades():
    out = compute_pnl_by_period({"trades": [_open_trade()]},
                                  now=_utc(2026, 4, 25))
    assert out["all"]["count"] == 0


# ============================================================================
# P&L by symbol
# ============================================================================

def test_pnl_by_symbol_aggregates_per_ticker():
    journal = {"trades": [
        _trade(symbol="AAPL", pnl=100),
        _trade(symbol="AAPL", pnl=-30),
        _trade(symbol="MSFT", pnl=50),
    ]}
    out = compute_pnl_by_symbol(journal)
    by_sym = {r["symbol"]: r for r in out}
    assert by_sym["AAPL"]["pnl"] == 70.0
    assert by_sym["AAPL"]["count"] == 2
    assert by_sym["AAPL"]["wins"] == 1
    assert by_sym["AAPL"]["losses"] == 1
    assert by_sym["MSFT"]["pnl"] == 50.0


def test_pnl_by_symbol_resolves_occ_underlying():
    """Option contracts roll up to the underlying ticker."""
    journal = {"trades": [
        _trade(symbol="HIMS260508P00027000", pnl=100),
        _trade(symbol="HIMS", pnl=50),
    ]}
    out = compute_pnl_by_symbol(journal)
    by_sym = {r["symbol"]: r for r in out}
    # Both should aggregate under "HIMS"
    assert "HIMS" in by_sym
    assert by_sym["HIMS"]["pnl"] == 150.0
    assert by_sym["HIMS"]["count"] == 2


def test_pnl_by_symbol_sorted_by_absolute_impact():
    """Top entries should be the most-impactful (positive or negative).
    A -$500 stock is more "interesting" to surface than a +$50 one."""
    journal = {"trades": [
        _trade(symbol="A", pnl=10),
        _trade(symbol="B", pnl=-500),
        _trade(symbol="C", pnl=200),
    ]}
    out = compute_pnl_by_symbol(journal, top_n=3)
    # B (-500), C (200), A (10)
    assert [r["symbol"] for r in out] == ["B", "C", "A"]


def test_pnl_by_symbol_caps_at_top_n():
    journal = {"trades": [_trade(symbol=f"S{i}", pnl=i)
                            for i in range(1, 20)]}
    out = compute_pnl_by_symbol(journal, top_n=5)
    assert len(out) == 5


# ============================================================================
# P&L by exit reason
# ============================================================================

def test_pnl_by_exit_reason_aggregates_and_sorts():
    journal = {"trades": [
        _trade(pnl=100, exit_reason="target_hit"),
        _trade(pnl=-50, exit_reason="stop_triggered"),
        _trade(pnl=-30, exit_reason="stop_triggered"),
        _trade(pnl=20, exit_reason="target_hit"),
    ]}
    out = compute_pnl_by_exit_reason(journal)
    by_r = {r["exit_reason"]: r for r in out}
    assert by_r["target_hit"]["pnl"] == 120.0
    assert by_r["target_hit"]["count"] == 2
    assert by_r["stop_triggered"]["pnl"] == -80.0
    assert by_r["stop_triggered"]["count"] == 2
    # Sorted worst-first (so leakers surface)
    assert out[0]["exit_reason"] == "stop_triggered"


def test_pnl_by_exit_reason_handles_missing_reason():
    journal = {"trades": [_trade(pnl=10, exit_reason=None)]}
    out = compute_pnl_by_exit_reason(journal)
    assert out[0]["exit_reason"] == "unknown"


# ============================================================================
# Hold-time distribution
# ============================================================================

def test_hold_time_distribution_buckets():
    journal = {"trades": [
        # 0.5 day → <1d
        _trade(entry="2026-04-15T09:00:00+00:00",
                exit="2026-04-15T21:00:00+00:00"),
        # 2 days → 1-3d
        _trade(entry="2026-04-15T09:00:00+00:00",
                exit="2026-04-17T09:00:00+00:00"),
        # 5 days → 3-7d
        _trade(entry="2026-04-15T09:00:00+00:00",
                exit="2026-04-20T09:00:00+00:00"),
    ]}
    out = compute_hold_time_distribution(journal)
    by_bucket = {r["bucket"]: r["count"] for r in out}
    assert by_bucket["<1d"] == 1
    assert by_bucket["1-3d"] == 1
    assert by_bucket["3-7d"] == 1


def test_hold_time_distribution_returns_all_buckets():
    """Even with no trades, all 6 buckets should be in output (with
    count=0). Lets the dashboard render a stable chart shape."""
    out = compute_hold_time_distribution({"trades": []})
    buckets = [r["bucket"] for r in out]
    assert buckets == ["<1d", "1-3d", "3-7d", "7-14d",
                        "14-30d", "30d+"]
    for r in out:
        assert r["count"] == 0


# ============================================================================
# P&L distribution
# ============================================================================

def test_pnl_distribution_buckets_correctly():
    journal = {"trades": [
        _trade(pnl=-300),  # big_loss
        _trade(pnl=-100),  # loss
        _trade(pnl=-25),   # small_loss
        _trade(pnl=25),    # small_win
        _trade(pnl=100),   # win
        _trade(pnl=300),   # big_win
    ]}
    out = compute_pnl_distribution(journal)
    by_key = {r["key"]: r["count"] for r in out}
    assert by_key["big_loss"] == 1
    assert by_key["loss"] == 1
    assert by_key["small_loss"] == 1
    assert by_key["small_win"] == 1
    assert by_key["win"] == 1
    assert by_key["big_win"] == 1


def test_pnl_distribution_returns_all_buckets():
    """Stable chart shape — empty journal still returns 6 buckets."""
    out = compute_pnl_distribution({"trades": []})
    assert len(out) == 6
    for r in out:
        assert r["count"] == 0


# ============================================================================
# Best/worst trades
# ============================================================================

def test_best_worst_trades_returns_top_and_bottom():
    journal = {"trades": [
        _trade(symbol="A", pnl=100),
        _trade(symbol="B", pnl=200),
        _trade(symbol="C", pnl=-50),
        _trade(symbol="D", pnl=-200),
        _trade(symbol="E", pnl=50),
    ]}
    out = compute_best_worst_trades(journal, top_n=2)
    assert [t["symbol"] for t in out["best"]] == ["B", "A"]
    assert [t["symbol"] for t in out["worst"]] == ["D", "C"]


def test_best_worst_trades_only_includes_pnl_keys():
    """The returned trade dicts should be sanitised — only the
    fields the dashboard needs to render."""
    journal = {"trades": [_trade(symbol="X", pnl=100)]}
    out = compute_best_worst_trades(journal, top_n=1)
    keys = set(out["best"][0].keys())
    expected = {"symbol", "strategy", "pnl", "pnl_pct", "exit_reason",
                 "timestamp", "exit_timestamp", "qty", "price",
                 "exit_price"}
    assert keys == expected


def test_best_worst_trades_skips_open_and_no_pnl():
    journal = {"trades": [
        _trade(pnl=100),
        _open_trade(),  # no pnl
        _trade(pnl=None),  # closed but no pnl
    ]}
    out = compute_best_worst_trades(journal)
    # Only the 1 valid trade
    assert len(out["best"]) == 1
    assert len(out["worst"]) == 1


# ============================================================================
# Filter summary
# ============================================================================

def test_filter_summary_counts_by_reason():
    picks = [
        {"symbol": "A", "filter_reasons": ["already_held"]},
        {"symbol": "B", "filter_reasons": ["below_50ma"]},
        {"symbol": "C", "filter_reasons": ["already_held",
                                              "below_50ma"]},
        {"symbol": "D", "filter_reasons": []},  # deployable
        {"symbol": "E"},                          # deployable
    ]
    out = compute_filter_summary(picks)
    assert out["total_picks"] == 5
    assert out["deployable"] == 2
    assert out["blocked"] == 3
    by_reason = {r["reason"]: r["count"] for r in out["by_reason"]}
    assert by_reason["already_held"] == 2
    assert by_reason["below_50ma"] == 2


def test_filter_summary_normalizes_chase_block_value():
    """chase_block carries a value string ("chase_block (+12.3% intraday)")
    — count should bucket by the prefix only."""
    picks = [
        {"filter_reasons": ["chase_block (+12.3% intraday)"]},
        {"filter_reasons": ["chase_block (+8.5% intraday)"]},
    ]
    out = compute_filter_summary(picks)
    by_reason = {r["reason"]: r["count"] for r in out["by_reason"]}
    assert by_reason["chase_block"] == 2


def test_filter_summary_handles_empty_input():
    out = compute_filter_summary([])
    assert out["total_picks"] == 0
    assert out["deployable"] == 0
    assert out["blocked"] == 0
    assert out["by_reason"] == []


# ============================================================================
# Strategy breakdown
# ============================================================================

def test_strategy_breakdown_basic():
    journal = {"trades": [
        _trade(strategy="breakout", pnl=100),
        _trade(strategy="breakout", pnl=-50),
        _trade(strategy="wheel", pnl=20),
    ]}
    out = compute_strategy_breakdown(journal)
    assert out["breakout"]["count"] == 2
    assert out["breakout"]["wins"] == 1
    assert out["breakout"]["losses"] == 1
    assert out["breakout"]["win_rate"] == 0.5
    assert out["breakout"]["total_pnl"] == 50.0
    assert out["wheel"]["count"] == 1


def test_strategy_breakdown_tracks_best_worst_per_strategy():
    journal = {"trades": [
        _trade(strategy="breakout", pnl=100),
        _trade(strategy="breakout", pnl=-50),
        _trade(strategy="breakout", pnl=200),
    ]}
    out = compute_strategy_breakdown(journal)
    assert out["breakout"]["best"] == 200.0
    assert out["breakout"]["worst"] == -50.0


# ============================================================================
# End-to-end builder
# ============================================================================

def test_build_analytics_view_returns_all_keys():
    """The end-to-end builder must always return the same top-level
    schema so the dashboard renderer can rely on it."""
    out = build_analytics_view(
        journal={"trades": [_trade(pnl=100)]},
        scorecard={"daily_snapshots": [
            {"date": "2026-04-15", "portfolio_value": 100000},
        ]},
        account={"portfolio_value": 100000},
        picks=[],
    )
    expected_keys = {
        "kpis", "equity_curve", "drawdown_curve",
        "strategy_breakdown", "pnl_by_period",
        "pnl_by_symbol", "pnl_by_exit_reason",
        "hold_time_distribution", "pnl_distribution",
        "best_worst_trades", "filter_summary",
    }
    assert set(out.keys()) == expected_keys


def test_build_analytics_view_handles_all_none_inputs():
    """Robust to None inputs — every aggregator must degrade gracefully."""
    out = build_analytics_view(None, None, None, None)
    assert out["kpis"]["closed_trades"] == 0
    assert out["equity_curve"] == []
    assert out["drawdown_curve"] == []
    assert out["strategy_breakdown"] == {}
    assert out["best_worst_trades"]["best"] == []
    assert out["filter_summary"]["total_picks"] == 0
