"""Round-61 pt.96 — live-mode soft-launch wizard.

Paper validation runs from 2026-04-15 → ~2026-05-15. Pt.93 surfaced
the auto-promotion gate (closed trades, win rate, sharpe, drawdown,
audit findings) as a per-gate ✓/✗ readout. Pt.92 surfaced the
shadow-mode log so users can see what live *would* have done.
Both panels exist in Settings → Live Trading; they're useful but
nothing tells the user what to DO with them.

Pt.96 wraps the existing pieces into a four-phase launch flow:

    1. preflight    — keys saved + email/ntfy set + funded account
    2. ready        — promotion gate (pt.72 / pt.93) green
    3. shadow       — shadow mode running for ≥48h with non-trivial activity
    4. verify       — user explicitly reviewed the shadow log
    5. live         — toggle flipped

This module is pure: callers pass already-loaded data, we return
``{phase, phase_label, steps, next_action, recommended_shadow_hours}``.
The actual flip-to-live still goes through ``handle_toggle_live_mode``.

State is persisted in ``users/<id>/guardrails.json`` under
``live_launch`` so it survives restarts and isn't lost when the
user closes the modal:

    guardrails["live_launch"] = {
        "shadow_started_at": "2026-04-26T03:14:00Z",
        "shadow_reviewed_at": "2026-04-28T19:02:00Z",
    }

Conservative defaults: 48h shadow window, 5 events minimum so the
log isn't empty, then the verify check arms.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Optional


# ---------- Constants ----------

PHASE_PREFLIGHT = "preflight"
PHASE_READY     = "ready"
PHASE_SHADOW    = "shadow"
PHASE_VERIFY    = "verify"
PHASE_LIVE      = "live"

PHASE_ORDER = (PHASE_PREFLIGHT, PHASE_READY, PHASE_SHADOW,
                PHASE_VERIFY, PHASE_LIVE)

PHASE_LABELS = {
    PHASE_PREFLIGHT: "1 of 4 — Preflight checks",
    PHASE_READY:     "2 of 4 — Promotion gate clears",
    PHASE_SHADOW:    "3 of 4 — Shadow window running",
    PHASE_VERIFY:    "4 of 4 — Final review",
    PHASE_LIVE:      "Live trading enabled",
}

# Default 48h shadow window. Can be overridden in tests / future UI.
DEFAULT_SHADOW_HOURS: float = 48.0
# Minimum non-trivial shadow activity before the window counts as
# "exercised" — without this an idle weekend counts as a successful
# 48h shadow run with zero data.
DEFAULT_MIN_SHADOW_EVENTS: int = 5


# ---------- Helpers ----------

def _now_utc():
    """Indirection for testability."""
    return datetime.now(timezone.utc)


def _parse_iso(ts):
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # Accept both ``...Z`` and ``...+00:00``.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_since(ts) -> float:
    parsed = _parse_iso(ts)
    if parsed is None:
        return 0.0
    delta = _now_utc() - parsed
    return max(0.0, delta.total_seconds() / 3600.0)


def _step(key, label, done, *, hint=None, action=None):
    out = {"key": key, "label": label, "done": bool(done)}
    if hint:
        out["hint"] = hint
    if action:
        out["action"] = action
    return out


# ---------- Mutators (called by the action endpoint) ----------

def start_shadow_window(guardrails):
    """Stamp the start of the shadow window. Idempotent — calling
    twice doesn't reset the clock once set."""
    if not isinstance(guardrails, dict):
        guardrails = {}
    ll = dict(guardrails.get("live_launch") or {})
    if not ll.get("shadow_started_at"):
        ll["shadow_started_at"] = _now_utc().isoformat()
    # Resetting the wizard clears the reviewed flag too — covered
    # by reset_launch().
    guardrails["live_launch"] = ll
    return guardrails


def mark_shadow_reviewed(guardrails):
    """User has clicked ‘I've reviewed the shadow log’. Stamps now."""
    if not isinstance(guardrails, dict):
        guardrails = {}
    ll = dict(guardrails.get("live_launch") or {})
    ll["shadow_reviewed_at"] = _now_utc().isoformat()
    guardrails["live_launch"] = ll
    return guardrails


def reset_launch(guardrails):
    """Clears the wizard state — useful if the user wants to redo
    the shadow window from scratch (or aborts mid-flow)."""
    if not isinstance(guardrails, dict):
        guardrails = {}
    guardrails["live_launch"] = {}
    return guardrails


# ---------- Reader ----------

