"""
Round-61 pt.7 — behavioral coverage for scorecard_core.py.

scorecard_core was extracted from update_scorecard.py (which is in
pyproject.toml's coverage omit list because it runs as a subprocess).
These tests drive every branch of the pure math so pytest-cov sees
it and any regression in the scorecard math gets caught loudly.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import scorecard_core as sc


# ============================================================================
# _dec / _to_cents_float helpers
# ============================================================================

class TestDec:
    def test_none_returns_default(self):
        assert sc._dec(None) == Decimal("0")
        assert sc._dec(None, default=Decimal("5")) == Decimal("5")

    def test_empty_string_returns_default(self):
        assert sc._dec("") == Decimal("0")

    def test_int(self):
        assert sc._dec(42) == Decimal("42")

    def test_float_via_str_avoids_ieee_imprecision(self):
        # Decimal(float(0.1)) = 0.10000000000000000555... — we use str().
        assert sc._dec(0.1) == Decimal("0.1")

    def test_already_decimal_passthrough(self):
        d = Decimal("3.14")
        assert sc._dec(d) is d

    def test_bad_input_returns_default(self):
        assert sc._dec(object()) == Decimal("0")

    def test_bad_input_returns_custom_default(self):
        assert sc._dec(object(), default=Decimal("99")) == Decimal("99")


class TestToCentsFloat:
    def test_rounds_to_cents(self):
        assert sc._to_cents_float(Decimal("1.234")) == 1.23
        # Banker's rounding: 1.235 → 1.24; 1.225 → 1.22 (round-half-even)
        assert sc._to_cents_float(Decimal("1.225")) == 1.22

    def test_accepts_non_decimal(self):
        assert sc._to_cents_float(1.5) == 1.50
        assert sc._to_cents_float("2.999") == 3.00
        assert sc._to_cents_float(None) == 0.0


# ============================================================================
# normalize_strategy_name
# ============================================================================

class TestNormalizeStrategyName:
    def test_empty(self):
        assert sc.normalize_strategy_name("") == ""
        assert sc.normalize_strategy_name(None) == ""

    def test_lowercase_and_replace(self):
        assert sc.normalize_strategy_name("Copy Trading") == "copy_trading"
        assert sc.normalize_strategy_name("trailing-stop") == "trailing_stop"
        assert sc.normalize_strategy_name("  Wheel Strategy  ") == "wheel_strategy"

    def test_non_string_coerced(self):
        assert sc.normalize_strategy_name(42) == "42"


# ============================================================================
# count_trade_statuses + split_wins_losses
# ============================================================================

class TestCountTradeStatuses:
    def test_empty(self):
        assert sc.count_trade_statuses([]) == {"total": 0, "open": 0, "closed": 0}

    def test_mixed(self):
        trades = [
            {"status": "open"},
            {"status": "closed"},
            {"status": "closed"},
            {"status": "weird-other"},
        ]
        out = sc.count_trade_statuses(trades)
        assert out == {"total": 4, "open": 1, "closed": 2}


class TestSplitWinsLosses:
    def test_splits_correctly(self):
        trades = [
            {"status": "closed", "pnl": 100},
            {"status": "closed", "pnl": -50},
            {"status": "closed", "pnl": 0},   # 0 → loss bucket
            {"status": "closed", "pnl": None},  # None → excluded entirely
            {"status": "open", "pnl": 999},     # open → excluded
        ]
        closed, wins, losses = sc.split_wins_losses(trades)
        assert len(closed) == 3   # excludes the None and the open
        assert len(wins) == 1
        assert len(losses) == 2  # includes the 0-pnl trade


# ============================================================================
# win_rate_pct / avg_pnl_pct
# ============================================================================

class TestWinRatePct:
    def test_empty_closed(self):
        assert sc.win_rate_pct([], []) == 0.0

    def test_normal(self):
        wins = [{"pnl": 1}, {"pnl": 2}, {"pnl": 3}]
        closed = [{"pnl": 1}] * 4
        # 3 / 4 * 100 = 75
        assert sc.win_rate_pct(wins, closed) == 75.0


class TestAvgPnlPct:
    def test_empty(self):
        assert sc.avg_pnl_pct([]) == 0.0

    def test_average(self):
        trades = [{"pnl_pct": 1.0}, {"pnl_pct": 3.0}, {"pnl_pct": 2.0}]
        assert sc.avg_pnl_pct(trades) == 2.0

    def test_missing_pnl_pct_treated_as_zero(self):
        trades = [{"pnl_pct": 2.0}, {"pnl_pct": 4.0}, {}]
        # (2+4+0)/3 = 2
        assert sc.avg_pnl_pct(trades) == 2.0


# ============================================================================
# profit_factor
# ============================================================================

class TestProfitFactor:
    def test_no_trades(self):
        assert sc.profit_factor([], []) == 0.0

    def test_normal(self):
        wins = [{"pnl": 100}, {"pnl": 200}]   # total 300
        losses = [{"pnl": -50}, {"pnl": -100}]  # total 150
        # 300 / 150 = 2.0
        assert sc.profit_factor(wins, losses) == 2.0

    def test_no_losses_returns_total_wins(self):
        wins = [{"pnl": 500}]
        assert sc.profit_factor(wins, []) == 500.0

    def test_no_wins_no_losses(self):
        assert sc.profit_factor([], [{"pnl": 0}]) == 0.0


class TestLargestWinLoss:
    def test_empty(self):
        lw, ll = sc.largest_win_loss([], [])
        assert lw == Decimal("0")
        assert ll == Decimal("0")

    def test_picks_extremes(self):
        wins = [{"pnl": 50}, {"pnl": 300}, {"pnl": 100}]
        losses = [{"pnl": -40}, {"pnl": -250}, {"pnl": -10}]
        lw, ll = sc.largest_win_loss(wins, losses)
        assert lw == Decimal("300")
        assert ll == Decimal("-250")


# ============================================================================
# avg_holding_days
# ============================================================================

class TestAvgHoldingDays:
    def test_empty(self):
        assert sc.avg_holding_days([]) == 0.0

    def test_normal_case(self):
        trades = [
            {"timestamp": "2026-04-01T10:00:00+00:00",
             "exit_timestamp": "2026-04-03T10:00:00+00:00"},  # 2 days
            {"timestamp": "2026-04-05T10:00:00+00:00",
             "exit_timestamp": "2026-04-09T10:00:00+00:00"},  # 4 days
        ]
        # Average = 3 days
        assert sc.avg_holding_days(trades) == 3.0

    def test_iso_Z_suffix_handled(self):
        trades = [{"timestamp": "2026-04-01T10:00:00Z",
                   "exit_timestamp": "2026-04-02T10:00:00Z"}]
        assert sc.avg_holding_days(trades) == 1.0

    def test_missing_timestamps_skipped(self):
        trades = [
            {"timestamp": "2026-04-01T10:00:00Z",
             "exit_timestamp": "2026-04-02T10:00:00Z"},     # 1 day
            {"timestamp": None, "exit_timestamp": "2026-04-09T10:00:00Z"},
            {"timestamp": "2026-04-05T10:00:00Z"},            # missing exit
        ]
        # Only the first counts
        assert sc.avg_holding_days(trades) == 1.0

    def test_malformed_timestamp_swallowed(self):
        trades = [{"timestamp": "not-a-date",
                   "exit_timestamp": "2026-04-05T10:00:00Z"}]
        assert sc.avg_holding_days(trades) == 0.0


# ============================================================================
# max_drawdown
# ============================================================================

class TestMaxDrawdown:
    def test_no_snapshots_uses_current_value(self):
        """Peak should equal portfolio_value if both snapshots and
        scorecard_peak are below current."""
        max_dd, peak = sc.max_drawdown(
            [], Decimal("100000"), Decimal("100100"), Decimal("100000"))
        # peak = max(100000, 100100, 100000) = 100100; current dd = 0
        assert peak == Decimal("100100")
        assert max_dd == 0.0

    def test_drawdown_from_snapshot_peak(self):
        """Snapshot hit peak of 110k then fell to 99k → 10% drawdown."""
        snaps = [
            {"portfolio_value": 100000},
            {"portfolio_value": 110000},
            {"portfolio_value": 99000},
        ]
        max_dd, peak = sc.max_drawdown(
            snaps, Decimal("100000"), Decimal("99000"), Decimal("100000"))
        # (110k - 99k) / 110k = 10.0%
        assert peak == Decimal("110000")
        assert abs(max_dd - 10.0) < 0.01

    def test_current_drawdown_vs_scorecard_peak(self):
        """scorecard_peak catches a historical peak that snapshots missed."""
        snaps = [{"portfolio_value": 100000}]
        max_dd, peak = sc.max_drawdown(
            snaps, Decimal("100000"), Decimal("95000"), Decimal("120000"))
        # Uses scorecard_peak 120k: (120k - 95k) / 120k ≈ 20.83%
        assert peak == Decimal("120000")
        assert abs(max_dd - 20.83) < 0.05


# ============================================================================
# daily_returns_from_snapshots
# ============================================================================

class TestDailyReturnsFromSnapshots:
    def test_empty(self):
        assert sc.daily_returns_from_snapshots([]) == []

    def test_single_snapshot(self):
        assert sc.daily_returns_from_snapshots([{"portfolio_value": 100}]) == []

    def test_normal(self):
        snaps = [{"portfolio_value": 100}, {"portfolio_value": 110},
                 {"portfolio_value": 99}]
        returns = sc.daily_returns_from_snapshots(snaps)
        assert len(returns) == 2
        # 110/100-1 = 0.1; 99/110-1 ≈ -0.1
        assert abs(returns[0] - 0.1) < 1e-9
        assert abs(returns[1] + 0.1) < 0.01

    def test_zero_prev_value_skipped(self):
        snaps = [{"portfolio_value": 0}, {"portfolio_value": 100}]
        # prev_val=0 → skip that row
        assert sc.daily_returns_from_snapshots(snaps) == []


# ============================================================================
# sharpe_sortino
# ============================================================================

class TestSharpeSortino:
    def test_fewer_than_two_returns_zero(self):
        assert sc.sharpe_sortino([]) == (0.0, 0.0)
        assert sc.sharpe_sortino([0.01]) == (0.0, 0.0)

    def test_zero_variance_returns_zero_sharpe(self):
        # All same return → variance 0 → sharpe 0
        sharpe, sortino = sc.sharpe_sortino([0.01, 0.01, 0.01])
        assert sharpe == 0.0
        assert sortino == 0.0  # no negative returns

    def test_positive_and_negative_returns(self):
        # Meaningful spread
        returns = [0.01, -0.005, 0.02, -0.01, 0.015]
        sharpe, sortino = sc.sharpe_sortino(returns)
        # Both should be non-zero (sortino > sharpe typically given
        # mean > 0 and downside only).
        assert sharpe != 0.0
        assert sortino != 0.0

    def test_only_positive_returns_sortino_zero(self):
        """No negative returns → sortino 0 (no downside to penalise)."""
        returns = [0.01, 0.02, 0.03]
        _sharpe, sortino = sc.sharpe_sortino(returns)
        assert sortino == 0.0


# ============================================================================
# total_return_pct
# ============================================================================

class TestTotalReturnPct:
    def test_profit(self):
        assert sc.total_return_pct(Decimal("110000"), Decimal("100000")) == 10.0

    def test_loss(self):
        val = sc.total_return_pct(Decimal("90000"), Decimal("100000"))
        assert abs(val - (-10.0)) < 1e-9

    def test_zero_starting(self):
        assert sc.total_return_pct(Decimal("1000"), Decimal("0")) == 0.0

    def test_negative_starting_returns_zero(self):
        assert sc.total_return_pct(Decimal("1000"), Decimal("-10")) == 0.0


# ============================================================================
# build_strategy_breakdown
# ============================================================================

class TestBuildStrategyBreakdown:
    def test_empty(self):
        out = sc.build_strategy_breakdown([])
        # All buckets present with zeros
        for name in sc.STRATEGY_BUCKETS:
            assert out[name] == {"trades": 0, "wins": 0, "pnl": 0.0}

    def test_name_normalisation(self):
        trades = [
            {"status": "closed", "pnl": 50, "strategy": "Copy Trading"},
            {"status": "closed", "pnl": -25, "strategy": "trailing-stop"},
            {"status": "open", "strategy": "Wheel"},
        ]
        out = sc.build_strategy_breakdown(trades)
        assert out["copy_trading"]["trades"] == 1
        assert out["copy_trading"]["wins"] == 1
        assert out["copy_trading"]["pnl"] == 50.0
        assert out["trailing_stop"]["trades"] == 1
        assert out["trailing_stop"]["wins"] == 0
        assert out["trailing_stop"]["pnl"] == -25.0
        assert out["wheel"]["trades"] == 1   # open trade counts
        assert out["wheel"]["pnl"] == 0.0

    def test_unknown_strategy_ignored(self):
        trades = [{"status": "closed", "pnl": 99, "strategy": "martingale"}]
        out = sc.build_strategy_breakdown(trades)
        for name in sc.STRATEGY_BUCKETS:
            assert out[name]["trades"] == 0

    def test_pead_bucket_present(self):
        """Round-10 fix: pead MUST exist as a bucket."""
        assert "pead" in sc.build_strategy_breakdown([])


# ============================================================================
# build_ab_testing
# ============================================================================

class TestBuildAbTesting:
    def test_all_below_threshold_empty(self):
        bd = {n: {"trades": 2, "wins": 1, "pnl": 10.0}
              for n in sc.STRATEGY_BUCKETS}
        assert sc.build_ab_testing(bd) == {}

    def test_pair_with_5plus_reports(self):
        bd = {
            "breakout": {"trades": 10, "wins": 7, "pnl": 500.0},
            "wheel":    {"trades": 8,  "wins": 4, "pnl": 200.0},
            # rest are below the threshold
            "trailing_stop": {"trades": 0, "wins": 0, "pnl": 0.0},
            "copy_trading": {"trades": 0, "wins": 0, "pnl": 0.0},
            "mean_reversion": {"trades": 0, "wins": 0, "pnl": 0.0},
            "pead": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        out = sc.build_ab_testing(bd)
        # Only the breakout-wheel pairing qualifies
        assert list(out.keys()) == ["breakout_vs_wheel"]
        row = out["breakout_vs_wheel"]
        # breakout avg = 50, wheel avg = 25 → breakout wins
        assert row["better_avg_pnl"] == "breakout"
        assert row["breakout"]["trades"] == 10

    def test_tie_winner_string(self):
        bd = {
            "breakout": {"trades": 5, "wins": 3, "pnl": 100.0},
            "wheel":    {"trades": 5, "wins": 3, "pnl": 100.0},
            "trailing_stop": {"trades": 0, "wins": 0, "pnl": 0.0},
            "copy_trading": {"trades": 0, "wins": 0, "pnl": 0.0},
            "mean_reversion": {"trades": 0, "wins": 0, "pnl": 0.0},
            "pead": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        out = sc.build_ab_testing(bd)
        assert out["breakout_vs_wheel"]["better_avg_pnl"] == "tie"

    def test_b_wins_when_higher_avg(self):
        bd = {
            "breakout": {"trades": 5, "wins": 2, "pnl": 50.0},
            "wheel":    {"trades": 5, "wins": 4, "pnl": 200.0},
            "trailing_stop": {"trades": 0, "wins": 0, "pnl": 0.0},
            "copy_trading": {"trades": 0, "wins": 0, "pnl": 0.0},
            "mean_reversion": {"trades": 0, "wins": 0, "pnl": 0.0},
            "pead": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        out = sc.build_ab_testing(bd)
        assert out["breakout_vs_wheel"]["better_avg_pnl"] == "wheel"


# ============================================================================
# build_correlation_warning
# ============================================================================

class TestBuildCorrelationWarning:
    def test_empty_positions_none(self):
        assert sc.build_correlation_warning([]) is None
        assert sc.build_correlation_warning(None) is None

    def test_non_list_none(self):
        assert sc.build_correlation_warning("not a list") is None  # type: ignore

    def test_under_threshold_no_warning(self):
        positions = [
            {"symbol": "AAPL"}, {"symbol": "MSFT"},
        ]
        sector_map = {"AAPL": "Technology", "MSFT": "Technology"}
        assert sc.build_correlation_warning(positions, sector_map=sector_map) is None

    def test_sector_concentration_triggers_warning(self):
        positions = [
            {"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "NVDA"},
        ]
        sector_map = {"AAPL": "Technology", "MSFT": "Technology",
                      "NVDA": "Technology"}
        w = sc.build_correlation_warning(positions, sector_map=sector_map)
        assert w is not None
        assert "Technology" in w["sectors"]
        assert len(w["sectors"]["Technology"]) == 3
        assert "Technology: AAPL, MSFT, NVDA (3 positions)" in w["details"]

    def test_annotate_fn_wins_over_sector_map(self):
        """Injected annotator resolves OCC option → underlying."""
        def annotate(ps):
            return [
                {"_sector": "Healthcare", "_underlying": "HIMS"},
                {"_sector": "Healthcare", "_underlying": "HIMS"},
                {"_sector": "Healthcare", "_underlying": "HIMS"},
            ]

        positions = [
            {"symbol": "HIMS260508P00027000"},
            {"symbol": "HIMS260508C00027000"},
            {"symbol": "HIMS"},
        ]
        w = sc.build_correlation_warning(positions, annotate_fn=annotate)
        assert w is not None
        assert "Healthcare" in w["sectors"]
        # Displays underlying HIMS, not the raw OCC symbol
        assert w["sectors"]["Healthcare"] == ["HIMS", "HIMS", "HIMS"]

    def test_annotate_fn_exception_falls_back_to_sector_map(self):
        def bad_annotate(ps):
            raise RuntimeError("broken")

        positions = [
            {"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "NVDA"},
        ]
        sector_map = {"AAPL": "Technology", "MSFT": "Technology",
                      "NVDA": "Technology"}
        w = sc.build_correlation_warning(positions,
                                          sector_map=sector_map,
                                          annotate_fn=bad_annotate)
        # Still produces a warning via sector_map fallback
        assert w is not None
        assert "Technology" in w["sectors"]

    def test_missing_symbol_falls_to_other(self):
        positions = [
            {"symbol": "X1"}, {"symbol": "X2"}, {"symbol": "X3"},
        ]
        w = sc.build_correlation_warning(positions, sector_map={})
        # All three unknown → all in "Other"
        assert w is not None
        assert w["sectors"].get("Other") == ["X1", "X2", "X3"]


# ============================================================================
# compute_readiness
# ============================================================================

class TestComputeReadiness:
    def test_all_pass_yields_100_ready(self):
        score, ready = sc.compute_readiness(
            days_tracked=45, win_rate=60.0, max_dd=5.0,
            profit_factor_val=2.0, sharpe=1.0, criteria={})
        assert score == 100
        assert ready is True

    def test_none_pass_yields_zero(self):
        score, ready = sc.compute_readiness(
            days_tracked=10, win_rate=40.0, max_dd=20.0,
            profit_factor_val=1.0, sharpe=0.1, criteria={})
        assert score == 0
        assert ready is False

    def test_four_of_five_below_threshold(self):
        """80 points is the ready line — must be >= 80, not >."""
        score, ready = sc.compute_readiness(
            days_tracked=45, win_rate=60.0, max_dd=5.0,
            profit_factor_val=2.0, sharpe=0.0, criteria={})
        assert score == 80
        assert ready is True  # >= 80

    def test_custom_criteria_respected(self):
        # Looser criteria → passes more often
        score, _ = sc.compute_readiness(
            days_tracked=5, win_rate=30.0, max_dd=25.0,
            profit_factor_val=0.5, sharpe=-1.0,
            criteria={"min_days": 1, "min_win_rate": 10,
                      "max_drawdown": 50, "min_profit_factor": 0.1,
                      "min_sharpe": -5.0})
        assert score == 100


# ============================================================================
# apply_snapshot_retention
# ============================================================================

class TestApplySnapshotRetention:
    def test_under_cap_unchanged(self):
        snaps = [{"i": i} for i in range(5)]
        assert sc.apply_snapshot_retention(snaps, max_count=10) == snaps

    def test_over_cap_trims_oldest(self):
        snaps = [{"i": i} for i in range(10)]
        out = sc.apply_snapshot_retention(snaps, max_count=3)
        assert len(out) == 3
        assert [s["i"] for s in out] == [7, 8, 9]

    def test_default_max_800(self):
        assert sc.DEFAULT_MAX_SNAPSHOTS == 800


# ============================================================================
# calculate_metrics orchestrator
# ============================================================================

class TestCalculateMetrics:
    def _base_now(self):
        return datetime(2026, 4, 24, 16, 0)

    def test_empty_journal_and_account(self):
        out = sc.calculate_metrics({}, {}, {}, [], now_fn=self._base_now)
        assert out["total_trades"] == 0
        assert out["closed_trades"] == 0
        assert out["winning_trades"] == 0
        assert out["losing_trades"] == 0
        assert out["win_rate_pct"] == 0.0
        assert out["profit_factor"] == 0
        assert out["current_value"] == 100000.0   # default starting
        # Empty journal: max_drawdown=0 passes the drawdown criteria
        # → +20 points. No other criterion passes on empty data.
        assert out["readiness_score"] == 20
        assert out["ready_for_live"] is False
        assert out["last_updated"] == "2026-04-24T16:00:00"

    def test_full_cycle(self):
        journal = {
            "trades": [
                {"status": "closed", "pnl": 500, "pnl_pct": 5.0,
                 "strategy": "Breakout",
                 "timestamp": "2026-04-01T10:00:00Z",
                 "exit_timestamp": "2026-04-03T10:00:00Z"},
                {"status": "closed", "pnl": -200, "pnl_pct": -2.0,
                 "strategy": "Wheel Strategy",
                 "timestamp": "2026-04-05T10:00:00Z",
                 "exit_timestamp": "2026-04-06T10:00:00Z"},
                {"status": "open", "strategy": "PEAD"},
            ],
            "daily_snapshots": [
                {"portfolio_value": 100000},
                {"portfolio_value": 100500},
                {"portfolio_value": 101000},
                {"portfolio_value": 100700},
            ],
        }
        scorecard = {"starting_capital": 100000, "peak_value": 101000}
        account = {"portfolio_value": 100300}
        out = sc.calculate_metrics(journal, scorecard, account, [],
                                    now_fn=self._base_now)
        assert out["total_trades"] == 3
        assert out["closed_trades"] == 2
        assert out["open_trades"] == 1
        assert out["winning_trades"] == 1
        assert out["losing_trades"] == 1
        assert out["win_rate_pct"] == 50.0
        assert out["profit_factor"] == 2.5
        assert out["largest_win"] == 500.0
        assert out["largest_loss"] == -200.0
        assert out["current_value"] == 100300.0
        assert out["peak_value"] >= 101000.0
        # 2 closed trades with avg 1.5 days hold each
        assert out["avg_holding_days"] == 1.5
        assert out["last_updated"] == "2026-04-24T16:00:00"
        # correlation_warning absent with empty positions
        assert "correlation_warning" not in out

    def test_portfolio_value_falls_to_starting_when_zero(self):
        """Account PV=0 (e.g. API error) falls back to starting_capital
        so current_value isn't a misleading $0."""
        out = sc.calculate_metrics({}, {"starting_capital": 50000},
                                    {"portfolio_value": 0}, [],
                                    now_fn=self._base_now)
        assert out["current_value"] == 50000.0

    def test_correlation_warning_attached_when_positions_concentrated(self):
        positions = [
            {"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "NVDA"},
        ]
        sector_map = {"AAPL": "Technology", "MSFT": "Technology",
                      "NVDA": "Technology"}
        out = sc.calculate_metrics({}, {}, {}, positions,
                                    now_fn=self._base_now,
                                    sector_map=sector_map)
        assert "correlation_warning" in out
        assert "Technology" in out["correlation_warning"]["sectors"]

    def test_readiness_criteria_fallback(self):
        """When scorecard.readiness_criteria is missing, the
        returned scorecard echoes the evaluated criteria (empty dict)."""
        out = sc.calculate_metrics({}, {}, {}, [], now_fn=self._base_now)
        # Readiness criteria echo: empty dict → output empty
        assert out["readiness_criteria"] == {}

    def test_readiness_criteria_echoed_when_present(self):
        criteria = {"min_days": 10, "min_win_rate": 30, "max_drawdown": 20,
                    "min_profit_factor": 1.0, "min_sharpe": 0.0}
        out = sc.calculate_metrics({}, {"readiness_criteria": criteria},
                                    {}, [], now_fn=self._base_now)
        assert out["readiness_criteria"] == criteria


# ============================================================================
# take_daily_snapshot orchestrator
# ============================================================================

class TestTakeDailySnapshot:
    def _now(self):
        return datetime(2026, 4, 24, 16, 0)

    def test_first_snapshot(self):
        journal = {"daily_snapshots": [], "trades": []}
        snap = sc.take_daily_snapshot(journal, {"portfolio_value": 100500,
                                                 "cash": 50000},
                                       [], {"starting_capital": 100000},
                                       now_fn=self._now)
        assert snap["date"] == "2026-04-24"
        assert snap["portfolio_value"] == 100500.0
        assert snap["cash"] == 50000.0
        assert snap["positions_count"] == 0
        assert snap["total_pnl"] == 500.0
        assert snap["total_pnl_pct"] == 0.5
        assert snap["daily_pnl"] == 500.0
        assert snap["open_trades"] == 0
        assert snap["closed_today"] == 0
        assert journal["daily_snapshots"] == [snap]

    def test_dedup_today(self):
        """If today's snapshot already exists, it's replaced."""
        existing = {"date": "2026-04-24", "portfolio_value": 99999}
        journal = {"daily_snapshots": [existing], "trades": []}
        snap = sc.take_daily_snapshot(journal, {"portfolio_value": 101000,
                                                 "cash": 0}, [],
                                       {"starting_capital": 100000},
                                       now_fn=self._now)
        # Only one entry for today now, and it's the new one
        assert len(journal["daily_snapshots"]) == 1
        assert journal["daily_snapshots"][0]["portfolio_value"] == 101000.0
        assert snap["date"] == "2026-04-24"

    def test_closed_today_counts(self):
        trades = [
            {"status": "closed", "exit_timestamp": "2026-04-24T12:00:00Z",
             "pnl": 50},
            {"status": "closed", "exit_timestamp": "2026-04-24T14:00:00Z",
             "pnl": -10},
            {"status": "closed", "exit_timestamp": "2026-04-24T15:00:00Z",
             "pnl": 0},  # zero counts as loss per scorecard convention
            {"status": "closed", "exit_timestamp": "2026-04-23T12:00:00Z",
             "pnl": 999},  # yesterday; excluded
            {"status": "open"},
        ]
        journal = {"daily_snapshots": [], "trades": trades}
        snap = sc.take_daily_snapshot(journal, {"portfolio_value": 100000,
                                                 "cash": 0}, [],
                                       {"starting_capital": 100000},
                                       now_fn=self._now)
        assert snap["closed_today"] == 3
        assert snap["wins_today"] == 1
        assert snap["losses_today"] == 2
        assert snap["open_trades"] == 1

    def test_peak_tracked_above_starting(self):
        journal = {
            "daily_snapshots": [
                {"date": "2026-04-20", "portfolio_value": 105000},
                {"date": "2026-04-22", "portfolio_value": 102000},
            ],
            "trades": [],
        }
        snap = sc.take_daily_snapshot(journal,
                                       {"portfolio_value": 101000, "cash": 0},
                                       [],
                                       {"starting_capital": 100000,
                                        "peak_value": 105000},
                                       now_fn=self._now)
        # peak 105k, current 101k → dd ≈ 3.81
        assert abs(snap["max_drawdown_pct"] - 3.81) < 0.05

    def test_retention_cap_applied(self):
        """If adding today's snapshot pushes past max_snapshots, the
        journal trims oldest."""
        snaps = [{"date": f"2026-01-{(i % 27) + 1:02d}", "portfolio_value": 100000}
                 for i in range(5)]
        journal = {"daily_snapshots": list(snaps), "trades": []}
        sc.take_daily_snapshot(journal,
                                {"portfolio_value": 100000, "cash": 0},
                                [], {"starting_capital": 100000},
                                now_fn=self._now,
                                max_snapshots=3)
        # After trimming, most-recent 3 kept (includes today)
        assert len(journal["daily_snapshots"]) == 3
        assert journal["daily_snapshots"][-1]["date"] == "2026-04-24"

    def test_positions_count(self):
        journal = {"daily_snapshots": [], "trades": []}
        snap = sc.take_daily_snapshot(journal,
                                       {"portfolio_value": 100000, "cash": 0},
                                       [{"symbol": "A"}, {"symbol": "B"}],
                                       {"starting_capital": 100000},
                                       now_fn=self._now)
        assert snap["positions_count"] == 2

    def test_positions_not_list_counts_zero(self):
        journal = {"daily_snapshots": [], "trades": []}
        snap = sc.take_daily_snapshot(journal,
                                       {"portfolio_value": 100000, "cash": 0},
                                       {"error": "api fail"},
                                       {"starting_capital": 100000},
                                       now_fn=self._now)
        assert snap["positions_count"] == 0

    def test_zero_starting_capital_pnl_pct_zero(self):
        journal = {"daily_snapshots": [], "trades": []}
        snap = sc.take_daily_snapshot(journal,
                                       {"portfolio_value": 0, "cash": 0},
                                       [], {"starting_capital": 0},
                                       now_fn=self._now)
        assert snap["total_pnl_pct"] == 0
        assert snap["daily_pnl_pct"] == 0

    def test_snapshot_exceeds_scorecard_peak(self):
        """A stored snapshot higher than scorecard.peak_value must
        update `peak` during the loop."""
        journal = {
            "daily_snapshots": [
                {"date": "2026-04-10", "portfolio_value": 100_000},
                {"date": "2026-04-15", "portfolio_value": 108_000},
            ],
            "trades": [],
        }
        snap = sc.take_daily_snapshot(journal,
                                       {"portfolio_value": 100_000, "cash": 0},
                                       [],
                                       {"starting_capital": 100_000,
                                        "peak_value": 101_000},
                                       now_fn=self._now)
        # Peak advanced via the snapshot loop to 108k; dd = (108k-100k)/108k
        assert abs(snap["max_drawdown_pct"] - 7.41) < 0.05

    def test_default_now_fn(self):
        """Without now_fn, real clock is used — we just verify no crash
        and the date slot looks like a YYYY-MM-DD."""
        journal = {"daily_snapshots": [], "trades": []}
        snap = sc.take_daily_snapshot(journal,
                                       {"portfolio_value": 100_000, "cash": 0},
                                       [],
                                       {"starting_capital": 100_000})
        assert len(snap["date"]) == 10
        assert snap["date"][4] == "-" and snap["date"][7] == "-"


class TestCalculateMetricsDefaults:
    def test_default_now_fn_sets_last_updated(self):
        """Without now_fn, the orchestrator uses the real clock."""
        out = sc.calculate_metrics({}, {}, {}, [])
        # last_updated starts with YYYY-MM-DDT...
        assert "T" in out["last_updated"]
        assert out["last_updated"][4] == "-"
