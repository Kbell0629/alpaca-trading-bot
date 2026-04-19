"""
state_recovery.py — boot-time consistency validator.

Round-16 follow-up to the 5-storage-layer audit finding. This doesn't
auto-fix anything — it READS state from the multiple persistence layers
(wheel JSONs, trade journal, scorecard, Alpaca positions) and surfaces
discrepancies via observability.capture_message so the operator sees
drift before it becomes a real-money problem.

What gets checked per user:

  * Each wheel state file: does shares_owned match what Alpaca says?
    A mismatch can happen if (a) a stock split landed during a Railway
    redeploy, (b) the user manually closed a position, (c) Alpaca's
    position endpoint lags. Round-13 added split auto-resolve; this
    validator catches the (b) + (c) cases that the wheel state machine
    can't auto-resolve.

  * Trade journal vs Alpaca positions: any open trade in the journal
    where Alpaca shows zero position? Could be a closed-but-not-recorded
    trade. Logged as a warning so the operator can reconcile.

This runs at scheduler startup AFTER Sentry is initialised, so any
warning lands in your Sentry feed. Designed to be idempotent + cheap
(<5s for typical user counts).

USAGE: called from cloud_scheduler.start_scheduler() after observability
is wired up. Failure of the validator never blocks the scheduler — it's
purely diagnostic.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def reconcile_wheel_vs_positions(wheel_states, positions):
    """Pure, testable. Returns a list of {symbol, expected_shares,
    actual_shares, severity, hint} dicts — one per discrepancy found.

    `wheel_states` is {symbol: state_dict}.
    `positions` is the list of Alpaca position dicts (qty key).
    """
    discrepancies = []
    pos_by_sym = {
        (p.get("symbol") or "").upper(): _safe_int(p.get("qty"))
        for p in (positions or [])
    }
    for symbol, state in (wheel_states or {}).items():
        expected = _safe_int(state.get("shares_owned", 0))
        actual = pos_by_sym.get((symbol or "").upper(), 0)
        if expected == actual:
            continue
        # We only care if our state thinks we own shares but Alpaca
        # disagrees by more than a small tolerance. The opposite case
        # (Alpaca says shares, wheel says 0) is normal during stage_1
        # before a put is assigned.
        if expected == 0:
            continue
        delta = actual - expected
        # The round-13 split auto-resolve already handles the case
        # where actual is ~2× expected. Skip those — they'll resolve
        # at the next monitor tick.
        if abs(delta) < expected * 0.1 and abs(delta) < 5:
            severity = "info"  # likely lot dust, don't bother
        elif actual == 0:
            severity = "warning"  # we think we own but Alpaca shows none
        elif actual < expected:
            severity = "warning"  # manual sale or partial liquidation
        else:
            severity = "info"  # extra shares — split or DRIP, auto-resolves
        discrepancies.append({
            "symbol": symbol,
            "expected_shares": expected,
            "actual_shares": actual,
            "delta": delta,
            "severity": severity,
            "hint": _hint_for_delta(expected, actual, delta),
        })
    return discrepancies


def _hint_for_delta(expected, actual, delta):
    if actual == 0:
        return ("wheel state says we own shares but Alpaca shows none — "
                "did you close manually? Reconcile state.shares_owned + "
                "state.cost_basis by hand or clear the wheel state file.")
    if actual == expected * 2:
        return ("looks like a 2:1 stock split — round-13 auto-resolver "
                "should pick this up at the next monitor tick.")
    if actual < expected:
        return ("Alpaca reports fewer shares than wheel state expects — "
                "manual sale, margin call, or partial assignment. Wheel "
                "may freeze on next monitor tick (round-12 anomaly guard).")
    return ("Alpaca reports MORE shares than wheel state expects — "
            "stock split, DRIP, or transfer-in. Round-13 auto-resolver "
            "handles 2:1+ splits; smaller deltas may indicate a manual "
            "buy.")


def reconcile_journal_vs_positions(open_trades, positions):
    """Returns list of orphaned-trade discrepancies. An OPEN trade in
    the journal with NO matching Alpaca position is suspicious — could
    be a closed-but-not-recorded trade or a manual close.

    `open_trades`: list of trade dicts from journal (status='open').
    `positions`: Alpaca position dicts."""
    discrepancies = []
    pos_syms = {(p.get("symbol") or "").upper()
                for p in (positions or [])
                if _safe_int(p.get("qty")) > 0}
    for trade in (open_trades or []):
        if trade.get("status") != "open":
            continue
        sym = (trade.get("symbol") or "").upper()
        if sym and sym not in pos_syms:
            discrepancies.append({
                "symbol": sym,
                "trade_id": trade.get("id") or trade.get("entry_order_id"),
                "severity": "warning",
                "hint": ("trade journal says position is OPEN but Alpaca "
                         "shows zero shares — manual close, stop-out, or "
                         "lost-fill. Reconcile by setting status='closed' "
                         "+ filling exit_date / exit_price."),
            })
    return discrepancies


def reconcile_user(user, wheel_states_path, journal_path,
                    fetch_positions):
    """End-to-end check for one user. Returns dict with both lists.

    `fetch_positions` is a callable() -> list of position dicts,
    injected so tests don't hit Alpaca."""
    wheel_states = {}
    if os.path.isdir(wheel_states_path):
        for fname in os.listdir(wheel_states_path):
            if not fname.startswith("wheel_") or not fname.endswith(".json"):
                continue
            symbol = fname[len("wheel_"):-len(".json")]
            state = _load_json(os.path.join(wheel_states_path, fname))
            if state:
                wheel_states[symbol] = state

    journal = _load_json(journal_path) or {}
    open_trades = journal.get("open_trades") or []

    try:
        positions = fetch_positions()
        if not isinstance(positions, list):
            positions = []
    except Exception as e:
        log.warning("reconcile: fetch_positions failed",
                    extra={"error": type(e).__name__})
        return {"wheel_discrepancies": [], "journal_discrepancies": [],
                "fetch_failed": True}

    return {
        "wheel_discrepancies": reconcile_wheel_vs_positions(
            wheel_states, positions),
        "journal_discrepancies": reconcile_journal_vs_positions(
            open_trades, positions),
        "fetch_failed": False,
    }


def report_to_observability(user, result):
    """Emit a capture_message for each discrepancy so they show up in
    Sentry. Severity 'warning' surfaces as a yellow event; 'info' as
    blue. Never raises; falls back to the structured logger."""
    try:
        from observability import capture_message
    except Exception:
        capture_message = None

    uname = (user or {}).get("username", "unknown")
    for d in result.get("wheel_discrepancies", []):
        msg = (f"[reconcile] {uname}/{d['symbol']}: wheel expects "
               f"{d['expected_shares']}, Alpaca shows "
               f"{d['actual_shares']} (delta {d['delta']:+d}). "
               f"{d['hint']}")
        if capture_message:
            try:
                capture_message(msg, level=d["severity"],
                                component="state_recovery",
                                user=uname, symbol=d["symbol"])
            except Exception:
                pass
        log.warning(msg)
    for d in result.get("journal_discrepancies", []):
        msg = (f"[reconcile] {uname}/{d['symbol']}: open trade "
               f"{d.get('trade_id', '?')} but Alpaca shows zero shares. "
               f"{d['hint']}")
        if capture_message:
            try:
                capture_message(msg, level=d["severity"],
                                component="state_recovery",
                                user=uname, symbol=d["symbol"])
            except Exception:
                pass
        log.warning(msg)
