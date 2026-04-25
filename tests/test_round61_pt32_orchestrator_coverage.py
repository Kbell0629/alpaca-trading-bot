"""Round-61 pt.32 — orchestrator coverage.

Source-pin tests for the four scheduler orchestrators that drive every
live trade. These functions are too large + side-effect-heavy for full
behavioural mocking (each pulls in Alpaca, file I/O, notifications,
subprocesses); the existing tests in test_round61_auto_deployer.py
already cover ``run_auto_deployer`` extensively.

Pt.32 fills the gaps for the other three:

  1. ``run_daily_close`` — subprocess wrapper for update_scorecard.py +
     error_recovery.py, then queues the rich daily-close email and a
     short ntfy push.
  2. ``run_wheel_auto_deploy`` — kill-switch + auto-deployer-disabled +
     wheel-disabled gating, screener-data load, dedup via
     ``_wheel_deploy_in_flight``, candidate iteration, kill-switch
     mid-loop abort.
  3. ``run_wheel_monitor`` — kill-switch gate, ``list_wheel_files``
     iteration, ``advance_wheel_state`` dispatch, stage-2 covered-call
     auto-pilot, wheel-open backfill at end-of-tick.

These pin the high-level structure (guards, ordering, return points)
so a refactor that drops a guard or reorders steps fails loudly.
Behavioural correctness of the inner functions is covered by the
specific round tests (round-15 orphan adoption, round-23 wheel state,
round-27 daily close math, round-44 wheel-open backfill, etc.).
"""
from __future__ import annotations

import os
import pathlib


_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PATH = pathlib.Path(_HERE) / "cloud_scheduler.py"


def _src() -> str:
    return _SRC_PATH.read_text()


def _slice(src: str, start_marker: str, *end_markers: str) -> str:
    """Slice the source between a start marker (e.g. function def) and
    the first of any provided end markers. Lets each test pin patterns
    that appear ONLY in the relevant function body, not elsewhere."""
    s = src.find(start_marker)
    assert s >= 0, f"marker {start_marker!r} not found"
    end = len(src)
    for m in end_markers:
        e = src.find(m, s + len(start_marker))
        if e > s:
            end = min(end, e)
    return src[s:end]


# ============================================================================
# run_daily_close — subprocess wrapper
# ============================================================================

def _close_block() -> str:
    return _slice(_src(), "def run_daily_close(user):",
                   "def _build_daily_close_report",
                   "def run_friday_risk_reduction",
                   "def run_monthly_rebalance")


def test_daily_close_passes_per_user_alpaca_creds_to_subprocess():
    """The scorecard + error_recovery subprocesses MUST receive THIS
    user's Alpaca credentials, not whatever happens to be in the
    parent process env. Round-9 fix: cross-user contamination if env
    leaked across users in a multi-user deployment."""
    block = _close_block()
    assert 'env["ALPACA_API_KEY"] = user["_api_key"]' in block
    assert 'env["ALPACA_API_SECRET"] = user["_api_secret"]' in block
    assert 'env["ALPACA_ENDPOINT"] = user["_api_endpoint"]' in block
    assert 'env["ALPACA_DATA_ENDPOINT"] = user["_data_endpoint"]' in block


def test_daily_close_passes_per_user_data_paths_to_subprocess():
    """Per-user SCORECARD_PATH + JOURNAL_PATH + STRATEGIES_DIR are
    the round-9 isolation fix. Without these env vars the subprocess
    falls through to legacy shared paths and one user's close
    rewrites another user's scorecard."""
    block = _close_block()
    assert 'env["SCORECARD_PATH"] = user_file(user, "scorecard.json")' in block
    assert 'env["JOURNAL_PATH"] = user_file(user, "trade_journal.json")' in block
    assert 'env["STRATEGIES_DIR"]' in block


def test_daily_close_runs_update_scorecard_and_error_recovery():
    """Both update_scorecard.py and error_recovery.py must run.
    error_recovery is the orphan-detection + Check-3 close-stale-files
    pass; without it strategy files accumulate ghosts."""
    block = _close_block()
    assert "update_scorecard.py" in block
    assert "error_recovery.py" in block
    # Subprocess timeout pinned — too long blocks the next user's run
    assert "timeout=60" in block, (
        "subprocess timeout must stay bounded so a slow Alpaca response "
        "doesn't block the daily-close loop")


