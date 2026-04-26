"""Round-61 pt.93 — live-mode promotion gate UI in Settings →
Live Trading.

Pt.72 shipped ``live_mode_gate.check_live_mode_readiness`` and wired
it into ``handle_toggle_live_mode``. But the only place a user found
out which gate failed was the 400-response error string AFTER they
clicked Enable Live Trading. Pt.93 wires the missing UI piece:

  * Read-only POST endpoint ``/api/live-mode-readiness`` returning
    {ready, blockers, warnings, summary, metrics, thresholds,
    readiness_score}.
  * Promotion-gate panel in Settings → Live Trading rendering each
    of the 5 gates (closed trades / win rate / sharpe / drawdown /
    HIGH findings) with ✓/✗ icons + actual vs threshold.
  * JS handler ``refreshLiveModeReadiness()`` populates the panel.
  * ``switchSettingsTab('live')`` triggers the refresh.

Tests cover UI markup, JS handler shape, server dispatch, and the
mixin handler body.
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent
_DASH = (_HERE / "templates" / "dashboard.html").read_text()
_SERVER = (_HERE / "server.py").read_text()
_ACTIONS = (_HERE / "handlers" / "actions_mixin.py").read_text()


# ============================================================================
# Settings panel markup
# ============================================================================

def test_promotion_gate_panel_in_live_trading_panel():
    idx = _DASH.find('id="settingsPanel-live"')
    assert idx > 0
    end = _DASH.find('<!-- Sharing tab -->', idx)
    panel = _DASH[idx:end]
    assert "Promotion gate" in panel
    assert 'id="liveReadinessPanel"' in panel
    assert 'id="liveReadinessContent"' in panel
    assert 'onclick="refreshLiveModeReadiness()' in panel


def test_promotion_gate_panel_mentions_override():
    """Users need to know there's an escape hatch (the existing
    override_readiness flag) for cases where the gate is wrong."""
    idx = _DASH.find('id="liveReadinessPanel"')
    assert idx > 0
    block = _DASH[idx:idx + 1500]
    assert "override_readiness" in block


# ============================================================================
# JS handler
# ============================================================================

def test_refresh_live_mode_readiness_helper_defined():
    assert "async function refreshLiveModeReadiness" in _DASH
    assert "/api/live-mode-readiness" in _DASH


def test_refresh_live_mode_readiness_renders_per_gate_rows():
    idx = _DASH.find("async function refreshLiveModeReadiness")
    fn_block = _DASH[idx:idx + 5000]
    # Each of the 5 gates gets a row label.
    for label in ("Closed trades", "Win rate", "Sharpe ratio",
                  "Max drawdown", "HIGH audit findings"):
        assert label in fn_block


def test_refresh_live_mode_readiness_uses_blocker_keys():
    """The ✓/✗ for each row is driven by the gate-key set so the
    row state stays in lockstep with the server's blocker list."""
    idx = _DASH.find("async function refreshLiveModeReadiness")
    fn_block = _DASH[idx:idx + 5000]
    for key in ("insufficient_trades", "low_win_rate", "low_sharpe",
                "high_drawdown", "high_audit_findings"):
        assert key in fn_block


def test_refresh_live_mode_readiness_renders_status_pill():
    idx = _DASH.find("async function refreshLiveModeReadiness")
    fn_block = _DASH[idx:idx + 5000]
    assert "READY" in fn_block
    assert "NOT READY" in fn_block


def test_settings_tab_live_triggers_readiness_refresh():
    idx = _DASH.find("function switchSettingsTab")
    fn_block = _DASH[idx:idx + 1500]
    assert "name === 'live'" in fn_block
    assert "refreshLiveModeReadiness" in fn_block


# ============================================================================
# Server endpoint dispatch
# ============================================================================

def test_server_dispatches_live_mode_readiness_to_mixin():
    """Pt.93: server.py keeps a one-line dispatch; the body lives
    in actions_mixin.handle_live_mode_readiness."""
    idx = _SERVER.find('"/api/live-mode-readiness"')
    assert idx > 0
    block = _SERVER[idx:idx + 200]
    assert "handle_live_mode_readiness" in block
    # Body must NOT be inline — no live_mode_gate import in the
    # dispatch block.
    assert "live_mode_gate" not in block


# ============================================================================
# Mixin handler body
# ============================================================================

def test_handle_live_mode_readiness_uses_gate_module():
    idx = _ACTIONS.find("def handle_live_mode_readiness")
    assert idx > 0
    block = _ACTIONS[idx:idx + 2500]
    assert "live_mode_gate" in block
    assert "check_live_mode_readiness" in block


def test_handle_live_mode_readiness_requires_auth():
    idx = _ACTIONS.find("def handle_live_mode_readiness")
    block = _ACTIONS[idx:idx + 2500]
    assert "self.current_user" in block
    assert "401" in block


def test_handle_live_mode_readiness_returns_full_shape():
    idx = _ACTIONS.find("def handle_live_mode_readiness")
    block = _ACTIONS[idx:idx + 2500]
    for k in ("ready", "summary", "blockers", "warnings",
              "metrics", "thresholds", "readiness_score"):
        assert f'"{k}"' in block


def test_handle_live_mode_readiness_surfaces_thresholds():
    """Thresholds must come from the gate module's defaults so the
    UI's 'need ≥X' hints stay in sync with the server's enforced
    values."""
    idx = _ACTIONS.find("def handle_live_mode_readiness")
    block = _ACTIONS[idx:idx + 2500]
    for thresh in ("DEFAULT_MIN_CLOSED_TRADES", "DEFAULT_MIN_WIN_RATE",
                   "DEFAULT_MIN_SHARPE", "DEFAULT_MAX_DRAWDOWN_PCT"):
        assert thresh in block


def test_handle_live_mode_readiness_loads_journal_and_scorecard():
    idx = _ACTIONS.find("def handle_live_mode_readiness")
    block = _ACTIONS[idx:idx + 2500]
    assert "trade_journal.json" in block
    assert "scorecard.json" in block


def test_handle_live_mode_readiness_passes_audit_findings_through():
    """The pt.72 gate enforces 0 HIGH-severity audit findings, so
    the read-only endpoint must surface them too."""
    idx = _ACTIONS.find("def handle_live_mode_readiness")
    block = _ACTIONS[idx:idx + 2500]
    assert "audit_findings" in block
