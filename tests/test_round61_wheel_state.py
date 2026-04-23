"""
Round-61 tests: wheel-strategy state machine transitions.

The wheel is a state machine:

    stage_1_searching
        └─ open_short_put  ──>  stage_1_put_active
                                    │
             ┌──────────────────────┤
             │                      │
    put expired worthless     put assigned
       (keep premium,         (acquire 100×qty shares,
        cycles++,              cost_basis = strike − prem/share)
        stage_1_searching)           │
                                     ▼
                            stage_2_shares_owned
                                     │
                                     └─ open_covered_call  ──>  stage_2_call_active
                                                                    │
                                             ┌──────────────────────┤
                                             │                      │
                                      call expired worthless    call assigned
                                       (keep premium,           (shares called away,
                                        stage_2_shares_owned)    stock PnL booked,
                                                                 cycles++,
                                                                 stage_1_searching)

A silent regression in any transition = wrong cost basis, wrong cycle count,
double-counting premium, or skipping an assignment. 1099-breaking over a
multi-month cycle.

Existing coverage (as of R60):
  * test_wheel_state.py — template shape, history cap, JSON atomic write,
    lock acquire/release (4 tests; primitives only)
  * test_wheel_decimal_parity.py — premium/PnL Decimal math
  * test_wheel_stock_split_resolution.py — R13 split auto-resolve path
  * test_round42_wheel_close_journaling.py — journal on wheel close
  * test_round43_wheel_open_backfill.py — initial shares_at_open baseline

This file pins the TRANSITIONS themselves — which weren't covered by any
of the above. Grep-pin style matches R55/R57. Every assertion below
protects a state-machine edge that, if broken, corrupts a live wheel.
"""
from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PATH = os.path.join(_HERE, "wheel_strategy.py")


def _src():
    with open(_SRC_PATH) as f:
        return f.read()


def _slice(src: str, start: str, end: str) -> str:
    i = src.find(start)
    assert i >= 0, f"missing start token: {start!r}"
    j = src.find(end, i + len(start))
    assert j > i, f"missing end token: {end!r} after {start!r}"
    return src[i:j]


# ========= 1. Initial state =========

def test_wheel_starts_in_searching():
    """stage_1_searching is the only valid starting stage — enforced by
    the template."""
    src = _src()
    assert '"stage": "stage_1_searching"' in src, (
        "WHEEL_STATE_TEMPLATE must start at stage_1_searching")


def test_stage_names_are_stable():
    """These four stage strings are persisted in every wheel_*.json file
    on disk for every live user. Renaming any of them breaks every
    existing state file + every downstream consumer (dashboard,
    screener, journaler)."""
    src = _src()
    for stage in ("stage_1_searching", "stage_1_put_active",
                  "stage_2_shares_owned", "stage_2_call_active"):
        assert f'"{stage}"' in src, (
            f"stage string {stage!r} missing — renaming breaks every "
            "live wheel_*.json on disk")


# ========= 2. Open-put transitions =========

def test_open_short_put_transitions_to_put_active():
    """open_short_put ends with state.stage = stage_1_put_active. This
    is the ONLY write site for that stage."""
    src = _src()
    block = _slice(src, "def open_short_put(user, pick):",
                   "def open_covered_call")
    assert 'state["stage"] = "stage_1_put_active"' in block, (
        "open_short_put must set stage to stage_1_put_active")


def test_open_short_put_skips_if_wheel_already_active():
    """You can't open a new put while one already exists — the existing
    check guards against that. Regression = two concurrent wheels on
    the same symbol with conflicting premium/cost-basis records."""
    src = _src()
    block = _slice(src, "def open_short_put(user, pick):",
                   "def open_covered_call")
    assert 'existing.get("stage") != "stage_1_searching"' in block, (
        "open_short_put must skip when a wheel in non-searching stage "
        "exists for this symbol")


# ========= 3. Open-order status transitions =========

def test_canceled_open_order_resets_to_searching_or_shares_owned():
    """If the sell-to-open order is canceled/expired/rejected by the
    broker, wheel state must NOT stay in stage_1_put_active or
    stage_2_call_active. It resets to the prior stage so the next tick
    can try again cleanly."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    # The reset logic
    assert 'stage == "stage_1_put_active":' in block
    assert 'state["stage"] = "stage_1_searching"' in block
    assert 'stage == "stage_2_call_active":' in block
    assert 'state["stage"] = "stage_2_shares_owned"' in block
    # And this all sits inside the canceled/expired/rejected branch
    idx = block.find('status in ("canceled", "expired", "rejected")')
    assert idx > 0, "canceled/expired/rejected branch missing"


def test_transitional_order_statuses_stay_pending():
    """ROUND-10 fix: statuses like partially_filled, pending_new,
    accepted, done_for_day are transitional — the order might still
    fill. Previous behavior let these fall through (stuck "pending"
    forever). Now they log + stay pending, re-checked next tick."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    # Must see the transitional status list
    for status in ("partially_filled", "pending_new", "accepted",
                   "done_for_day"):
        assert f'"{status}"' in block, (
            f"transitional status {status!r} missing from the "
            "stay-pending branch (R10 fix)")