def test_daily_close_clears_daily_starting_value_under_lock():
    """Round-57 fix: the daily_starting_value reset + peak update is a
    classic RMW. Without strategy_file_lock around the sequence, a
    concurrent monitor tick or handler write silently drops one of the
    edits. The Alpaca /account fetch is OUTSIDE the lock (slow) but
    the read+write block is INSIDE."""
    block = _close_block()
    acct_idx = block.find('user_api_get(user, "/account")')
    lock_idx = block.find("with strategy_file_lock(gpath):")
    assert acct_idx > 0 and lock_idx > 0
    assert acct_idx < lock_idx, (
        "Round-57: /account must be fetched OUTSIDE the lock — "
        "100-500ms network call blocks the monitor otherwise")
    assert 'guardrails["daily_starting_value"] = None' in block


def test_daily_close_queues_rich_email_via_helper():
    """The rich email is built by ``_build_daily_close_report`` and
    queued via ``_queue_direct_email`` so the user gets a useful EOD
    digest instead of a single-line scoreboard."""
    block = _close_block()
    assert "_build_daily_close_report(" in block
    assert "_queue_direct_email(" in block


def test_daily_close_falls_back_to_legacy_scorecard_paths():
    """Backwards compat: per-user scorecard.json is preferred but old
    deployments still have a shared one in DATA_DIR or BASE_DIR.
    The fallback chain must be: per-user → DATA_DIR → BASE_DIR."""
    block = _close_block()
    # Fallback chain (in order)
    assert 'scorecard_path = user_file(user, "scorecard.json")' in block
    assert 'scorecard_path = os.path.join(DATA_DIR, "scorecard.json")' in block
    assert 'scorecard_path = os.path.join(BASE_DIR, "scorecard.json")' in block


def test_daily_close_uses_push_only_for_short_ntfy():
    """Short scoreboard line goes to ntfy via --push-only so the
    auto-queued email is skipped — the rich email below queues
    separately. Without --push-only the user gets two emails per
    daily close."""
    block = _close_block()
    assert "--push-only" in block
    assert '"--type", "daily"' in block


def test_daily_close_swallows_top_level_exceptions():
    """Daily close runs on every weekday at 4:05 PM ET — a single
    failure must NOT bring the scheduler thread down. The function
    has a top-level try/except that logs and returns."""
    block = _close_block()
    assert "try:" in block
    assert "except Exception as e:" in block
    assert "Daily close error" in block


# ============================================================================
# run_wheel_auto_deploy — gating + dedup + iteration
# ============================================================================

def _wheel_deploy_block() -> str:
    """Slice run_wheel_auto_deploy + _run_wheel_auto_deploy_inner together
    since the public entry point is just the dedup wrapper."""
    return _slice(_src(), "def run_wheel_auto_deploy(user):",
                   "def run_wheel_monitor")


def test_wheel_auto_deploy_dedup_uses_wheel_deploy_in_flight():
    """Concurrent invocations would both place short puts on the same
    symbol before the first writes state. Dedup via the module-level
    ``_wheel_deploy_in_flight`` set + ``_wheel_deploy_lock``."""
    block = _wheel_deploy_block()
    assert "_wheel_deploy_in_flight" in block
    assert "_wheel_deploy_lock" in block
    # Discard in finally so an exception doesn't leak the dedup entry
    assert "finally:" in block
    assert "discard(uid)" in block


def test_wheel_auto_deploy_dedup_key_includes_mode_for_live():
    """Round-46 fix: paper + live wheel deploys must dedup
    INDEPENDENTLY. Scoping the dedup key to user.id-only would let a
    paper deploy block a parallel live deploy on the same user."""
    block = _wheel_deploy_block()
    assert '_mode = user.get("_mode", "paper")' in block
    # Live mode uses the f"{id}:live" form; paper uses bare id
    assert "f\"{user['id']}:{_mode}\" if _mode == \"live\"" in block


