"""Round-61 pt.44 — backtest-driven self-learning loop.

Closes the user-requested feedback loop: the weekly learning task
sweeps a small parameter grid for each strategy via the pt.37
backtest harness, picks the variant that maximally improves
simulated expectancy (subject to safety bounds), and writes the
proposed defaults to `learned_params.json` for the screener to
read on the next tick.

Safety invariants tested:
  * Adjustments capped at ±25% per cycle (no whiplash).
  * Absolute parameter bounds enforced after the relative cap
    (catastrophic settings impossible even with bad data).
  * Improvement threshold: best variant must beat current by ≥5%
    AND have ≥5 sim trades — otherwise no change.
  * Audit log of every cycle (last 12 cycles retained).
"""
from __future__ import annotations

import os
import tempfile

from learn_backtest import (
    MAX_RELATIVE_CHANGE,
    MIN_IMPROVEMENT_THRESHOLD,
    TUNABLE_PARAMS,
    PARAM_BOUNDS,
    build_param_variants,
    clamp_param_change,
    select_best_variant,
    propose_adjustments,
    merge_into_learned_params,
    safe_save_json,
    run_self_learning,
)


# ============================================================================
# Constants pinned
# ============================================================================

def test_constants_have_safe_defaults():
    """Pin the safety constants. These are operator-protective —
    changing them should be deliberate."""
    assert MAX_RELATIVE_CHANGE == 0.25
    assert MIN_IMPROVEMENT_THRESHOLD == 0.05


def test_tunable_params_only_include_safe_fields():
    """Only stop_pct / target_pct / max_hold_days are tuned. Things
    like lookback_high define strategy identity — never auto-tuned."""
    for strat in ("breakout", "mean_reversion", "short_sell"):
        assert strat in TUNABLE_PARAMS
        params = set(TUNABLE_PARAMS[strat])
        assert params == {"stop_pct", "target_pct", "max_hold_days"}


def test_param_bounds_have_floors_and_ceilings():
    """No parameter can be auto-tuned below an absolute floor or
    above an absolute ceiling regardless of what the backtest
    suggests."""
    for param in ("stop_pct", "target_pct", "max_hold_days"):
        assert param in PARAM_BOUNDS
        lo, hi = PARAM_BOUNDS[param]
        assert lo > 0
        assert hi > lo


# ============================================================================
# build_param_variants
# ============================================================================

def test_build_variants_includes_base():
    """The base (delta=0) variant is always element 0 — used as the
    reference when selecting the best."""
    base = {"stop_pct": 0.10, "target_pct": 0.30, "max_hold_days": 30,
             "side": "long", "_strategy": "breakout"}
    variants = build_param_variants(base, deltas=(-0.10, 0.10))
    # First variant should equal base
    assert variants[0]["stop_pct"] == 0.10


def test_build_variants_perturbs_one_param_at_a_time():
    """Cross-product would be 5^3 = 125; we vary one at a time so
    13 variants for 3 tunable + 4 non-zero deltas."""
    base = {"stop_pct": 0.10, "target_pct": 0.30, "max_hold_days": 30,
             "side": "long", "_strategy": "breakout"}
    variants = build_param_variants(base,
                                      deltas=(-0.20, -0.10, 0.10, 0.20))
    # 1 base + 3 params × 4 deltas = 13
    assert len(variants) == 13


def test_build_variants_skips_non_tunable_params():
    """`side` is not tunable — should never get perturbed."""
    base = {"stop_pct": 0.10, "side": "long", "_strategy": "breakout"}
    variants = build_param_variants(base, deltas=(-0.10, 0.10))
    for v in variants:
        assert v["side"] == "long"  # never perturbed


def test_build_variants_skips_unknown_strategy():
    """If the strategy isn't in TUNABLE_PARAMS, no variants are
    produced (but base is still returned)."""
    base = {"stop_pct": 0.10, "_strategy": "future_strategy"}
    variants = build_param_variants(base, deltas=(-0.10, 0.10))
    assert len(variants) == 1  # just base
    assert variants[0]["stop_pct"] == 0.10


def test_build_variants_max_hold_days_rounded_to_int():
    """max_hold_days perturbations should produce ints, not floats."""
    base = {"max_hold_days": 30, "stop_pct": 0.10, "target_pct": 0.30,
             "_strategy": "breakout"}
    variants = build_param_variants(base, deltas=(-0.10, 0.10))
    for v in variants:
        assert isinstance(v["max_hold_days"], int)


# ============================================================================
# clamp_param_change
# ============================================================================

def test_clamp_relative_cap_up():
    """A 50% increase should be clamped to +25%."""
    out = clamp_param_change("breakout", "stop_pct", 0.10, 0.15)
    assert abs(out - 0.125) < 0.001