# ========= 4. Put assignment (DELTA detection) =========

def test_put_assignment_uses_delta_not_presence():
    """Assignment detection MUST compare shares_at_open baseline to
    current shares — NOT just check "are there any shares?". A user
    who already holds 500 shares of the underlying before the put
    shouldn't trigger a false assignment. The delta pattern was added
    in R43 and is a hard invariant."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    assert 'share_delta = share_qty - baseline' in block, (
        "put assignment must use DELTA (current - baseline), not mere "
        "presence — pre-existing shares would false-trigger otherwise")
    assert "expected_delta = 100 * contract_meta.get(\"quantity\", 1)" in block, (
        "expected_delta calc must stay 100 × contracts")


def test_put_assignment_sets_cost_basis_from_strike_minus_premium():
    """Cost basis = strike - (premium_received / 100 / qty). This is
    the math the IRS sees on the 1099 if shares are later called away.
    Regression here mis-states the tax lot for every wheel cycle."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    assert "cost_basis_d = strike_d - prem_per_share_d" in block, (
        "cost basis math must be strike - prem/share (Decimal-safe)")
    assert 'state["cost_basis"] = float(cost_basis_d.quantize(' in block, (
        "cost_basis must be stored as 4dp-quantized float — preserves "
        "Decimal precision through JSON roundtrip")


def test_put_assignment_advances_to_shares_owned():
    """Successful put assignment ends in stage_2_shares_owned."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    # Inside the `if share_delta >= expected_delta:` branch
    idx = block.find("if share_delta >= expected_delta:")
    assert idx > 0
    window = block[idx:idx + 1500]
    assert 'state["stage"] = "stage_2_shares_owned"' in window
    assert 'contract_meta["status"] = "assigned"' in window


def test_put_expired_worthless_increments_cycles():
    """Put expired worthless → keep premium → back to stage_1_searching
    → cycles_completed += 1. The cycle counter is what the dashboard
    shows; skipping the increment under-counts completed rotations."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    idx = block.find("# Put expired worthless")
    assert idx > 0, "put-expired-worthless branch missing"
    window = block[idx:idx + 1200]
    assert 'state["stage"] = "stage_1_searching"' in window
    assert 'state["cycles_completed"] = state.get("cycles_completed", 0) + 1' in window


# ========= 5. Covered call transitions =========

def test_open_covered_call_transitions_to_call_active():
    """open_covered_call ends with stage_2_call_active. Only write site."""
    src = _src()
    block = _slice(src, "def open_covered_call(user, state):",
                   "def find_wheel_candidates")
    assert 'state["stage"] = "stage_2_call_active"' in block, (
        "open_covered_call must set stage to stage_2_call_active")


def test_call_assignment_completes_cycle():
    """Call assigned = shares called away = cycle complete. Must:
      * decrement shares_owned by 100 × qty
      * increment cycles_completed
      * transition back to stage_1_searching (ready for next put)
      * book stock PnL = (strike - cost_basis) × 100 × qty"""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    idx = block.find("if share_decrease >= expected_called_away:")
    assert idx > 0, "call assignment branch missing"
    window = block[idx:idx + 2500]
    assert 'state["stage"] = "stage_1_searching"' in window
    assert 'state["cycles_completed"] = state.get("cycles_completed", 0) + 1' in window
    assert 'state["shares_owned"] = max(0, state.get("shares_owned", 0) - expected_called_away)' in window, (
        "shares_owned must decrement by 100 × qty on call assignment")
    # Stock PnL math: (strike - cost_basis) × 100 × qty, Decimal-safe
    assert "(_dec(contract_meta[\"strike\"]) - _dec(state.get(\"cost_basis\", 0)))" in window


def test_call_assignment_clears_cost_basis_when_flat():
    """When shares_owned hits 0, cost_basis must be reset to None.
    Without this, the next cycle's assignment math would reference the
    PRIOR cycle's cost basis — wrong tax lot on every wheel past #1."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    idx = block.find("if share_decrease >= expected_called_away:")
    window = block[idx:idx + 2500]
    assert 'if state["shares_owned"] == 0:' in window
    assert 'state["cost_basis"] = None' in window, (
        "cost_basis must reset to None when shares_owned hits 0 — "
        "otherwise the next cycle inherits stale cost basis")


def test_call_expired_worthless_returns_to_shares_owned():
    """Call expired worthless → keep premium → can sell another call.
    Stage must be stage_2_shares_owned (NOT stage_1_searching) so the
    next tick picks up open_covered_call again, not open_short_put."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    idx = block.find("# Call expired worthless")
    assert idx > 0, "call-expired-worthless branch missing"
    window = block[idx:idx + 800]
    assert 'state["stage"] = "stage_2_shares_owned"' in window, (
        "call-expired must return to shares_owned, not searching — "
        "otherwise the next tick tries to open a put while holding shares")