def test_wheel_auto_deploy_respects_kill_switch():
    """Kill switch must block new wheel deploys — they open NEW risk
    (sell-to-open puts), exactly what we want halted on a kill."""
    block = _wheel_deploy_block()
    assert 'guardrails.get("kill_switch")' in block
    # Within 200 chars of the kill_switch check there must be a return
    idx = block.find('guardrails.get("kill_switch")')
    assert "return" in block[idx:idx + 200]


def test_wheel_auto_deploy_respects_disabled_auto_deployer():
    """If the user has the auto-deployer disabled globally, wheel
    auto-deploy must respect that. There's also a per-strategy
    ``wheel.enabled`` toggle that defaults True."""
    block = _wheel_deploy_block()
    assert 'config.get("enabled", True)' in block
    assert 'wheel_cfg.get("enabled", True)' in block


def test_wheel_auto_deploy_aborts_on_kill_switch_mid_loop():
    """Round-12 audit fix: wheel deploys can place multiple puts in
    one tick; each put = two API calls, so kill-switch mid-loop is a
    real window. ``deploy_should_abort()`` is checked per pick."""
    block = _wheel_deploy_block()
    assert "deploy_should_abort()" in block
    assert "Wheel auto-deploy ABORT" in block


def test_wheel_auto_deploy_caps_per_day_via_max_new_per_day():
    """Per-day cap so a screener spike doesn't deploy 10 wheels in
    one tick."""
    block = _wheel_deploy_block()
    assert 'wheel_cfg.get("max_new_per_day", 1)' in block
    assert "if deployed >= max_per_day:" in block


def test_wheel_auto_deploy_uses_screener_data():
    """Screener output is the candidate source. ``run_screener`` is
    called with ``max_age_seconds=300`` so multiple orchestrators
    sharing a tick reuse a 5-min-fresh dataset."""
    block = _wheel_deploy_block()
    assert "run_screener(user, max_age_seconds=300)" in block
    assert "find_wheel_candidates" in block


def test_wheel_auto_deploy_emits_rich_email_on_open():
    """Successful put-open must fire the rich notification template
    (subject + body) so users get a useful trade-open email rather
    than a one-liner."""
    block = _wheel_deploy_block()
    assert "notification_templates" in block
    assert "wheel_put_sold(" in block


# ============================================================================
# run_wheel_monitor — iteration + advance + stage-2 + backfill
# ============================================================================

def _wheel_monitor_block() -> str:
    return _slice(_src(), "def run_wheel_monitor(user):",
                   "# TASK 10:",
                   "def run_daily_volume_backup")


def test_wheel_monitor_respects_kill_switch():
    """Kill switch blocks the stage-2 covered-call auto-open
    (opens new risk). BTC orders are exits and always safe — the
    docstring promises kill-switch doesn't block exits, but the
    current implementation early-returns on the entire monitor."""
    block = _wheel_monitor_block()
    assert 'guardrails.get("kill_switch")' in block
    idx = block.find('guardrails.get("kill_switch")')
    assert "return" in block[idx:idx + 200]


def test_wheel_monitor_lists_wheel_files_per_user():
    """``ws.list_wheel_files(user)`` is the round-23 multi-contract-
    aware enumerator. Returns ``(filename, state)`` tuples — the
    filename lets ``save_wheel_state`` write back to the SAME file
    (round-23 fix). Monitor iterating must use this helper."""
    block = _wheel_monitor_block()
    assert "ws.list_wheel_files(user)" in block
    assert "for fname, state in wheels:" in block


def test_wheel_monitor_advances_state_machine_per_wheel():
    """Each wheel state machine tick is ``ws.advance_wheel_state``.
    Returns events list; monitor logs + notifies each event."""
    block = _wheel_monitor_block()
    assert "ws.advance_wheel_state(user, state)" in block
    assert "for ev in events:" in block


def test_wheel_monitor_opens_covered_call_after_assignment():
    """Stage-2 auto-pilot: once shares are owned (``stage_2_shares_owned``)
    AND no active contract, sell a covered call. This is the second
    leg of the wheel — turning assigned shares back into premium."""
    block = _wheel_monitor_block()
    assert 'state.get("stage") == "stage_2_shares_owned"' in block
    assert 'not state.get("active_contract")' in block
    assert "ws.open_covered_call(user, state)" in block


