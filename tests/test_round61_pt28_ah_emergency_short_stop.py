"""Round-61 pt.28 — emergency cover-stop placement in AH mode for
unprotected short positions.

Pt.24/25/26 built out the liveness + cross-check infrastructure so
that `process_short_strategy` correctly resets a stale `cover_order_id`
and retries placement. But pt.26 deployed AFTER Friday's 4:00 PM
market close on 2026-04-24. The Round-55 invariant hard-skipped
shorts in after-hours mode (both at the outer monitor loop AND
inside process_strategy_file), so the pt.26 fix never had a chance
to run on SOXL until Monday 9:30 AM — leaving the short unprotected
for the full weekend.

Pt.28 narrows the R55 AH skip: shorts now flow through to
`process_short_strategy(extended_hours=True)`, which runs ONLY the
initial cover-stop placement path for positions with no live
`cover_order_id`. Everything else on the short side
(profit-target limit placement, trailing-stop tightening,
cover-fill processing, force-cover on max-hold) stays regular-hours-
only because those depend on thin-book liquidity or regular-hours
execution.

Safety argument: we place cover-stops with `time_in_force=gtc` and
do NOT set `extended_hours=true` on the order. Alpaca only triggers
such stops when REGULAR-hours price crosses. So a stop submitted in
AH sits idle at the broker, waiting for next regular-hours open.
The "brutal thin-book cover fill" concern that motivated the
original R55 skip doesn't apply to placement — it only applies to
firing. Placement in AH is the same risk profile as placement in
regular hours.
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
    orig_patch = getattr(cs, "user_api_patch", None)
    orig_del = getattr(cs, "user_api_delete", None)
    cs.user_api_get = alpaca_mock["get"]
    cs.user_api_post = alpaca_mock["post"]
    if "patch" in alpaca_mock:
        cs.user_api_patch = alpaca_mock["patch"]
    if "delete" in alpaca_mock:
        cs.user_api_delete = alpaca_mock["delete"]
    try:
        cs.process_short_strategy(
            user, fpath, strat, state, strat.get("rules", {}),
            extended_hours=extended_hours,
        )
    finally:
        cs.user_api_get = orig_get
        cs.user_api_post = orig_post
        if orig_patch is not None:
            cs.user_api_patch = orig_patch
        if orig_del is not None:
            cs.user_api_delete = orig_del
    return state


def _soxl_state(cover_order_id=None, target_order_id=None,
                lowest_price_seen=None, trailing_activated=False):
    return {
        "entry_fill_price": 110.65,
        "shares_shorted": 29,
        "total_shares_held": 29,
        "cover_order_id": cover_order_id,
        "target_order_id": target_order_id,
        "lowest_price_seen": lowest_price_seen,
        "trailing_activated": trailing_activated,
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


# --------- 1. AH mode PLACES a stop when cover_order_id is None ---------

def test_ah_places_cover_stop_when_unprotected(monkeypatch):
    """The whole point of pt.28: an unprotected short in AH mode must
    get a protective BUY STOP placed, not be skipped."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "fresh-stop-id", "status": "new"}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post},
                        extended_hours=True)

    cover_stops = [p for p in placed
                   if p.get("side") == "buy" and p.get("type") == "stop"]
    assert cover_stops, (
        "Pt.28: AH mode must place a BUY STOP for unprotected short. "
        f"Got placements: {placed}")
    assert state["cover_order_id"] == "fresh-stop-id"
    # Adaptive: max(entry*1.10, current*1.05) = max(121.72, 135.30) = 135.30
    assert float(cover_stops[0]["stop_price"]) == 135.30


# --------- 2. AH mode does NOT place profit-target limit ---------

