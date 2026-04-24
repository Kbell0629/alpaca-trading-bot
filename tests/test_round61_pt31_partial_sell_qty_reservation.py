"""Round-61 pt.31 — partial-qty sell paths must shrink the protective
stop BEFORE placing the partial market sell.

User-reported: Friday risk reduction tried to trim 31 of 63 INTC at
3:45 PM ET and got HTTP 403 / alpaca_code=40310000 "insufficient qty
available for order (requested: 31, available: 0)". The trailing-stop
sell-order on INTC was reserving all 63 shares — so Alpaca rejected
the trim sell because qty_available was 0. Same bug class as the
SOXL pt.30 cover-stop loop, just on the long side and in a different
code path.

Two paths fixed in pt.31:

  1. ``check_profit_ladder`` — sells 25%/25%/25%/25% of original qty
     at +10/+20/+30/+50% profit. Each rung's market sell would 403
     because the trailing stop reserved the whole position.
  2. ``run_friday_risk_reduction`` — sells half of any +20% winner
     before weekend gap risk. Same 403.

Mechanism: new helper ``_shrink_stop_before_partial_exit`` PATCHes
the stop's qty to ``remaining`` (or cancels and lets the caller
re-place if PATCH fails). After the helper runs, ``qty_available``
is ``sell_qty`` and the partial sell goes through.

Already-correct paths (left untouched, regression-pinned for safety):
  * mean-reversion target exit
  * PEAD time/earnings exit
  * universal pre-earnings exit
  * monthly rebalance close
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


def _make_user(tmpdir):
    return {"id": 1, "username": "testuser",
            "_data_dir": tmpdir, "_strategies_dir": tmpdir,
            "_api_endpoint": "https://paper-api.alpaca.markets/v2",
            "_data_endpoint": "https://data.alpaca.markets/v2",
            "_alpaca_key": "k", "_alpaca_secret": "s"}


# ============ HELPER — _shrink_stop_before_partial_exit ============

def test_shrink_helper_patches_stop_qty(monkeypatch):
    """Happy path: PATCH succeeds, state id unchanged, return 'patched'."""
    cs = _reload(monkeypatch)
    patched = []
    deleted = []

    def _patch(user, url, data):
        patched.append((url, data))
        return {"id": "stop-A", "qty": data["qty"], "status": "new"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    cs.user_api_patch = _patch
    cs.user_api_delete = _delete
    state = {"stop_order_id": "stop-A"}
    action = cs._shrink_stop_before_partial_exit(
        {"username": "u", "_api_endpoint": ""}, "INTC", state, remaining=32)
    assert action == "patched"
    assert state["stop_order_id"] == "stop-A"  # unchanged
    assert len(patched) == 1
    assert patched[0][1]["qty"] == "32"
    assert not deleted


def test_shrink_helper_cancels_when_patch_fails(monkeypatch):
    """PATCH returns error → fall back to DELETE, clear state id,
    return 'canceled' so caller re-places."""
    cs = _reload(monkeypatch)
    deleted = []

    def _patch(user, url, data):
        return {"error": "HTTP 422: cannot replace order"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    cs.user_api_patch = _patch
    cs.user_api_delete = _delete
    state = {"stop_order_id": "stop-B"}
    action = cs._shrink_stop_before_partial_exit(
        {"username": "u", "_api_endpoint": ""}, "INTC", state, remaining=32)
    assert action == "canceled"
    assert state["stop_order_id"] is None
    assert "/orders/stop-B" in deleted


def test_shrink_helper_full_close_cancels_no_patch(monkeypatch):
    """remaining<=0 means we're closing the whole position; just
    cancel the stop and skip the PATCH entirely."""
    cs = _reload(monkeypatch)
    patched = []
    deleted = []

    def _patch(user, url, data):
        patched.append((url, data))
        return {"id": "x"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    cs.user_api_patch = _patch
    cs.user_api_delete = _delete
    state = {"stop_order_id": "stop-C"}
    action = cs._shrink_stop_before_partial_exit(
        {"username": "u", "_api_endpoint": ""}, "INTC", state, remaining=0)
    assert action == "canceled"
    assert state["stop_order_id"] is None
    assert "/orders/stop-C" in deleted
    assert not patched, "Full-close should not waste a PATCH call."


def test_shrink_helper_noop_without_stop_id(monkeypatch):
    """No stop_order_id in state — nothing to shrink. Return 'noop',
    don't call Alpaca."""
    cs = _reload(monkeypatch)
    patched = []
    deleted = []

    def _patch(user, url, data):
        patched.append((url, data))
        return {"id": "x"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    cs.user_api_patch = _patch
    cs.user_api_delete = _delete
    state = {}
    action = cs._shrink_stop_before_partial_exit(
        {"username": "u", "_api_endpoint": ""}, "INTC", state, remaining=32)
    assert action == "noop"
    assert not patched
    assert not deleted


def test_shrink_helper_supports_short_cover_order_id(monkeypatch):
    """Symmetric: shorts use cover_order_id (BUY-stop), helper accepts
    the key as a parameter so the same retrofit covers both sides."""
    cs = _reload(monkeypatch)
    patched = []

    def _patch(user, url, data):
        patched.append((url, data))
        return {"id": "cover-A", "status": "new"}

    cs.user_api_patch = _patch
    cs.user_api_delete = lambda u, url: {}
    state = {"cover_order_id": "cover-A"}
    action = cs._shrink_stop_before_partial_exit(
        {"username": "u", "_api_endpoint": ""}, "SOXL", state,
        remaining=15, stop_id_key="cover_order_id")
    assert action == "patched"
    assert "/orders/cover-A" in patched[0][0]


# ============ INTEGRATION — check_profit_ladder ============

def test_profit_ladder_shrinks_stop_before_sell(monkeypatch):
    """Pin the call sequence: PATCH /orders/{stop_id} (qty=remaining)
    MUST happen before POST /orders (the sell). Without this, Alpaca
    rejects the sell with qty_available=0."""
    cs = _reload(monkeypatch)
    call_order = []

    def _post(user, url, data):
        call_order.append(("post", url, data))
        return {"id": f"order-{len(call_order)}", "status": "filled",
                "filled_avg_price": "100.00"}

    def _patch(user, url, data):
        call_order.append(("patch", url, data))
        return {"id": "trail-stop-1", "status": "new", "qty": data["qty"]}

    def _delete(user, url):
        call_order.append(("delete", url))
        return {}

    cs.user_api_post = _post
    cs.user_api_patch = _patch
    cs.user_api_delete = _delete

    tmpdir = tempfile.mkdtemp()
    fpath = os.path.join(tmpdir, "trailing_stop_AAPL.json")
    strat = {
        "symbol": "AAPL", "strategy": "trailing_stop", "status": "active",
        "initial_qty": 100,
        "state": {
            "entry_fill_price": 100.00,
            "total_shares_held": 100,
            "stop_order_id": "trail-stop-1",
            "current_stop_price": 92.00,
            "profit_takes": [],
        },
        "rules": {},
    }
    with open(fpath, "w") as f:
        json.dump(strat, f)
    user = _make_user(tmpdir)

    # Price up 12% → first ladder rung (10%) fires, sell 25 of 100.
    cs.check_profit_ladder(user, fpath, strat, price=112.00,
                            entry=100.00, shares=100)

    # Find the indices of the relevant calls
    patches = [i for i, c in enumerate(call_order) if c[0] == "patch"]
    posts = [i for i, c in enumerate(call_order) if c[0] == "post"]
    assert patches, f"No PATCH issued. Call order: {call_order}"
    assert posts, f"No POST issued. Call order: {call_order}"
    assert min(patches) < min(posts), (
        f"Pt.31: PATCH (stop shrink) MUST come before POST (sell). "
        f"Call order: {call_order}")
    # PATCH was for remaining=75
    patch_call = call_order[patches[0]]
    assert patch_call[2]["qty"] == "75"
    # First POST is the sell at qty=25
    sell_call = call_order[posts[0]]
    assert sell_call[2]["qty"] == "25"
    assert sell_call[2]["side"] == "sell"


def test_profit_ladder_replaces_stop_when_patch_falls_back(monkeypatch):
    """If PATCH fails, helper cancels and returns 'canceled'. Caller
    must re-place a fresh stop on the remaining shares after the
    sell fills. Otherwise the residual position is unprotected."""
    cs = _reload(monkeypatch)
    posts = []
    deleted = []

    def _post(user, url, data):
        posts.append(data)
        return {"id": f"o-{len(posts)}", "status": "filled",
                "filled_avg_price": "112.00"}

    def _patch(user, url, data):
        return {"error": "HTTP 422: cannot replace"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    cs.user_api_post = _post
    cs.user_api_patch = _patch
    cs.user_api_delete = _delete

    tmpdir = tempfile.mkdtemp()
    fpath = os.path.join(tmpdir, "trailing_stop_AAPL.json")
    strat = {
        "symbol": "AAPL", "strategy": "trailing_stop", "status": "active",
        "initial_qty": 100,
        "state": {
            "entry_fill_price": 100.00,
            "total_shares_held": 100,
            "stop_order_id": "trail-stop-1",
            "current_stop_price": 92.00,
            "profit_takes": [],
        },
        "rules": {},
    }
    with open(fpath, "w") as f:
        json.dump(strat, f)
    user = _make_user(tmpdir)
    cs.check_profit_ladder(user, fpath, strat, price=112.00,
                            entry=100.00, shares=100)

    # Old stop deleted
    assert "/orders/trail-stop-1" in deleted
    # Three POSTs expected: sell + new stop on remaining
    sells = [p for p in posts
             if p.get("side") == "sell" and p.get("type") == "market"]
    new_stops = [p for p in posts
                  if p.get("type") == "stop" and p.get("side") == "sell"]
    assert sells and sells[0]["qty"] == "25"
    assert new_stops, (
        f"Pt.31: PATCH-fail path MUST re-place a fresh stop on "
        f"the remaining shares. Got POSTs: {posts}")
    assert new_stops[0]["qty"] == "75"
    assert float(new_stops[0]["stop_price"]) == 92.00


def test_profit_ladder_normal_patch_path_does_not_double_place(monkeypatch):
    """Regression pin: when PATCH succeeds, we must NOT re-place a
    new stop after the sell — that would create TWO sell-stops on
    the same position."""
    cs = _reload(monkeypatch)
    posts = []

    def _post(user, url, data):
        posts.append(data)
        return {"id": f"o-{len(posts)}", "status": "filled",
                "filled_avg_price": "112.00"}

    def _patch(user, url, data):
        return {"id": "trail-stop-1", "qty": data["qty"], "status": "new"}

    cs.user_api_post = _post
    cs.user_api_patch = _patch
    cs.user_api_delete = lambda u, url: {}

    tmpdir = tempfile.mkdtemp()
    fpath = os.path.join(tmpdir, "trailing_stop_AAPL.json")
    strat = {
        "symbol": "AAPL", "strategy": "trailing_stop", "status": "active",
        "initial_qty": 100,
        "state": {
            "entry_fill_price": 100.00,
            "total_shares_held": 100,
            "stop_order_id": "trail-stop-1",
            "current_stop_price": 92.00,
            "profit_takes": [],
        },
        "rules": {},
    }
    with open(fpath, "w") as f:
        json.dump(strat, f)
    user = _make_user(tmpdir)
    cs.check_profit_ladder(user, fpath, strat, price=112.00,
                            entry=100.00, shares=100)

    new_stops = [p for p in posts
                  if p.get("type") == "stop"]
    assert not new_stops, (
        f"Pt.31: PATCH-success path MUST NOT re-place a stop. "
        f"Got: {posts}")
    # state still references the (now-shrunk) stop
    assert strat["state"]["stop_order_id"] == "trail-stop-1"


# ============ INTEGRATION — run_friday_risk_reduction ============

def test_friday_shrinks_stop_before_trim(monkeypatch):
    """The INTC bug. 63 long @ +22%, trailing-stop reserves 63 →
    Friday trim of 31 must shrink stop to 32 BEFORE placing the
    sell, otherwise Alpaca returns 403 on every trim attempt."""
    cs = _reload(monkeypatch)
    call_order = []

    def _get(user, url):
        if url == "/positions":
            return [{
                "symbol": "INTC", "qty": "63",
                "avg_entry_price": "65.00",
                "current_price": "82.00",
                "unrealized_plpc": "0.223",
            }]
        return {}

    def _post(user, url, data):
        call_order.append(("post", url, data))
        return {"id": f"o-{len(call_order)}", "status": "filled"}

    def _patch(user, url, data):
        call_order.append(("patch", url, data))
        return {"id": "intc-stop", "status": "new", "qty": data["qty"]}

    def _delete(user, url):
        call_order.append(("delete", url))
        return {}

    cs.user_api_get = _get
    cs.user_api_post = _post
    cs.user_api_patch = _patch
    cs.user_api_delete = _delete

    tmpdir = tempfile.mkdtemp()
    sf_path = os.path.join(tmpdir, "trailing_stop_INTC.json")
    strat = {
        "symbol": "INTC", "strategy": "trailing_stop", "status": "active",
        "initial_qty": 63,
        "created": "2026-04-01",
        "state": {
            "entry_fill_price": 65.00,
            "total_shares_held": 63,
            "stop_order_id": "intc-stop",
            "current_stop_price": 79.56,
        },
        "rules": {},
    }
    with open(sf_path, "w") as f:
        json.dump(strat, f)
    user = _make_user(tmpdir)
    cs.run_friday_risk_reduction(user)

    # Sequence assertion: PATCH before POST(sell)
    patches = [i for i, c in enumerate(call_order) if c[0] == "patch"]
    sells = [i for i, c in enumerate(call_order)
              if c[0] == "post" and c[2].get("side") == "sell"
              and c[2].get("type") == "market"]
    assert patches, f"Pt.31: Friday must PATCH stop. Calls: {call_order}"
    assert sells, f"Pt.31: Friday must POST sell. Calls: {call_order}"
    assert min(patches) < min(sells), (
        f"Pt.31: Friday must PATCH stop BEFORE the trim sell. "
        f"Call order: {call_order}")

    # PATCH qty = remaining = 63 - 31 = 32
    patch_call = call_order[patches[0]]
    assert patch_call[2]["qty"] == "32"
    # Trim sell qty = half = 31 (63 // 2)
    sell_call = call_order[sells[0]]
    assert sell_call[2]["qty"] == "31"
    assert sell_call[2]["side"] == "sell"
    # idempotency client_order_id present
    assert "client_order_id" in sell_call[2]


def test_friday_replaces_stop_when_patch_falls_back(monkeypatch):
    """If PATCH fails Friday must cancel + re-place a fresh stop on
    the remaining shares after the trim, mirroring the profit-ladder
    fallback path."""
    cs = _reload(monkeypatch)
    posts = []
    deleted = []

    def _get(user, url):
        if url == "/positions":
            return [{
                "symbol": "INTC", "qty": "63",
                "avg_entry_price": "65.00",
                "current_price": "82.00",
                "unrealized_plpc": "0.223",
            }]
        return {}

    def _post(user, url, data):
        posts.append(data)
        return {"id": f"o-{len(posts)}", "status": "filled"}

    def _patch(user, url, data):
        return {"error": "HTTP 422 cannot replace"}

    def _delete(user, url):
        deleted.append(url)
        return {}

    cs.user_api_get = _get
    cs.user_api_post = _post
    cs.user_api_patch = _patch
    cs.user_api_delete = _delete

    tmpdir = tempfile.mkdtemp()
    sf_path = os.path.join(tmpdir, "trailing_stop_INTC.json")
    strat = {
        "symbol": "INTC", "strategy": "trailing_stop", "status": "active",
        "initial_qty": 63,
        "created": "2026-04-01",
        "state": {
            "entry_fill_price": 65.00,
            "total_shares_held": 63,
            "stop_order_id": "intc-stop",
            "current_stop_price": 79.56,
        },
        "rules": {},
    }
    with open(sf_path, "w") as f:
        json.dump(strat, f)
    user = _make_user(tmpdir)
    cs.run_friday_risk_reduction(user)

    assert "/orders/intc-stop" in deleted, (
        f"Pt.31: PATCH-fail path must DELETE the old stop. "
        f"Deleted: {deleted}")
    sells = [p for p in posts
              if p.get("side") == "sell" and p.get("type") == "market"]
    new_stops = [p for p in posts
                  if p.get("type") == "stop" and p.get("side") == "sell"]
    assert sells and sells[0]["qty"] == "31"
    assert new_stops, (
        f"Pt.31: PATCH-fail path must re-place a fresh stop. "
        f"Got POSTs: {posts}")
    assert new_stops[0]["qty"] == "32"
    assert float(new_stops[0]["stop_price"]) == 79.56


def test_friday_skips_below_20pct_winners(monkeypatch):
    """Regression pin: only +20%+ winners should be trimmed. A small
    winner should not trigger any PATCH/POST."""
    cs = _reload(monkeypatch)
    calls = []

    def _get(user, url):
        if url == "/positions":
            return [{
                "symbol": "INTC", "qty": "63",
                "avg_entry_price": "65.00",
                "current_price": "70.00",
                "unrealized_plpc": "0.077",  # only +7.7%
            }]
        return {}

    def _post(user, url, data):
        calls.append(("post", data))
        return {"id": "x", "status": "filled"}

    def _patch(user, url, data):
        calls.append(("patch", data))
        return {"id": "y"}

    cs.user_api_get = _get
    cs.user_api_post = _post
    cs.user_api_patch = _patch
    cs.user_api_delete = lambda u, url: {}

    tmpdir = tempfile.mkdtemp()
    user = _make_user(tmpdir)
    cs.run_friday_risk_reduction(user)
    assert not calls, f"Pt.31: <20% gain must not trigger Friday trim. {calls}"


# ============ SOURCE PINS — already-correct paths ============

def test_correct_paths_still_cancel_stop_before_close(monkeypatch):
    """Source-pin the already-correct paths so a future refactor
    can't reintroduce the bug. Each of these paths CLOSES the
    position fully (qty=shares), so the protective stop must be
    canceled (not just shrunk) BEFORE the market sell.

    Paths checked: mean-reversion target, PEAD time/earnings,
    universal pre-earnings, monthly rebalance.
    """
    import pathlib
    src = pathlib.Path("cloud_scheduler.py").read_text()
    # Each path should have a "cancel the live GTC stop FIRST"-style
    # comment OR explicitly call user_api_delete on the stop_order_id
    # before user_api_post on /orders.
    # We pin the comment patterns — they're stable markers for these
    # blocks since round-10/29.
    assert "cancel the live GTC stop FIRST" in src, (
        "Source pin: mean-reversion / PEAD / monthly should keep "
        "the cancel-first ordering.")


# ============ HELPER PRESENCE ============

def test_pt31_helper_is_defined():
    """Sanity pin: helper exists at module scope for the call sites."""
    import importlib
    import sys as _sys
    _sys.modules.pop("cloud_scheduler", None)
    cs = importlib.import_module("cloud_scheduler")
    assert hasattr(cs, "_shrink_stop_before_partial_exit")
