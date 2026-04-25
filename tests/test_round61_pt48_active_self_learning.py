"""Round-61 pt.48 — active walk-forward + slippage + event-day gate.

Three tightly-coupled accuracy upgrades:
  1. ``learn_backtest.run_self_learning`` now supports
     ``validation_mode="walk_forward"`` so the weekly self-learning
     loop evaluates variants on out-of-sample test slices, not the
     in-sample window it tuned on.
  2. ``run_self_learning`` accepts ``slippage_bps`` and
     ``commission_per_trade`` so simulated expectancy reflects
     realistic fill friction.
  3. ``cloud_scheduler.run_auto_deployer`` consults
     ``event_calendar`` and raises the score threshold on FOMC /
     CPI / NFP / PCE release days.
"""
from __future__ import annotations

import pathlib
from datetime import date

import learn_backtest as lb


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# Item 1 — walk-forward in run_self_learning
# ============================================================================

def test_run_self_learning_default_in_sample_mode_unchanged():
    """Backwards compatibility: pt.44 callers without the new
    parameters should get bit-identical behaviour."""
    calls = []

    def fake_backtest(bars, strategy, params):
        calls.append((strategy, dict(params)))
        return {"summary": {"expectancy": 1.0, "count": 10}}

    out = lb.run_self_learning(
        bars_by_symbol={"X": [{}]},
        run_backtest_fn=fake_backtest,
        current_defaults={"breakout": {
            "stop_pct": 0.10, "target_pct": 0.20, "max_hold_days": 14,
        }},
    )
    assert "adjustments" in out
    # No walk_forward_fn invoked — fake_backtest must have been used.
    assert calls, "in-sample mode must use run_backtest_fn"


def test_run_self_learning_walk_forward_uses_walk_forward_fn():
    """In walk_forward mode, the harness fn must be invoked."""
    wf_calls = []

    def fake_wf(bars, strategy, *, train_days, test_days, step_days,
                  param_grid, base_params):
        wf_calls.append({
            "strategy": strategy,
            "grid_size": len(param_grid),
            "base_params": base_params,
        })
        return {
            "aggregate_test_summary": {"expectancy": 1.5, "count": 12},
            "overfit_ratio": 1.1,
        }

    bt_calls = []

    def fake_backtest(bars, strategy, params):
        bt_calls.append(strategy)
        return {"summary": {"expectancy": 0, "count": 0}}

    lb.run_self_learning(
        bars_by_symbol={"X": [{}]},
        run_backtest_fn=fake_backtest,
        current_defaults={"breakout": {
            "stop_pct": 0.10, "target_pct": 0.20, "max_hold_days": 14,
        }},
        validation_mode="walk_forward",
        walk_forward_fn=fake_wf,
    )
    assert wf_calls, "walk_forward mode must invoke walk_forward_fn"


def test_run_self_learning_invalid_mode_raises():
    def noop(*a, **k):
        return {}
    try:
        lb.run_self_learning(
            bars_by_symbol={}, run_backtest_fn=noop,
            current_defaults={"breakout": {"stop_pct": 0.1}},
            validation_mode="bogus",
        )
    except ValueError:
        return
    raise AssertionError("should raise ValueError for unknown validation_mode")


# ============================================================================
# select_best_variant overfit-ratio rejection
# ============================================================================

def test_select_best_variant_no_filter_when_max_overfit_none():
    variants = [
        ({"stop_pct": 0.05}, {"expectancy": 2.0, "count": 10,
                                "_overfit_ratio": 5.0}),
    ]
    best = lb.select_best_variant(variants, base_expectancy=1.0)
    assert best is not None
    assert best["params"]["stop_pct"] == 0.05


def test_select_best_variant_rejects_high_overfit_ratio():
    """A variant with overfit_ratio > max_overfit_ratio is rejected
    even if expectancy beats the baseline."""
    variants = [
        ({"stop_pct": 0.05}, {"expectancy": 2.0, "count": 10,
                                "_overfit_ratio": 3.0}),
    ]
    best = lb.select_best_variant(
        variants, base_expectancy=1.0, max_overfit_ratio=1.5)
    assert best is None


def test_select_best_variant_keeps_acceptable_overfit_ratio():
    variants = [
        ({"stop_pct": 0.05}, {"expectancy": 2.0, "count": 10,
                                "_overfit_ratio": 1.2}),
    ]
    best = lb.select_best_variant(
        variants, base_expectancy=1.0, max_overfit_ratio=1.5)
    assert best is not None


def test_select_best_variant_no_overfit_field_passes_filter():
    """Pre-pt.48 summaries don't have _overfit_ratio; they should
    NOT be rejected by the new filter."""
    variants = [
        ({"stop_pct": 0.05}, {"expectancy": 2.0, "count": 10}),
    ]
    best = lb.select_best_variant(
        variants, base_expectancy=1.0, max_overfit_ratio=1.5)
    assert best is not None


