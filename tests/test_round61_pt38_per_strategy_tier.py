"""Round-61 pt.38 — per-strategy stop / target / max-hold-days
overrides keyed by portfolio tier.

The round-50 tier system already calibrates max_positions /
max_position_pct / strategies_enabled per tier. Pt.38 adds a
per-STRATEGY override layer so a $500 account doesn't take 12%
stop-losses on $50 trades (which would limit-lose 4-5 in a row),
and a $25k+ account doesn't take 5% stops that get whipsawed out
of valid setups by intraday noise.

Tested:
  * `TIER_STRATEGY_PARAMS` table contents — each tier has at minimum
    the strategies in its `strategies_enabled` list.
  * `get_strategy_param(tier_cfg, strategy, param_name, default)`
    returns tier-specific values when present + falls back cleanly.
  * Risk-budget invariants — micro stops < small stops < standard
    stops, mirroring the documented design intent.
"""
from __future__ import annotations

import portfolio_calibration as pc


# ============================================================================
# Table coverage — every enabled strategy in every tier has a config row
# ============================================================================

def test_each_tier_has_strategy_params_for_its_enabled_strategies():
    """Every strategy in a tier's `strategies_enabled` list MUST have
    a corresponding entry in TIER_STRATEGY_PARAMS — otherwise the
    auto-deployer falls back to bare defaults for some strategies
    but tier-aware values for others (inconsistent risk profile).
    """
    for tier in pc.TIER_DEFAULTS:
        name = tier["name"]
        params_for_tier = pc.TIER_STRATEGY_PARAMS.get(name) or {}
        for strat in tier.get("strategies_enabled", []):
            # `trailing_stop` is the universal exit policy; it's
            # always present alongside any entry strategy. We pin it
            # explicitly. Other strategies should be present too.
            assert strat in params_for_tier, (
                f"Tier '{name}' enables strategy '{strat}' but "
                f"TIER_STRATEGY_PARAMS has no entry for it.")


def test_strategy_params_have_required_keys():
    """Every strategy entry must have at least one of the recognised
    risk parameters (stop_loss_pct / profit_target_pct /
    max_hold_days / trail_distance_pct)."""
    valid_keys = {
        "stop_loss_pct", "profit_target_pct", "max_hold_days",
        "trail_distance_pct", "trailing_activation_pct",
    }
    for tier_name, table in pc.TIER_STRATEGY_PARAMS.items():
        for strat, params in table.items():
            assert params, (
                f"{tier_name}.{strat}: empty params dict")
            unknown = set(params.keys()) - valid_keys
            assert not unknown, (
                f"{tier_name}.{strat}: unknown params {unknown}")


# ============================================================================
# Risk-budget invariants (the WHY behind the tier table)
# ============================================================================

def test_breakout_stop_increases_with_account_size():
    """Micro tier should have TIGHTER stops than standard tier.
    Rationale: small accounts can't afford many losses, so the
    risk-per-trade dollar amount is held lower."""
    micro = pc.TIER_STRATEGY_PARAMS["cash_micro"]["breakout"]["stop_loss_pct"]
    small = pc.TIER_STRATEGY_PARAMS["cash_small"]["breakout"]["stop_loss_pct"]
    std = pc.TIER_STRATEGY_PARAMS["cash_standard"]["breakout"]["stop_loss_pct"]
    assert micro <= small <= std, (
        f"breakout stop_loss_pct should grow with tier: "
        f"micro={micro}, small={small}, standard={std}")


def test_max_hold_days_grows_with_account_size():
    """Larger accounts can afford to hold positions longer (slower
    capital recycle is OK when you have plenty of capital)."""
    micro = pc.TIER_STRATEGY_PARAMS["cash_micro"]["breakout"]["max_hold_days"]
    std = pc.TIER_STRATEGY_PARAMS["cash_standard"]["breakout"]["max_hold_days"]
    assert std >= micro


def test_target_grows_with_account_size():
    """Standard tier targets a bigger move per trade — when each trade
    is %-of-portfolio smaller, you can afford to wait for the bigger win."""
    micro = pc.TIER_STRATEGY_PARAMS["cash_micro"]["breakout"]["profit_target_pct"]
    std = pc.TIER_STRATEGY_PARAMS["cash_standard"]["breakout"]["profit_target_pct"]
    assert std >= micro


def test_short_sell_disabled_on_cash_tiers():
    """Cash accounts can't short — pt.38 must NOT define short_sell
    params for cash tiers (would suggest the strategy is supported
    when it isn't)."""
    # Defined for margin tiers
    assert "short_sell" in pc.TIER_STRATEGY_PARAMS["margin_small"]
    assert "short_sell" in pc.TIER_STRATEGY_PARAMS["margin_standard"]
    # Cash standard tier — we do define short_sell because
    # `strategies_enabled` doesn't include it, so the table entry is
    # never consulted. But cash_standard has no short_sell defined:
    assert "short_sell" not in pc.TIER_STRATEGY_PARAMS["cash_standard"]


