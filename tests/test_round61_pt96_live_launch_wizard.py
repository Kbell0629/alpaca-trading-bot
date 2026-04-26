"""Round-61 pt.96 — live-mode soft-launch wizard.

Pt.92 surfaced the shadow-mode log; pt.93 surfaced the auto-
promotion gate. Pt.96 wraps both into a four-phase soft-launch
flow: preflight → promotion gate → 48h shadow window → final
review → live. The wizard is read-only on the dashboard except
for two action buttons (start shadow window / mark reviewed)
that mutate ``guardrails.live_launch``.

Test coverage:

* Pure module ``live_launch`` — phase transitions, step list
  shape, hint text, mutators (start / review / reset), idempotence,
  shadow-window timing math.
* Server dispatch — both endpoints landed in server.py as one-line
  delegations to actions_mixin.
* Mixin handlers — auth gate, response shape, action validation.
* UI markup — wizard panel + JS handler + lazy-load wiring.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
from datetime import datetime, timedelta, timezone


_HERE = pathlib.Path(__file__).resolve().parent.parent
_DASH = (_HERE / "templates" / "dashboard.html").read_text()
_SERVER = (_HERE / "server.py").read_text()
_ACTIONS = (_HERE / "handlers" / "actions_mixin.py").read_text()


def _import_live_launch():
    sys.path.insert(0, str(_HERE))
    if "live_launch" in sys.modules:
        return importlib.reload(sys.modules["live_launch"])
    import live_launch  # noqa: E402
    return live_launch


# ============================================================================
# Pure module — phase transitions
# ============================================================================

def test_compute_launch_state_starts_at_preflight_when_keys_missing():
    ll = _import_live_launch()
    state = ll.compute_launch_state(
        user={},
        gate_result={"ready": False},
        guardrails={},
        shadow_summary={"total": 0},
    )
    assert state["phase"] == "preflight"
    keys_step = next(s for s in state["steps"] if s["key"] == "keys_saved")
    assert keys_step["done"] is False
    assert state["next_action"]["kind"] == "settings_alpaca"


def test_compute_launch_state_advances_to_ready_when_preflight_clean():
    ll = _import_live_launch()
    state = ll.compute_launch_state(
        user={
            "alpaca_live_key_encrypted": "x",
            "alpaca_live_secret_encrypted": "y",
            "notification_email": "u@x.com",
            "ntfy_topic": "topic",
        },
        gate_result={"ready": False, "summary": "Need 30 closed trades"},
        guardrails={},
        shadow_summary={"total": 0},
    )
    assert state["phase"] == "ready"
    gate_step = next(s for s in state["steps"]
                     if s["key"] == "promotion_gate")
    assert gate_step["done"] is False
    assert "30 closed trades" in (gate_step.get("hint") or "")
    assert state["next_action"]["kind"] == "wait_gate"


def test_compute_launch_state_advances_to_shadow_when_gate_clears():
    ll = _import_live_launch()
    state = ll.compute_launch_state(
        user={
            "alpaca_live_key_encrypted": "x",
            "alpaca_live_secret_encrypted": "y",
            "notification_email": "u@x.com",
            "ntfy_topic": "topic",
        },
        gate_result={"ready": True, "summary": "READY"},
        guardrails={},
        shadow_summary={"total": 0},
    )
    assert state["phase"] == "shadow"
    sw = next(s for s in state["steps"] if s["key"] == "shadow_window")
    assert sw["done"] is False
    assert sw.get("action") == "start_shadow"
    assert state["next_action"]["kind"] == "start_shadow"


def test_compute_launch_state_shadow_window_in_progress():
    ll = _import_live_launch()
    started = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    state = ll.compute_launch_state(
        user={
            "alpaca_live_key_encrypted": "x",
            "alpaca_live_secret_encrypted": "y",
            "notification_email": "u@x.com",
            "ntfy_topic": "topic",
        },
        gate_result={"ready": True},
        guardrails={"live_launch": {"shadow_started_at": started},
                    "live_shadow_mode": True},
        shadow_summary={"total": 7},
    )
    assert state["phase"] == "shadow"
    sw = next(s for s in state["steps"] if s["key"] == "shadow_window")
    assert sw["done"] is False
    assert "remaining" in (sw.get("hint") or "")
    assert state["next_action"]["kind"] == "wait_shadow"


def test_compute_launch_state_advances_to_verify_when_window_done():
    ll = _import_live_launch()
    started = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    state = ll.compute_launch_state(
        user={
            "alpaca_live_key_encrypted": "x",
            "alpaca_live_secret_encrypted": "y",
            "notification_email": "u@x.com",
            "ntfy_topic": "topic",
        },
        gate_result={"ready": True},
        guardrails={"live_launch": {"shadow_started_at": started}},
        shadow_summary={"total": 12},
    )
    assert state["phase"] == "verify"
    sw = next(s for s in state["steps"] if s["key"] == "shadow_window")
    assert sw["done"] is True
    review = next(s for s in state["steps"] if s["key"] == "shadow_review")
    assert review.get("action") == "mark_reviewed"
    assert state["next_action"]["kind"] == "mark_reviewed"


def test_compute_launch_state_blocks_on_low_event_count():
    """48h elapsed but only 1 shadow event isn't enough — wait."""
    ll = _import_live_launch()
    started = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    state = ll.compute_launch_state(
        user={
            "alpaca_live_key_encrypted": "x",
            "alpaca_live_secret_encrypted": "y",
            "notification_email": "u@x.com",
            "ntfy_topic": "topic",
        },
        gate_result={"ready": True},
        guardrails={"live_launch": {"shadow_started_at": started}},
        shadow_summary={"total": 1},
    )
    assert state["phase"] == "shadow"
    sw = next(s for s in state["steps"] if s["key"] == "shadow_window")
    assert sw["done"] is False
    assert "events" in (sw.get("hint") or "").lower()


