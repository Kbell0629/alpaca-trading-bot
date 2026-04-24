"""Round-61 pt.26 — aggressive open-orders cross-check + loud
placement-failure logging for short/long stop placement.

User reported SOXL STILL showed missing_stop HIGH after pt.25
deploy. Pt.25 correctly handled canceled/rejected/expired order
statuses but missed cases where:
  - The order is in some non-dead-but-also-not-open status
    (e.g. "accepted", "pending_new")
  - Alpaca's `/orders/{id}` lookup returns partial / unexpected
    data that doesn't match the dead-status list

Pt.26 adds a belt-and-suspenders check: if state.cover_order_id (or
state.stop_order_id) is set but the id is NOT in Alpaca's
`/orders?status=open&symbols=<SYM>` list, reset + retry. This catches
any drift between our local state and Alpaca's source of truth.

Also: placement failures now log loudly with the Alpaca error text
so the audit's "missing_stop" finding has a diagnosable cause
instead of a silent skip.
"""
from __future__ import annotations

import json
import tempfile
import os


def _call_process_short(state, strat, alpaca_mock):
    import cloud_scheduler as cs
    tmpdir = tempfile.mkdtemp()
    fpath = os.path.join(tmpdir, "short_sell_SOXL.json")
    with open(fpath, "w") as f:
        json.dump(strat, f)
    user = {"id": 1, "username": "testuser",
            "_data_dir": tmpdir, "_strategies_dir": tmpdir}
    orig_get = cs.user_api_get
    orig_post = cs.user_api_post
    cs.user_api_get = alpaca_mock["get"]
    cs.user_api_post = alpaca_mock["post"]
    try:
        cs.process_short_strategy(user, fpath, strat, state, strat.get("rules", {}))
    finally:
        cs.user_api_get = orig_get
        cs.user_api_post = orig_post
    return state


def _make_soxl_state(cover_order_id=None):
    return {
        "entry_fill_price": 110.65,
        "shares_shorted": 29,
        "total_shares_held": 29,
        "cover_order_id": cover_order_id,
    }


def _make_soxl_strat(state):
    return {
        "symbol": "SOXL", "strategy": "short_sell", "status": "active",
        "state": state, "rules": {"stop_loss_pct": 0.08, "profit_target_pct": 0.15},
    }


def test_pt26_resets_when_cover_id_not_in_open_orders(monkeypatch):
    """Cover_order_id is set, /orders/{id} returns status=new
    (looks live), BUT /orders?status=open doesn't list it → reset."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders/ghost-id" in url:
            # Single-order lookup: reports as 'new' but doesn't
            # actually exist in the broker's open-orders list.
            return {"id": "ghost-id", "status": "new"}
        if "/orders?status=open" in url:
            # Empty list — order is a ghost.
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "fresh-id", "status": "new"}

    state = _make_soxl_state(cover_order_id="ghost-id")
    strat = _make_soxl_strat(state)
    _call_process_short(state, strat, {"get": _get, "post": _post})

    assert placed, (
        "Pt.26 cross-check must reset cover_order_id when it's not "
        "in the /orders?status=open list, even if individual-order "
        "status lookup says 'new'.")
    assert state["cover_order_id"] == "fresh-id"


def test_pt26_leaves_cover_id_if_actually_open(monkeypatch):
    """Live order, present in open-orders list → no reset, no re-place."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders/live-id" in url:
            return {"id": "live-id", "status": "new"}
        if "/orders?status=open" in url:
            return [{"id": "live-id", "status": "new",
                     "side": "buy", "type": "stop"}]
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "SHOULD_NOT_HAPPEN"}

    state = _make_soxl_state(cover_order_id="live-id")
    strat = _make_soxl_strat(state)
    _call_process_short(state, strat, {"get": _get, "post": _post})

    cover_stops = [p for p in placed
                   if p.get("side") == "buy" and p.get("type") == "stop"]
    assert not cover_stops, (
        f"Live cover_order_id must not be replaced. Got: {placed}")
    assert state["cover_order_id"] == "live-id"


def test_pt26_logs_placement_failure_loudly(monkeypatch, caplog):
    """When Alpaca rejects the BUY stop placement, we log the error
    text so the audit's missing_stop finding has a cause."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        return {"error": "HTTP 403: shorting disabled for this symbol"}

    state = _make_soxl_state(cover_order_id=None)
    strat = _make_soxl_strat(state)
    # Capture log() calls. cloud_scheduler.log writes to a structured
    # log; we intercept via monkeypatch.
    import cloud_scheduler as cs
    captured = []
    monkeypatch.setattr(cs, "log", lambda msg, *a, **kw: captured.append(msg))
    _call_process_short(state, strat, {"get": _get, "post": _post})
    failure_logs = [m for m in captured
                    if "placement FAILED" in m and "SOXL" in m]
    assert failure_logs, (
        f"Placement failure must log loudly. Captured: {captured}")


def test_pt26_source_pin_short():
    with open("cloud_scheduler.py") as f:
        src = f.read()
    # Aggressive open-orders check for shorts.
    idx = src.find("Pt.26: aggressive cross-check")
    assert idx > 0
    block = src[idx:idx + 1200]
    assert 'orders?status=open&symbols={symbol}' in block \
        or "orders?status=open" in block
    assert "open_ids" in block


def test_pt26_source_pin_long():
    with open("cloud_scheduler.py") as f:
        src = f.read()
    idx = src.find("Pt.26: aggressive cross-check (same as the short-side)")
    assert idx > 0
    block = src[idx:idx + 1200]
    assert "open_orders_long" in block
    assert "open_ids_long" in block
