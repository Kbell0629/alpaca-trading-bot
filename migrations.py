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

import contextlib
import errno
import json
import logging
import os
import tempfile

# fcntl is POSIX-only. On Windows, the flock helper degrades to a no-op
# (which matches Windows' single-process Railway constraint anyway).
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

log = logging.getLogger(__name__)


MIGRATION_ROUND20_POSITION_CAP = "round20_position_cap_0.07"
MIGRATION_ROUND21_BREAKOUT_STOP = "round21_breakout_stop_0.12"
MIGRATION_ROUND29_EARNINGS_EXIT = "round29_earnings_exit_days_before_1"
MIGRATION_ROUND51_CALIBRATION_ADOPT = "round51_calibration_adopted"


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


def migrate_guardrails_round29(guardrails_path):
    """Round-29: add `earnings_exit_days_before: 1` default to per-user
    guardrails so the new pre-earnings exit rule fires for trailing_stop
    / breakout / mean_reversion / copy_trading positions without the
    user having to touch Settings.

    Idempotent via `_migrations_applied` list. Safe if
    `earnings_exit_days_before` is already set — stamps the migration
    as applied without overwriting.

    Returns: "migrated" | "already_applied" | "user_customised" |
    "no_file".
    """
    if not os.path.exists(guardrails_path):
        return "no_file"
    g = _load_json(guardrails_path)
    if not isinstance(g, dict):
        return "no_file"
    applied = g.get("_migrations_applied") or []
    if MIGRATION_ROUND29_EARNINGS_EXIT in applied:
        return "already_applied"
    existing = g.get("earnings_exit_days_before")
    user_customised = existing is not None
    if not user_customised:
        g["earnings_exit_days_before"] = 1
    g["_migrations_applied"] = applied + [MIGRATION_ROUND29_EARNINGS_EXIT]
    _save_json_atomic(guardrails_path, g)
    return "user_customised" if user_customised else "migrated"


def migrate_guardrails_round51(guardrails_path, user, account_fetcher):
    """Round-51: adopt portfolio-auto-calibration defaults for EXISTING
    users. One-time; idempotent via `_migrations_applied` stamp.

    Backs up the existing guardrails.json to
    `guardrails.json.pre-round51.backup` before overwriting so the user
    can revert if they dislike the new defaults.

    `account_fetcher(user)` is a callable that returns Alpaca's
    /v2/account response for the user. Factored out so tests can stub
    without mocking HTTP.

    Returns one of:
      * "migrated"        — calibration adopted; backup written
      * "already_applied" — stamp present; no-op
      * "no_file"         — guardrails.json doesn't exist yet (new user,
                            nothing to migrate; tier defaults apply on
                            first deploy)
      * "no_tier"         — Alpaca /account unavailable or equity < $500;
                            stamp NOT written so we retry next boot
      * "error:<type>"    — exception; stamp NOT written
    """
    if not os.path.exists(guardrails_path):
        # Fresh user — no migration needed. Tier defaults will apply on
        # first run_auto_deployer call (round-50 already handles this).
        return "no_file"
    g = _load_json(guardrails_path)
    if not isinstance(g, dict):
        return "no_file"
    applied = g.get("_migrations_applied") or []
    if MIGRATION_ROUND51_CALIBRATION_ADOPT in applied:
        return "already_applied"
    try:
        import portfolio_calibration as pc
        account = account_fetcher(user) if account_fetcher else None
        tier = pc.detect_tier(account) if account else None
        if not tier:
            return "no_tier"  # retry next boot
        # Backup the pre-migration guardrails so user can revert.
        # Round-52: track whether WE created it in this call so we can
        # roll back if the main write fails downstream.
        backup_path = guardrails_path + ".pre-round51.backup"
        backup_created_this_call = False
        if not os.path.exists(backup_path):
            try:
                with open(guardrails_path) as _orig, open(backup_path, "w") as _bak:
                    _bak.write(_orig.read())
                backup_created_this_call = True
            except OSError as _e:
                return f"error:backup_{type(_e).__name__}"
        # Merge tier defaults. User overrides that pre-date round-51 get
        # preserved for risk-preference keys (daily_loss_limit_pct,
        # earnings_exit_*, kill_switch_*, etc.). Tier defaults are
        # applied for sizing/fractional/strategy keys.
        TIER_ADOPTED_KEYS = (
            "max_positions", "max_position_pct", "min_stock_price",
            "fractional_enabled", "wheel_enabled", "short_enabled",
            "strategies_enabled",
        )
        for key in TIER_ADOPTED_KEYS:
            if key == "fractional_enabled":
                g[key] = bool(tier.get("fractional_default", False))
            elif key in tier:
                g[key] = tier[key]
        # Record which tier was adopted (useful for debugging)
        g["_round51_tier_adopted"] = tier.get("name")
        g["_migrations_applied"] = applied + [MIGRATION_ROUND51_CALIBRATION_ADOPT]
        # Round-52 fix: if the main write fails AFTER the backup was
        # successfully written, remove the orphan backup so the next
        # boot retries cleanly (otherwise the "if not os.path.exists"
        # guard at line 219 skips re-creating the backup, leaving user
        # with an old backup referencing a main file that was never
        # actually updated).
        try:
            _save_json_atomic(guardrails_path, g)
        except Exception as _write_err:
            # Main write failed — roll back the backup we just wrote if
            # it didn't exist before this call
            if backup_created_this_call:
                try:
                    os.unlink(backup_path)
                except OSError:
                    pass
            raise _write_err
        return "migrated"
    except Exception as e:
        log.warning(f"round51 calibration migration failed: {e}")
        return f"error:{type(e).__name__}"


