"""Round-61 pt.47 — strategy_params resolver (pt.44b + pt.38b wiring).

Pure-module tests for the precedence chain
(learned > tier > fallback) plus source-pin tests for the
cloud_scheduler integration sites.
"""
from __future__ import annotations

import pathlib

import strategy_params as sp


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# resolve_strategy_param — precedence chain
# ============================================================================

def test_fallback_wins_when_no_overrides():
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.08)
    assert val == 0.08


def test_tier_wins_over_fallback():
    tier_cfg = {
        "name": "cash_standard",
        # portfolio_calibration's get_strategy_param looks up
        # TIER_STRATEGY_PARAMS by tier name; we don't need to fake the
        # whole table here — just point at a real entry.
    }
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.08,
        tier_cfg=tier_cfg)
    # cash_standard.breakout.stop_loss_pct should be set in the SSOT.
    # We don't know the exact value — just assert it's not the fallback.
    import portfolio_calibration as pc
    expected = pc.get_strategy_param(tier_cfg, "breakout",
                                       "stop_loss_pct", None)
    if expected is not None:
        assert val == expected
    else:
        # If the tier table doesn't have this entry, fallback wins
        assert val == 0.08


def test_learned_wins_over_tier():
    tier_cfg = {"name": "cash_standard"}
    learned = {"params": {"breakout": {"stop_pct": 0.07}}}
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.08,
        tier_cfg=tier_cfg, learned_params=learned)
    assert val == 0.07


def test_learned_wins_over_fallback_when_no_tier():
    learned = {"params": {"breakout": {"stop_pct": 0.06}}}
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.08,
        learned_params=learned)
    assert val == 0.06


def test_learned_param_name_aliasing_stop_loss():
    """learned_params uses 'stop_pct'; consumer asks for 'stop_loss_pct'."""
    learned = {"params": {"breakout": {"stop_pct": 0.09}}}
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.10,
        learned_params=learned)
    assert val == 0.09


def test_learned_param_name_aliasing_profit_target():
    learned = {"params": {"breakout": {"target_pct": 0.18}}}
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="profit_target_pct", fallback=0.15,
        learned_params=learned)
    assert val == 0.18


def test_learned_param_name_passthrough_max_hold():
    """max_hold_days is the same name in both schemas."""
    learned = {"params": {"breakout": {"max_hold_days": 21}}}
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="max_hold_days", fallback=14,
        learned_params=learned)
    assert val == 21


def test_learned_accepts_bare_params_dict():
    """Caller may pass either the full file dict or just the inner
    'params' subdict."""
    learned = {"breakout": {"stop_pct": 0.05}}
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.10,
        learned_params=learned)
    assert val == 0.05


def test_learned_strategy_not_in_table_falls_through():
    """If learned_params has 'breakout' but caller asks about 'wheel',
    fall through to tier/fallback."""
    learned = {"params": {"breakout": {"stop_pct": 0.05}}}
    val = sp.resolve_strategy_param(
        strategy="wheel", param_name="stop_loss_pct", fallback=0.10,
        learned_params=learned)
    assert val == 0.10


def test_learned_param_not_in_strategy_entry_falls_through():
    """learned_params has breakout but no stop_pct; fall through."""
    learned = {"params": {"breakout": {"max_hold_days": 21}}}
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.10,
        learned_params=learned)
    assert val == 0.10


def test_handles_none_inputs():
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.10,
        tier_cfg=None, learned_params=None)
    assert val == 0.10


def test_handles_garbage_learned_params():
    """Non-dict learned_params → fall through, don't crash."""
    for bad in (42, "string", [1, 2, 3], True):
        val = sp.resolve_strategy_param(
            strategy="breakout", param_name="stop_loss_pct", fallback=0.10,
            learned_params=bad)
        assert val == 0.10