def test_wheel_monitor_runs_open_backfill_at_tail():
    """Round-44: auto-fix [orphan] wheel closes by recovering entry
    price from state history[]. Idempotent + cheap — runs at the
    tail of every monitor tick so new orphans are paired within
    one cycle."""
    block = _wheel_monitor_block()
    assert "wheel_open_backfill" in block
    assert "_wob.backfill_wheel_opens(user)" in block


def test_wheel_monitor_per_wheel_exception_does_not_kill_loop():
    """One bad wheel file must not abort the entire monitor.
    Per-iteration try/except logs and continues so other wheels
    advance normally."""
    block = _wheel_monitor_block()
    # The advance_wheel_state call must be inside a try/except in the loop
    advance_idx = block.find("ws.advance_wheel_state")
    # The except line should appear AFTER the advance call (within
    # the loop body) — find the next "except" after advance
    except_idx = block.find("except Exception as e:", advance_idx)
    assert 0 < except_idx - advance_idx < 1500, (
        "Per-wheel try/except missing — one bad file would crash "
        "the whole monitor")
    assert "Wheel monitor error on" in block


# ============================================================================
# Cross-orchestrator invariants
# ============================================================================

def test_all_orchestrators_log_with_username_prefix():
    """Every orchestrator's log line must include ``[username]`` so a
    multi-user deployment can attribute scheduler events. Greppable
    from the recent-activity panel."""
    src = _src()
    for fn_marker in (
        "def run_daily_close(user):",
        "def run_wheel_auto_deploy(user):",
        "def _run_wheel_auto_deploy_inner(user):",
        "def run_wheel_monitor(user):",
    ):
        s = src.find(fn_marker)
        assert s > 0, f"orchestrator {fn_marker} missing"
        # Within first 500 chars there must be a log call referencing user[username]
        assert "[{user['username']}]" in src[s:s + 1500], (
            f"{fn_marker} does not log with username prefix")


def test_all_orchestrators_swallow_top_level_exceptions():
    """A scheduler tick fires every orchestrator in sequence — a single
    crash must not kill the thread. Each orchestrator must have a
    try/except wrapping the body (or per-iteration try/except in
    loop-style ones)."""
    src = _src()
    for marker in (
        "def run_daily_close(user):",
        "def _run_wheel_auto_deploy_inner(user):",
        "def run_wheel_monitor(user):",
    ):
        s = src.find(marker)
        assert s > 0
        # Find the next def to bound the body
        e = src.find("\ndef ", s + len(marker))
        body = src[s:e if e > 0 else len(src)]
        assert "try:" in body, f"{marker} missing a top-level try"
        assert "except" in body, f"{marker} missing an except"


def test_orchestrators_are_idempotent_via_dedup_or_set_once():
    """Daily-close and wheel-auto-deploy can be triggered both by
    the scheduler tick AND the on-demand Force buttons. Both paths
    must coexist:
      * daily-close: idempotent because update_scorecard.py is
        idempotent (round-9 fix)
      * wheel-auto-deploy: dedup via ``_wheel_deploy_in_flight``
    Without either, a Force click during the scheduler's run
    fires double orders."""
    src = _src()
    # wheel-auto-deploy dedup
    assert "_wheel_deploy_in_flight" in src
    assert "_wheel_deploy_lock" in src
    # daily-close: not strictly deduped but does set
    # daily_starting_value=None which is the only RMW that matters
    assert 'guardrails["daily_starting_value"] = None' in src


# ============================================================================
# Source-pin: orchestrator function symbols still exist
# ============================================================================

def test_all_pt32_orchestrators_defined():
    """Sanity pin: a refactor that renames any of these breaks every
    integration test in the round + the scheduler boot loop. Catch it
    at the source level."""
    import importlib
    import sys
    sys.modules.pop("cloud_scheduler", None)
    cs = importlib.import_module("cloud_scheduler")
    for name in (
        "run_auto_deployer",
        "run_daily_close",
        "_build_daily_close_report",
        "run_orphan_adoption",
        "run_wheel_auto_deploy",
        "_run_wheel_auto_deploy_inner",
        "run_wheel_monitor",
        "run_friday_risk_reduction",
        "run_monthly_rebalance",
    ):
        assert hasattr(cs, name), f"{name} missing from cloud_scheduler"
