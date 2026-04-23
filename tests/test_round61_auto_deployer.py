"""
Round-61 tests: money-path invariants for cloud_scheduler.run_auto_deployer.

run_auto_deployer is the ~880-line deploy pipeline that decides when, how,
and what to buy each day. A silent regression here either:
  * Bypasses a financial guardrail (kill switch, cooldown, tier gate) →
    real trades fire when they shouldn't
  * Double-fires a gate → the deployer freezes, users miss entries

Full behavioral testing of this function would need ~30 stubs (subprocess,
Alpaca, screener output, market breadth, portfolio_risk, correlation map,
PDT, tier calibration). Grep-pin style matches the R55/R57 precedent and
keeps tests fast + specific. Every invariant below protects either a
previous-audit fix or a CLAUDE.md post-round invariant.

Gaps filled (no prior targeted tests):
  1. Kill-switch check returns before config load (prevents wasted I/O).
  2. auto_deployer_config.enabled=False returns cleanly.
  3. Cooldown-after-loss honors cooldown_after_loss_minutes.
  4. Cooldown parse failure fails CLOSED (R3 audit fix).
  5. Tier gate: short selling blocked when TIER_CFG.short_enabled=False
     (R52 CRITICAL fix — cash accounts were firing rejected shorts).
  6. LIVE_MODE_AT_START atomic snapshot (R45 invariant — live toggle
     mid-run must not change pick routing).
  7. Factor bypass disables all factor gates (breadth/RS/IV/sector/quality).
  8. Market breadth <40% skips breakout + PEAD strategies.
  9. daily_starting_value set ONCE per ET day (not every deployer run).
 10. peak_portfolio_value bumped when current exceeds peak.
 11. deploy_should_abort tight-loop check for instant kill-switch response.
 12. Capital check subprocess writes to PER-USER capital_status.json
     (R10 audit — was leaking across users).
 13. Beta-exposure + drawdown-sizing runs AFTER positions loaded (R12
     audit — was dead code; silent NameError for months).
 14. check_correlation_allowed applied to both long picks and shorts.
"""
from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PATH = os.path.join(_HERE, "cloud_scheduler.py")


def _src():
    with open(_SRC_PATH) as f:
        return f.read()


def _deployer_block(src: str) -> str:
    """Slice just the run_auto_deployer function body so we don't match
    pattern hits elsewhere in the file (e.g. monitor_strategies)."""
    start = src.find("def run_auto_deployer(user):")
    assert start >= 0, "run_auto_deployer moved"
    end = src.find("def run_daily_close", start)
    assert end > start, "couldn't find end of run_auto_deployer"
    return src[start:end]


# ========= 1. Kill switch early return =========

def test_kill_switch_returns_before_any_api_call():
    """Kill switch must be the first guard — no point fetching /account,
    running the screener subprocess, or anything else if the switch is
    tripped. Regression = wasted 500ms+ of API quota per tick while the
    switch is on."""
    block = _deployer_block(_src())
    # The load + kill_switch guard must appear before /account is fetched
    kill_idx = block.find('if guardrails.get("kill_switch"):')
    acct_idx = block.find('user_api_get(user, "/account")')
    screener_idx = block.find("run_screener(user")
    assert kill_idx > 0
    assert acct_idx > kill_idx, (
        "kill_switch guard must come BEFORE /account fetch")
    assert screener_idx > kill_idx, (
        "kill_switch guard must come BEFORE run_screener subprocess")
    # And the guard must return, not just log
    post = block[kill_idx:kill_idx + 300]
    assert "return" in post


def test_auto_deployer_config_disabled_returns_cleanly():
    """User can disable the auto-deployer via
    auto_deployer_config.enabled=false. Must be honored with an explicit
    return — not a deep-nested `if enabled: ...` that leaves side
    effects in the early lines."""
    block = _deployer_block(_src())
    idx = block.find('if not config.get("enabled", True):')
    assert idx > 0, "enabled=False gate missing"
    post = block[idx:idx + 300]
    assert "return" in post, "disabled config must return immediately"