def test_compute_launch_state_ready_for_live_when_reviewed():
    ll = _import_live_launch()
    started = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    reviewed = datetime.now(timezone.utc).isoformat()
    state = ll.compute_launch_state(
        user={
            "alpaca_live_key_encrypted": "x",
            "alpaca_live_secret_encrypted": "y",
            "notification_email": "u@x.com",
            "ntfy_topic": "topic",
        },
        gate_result={"ready": True},
        guardrails={"live_launch": {
            "shadow_started_at": started,
            "shadow_reviewed_at": reviewed,
        }},
        shadow_summary={"total": 12},
    )
    assert state["next_action"]["kind"] == "enable_live"


def test_compute_launch_state_reports_live_when_enabled():
    ll = _import_live_launch()
    state = ll.compute_launch_state(
        user={"live_mode": True,
              "alpaca_live_key_encrypted": "x",
              "alpaca_live_secret_encrypted": "y",
              "notification_email": "u@x.com",
              "ntfy_topic": "topic"},
        gate_result={"ready": True},
        guardrails={},
        shadow_summary={"total": 5},
    )
    assert state["phase"] == "live"
    assert state["next_action"]["kind"] == "live"


# ============================================================================
# Pure module — mutators
# ============================================================================

def test_start_shadow_window_is_idempotent():
    ll = _import_live_launch()
    g = {}
    g = ll.start_shadow_window(g)
    first = g["live_launch"]["shadow_started_at"]
    g = ll.start_shadow_window(g)
    second = g["live_launch"]["shadow_started_at"]
    assert first == second  # second call must NOT reset the timer


def test_mark_shadow_reviewed_stamps_now():
    ll = _import_live_launch()
    g = {}
    g = ll.mark_shadow_reviewed(g)
    ts = g["live_launch"]["shadow_reviewed_at"]
    assert isinstance(ts, str) and "T" in ts


def test_reset_launch_clears_state():
    ll = _import_live_launch()
    g = {"live_launch": {"shadow_started_at": "x",
                           "shadow_reviewed_at": "y"}}
    g = ll.reset_launch(g)
    assert g["live_launch"] == {}


