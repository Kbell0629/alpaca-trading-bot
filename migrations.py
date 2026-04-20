"""
migrations.py — idempotent, boot-time user-config migrations.

Round-20 paper-trading observation: the top-50 screener was surfacing
high-volatility meme-tier names (INFQ vol 33%, already +12.5% on the
session) that stopped out within 2-3 days on normal noise. Fix set:

  * skip breakout/PEAD picks where daily_change > 8% (don't-chase gate)
  * skip breakout/PEAD picks where volatility > 20% (vol cap)
  * max_position_pct 0.10 → 0.07 in the "moderate" preset

The filter gates live in cloud_scheduler.run_auto_deployer and take
effect on the next scheduler tick with no config change needed.

The position-cap lives in PER-USER `guardrails.json` files under
$DATA_DIR/users/<id>/. Repo-level default changes don't rewrite
existing per-user files. This module bridges that gap: on every boot
we look at each user's guardrails, and if they're still on the pre-
round-20 0.10 cap AND haven't already been migrated, bump them to
0.07 and stamp `_migrations_applied` so we never run twice.

USER OVERRIDE: after the migration, the user can re-set
`max_position_pct` to whatever they want via Settings → Guardrails.
The migration only runs once per user — the stamp prevents re-runs.

USAGE: called from cloud_scheduler.start_scheduler after state_recovery
for every user. Failure of any single user's migration doesn't block
others (wrapped in try/except at the call site).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

log = logging.getLogger(__name__)


MIGRATION_ROUND20_POSITION_CAP = "round20_position_cap_0.07"
MIGRATION_ROUND21_BREAKOUT_STOP = "round21_breakout_stop_0.12"


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_json_atomic(path, data):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def migrate_guardrails_round20(guardrails_path):
    """Pure, testable. Takes an absolute guardrails.json path. Returns
    the migration action taken, one of:
      * "migrated" — was on 0.10 or unset; bumped to 0.07 + stamp written
      * "already_applied" — stamp present; no-op
      * "user_customised" — max_position_pct != 0.10 AND no stamp; no-op
        (operator set their own value intentionally)
      * "no_file" — guardrails.json doesn't exist

    Safe to call repeatedly; idempotent via `_migrations_applied` list.
    """
    if not os.path.exists(guardrails_path):
        return "no_file"
    g = _load_json(guardrails_path)
    if not isinstance(g, dict):
        return "no_file"
    applied = g.get("_migrations_applied") or []
    if MIGRATION_ROUND20_POSITION_CAP in applied:
        return "already_applied"
    cap = g.get("max_position_pct")
    # Only migrate users still on the round-13 default of 0.10 (or users
    # who never explicitly set it — None/unset). If someone already chose
    # a custom value (0.05, 0.15, etc.) respect it and stamp the
    # migration as applied so we never touch their file again.
    if cap is not None and cap != 0.10:
        g["_migrations_applied"] = applied + [MIGRATION_ROUND20_POSITION_CAP]
        _save_json_atomic(guardrails_path, g)
        return "user_customised"
    g["max_position_pct"] = 0.07
    g["_migrations_applied"] = applied + [MIGRATION_ROUND20_POSITION_CAP]
    _save_json_atomic(guardrails_path, g)
    return "migrated"


def migrate_auto_deployer_config_round21(config_path):
    """Round-21: widen breakout stop from the old 0.05 default (which
    was tighter than every other strategy — backwards) to 0.12. Also
    pin max_portfolio_pct_per_stock to 0.07 to match the round-20
    guardrails cap (when values are out of sync the narrower one wins
    in run_auto_deployer, but keeping them aligned avoids confusion).

    Idempotent via `_migrations_applied` list on the config JSON. Only
    writes if values are the pre-round-21 defaults (0.05 or missing,
    and 0.10 or missing) — operators who customised stay untouched.
    Returns: "migrated" | "already_applied" | "user_customised" |
    "no_file".
    """
    if not os.path.exists(config_path):
        return "no_file"
    c = _load_json(config_path)
    if not isinstance(c, dict):
        return "no_file"
    applied = c.get("_migrations_applied") or []
    if MIGRATION_ROUND21_BREAKOUT_STOP in applied:
        return "already_applied"

    risk = c.get("risk_settings")
    if not isinstance(risk, dict):
        risk = {}
        c["risk_settings"] = risk

    current_breakout_stop = risk.get("breakout_stop_loss_pct")
    current_per_stock = c.get("max_portfolio_pct_per_stock")

    # User customised if EITHER value was set to something other than
    # the pre-round-21 defaults. Stamp as applied so we don't retry.
    user_customised = False
    if current_breakout_stop is not None and current_breakout_stop not in (0.05, 0.10):
        user_customised = True
    if current_per_stock is not None and current_per_stock not in (0.10, 0.07):
        user_customised = True

    if not user_customised:
        risk["breakout_stop_loss_pct"] = 0.12
        c["max_portfolio_pct_per_stock"] = 0.07

    c["_migrations_applied"] = applied + [MIGRATION_ROUND21_BREAKOUT_STOP]
    _save_json_atomic(config_path, c)
    return "user_customised" if user_customised else "migrated"


def run_all_migrations(users, user_file_fn):
    """Apply every idempotent migration to every user.

    `users` is the list of user dicts from get_all_users_for_scheduling.
    `user_file_fn(user, filename)` is the path resolver from
    cloud_scheduler (avoids circular import).

    Returns summary dict {user_id: {migration: action, ...}}."""
    summary = {}
    for u in users or []:
        uid = u.get("id") or u.get("username", "unknown")
        user_result = {}
        try:
            gpath = user_file_fn(u, "guardrails.json")
            user_result["round20_position_cap"] = migrate_guardrails_round20(gpath)
        except Exception as e:
            user_result["round20_position_cap"] = f"error: {type(e).__name__}"
            log.warning(f"migration round20 failed for {uid}: {e}")
        try:
            apath = user_file_fn(u, "auto_deployer_config.json")
            user_result["round21_breakout_stop"] = (
                migrate_auto_deployer_config_round21(apath)
            )
        except Exception as e:
            user_result["round21_breakout_stop"] = f"error: {type(e).__name__}"
            log.warning(f"migration round21 failed for {uid}: {e}")
        summary[uid] = user_result
    return summary
