"""
Round-50: portfolio auto-calibration.

Reads Alpaca's /v2/account endpoint to determine the user's true
account state (cash vs margin, PDT flag, shorting enabled, equity,
settled cash, day-trade buying power) and returns a calibrated
config dict that gates strategies, position sizing, and features.

No more guessing from equity alone — Alpaca's `multiplier` field is
the ground truth:
  * multiplier == 1 → CASH account
  * multiplier == 2 → Margin (RegT, non-PDT)
  * multiplier == 4 → Margin (PDT-qualified, ≥$25k)

Tiers are ACCOUNT_TYPE × EQUITY:
  * Cash $500-$2k       (micro)   — 1-2 positions, fractional ON, 15% max
  * Cash $2k-$25k       (small)   — 3-5 positions, fractional ON, 10% max
  * Cash $25k+          (standard)— 5-8 positions, optional fractional, 7% max
  * Margin $2k-$25k     (small-m) — PDT rules apply; shorts allowed (ETB-only)
  * Margin $25k+        (large-m) — full features, ALL 6 strategies

User overrides from guardrails.json are respected. The calibration
layer provides DEFAULTS; user choices win.
"""
from __future__ import annotations

from typing import Optional

# Tier definitions — ordered low → high. First match wins.
# Each tier specifies the DEFAULT config; user overrides in guardrails.json
# take precedence.
#
# Strategy keys match the existing config (trailing_stop, breakout,
# mean_reversion, pead, copy_trading, wheel, short_sell).
TIER_DEFAULTS = [
    # -------- CASH ACCOUNTS --------
    {
        "name": "cash_micro",
        "display": "🌱 Cash Micro ($500-$2k)",
        "account_type": "cash",
        "equity_min": 500,
        "equity_max": 2000,
        "max_positions": 2,
        "max_position_pct": 0.15,
        "strategies_enabled": ["trailing_stop", "breakout", "mean_reversion"],
        "fractional_default": True,
        "min_stock_price": 0,  # fractional unlocks any price
        "wheel_enabled": False,
        "short_enabled": False,
        "long_options_enabled": True,  # if options_approved_level >= 2
        "pdt_applies": False,  # cash accounts: no PDT ever
        "settled_funds_required": True,  # cash: Good Faith Violation risk
        "description": "Tiny account. Fractional enabled for full stock access. "
                       "No options wheel (not enough cash to cover puts). "
                       "No shorting (not allowed on cash accounts).",
    },
    {
        "name": "cash_small",
        "display": "🌿 Cash Small ($2k-$25k)",
        "account_type": "cash",
        "equity_min": 2000,
        "equity_max": 25000,
        "max_positions": 5,
        "max_position_pct": 0.10,
        "strategies_enabled": ["trailing_stop", "breakout", "mean_reversion",
                                "pead", "copy_trading"],
        "fractional_default": True,
        "min_stock_price": 0,
        "wheel_enabled": False,  # wheel re-evaluated dynamically if cash ≥ $5k
        "short_enabled": False,
        "long_options_enabled": True,
        "pdt_applies": False,
        "settled_funds_required": True,
        "description": "Small cash account. Fractional on for full stock "
                       "diversification. Options long-only (no wheel CSP "
                       "until $25k+ cash). No shorting on cash accounts.",
    },
    {
        "name": "cash_standard",
        "display": "🌳 Cash Standard ($25k+)",
        "account_type": "cash",
        "equity_min": 25000,
        "equity_max": 10**12,
        "max_positions": 8,
        "max_position_pct": 0.07,
        "strategies_enabled": ["trailing_stop", "breakout", "mean_reversion",
                                "pead", "copy_trading", "wheel"],
        "fractional_default": False,  # whole-share default at this size
        "min_stock_price": 10,
        "wheel_enabled": True,
        "short_enabled": False,
        "long_options_enabled": True,
        "pdt_applies": False,
        "settled_funds_required": True,
        "description": "Standard cash account. Full wheel strategy enabled. "
                       "Fractional off by default (cleaner tax lots); can "
                       "opt-in via Settings. No shorting without margin.",
    },
    # -------- MARGIN ACCOUNTS --------
    {
        "name": "margin_small",
        "display": "📘 Margin Small ($2k-$25k, PDT rules apply)",
        "account_type": "margin",
        "equity_min": 2000,
        "equity_max": 25000,
        "max_positions": 6,
        "max_position_pct": 0.08,
        "strategies_enabled": ["trailing_stop", "breakout", "mean_reversion",
                                "pead", "copy_trading", "short_sell"],
        "fractional_default": True,
        "min_stock_price": 3,  # Alpaca doesn't allow margin on <$3
        "wheel_enabled": False,  # deferred to cash ≥ $25k tier; margin wheel possible but edge case
        "short_enabled": True,   # margin + ≥$2k equity gate met
        "long_options_enabled": True,
        "pdt_applies": True,  # ← PDT applies below $25k margin
        "settled_funds_required": False,  # margin: no T+1 constraint
        "description": "Small margin account. Shorting enabled (ETB-only). "
                       "PDT rules active: bot tracks day_trades_remaining "
                       "and holds overnight when close to the 3-in-5 limit.",
    },
    {
        "name": "margin_standard",
        "display": "🏛️ Margin Standard ($25k+, no PDT limit)",
        "account_type": "margin",
        "equity_min": 25000,
        "equity_max": 500000,
        "max_positions": 10,
        "max_position_pct": 0.06,
        "strategies_enabled": ["trailing_stop", "breakout", "mean_reversion",
                                "pead", "copy_trading", "wheel", "short_sell"],
        "fractional_default": False,
        "min_stock_price": 3,
        "wheel_enabled": True,
        "short_enabled": True,
        "long_options_enabled": True,
        "pdt_applies": False,  # ≥$25k → PDT limits don't bite
        "settled_funds_required": False,
        "description": "Standard margin account. All 6 strategies enabled "
                       "including shorts with up to 4x day-trading BP. No "
                       "PDT limits at this equity.",
    },
    {
        "name": "margin_whale",
        "display": "🐋 Margin Whale ($500k+)",
        "account_type": "margin",
        "equity_min": 500000,
        "equity_max": 10**12,
        "max_positions": 15,
        "max_position_pct": 0.04,
        "strategies_enabled": ["trailing_stop", "breakout", "mean_reversion",
                                "pead", "copy_trading", "wheel", "short_sell"],
        "fractional_default": False,
        "min_stock_price": 3,
        "wheel_enabled": True,
        "short_enabled": True,
        "long_options_enabled": True,
        "pdt_applies": False,
        "settled_funds_required": False,
        "single_ticker_cap_pct": 0.08,  # hard cap: no single ticker > 8% of equity
        "description": "Whale margin account. Heavy diversification (15 "
                       "positions max) + single-ticker concentration cap "
                       "at 8% to prevent large losses from one name.",
    },
]


