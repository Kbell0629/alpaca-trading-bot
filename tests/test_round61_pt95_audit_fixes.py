"""Round-61 pt.95 — forensic audit-sweep regression tests.

After pt.94 the user requested a forensic audit; 5 parallel Explore
agents (security, DB/concurrency, trading logic, UI/UX/mobile,
tests/ops) returned ~30 findings. Pt.95 bundles the high-confidence
auto-fix wins. This test file pins each fix so a future regression
trips immediately.

Findings closed:

  Security (auth.py)
  ──────────────────
  * ZIP export hardened against symlink-traversal escape from
    ``users/<id>/`` (followlinks=False + realpath containment +
    ``..`` / absolute-path arcname guard).
  * Live (Round-45 dual-mode) encrypted credential columns
    (``alpaca_live_key_encrypted`` / ``alpaca_live_secret_encrypted``)
    added to the export sanitisation list — they had been missed
    when the columns landed.

  DB / concurrency (update_scorecard.py)
  ──────────────────────────────────────
  * Subprocess journal write now wraps the RMW in a flock against
    ``<JOURNAL_PATH>.lock`` matching cloud_scheduler's
    ``strategy_file_lock`` naming, so the scheduler thread's
    ``record_trade_close()`` can't be silently clobbered when the
    nightly subprocess saves an in-memory journal that's older than
    the on-disk version.

  UI / a11y (templates/dashboard.html)
  ───────────────────────────────────
  * pt.88 slippage + rationale panels now collapse to single-column
    on <600px so the per-strategy mini-grid + winners-vs-losers
    table read on mobile.
  * Admin audit filter selects + backtest selects gain ``for=`` /
    ``aria-label`` so screen readers can announce them.

  Ops (CI)
  ────────
  * `.github/workflows/ci.yml` gains a ``concurrency:`` cancellation
    group so force-pushes don't burn quota on stale builds.
  * `tests/test_round6.py` ratchet bumped 3410 → 3450 (+40 line
    cushion) so pt.96 / pt.97 dispatch one-liners don't require a
    per-PR ratchet bump.
  * `CLAUDE.md` developer-onboarding command no longer over-
    deselects two tests that have passed on real CI for several
    rounds.
"""
from __future__ import annotations

import os
import pathlib
import threading

import pytest


_HERE = pathlib.Path(__file__).resolve().parent.parent


# ============================================================================
# auth.py — ZIP export hardening
# ============================================================================

def test_export_sanitises_live_encrypted_columns():
    src = (_HERE / "auth.py").read_text()
    sanitise_block = src[src.find("# Strip sensitive fields"):
                          src.find("# Strip sensitive fields") + 600]
    for col in ("alpaca_live_key_encrypted",
                "alpaca_live_secret_encrypted"):
        assert col in sanitise_block, (
            f"{col} must be in the export sanitisation list — "
            "live credentials should not be included in the user-"
            "facing export by principle of least privilege.")


def test_export_walk_uses_followlinks_false():
    src = (_HERE / "auth.py").read_text()
    assert "os.walk(udir, followlinks=False)" in src, (
        "Round-61 pt.95: ZIP export must pass followlinks=False so "
        "a planted symlink can't make os.walk traverse outside the "
        "user dir.")


def test_export_has_path_traversal_guard():
    src = (_HERE / "auth.py").read_text()
    walk_idx = src.find("os.walk(udir, followlinks=False)")
    assert walk_idx > 0
    block = src[walk_idx:walk_idx + 1500]
    assert "rel.startswith(\"..\")" in block, (
        "ZIP arcname must be guarded against .. traversal.")
    assert "os.path.isabs(rel)" in block, (
        "ZIP arcname must be guarded against absolute paths.")
    assert "realpath" in block, (
        "ZIP must verify each file's resolved path stays inside udir.")


# ============================================================================
# update_scorecard.py — journal write under flock
# ============================================================================

def test_update_scorecard_imports_fcntl_for_locking():
    src = (_HERE / "update_scorecard.py").read_text()
    assert "import fcntl" in src
    assert "_journal_lock" in src
    assert ".lock" in src, (
        "Lock-file naming must match cloud_scheduler's "
        "strategy_file_lock so both contenders serialise on the "
        "same kernel-level lock.")


def test_update_scorecard_save_runs_under_lock():
    src = (_HERE / "update_scorecard.py").read_text()
    save_idx = src.find("safe_save_json(JOURNAL_PATH")
    assert save_idx > 0
    # The save call must be inside a `with _journal_lock(...)` block.
    block = src[max(0, save_idx - 800):save_idx + 200]
    assert "_journal_lock(JOURNAL_PATH)" in block


def test_update_scorecard_reload_under_lock_preserves_concurrent_writes():
    """Round-61 pt.95: under the lock we reload the on-disk journal
    so trades the scheduler appended during our metrics run aren't
    silently overwritten by our older in-memory copy."""
    src = (_HERE / "update_scorecard.py").read_text()
    save_idx = src.find("safe_save_json(JOURNAL_PATH")
    block = src[max(0, save_idx - 800):save_idx + 200]
    assert "load_json(JOURNAL_PATH)" in block, (
        "Must reload the latest journal under the lock before saving.")