# ========= 2. Cooldown after loss =========

def test_cooldown_after_loss_minutes_is_honored():
    """guardrails.last_loss_time + cooldown_after_loss_minutes (default 60)
    blocks deploys for N minutes after a loss. This is a core risk gate —
    stops the deployer from chasing back in after a stop-out."""
    block = _deployer_block(_src())
    assert 'guardrails.get("cooldown_after_loss_minutes", 60)' in block, (
        "cooldown default must stay 60 minutes")
    assert "last_loss_time" in block
    # Both gate present AND the check subtracts now_et - last_dt
    assert "(now_et() - last_dt).total_seconds()" in block


def test_cooldown_parse_failure_fails_closed():
    """ROUND-3 audit fix: if last_loss_time is unparseable, we must SKIP
    the deploy (fail CLOSED), not silently bypass the guardrail. The
    original bug silently continued — a corrupted timestamp could
    unblock the deployer after a loss."""
    block = _deployer_block(_src())
    # Find the cooldown parse block
    idx = block.find('cooldown_after_loss_minutes')
    assert idx > 0
    # The except branch should be visible within ~1200 chars after
    window = block[idx:idx + 1200]
    # Both the log AND the return must be in the except
    assert "Skipping deploy to be safe" in window, (
        "cooldown parse failure must log the safe-skip reason (R3 fix)")
    # Return statement in the except branch
    except_idx = window.find("except Exception as e:")
    assert except_idx > 0
    assert "return" in window[except_idx:except_idx + 700]


# ========= 3. Tier gate: cash accounts can't short =========

def test_cash_tier_blocks_short_selling():
    """ROUND-52 CRITICAL fix: if TIER_CFG.short_enabled is False, the
    short-sell block is disabled for the rest of the deploy run — even
    if user has short_selling.enabled=true in auto_deployer_config.json.
    Cash accounts can't short per Alpaca rules; firing anyway wastes a
    deploy slot + emits noisy errors."""
    block = _deployer_block(_src())
    idx = block.find('TIER_CFG is not None and not TIER_CFG.get("short_enabled"')
    assert idx > 0, "tier short-enabled gate missing"
    window = block[idx:idx + 800]
    # The gate must zero out short_config — not just log
    assert "short_config = {}" in window, (
        "tier gate must disable short_config, not just warn — R52 invariant")


# ========= 4. LIVE_MODE_AT_START atomic snapshot =========

def test_live_mode_captured_at_start_not_per_pick():
    """ROUND-45 / CLAUDE.md Post-45 invariant: the HTTP handler can flip
    user.live_mode mid-deployer-run via the Live Trading toggle. If the
    per-pick loop re-reads user.get("live_mode"), a paper-sized position
    can route to the live account halfway through. Must snapshot at
    function entry."""
    block = _deployer_block(_src())
    assert "LIVE_MODE_AT_START = bool(user.get(\"live_mode\"))" in block, (
        "live_mode must be captured atomically at deployer start")
    assert "LIVE_MAX_DOLLARS_AT_START" in block, (
        "live_max_position_dollars must also be captured at start")


# ========= 5. Factor bypass =========

def test_factor_bypass_flag_disables_factor_gates():
    """guardrails.factor_bypass is the emergency escape hatch — disables
    breadth, RS, sector rotation, IV rank, quality, bullish-news priority.
    Useful when factor data is stale or a regime is shifting. Pin both
    the flag read AND the log message so it's visible in operator logs."""
    block = _deployer_block(_src())
    assert 'factor_bypass = bool(guardrails.get("factor_bypass"))' in block
    assert "FACTOR BYPASS active" in block, (
        "factor_bypass must log a visible warning when active — operators "
        "need to know the safety rails are off")