# ============================================================================
# Server dispatch
# ============================================================================

def test_server_dispatches_live_launch_state():
    idx = _SERVER.find('"/api/live-launch-state"')
    assert idx > 0
    block = _SERVER[idx:idx + 200]
    assert "handle_live_launch_state" in block
    # Body must NOT be inline — no live_launch import in the dispatch
    # block (a sibling dispatch line for handle_live_launch_step right
    # after is fine).
    assert "import live_launch" not in block
    assert "compute_launch_state" not in block


def test_server_dispatches_live_launch_step():
    idx = _SERVER.find('"/api/live-launch-step"')
    assert idx > 0
    block = _SERVER[idx:idx + 200]
    assert "handle_live_launch_step" in block


# ============================================================================
# Mixin handlers
# ============================================================================

def test_handle_live_launch_state_uses_gate_and_shadow_modules():
    idx = _ACTIONS.find("def handle_live_launch_state")
    assert idx > 0
    block = _ACTIONS[idx:idx + 2500]
    assert "live_launch" in block
    assert "live_mode_gate" in block
    assert "shadow_mode" in block
    assert "compute_launch_state" in block


def test_handle_live_launch_state_requires_auth():
    idx = _ACTIONS.find("def handle_live_launch_state")
    block = _ACTIONS[idx:idx + 2500]
    assert "self.current_user" in block
    assert "401" in block


def test_handle_live_launch_step_validates_action():
    idx = _ACTIONS.find("def handle_live_launch_step")
    assert idx > 0
    block = _ACTIONS[idx:idx + 2500]
    for action in ("start_shadow", "mark_reviewed", "reset"):
        assert action in block
    assert "Unknown action" in block


def test_handle_live_launch_step_persists_to_guardrails():
    idx = _ACTIONS.find("def handle_live_launch_step")
    block = _ACTIONS[idx:idx + 2500]
    assert "guardrails.json" in block
    assert "save_json" in block


def test_handle_live_launch_step_flips_shadow_mode_on_start():
    idx = _ACTIONS.find("def handle_live_launch_step")
    block = _ACTIONS[idx:idx + 2500]
    assert 'gr["live_shadow_mode"] = True' in block


def test_handle_live_launch_step_clears_shadow_mode_on_reset():
    idx = _ACTIONS.find("def handle_live_launch_step")
    block = _ACTIONS[idx:idx + 2500]
    assert 'gr["live_shadow_mode"] = False' in block


# ============================================================================
# Dashboard markup + JS
# ============================================================================

def test_launch_panel_in_live_trading_panel():
    idx = _DASH.find('id="settingsPanel-live"')
    assert idx > 0
    end = _DASH.find('<!-- Sharing tab -->', idx)
    panel = _DASH[idx:end]
    assert 'id="liveLaunchPanel"' in panel
    assert 'id="liveLaunchContent"' in panel
    assert "Live launch checklist" in panel


def test_refresh_live_launch_state_helper_defined():
    assert "async function refreshLiveLaunchState" in _DASH
    assert "/api/live-launch-state" in _DASH


def test_live_launch_step_helper_defined():
    assert "async function liveLaunchStep" in _DASH
    assert "/api/live-launch-step" in _DASH


def test_live_launch_step_handles_three_actions():
    idx = _DASH.find("async function liveLaunchStep")
    fn_block = _DASH[idx:idx + 2500]
    for action in ("start_shadow", "mark_reviewed", "reset"):
        assert action in fn_block


def test_settings_tab_live_triggers_launch_refresh():
    idx = _DASH.find("function switchSettingsTab")
    fn_block = _DASH[idx:idx + 1500]
    assert "name === 'live'" in fn_block
    assert "refreshLiveLaunchState" in fn_block


def test_launch_panel_renders_phase_pill():
    idx = _DASH.find("async function refreshLiveLaunchState")
    fn_block = _DASH[idx:idx + 5000]
    # Phase label is rendered in upper-case via the pill.
    assert "phase_label" in fn_block
    assert "phase" in fn_block
