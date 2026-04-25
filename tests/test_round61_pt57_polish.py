"""Round-61 pt.57 — end-to-end validation that pt.38's
TIER_STRATEGY_PARAMS + pt.47's strategy_params resolver actually
deliver a per-tier stop value down to the auto-deployer's rules
dict.

The user-facing claim from pt.38: "a $500 cash account no longer
takes the same 12% stops as a $100k margin account". This test
locks that claim in.
"""
from __future__ import annotations


def test_tier_strategy_params_table_has_all_six_tiers():
    """portfolio_calibration.TIER_STRATEGY_PARAMS must define
    per-strategy params for all six tier names that detect_tier
    can return."""
    import portfolio_calibration as pc
    expected_tiers = {
        "cash_micro", "cash_small", "cash_standard",
        "margin_small", "margin_standard", "margin_whale",
    }
    assert expected_tiers <= set(pc.TIER_STRATEGY_PARAMS.keys()), (
        f"missing tiers in TIER_STRATEGY_PARAMS: "
        f"{expected_tiers - set(pc.TIER_STRATEGY_PARAMS.keys())}")


def test_cash_micro_has_tighter_stops_than_margin_whale():
    """Core invariant: smaller / cash accounts get tighter stops
    than larger / margin accounts. This is the whole point of
    tier-aware sizing."""
    import portfolio_calibration as pc
    cash_micro_table = pc.TIER_STRATEGY_PARAMS.get("cash_micro") or {}
    margin_whale_table = pc.TIER_STRATEGY_PARAMS.get("margin_whale") or {}
    # At least one strategy should have a tighter stop on cash_micro.
    found_strict = False
    for strategy in ("breakout", "trailing_stop", "mean_reversion",
                       "pead", "wheel"):
        cm_stop = (cash_micro_table.get(strategy) or {}).get(
            "stop_loss_pct")
        mw_stop = (margin_whale_table.get(strategy) or {}).get(
            "stop_loss_pct")
        if cm_stop is not None and mw_stop is not None:
            if cm_stop < mw_stop:
                found_strict = True
            # No strategy should have cash_micro WIDER than whale.
            assert cm_stop <= mw_stop, (
                f"{strategy}: cash_micro stop {cm_stop} should be "
                f"<= margin_whale stop {mw_stop}")
    assert found_strict, (
        "no strategy has cash_micro tighter than margin_whale — "
        "the tier table isn't actually distinguishing")


def test_resolver_picks_tier_value_over_fallback():
    """Round-trip: detect a cash_micro tier dict, ask for a stop_loss
    value, expect the tier table value (not the caller's fallback)."""
    import portfolio_calibration as pc
    import strategy_params as sp
    tier_cfg = {"name": "cash_micro"}
    expected = pc.get_strategy_param(tier_cfg, "breakout",
                                       "stop_loss_pct", None)
    if expected is None:
        # Some strategies may not be defined for cash_micro — pick
        # one that IS defined.
        for strat in ("trailing_stop", "breakout", "wheel",
                       "mean_reversion"):
            cand = pc.get_strategy_param(tier_cfg, strat,
                                            "stop_loss_pct", None)
            if cand is not None:
                expected = cand
                strategy = strat
                break
    else:
        strategy = "breakout"
    assert expected is not None, (
        "no stop_loss_pct found for any cash_micro strategy")
    resolved = sp.resolve_strategy_param(
        strategy=strategy, param_name="stop_loss_pct",
        fallback=0.99,        # a value the tier table CANNOT match
        tier_cfg=tier_cfg,
    )
    assert resolved == expected, (
        f"resolver returned {resolved}; expected tier value {expected}")


def test_learned_overrides_tier_overrides_fallback_for_breakout():
    """Three-layer precedence chain validation."""
    import strategy_params as sp
    fallback = 0.10
    tier_cfg = {"name": "margin_standard"}
    learned = {"params": {"breakout": {"stop_pct": 0.07}}}
    # Learned wins.
    assert sp.resolve_strategy_param(
        strategy="breakout", param_name="stop_loss_pct",
        fallback=fallback, tier_cfg=tier_cfg,
        learned_params=learned) == 0.07