# Round-61 pt.38: per-strategy stop / target / max-hold-days
# overrides keyed by tier name. Lets the auto-deployer pick risk
# parameters that match the user's account size — micro accounts
# get tighter risk-per-trade (~5%) so a single loss doesn't blow
# out the account, while standard accounts get wider stops (~10-12%)
# to avoid getting whipsawed out of valid setups.
#
# Usage: ``get_strategy_param(tier_cfg, strategy, param_name,
# default)`` returns the tier-specific value if present, falling
# back to ``default``. Consumers (cloud_scheduler.run_auto_deployer,
# capital_check, monitor_strategies) pass the already-loaded
# ``tier_cfg`` so this lookup is O(1) per call.
#
# Schema: {tier_name: {strategy_name: {param_name: value}}}.
# Strategies absent from a tier's table fall through to the global
# DEFAULT_STRATEGY_PARAMS below. Same for params absent on a
# present-strategy entry.
#
# Why these specific values:
#   * **Micro tier** (Cash $500-$2k): 5-6% stops to limit absolute
#     dollar loss per trade ($25 on $500 risk). Smaller targets
#     (15-20%) since the account can't afford many drawdowns.
#     Shorter hold (10 days) to free capital faster.
#   * **Small tier** ($2k-$25k cash, $2k-$25k margin): 7-8% stops,
#     20-25% targets. Goldilocks zone — enough capital to wait
#     out drawdowns, not so much that wide stops are wasteful.
#   * **Standard tier** ($25k+): 10-12% stops, 25-35% targets.
#     Wider stops to avoid noise-driven whipsaws on the larger
#     positions a $25k+ account can afford.
#
# Adding a new strategy here is the single edit point — one place
# to set tier-aware defaults. User overrides in guardrails.json
# still win (apply_user_overrides happens AFTER calibration so the
# user can flatten any of these).
TIER_STRATEGY_PARAMS = {
    "cash_micro": {
        "breakout":       {"stop_loss_pct": 0.05, "profit_target_pct": 0.20, "max_hold_days": 10},
        "mean_reversion": {"stop_loss_pct": 0.06, "profit_target_pct": 0.10, "max_hold_days": 7},
        "trailing_stop":  {"stop_loss_pct": 0.06, "trail_distance_pct": 0.05},
        "pead":           {"stop_loss_pct": 0.08, "max_hold_days": 30},
        "short_sell":     {"stop_loss_pct": 0.05, "profit_target_pct": 0.10, "max_hold_days": 7},
    },
    "cash_small": {
        "breakout":       {"stop_loss_pct": 0.07, "profit_target_pct": 0.25, "max_hold_days": 14},
        "mean_reversion": {"stop_loss_pct": 0.07, "profit_target_pct": 0.12, "max_hold_days": 10},
        "trailing_stop":  {"stop_loss_pct": 0.08, "trail_distance_pct": 0.06},
        "pead":           {"stop_loss_pct": 0.08, "max_hold_days": 45},
        "copy_trading":   {"stop_loss_pct": 0.08, "max_hold_days": 30},
    },
    "cash_standard": {
        "breakout":       {"stop_loss_pct": 0.10, "profit_target_pct": 0.30, "max_hold_days": 21},
        "mean_reversion": {"stop_loss_pct": 0.08, "profit_target_pct": 0.15, "max_hold_days": 14},
        "trailing_stop":  {"stop_loss_pct": 0.10, "trail_distance_pct": 0.07},
        "pead":           {"stop_loss_pct": 0.08, "max_hold_days": 60},
        "copy_trading":   {"stop_loss_pct": 0.10, "max_hold_days": 45},
        "wheel":          {"profit_target_pct": 0.50, "max_hold_days": 45},
    },
    "margin_small": {
        "breakout":       {"stop_loss_pct": 0.07, "profit_target_pct": 0.22, "max_hold_days": 14},
        "mean_reversion": {"stop_loss_pct": 0.07, "profit_target_pct": 0.12, "max_hold_days": 10},
        "trailing_stop":  {"stop_loss_pct": 0.08, "trail_distance_pct": 0.06},
        "pead":           {"stop_loss_pct": 0.08, "max_hold_days": 45},
        "copy_trading":   {"stop_loss_pct": 0.08, "max_hold_days": 30},
        "short_sell":     {"stop_loss_pct": 0.07, "profit_target_pct": 0.15, "max_hold_days": 10},
    },
    "margin_standard": {
        "breakout":       {"stop_loss_pct": 0.10, "profit_target_pct": 0.30, "max_hold_days": 21},
        "mean_reversion": {"stop_loss_pct": 0.08, "profit_target_pct": 0.15, "max_hold_days": 14},
        "trailing_stop":  {"stop_loss_pct": 0.10, "trail_distance_pct": 0.07},
        "pead":           {"stop_loss_pct": 0.08, "max_hold_days": 60},
        "copy_trading":   {"stop_loss_pct": 0.10, "max_hold_days": 45},
        "short_sell":     {"stop_loss_pct": 0.08, "profit_target_pct": 0.18, "max_hold_days": 14},
        "wheel":          {"profit_target_pct": 0.50, "max_hold_days": 45},
    },
    "margin_whale": {
        "breakout":       {"stop_loss_pct": 0.12, "profit_target_pct": 0.35, "max_hold_days": 30},
        "mean_reversion": {"stop_loss_pct": 0.10, "profit_target_pct": 0.18, "max_hold_days": 21},
        "trailing_stop":  {"stop_loss_pct": 0.12, "trail_distance_pct": 0.08},
        "pead":           {"stop_loss_pct": 0.10, "max_hold_days": 60},
        "copy_trading":   {"stop_loss_pct": 0.10, "max_hold_days": 45},
        "short_sell":     {"stop_loss_pct": 0.10, "profit_target_pct": 0.20, "max_hold_days": 14},
        "wheel":          {"profit_target_pct": 0.50, "max_hold_days": 45},
    },
}


