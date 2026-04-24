"""Round-61 pt.30 — position-drift guard in process_short_strategy
and process_strategy_file.

User-reported: SOXL SHORT cover-stop placement fails every ~2 min with
HTTP 403 "insufficient qty available for order (requested: 29,
available: 0)". Strategy file says we're short 29 SOXL, but the real
Alpaca position is gone (externally covered — manual or margin call).
Pt.26's open-orders cross-check resets ``cover_order_id`` because no
cover order is live, then placement retries forever. Pt.29 surfaced
the Alpaca error body so we can actually see it; pt.30 fixes the
underlying loop.

Fix: query /positions/{symbol} at the top of the strategy-processing
path. If Alpaca returns a 404 error dict or ``qty == 0``, mark the
strategy closed, cancel lingering orders, record a best-effort journal
close, and stop. If Alpaca reports a smaller-magnitude position than
state expects (partial external cover), sync state to the broker's
truth so the next placement uses the correct qty.

Same guard applied on the long path in process_strategy_file — a user
selling manually without going through the bot hits the same loop.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile


def _reload(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


def _call_process_short(cs, state, strat, alpaca_mock, extended_hours=False):
    tmpdir = tempfile.mkdtemp()
    fpath = os.path.join(tmpdir, "short_sell_SOXL.json")
    with open(fpath, "w") as f:
        json.dump(strat, f)
    user = {"id": 1, "username": "testuser",
            "_data_dir": tmpdir, "_strategies_dir": tmpdir}
    orig_get = cs.user_api_get
    orig_post = cs.user_api_post
    orig_del = cs.user_api_delete
    cs.user_api_get = alpaca_mock["get"]
    cs.user_api_post = alpaca_mock["post"]
    cs.user_api_delete = alpaca_mock.get("delete", lambda u, url: {})
    try:
        cs.process_short_strategy(
            user, fpath, strat, state, strat.get("rules", {}),
            extended_hours=extended_hours,
        )
    finally:
        cs.user_api_get = orig_get
        cs.user_api_post = orig_post
        cs.user_api_delete = orig_del
    # Re-load persisted state so tests can assert on-disk result too
    with open(fpath) as f:
        persisted = json.load(f)
    return state, persisted, fpath, user


def _call_process_long(cs, strat, alpaca_mock, extended_hours=False):
    tmpdir = tempfile.mkdtemp()
    symbol = strat["symbol"]
    fpath = os.path.join(tmpdir, f"trailing_stop_{symbol}.json")
    with open(fpath, "w") as f:
        json.dump(strat, f)
    user = {"id": 1, "username": "testuser",
            "_data_dir": tmpdir, "_strategies_dir": tmpdir}
    orig_get = cs.user_api_get
    orig_post = cs.user_api_post
    orig_del = cs.user_api_delete
    cs.user_api_get = alpaca_mock["get"]
    cs.user_api_post = alpaca_mock["post"]
    cs.user_api_delete = alpaca_mock.get("delete", lambda u, url: {})
    try:
        cs.process_strategy_file(user, fpath, strat,
                                  extended_hours=extended_hours)
    finally:
        cs.user_api_get = orig_get
        cs.user_api_post = orig_post
        cs.user_api_delete = orig_del
    with open(fpath) as f:
        persisted = json.load(f)
    return persisted, fpath, user


def _soxl_state(cover_order_id=None, target_order_id=None):
    return {
        "entry_fill_price": 110.65,
        "shares_shorted": 29,
        "total_shares_held": 29,
        "cover_order_id": cover_order_id,
        "target_order_id": target_order_id,
    }


def _soxl_strat(state):
    return {
        "symbol": "SOXL", "strategy": "short_sell", "status": "active",
        "state": state, "created": "2026-04-23",
        "rules": {
            "stop_loss_pct": 0.08, "profit_target_pct": 0.15,
            "short_trail_activation_pct": 0.05,
            "short_trail_distance_pct": 0.05,
            "max_hold_days": 14,
        },
    }


def _aapl_state():
    return {
        "entry_fill_price": 180.00,
        "total_shares_held": 10,
    }


def _aapl_strat(state):
    return {
        "symbol": "AAPL", "strategy": "trailing_stop", "status": "active",
        "state": state, "created": "2026-04-23",
        "initial_qty": 10,
        "rules": {
            "stop_loss_pct": 0.10, "trail_activation_pct": 0.05,
            "trail_distance_pct": 0.05,
        },
    }


# ========== SHORT PATH ==========

# --------- 1. Position gone → strategy closed ---------

def test_short_position_404_marks_strategy_closed(monkeypatch):
    """The SOXL case. Alpaca returns 404 for /positions/SOXL (no
    position exists). Strategy file MUST be marked closed so the
    monitor stops retrying."""
    cs = _reload(monkeypatch)
    placed = []
    deleted = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            return {"error": "404 position does not exist"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "SHOULD_NOT_FIRE", "status": "new"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    state = _soxl_state(cover_order_id="stale-cover-id",
                        target_order_id="stale-target-id")
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post, "delete": _delete})

    assert strat["status"] == "closed", (
        "Pt.30: missing Alpaca position must mark strategy closed. "
        f"Status={strat['status']}")
    assert state["exit_reason"] == "closed_externally"
    assert state["shares_shorted"] == 0
    assert state["total_shares_held"] == 0
    # No retry placement
    assert not any(p.get("type") == "stop" for p in placed), (
        f"Pt.30: must NOT place a cover-stop when position is gone. "
        f"Got: {placed}")
    # Lingering orders canceled
    assert "/orders/stale-cover-id" in deleted
    assert "/orders/stale-target-id" in deleted
    # State ids cleared
    assert state["cover_order_id"] is None
    assert state["target_order_id"] is None


# --------- 2. Position qty=0 → strategy closed ---------

def test_short_position_qty_zero_marks_strategy_closed(monkeypatch):
    """Alpaca occasionally returns a position dict with qty=0 after an
    intra-tick close. Same handling as 404."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            return {"symbol": "SOXL", "qty": "0", "side": "short"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "SHOULD_NOT_FIRE", "status": "new"}

    state = _soxl_state()
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post})

    assert strat["status"] == "closed"
    assert state["exit_reason"] == "closed_externally"
    assert not placed, f"Pt.30: no orders placed when qty=0. Got: {placed}"