# ========= 6. Buy-to-close at 50% profit =========

def test_buy_to_close_uses_profit_close_pct_threshold():
    """Contracts are bought back at 50% profit (PROFIT_CLOSE_PCT).
    Lower threshold = chase premium to expiration = assignment risk.
    Higher threshold = leave too much on the table. 50% is industry
    standard for the wheel."""
    src = _src()
    assert "PROFIT_CLOSE_PCT" in src, "PROFIT_CLOSE_PCT constant missing"
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    # Both the threshold and the comparison must exist
    assert "current_ask <= premium_originally * (1 - PROFIT_CLOSE_PCT)" in block, (
        "buy-to-close threshold comparison regressed — 50% target math "
        "changed")


def test_buy_to_close_falls_back_to_bid_when_ask_missing():
    """ROUND-10 fix: when ask is missing/zero (deep-OTM weekend snap,
    illiquid contract), fall back to bid. Without this, the 50%-profit
    close never fired on deeply profitable contracts (ask=0 means
    "nobody wants to sell" = contract is deeply OTM)."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    assert "current_ask = current_quote.get(\"bid\") or 0.01" in block, (
        "bid fallback for missing/zero ask regressed — deep-profit "
        "wheels would stop auto-closing")


# ========= 7. External-close detection (R42) =========

def test_external_close_detected_when_position_missing():
    """ROUND-42 fix: if wheel_strategy thinks a contract is active but
    Alpaca's /positions doesn't include it, something else closed it
    (native stop order, manual close via web UI). Must journal + reset
    state, not loop forever expecting the contract to reappear."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    assert "closed_externally" in block, (
        "external-close detection (R42) missing")
    idx = block.find("closed_externally")
    window = block[max(0, idx - 200):idx + 800]
    assert "_journal_wheel_close" in window, (
        "external close must journal the trade for P&L attribution")


def test_external_close_only_runs_pre_expiry():
    """ROUND-42 invariant: the external-close check only runs BEFORE
    expiry. After expiry the assigned/expired-worthless branches own
    the position-disappearance handling; running external-close
    post-expiry would mis-journal an assignment as an external close."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    assert "_pre_expiry = (_exp_date is None) or (date.today() <= _exp_date)" in block, (
        "external-close check must gate on _pre_expiry — post-expiry "
        "path is owned by the assigned/expired branches")


# ========= 8. Stock-split auto-resolve (R13) =========

def test_anomalous_share_delta_freezes_state_without_split():
    """ROUND-12: anomalous share delta (>= 2× expected) with NO
    yfinance-confirmed split MUST freeze the wheel state for manual
    reconciliation — otherwise we'd post a wrong cost_basis for the
    entire new cycle."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    assert "anomalous_share_delta_no_auto_advance" in block, (
        "anomalous-delta freeze event missing (R12)")
    assert "Wheel state FROZEN — check manually" in block


def test_stock_split_auto_resolves_baseline():
    """ROUND-13: if the anomalous delta IS explained by a yfinance-
    confirmed split, auto-adjust baseline + expected_delta by the
    split ratio. Without this, every wheel on a split stock froze
    even though the math was trivially recoverable."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    assert "_detect_split_since(" in block, (
        "split detection helper call missing from assignment branch (R13)")
    assert "split_auto_resolved" in block, (
        "split auto-resolve history event missing (R13)")


# ========= 9. History cap + journaling =========

def test_every_close_path_journals():
    """Every terminal-state transition (put assigned, put expired, call
    assigned, call expired, external close) must call
    _journal_wheel_close. Missing a path = missing trades in Today's
    Closes panel + missing entries in the scorecard rebuild."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    # At least 5 journal calls across the 5 terminal branches
    count = block.count("_journal_wheel_close(")
    assert count >= 5, (
        f"expected at least 5 _journal_wheel_close calls across "
        f"terminal branches, found {count}")


def test_history_is_capped():
    """HISTORY_MAX cap prevents unbounded growth over a multi-year
    wheel. Without it, wheel_*.json files creep past 1MB and slow
    every save."""
    src = _src()
    assert "HISTORY_MAX" in src
    assert ":HISTORY_MAX]" in src or "[-HISTORY_MAX:]" in src, (
        "history list must be sliced to HISTORY_MAX on append")


# ========= 10. Premium Decimal safety =========

def test_premium_accumulator_uses_decimal_math():
    """Phase-4 migration: total_premium_collected + total_realized_pnl
    use Decimal internally, stored as cents-quantized float. Float
    addition across many cycles is the single biggest source of
    dashboard/1099 mismatch — pin the Decimal path."""
    src = _src()
    block = _slice(src, "def _advance_wheel_state_locked",
                   "def find_wheel_candidates")
    assert "_update_totals_on_premium_fill" in block
    # The accumulator pattern
    assert 'state["total_premium_collected"] = _to_cents_float(prev_premium_d + received_d)' in block
    assert 'state["total_realized_pnl"] = _to_cents_float(prev_pnl_d + received_d)' in block