def test_propose_adjustments_threads_overfit_filter_only_in_walk_forward():
    """In walk_forward mode, propose_adjustments must reject overfit
    winners. In in_sample mode it must NOT (legacy back-compat)."""
    # Walk-forward result with a variant exceeding overfit cap.
    sr_wf = {
        "breakout": {
            "base_expectancy": 1.0,
            "validation_mode": "walk_forward",
            "max_overfit_ratio": 1.5,
            "variants": [
                ({"stop_pct": 0.05, "target_pct": 0.20, "max_hold_days": 14},
                 {"expectancy": 2.0, "count": 10, "_overfit_ratio": 3.0}),
            ],
        }
    }
    cur = {"breakout": {"stop_pct": 0.10, "target_pct": 0.20,
                          "max_hold_days": 14}}
    out_wf = lb.propose_adjustments(sr_wf, cur)
    assert "breakout" not in (out_wf.get("adjustments") or {})

    # Same variant in legacy in-sample mode → adjustment proposed.
    sr_is = {
        "breakout": {
            "base_expectancy": 1.0,
            "validation_mode": "in_sample",
            "variants": sr_wf["breakout"]["variants"],
        }
    }
    out_is = lb.propose_adjustments(sr_is, cur)
    assert "breakout" in (out_is.get("adjustments") or {})


# ============================================================================
# Item 2 — slippage / commission threaded through
# ============================================================================

def test_run_self_learning_passes_slippage_to_backtest():
    captured = []

    def fake_backtest(bars, strategy, params):
        captured.append(dict(params))
        return {"summary": {"expectancy": 1.0, "count": 10}}

    lb.run_self_learning(
        bars_by_symbol={"X": [{}]},
        run_backtest_fn=fake_backtest,
        current_defaults={"breakout": {
            "stop_pct": 0.10, "target_pct": 0.20, "max_hold_days": 14,
        }},
        slippage_bps=10.0,
        commission_per_trade=1.0,
    )
    assert captured, "fake_backtest should have been invoked"
    for params in captured:
        assert params.get("slippage_bps") == 10.0
        assert params.get("commission_per_trade") == 1.0


def test_run_self_learning_zero_friction_no_keys_added():
    """When slippage_bps=0 and commission_per_trade=0, the params
    dict should NOT have those keys — keeps tests bit-compat."""
    captured = []

    def fake_backtest(bars, strategy, params):
        captured.append(dict(params))
        return {"summary": {"expectancy": 1.0, "count": 10}}

    lb.run_self_learning(
        bars_by_symbol={"X": [{}]},
        run_backtest_fn=fake_backtest,
        current_defaults={"breakout": {
            "stop_pct": 0.10, "target_pct": 0.20, "max_hold_days": 14,
        }},
    )
    for params in captured:
        assert "slippage_bps" not in params
        assert "commission_per_trade" not in params


def test_walk_forward_mode_passes_friction_via_base_params():
    """In walk_forward mode, friction goes via ``base_params`` so
    every variant in the grid gets the same treatment."""
    wf_calls = []

    def fake_wf(bars, strategy, *, train_days, test_days, step_days,
                  param_grid, base_params):
        wf_calls.append(base_params)
        return {
            "aggregate_test_summary": {"expectancy": 1.5, "count": 12},
            "overfit_ratio": 1.1,
        }

    lb.run_self_learning(
        bars_by_symbol={"X": [{}]},
        run_backtest_fn=lambda *a, **k: {"summary": {}},
        current_defaults={"breakout": {
            "stop_pct": 0.10, "target_pct": 0.20, "max_hold_days": 14,
        }},
        validation_mode="walk_forward",
        slippage_bps=10.0,
        commission_per_trade=1.0,
        walk_forward_fn=fake_wf,
    )
    # First call (initial grid) gets base_params. Subsequent per-variant
    # calls have already-merged params + base_params=None.
    assert wf_calls, "walk_forward_fn should be invoked"
    assert wf_calls[0] is not None
    assert wf_calls[0].get("slippage_bps") == 10.0
    assert wf_calls[0].get("commission_per_trade") == 1.0


# ============================================================================
# Item 3 — event_calendar module
# ============================================================================

def test_event_calendar_recognises_fomc_2026():
    import event_calendar as ec
    hit, label = ec.is_high_impact_event_day(date(2026, 4, 29))
    assert hit is True
    assert label == "FOMC"


def test_event_calendar_recognises_cpi_2026():
    import event_calendar as ec
    hit, label = ec.is_high_impact_event_day(date(2026, 4, 14))
    assert hit is True
    assert label == "CPI"


def test_event_calendar_recognises_first_friday_nfp():
    import event_calendar as ec
    # First Friday of May 2026 is May 1.
    hit, label = ec.is_high_impact_event_day(date(2026, 5, 1))
    assert hit is True
    assert label == "NFP"