# --------- 3. Partial drift → shares synced ---------

def test_short_position_partial_drift_syncs_shares(monkeypatch):
    """Alpaca reports qty=-15 (short 15), state says 29. We were partially
    externally covered. Sync shares_shorted=15 so the next cover-stop
    placement uses the correct qty."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            # Alpaca reports shorts with negative qty
            return {"symbol": "SOXL", "qty": "-15", "side": "short"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": f"order-{len(placed)}", "status": "new"}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post})

    # State synced
    assert state["shares_shorted"] == 15, (
        f"Pt.30: state should sync to broker truth (15), "
        f"got {state['shares_shorted']}")
    assert state["total_shares_held"] == 15
    # Strategy NOT closed (still have 15 short)
    assert strat["status"] == "active"
    # Cover-stop placed with qty=15 (the real position)
    cover_stops = [p for p in placed
                   if p.get("side") == "buy" and p.get("type") == "stop"]
    assert cover_stops, f"Pt.30: should still place stop. Got: {placed}"
    assert cover_stops[0]["qty"] == "15", (
        f"Pt.30: cover-stop qty must match synced state. "
        f"Got: {cover_stops[0]}")


# --------- 4. Position matches state → normal flow ---------

def test_short_position_matches_state_runs_normal_flow(monkeypatch):
    """Regression pin: when Alpaca /positions reports the expected
    magnitude, the drift guard must be a no-op and the rest of the
    function (cover-stop placement) must run normally."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            return {"symbol": "SOXL", "qty": "-29", "side": "short"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": f"order-{len(placed)}", "status": "new"}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post})

    # Strategy stays active
    assert strat["status"] == "active"
    # State unchanged
    assert state["shares_shorted"] == 29
    # Cover-stop placed normally
    cover_stops = [p for p in placed
                   if p.get("side") == "buy" and p.get("type") == "stop"]
    assert cover_stops
    assert cover_stops[0]["qty"] == "29"


