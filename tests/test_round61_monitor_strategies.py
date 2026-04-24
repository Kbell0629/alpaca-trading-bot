"""
Round-61 tests: money-path invariants for cloud_scheduler.monitor_strategies.

monitor_strategies is the 60-second loop that enforces kill-switch, daily-loss,
max-drawdown, stop raises, profit takes, and exits — basically the heart of
loss prevention. It calls ~10 other functions + touches JSON + Alpaca, so a
full behavioral test would need >20 stubs. Instead this module pins the
architectural invariants via grep on the source. Same style as R55 + R57.

A broken invariant here = loss of real money. Every test below protects a
specific failure mode that has already cost users money at least once, or
that CLAUDE.md lists as a post-round invariant.

Fixtures:
  * No `isolated_data_dir` needed — we read cloud_scheduler.py from disk,
    don't import + execute it. This avoids the module-reload dance and
    keeps tests fast.

Gaps filled (vs R55 + R57 + R51):
  1. Kill-switch check short-circuits the entire monitor tick.
  2. Max-drawdown breach: peak→trough math, critical_alert fire,
     kill_switch_triggered_at + kill_switch_reason fields set, and
     _flatten_all_user runs.
  3. Daily-loss breach: same flow from a different trigger.
  4. peak_portfolio_value seeded under lock when missing.
  5. daily_starting_value seeded + date-stamped with ET trading day.
  6. Per-strategy main loop skips copy_trading.json + wheel_strategy.json
     (those have their own handlers; mixing would double-act).
  7. Per-strategy main loop requires status in (active, awaiting_fill).
  8. Top-level try/except keeps the monitor alive across per-user crashes.
  9. AH mode skips paused strategies.
 10. Round-61 pt.28: AH mode flows short_sell strategies through to
     process_short_strategy which runs ONLY initial cover-stop
     placement for unprotected shorts (GTC stops sit until regular
     hours, no thin-book fill risk). Thin-book-risky short paths
     (trailing tighten, cover-fill processing, force-cover) stay
     gated on regular hours.
"""
from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PATH = os.path.join(_HERE, "cloud_scheduler.py")


def _src():
    with open(_SRC_PATH) as f:
        return f.read()


def _slice(src: str, start_token: str, end_token: str | None = None,
           end_offset: int = 8000) -> str:
    """Return a chunk of src starting at start_token. If end_token is given,
    bound it there; else take end_offset chars. Raises if start_token not
    found — that means the function moved and the test is stale."""
    i = src.find(start_token)
    assert i >= 0, f"start token missing (did the function move?): {start_token!r}"
    if end_token:
        j = src.find(end_token, i + len(start_token))
        assert j > i, f"end token missing after start: {end_token!r}"
        return src[i:j]
    return src[i:i + end_offset]


# ========= 1. Kill switch short-circuits =========

def test_kill_switch_returns_early_from_monitor():
    """The very first thing after loading guardrails must be a kill-switch
    check. If the switch is set, we return without touching Alpaca at all —
    no /account fetch, no per-strategy loop. A forgotten early-return here
    means a tripped kill switch still places profit-take and stop orders
    for 60+ more seconds."""
    src = _src()
    block = _slice(src, "def monitor_strategies(user",
                   "def check_profit_ladder")
    # The load + check + return pattern must appear BEFORE the extended-hours
    # branch and BEFORE the /account fetch. Pin the sequence.
    kill_idx = block.find('if guardrails.get("kill_switch"):')
    assert kill_idx > 0, "kill_switch guard missing at top of monitor_strategies"
    # Next non-comment line must be `return`
    after = block[kill_idx:kill_idx + 200]
    assert "return" in after.split("\n")[1], (
        "kill_switch check must be followed by an immediate return")
    # AND /account fetch must come AFTER this check, not before
    acct_idx = block.find("user_api_get(user, \"/account\")")
    assert acct_idx > kill_idx, (
        "/account fetch must not precede the kill_switch guard — "
        "tripped switch should prevent all Alpaca calls this tick")


# ========= 2. Max drawdown → kill switch =========