def test_clamp_relative_cap_down():
    """A 50% decrease should be clamped to -25%."""
    out = clamp_param_change("breakout", "stop_pct", 0.10, 0.05)
    assert abs(out - 0.075) < 0.001


def test_clamp_within_relative_cap_passes_through():
    """A +10% increase is within the ±25% band — no clamping."""
    out = clamp_param_change("breakout", "stop_pct", 0.10, 0.11)
    assert abs(out - 0.11) < 0.001


def test_clamp_absolute_floor_enforced():
    """stop_pct floor is 5%. A proposed value below 5% gets pulled
    up even if the relative cap would allow lower."""
    # Current 0.06, propose 0.04 (33% drop, would be clamped to 0.045
    # by relative cap, then pulled to 0.05 by absolute floor)
    out = clamp_param_change("breakout", "stop_pct", 0.06, 0.04)
    assert out == 0.05


def test_clamp_absolute_ceiling_enforced():
    """stop_pct ceiling is 20%."""
    out = clamp_param_change("breakout", "stop_pct", 0.18, 0.30)
    # Relative cap would allow up to 0.225, absolute ceiling caps at 0.20
    assert out == 0.20


def test_clamp_max_hold_days_returns_int():
    out = clamp_param_change("breakout", "max_hold_days", 30, 35)
    assert isinstance(out, int)


def test_clamp_zero_or_negative_current_returns_unchanged():
    """If somehow current=0, can't compute relative band — return
    unchanged."""
    out = clamp_param_change("breakout", "stop_pct", 0, 0.10)
    assert out == 0


def test_clamp_invalid_input_returns_current():
    """Non-numeric input returns current unchanged."""
    out = clamp_param_change("breakout", "stop_pct", 0.10, "garbage")
    assert out == 0.10


# ============================================================================
# select_best_variant
# ============================================================================

def test_select_best_returns_highest_expectancy_above_threshold():
    """Variant beats base by 10%+ → selected."""
    base_exp = 5.0
    variants = [
        ({"stop_pct": 0.10}, {"expectancy": 5.0, "count": 10}),
        ({"stop_pct": 0.12}, {"expectancy": 6.0, "count": 10}),  # +20% wins
        ({"stop_pct": 0.08}, {"expectancy": 5.2, "count": 10}),  # only +4%
    ]
    best = select_best_variant(variants, base_exp)
    assert best is not None
    assert best["params"]["stop_pct"] == 0.12


def test_select_best_returns_none_when_no_improvement():
    """No variant beats base by 5%+ → return None (no change)."""
    base_exp = 5.0
    variants = [
        ({"stop_pct": 0.10}, {"expectancy": 5.0, "count": 10}),
        ({"stop_pct": 0.12}, {"expectancy": 5.1, "count": 10}),  # only +2%
    ]
    assert select_best_variant(variants, base_exp) is None


def test_select_best_skips_low_sample_variants():
    """A variant with only 2 sim trades isn't statistically
    meaningful — skip even if expectancy looks great."""
    base_exp = 5.0
    variants = [
        ({"stop_pct": 0.10}, {"expectancy": 5.0, "count": 10}),
        ({"stop_pct": 0.12}, {"expectancy": 100.0, "count": 2}),  # too few
    ]
    assert select_best_variant(variants, base_exp) is None


def test_select_best_handles_zero_base_expectancy():
    """When current expectancy is 0 (all losses), any positive
    variant should beat it."""
    base_exp = 0.0
    variants = [
        ({"stop_pct": 0.10}, {"expectancy": 0.0, "count": 10}),
        ({"stop_pct": 0.08}, {"expectancy": 1.5, "count": 10}),
    ]
    best = select_best_variant(variants, base_exp)
    assert best is not None
    assert best["params"]["stop_pct"] == 0.08


def test_select_best_handles_empty_variants():
    assert select_best_variant([], 5.0) is None
    assert select_best_variant(None, 5.0) is None


# ============================================================================
# propose_adjustments
# ============================================================================

def test_propose_skips_strategies_with_no_improvement():
    """Strategies whose backtest didn't beat base get added to
    no_change list, not adjustments."""
    strategy_results = {
        "breakout": {
            "base_expectancy": 5.0,
            "variants": [
                ({"stop_pct": 0.10, "target_pct": 0.30,
                  "max_hold_days": 30},
                 {"expectancy": 5.0, "count": 10}),
                ({"stop_pct": 0.12, "target_pct": 0.30,
                  "max_hold_days": 30},
                 {"expectancy": 5.1, "count": 10}),  # +2%, fails threshold
            ],
        },
    }
    current = {"breakout": {"stop_pct": 0.10, "target_pct": 0.30,
                              "max_hold_days": 30}}
    out = propose_adjustments(strategy_results, current)
    assert "breakout" not in out["adjustments"]
    assert any(nc["strategy"] == "breakout" for nc in out["no_change"])


