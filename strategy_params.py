"""Round-61 pt.47: per-strategy parameter resolution.

Single source of truth for "what stop / target / hold value should
this strategy use for this user right now?". Three-layer lookup,
highest precedence first:

    1. learned_params (per-user, written by the pt.44 self-learning
       loop)
    2. TIER_STRATEGY_PARAMS (pt.38 — per-tier risk calibration)
    3. caller-provided fallback (the legacy hardcoded value)

Pure module — no I/O. Callers load JSON and pass dicts in.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


# learned_params.json uses short names; TIER_STRATEGY_PARAMS + the
# deployer's `rules` dict use the long form. Map between them for the
# learned_params lookup.
_LEARNED_NAME_MAP: Mapping[str, str] = {
    "stop_loss_pct": "stop_pct",
    "profit_target_pct": "target_pct",
    "max_hold_days": "max_hold_days",
}


def _read_learned_param(learned_params: Optional[Mapping[str, Any]],
                         strategy: str, param_name: str) -> Any:
    """Look up a learned param. Returns None if absent."""
    if not isinstance(learned_params, Mapping):
        return None
    # Accept either {"params": {...}} (full file) or {...} (just the
    # params subdict).
    params = learned_params.get("params") if "params" in learned_params else learned_params
    if not isinstance(params, Mapping):
        return None
    strat_params = params.get(strategy)
    if not isinstance(strat_params, Mapping):
        return None
    short_name = _LEARNED_NAME_MAP.get(param_name, param_name)
    val = strat_params.get(short_name)
    return val


def _read_tier_param(tier_cfg: Optional[Mapping[str, Any]],
                      strategy: str, param_name: str) -> Any:
    """Look up a tier-specific param via portfolio_calibration helper."""
    if not isinstance(tier_cfg, Mapping):
        return None
    try:
        import portfolio_calibration as _pc
        return _pc.get_strategy_param(tier_cfg, strategy, param_name, None)
    except Exception:
        return None


def resolve_strategy_param(*,
                            strategy: str,
                            param_name: str,
                            fallback: Any,
                            tier_cfg: Optional[Mapping[str, Any]] = None,
                            learned_params: Optional[Mapping[str, Any]] = None,
                            ) -> Any:
    """Return the effective per-strategy parameter for `param_name`.

    Precedence (first non-None wins):
        1. learned_params[strategy][short_name]
        2. TIER_STRATEGY_PARAMS[tier][strategy][param_name]
        3. fallback (the caller's legacy default)

    All inputs are dict-or-None; missing data falls through to the
    next layer. Callers should pass `learned_params` as the loaded
    `learned_params.json` (or just its `"params"` sub-dict) and
    `tier_cfg` as the user's `_tier_cfg` from
    `portfolio_calibration.detect_tier` + `apply_user_overrides`.
    """
    learned = _read_learned_param(learned_params, strategy, param_name)
    if learned is not None:
        return learned
    tier = _read_tier_param(tier_cfg, strategy, param_name)
    if tier is not None:
        return tier
    return fallback


def resolve_rules_dict(*,
                        strategy: str,
                        base_rules: Mapping[str, Any],
                        tier_cfg: Optional[Mapping[str, Any]] = None,
                        learned_params: Optional[Mapping[str, Any]] = None,
                        ) -> dict:
    """Return a copy of `base_rules` with stop_loss_pct,
    profit_target_pct, and max_hold_days resolved through the
    learned/tier/fallback precedence chain. Other rule keys pass
    through unchanged.

    This is the high-level helper for the auto-deployer to use when
    building a new strategy file's `rules` dict — wraps the legacy
    hardcoded values with one call.
    """
    out = dict(base_rules)
    for pname in ("stop_loss_pct", "profit_target_pct", "max_hold_days"):
        if pname in base_rules:
            fallback = base_rules.get(pname)
            resolved = resolve_strategy_param(
                strategy=strategy, param_name=pname, fallback=fallback,
                tier_cfg=tier_cfg, learned_params=learned_params,
            )
            out[pname] = resolved
    return out