def test_wheel_only_on_tiers_where_wheel_enabled():
    """Wheel params should only be defined for tiers where
    wheel_enabled=True (cash_standard + margin_standard +
    margin_large). Otherwise we'd be advertising a strategy the
    tier blocks elsewhere."""
    for tier in pc.TIER_DEFAULTS:
        name = tier["name"]
        wheel_enabled = bool(tier.get("wheel_enabled"))
        params_for_tier = pc.TIER_STRATEGY_PARAMS.get(name) or {}
        if not wheel_enabled:
            assert "wheel" not in params_for_tier, (
                f"Tier {name} has wheel_enabled=False but "
                f"TIER_STRATEGY_PARAMS includes wheel — remove it "
                f"from the table or enable wheel on the tier.")


# ============================================================================
# get_strategy_param helper
# ============================================================================

def test_get_strategy_param_returns_tier_value_when_present():
    tier_cfg = {"name": "cash_micro"}
    val = pc.get_strategy_param(tier_cfg, "breakout", "stop_loss_pct", 0.10)
    # Micro tier: 5%, NOT the default 10%
    assert val == 0.05


def test_get_strategy_param_falls_back_to_default_for_missing_tier():
    """A tier name that isn't in TIER_STRATEGY_PARAMS should fall
    through to the caller-provided default."""
    val = pc.get_strategy_param(
        {"name": "made_up_tier"}, "breakout", "stop_loss_pct", 0.10)
    assert val == 0.10


def test_get_strategy_param_falls_back_when_tier_cfg_is_none():
    val = pc.get_strategy_param(None, "breakout", "stop_loss_pct", 0.10)
    assert val == 0.10


def test_get_strategy_param_falls_back_when_tier_cfg_missing_name():
    val = pc.get_strategy_param({}, "breakout", "stop_loss_pct", 0.10)
    assert val == 0.10


def test_get_strategy_param_falls_back_for_missing_strategy():
    """Strategy name not in the tier's entry → use default."""
    val = pc.get_strategy_param(
        {"name": "cash_micro"}, "garbage_strategy", "stop_loss_pct", 0.99)
    assert val == 0.99


def test_get_strategy_param_falls_back_for_missing_param():
    """Strategy is in the tier's entry but the param name isn't —
    use the caller's default."""
    val = pc.get_strategy_param(
        {"name": "cash_micro"}, "breakout", "garbage_param", 1234)
    assert val == 1234


def test_get_strategy_param_handles_non_dict_input():
    """Defensive: garbage input must not crash the caller."""
    assert pc.get_strategy_param("not_a_dict", "breakout",
                                    "stop_loss_pct", 0.1) == 0.1
    assert pc.get_strategy_param(42, "breakout",
                                    "stop_loss_pct", 0.1) == 0.1


def test_get_strategy_param_returns_zero_value_correctly():
    """A legitimate 0 in the tier table must NOT trigger the default
    (avoid the classic ``or default`` bug)."""
    # Add a temporary 0-value override and verify it returns
    pc.TIER_STRATEGY_PARAMS.setdefault("__test_tier", {})["test_strat"] = {
        "test_param": 0,
    }
    try:
        val = pc.get_strategy_param(
            {"name": "__test_tier"}, "test_strat", "test_param", 999)
        assert val == 0
    finally:
        del pc.TIER_STRATEGY_PARAMS["__test_tier"]


# ============================================================================
# Schema integrity — every TIER_DEFAULTS entry has a TIER_STRATEGY_PARAMS row
# ============================================================================

def test_every_tier_has_a_strategy_params_table():
    """Pt.38 invariant: every tier in TIER_DEFAULTS must have a
    corresponding entry in TIER_STRATEGY_PARAMS (even if minimal).
    Otherwise tier detection silently falls back to defaults for
    that account size."""
    tier_names = {t["name"] for t in pc.TIER_DEFAULTS}
    param_keys = set(pc.TIER_STRATEGY_PARAMS.keys())
    missing = tier_names - param_keys
    assert not missing, (
        f"TIER_STRATEGY_PARAMS missing entries for tiers: {missing}")


def test_no_orphan_tier_names_in_params_table():
    """Inverse pin: every key in TIER_STRATEGY_PARAMS must correspond
    to a real tier in TIER_DEFAULTS."""
    tier_names = {t["name"] for t in pc.TIER_DEFAULTS}
    orphans = set(pc.TIER_STRATEGY_PARAMS.keys()) - tier_names
    assert not orphans, (
        f"TIER_STRATEGY_PARAMS has orphan tier names: {orphans}")