def test_max_drawdown_breach_triggers_kill_switch():
    """When peak→current drawdown exceeds max_drawdown_pct, the monitor
    must set kill_switch=True, stamp kill_switch_reason, persist under lock,
    flatten, and fire critical_alert (Sentry + ntfy + email). A silent
    drawdown is worse than a daily-loss trip because it spans multiple days."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")

    # The drawdown check uses peak - current / peak
    assert "(peak_val - current_val) / peak_val" in block, (
        "drawdown math must compute (peak-current)/peak — regression would "
        "invert the sign and never fire")

    # Threshold key + default
    assert 'guardrails.get("max_drawdown_pct", 0.10)' in block, (
        "max_drawdown_pct default must stay 10% (0.10) — regressing the "
        "default to 0 or None would disable the guard")

    # On breach: kill_switch + triggered_at + reason
    # (check they all appear within the drawdown branch, not just anywhere)
    dd_idx = block.find("(peak_val - current_val) / peak_val")
    dd_block = block[dd_idx:dd_idx + 2500]
    assert 'guardrails["kill_switch"] = True' in dd_block
    assert "kill_switch_triggered_at" in dd_block
    assert "kill_switch_reason" in dd_block
    # Must persist to disk inside the lock
    assert "strategy_file_lock(guardrails_path)" in dd_block, (
        "drawdown-trip RMW must hold strategy_file_lock — a concurrent "
        "handler POST could overwrite the kill_switch field otherwise")
    # Must flatten + notify
    assert "_flatten_all_user(user)" in dd_block, (
        "drawdown-trip must flatten all positions + orders")
    assert "critical_alert" in dd_block, (
        "drawdown kill-switch must fire critical_alert (Sentry + ntfy + email)")


def test_max_drawdown_default_stays_10_percent():
    """10% is what CLAUDE.md + the auto-deployer assume. If someone bumps
    this to 20%+ the drawdown guard becomes cosmetic."""
    src = _src()
    assert 'guardrails.get("max_drawdown_pct", 0.10)' in src, (
        "max_drawdown_pct default must remain 0.10 (10%)")


# ========= 3. Daily loss → kill switch =========

def test_daily_loss_breach_triggers_kill_switch():
    """The other kill-switch arm: daily_loss_limit_pct (default 3%). Same
    invariants as drawdown — lock, flatten, notify, field stamps."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")

    # The loss math is (daily_start - current) / daily_start
    assert "(daily_start - current_val) / daily_start" in block
    assert 'guardrails.get("daily_loss_limit_pct", 0.03)' in block, (
        "daily_loss_limit_pct default must stay 3% — regression to a "
        "higher value lets real losses accumulate silently")

    loss_idx = block.find("(daily_start - current_val) / daily_start")
    loss_block = block[loss_idx:loss_idx + 2500]
    assert 'guardrails["kill_switch"] = True' in loss_block
    assert "strategy_file_lock(guardrails_path)" in loss_block
    assert "_flatten_all_user(user)" in loss_block
    assert "notify_rich" in loss_block, (
        "daily-loss trip must fire notify_rich for email+push")


# ========= 4. peak_portfolio_value seeded under lock =========

def test_peak_portfolio_value_seeded_when_missing():
    """Users who signed up before round-10 have no peak_portfolio_value
    in guardrails. Without a seed, the drawdown check's `peak_val` is
    falsy and the guard silently does nothing. The seed must happen on
    the first tick that sees a valid current_val."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")
    seed_idx = block.find("peak_val = guardrails.get(\"peak_portfolio_value\")")
    assert seed_idx > 0, "peak_val lookup missing"
    seed_block = block[seed_idx:seed_idx + 600]
    # The seed path must:
    #  * check `not peak_val and current_val`
    #  * acquire the file lock
    #  * setdefault (don't overwrite an existing value on race)
    assert "if not peak_val and current_val" in seed_block
    assert "strategy_file_lock(guardrails_path)" in seed_block
    assert 'setdefault("peak_portfolio_value"' in seed_block, (
        "peak seed must use setdefault so a concurrent handler write can't "
        "be clobbered")


# ========= 5. daily_starting_value seeded + date-stamped =========

def test_daily_starting_value_uses_et_date_and_locks():
    """daily_starting_value must be stamped with the ET trading-day string
    (YYYY-MM-DD). Without the date, a multi-day drawdown shows as a single
    day's loss. Without the lock, a concurrent handler can clobber."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")

    # ET-date stamping
    assert "get_et_time().strftime(\"%Y-%m-%d\")" in block, (
        "daily_starting_value must be ET-tagged, not UTC")
    # Field name
    assert 'guardrails["daily_starting_value_date"]' in block

    # Under lock
    dsv_idx = block.find('guardrails["daily_starting_value"] = current_val')
    assert dsv_idx > 0
    dsv_block = block[dsv_idx:dsv_idx + 400]
    assert "strategy_file_lock(guardrails_path)" in dsv_block, (
        "daily_starting_value RMW must be inside strategy_file_lock "
        "(CLAUDE.md Post-57 invariant)")


# ========= 6. Main loop skips wheel + copy_trading files =========