# --------- 5. AH mode honors the drift guard ---------

def test_short_position_drift_guard_fires_in_ah_mode(monkeypatch):
    """The phantom-position loop ran in AH mode too (pt.28 lets AH
    trigger placement for unprotected shorts). Drift guard must fire
    there as well — otherwise the loop survives weekends."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            return {"error": "404 position does not exist"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "SHOULD_NOT_FIRE", "status": "new"}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post},
                        extended_hours=True)

    assert strat["status"] == "closed"
    assert not placed, (
        f"Pt.30: AH mode must also skip placement when position gone. "
        f"Got: {placed}")


# --------- 6. Journal close recorded on drift-close ---------

def test_short_drift_close_records_journal_entry(monkeypatch):
    """When we close a ghost position, record the exit in the trade
    journal so the scorecard + dashboard see it. Uses current price
    as approximate exit (actual fill price is unknown)."""
    cs = _reload(monkeypatch)
    calls = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            return {"error": "404"}
        return {}

    def _post(user, url, data):
        return {"id": "x"}

    orig_record = cs.record_trade_close

    def _fake_record(user, symbol, strategy, exit_price, pnl,
                     exit_reason, qty=None, side="sell"):
        calls.append({
            "symbol": symbol, "strategy": strategy, "exit_price": exit_price,
            "pnl": pnl, "exit_reason": exit_reason, "qty": qty, "side": side,
        })

    cs.record_trade_close = _fake_record
    try:
        state = _soxl_state()
        strat = _soxl_strat(state)
        _call_process_short(cs, state, strat,
                            {"get": _get, "post": _post})
    finally:
        cs.record_trade_close = orig_record

    assert len(calls) == 1, f"Pt.30: expect one journal close. Got: {calls}"
    call = calls[0]
    assert call["symbol"] == "SOXL"
    assert call["strategy"] == "short_sell"
    assert call["exit_reason"] == "closed_externally"
    assert call["qty"] == 29
    assert call["side"] == "buy"  # covering a short
    # P&L = (entry - current) * shares for shorts
    # = (110.65 - 127.82) * 29 = -497.93 (short is underwater)
    assert abs(call["pnl"] - (-497.93)) < 0.5


# ========== LONG PATH ==========

# --------- 7. Long position gone → strategy closed ---------

def test_long_position_404_marks_strategy_closed(monkeypatch):
    """Same drift bug bites longs: user manually sold, strategy file
    still thinks we hold 10 AAPL, next sell-stop placement 403s."""
    cs = _reload(monkeypatch)
    placed = []
    deleted = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 185.00}}
        if url == "/positions/AAPL":
            return {"error": "404 position does not exist"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "SHOULD_NOT_FIRE", "status": "new"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    state = _aapl_state()
    state["stop_order_id"] = "stale-stop-id"
    strat = _aapl_strat(state)
    _call_process_long(cs, strat,
                       {"get": _get, "post": _post, "delete": _delete})

    assert strat["status"] == "closed"
    assert state["exit_reason"] == "closed_externally"
    assert state["total_shares_held"] == 0
    assert not any(p.get("type") == "stop" for p in placed), (
        f"Pt.30: no sell-stop placement when position gone. Got: {placed}")
    assert "/orders/stale-stop-id" in deleted


# --------- 8. Long partial drift → shares synced ---------

def test_long_position_partial_drift_syncs_shares(monkeypatch):
    """User sold 4 of 10 AAPL manually. Alpaca reports qty=6. State
    must sync to 6 so the next sell-stop doesn't 403 on qty=10."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 185.00}}
        if url == "/positions/AAPL":
            return {"symbol": "AAPL", "qty": "6", "side": "long"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": f"order-{len(placed)}", "status": "new"}

    state = _aapl_state()
    strat = _aapl_strat(state)
    _call_process_long(cs, strat,
                       {"get": _get, "post": _post})

    assert state["total_shares_held"] == 6, (
        f"Pt.30: long state should sync to 6, got {state['total_shares_held']}")
    assert strat["status"] == "active"
    sell_stops = [p for p in placed
                  if p.get("side") == "sell" and p.get("type") == "stop"]
    assert sell_stops, f"Pt.30: should still place sell-stop. Got: {placed}"
    assert sell_stops[0]["qty"] == "6"


# --------- 9. Long normal case → drift guard no-op ---------

def test_long_position_matches_state_runs_normal_flow(monkeypatch):
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 185.00}}
        if url == "/positions/AAPL":
            return {"symbol": "AAPL", "qty": "10", "side": "long"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": f"order-{len(placed)}", "status": "new"}

    state = _aapl_state()
    strat = _aapl_strat(state)
    _call_process_long(cs, strat,
                       {"get": _get, "post": _post})

    assert strat["status"] == "active"
    assert state["total_shares_held"] == 10
    sell_stops = [p for p in placed
                  if p.get("side") == "sell" and p.get("type") == "stop"]
    assert sell_stops
    assert sell_stops[0]["qty"] == "10"


# --------- 10. Fail-open: unknown response shape does NOT close strategy ---------

def test_drift_guard_fails_open_on_unknown_position_shape(monkeypatch):
    """Safety pin: the guard must only close on POSITIVE evidence of
    drift — a 404 error dict, or a dict with an explicit ``qty`` field.
    If Alpaca returns something we don't recognize (empty dict,
    transient oddity, schema surprise), we must NOT close a live
    strategy. Default flow (cover-stop placement) runs as normal."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            # Empty dict — no "error" key, no "qty" field. Must be
            # treated as "unknown", NOT "position gone".
            return {}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "fresh-stop", "status": "new"}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post})

    # Strategy stays active (NOT falsely closed)
    assert strat["status"] == "active", (
        "Pt.30: unknown Alpaca response must NOT close a live strategy. "
        f"Status={strat['status']}")
    # Normal flow ran — cover-stop placed
    cover_stops = [p for p in placed
                   if p.get("side") == "buy" and p.get("type") == "stop"]
    assert cover_stops, (
        f"Pt.30: normal placement must run on unknown response. Got: {placed}")


# --------- 11. Transient errors MUST NOT trigger close ---------

def test_drift_guard_ignores_transient_errors(monkeypatch):
    """If /positions returns a non-404 error (circuit breaker, rate
    limit, 5xx), the guard must fail open. Closing live strategies
    because of a transient Alpaca outage would be catastrophic."""
    cs = _reload(monkeypatch)
    for transient_err in [
        "circuit_breaker_open",
        "rate_limited_local",
        "Request failed after retries",
        "HTTP 500: Internal Server Error",
        "HTTP 429: Too Many Requests",
    ]:
        placed = []

        def _get(user, url, _err=transient_err):
            if "/trades/latest" in url:
                return {"trade": {"p": 127.82}}
            if url == "/positions/SOXL":
                return {"error": _err}
            if "/orders?status=open" in url:
                return []
            return {}

        def _post(user, url, data, _placed=placed):
            _placed.append(data)
            return {"id": "fresh", "status": "new"}

        state = _soxl_state(cover_order_id=None)
        strat = _soxl_strat(state)
        _call_process_short(cs, state, strat,
                            {"get": _get, "post": _post})

        assert strat["status"] == "active", (
            f"Pt.30: transient error ({transient_err!r}) must NOT close "
            f"a live strategy. Status={strat['status']}")
        # Normal flow still ran (cover-stop placed)
        cover_stops = [p for p in placed
                       if p.get("side") == "buy" and p.get("type") == "stop"]
        assert cover_stops, (
            f"Pt.30: transient error ({transient_err!r}) must still allow "
            f"normal placement. Got: {placed}")


# --------- 12. Real 404 message shapes all trigger close ---------

def test_drift_guard_fires_on_various_404_message_shapes(monkeypatch):
    """Alpaca's exact error string varies across SDK versions and
    regions. Make sure our matcher accepts the common shapes."""
    cs = _reload(monkeypatch)
    for msg_404 in [
        "HTTP 404: Not Found — position does not exist",
        "404 not found",
        "position not found",
        "HTTP 404: Not Found",
    ]:
        placed = []

        def _get(user, url, _msg=msg_404):
            if "/trades/latest" in url:
                return {"trade": {"p": 127.82}}
            if url == "/positions/SOXL":
                return {"error": _msg}
            if "/orders?status=open" in url:
                return []
            return {}

        def _post(user, url, data, _placed=placed):
            _placed.append(data)
            return {"id": "x", "status": "new"}

        state = _soxl_state(cover_order_id=None)
        strat = _soxl_strat(state)
        _call_process_short(cs, state, strat,
                            {"get": _get, "post": _post})

        assert strat["status"] == "closed", (
            f"Pt.30: 404-style message ({msg_404!r}) must close strategy. "
            f"Status={strat['status']}")
        assert not placed, (
            f"Pt.30: no placement on 404. msg={msg_404!r}, got: {placed}")


# --------- 13. Qty-available retry: cancel stale BUY orders, retry once ---------

def test_qty_unavailable_error_flushes_orders_and_retries(monkeypatch):
    """The actual SOXL production bug. Position IS live at Alpaca
    (-29), but placement fails with "insufficient qty available for
    order (requested: 29, available: 0)". Some hidden BUY order is
    reserving the qty. Retry path must cancel the stale BUY orders,
    then retry placement once."""
    cs = _reload(monkeypatch)
    placed = []
    deleted = []
    get_calls = []

    def _get(user, url):
        get_calls.append(url)
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            # Position is live — 29 shares short.
            return {"symbol": "SOXL", "qty": "-29", "side": "short"}
        if "/orders?status=open" in url and "symbols=SOXL" in url:
            # Hidden orphan BUY limit is reserving all 29 shares.
            return [{"id": "orphan-buy-id", "side": "buy", "type": "limit",
                     "status": "new"}]
        return {}

    def _post(user, url, data):
        placed.append(data)
        # First placement fails with the qty-available error, second
        # (post-cancel) succeeds.
        if len(placed) == 1:
            return {"error": ("HTTP 403: Forbidden — insufficient qty "
                              "available for order (requested: 29, "
                              "available: 0) alpaca_code=40310000")}
        return {"id": "fresh-cover-id", "status": "new"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post, "delete": _delete},
                        extended_hours=True)

    # Two placement attempts — initial + retry. AH mode, so no
    # follow-up target-limit placement confuses the count.
    assert len(placed) == 2, (
        f"Pt.30: expected 2 placement attempts (initial + retry). "
        f"Got {len(placed)}: {placed}")
    # Both were cover-stops (same shape)
    for p in placed:
        assert p.get("type") == "stop" and p.get("side") == "buy"
    # Orphan BUY canceled
    assert "/orders/orphan-buy-id" in deleted, (
        f"Pt.30: orphan BUY must be canceled. Deleted: {deleted}")
    # Retry succeeded
    assert state["cover_order_id"] == "fresh-cover-id"


def test_qty_unavailable_retry_only_cancels_buy_orders(monkeypatch):
    """Safety pin: the retry path must not cancel SELL orders — only
    BUY orders compete for short-cover qty. A SELL order on the same
    symbol would be for a different position (long) and must be
    untouched."""
    cs = _reload(monkeypatch)
    deleted = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            return {"symbol": "SOXL", "qty": "-29", "side": "short"}
        if "/orders?status=open" in url:
            return [
                {"id": "buy-orphan", "side": "buy", "type": "limit",
                 "status": "new"},
                {"id": "sell-unrelated", "side": "sell", "type": "stop",
                 "status": "new"},
            ]
        return {}

    def _post(user, url, data):
        if not getattr(_post, "_n", 0):
            _post._n = 1
            return {"error": ("HTTP 403 insufficient qty available "
                              "alpaca_code=40310000")}
        return {"id": "retry-ok", "status": "new"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post, "delete": _delete},
                        extended_hours=True)

    assert "/orders/buy-orphan" in deleted
    assert "/orders/sell-unrelated" not in deleted, (
        f"Pt.30: retry path must not cancel SELL orders. Deleted: {deleted}")


def test_qty_unavailable_retry_clears_stale_state_ids(monkeypatch):
    """If the canceled BUY happens to be the target_order_id we have
    in state, that state id must be cleared so we don't think we
    still have a live target order."""
    cs = _reload(monkeypatch)
    deleted = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            return {"symbol": "SOXL", "qty": "-29"}
        if "/orders?status=open" in url:
            return [{"id": "our-target-id", "side": "buy", "type": "limit",
                     "status": "new"}]
        return {}

    def _post(user, url, data):
        if not getattr(_post, "_n", 0):
            _post._n = 1
            return {"error": "40310000 insufficient qty available"}
        return {"id": "new-cover", "status": "new"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    state = _soxl_state(cover_order_id=None, target_order_id="our-target-id")
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post, "delete": _delete},
                        extended_hours=True)

    # target_order_id cleared because that exact id was canceled.
    # AH mode so target doesn't get re-placed in the same tick.
    assert state.get("target_order_id") is None, (
        f"Pt.30: canceled target_order_id must be cleared from state. "
        f"Got: {state.get('target_order_id')}")


def test_non_qty_error_does_not_trigger_retry(monkeypatch):
    """Other placement errors (auth, 5xx, asset-not-shortable) must
    NOT trigger the cancel-and-retry. Only qty-available errors do."""
    cs = _reload(monkeypatch)
    placed = []
    deleted = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 127.82}}
        if url == "/positions/SOXL":
            return {"symbol": "SOXL", "qty": "-29"}
        if "/orders?status=open" in url:
            return [{"id": "some-buy", "side": "buy", "type": "limit",
                     "status": "new"}]
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"error": "HTTP 403: Forbidden — asset SOXL is not shortable"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post, "delete": _delete},
                        extended_hours=True)

    # Only ONE placement attempt (no retry)
    assert len(placed) == 1, (
        f"Pt.30: non-qty errors must not trigger retry. Got: {placed}")
    # No cancels (retry path not entered)
    assert not deleted, (
        f"Pt.30: non-qty errors must not cancel any orders. Got: {deleted}")


# --------- 14. Source-pin: drift guard calls /positions/{symbol} ---------

def test_source_pin_drift_guard_present(monkeypatch):
    """Pin the drift-guard pattern at the source level so a refactor
    that drops the /positions check surfaces immediately."""
    import pathlib
    src = pathlib.Path("cloud_scheduler.py").read_text()
    # Short path
    assert src.count('/positions/{symbol}') >= 2, (
        "Pt.30: both process_short_strategy and process_strategy_file "
        "must query /positions/{symbol} for the drift guard.")
    # closed_externally exit_reason should appear at both guard sites
    assert src.count('closed_externally') >= 2
    # The narrative marker — don't silently remove the guard
    assert 'pt.30' in src.lower() or 'position-drift guard' in src