def test_ah_does_not_place_profit_target_limit(monkeypatch):
    """Profit-target limits are thin-book-risky (a weekend AH quote
    near the target could fire a $94 buy on stale liquidity). Stay
    regular-hours-only."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        # Return fresh-stop-id for the cover stop, no limit placement
        # should ever fire so the id here only matters for the first call.
        return {"id": f"order-{len(placed)}", "status": "new"}

    state = _soxl_state(cover_order_id=None, target_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post},
                        extended_hours=True)

    limits = [p for p in placed
              if p.get("side") == "buy" and p.get("type") == "limit"]
    assert not limits, (
        "Pt.28: AH mode must NOT place a profit-target limit. "
        f"Got placements: {placed}")
    # target_order_id must remain None
    assert state.get("target_order_id") is None


# --------- 3. AH mode does NOT tighten trailing stop ---------

def test_ah_does_not_tighten_trailing_stop(monkeypatch):
    """Trailing tighten is part of R55's regular-hours-only path for
    shorts (pt.28 only carves out initial placement)."""
    cs = _reload(monkeypatch)
    placed = []
    patched = []
    deleted = []

    def _get(user, url):
        if "/trades/latest" in url:
            # Price well below entry — would normally trigger tightening
            return {"trade": {"p": 100.00}}
        if "/orders/live-stop-id" in url:
            return {"id": "live-stop-id", "status": "new"}
        if "/orders?status=open" in url:
            return [{"id": "live-stop-id", "status": "new",
                     "side": "buy", "type": "stop"}]
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "SHOULD_NOT_FIRE", "status": "new"}

    def _patch(user, url, data):
        patched.append((url, data))
        return {"id": "SHOULD_NOT_FIRE", "status": "new"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    state = _soxl_state(cover_order_id="live-stop-id",
                        lowest_price_seen=105.0,
                        trailing_activated=True)
    state["current_stop_price"] = 135.30
    strat = _soxl_strat(state)
    _call_process_short(
        cs, state, strat,
        {"get": _get, "post": _post, "patch": _patch, "delete": _delete},
        extended_hours=True,
    )

    # No stop-price PATCH or cancel-and-replace
    assert not patched, f"AH must not PATCH stop price. Got: {patched}"
    # The only allowed POST is the initial placement; since cover_order_id
    # is set + live, no new placement either.
    assert not placed, f"AH must not re-place stop. Got: {placed}"


# --------- 4. Regular hours unchanged — still runs full flow ---------

def test_regular_hours_still_places_target_limit(monkeypatch):
    """Regression pin: regular-hours mode must still place the
    profit-target limit. Pt.28 ONLY gates the AH path."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": f"order-{len(placed)}", "status": "new"}

    state = _soxl_state(cover_order_id=None, target_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post},
                        extended_hours=False)

    stops = [p for p in placed if p.get("type") == "stop"]
    limits = [p for p in placed if p.get("type") == "limit"]
    assert stops, f"Regular-hours must still place stop. Got: {placed}"
    assert limits, f"Regular-hours must still place target limit. Got: {placed}"


# --------- 5. GTC without extended_hours flag (safety) ---------

def test_ah_placed_stop_is_gtc_without_extended_hours_flag(monkeypatch):
    """Safety pin: the stop we place in AH must be ``time_in_force=gtc``
    and must NOT carry ``extended_hours=true``. Otherwise Alpaca would
    fire it against thin AH quotes — exactly the risk R55 was avoiding.
    GTC without the flag means the stop sits idle until regular-hours
    price crosses."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "fresh", "status": "new"}

    state = _soxl_state(cover_order_id=None)
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post},
                        extended_hours=True)

    assert placed
    order = placed[0]
    assert order.get("time_in_force") == "gtc"
    assert order.get("type") == "stop"
    # Crucial: don't ever opt into AH-fire for this stop.
    assert order.get("extended_hours") is not True
    assert "extended_hours" not in order or order["extended_hours"] is False


# --------- 6. AH loop level: short_sell no longer skipped ---------

def test_ah_loop_no_longer_skips_short_sell(monkeypatch):
    """Source pin for the outer AH per-user loop in monitor_strategies.
    Pt.28 removed the `or strat.get("strategy") == "short_sell"` clause
    from the skip condition so shorts flow through to
    process_strategy_file → process_short_strategy."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    idx = src.find('if extended_hours:')
    assert idx > 0
    # Slice out just the AH branch up to the regular-hours comment marker
    ah_block = src[idx:idx + 3500]
    # Paused still skipped
    assert 'if strat.get("paused"):' in ah_block
    # short_sell clause removed
    assert 'strat.get("strategy") == "short_sell"' not in ah_block
    # Still skips wheel files
    assert 'fname.startswith("wheel_")' in ah_block