def get_strategy_param(tier_cfg, strategy, param_name, default=None):
    """Round-61 pt.38: return the tier-aware value for
    ``param_name`` on ``strategy``, or ``default`` if no tier
    override exists.

    ``tier_cfg`` — the dict returned by ``apply_user_overrides`` (or
        ``detect_tier``). Must carry a ``name`` key (e.g.
        ``"cash_standard"``); falls open to ``default`` otherwise.
    ``strategy`` — strategy name (one of constants.STRATEGY_NAMES).
    ``param_name`` — e.g. ``"stop_loss_pct"``, ``"profit_target_pct"``,
        ``"max_hold_days"``, ``"trail_distance_pct"``.
    ``default`` — fallback value when the tier or param isn't in
        the table.

    User overrides applied via ``apply_user_overrides`` LIVE on the
    tier_cfg dict as top-level keys (round-50 schema). They WIN over
    the per-strategy table — caller pattern is:
        guardrails.get(name) or get_strategy_param(...) or default

    See cloud_scheduler.run_auto_deployer for the wiring.
    """
    if not isinstance(tier_cfg, dict):
        return default
    name = tier_cfg.get("name")
    if not name:
        return default
    tier_table = TIER_STRATEGY_PARAMS.get(name) or {}
    strat_params = tier_table.get(strategy) or {}
    if param_name in strat_params:
        return strat_params[param_name]
    return default