# ============================================================================
# UI / a11y — pt.88 mobile + select labels
# ============================================================================

def test_pt88_panels_collapse_to_single_column_on_mobile():
    """The pt.88 slippage panel grid must be targeted by SOME
    @media (max-width: 600px) block so it collapses to single-
    column on mobile. (Pt.99 added another @media block earlier
    in the file for the user-dropdown anchor — so this test now
    searches for the rule by content, not by file order.)"""
    src = (_HERE / "templates" / "dashboard.html").read_text()
    # Find any media block that targets the pt.88 minmax(160px grid.
    needle = "minmax(160px"
    rule_idx = src.find(needle)
    assert rule_idx > 0
    # The enclosing @media block must be a 600px breakpoint.
    media_idx = src.rfind("@media (max-width: 600px) {", 0, rule_idx)
    assert media_idx > 0, (
        "pt.88 slippage panel rule found, but it isn't inside a "
        "@media (max-width: 600px) block — collapse won't fire.")


def test_admin_audit_select_has_label_association():
    src = (_HERE / "templates" / "dashboard.html").read_text()
    assert 'for="adminAuditFilter"' in src
    assert 'aria-label="Filter audit log by action type"' in src
    assert 'for="adminAuditUserFilter"' in src
    assert 'aria-label="Filter audit log by user"' in src


def test_backtest_selects_have_aria_label():
    src = (_HERE / "templates" / "dashboard.html").read_text()
    # JS-injected, no static <label> sibling exists — aria-label is
    # the only screen-reader association available.
    assert 'id="backtestStrategySelect" aria-label="Backtest strategy"' in src
    assert 'id="backtestStockSelector"' in src
    selector_block = src[src.find('id="backtestStockSelector"'):
                          src.find('id="backtestStockSelector"') + 400]
    assert "aria-label" in selector_block


# ============================================================================
# Ops — CI + ratchet + CLAUDE.md
# ============================================================================

def test_ci_workflow_has_concurrency_cancellation():
    src = (_HERE / ".github" / "workflows" / "ci.yml").read_text()
    assert "concurrency:" in src
    assert "cancel-in-progress: true" in src
    assert "${{ github.workflow }}-${{ github.ref }}" in src


def test_loc_ratchet_bumped_to_3450():
    src = (_HERE / "tests" / "test_round6.py").read_text()
    assert "assert server_lines < 3450" in src
    # The bump comment must reference pt.95 so future readers can see
    # why the cap moved.
    assert "round-61 pt.95" in src.lower() or "pt.95" in src


def test_claude_md_does_not_overspecify_deselects():
    """CLAUDE.md instructed devs to deselect 3 tests; CI only
    deselected 1. The two extras pass on real CI; pt.95 brings the
    onboarding command in line with reality."""
    src = (_HERE / "CLAUDE.md").read_text()
    # The trading-session deselect is real and stays.
    assert "test_trading_session_is_computed_live_not_from_stale_json" in src
    # The two stale deselects should NOT appear in the current
    # onboarding command (they may still be referenced in the
    # explanation paragraph, but not as an active flag).
    onboarding_idx = src.find("python3 -m pytest tests/")
    assert onboarding_idx > 0
    onboarding_line = src[onboarding_idx:src.find("\n", onboarding_idx)]
    assert "test_password_strength_rejects_weak" not in onboarding_line
    assert "test_ruff_clean_on_real_bug_rules" not in onboarding_line


# ============================================================================
# End-to-end: actual flock contention test for the journal lock
# ============================================================================

def test_journal_lock_serialises_concurrent_writers(tmp_path):
    """Wire-test: two threads both calling _journal_lock against the
    same path must serialise. Without the lock, the file's content
    after the race is unpredictable; with it, both writers' updates
    are preserved."""
    import sys
    sys.path.insert(0, str(_HERE))
    import update_scorecard as us  # noqa: E402

    journal_path = str(tmp_path / "trade_journal.json")
    pathlib.Path(journal_path).write_text('{"trades": [], "daily_snapshots": []}')

    results = []

    def writer(label):
        with us._journal_lock(journal_path):
            # Read, append, write — classic RMW.
            import json as _j
            with open(journal_path) as fh:
                doc = _j.load(fh)
            doc["trades"].append({"label": label})
            with open(journal_path, "w") as fh:
                _j.dump(doc, fh)
            results.append(label)

    threads = [threading.Thread(target=writer, args=(f"t{i}",))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    import json as _j
    with open(journal_path) as fh:
        final = _j.load(fh)
    # Without the lock, lost-update means len(trades) < 8. With the
    # lock, every writer's append survives.
    assert len(final["trades"]) == 8
    assert sorted(t["label"] for t in final["trades"]) == \
        sorted(f"t{i}" for i in range(8))
