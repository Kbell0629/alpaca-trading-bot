"""Round-61 pt.49 — pipeline-aware backtest harness.

Pure-module tests for ``pipeline_backtest`` covering each gate +
the end-to-end replay flow.
"""
from __future__ import annotations

import datetime as _dt

import pipeline_backtest as pb


def _pick(symbol="X", *, score=80, dc=2.0, vol=10.0, strategy="breakout",
            sector=None, **kw):
    p = {
        "symbol": symbol,
        "best_strategy": strategy,
        "best_score": score,
        "daily_change": dc,
        "volatility": vol,
        "sector": sector,
    }
    p.update(kw)
    return p


# ============================================================================
# evaluate_gates — single-pick scenarios
# ============================================================================

def test_clean_pick_deploys():
    res = pb.evaluate_gates(_pick(score=80, dc=2.0, vol=8.0))
    assert res["deploy"] is True
    assert res["block_reasons"] == []


def test_already_held_blocks():
    res = pb.evaluate_gates(_pick(symbol="AAPL"),
                              held_symbols={"AAPL"})
    assert res["deploy"] is False
    assert "already_held" in res["block_reasons"]


def test_chase_block_fires():
    res = pb.evaluate_gates(_pick(dc=12.0))
    assert "chase_block" in res["block_reasons"]


def test_chase_block_threshold_configurable():
    res = pb.evaluate_gates(_pick(dc=10.0), chase_block_pct=15.0)
    assert "chase_block" not in res["block_reasons"]


def test_volatility_block_fires():
    res = pb.evaluate_gates(_pick(vol=30.0))
    assert "volatility_block" in res["block_reasons"]


def test_below_50ma_long_strategy():
    """Long pick filtered by trend → tagged below_50ma."""
    p = _pick(strategy="breakout")
    p["_filtered_by_trend"] = True
    res = pb.evaluate_gates(p)
    assert "below_50ma" in res["block_reasons"]


def test_above_50ma_short_strategy():
    p = _pick(strategy="short_sell")
    p["_filtered_by_trend"] = True
    res = pb.evaluate_gates(p)
    assert "above_50ma" in res["block_reasons"]


def test_breakout_unconfirmed_blocks():
    p = _pick()
    p["_breakout_unconfirmed"] = True
    res = pb.evaluate_gates(p)
    assert "breakout_unconfirmed" in res["block_reasons"]


def test_sector_cap_blocks_when_full():
    sector_map = {"AAPL": "Tech"}
    res = pb.evaluate_gates(
        _pick(symbol="AAPL"),
        sector_counts={"Tech": 2},
        sector_map=sector_map,
    )
    assert "sector_cap" in res["block_reasons"]


def test_sector_cap_allows_when_under():
    sector_map = {"AAPL": "Tech"}
    res = pb.evaluate_gates(
        _pick(symbol="AAPL"),
        sector_counts={"Tech": 1},
        sector_map=sector_map,
    )
    assert "sector_cap" not in res["block_reasons"]


def test_event_day_score_gate_blocks_low_score():
    """On FOMC day (multiplier 2.0), a score=70 pick is blocked
    because gate = 50 * 2 = 100."""
    res = pb.evaluate_gates(
        _pick(score=70),
        event_label="FOMC", event_multiplier=2.0,
    )
    assert any(r.startswith("event_day_") for r in res["block_reasons"])


def test_event_day_score_gate_allows_high_score():
    res = pb.evaluate_gates(
        _pick(score=120),
        event_label="FOMC", event_multiplier=2.0,
    )
    assert not any(r.startswith("event_day_")
                     for r in res["block_reasons"])


def test_min_score_blocks_low_score():
    res = pb.evaluate_gates(_pick(score=20), min_score=50)
    assert "min_score" in res["block_reasons"]


def test_min_score_not_double_counted_with_event_day():
    """If event_day fires, min_score should NOT also fire."""
    res = pb.evaluate_gates(
        _pick(score=10),
        event_label="FOMC", event_multiplier=2.0,
        min_score=50,
    )
    assert "min_score" not in res["block_reasons"]
    assert any(r.startswith("event_day_") for r in res["block_reasons"])


def test_invalid_pick_returns_block():
    res = pb.evaluate_gates(None)
    assert res["deploy"] is False


def test_missing_symbol_blocks():
    res = pb.evaluate_gates({"best_score": 80})
    assert res["deploy"] is False
    assert "missing_symbol" in res["block_reasons"]


def test_multiple_block_reasons_collected():
    res = pb.evaluate_gates(_pick(dc=15.0, vol=30.0, score=20))
    assert "chase_block" in res["block_reasons"]
    assert "volatility_block" in res["block_reasons"]
    assert "min_score" in res["block_reasons"]


# ============================================================================
# run_pipeline_backtest — end-to-end
# ============================================================================

def test_pipeline_backtest_empty_input():
    out = pb.run_pipeline_backtest([])
    assert out["total_picks"] == 0
    assert out["would_deploy"] == 0
    assert out["block_rate"] == 0.0


def test_pipeline_backtest_clean_picks_all_deploy():
    picks_history = [{
        "date": "2026-04-22",
        "picks": [
            _pick(symbol="AAPL", sector="Tech"),
            _pick(symbol="JPM", sector="Finance"),
        ],
    }]
    out = pb.run_pipeline_backtest(
        picks_history,
        sector_map={"AAPL": "Tech", "JPM": "Finance"},
    )
    assert out["total_picks"] == 2
    assert out["would_deploy"] == 2