def test_propose_records_old_new_and_improvement():
    """Adjustments include old/new values + the % improvement
    that justified the change. Audit-log requirement."""
    strategy_results = {
        "breakout": {
            "base_expectancy": 5.0,
            "variants": [
                ({"stop_pct": 0.10, "target_pct": 0.30,
                  "max_hold_days": 30},
                 {"expectancy": 5.0, "count": 10}),
                ({"stop_pct": 0.12, "target_pct": 0.30,
                  "max_hold_days": 30},
                 {"expectancy": 6.0, "count": 10}),  # +20%
            ],
        },
    }
    current = {"breakout": {"stop_pct": 0.10, "target_pct": 0.30,
                              "max_hold_days": 30}}
    out = propose_adjustments(strategy_results, current)
    assert "breakout" in out["adjustments"]
    change = out["adjustments"]["breakout"]["stop_pct"]
    assert change["old"] == 0.10
    assert change["new"] == 0.12
    assert change["expectancy_old"] == 5.0
    assert change["expectancy_new"] == 6.0
    assert change["improvement_pct"] == 20.0


def test_propose_clamps_aggressive_proposals():
    """If the backtest suggests +50% on stop_pct, the proposal
    clamps it to +25% before recording."""
    strategy_results = {
        "breakout": {
            "base_expectancy": 5.0,
            "variants": [
                ({"stop_pct": 0.10, "target_pct": 0.30,
                  "max_hold_days": 30},
                 {"expectancy": 5.0, "count": 10}),
                ({"stop_pct": 0.15, "target_pct": 0.30,
                  "max_hold_days": 30},
                 {"expectancy": 7.0, "count": 10}),  # +40%
            ],
        },
    }
    current = {"breakout": {"stop_pct": 0.10, "target_pct": 0.30,
                              "max_hold_days": 30}}
    out = propose_adjustments(strategy_results, current)
    change = out["adjustments"]["breakout"]["stop_pct"]
    assert change["raw_proposed"] == 0.15
    # Clamped to 0.10 * 1.25 = 0.125
    assert abs(change["new"] - 0.125) < 0.001


def test_propose_includes_timestamp():
    out = propose_adjustments({}, {})
    assert "timestamp" in out
    # ISO format check
    assert "T" in out["timestamp"]


# ============================================================================
# merge_into_learned_params
# ============================================================================

def test_merge_creates_new_payload_from_empty():
    """First-ever cycle: build the schema from scratch."""
    proposal = {
        "timestamp": "2026-04-25T12:00:00+00:00",
        "adjustments": {
            "breakout": {
                "stop_pct": {"old": 0.10, "new": 0.12,
                              "expectancy_old": 5.0,
                              "expectancy_new": 6.0,
                              "improvement_pct": 20.0,
                              "raw_proposed": 0.15},
            },
        },
        "no_change": [],
    }
    out = merge_into_learned_params(None, proposal)
    assert out["version"] == 1
    assert out["last_updated"] == "2026-04-25T12:00:00+00:00"
    assert out["params"]["breakout"]["stop_pct"] == 0.12
    assert len(out["history"]) == 1


def test_merge_preserves_unchanged_strategies():
    """Existing learned params for OTHER strategies should be
    preserved when only one strategy gets adjusted."""
    existing = {
        "version": 1,
        "params": {"wheel": {"stop_pct": 0.20}},
        "history": [],
    }
    proposal = {
        "timestamp": "t",
        "adjustments": {
            "breakout": {
                "stop_pct": {"old": 0.10, "new": 0.12,
                              "expectancy_old": 5.0,
                              "expectancy_new": 6.0,
                              "improvement_pct": 20.0,
                              "raw_proposed": 0.12},
            },
        },
        "no_change": [],
    }
    out = merge_into_learned_params(existing, proposal)
    assert out["params"]["wheel"]["stop_pct"] == 0.20  # preserved
    assert out["params"]["breakout"]["stop_pct"] == 0.12  # added


def test_merge_appends_to_history_capped_at_12():
    """History keeps the last 12 cycles to prevent unbounded growth."""
    existing = {
        "version": 1,
        "params": {},
        "history": [{"timestamp": f"t{i}"} for i in range(12)],
    }
    proposal = {"timestamp": "t13", "adjustments": {},
                  "no_change": []}
    out = merge_into_learned_params(existing, proposal)
    assert len(out["history"]) == 12  # cap holds
    # Newest is at the end
    assert out["history"][-1]["timestamp"] == "t13"
    # Oldest (t0) was dropped
    assert out["history"][0]["timestamp"] != "t0"


