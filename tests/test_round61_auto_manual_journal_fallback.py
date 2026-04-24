"""
Round-61 user-reported bug (2026-04-24):
  Dashboard labeled SOXL (-29 shares short) and HIMS260508P00027000
  (wheel put option) as "MANUAL" — but both were deployed by the
  auto-deployer. User quote: "these were both auto trades also".

Root cause: server.py._mark_auto_deployed looked ONLY at strategy
files on disk. If a strategy file gets cleaned up, renamed, or never
existed (old deploy path, manual filesystem edit, different-mode
directory), the position gets labeled MANUAL even though the trade
journal still has an open entry showing it was auto-deployed.

Fix: add a journal-based fallback. If no strategy file matches a
position's symbol, consult trade_journal.json — if there's an open
entry with deployer in (cloud_scheduler, wheel_strategy,
error_recovery), label AUTO with the journal's strategy field.

These pin the fallback so a future refactor can't silently drop it.
"""
from __future__ import annotations

import json
import os


def test_auto_deployed_fallback_code_present():
    """The journal-fallback path must exist in _mark_auto_deployed."""
    with open("server.py") as f:
        src = f.read()
    assert "journal_symbol_to_strategy" in src, (
        "journal fallback dict missing — positions without a matching "
        "strategy file will be mis-labeled MANUAL")
    # The auto-deployer tuple must include every known backend. Previously
    # asserted as a single multi-string literal; we now check each value
    # independently so future entries can be added (or the tuple reformatted
    # with comments) without tripping this pin. Round-61 pt.8 added
    # "wheel_auto_deploy" — see the dedicated test below for why.
    for expected in ("cloud_scheduler", "wheel_strategy", "error_recovery"):
        assert f'"{expected}"' in src, (
            f"journal fallback must recognize '{expected}' as an auto-deploy "
            "backend — missing it means positions opened by that backend "
            "get mis-labeled MANUAL")


def test_wheel_auto_deploy_is_recognized_as_auto():
    """Round-61 pt.8 fix: wheel_strategy.py:791 writes
    deployer='wheel_auto_deploy' for sold-to-open put entries. Before
    this fix, that value was NOT in _mark_auto_deployed's allowed
    tuple, so every DKNG / HIMS / etc. wheel-put position showed up as
    MANUAL in the dashboard even though the wheel bot auto-deployed
    them. User reported 5 positions (CRDO long, DKNG put, HIMS put,
    INTC long, SOXL short) all mislabeled MANUAL; the two OCC-option
    ones had `deployer='wheel_auto_deploy'` in the journal.

    Pin: the allowed tuple must include 'wheel_auto_deploy'. Do not
    remove without removing the wheel_strategy.py write-site in lockstep."""
    with open("server.py") as f:
        src = f.read()
    assert '"wheel_auto_deploy"' in src, (
        "_mark_auto_deployed must treat 'wheel_auto_deploy' as an "
        "auto-deployer — wheel_strategy.record_trade_open writes that "
        "string and without it every wheel-sold put shows as MANUAL.")

    # Cross-check the producer is still writing that exact string.
    with open("wheel_strategy.py") as f:
        ws_src = f.read()
    assert 'deployer="wheel_auto_deploy"' in ws_src, (
        "wheel_strategy.py must keep writing deployer='wheel_auto_deploy' — "
        "the server-side allowlist pins this exact string.")


def test_journal_fallback_only_considers_open_entries():
    """A closed entry in the journal shouldn't make a CURRENT position
    show as auto-deployed — only the latest OPEN entry should drive
    the AUTO label."""
    with open("server.py") as f:
        src = f.read()
    # The status filter
    assert 'if (_t.get("status") or "open") != "open"' in src, (
        "journal fallback must skip non-open entries — otherwise a "
        "historically-auto-deployed symbol would keep its AUTO label "
        "after being manually re-opened")


def test_journal_fallback_walks_newest_first():
    """If a symbol has multiple journal entries (e.g. re-opened after
    close), the most-recent OPEN entry should win — so walk reversed."""
    with open("server.py") as f:
        src = f.read()
    # The reversed-walk pattern
    assert "for _t in reversed(_journal.get(\"trades\") or [])" in src, (
        "journal fallback must walk trades newest-first (reversed) so "
        "the most recent open wins when a symbol has been re-opened")
    # The setdefault (not overwrite) pattern
    assert "journal_symbol_to_strategy.setdefault(_sym, _strat)" in src, (
        "walk-newest-first plus setdefault ensures we pick the most "
        "recent open per symbol — a later overwrite pattern would "
        "pick the oldest, which is wrong")