def test_main_loop_skips_wheel_and_copy_trading_files():
    """wheel_strategy.json and copy_trading.json have their own dedicated
    handlers elsewhere in cloud_scheduler. If monitor_strategies processes
    them too, you get double-action (2x profit takes, 2x stop raises).
    Pin the skip."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")
    # Look for the regular-hours per-strategy loop skip
    assert 'if fname in ("copy_trading.json", "wheel_strategy.json"):' in block, (
        "regular-hours main loop must skip copy_trading.json + "
        "wheel_strategy.json (those have dedicated handlers)")


def test_main_loop_requires_active_or_awaiting_fill_status():
    """Only strategies in active / awaiting_fill status should be processed.
    Skipping paused + closed + errored is required so that a paused
    strategy doesn't silently resume trading next tick."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")
    assert 'status not in ("active", "awaiting_fill") or not symbol:' in block, (
        "main loop status gate regressed — paused/closed strategies could "
        "get re-processed")


# ========= 7. Top-level exception isolation =========

def test_monitor_catches_top_level_exceptions():
    """A crash in one user's monitor tick must not propagate — the
    scheduler thread runs for all users in sequence, and an uncaught
    exception would kill the thread entirely (silent stop of trading
    for everyone). This invariant has been broken in prior audits and
    cost a weekend of missed ticks."""
    src = _src()
    # Round-61 pt.31 inserted ``_shrink_stop_before_partial_exit``
    # between monitor_strategies and check_profit_ladder, so slice
    # to the helper boundary instead.
    block = _slice(src, "def monitor_strategies",
                    "def _shrink_stop_before_partial_exit")
    # Last ~400 chars of the function should have `except Exception`
    # with log — not a bare `raise`
    tail = block[-600:]
    assert "except Exception as e:" in tail
    assert "Monitor error" in tail, (
        "top-level except must log, not swallow silently")


# ========= 8. AH mode: paused + short strategies skipped =========

def test_ah_mode_skips_paused_strategies():
    """In AH mode, paused strategies must stay paused. A tick that
    processes a paused strategy could raise the trailing stop under
    the user's intent to leave it alone.

    Round-61 pt.28 narrowed this: only ``paused`` is filtered at the
    loop level now. short_sell strategies flow through to
    ``process_short_strategy(extended_hours=True)`` which runs ONLY
    the initial cover-stop placement path for unprotected shorts."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")
    ah_idx = block.find('if extended_hours:')
    assert ah_idx > 0
    ah_block = block[ah_idx:ah_idx + 3000]
    # Pt.28: paused still skipped, short_sell no longer skipped here.
    assert 'if strat.get("paused"):' in ah_block, (
        "AH loop must still skip paused strategies")
    assert 'strat.get("strategy") == "short_sell"' not in ah_block, (
        "Pt.28: short_sell must NOT be skipped at the loop level — "
        "process_strategy_file → process_short_strategy handles the "
        "AH gating internally (placement-only for unprotected shorts).")


def test_ah_mode_skips_wheel_files_in_per_user_loop():
    """CLAUDE.md Post-55 invariant: AH loop skips wheel_*.json because
    options books are too thin for automated action overnight."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")
    ah_idx = block.find('if extended_hours:')
    assert ah_idx > 0
    ah_block = block[ah_idx:ah_idx + 3000]
    assert 'fname.startswith("wheel_")' in ah_block, (
        "AH loop must skip wheel_*.json files")


# ========= 9. Notifications on kill-switch paths =========

def test_kill_switch_notifications_use_notify_rich_not_plain_notify():
    """Both kill-switch arms (drawdown, daily-loss) use notify_rich with
    a rich_subject + rich_body template. Plain notify_user would drop
    the HTML email and leave the user with just an ntfy line. notify_rich
    is required for the email path."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")
    # Inside the drawdown branch
    dd_idx = block.find("(peak_val - current_val) / peak_val")
    dd_block = block[dd_idx:dd_idx + 2500]
    assert "notify_rich" in dd_block
    assert "kill_switch" in dd_block
    # Inside the daily-loss branch
    loss_idx = block.find("(daily_start - current_val) / daily_start")
    loss_block = block[loss_idx:loss_idx + 2500]
    assert "notify_rich" in loss_block


# ========= 10. Tier detection happens each tick =========

def test_monitor_stashes_tier_cfg_each_tick():
    """Post-51 invariant: the monitor detects portfolio tier once per tick
    via detect_tier + apply_user_overrides, stashes on user['_tier_cfg']
    so every downstream exit decision (profit ladder PDT, short check)
    can consult it without re-fetching /account. Regression here breaks
    the PDT guard in check_profit_ladder."""
    src = _src()
    block = _slice(src, "def monitor_strategies", "def check_profit_ladder")
    # At least twice (AH branch + regular-hours branch)
    count = block.count('user["_tier_cfg"] = _pc.apply_user_overrides')
    assert count >= 2, (
        f"_tier_cfg must be stashed in BOTH AH and regular-hours branches, "
        f"found {count} occurrences")
    # detect_tier must be called inside a try/except — tier detection is
    # advisory, never blocks the monitor.
    assert "pass  # calibration is advisory" in block, (
        "tier-detection failure must not block monitor — the try/except "
        "comment is the regression fuse")