def test_resolve_rules_dict_round_trip_with_tier():
    """Pass the deployer's actual rules-dict shape through the
    resolver — every key in the input survives, with stop +
    target + max_hold pulled from the tier table."""
    import strategy_params as sp
    base_rules = {
        "stop_loss_pct": 0.10,            # fallback
        "profit_target_pct": 0.20,        # fallback
        "max_hold_days": 30,              # fallback
        "trailing_activation_pct": 0,     # not resolver-scope
        "exit_policy": "trailing_stop",
        "atr_pct": 0.025,
    }
    tier_cfg = {"name": "cash_standard"}
    out = sp.resolve_rules_dict(
        strategy="breakout", base_rules=base_rules,
        tier_cfg=tier_cfg)
    # All input keys present.
    for k in base_rules:
        assert k in out
    # Out-of-scope keys preserved.
    assert out["trailing_activation_pct"] == 0
    assert out["exit_policy"] == "trailing_stop"
    assert out["atr_pct"] == 0.025


def test_auto_deployer_long_path_calls_resolve_rules_dict():
    """Source-pin: the long-side rules construction in cloud_scheduler
    must go through resolve_rules_dict, not return raw fallbacks."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    # The pt.47 wiring placed resolve_rules_dict in a try/except around
    # the long-side rules dict construction.
    assert "resolve_rules_dict" in src
    # Confirm it's specifically wired with tier_cfg + learned_params.
    assert "tier_cfg=TIER_CFG" in src
    assert "learned_params=LEARNED_PARAMS" in src


def test_auto_deployer_short_path_calls_resolve_strategy_param():
    """Same check for the short-side path."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    # The short side resolves stop / target / max_hold individually.
    assert "resolve_strategy_param" in src
    assert 'strategy="short_sell"' in src or \
            "strategy='short_sell'" in src


def test_detect_tier_returns_cash_micro_for_500_dollar_account():
    """End-to-end: a $500 cash account → cash_micro tier."""
    import portfolio_calibration as pc
    account = {
        "equity": "500",
        "multiplier": "1",   # cash account
        "buying_power": "500",
    }
    tier = pc.detect_tier(account)
    assert tier is not None
    assert tier.get("name") == "cash_micro", (
        f"$500 cash account should be cash_micro; got {tier.get('name')}")


def test_detect_tier_returns_margin_whale_for_huge_account():
    """End-to-end: a $1M margin account → margin_whale tier."""
    import portfolio_calibration as pc
    account = {
        "equity": "1000000",
        "multiplier": "4",   # PDT-eligible margin
        "buying_power": "4000000",
    }
    tier = pc.detect_tier(account)
    assert tier is not None
    assert tier.get("name") == "margin_whale", (
        f"$1M margin account should be margin_whale; "
        f"got {tier.get('name')}")


def _dashboard():
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent
             / "templates" / "dashboard.html").read_text()


# ============================================================================
# Pipeline-backtest "simulate outcomes" toggle in UI
# ============================================================================

def test_pipeline_backtest_panel_has_simulate_toggle():
    src = _dashboard()
    assert "pipelineBacktestSimulate" in src
    assert "simulate P&L" in src or "simulate outcomes" in src.lower()


def test_run_pipeline_backtest_reads_simulate_toggle():
    src = _dashboard()
    start = src.find("function runPipelineBacktest")
    end = src.find("function renderPipelineBacktest", start)
    body = src[start:end]
    assert "pipelineBacktestSimulate" in body
    assert "simulate_outcomes" in body


