"""
Round-61 pt.8 audit: concurrency fixes for Friday risk-reduction + monthly
rebalance.

Both sites had RMW patterns on strategy files (load_json → mutate → save_json)
without holding strategy_file_lock. A concurrent 60s monitor tick could
interleave a read between our load and save and then clobber the update.

The fix wraps both RMW sequences in `with strategy_file_lock(sf_path):`.
These pins guard against a future refactor that removes either lock —
a regression there would silently corrupt friday_trims history /
total_shares_held counters / stop_order_id state.
"""
from __future__ import annotations


def test_friday_trim_holds_strategy_file_lock():
    """Round-61 pt.8 pin: run_friday_risk_reduction wraps its strategy-
    file RMW in strategy_file_lock(sf_path) so the 60s monitor can't
    interleave."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    # Locate the Friday trim block by an anchor unique to it.
    anchor = "Friday strategy-file update failed"
    idx = src.find(anchor)
    assert idx > 0, f"anchor '{anchor}' not found — function renamed?"
    # Walk back a bit to find the surrounding RMW window (the load_json
    # call before this exception handler).
    window = src[max(0, idx - 2500):idx]
    assert "strategy_file_lock(sf_path)" in window, (
        "Friday trim RMW must hold strategy_file_lock(sf_path) — without "
        "it the 60s monitor can read between load and save and silently "
        "clobber friday_trims history + total_shares_held.")
    # The save_json call must be inside the lock block (not after it).
    assert "with strategy_file_lock(sf_path):" in window, (
        "strategy_file_lock must be used as a `with` context manager so "
        "the save_json runs inside the lock-held region.")


def test_monthly_rebalance_holds_strategy_file_lock():
    """Round-61 pt.8 pin: run_monthly_rebalance wraps its stop-cancel
    RMW in strategy_file_lock(sf_path)."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    anchor = "rebalance cancel-stop failed"
    idx = src.find(anchor)
    assert idx > 0, f"anchor '{anchor}' not found — function renamed?"
    window = src[max(0, idx - 1500):idx]
    assert "strategy_file_lock(sf_path)" in window, (
        "Monthly rebalance stop-cancel RMW must hold "
        "strategy_file_lock(sf_path) — without it, a gap between load "
        "and save can cause a user-triggered pause/resize to be "
        "clobbered, or the monitor to re-cancel an already-cancelled "
        "stop on its next tick.")
    assert "with strategy_file_lock(sf_path):" in window


def test_signup_invite_code_has_label_association():
    """Round-61 pt.8 UX pin: the invite_code input must have a
    `<label for="invite_code">` for keyboard + screen-reader access."""
    with open("templates/signup.html") as f:
        src = f.read()
    assert '<label for="invite_code">' in src, (
        "signup invite_code field must have a label with for=invite_code "
        "— without it, clicking the label doesn't focus the field and "
        "screen readers don't announce the association.")


def test_reset_password_has_label_association():
    """Round-61 pt.8 UX pin: the reset-password New Password input must
    have a `<label for="password">`."""
    with open("templates/reset.html") as f:
        src = f.read()
    assert '<label for="password">' in src, (
        "reset-password field must have a label with for=password — "
        "same accessibility fix as signup invite_code.")