def detect_tier(account: dict) -> dict:
    """Given an Alpaca /v2/account response dict, return the matching
    tier-defaults dict (deep-copied — safe to mutate). Returns None if
    no tier matches (e.g., under $500 equity or Alpaca returned nothing).

    Account type detection:
      * multiplier == 1  → cash
      * multiplier in (2, 4) → margin
    Equity is read from the 'equity' field (current portfolio value).
    """
    if not isinstance(account, dict):
        return None
    try:
        multiplier = int(float(account.get("multiplier") or 1))
        equity = float(account.get("equity") or 0)
    except (TypeError, ValueError):
        return None
    if equity < 500:
        return None  # below the smallest tier
    acct_type = "cash" if multiplier == 1 else "margin"
    for tier in TIER_DEFAULTS:
        if tier["account_type"] != acct_type:
            continue
        if tier["equity_min"] <= equity < tier["equity_max"]:
            # Deep copy so callers can mutate without affecting defaults
            import copy
            result = copy.deepcopy(tier)
            # Stamp live fields
            result["_detected_equity"] = equity
            result["_detected_multiplier"] = multiplier
            result["_detected_pattern_day_trader"] = bool(
                account.get("pattern_day_trader", False))
            result["_detected_shorting_enabled"] = bool(
                account.get("shorting_enabled", False))
            result["_detected_day_trades_remaining"] = _safe_int(
                account.get("day_trades_remaining"), None)
            result["_detected_cash"] = float(account.get("cash") or 0)
            result["_detected_cash_withdrawable"] = float(
                account.get("cash_withdrawable")
                or account.get("cash") or 0)
            result["_detected_buying_power"] = float(
                account.get("buying_power") or 0)
            return result
    return None


def apply_user_overrides(tier: dict, guardrails: dict) -> dict:
    """Merge user overrides from guardrails.json into the tier defaults.
    User choices win. Returns the final config dict.

    Recognised override keys in guardrails.json:
      * max_positions, max_position_pct
      * strategies_enabled (list of strategy names — whitelist)
      * fractional_enabled
      * min_stock_price
      * wheel_enabled, short_enabled
    """
    if not tier:
        return {}
    cfg = dict(tier)
    if not isinstance(guardrails, dict):
        return cfg
    for key in ("max_positions", "max_position_pct", "min_stock_price",
                "fractional_enabled", "wheel_enabled", "short_enabled",
                "long_options_enabled", "single_ticker_cap_pct"):
        if key in guardrails:
            cfg[f"user_override_{key}"] = True
            # fractional_enabled renames fractional_default internally
            if key == "fractional_enabled":
                cfg["fractional_default"] = bool(guardrails[key])
            else:
                cfg[key] = guardrails[key]
    if "strategies_enabled" in guardrails:
        # Validate: only allow strategies Alpaca-legal for this account type
        allowed = set(tier["strategies_enabled"])
        if not tier.get("short_enabled", False):
            # User tried to enable shorts on an account where it's
            # illegal — strip it silently (UI should have blocked earlier).
            user_list = [s for s in guardrails["strategies_enabled"]
                         if s != "short_sell"]
        else:
            user_list = list(guardrails["strategies_enabled"])
        cfg["strategies_enabled"] = user_list
        cfg["user_override_strategies_enabled"] = True
    # Wheel / short overrides also must respect account-type rules
    if cfg.get("user_override_short_enabled") and not tier.get("short_enabled"):
        cfg["short_enabled"] = False  # can't override cash → shorting
        cfg["short_override_rejected"] = True
    return cfg


