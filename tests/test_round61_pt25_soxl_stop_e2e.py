"""Round-61 pt.25 — end-to-end test that SOXL-like scenarios actually
get a cover stop placed on the next monitor tick.

User-reported: pt.24 was supposed to reset stale cover_order_id and
retry placement, but SOXL still showed missing_stop in the audit.
Found a bug in pt.24's short-side code: the `elif` for handling
error-response dicts was unreachable (first branch already matched
on any dict). Pt.25 fixes the structure AND this test exercises the
full path end-to-end.

Scenarios covered:
  1. No existing cover_order_id + Alpaca accepts new order → stop
     lands, cover_order_id persisted.
  2. Stale cover_order_id + Alpaca returns 404 error dict → reset,
     then place fresh.
  3. Stale cover_order_id with status="canceled" → reset + retry.
  4. Stale cover_order_id with status="rejected" → reset + retry.
  5. Live cover_order_id with status="new" → leave alone.
"""
from __future__ import annotations

import json


def _call_process_short(state, strat, alpaca_mock):
    """Drive process_short_strategy with a mocked Alpaca. Returns the
    final state after one monitor tick."""
    import cloud_scheduler as cs
    import tempfile
    import os
    # Write strat to a temp file so save_json in the function works.
    tmpdir = tempfile.mkdtemp()
    fpath = os.path.join(tmpdir, "short_sell_SOXL.json")
    with open(fpath, "w") as f:
        json.dump(strat, f)
    rules = strat.get("rules", {})
    user = {"id": 1, "username": "testuser",
            "_data_dir": tmpdir, "_strategies_dir": tmpdir}

    # Patch Alpaca API helpers.
    import builtins
    orig_get = cs.user_api_get
    orig_post = cs.user_api_post
    cs.user_api_get = alpaca_mock["get"]
    cs.user_api_post = alpaca_mock["post"]
    try:
        cs.process_short_strategy(user, fpath, strat, state, rules)
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
        "current_stop_price": None,
    }


def _make_soxl_strat(state):
    return {
        "symbol": "SOXL",
        "strategy": "short_sell",
        "status": "active",
        "state": state,
        "rules": {"stop_loss_pct": 0.08, "profit_target_pct": 0.15},
    }


def test_soxl_no_existing_cover_order_places_adaptive_stop(monkeypatch):
    """Fresh adoption: cover_order_id=None, Alpaca accepts POST.
    Monitor must place a BUY stop at max(entry*1.10, current*1.05)."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)

    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}  # current price
        if "/orders/" in url:
            # Shouldn't be called — no existing cover_order_id.
            return {"error": "unexpected lookup"}
        return {"error": "not mocked"}

    def _post(user, url, data):
        if url == "/orders":
            placed.append(data)
            return {"id": "fake-new-order-id", "status": "new"}
        return {"error": "not mocked"}

    state = _make_soxl_state(cover_order_id=None)
    strat = _make_soxl_strat(state)
    _call_process_short(state, strat, {"get": _get, "post": _post})

    assert len(placed) >= 1, "Monitor must POST a new order"
    buy_stops = [p for p in placed
                 if p.get("side") == "buy" and p.get("type") == "stop"]
    assert buy_stops, f"Expected a BUY stop, got: {placed}"
    stop_price = float(buy_stops[0]["stop_price"])
    # Adaptive formula: max(110.65*1.10, 128.86*1.05) = max(121.72, 135.30)
    assert stop_price >= 135.00, (
        f"Expected adaptive stop around $135.30, got ${stop_price}")
    # state updated.
    assert state["cover_order_id"] == "fake-new-order-id"
    assert abs(state["current_stop_price"] - stop_price) < 0.01


def test_soxl_stale_cover_id_with_error_dict_resets_and_retries(monkeypatch):
    """Pt.24 bug: elif on error dict was unreachable. Pt.25 fix:
    check 'error' FIRST. This test forces Alpaca to return an error
    dict for the existing cover_order_id lookup — monitor must
    reset to None and place a fresh stop."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)

    placed = []
    orders_lookups = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders/stale-id" in url:
            orders_lookups.append(url)
            return {"error": "order not found (404)"}
        return {"error": "not mocked"}

    def _post(user, url, data):
        if url == "/orders":
            placed.append(data)
            return {"id": "fresh-order-id", "status": "new"}
        return {"error": "not mocked"}

    state = _make_soxl_state(cover_order_id="stale-id")
    strat = _make_soxl_strat(state)
    _call_process_short(state, strat, {"get": _get, "post": _post})

    assert orders_lookups, "Monitor must have queried the stale id"
    assert placed, (
        f"After error-dict lookup, monitor MUST reset + place a fresh "
        f"stop. Nothing placed. orders_lookups={orders_lookups}")
    assert state["cover_order_id"] == "fresh-order-id"


def test_soxl_stale_cover_id_canceled_resets_and_retries(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders/canceled-id" in url:
            return {"id": "canceled-id", "status": "canceled"}
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "retry-order-id", "status": "new"}

    state = _make_soxl_state(cover_order_id="canceled-id")
    strat = _make_soxl_strat(state)
    _call_process_short(state, strat, {"get": _get, "post": _post})

    assert placed, "Canceled order must trigger reset + fresh placement"
    assert state["cover_order_id"] == "retry-order-id"


def test_soxl_stale_cover_id_rejected_resets_and_retries(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders/rejected-id" in url:
            return {"id": "rejected-id", "status": "rejected"}
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "retry-order-id", "status": "new"}

    state = _make_soxl_state(cover_order_id="rejected-id")
    strat = _make_soxl_strat(state)
    _call_process_short(state, strat, {"get": _get, "post": _post})

    assert placed, "Rejected order must trigger reset + fresh placement"


def test_soxl_live_cover_id_does_not_replace(monkeypatch):
    """A healthy 'new' order should NOT be replaced on every tick —
    monitor should leave it alone."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders/live-id" in url:
            return {"id": "live-id", "status": "new", "stop_price": "135.00"}
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "SHOULD_NOT_HAPPEN"}

    state = _make_soxl_state(cover_order_id="live-id")
    strat = _make_soxl_strat(state)
    _call_process_short(state, strat, {"get": _get, "post": _post})

    # No new POST should have happened for the cover stop (profit
    # target POST may happen separately, but not the cover stop).
    cover_stops = [p for p in placed
                   if p.get("side") == "buy" and p.get("type") == "stop"]
    assert not cover_stops, (
        "Live cover_order_id must NOT be replaced every tick — "
        f"monitor should leave it. Got: {placed}")
    # cover_order_id preserved.
    assert state["cover_order_id"] == "live-id"