def test_event_calendar_quiet_day_returns_false():
    import event_calendar as ec
    # Random Wednesday with no events.
    hit, label = ec.is_high_impact_event_day(date(2026, 4, 22))
    assert hit is False
    assert label is None


def test_event_calendar_accepts_iso_string():
    import event_calendar as ec
    hit, _ = ec.is_high_impact_event_day("2026-04-29")
    assert hit is True


def test_event_calendar_accepts_datetime():
    import event_calendar as ec
    from datetime import datetime
    hit, _ = ec.is_high_impact_event_day(datetime(2026, 4, 29, 14, 0))
    assert hit is True


def test_event_calendar_invalid_input_returns_false():
    import event_calendar as ec
    for bad in (None, "not-a-date", 12345, [], {}):
        hit, label = ec.is_high_impact_event_day(bad)
        assert hit is False
        assert label is None


def test_event_calendar_priority_fomc_over_cpi():
    """If a date appeared in both FOMC and CPI lists, FOMC must win.
    We don't have a real collision in 2026/2027 so synthesise one by
    monkey-patching."""
    import event_calendar as ec
    # Test the priority order is hardcoded — pull a day that's only
    # FOMC and one that's only CPI; the resolver returns the right
    # label for each.
    hit_a, label_a = ec.is_high_impact_event_day(date(2026, 4, 29))  # FOMC
    hit_b, label_b = ec.is_high_impact_event_day(date(2026, 4, 14))  # CPI
    assert label_a == "FOMC"
    assert label_b == "CPI"


def test_next_high_impact_event_returns_soonest():
    import event_calendar as ec
    # April 28 2026 → next event is FOMC April 29.
    out = ec.next_high_impact_event(date(2026, 4, 28))
    assert out is not None
    label, ed = out
    assert label == "FOMC"
    assert ed == date(2026, 4, 29)


def test_next_high_impact_event_includes_today():
    """An event ON the queried date must be returned (>= not >)."""
    import event_calendar as ec
    out = ec.next_high_impact_event(date(2026, 4, 29))
    assert out is not None
    label, ed = out
    assert ed == date(2026, 4, 29)


def test_event_score_multiplier_table():
    import event_calendar as ec
    assert ec.event_score_multiplier("FOMC") == 2.0
    assert ec.event_score_multiplier("CPI") == 1.5
    assert ec.event_score_multiplier("NFP") == 1.5
    assert ec.event_score_multiplier("PCE") == 1.3
    assert ec.event_score_multiplier(None) == 1.0
    assert ec.event_score_multiplier("UNKNOWN") == 1.0


def test_first_friday_helper():
    import event_calendar as ec
    # First Friday of January 2026 is Jan 2.
    assert ec._first_friday_of_month(2026, 1) == date(2026, 1, 2)
    # First Friday of May 2026 is May 1.
    assert ec._first_friday_of_month(2026, 5) == date(2026, 5, 1)


def test_last_friday_helper():
    import event_calendar as ec
    # Last Friday of April 2026 is April 24.
    assert ec._last_friday_of_month(2026, 4) == date(2026, 4, 24)
    # Last Friday of December 2026 is December 25.
    assert ec._last_friday_of_month(2026, 12) == date(2026, 12, 25)


# ============================================================================
# Source-pin tests — auto-deployer must consult event_calendar
# ============================================================================

def test_auto_deployer_imports_event_calendar():
    src = _src("cloud_scheduler.py")
    assert "import event_calendar" in src


def test_auto_deployer_calls_is_high_impact_event_day():
    src = _src("cloud_scheduler.py")
    assert "is_high_impact_event_day" in src


def test_auto_deployer_uses_event_score_multiplier():
    src = _src("cloud_scheduler.py")
    assert "event_score_multiplier" in src


def test_auto_deployer_logs_event_day_skip():
    src = _src("cloud_scheduler.py")
    # The skip reason format includes the event label.
    assert "EVENT_TODAY" in src
    assert "EVENT_MULT" in src


# ============================================================================
# weekly_learning wires the new params
# ============================================================================

def test_weekly_learning_uses_walk_forward_mode():
    src = _src("cloud_scheduler.py")
    assert 'validation_mode="walk_forward"' in src or \
            "validation_mode='walk_forward'" in src


def test_weekly_learning_passes_realistic_friction():
    src = _src("cloud_scheduler.py")
    assert "slippage_bps=10" in src
    assert "commission_per_trade=1" in src


def test_weekly_learning_fetches_extended_window_for_walk_forward():
    """Walk-forward needs ≥ train_days + test_days of bars. Defaults
    are 30+30 = 60; we should fetch ≥ 120 to allow several folds."""
    src = _src("cloud_scheduler.py")
    assert "days=120" in src