def wheel_is_affordable(tier_cfg: dict, lowest_strike: float = 5.0) -> bool:
    """Dynamic wheel-enable check. Even if the tier says wheel is
    disabled by equity, a user with enough settled cash to cover a
    low-strike CSP can open one.

    Rule: `cash_withdrawable ≥ 1.5 × 100 × lowest_strike` → enable.
    1.5x buffer covers the premium paid + potential assignment at strike.
    """
    if not tier_cfg:
        return False
    cash = tier_cfg.get("_detected_cash_withdrawable", 0)
    required = 1.5 * 100 * lowest_strike
    return cash >= required


def pdt_allows_exit(tier_cfg: dict, is_intraday_exit: bool = False,
                     buffer: int = 1) -> tuple:
    """Check if the bot can perform an intraday exit without violating
    PDT rules. Returns (allowed: bool, reason: str).

    Rules:
      * Cash accounts or margin ≥$25k → always allowed
      * Margin <$25k with day_trades_remaining >= buffer+1 → allowed
      * Margin <$25k with day_trades_remaining <= buffer → deny
        (save the remaining day-trade slots for emergencies)

    The `buffer` param means: when we have only `buffer` day-trades
    left, stop voluntarily using them. Default buffer=1 leaves one
    emergency slot (e.g., for a kill-switch flatten).

    If `is_intraday_exit` is False (position held overnight),
    returns (True, "") — the rule only applies to same-day buy+sell.
    """
    if not tier_cfg or not is_intraday_exit:
        return True, ""
    if not tier_cfg.get("pdt_applies", False):
        return True, ""
    remaining = tier_cfg.get("_detected_day_trades_remaining")
    if remaining is None:
        # Unknown — be conservative and allow (Alpaca will reject if
        # actually over limit). Don't block exits on data uncertainty.
        return True, ""
    if remaining <= buffer:
        return False, (f"PDT buffer low: day_trades_remaining={remaining}, "
                       f"buffer={buffer}. Holding overnight to preserve "
                       "emergency day-trade slot.")
    return True, ""


def settled_funds_available(tier_cfg: dict, desired_spend: float = 0) -> tuple:
    """For cash accounts, verify we have enough SETTLED cash before
    deploying. Returns (available: bool, settled_cash: float, reason: str).

    Uses Alpaca's `cash_withdrawable` field (settled cash only). Apply
    a 5% buffer so we don't exactly max out and risk a rounding
    Good Faith Violation.
    """
    if not tier_cfg or not tier_cfg.get("settled_funds_required"):
        return True, float("inf"), ""
    settled = tier_cfg.get("_detected_cash_withdrawable", 0)
    usable = settled * 0.95  # 5% buffer
    if desired_spend <= usable:
        return True, settled, ""
    return False, settled, (
        f"Insufficient settled cash: need ${desired_spend:.2f}, "
        f"have ${settled:.2f} settled (usable ${usable:.2f} after 5% buffer). "
        "Wait for T+1/T+2 settlement before deploying.")


def calibration_summary(tier_cfg: dict) -> dict:
    """Return a human-readable summary for the dashboard UI."""
    if not tier_cfg:
        return {"detected": False,
                "reason": "Equity below $500 or Alpaca /account returned no data"}
    return {
        "detected": True,
        "tier_name": tier_cfg.get("name"),
        "tier_display": tier_cfg.get("display"),
        "account_type": tier_cfg.get("account_type"),
        "equity": tier_cfg.get("_detected_equity"),
        "cash": tier_cfg.get("_detected_cash"),
        "cash_withdrawable": tier_cfg.get("_detected_cash_withdrawable"),
        "buying_power": tier_cfg.get("_detected_buying_power"),
        "pattern_day_trader": tier_cfg.get("_detected_pattern_day_trader"),
        "shorting_enabled": tier_cfg.get("_detected_shorting_enabled"),
        "day_trades_remaining": tier_cfg.get("_detected_day_trades_remaining"),
        "max_positions": tier_cfg.get("max_positions"),
        "max_position_pct": tier_cfg.get("max_position_pct"),
        "strategies_enabled": tier_cfg.get("strategies_enabled"),
        "fractional_default": tier_cfg.get("fractional_default"),
        "min_stock_price": tier_cfg.get("min_stock_price"),
        "wheel_enabled": tier_cfg.get("wheel_enabled"),
        "short_enabled": tier_cfg.get("short_enabled"),
        "pdt_applies": tier_cfg.get("pdt_applies"),
        "settled_funds_required": tier_cfg.get("settled_funds_required"),
        "description": tier_cfg.get("description"),
    }


def _safe_int(v, default):
    try:
        return int(float(v)) if v is not None else default
    except (TypeError, ValueError):
        return default
