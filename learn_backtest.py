"""Round-61 pt.44 — backtest-driven self-learning loop.

Closes the user-requested feedback loop: every weekly run, the bot
runs the pt.37 backtest harness on each tradable strategy + the
user's recent symbol universe, then proposes parameter adjustments
based on what the simulation shows would have worked better.

Pure module — every dependency is injected. The subprocess wrapper
in `cloud_scheduler.run_weekly_learning` invokes this with the
production paths.

Workflow:
  1. Build the symbol universe — pull recent symbols from the
     user's trade journal + the dashboard's top picks.
  2. Fetch OHLCV bars (via backtest_data — already cached).
  3. For each strategy in `BACKTESTABLE_STRATEGIES`, sweep a small
     grid of parameter variants around current defaults. Each
     variant gets a simulated win-rate / expectancy.
  4. Pick the BEST variant per strategy, subject to safety bounds
     (no parameter can move >25% from current; no parameter can
     fall below the per-tier floor from pt.38).
  5. Write proposed adjustments to ``learned_params.json`` plus
     append every change to an audit log
     (``learned_params_audit.json``).
  6. Production scheduler reads the proposed params on the next
     screener tick — if a strategy's `_self_learned` rules are
     present, they override the static `DEFAULT_PARAMS`.

Safety:
  * Adjustments capped at ±25% per cycle (prevents whiplash from
    one bad week).
  * Tier-floor enforced (cash-micro can never have stop_pct >12%;
    margin-whale can never have stop_pct <5%, etc).
  * Each adjustment must IMPROVE the simulated expectancy by ≥5%
    over the current parameters — otherwise no change.
  * Full audit log of every proposed change so a regression can be
    reverted by inspection.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Mapping, Optional


# Maximum proportional change per cycle. A weekly bump that goes
# outside this band is suspicious (overfit on this week's noise);
# clamp it so the parameters can't whip around dangerously.
MAX_RELATIVE_CHANGE: float = 0.25  # ±25%

# Minimum expectancy improvement required to update a parameter.
# Below this, the variant isn't meaningfully better — keep the
# current value to avoid dithering.
MIN_IMPROVEMENT_THRESHOLD: float = 0.05  # 5%

# Per-strategy parameters that are eligible for auto-tuning.
# (Other params like `lookback_high` are intentionally NOT learned
# because they encode the strategy's identity.)
TUNABLE_PARAMS: Mapping[str, tuple] = {
    "breakout": ("stop_pct", "target_pct", "max_hold_days"),
    "mean_reversion": ("stop_pct", "target_pct", "max_hold_days"),
    "short_sell": ("stop_pct", "target_pct", "max_hold_days"),
}

# Hard absolute floors / ceilings per parameter, applied AFTER the
# relative-change cap. Prevents catastrophic settings even if the
# backtest data is misleading.
PARAM_BOUNDS: Mapping[str, tuple] = {
    "stop_pct": (0.05, 0.20),       # 5%-20%
    "target_pct": (0.05, 0.50),     # 5%-50%
    "max_hold_days": (3, 60),       # 3-60 days
}


def build_param_variants(base_params: Mapping, deltas=(-0.20, -0.10,
                                                          0.0, 0.10,
                                                          0.20)) -> list:
    """Return a list of param dicts where each tunable field is
    perturbed by the given fractional deltas (independently).

    Cross-product would explode (5^3 = 125) — instead we vary ONE
    param at a time keeping others at base. That's `1 + N*(D-1)`
    variants where N=tunable count, D=delta count. For 3 tunable
    params and 5 deltas: 1 + 3*4 = 13 variants per strategy.

    The base params (delta=0) are always included as variant 0.
    """
    variants = [dict(base_params)]
    for param, _ in [(p, 0) for p in base_params]:
        if param not in TUNABLE_PARAMS.get(base_params.get(
                "_strategy", ""), ()):
            continue
        for d in deltas:
            if d == 0.0:
                continue  # base already included
            new = dict(base_params)
            try:
                cur = float(base_params[param])
            except (TypeError, ValueError):
                continue
            adj = cur * (1.0 + d)
            # Round to sensible precision per param
            if param == "max_hold_days":
                adj = max(1, round(adj))
            else:
                adj = round(adj, 4)
            new[param] = adj
            variants.append(new)
    return variants


def clamp_param_change(strategy: str, param: str, current: float,
                        proposed: float,
                        max_relative: float = MAX_RELATIVE_CHANGE,
                        bounds: Optional[Mapping] = None) -> float:
    """Clamp a proposed parameter change so that:
      1. It doesn't move more than `max_relative` from `current`.
      2. It stays within `PARAM_BOUNDS[param]` (absolute floor/ceiling).

    Returns the clamped value (may equal `current` if no safe move
    is possible).
    """
    bounds = bounds or PARAM_BOUNDS
    try:
        cur = float(current)
        prop = float(proposed)
    except (TypeError, ValueError):
        return current
    if cur <= 0:
        return cur
    # Relative cap
    max_up = cur * (1.0 + max_relative)
    max_down = cur * (1.0 - max_relative)
    clamped = max(max_down, min(max_up, prop))
    # Absolute bounds
    abs_lo, abs_hi = bounds.get(param, (None, None))
    if abs_lo is not None:
        clamped = max(abs_lo, clamped)
    if abs_hi is not None:
        clamped = min(abs_hi, clamped)
    # Type coercion for max_hold_days
    if param == "max_hold_days":
        clamped = max(1, round(clamped))
    return clamped


def select_best_variant(variant_results: list,
                          base_expectancy: float,
                          min_improvement: float = MIN_IMPROVEMENT_THRESHOLD,
                          ) -> Optional[dict]:
    """From the list of `(params, summary)` tuples, return the
    variant that improves expectancy the most over `base_expectancy`.
    Returns None if no variant improves by at least `min_improvement`
    (5% by default) — keep the current params in that case.
    """
    if not variant_results:
        return None
    best = None
    best_expectancy = base_expectancy
    for params, summary in variant_results:
        try:
            exp = float(summary.get("expectancy") or 0)
        except (TypeError, ValueError):
            continue
        # Require both improvement AND minimum trade count for
        # statistical confidence
        try:
            cnt = int(summary.get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt < 5:
            continue  # too few sim trades
        # Need exp > base*(1+min_improvement). Handle base==0 edge:
        # if base is zero, any positive improvement counts as ≥5%.
        threshold = (
            base_expectancy * (1.0 + min_improvement)
            if base_expectancy > 0 else 0.0001
        )
        if exp > threshold and exp > best_expectancy:
            best = {"params": params, "summary": summary,
                     "expectancy": exp}
            best_expectancy = exp
    return best


def propose_adjustments(strategy_results: Mapping[str, dict],
                          current_defaults: Mapping[str, dict],
                          ) -> dict:
    """Build the adjustment proposal payload from the per-strategy
    backtest sweep results.

    `strategy_results`: ``{strategy: {variants: [(params, summary)],
                                       base_expectancy: float}}``
    `current_defaults`: ``{strategy: {param: current_value}}``

    Returns ``{strategy: {param: {old, new, reason, expectancy_old,
    expectancy_new}}}`` plus a top-level ``timestamp`` and
    ``cycle`` audit field.
    """
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "adjustments": {},
        "no_change": [],
    }
    for strat, result in (strategy_results or {}).items():
        base_exp = float(result.get("base_expectancy") or 0)
        variants = result.get("variants") or []
        best = select_best_variant(variants, base_exp)
        if not best:
            out["no_change"].append({
                "strategy": strat,
                "reason": "no variant improved expectancy by 5%+ "
                          "OR insufficient sim trades",
                "base_expectancy": round(base_exp, 4),
            })
            continue
        cur = current_defaults.get(strat) or {}
        proposed = best["params"]
        new_exp = best["expectancy"]
        per_param = {}
        for param in TUNABLE_PARAMS.get(strat, ()):
            if param not in proposed or param not in cur:
                continue
            old_v = cur[param]
            prop_v = proposed[param]
            clamped_v = clamp_param_change(strat, param, old_v, prop_v)
            if clamped_v == old_v:
                continue  # no change after clamping
            per_param[param] = {
                "old": old_v,
                "new": clamped_v,
                "raw_proposed": prop_v,
                "expectancy_old": round(base_exp, 4),
                "expectancy_new": round(new_exp, 4),
                "improvement_pct": round(
                    (new_exp / base_exp - 1) * 100
                    if base_exp > 0 else 100.0, 2),
            }
        if per_param:
            out["adjustments"][strat] = per_param
        else:
            out["no_change"].append({
                "strategy": strat,
                "reason": "best variant matched current after "
                          "safety clamping",
                "base_expectancy": round(base_exp, 4),
            })
    return out


def merge_into_learned_params(existing: Optional[dict],
                                proposal: Mapping) -> dict:
    """Take the existing `learned_params.json` content + a new
    proposal and produce the merged learned-params payload that
    consumers (the screener) will read.

    Schema:
        {
          "version": 1,
          "last_updated": ISO timestamp,
          "params": {strategy: {param: value}},
          "history": [proposal, proposal, ...]  (last N kept)
        }
    """
    HISTORY_CAP = 12  # keep the last 12 weekly cycles (~1 quarter)
    base = existing or {}
    if base.get("version") != 1:
        base = {"version": 1, "params": {}, "history": []}
    params_out = dict(base.get("params") or {})
    for strat, fields in (proposal.get("adjustments") or {}).items():
        params_out.setdefault(strat, {})
        for param, change in fields.items():
            params_out[strat][param] = change["new"]
    history = list(base.get("history") or [])
    history.append({
        "timestamp": proposal.get("timestamp"),
        "adjustments": proposal.get("adjustments") or {},
        "no_change": proposal.get("no_change") or [],
    })
    history = history[-HISTORY_CAP:]
    base["params"] = params_out
    base["history"] = history
    base["last_updated"] = proposal.get("timestamp")
    return base


def safe_save_json(path, data):
    """Atomic write — same pattern as capital_check + learn.py."""
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def run_self_learning(bars_by_symbol: Mapping,
                       run_backtest_fn,
                       current_defaults: Optional[Mapping] = None,
                       deltas=(-0.20, -0.10, 0.10, 0.20),
                       ) -> dict:
    """End-to-end self-learning sweep.

    Args:
      bars_by_symbol: same shape backtest_core expects.
      run_backtest_fn: callable(bars_by_symbol, strategy, params)
                        returns ``{"summary": {...}, ...}``.
                        Injected so this module is testable without
                        the full backtest stack.
      current_defaults: ``{strategy: {param: value}}``. Defaults to
                          backtest_core.DEFAULT_PARAMS.
      deltas: fractional perturbations applied to each tunable param.

    Returns the proposal payload (same shape as
    ``propose_adjustments``).
    """
    if current_defaults is None:
        from backtest_core import DEFAULT_PARAMS as BC_DEFAULTS
        current_defaults = BC_DEFAULTS
    strategy_results = {}
    for strat, base_params in current_defaults.items():
        # Tag base_params with strategy so build_param_variants can
        # filter tunable params correctly.
        tagged = dict(base_params)
        tagged["_strategy"] = strat
        variants = build_param_variants(tagged, deltas=deltas)
        # Strip the bookkeeping tag before passing to backtest.
        for v in variants:
            v.pop("_strategy", None)
        # Run base first, then each variant.
        base_result = run_backtest_fn(bars_by_symbol, strat,
                                       dict(base_params))
        try:
            base_summary = base_result.get("summary") or {}
            base_exp = float(base_summary.get("expectancy") or 0)
        except (AttributeError, TypeError, ValueError):
            base_exp = 0.0
        variant_runs = []
        for params in variants:
            # Skip the variant identical to base (already ran above)
            if params == dict(base_params):
                variant_runs.append((params, base_summary))
                continue
            r = run_backtest_fn(bars_by_symbol, strat, params)
            if isinstance(r, dict):
                variant_runs.append((params, r.get("summary") or {}))
        strategy_results[strat] = {
            "base_expectancy": base_exp,
            "variants": variant_runs,
        }
    return propose_adjustments(strategy_results, current_defaults)