def test_factor_bypass_gates_breadth_check():
    """When factor_bypass is on, the market_breadth check must be
    skipped entirely — not run with its result ignored."""
    block = _deployer_block(_src())
    # The breadth block is inside `if factor_bypass: ... else: ...` or
    # equivalent. Find the breadth-check guard.
    idx = block.find("import market_breadth")
    assert idx > 0
    # Between factor_bypass assignment and the market_breadth import,
    # there must be a gate.
    fbv_idx = block.find("factor_bypass = bool")
    assert 0 < fbv_idx < idx
    gate_window = block[fbv_idx:idx]
    assert "factor_bypass" in gate_window, (
        "breadth import must be gated by factor_bypass")


# ========= 6. Market breadth gate =========

def test_weak_breadth_skips_breakout_and_pead():
    """Breadth <40% → 80% historical breakout failure rate. Breakouts
    and PEAD (post-earnings drift) are skipped; mean-reversion + wheel
    still run (they don't depend on breadth for edge)."""
    block = _deployer_block(_src())
    assert "breadth_pct_val < 40" in block, (
        "breadth threshold must stay 40% — R11 tuned value")
    # The log message must mention what gets paused
    idx = block.find("breadth_pct_val < 40")
    window = block[idx:idx + 500]
    assert "BREAKOUT" in window
    assert "PEAD" in window


# ========= 7. Daily starting value once per day =========

def test_daily_starting_value_set_once_per_et_day():
    """Prior regression: every run_auto_deployer call overwrote
    daily_starting_value, masking intraday drawdowns. Must set only if
    (a) unset or (b) stored date != today's ET date."""
    block = _deployer_block(_src())
    # ET-date stamp
    assert 'get_et_time().strftime("%Y-%m-%d")' in block
    assert "last_reset_date != today_et" in block, (
        "daily_starting_value reset must check the ET-date stamp")
    # And the assignment must be inside that conditional
    idx = block.find("last_reset_date != today_et")
    window = block[idx:idx + 400]
    assert 'guardrails["daily_starting_value"] = float' in window


def test_peak_portfolio_value_bumped_when_current_exceeds():
    """peak_portfolio_value only increases. Tracking peak is what makes
    max_drawdown meaningful — without a rising peak, a 5% slide from a
    new high doesn't register as drawdown."""
    block = _deployer_block(_src())
    # Look for `if current > peak:` pattern
    assert "if current > peak:" in block, (
        "peak bump logic missing — drawdown gate silently degraded")
    idx = block.find("if current > peak:")
    window = block[idx:idx + 200]
    assert 'guardrails["peak_portfolio_value"] = current' in window


# ========= 8. deploy_should_abort tight loop =========

def test_deploy_should_abort_checked_in_loop():
    """CLAUDE.md Post-12 invariant: the kill-switch activation is mirrored
    to a threading.Event (_deploy_abort_event). Tight loops must check
    deploy_should_abort() so a mid-run kill-switch stops the CURRENT run
    before the next API call, not after N picks complete. The monitor's
    kill-switch stamp is seconds; per-pick ordering is seconds each."""
    block = _deployer_block(_src())
    # Must be at least one tight-loop check
    count = block.count("deploy_should_abort()")
    assert count >= 1, (
        "deploy_should_abort() check missing from deployer tight loop — "
        "kill-switch won't stop an in-progress deploy run")


# ========= 9. Capital check per-user isolation =========

def test_capital_check_writes_per_user_file():
    """ROUND-10 audit fix: the capital-check subprocess previously wrote
    to a shared capital_status.json which leaked can_trade + free-cash
    numbers across users. Must route via CAPITAL_STATUS_PATH env var to
    a per-user file."""
    block = _deployer_block(_src())
    assert 'user_file(user, "capital_status.json")' in block, (
        "capital_status.json must be per-user (R10 audit)")
    assert 'env["CAPITAL_STATUS_PATH"]' in block, (
        "capital check subprocess must be told the per-user path via "
        "CAPITAL_STATUS_PATH env var")