def test_pipeline_backtest_chase_block_in_aggregate():
    picks_history = [{
        "date": "2026-04-22",
        "picks": [_pick(symbol="X", dc=12.0)],
    }]
    out = pb.run_pipeline_backtest(picks_history,
                                       sector_map={"X": "Tech"})
    assert out["would_deploy"] == 0
    assert out["blocked_by_reason"].get("chase_block") == 1


def test_pipeline_backtest_sector_cap_after_intra_day_deploy():
    """Two picks same sector + same day → first deploys, second
    sector_cap-blocks because cap=2 means we need >=2 already
    held; with only 1 deploy there's no cap. With cap=1 the
    second should block."""
    picks_history = [{
        "date": "2026-04-22",
        "picks": [
            _pick(symbol="AAPL"),
            _pick(symbol="MSFT"),
            _pick(symbol="GOOGL"),
        ],
    }]
    sector_map = {"AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech"}
    out = pb.run_pipeline_backtest(
        picks_history, sector_map=sector_map, max_per_sector=2,
    )
    # First 2 deploy; 3rd hits sector cap.
    assert out["would_deploy"] == 2
    assert out["blocked_by_reason"].get("sector_cap") == 1


def test_pipeline_backtest_initial_held_blocks_first_pick():
    picks_history = [{
        "date": "2026-04-22",
        "picks": [_pick(symbol="AAPL")],
    }]
    out = pb.run_pipeline_backtest(
        picks_history,
        initial_held={"AAPL"},
        sector_map={"AAPL": "Tech"},
    )
    assert out["would_deploy"] == 0
    assert out["blocked_by_reason"].get("already_held") == 1


def test_pipeline_backtest_block_rate():
    picks_history = [{
        "date": "2026-04-22",
        "picks": [
            _pick(symbol="AAPL", score=80),       # deploys
            _pick(symbol="MSFT", score=10),       # min_score
        ],
    }]
    out = pb.run_pipeline_backtest(
        picks_history,
        sector_map={"AAPL": "Tech", "MSFT": "Tech"},
        max_per_sector=10,   # don't trigger sector cap
    )
    assert out["would_deploy"] == 1
    assert out["block_rate"] == 0.5


def test_pipeline_backtest_blocks_by_day_summary():
    picks_history = [
        {"date": "2026-04-22", "picks": [_pick(symbol="A", dc=12)]},
        {"date": "2026-04-23", "picks": [_pick(symbol="B")]},
    ]
    out = pb.run_pipeline_backtest(
        picks_history, sector_map={"A": "Tech", "B": "Finance"},
    )
    assert len(out["blocks_by_day"]) == 2
    by_date = {d["date"]: d for d in out["blocks_by_day"]}
    assert by_date["2026-04-22"]["deploys"] == 0
    assert by_date["2026-04-23"]["deploys"] == 1


def test_pipeline_backtest_event_label_fn_injection():
    """User can inject a custom event-label fn."""
    def fake_event_fn(d):
        # Always FOMC for test → multiplier 2.0
        return "FOMC", 2.0
    picks_history = [{
        "date": "2026-04-22",
        "picks": [_pick(symbol="X", score=70)],
    }]
    out = pb.run_pipeline_backtest(
        picks_history,
        sector_map={"X": "Tech"},
        event_label_fn=fake_event_fn,
    )
    assert out["would_deploy"] == 0
    assert any("event_day" in r for r in out["blocked_by_reason"])


def test_pipeline_backtest_picks_history_invalid_entries_skipped():
    picks_history = [
        None, "string", {"no_picks": "bad"},
        {"date": "2026-04-22", "picks": [_pick(symbol="A")]},
    ]
    out = pb.run_pipeline_backtest(
        picks_history, sector_map={"A": "Tech"})
    assert out["total_picks"] == 1


def test_pipeline_backtest_simulate_outcomes_optional():
    """When simulate_outcomes=True with bars, returns counterfactual."""
    picks_history = [{
        "date": "2026-04-22",
        "picks": [_pick(symbol="A", strategy="breakout")],
    }]
    # Synthesise minimal bars for the simulator.
    bars = []
    d = _dt.date(2026, 4, 22)
    for i in range(40):
        bars.append({
            "date": d.isoformat(),
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0 + i * 0.5, "volume": 2_000_000,
        })
        d += _dt.timedelta(days=1)
    out = pb.run_pipeline_backtest(
        picks_history,
        sector_map={"A": "Tech"},
        simulate_outcomes=True,
        bars_by_symbol={"A": bars},
    )
    assert out["would_deploy"] == 1
    assert "counterfactual" in out
    assert "summary" in out["counterfactual"]


def test_pipeline_backtest_simulate_skipped_without_bars():
    picks_history = [{
        "date": "2026-04-22",
        "picks": [_pick(symbol="A")],
    }]
    out = pb.run_pipeline_backtest(
        picks_history, sector_map={"A": "Tech"},
        simulate_outcomes=True,   # no bars → no counterfactual
        bars_by_symbol=None,
    )
    assert "counterfactual" not in out