@contextlib.contextmanager
def _user_migration_lock(user_dir):
    """Round-59: serialise migrations across multiple processes booting
    against the same per-user data dir. Two Railway containers booting
    within seconds of each other (rolling deploy) could both race the
    `_migrations_applied` check and apply the same migration twice. The
    second pass is mostly idempotent but the round-51 backup flag
    `backup_created_this_call` could be lost across the race window,
    risking a rare double-overwrite of guardrails.json.

    flock is POSIX-only — on Windows we degrade to a no-op (Railway's
    Linux containers always have fcntl, so production is covered).
    Lock file lives at `<user_dir>/.migrations.lock`. Best-effort: if
    the lock acquire fails (disk full, perms), we yield anyway rather
    than blocking boot."""
    if not _fcntl or not user_dir:
        yield
        return
    try:
        os.makedirs(user_dir, exist_ok=True)
    except OSError:
        yield
        return
    lock_path = os.path.join(user_dir, ".migrations.lock")
    fh = None
    try:
        # Open with O_RDWR | O_CREAT — flock needs a writable fd on Linux
        fh = open(lock_path, "a+")
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        except OSError as e:
            # EAGAIN means another process holds the lock + we'd block —
            # shouldn't happen with LOCK_EX (no LOCK_NB), but guard
            # anyway. Anything else: log + skip locking, don't block boot.
            if e.errno != errno.EAGAIN:
                log.warning("migration lock acquire failed: %s", e)
            yield
            return
        try:
            yield
        finally:
            try:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass
    except OSError as e:
        log.warning("migration lock setup failed: %s", e)
        yield
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


def run_all_migrations(users, user_file_fn, account_fetcher=None):
    """Apply every idempotent migration to every user.

    `users` is the list of user dicts from get_all_users_for_scheduling.
    `user_file_fn(user, filename)` is the path resolver from
    cloud_scheduler (avoids circular import).
    `account_fetcher(user)` returns Alpaca /v2/account for round-51
    calibration adoption. Optional — if None, round-51 skips until
    next boot.

    Round-59: each user's migrations run under a per-user flock
    (`<user_dir>/.migrations.lock`) so concurrent Railway boots don't
    race the read-check-write cycle.

    Returns summary dict {user_id: {migration: action, ...}}."""
    summary = {}
    for u in users or []:
        uid = u.get("id") or u.get("username", "unknown")
        user_result = {}
        # Resolve user_dir from any per-user file path (they all live
        # under the same dir). Use guardrails.json since every migration
        # touches it.
        try:
            _gpath = user_file_fn(u, "guardrails.json")
            _user_dir = os.path.dirname(_gpath) if _gpath else None
        except Exception:
            _user_dir = None

        with _user_migration_lock(_user_dir):
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
            try:
                gpath29 = user_file_fn(u, "guardrails.json")
                user_result["round29_earnings_exit"] = (
                    migrate_guardrails_round29(gpath29)
                )
            except Exception as e:
                user_result["round29_earnings_exit"] = f"error: {type(e).__name__}"
                log.warning(f"migration round29 failed for {uid}: {e}")
            # Round-51: adopt portfolio-auto-calibration defaults for
            # existing users. Needs Alpaca /account to know tier. Only
            # runs if account_fetcher is provided and returns a valid
            # account (equity ≥ $500).
            if account_fetcher is not None:
                try:
                    gpath51 = user_file_fn(u, "guardrails.json")
                    user_result["round51_calibration_adopt"] = (
                        migrate_guardrails_round51(gpath51, u, account_fetcher)
                    )
                except Exception as e:
                    user_result["round51_calibration_adopt"] = f"error: {type(e).__name__}"
                    log.warning(f"migration round51 failed for {uid}: {e}")
        summary[uid] = user_result
    return summary