def compute_launch_state(
        user: Optional[Mapping],
        gate_result: Optional[Mapping],
        guardrails: Optional[Mapping],
        shadow_summary: Optional[Mapping] = None,
        *,
        shadow_hours: float = DEFAULT_SHADOW_HOURS,
        min_shadow_events: int = DEFAULT_MIN_SHADOW_EVENTS,
        ) -> dict:
    """Resolve which phase the user is in and what the next step is.

    Args:
      user: dict-like user row. We read keys saved + notification_email
        + ntfy_topic + live_mode.
      gate_result: output of ``live_mode_gate.check_live_mode_readiness``.
      guardrails: per-user guardrails dict (carries ``live_launch``
        state + ``live_shadow_mode`` toggle).
      shadow_summary: output of ``shadow_mode.summarize_shadow_log``
        (for the event-count threshold).
      shadow_hours: required shadow-window length in hours.
      min_shadow_events: required non-trivial activity threshold.

    Returns:
      {
        phase: str,
        phase_label: str,
        steps: [ {key, label, done, hint?, action?}, ... ],
        next_action: { kind, label } | None,
        recommended_shadow_hours: float,
        shadow_started_at: str | None,
        shadow_hours_elapsed: float,
        shadow_reviewed_at: str | None,
      }
    """
    user = user or {}
    gate_result = gate_result or {}
    guardrails = guardrails or {}
    ll_state = guardrails.get("live_launch") or {}
    shadow_summary = shadow_summary or {}

    # --- Preflight checks (phase 1) ---
    keys_saved = bool(user.get("alpaca_live_key_encrypted")
                       and user.get("alpaca_live_secret_encrypted"))
    has_email = bool(user.get("notification_email"))
    has_ntfy = bool(user.get("ntfy_topic"))

    # --- Gate (phase 2) ---
    gate_ready = bool(gate_result.get("ready"))
    gate_summary = gate_result.get("summary") or ""

    # --- Shadow window (phase 3) ---
    shadow_active = bool(guardrails.get("live_shadow_mode"))
    shadow_started_at = ll_state.get("shadow_started_at")
    elapsed = _hours_since(shadow_started_at)
    shadow_event_count = int(shadow_summary.get("total") or 0)
    shadow_window_complete = (
        bool(shadow_started_at)
        and elapsed >= shadow_hours
        and shadow_event_count >= min_shadow_events
    )

    # --- Verify (phase 4) ---
    shadow_reviewed_at = ll_state.get("shadow_reviewed_at")
    reviewed = bool(shadow_reviewed_at)

    # --- Live (phase 5) ---
    is_live = bool(user.get("live_mode")) or bool(user.get("live_mode_enabled"))

    # Determine current phase — first un-done phase wins.
    if is_live:
        phase = PHASE_LIVE
    elif not (keys_saved and has_email and has_ntfy):
        phase = PHASE_PREFLIGHT
    elif not gate_ready:
        phase = PHASE_READY
    elif not shadow_window_complete:
        phase = PHASE_SHADOW
    elif not reviewed:
        phase = PHASE_VERIFY
    else:
        # All steps done → user can flip live.
        phase = PHASE_VERIFY

    # Build the step list — every step always present so the UI can
    # render the full checklist.
    steps = []
    steps.append(_step(
        "keys_saved", "Live API keys saved", keys_saved,
        hint=None if keys_saved
        else "Save live keys on the Alpaca tab"))
    steps.append(_step(
        "notification_email", "Notification email set", has_email,
        hint=None if has_email
        else "Add a notification email on the Profile tab"))
    steps.append(_step(
        "ntfy_topic", "ntfy topic set (push notifications)", has_ntfy,
        hint=None if has_ntfy
        else "Add an ntfy topic on the Notifications tab"))
    steps.append(_step(
        "promotion_gate", "Promotion gate clears", gate_ready,
        hint=gate_summary if not gate_ready else None))
    steps.append(_step(
        "shadow_window", f"Shadow window ≥{int(shadow_hours)}h",
        shadow_window_complete,
        hint=(_shadow_hint(shadow_active, shadow_started_at, elapsed,
                            shadow_event_count, shadow_hours,
                            min_shadow_events)
              if not shadow_window_complete else None),
        action="start_shadow" if not shadow_started_at else None,
    ))
    steps.append(_step(
        "shadow_review", "Shadow log reviewed", reviewed,
        hint=None if reviewed
        else "Click the panel below + review what live would have done",
        action="mark_reviewed" if shadow_window_complete and not reviewed
        else None,
    ))

    # next_action — what the user should click NEXT.
    next_action = _next_action(phase, keys_saved, has_email, has_ntfy,
                                gate_ready, shadow_active,
                                shadow_started_at, shadow_window_complete,
                                reviewed, is_live)

    return {
        "phase": phase,
        "phase_label": PHASE_LABELS[phase],
        "steps": steps,
        "next_action": next_action,
        "recommended_shadow_hours": shadow_hours,
        "shadow_started_at": shadow_started_at,
        "shadow_hours_elapsed": round(elapsed, 2),
        "shadow_event_count": shadow_event_count,
        "shadow_reviewed_at": shadow_reviewed_at,
    }


def _shadow_hint(active, started_at, elapsed, count, hours, min_events):
    if not started_at:
        return ("Click ‘Start shadow window’ — runs the deployer "
                 "through every gate but skips the order POST")
    remaining = max(0.0, hours - elapsed)
    if remaining > 0:
        return (f"{elapsed:.1f}h elapsed of {int(hours)}h "
                 f"(~{remaining:.0f}h remaining)")
    if count < min_events:
        return (f"{int(hours)}h elapsed but only {count} shadow "
                 f"events — need ≥{min_events} before final review")
    return None


def _next_action(phase, keys, email, ntfy, gate, shadow_on,
                  shadow_started, shadow_done, reviewed, is_live):
    if is_live:
        return {"kind": "live", "label": "Already live"}
    if not (keys and email and ntfy):
        if not keys:
            return {"kind": "settings_alpaca",
                     "label": "Save live API keys"}
        if not email:
            return {"kind": "settings_profile",
                     "label": "Add notification email"}
        return {"kind": "settings_notify",
                 "label": "Add ntfy topic"}
    if not gate:
        return {"kind": "wait_gate",
                 "label": "Wait for promotion gate to clear"}
    if not shadow_started:
        return {"kind": "start_shadow",
                 "label": "Start 48h shadow window"}
    if not shadow_done:
        return {"kind": "wait_shadow",
                 "label": "Shadow window in progress"}
    if not reviewed:
        return {"kind": "mark_reviewed",
                 "label": "Review the shadow log + mark done"}
    return {"kind": "enable_live",
             "label": "Enable Live Trading"}