def test_options_journal_lookup_handles_occ_and_underlying():
    """Wheel option positions may be journaled under either the full
    OCC contract symbol OR the underlying — the lookup must try both
    for option positions, with underlying as the fallback."""
    with open("server.py") as f:
        src = f.read()
    # The dual-lookup code
    assert 'asset_class == "us_option"' in src
    assert 'journal_symbol_to_strategy.get(journal_lookup, "")' in src
    # Fallback to underlying lookup for options
    idx = src.find('journal_symbol_to_strategy.get(journal_lookup, "")')
    assert idx > 0
    block = src[idx:idx + 500]
    assert 'journal_symbol_to_strategy.get(lookup, "")' in block, (
        "option-position fallback missing underlying-symbol journal "
        "lookup — wheel puts might be journaled under the underlying "
        "(e.g. HIMS) not the OCC symbol (e.g. HIMS260508P00027000)")


def test_user_dir_param_passed_to_mark_auto_deployed():
    """The caller must pass user_dir so the function can find the
    journal file. Previously _mark_auto_deployed only took strats_dir."""
    with open("server.py") as f:
        src = f.read()
    assert "user_dir=user_dir" in src or "_mark_auto_deployed(positions, strats_dir, user_dir" in src, (
        "caller must pass user_dir= to _mark_auto_deployed — without "
        "it the function can't find trade_journal.json and the fallback "
        "is a no-op")


def test_function_signature_includes_user_dir():
    """The function signature must accept user_dir (optional for
    backwards compat — old callers still work but get no fallback)."""
    with open("server.py") as f:
        src = f.read()
    assert "def _mark_auto_deployed(positions, strats_dir, user_dir=None):" in src, (
        "_mark_auto_deployed signature must include user_dir=None so "
        "the journal-fallback path is reachable")


def test_behavior_wheel_put_labeled_auto_via_journal(tmp_path):
    """Round-61 pt.8 behavioral pin for the wheel_auto_deploy fix.

    Given:
      * No strategy files on disk (empty strats_dir)
      * trade_journal.json with an open wheel put entry whose deployer
        is 'wheel_auto_deploy' (what wheel_strategy.py:791 writes)

    When _mark_auto_deployed runs over a position with the matching OCC
    contract symbol (us_option asset class), the position MUST be
    labeled _auto_deployed=True with strategy="wheel".
    """
    import json as _json
    # server is already imported by conftest/other tests; no reload needed.
    # The function is pure over its (positions, strats_dir, user_dir) args.
    import server as _server

    user_dir = tmp_path / "u1"
    user_dir.mkdir()
    strats_dir = user_dir / "strategies"
    strats_dir.mkdir()
    journal = {
        "trades": [
            {
                "timestamp": "2026-04-20T10:00:00",
                "symbol": "DKNG260515P00021000",
                "strategy": "wheel",
                "deployer": "wheel_auto_deploy",
                "status": "open",
                "side": "sell_short",
                "qty": -1,
                "price": 1.00,
            }
        ],
    }
    (user_dir / "trade_journal.json").write_text(_json.dumps(journal))

    positions = [
        {"symbol": "DKNG260515P00021000", "asset_class": "us_option", "qty": "-1"}
    ]
    out = _server._mark_auto_deployed(
        positions, str(strats_dir), user_dir=str(user_dir))
    assert out[0]["_auto_deployed"] is True, (
        f"Wheel-put position should be AUTO via journal fallback; got "
        f"{out[0]}")
    assert out[0]["_strategy"] == "wheel"


def test_strategy_file_still_preferred_over_journal():
    """If BOTH a strategy file AND a journal entry match a symbol,
    the strategy file wins (it's the source of truth for current
    state; the journal tracks history). Only fall back to journal
    when no strategy file matches."""
    with open("server.py") as f:
        src = f.read()
    # The fallback is inside `if not strat:` — meaning the strategy
    # file result is checked first.
    idx = src.find("strat = symbol_to_strategy.get(lookup, \"\")")
    assert idx > 0
    # Next ~600 chars should include the `if not strat:` guard
    block = src[idx:idx + 800]
    assert "if not strat:" in block, (
        "strategy-file lookup must be checked first; journal fallback "
        "should only fire when strat is empty — otherwise stale strategy "
        "files could be overridden by old journal entries")
