"""Round-61 pt.69 — retry-with-backoff after cancelling pending
orders, before retrying DELETE /positions.

User-reported bug: closing a SOXL short returned 422 "insufficient
qty available for order (requested: 29, available: 0)" even though
pt.53's `_cancel_pending_sell_orders` had successfully cancelled
the pending BUY-stop cover order.

Root cause: Alpaca's order cancellation is asynchronous. The broker
takes ~250-1000ms to actually release the reserved qty / buying-
power after returning the cancel ACK. Pt.53 retried DELETE
immediately and hit the same error.

Fix: poll DELETE up to 4 times with exponential backoff (0.3s,
0.6s, 1.0s, 1.5s) so the retry catches the release window.
"""
from __future__ import annotations

import pathlib

_HERE = pathlib.Path(__file__).resolve().parent.parent


# ============================================================================
# Source-pin: the new retry-with-backoff loop is wired into
# handle_close_position.
# ============================================================================

def test_close_path_has_backoff_retry():
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    # Loop with multiple backoff delays.
    assert "for _attempt, _delay in enumerate(" in body
    assert "time.sleep(_delay)" in body


def test_close_path_retries_only_on_same_error():
    """Different errors should surface immediately rather than burn
    through the retry budget."""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    # Inside the loop the SAME-error branch continues; other errors
    # surface via send_json.
    loop_start = body.find("for _attempt, _delay in enumerate(")
    loop_block = body[loop_start:loop_start + 2000]
    assert "continue" in loop_block
    assert "Different error" in loop_block or "different error" in loop_block.lower()


def test_close_path_has_max_retries_message():
    """When all retries exhaust with the same error, surface a clear
    hint rather than silently failing."""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    assert "still" in body.lower()
    # The exhaustion-path message references the time scale.
    assert "try again in a moment" in body or "try again" in body.lower()


def test_close_path_uses_4_retry_attempts():
    """Verify the backoff schedule matches what pt.69 specified."""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    # Schedule: 0.3, 0.6, 1.0, 1.5 → 4 attempts, ~3.4s total.
    assert "(0.3, 0.6, 1.0, 1.5)" in body


def test_close_path_records_settled_funds_after_retry_success():
    """Pt.67's settled-funds bridge must still fire after the retry
    succeeds (not just the immediate retry path)."""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    # The settled-funds call must appear AFTER the retry loop.
    loop_idx = body.find("for _attempt, _delay in enumerate(")
    settled_idx = body.find("_record_close_to_settled_funds(",
                              loop_idx)
    assert settled_idx > loop_idx > 0


def test_close_path_handles_zero_cancellations_unchanged():
    """When cancel scan finds no orders, behavior should be the same
    as before — surface the 'check Open Orders' hint. (No regression.)"""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    assert "check Open Orders" in body
    assert "reserving the shares" in body


def test_close_message_says_pending_orders_not_just_sell():
    """Pt.53's success message originally said 'sell order(s)' but
    pt.69 also covers BUY-to-cover cancellations. The success
    message should be neutral."""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    # The retry-success message uses neutral 'pending order(s)'.
    assert "pending order(s)" in body
