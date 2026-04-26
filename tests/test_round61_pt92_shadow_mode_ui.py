"""Round-61 pt.92 — shadow mode UI in Settings → Live Trading.

Pt.86 shipped the shadow_mode pure module + cloud_scheduler hook
but no way for users to enable it from the dashboard. Pt.92 wires
the missing UI piece:

  * Checkbox in Settings → Live Trading (persists in
    guardrails.live_shadow_mode)
  * Recent-events panel rendering the per-user shadow_log.json
  * Two new API endpoints:
      POST /api/set-shadow-mode  {enabled: bool}
      GET  /api/shadow-log       {events, summary, active, source}

Tests cover:
  * UI markup landed in the Live Trading panel
  * JS handlers (toggleShadowMode, refreshShadowLog) defined
  * Endpoints handled in server.py
  * switchSettingsTab('live') triggers refreshShadowLog
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

def test_shadow_mode_section_in_live_trading_panel():
    idx = _DASH.find('id="settingsPanel-live"')
    assert idx > 0
    end = _DASH.find('<!-- Sharing tab -->', idx)
    panel = _DASH[idx:end]
    # Section heading + description present.
    assert "Shadow Mode" in panel
    # Checkbox with the right id + handler.
    assert 'id="setShadowMode"' in panel
    assert 'onchange="toggleShadowMode' in panel
    # Recent-events container.
    assert 'id="shadowLogPanel"' in panel
    assert 'id="shadowLogContent"' in panel


def test_shadow_mode_section_explains_failsafe():
    """The user needs to know that an error in the shadow check
    falls through to the real POST."""
    idx = _DASH.find('id="setShadowMode"')
    assert idx > 0
    block = _DASH[max(0, idx - 1500):idx]
    assert "Fail-safe" in block or "fail-safe" in block.lower() \
        or "fail_safe" in block.lower()


# ============================================================================
# JS handlers
# ============================================================================

def test_toggle_shadow_mode_helper_defined():
    assert "async function toggleShadowMode" in _DASH
    assert "/api/set-shadow-mode" in _DASH


def test_refresh_shadow_log_helper_defined():
    assert "async function refreshShadowLog" in _DASH
    assert "/api/shadow-log" in _DASH


def test_toggle_shadow_mode_reverts_checkbox_on_error():
    """When the POST fails, the checkbox should snap back so the
    UI reflects actual state."""
    idx = _DASH.find("async function toggleShadowMode")
    fn_block = _DASH[idx:idx + 1500]
    assert "cb.checked = !enabled" in fn_block


def test_refresh_shadow_log_handles_empty_log():
    idx = _DASH.find("async function refreshShadowLog")
    fn_block = _DASH[idx:idx + 3500]
    assert "No shadow events yet" in fn_block


def test_refresh_shadow_log_renders_per_event_row():
    idx = _DASH.find("async function refreshShadowLog")
    fn_block = _DASH[idx:idx + 3500]
    # Each row shows action / symbol / strategy / qty / price.
    for piece in ("action", "symbol", "strategy", "qty", "price"):
        assert piece in fn_block


def test_settings_tab_live_triggers_log_refresh():
    """Opening the Live Trading tab lazy-loads the shadow log."""
    idx = _DASH.find("function switchSettingsTab")
    fn_block = _DASH[idx:idx + 1500]
    assert "name === 'live'" in fn_block
    assert "refreshShadowLog" in fn_block


# ============================================================================
# Server endpoints
# ============================================================================

def test_server_dispatches_set_shadow_mode_to_mixin():
    """Pt.92: server.py keeps a one-line dispatch; the body lives
    in actions_mixin.handle_set_shadow_mode (LOC-ratchet rule)."""
    idx = _SERVER.find('"/api/set-shadow-mode"')
    assert idx > 0
    block = _SERVER[idx:idx + 400]
    assert "handle_set_shadow_mode" in block
    # The big body should NOT be inline.
    assert "live_shadow_mode" not in block or "delegated" in block


def test_handle_set_shadow_mode_persists_to_guardrails():
    idx = _ACTIONS.find("def handle_set_shadow_mode")
    assert idx > 0
    block = _ACTIONS[idx:idx + 1500]
    assert "live_shadow_mode" in block
    assert "guardrails.json" in block
    assert "save_json" in block


def test_handle_set_shadow_mode_requires_auth():
    idx = _ACTIONS.find("def handle_set_shadow_mode")
    block = _ACTIONS[idx:idx + 1500]
    assert "self.current_user" in block
    assert "401" in block


def test_handle_shadow_log_returns_events_summary_active():
    idx = _ACTIONS.find("def handle_shadow_log")
    assert idx > 0
    block = _ACTIONS[idx:idx + 2500]
    assert "get_shadow_log" in block
    assert "summarize_shadow_log" in block
    assert "is_shadow_mode_active" in block
    for k in ("active", "source", "events", "summary"):
        assert f'"{k}"' in block


def test_handle_shadow_log_uses_session_mode_data_dir():
    idx = _ACTIONS.find("def handle_shadow_log")
    block = _ACTIONS[idx:idx + 2500]
    assert "session_mode" in block
    assert "user_data_dir" in block


def test_handle_shadow_log_surfaces_resolution_source():
    idx = _ACTIONS.find("def handle_shadow_log")
    block = _ACTIONS[idx:idx + 2500]
    assert "user setting" in block
    assert "LIVE_SHADOW_MODE" in block