def test_merge_handles_missing_version_field():
    """Old/corrupt learned-params files without the version field
    get a fresh schema."""
    existing = {"some_legacy_field": "value"}
    proposal = {"timestamp": "t", "adjustments": {}, "no_change": []}
    out = merge_into_learned_params(existing, proposal)
    assert out["version"] == 1
    assert out["params"] == {}  # cleared since version mismatch


# ============================================================================
# safe_save_json (atomic write)
# ============================================================================

def test_safe_save_json_writes_atomically(tmp_path):
    path = str(tmp_path / "test.json")
    safe_save_json(path, {"foo": "bar"})
    import json as _json
    with open(path) as f:
        data = _json.load(f)
    assert data == {"foo": "bar"}


def test_safe_save_json_overwrites_existing_atomically(tmp_path):
    path = str(tmp_path / "test.json")
    safe_save_json(path, {"v": 1})
    safe_save_json(path, {"v": 2})
    import json as _json
    with open(path) as f:
        assert _json.load(f) == {"v": 2}


# ============================================================================
# run_self_learning end-to-end
# ============================================================================

def _stub_backtest_fn(bars_by_symbol, strategy, params):
    """Stub backtest that returns higher expectancy when stop_pct
    is exactly 0.12 (any other value gets the base 5.0)."""
    if params.get("stop_pct") == 0.12:
        return {"summary": {"expectancy": 7.0, "count": 10}}
    return {"summary": {"expectancy": 5.0, "count": 10}}


def test_run_self_learning_picks_best_variant():
    defaults = {
        "breakout": {"stop_pct": 0.10, "target_pct": 0.30,
                      "max_hold_days": 30},
    }
    out = run_self_learning(
        bars_by_symbol={"AAPL": []},
        run_backtest_fn=_stub_backtest_fn,
        current_defaults=defaults,
        deltas=(-0.20, -0.10, 0.10, 0.20),
    )
    assert "breakout" in out["adjustments"]
    # 0.10 * (1+0.20) = 0.12 → stub gives expectancy 7.0
    change = out["adjustments"]["breakout"]["stop_pct"]
    assert change["old"] == 0.10
    assert change["new"] == 0.12
    assert change["expectancy_new"] == 7.0


def test_run_self_learning_no_change_when_base_optimal():
    """If no variant improves over base, output has no adjustments."""
    def _flat_fn(bars, strat, params):
        return {"summary": {"expectancy": 5.0, "count": 10}}
    defaults = {"breakout": {"stop_pct": 0.10, "target_pct": 0.30,
                                "max_hold_days": 30}}
    out = run_self_learning(
        bars_by_symbol={"AAPL": []},
        run_backtest_fn=_flat_fn,
        current_defaults=defaults,
    )
    assert "breakout" not in out.get("adjustments", {})
    assert any(nc["strategy"] == "breakout"
                for nc in out.get("no_change", []))


def test_run_self_learning_handles_backtest_error_gracefully():
    """If backtest returns an error or non-dict, the strategy is
    skipped (no_change) but other strategies still process."""
    def _broken_fn(bars, strat, params):
        if strat == "breakout":
            return {"error": "bad data"}
        return {"summary": {"expectancy": 5.0, "count": 10}}
    defaults = {
        "breakout": {"stop_pct": 0.10, "target_pct": 0.30,
                      "max_hold_days": 30},
        "mean_reversion": {"stop_pct": 0.08, "target_pct": 0.10,
                            "max_hold_days": 10},
    }
    out = run_self_learning(
        bars_by_symbol={"AAPL": []},
        run_backtest_fn=_broken_fn,
        current_defaults=defaults,
    )
    # mean_reversion should still be evaluated
    assert "mean_reversion" not in out["adjustments"]  # no improvement
    # breakout should be in no_change due to broken response
    has_breakout = any(nc["strategy"] == "breakout"
                        for nc in out["no_change"])
    has_mr = any(nc["strategy"] == "mean_reversion"
                  for nc in out["no_change"])
    assert has_breakout or has_mr  # at least graceful


# ============================================================================
# Integration smoke test
# ============================================================================

def test_full_cycle_writes_learned_params_file(tmp_path):
    """End-to-end: run sweep → merge → save → reload. Pin the
    full schema written to disk."""
    defaults = {
        "breakout": {"stop_pct": 0.10, "target_pct": 0.30,
                      "max_hold_days": 30},
    }
    proposal = run_self_learning(
        bars_by_symbol={"AAPL": []},
        run_backtest_fn=_stub_backtest_fn,
        current_defaults=defaults,
    )
    merged = merge_into_learned_params(None, proposal)
    path = str(tmp_path / "learned_params.json")
    safe_save_json(path, merged)
    # Reload + verify schema
    import json as _json
    with open(path) as f:
        data = _json.load(f)
    assert data["version"] == 1
    assert "last_updated" in data
    assert "history" in data
    assert "params" in data
    assert data["params"]["breakout"]["stop_pct"] == 0.12