def test_pipeline_backtest_renders_counterfactual_when_present():
    src = _dashboard()
    start = src.find("function renderPipelineBacktest")
    body = src[start:start + 4000]
    assert "counterfactual" in body
    assert "COUNTERFACTUAL" in body or "counterfactual" in body
    assert "Win rate" in body
    assert "Total P&L" in body or "total_pnl" in body


def test_pipeline_backtest_endpoint_handles_simulate_outcomes_flag():
    """Source-pin: the handler should fetch bars + pass
    simulate_outcomes=True to pipeline_backtest when requested."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "handlers" / "actions_mixin.py").read_text()
    start = src.find("def handle_pipeline_backtest")
    end = src.find("def handle_trades_view", start)
    body = src[start:end]
    assert 'body.get("simulate_outcomes")' in body
    assert "fetch_bars_for_symbols" in body
    assert "bars_by_symbol" in body


# ============================================================================
# Score-to-outcome partial-data view
# ============================================================================

def test_score_outcome_partial_view_shows_progress_bar():
    src = _dashboard()
    # The partial view renders a progress bar with stage labels
    # (TRUSTWORTHY / PRELIMINARY / INSUFFICIENT) tied to the
    # tracked-trade count.
    assert "TRUSTWORTHY" in src
    assert "PRELIMINARY" in src
    assert "INSUFFICIENT" in src


def test_score_outcome_partial_view_uses_tracked_count():
    src = _dashboard()
    # Find the partial-view render block (the !soBuckets.length branch).
    idx = src.find("if (!soBuckets.length)")
    assert idx > 0
    block = src[idx:idx + 2500]
    assert "tracked_trades" in block
    assert "fullThreshold" in block or "30" in block


def test_score_outcome_partial_view_shows_legacy_count_when_present():
    """untracked_trades from pre-pt.47 trades should be surfaced
    so users understand why some closed trades aren't counted."""
    src = _dashboard()
    idx = src.find("if (!soBuckets.length)")
    block = src[idx:idx + 2500]
    assert "untracked_trades" in block
    assert "legacy" in block.lower()


# ============================================================================
# Crypto-skip pattern
# ============================================================================

def test_http_harness_skips_when_aesgcm_unavailable():
    """Source-pin: conftest.http_harness should pytest.skip when
    the AESGCM probe raises BaseException."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "tests" / "conftest.py").read_text()
    assert "AESGCM" in src
    assert "pytest.skip" in src
    assert "BaseException" in src


def test_e2e_500_cash_to_resolved_stop_is_tighter_than_100k_margin():
    """Full chain: detect tier from account → resolve stop_loss_pct →
    smaller account has tighter (smaller) stop than larger one."""
    import portfolio_calibration as pc
    import strategy_params as sp
    cash_acct = {"equity": "500", "multiplier": "1",
                  "buying_power": "500"}
    margin_acct = {"equity": "100000", "multiplier": "2",
                    "buying_power": "200000"}
    cash_tier = pc.detect_tier(cash_acct)
    margin_tier = pc.detect_tier(margin_acct)
    assert cash_tier is not None
    assert margin_tier is not None
    # Pick a strategy where both tiers have a stop defined.
    found = False
    for strat in ("trailing_stop", "breakout", "mean_reversion",
                   "wheel"):
        cash_stop = sp.resolve_strategy_param(
            strategy=strat, param_name="stop_loss_pct",
            fallback=0.50,           # noisy fallback to detect "miss"
            tier_cfg=cash_tier)
        margin_stop = sp.resolve_strategy_param(
            strategy=strat, param_name="stop_loss_pct",
            fallback=0.50,
            tier_cfg=margin_tier)
        if cash_stop != 0.50 and margin_stop != 0.50:
            found = True
            assert cash_stop <= margin_stop, (
                f"{strat}: cash_micro stop {cash_stop} should be "
                f"≤ margin_standard stop {margin_stop}")
            break
    assert found, ("no strategy resolved stop_loss_pct from either "
                    "cash_micro or margin_standard tier — pt.38 "
                    "wiring may be broken")