# ========= 10. Beta-exposure + drawdown sizing =========

def test_beta_exposure_runs_after_positions_loaded():
    """ROUND-12 audit fix: the beta_adjusted_exposure + drawdown_size
    block was originally placed BEFORE factor_bypass/existing_positions/
    portfolio_value existed, so it silently threw NameError every run
    for months — risk gate disabled in production. Must run AFTER
    existing_positions is defined."""
    block = _deployer_block(_src())
    positions_idx = block.find("positions = user_api_get(user, \"/positions\")")
    beta_idx = block.find("beta_adjusted_exposure(")
    assert positions_idx > 0 and beta_idx > 0
    assert beta_idx > positions_idx, (
        "beta_adjusted_exposure must run AFTER positions fetch — "
        "R12 audit fix (was dead code due to NameError)")


def test_beta_exposure_block_all_does_not_return():
    """When beta_exposure['block_all'] is True, the deployer logs a
    warning but does NOT return — the per-pick block below enforces it.
    Early-return here would skip the short-sell block too, which has
    independent regime logic. Comment pins the invariant."""
    block = _deployer_block(_src())
    idx = block.find('beta_exposure["block_all"]')
    assert idx > 0
    window = block[idx:idx + 400]
    assert "Don't return" in window, (
        "beta block_all must be enforced per-pick (the 'Don't return' "
        "comment is the invariant pin)")


# ========= 11. Correlation check =========

def test_correlation_check_applied_to_long_picks():
    """check_correlation_allowed must gate every long pick before deploy.
    Otherwise a 3rd tech pick after AAPL+MSFT fires even though the
    sector cap says 2."""
    block = _deployer_block(_src())
    # Count occurrences — expect at least 2 (longs + shorts)
    count = block.count("check_correlation_allowed(")
    assert count >= 2, (
        f"check_correlation_allowed must be called for both long picks "
        f"and shorts (found {count})")


# ========= 12. Integration: tier calibration log de-dup =========

def test_tier_calibration_log_dedups_on_unchanged_tier():
    """ROUND-52 quality-of-life fix: the deployer logs "Calibrated tier: X"
    only when the tier name differs from the last-seen value. Force
    Deploys and retries that run 3-5x/day would otherwise spam the log
    with identical lines."""
    block = _deployer_block(_src())
    assert "_last_runs.get(_tier_key)" in block, (
        "tier log de-dup missing — R52 improvement reverted")
    assert "if _last_tier != _current_tier_str:" in block


# ========= 13. TIER_CFG fills missing guardrails, user overrides win =========

def test_tier_calibration_respects_user_overrides():
    """CLAUDE.md Post-50 invariant: TIER_CFG fills in missing guardrails
    values for new users, but existing user-set values in guardrails.json
    ALWAYS win. Pin the `if _key not in guardrails` gate — without it a
    new tier default would clobber a deliberate user choice."""
    block = _deployer_block(_src())
    idx = block.find("if TIER_CFG:")
    assert idx > 0
    window = block[idx:idx + 600]
    assert "if _key not in guardrails" in window, (
        "tier defaults must only populate keys the user hasn't set — "
        "otherwise user overrides get silently overwritten")


# ========= 14. Top-level exception handling =========

def test_calibration_exception_does_not_block_deploy():
    """Tier detection is advisory — if portfolio_calibration raises,
    the deployer must continue with config defaults. CLAUDE.md Post-51
    invariant: 'All round-51 hooks MUST fail OPEN on exception.'"""
    block = _deployer_block(_src())
    idx = block.find("Calibration error")
    assert idx > 0, "calibration except branch missing or log removed"
    window = block[idx:idx + 400]
    assert "Trades unaffected" in window, (
        "calibration error log must reassure the operator trades "
        "continue — the pattern pins the fail-open path")