# --------- 7. process_strategy_file delegates correctly ---------

def test_process_strategy_file_passes_extended_hours_through(monkeypatch):
    """Source pin for process_strategy_file's short_sell delegation.
    Before pt.28 it was an early `return` in AH; now it must pass
    extended_hours through to process_short_strategy."""
    cs = _reload(monkeypatch)
    src = open(cs.__file__).read()
    idx = src.find('if strategy_type == "short_sell":')
    assert idx > 0
    block = src[idx:idx + 800]
    assert "process_short_strategy(" in block
    assert "extended_hours=extended_hours" in block
    # The old guard comment + early return must be gone
    assert "cover fills would be brutal" not in block


# --------- 8. process_short_strategy signature pin ---------

def test_process_short_strategy_accepts_extended_hours_kwarg(monkeypatch):
    """API pin: process_short_strategy(...) must accept extended_hours
    as a keyword argument. Any refactor that removes the parameter
    breaks the whole pt.28 path."""
    cs = _reload(monkeypatch)
    import inspect
    sig = inspect.signature(cs.process_short_strategy)
    assert "extended_hours" in sig.parameters
    # Default must be False so all existing call sites keep the
    # regular-hours behavior.
    assert sig.parameters["extended_hours"].default is False


# --------- 9. AH mode respects existing live cover (no churn) ---------

def test_ah_leaves_live_cover_alone(monkeypatch):
    """If cover_order_id is set AND exists in Alpaca's open-orders list,
    AH mode must NOT re-place, NOT tighten, NOT delete. Just persist
    state and exit cleanly."""
    cs = _reload(monkeypatch)
    placed = []
    patched = []

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
        return {"id": "SHOULD_NOT_HAPPEN", "status": "new"}

    def _patch(user, url, data):
        patched.append((url, data))
        return {"id": "SHOULD_NOT_HAPPEN"}

    state = _soxl_state(cover_order_id="live-id")
    state["current_stop_price"] = 135.30
    strat = _soxl_strat(state)
    _call_process_short(
        cs, state, strat,
        {"get": _get, "post": _post, "patch": _patch},
        extended_hours=True,
    )

    assert not placed, f"AH + live cover must not re-place. Got: {placed}"
    assert not patched, f"AH + live cover must not PATCH. Got: {patched}"
    assert state["cover_order_id"] == "live-id"


# --------- 10. AH mode still triggers reset when cover_order_id stale ---------

def test_ah_resets_and_replaces_stale_cover_id(monkeypatch):
    """Pt.24/25/26 liveness checks (order-by-id + open-orders cross-
    check) must run in AH mode too — otherwise pt.28's "place if
    cover_order_id is None" never fires for the exact case that
    motivated pt.28 (SOXL had a stale id pre-pt.26)."""
    cs = _reload(monkeypatch)
    placed = []

    def _get(user, url):
        if "/trades/latest" in url:
            return {"trade": {"p": 128.86}}
        if "/orders/ghost-id" in url:
            return {"error": "404 order not found"}
        if "/orders?status=open" in url:
            return []
        return {}

    def _post(user, url, data):
        placed.append(data)
        return {"id": "fresh-stop-id", "status": "new"}

    state = _soxl_state(cover_order_id="ghost-id")
    strat = _soxl_strat(state)
    _call_process_short(cs, state, strat,
                        {"get": _get, "post": _post},
                        extended_hours=True)

    cover_stops = [p for p in placed
                   if p.get("side") == "buy" and p.get("type") == "stop"]
    assert cover_stops, (
        "AH mode must run the pt.24/25/26 liveness check and then "
        "pt.28's placement path. Got placements: {}".format(placed))
    assert state["cover_order_id"] == "fresh-stop-id"
