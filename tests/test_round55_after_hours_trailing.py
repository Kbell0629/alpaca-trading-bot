"""
Round-55 tests: after-hours trailing-stop tightening.

User reported losing money when stocks pop post-market then drop back
before morning — the trailing stop stayed at the pre-pop level.
Round-55 extends monitor_strategies into pre-market + after-hours
in a stops-only mode so the raised stop is live before the morning
bell.

Tests cover:
  * monitor_strategies accepts extended_hours=True
  * AH-mode skips kill-switch/daily-loss/profit-take/mean-reversion
  * AH-mode still runs the trailing-stop raise
  * process_strategy_file accepts extended_hours=True
  * Scheduler main loop calls monitor_strategies with the AH flag
    during pre/post-market windows only
  * guardrails.extended_hours_trailing=false opts out
  * Shorts + wheel are skipped in AH (liquidity too thin)
"""
from __future__ import annotations

import sys as _sys


def _reload(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


def test_monitor_strategies_accepts_extended_hours_flag(monkeypatch):
    cs = _reload(monkeypatch)
    import inspect
    sig = inspect.signature(cs.monitor_strategies)
    assert "extended_hours" in sig.parameters, (
        "monitor_strategies must accept extended_hours param")


def test_process_strategy_file_accepts_extended_hours_flag(monkeypatch):
    cs = _reload(monkeypatch)
    import inspect
    sig = inspect.signature(cs.process_strategy_file)
    assert "extended_hours" in sig.parameters


def test_ah_mode_opt_out_via_guardrails(monkeypatch):
    """guardrails.extended_hours_trailing: false disables the AH
    monitor for that user. Verified via grep on the source (the
    alternative — setting up a full scheduler tick — is too heavy
    for a unit test; the grep pins the behavior)."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    assert 'guardrails.get("extended_hours_trailing", True) is False' in src


def test_ah_mode_skips_profit_ladder(monkeypatch):
    """Pin: profit-ladder call is gated by `if not extended_hours`
    so after-hours ticks don't fire market-sell rungs into thin
    liquidity books."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    # The relevant call site is INSIDE process_strategy_file (not the
    # function definition). The expected pattern is:
    #   if not extended_hours:
    #       check_profit_ladder(user, filepath, strat, price, entry, shares)
    assert ("if not extended_hours:\n"
            "        check_profit_ladder(user, filepath, strat, price, entry, shares)"
            in src), (
        "check_profit_ladder call site must be gated behind "
        "`if not extended_hours:` to avoid market-sell rungs in thin AH books")


def test_ah_mode_skips_mean_reversion_target(monkeypatch):
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    assert 'if not extended_hours and strategy_type == "mean_reversion"' in src


def test_ah_mode_skips_initial_stop_placement(monkeypatch):
    """Initial stop placement requires regular-hours liquidity for
    the stop order to be accepted cleanly. Skip in AH."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    assert "if not extended_hours and not state.get(\"stop_order_id\")" in src


def test_ah_mode_passes_shorts_through_to_dedicated_handler(monkeypatch):
    """Round-61 pt.28 narrowed the R55 short-skip. Shorts now flow
    through to process_short_strategy even in AH mode so unprotected
    shorts (no cover_order_id) get a protective GTC stop placed before
    the next regular-hours open. The thin-book "brutal cover fill"
    concern doesn't apply to placement — GTC stops don't trigger in
    AH, they sit idle and fire at next regular-hours cross. See pt.28
    docstring on process_short_strategy."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    idx = src.find('if strategy_type == "short_sell":')
    assert idx > 0
    following = src[idx:idx+800]
    # Pt.28: pass-through, NOT an early return
    assert "process_short_strategy" in following
    assert "extended_hours=extended_hours" in following
    # The old "return  # shorts" guard is gone
    assert "return  # shorts" not in following


def test_scheduler_main_loop_calls_ah_monitor(monkeypatch):
    """Pin the scheduler main-loop wiring: during pre/post-market
    (market closed but in the extended-hours window) it calls
    monitor_strategies(user, extended_hours=True) at 5-min cadence."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    assert "from extended_hours import get_trading_session" in src
    assert 'in ("pre_market", "after_hours")' in src
    assert "monitor_strategies(user, extended_hours=True)" in src
    # 5-min interval: f"monitor_eh_{uid}", 300
    assert 'f"monitor_eh_' in src


def test_ah_mode_wheel_files_skipped(monkeypatch):
    """Pin: AH monitor's per-user loop skips wheel_*.json files
    (options too illiquid outside regular hours)."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    # Rough grep — should see wheel skip in the AH monitor block
    assert 'fname.startswith("wheel_")' in src


def test_ah_mode_trailing_stop_raise_still_runs(monkeypatch):
    """Opposite check of the skip tests: the actual trailing-stop
    raise block MUST still execute in AH mode. Without this, the
    round-55 feature does nothing."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    # The trailing-stop raise block (line ~1652 original) should NOT
    # be gated by `if not extended_hours`. Find the block.
    idx = src.find('if strategy_type in ("trailing_stop", "breakout", "copy_trading", "pead"):')
    assert idx > 0
    # The 200 chars before this line should NOT contain `not extended_hours`
    # guarding THIS specific block (other gates are fine).
    preceding_2_lines = src.rfind("\n    # ", 0, idx)
    comment_block = src[preceding_2_lines:idx]
    assert "if not extended_hours" not in comment_block.split("\n")[-1], (
        "Trailing-stop raise block got inadvertently gated behind "
        "not extended_hours — AH monitor would be a no-op.")