def test_handles_garbage_tier_cfg():
    for bad in (42, "string", [1, 2, 3], True):
        val = sp.resolve_strategy_param(
            strategy="breakout", param_name="stop_loss_pct", fallback=0.10,
            tier_cfg=bad)
        assert val == 0.10


def test_handles_none_value_in_learned():
    """Explicit None in learned_params should fall through, not return
    None as the resolved value."""
    learned = {"params": {"breakout": {"stop_pct": None}}}
    val = sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct", fallback=0.10,
        learned_params=learned)
    assert val == 0.10


# ============================================================================
# resolve_rules_dict — high-level helper
# ============================================================================

def test_rules_dict_passes_through_unrelated_keys():
    base = {
        "stop_loss_pct": 0.08,
        "profit_target_pct": 0.15,
        "max_hold_days": 14,
        "trailing_activation_pct": 0.10,  # not in resolver scope
        "exit_policy": "trailing_stop",
        "atr_pct": 0.025,
    }
    out = sp.resolve_rules_dict(strategy="breakout", base_rules=base)
    # Resolver-scope keys present + unchanged when no overrides
    assert out["stop_loss_pct"] == 0.08
    assert out["profit_target_pct"] == 0.15
    assert out["max_hold_days"] == 14
    # Out-of-scope keys passed through
    assert out["trailing_activation_pct"] == 0.10
    assert out["exit_policy"] == "trailing_stop"
    assert out["atr_pct"] == 0.025


def test_rules_dict_applies_learned_overrides():
    base = {
        "stop_loss_pct": 0.10,
        "profit_target_pct": 0.15,
        "max_hold_days": 14,
    }
    learned = {"params": {"breakout": {
        "stop_pct": 0.06, "target_pct": 0.20, "max_hold_days": 21,
    }}}
    out = sp.resolve_rules_dict(
        strategy="breakout", base_rules=base, learned_params=learned)
    assert out["stop_loss_pct"] == 0.06
    assert out["profit_target_pct"] == 0.20
    assert out["max_hold_days"] == 21


def test_rules_dict_does_not_mutate_input():
    base = {"stop_loss_pct": 0.10, "profit_target_pct": 0.15,
            "max_hold_days": 14}
    learned = {"params": {"breakout": {"stop_pct": 0.05}}}
    sp.resolve_rules_dict(strategy="breakout", base_rules=base,
                            learned_params=learned)
    assert base["stop_loss_pct"] == 0.10  # original untouched


def test_rules_dict_skips_keys_absent_from_base():
    """If the deployer's base_rules doesn't have profit_target_pct
    (PEAD doesn't set it explicitly), the resolver shouldn't add
    keys the caller didn't ask for."""
    base = {"stop_loss_pct": 0.08, "max_hold_days": 60}
    learned = {"params": {"pead": {"target_pct": 0.20}}}
    out = sp.resolve_rules_dict(
        strategy="pead", base_rules=base, learned_params=learned)
    assert "profit_target_pct" not in out


# ============================================================================
# Source-pin tests — cloud_scheduler must use the resolver
# ============================================================================

def test_cloud_scheduler_imports_resolver():
    src = _src("cloud_scheduler.py")
    assert "import strategy_params" in src or "from strategy_params" in src


def test_cloud_scheduler_loads_learned_params():
    src = _src("cloud_scheduler.py")
    assert "learned_params.json" in src
    assert "LEARNED_PARAMS" in src


def test_cloud_scheduler_calls_resolve_rules_dict():
    """Long-side rules must go through resolve_rules_dict."""
    src = _src("cloud_scheduler.py")
    assert "resolve_rules_dict" in src


def test_cloud_scheduler_calls_resolve_strategy_param_for_short():
    """Short-side has its own param resolution path."""
    src = _src("cloud_scheduler.py")
    assert "resolve_strategy_param" in src
    # Should be invoked with strategy="short_sell"
    assert 'strategy="short_sell"' in src or "strategy='short_sell'" in src
