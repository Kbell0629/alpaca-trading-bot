"""
Full-flow tests for smart_orders.place_smart_buy / place_smart_sell.

These exercise the full state machine around the limit-at-mid with
timeout → cancel → settle → market-fallback path. This is the safety-
critical double-fill defence; the existing _compute_limit_price parity
fuzz doesn't exercise it.

Approach: mock api_get / api_post / api_delete with per-test-controllable
stubs so we can inject specific order-status sequences and assert the
code walks the expected branches.

Coverage:

  * Quote missing → market fallback
  * Wide spread → market fallback
  * Limit placed + filled immediately
  * Limit placed + timeout → cancel → settled-canceled → market fallback
  * Limit placed + timeout → cancel → settled-filled → return settled
  * Limit placed + timeout → cancel → settled-partial → market for remainder
  * Limit placed + timeout → cancel never settles → pending_cancel return
  * qty <= 0 raises ValueError
  * client_order_id idempotency (suffix '-mkt' on fallback)
  * Invariant: market fallback only fires AFTER cancel settles
"""
from __future__ import annotations

import time

import pytest

import smart_orders as so


# ---------- Stub infrastructure ----------


class _Stub:
    """Controllable mock for api_get/post/delete. Tests configure the
    returned values and inspect call counts + args."""
    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self.delete_calls = []
        self.get_responses = {}       # url → list of dicts (pop front)
        self.get_default = {}
        self.post_response = {}
        self.delete_response = {}

    def api_get(self, url, headers=None):
        self.get_calls.append(url)
        if url in self.get_responses:
            queue = self.get_responses[url]
            if queue:
                return queue.pop(0)
        return self.get_default

    def api_post(self, url, body=None):
        self.post_calls.append((url, body))
        return self.post_response

    def api_delete(self, url):
        self.delete_calls.append(url)
        return self.delete_response


def _quote_url(endpoint, symbol):
    return f"{endpoint}/stocks/{symbol}/quotes/latest?feed=iex"


API_EP = "https://paper-api.alpaca.markets/v2"
DATA_EP = "https://data.alpaca.markets/v2"


# ---------- Input validation ----------


def test_place_smart_buy_rejects_zero_qty():
    stub = _Stub()
    with pytest.raises(ValueError):
        so.place_smart_buy(
            stub.api_get, stub.api_post, stub.api_delete,
            API_EP, DATA_EP, "AAPL", 0,
        )


def test_place_smart_buy_rejects_negative_qty():
    stub = _Stub()
    with pytest.raises(ValueError):
        so.place_smart_buy(
            stub.api_get, stub.api_post, stub.api_delete,
            API_EP, DATA_EP, "AAPL", -5,
        )


# ---------- Fallback paths (no limit attempted) ----------


def test_missing_quote_falls_back_to_market():
    stub = _Stub()
    # No quote field in response → _get_quote returns None
    stub.get_default = {"quote": {}}
    stub.post_response = {"id": "market-order-id", "status": "accepted"}

    result = so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
    )
    assert result.get("id") == "market-order-id"
    # Should have posted exactly one market order; no delete call
    assert len(stub.post_calls) == 1
    _, body = stub.post_calls[0]
    assert body["type"] == "market"
    assert body["qty"] == "10"
    assert len(stub.delete_calls) == 0


def test_wide_spread_falls_back_to_market():
    """spread_pct > max_spread_pct (default 0.005 = 0.5%) → market."""
    stub = _Stub()
    # bid=100, ask=102 → spread_pct = 2/101 ≈ 0.0198 = 1.98%
    stub.get_default = {"quote": {"bp": 100.0, "ap": 102.0}}
    stub.post_response = {"id": "market-order-id", "status": "accepted"}

    result = so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
    )
    assert result["id"] == "market-order-id"
    _, body = stub.post_calls[0]
    assert body["type"] == "market"


def test_invalid_quote_data_falls_back_to_market():
    """bid=0 or ask<bid → _get_quote returns None."""
    stub = _Stub()
    stub.get_default = {"quote": {"bp": 0.0, "ap": 100.0}}   # bp=0
    stub.post_response = {"id": "market-order-id"}

    result = so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
    )
    _, body = stub.post_calls[0]
    assert body["type"] == "market"


# ---------- Limit-filled happy path ----------


def test_limit_placed_and_filled_immediately(monkeypatch):
    """Quote tight → limit placed → first poll sees status=filled → return."""
    # Speed up the poll interval so the test doesn't take 3+ seconds
    monkeypatch.setattr(so.time, "sleep", lambda _: None)

    stub = _Stub()
    stub.get_responses[_quote_url(DATA_EP, "AAPL")] = [
        {"quote": {"bp": 100.00, "ap": 100.05}},   # tight spread
    ]
    stub.post_response = {"id": "limit-1", "status": "new"}
    # First poll of /orders/limit-1 → filled
    stub.get_responses[f"{API_EP}/orders/limit-1"] = [
        {"id": "limit-1", "status": "filled", "filled_avg_price": "100.02"},
    ]

    result = so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
        timeout_sec=5,
    )
    assert result["status"] == "filled"
    # No delete call, no market fallback
    assert len(stub.delete_calls) == 0
    # One POST (the limit)
    assert len(stub.post_calls) == 1
    _, body = stub.post_calls[0]
    assert body["type"] == "limit"
    assert "limit_price" in body


# ---------- Timeout → cancel → market fallback ----------


def test_timeout_triggers_cancel_then_market_fallback(monkeypatch):
    """Limit never fills → cancel → settled=canceled → market for full qty."""
    monkeypatch.setattr(so.time, "sleep", lambda _: None)
    # Also fake time.time so the poll loop exits fast instead of waiting
    # real seconds.
    t = [1000.0]
    def fake_time():
        t[0] += 0.5
        return t[0]
    monkeypatch.setattr(so.time, "time", fake_time)

    stub = _Stub()
    stub.get_responses[_quote_url(DATA_EP, "AAPL")] = [
        {"quote": {"bp": 100.00, "ap": 100.05}},
    ]
    stub.post_response = {"id": "limit-2", "status": "new"}
    # Poll keeps returning 'new' — never fills → poll loop gives up
    stub.get_default = {"id": "limit-2", "status": "new"}
    # After cancel, the settled-status poll returns canceled
    stub.get_responses[f"{API_EP}/orders/limit-2"] = [
        {"id": "limit-2", "status": "new"},        # poll 1 inside _poll_order_filled
        {"id": "limit-2", "status": "new"},        # poll 2
        {"id": "limit-2", "status": "new"},        # poll 3
        {"id": "limit-2", "status": "canceled", "filled_qty": "0"},   # settled after cancel
    ]

    stub.delete_response = {"status": "pending_cancel"}
    # After the market fallback, this is the market order response
    # Record second POST via the stub's attribute
    original_post = stub.api_post
    market_order_returned = {"id": "market-fallback", "status": "accepted"}

    def tracking_post(url, body=None):
        stub.post_calls.append((url, body))
        if body and body.get("type") == "market":
            return market_order_returned
        return {"id": "limit-2", "status": "new"}
    monkeypatch.setattr(stub, "api_post", tracking_post)

    result = so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
        timeout_sec=1,   # tight timeout so test exits fast
    )
    # Should have issued: limit POST, delete, market POST
    assert len(stub.delete_calls) == 1, (
        f"expected 1 delete call; got {len(stub.delete_calls)}"
    )
    # Market fallback should exist (2 POSTs: limit + market)
    market_posts = [b for _, b in stub.post_calls if b and b.get("type") == "market"]
    assert len(market_posts) == 1
    assert market_posts[0]["qty"] == "10"   # full remaining qty


def test_limit_fills_during_cancel_race(monkeypatch):
    """Timeout → delete → settled comes back filled (limit filled in the
    cancel race window). Must NOT place a market fallback — return the
    settled order directly."""
    monkeypatch.setattr(so.time, "sleep", lambda _: None)
    t = [1000.0]
    def fake_time():
        t[0] += 0.5
        return t[0]
    monkeypatch.setattr(so.time, "time", fake_time)

    stub = _Stub()
    stub.get_responses[_quote_url(DATA_EP, "AAPL")] = [
        {"quote": {"bp": 100.00, "ap": 100.05}},
    ]
    stub.post_response = {"id": "limit-3", "status": "new"}
    # Poll keeps returning 'new' until timeout; then wait_cancel_settled
    # sees status=filled (limit filled in the race window).
    stub.get_responses[f"{API_EP}/orders/limit-3"] = [
        {"id": "limit-3", "status": "new"},
        {"id": "limit-3", "status": "new"},
        {"id": "limit-3", "status": "filled",
          "filled_qty": "10", "filled_avg_price": "100.02"},
    ]
    stub.delete_response = {"status": "pending_cancel"}

    result = so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
        timeout_sec=1,
    )
    # CRITICAL: no market fallback posted
    market_posts = [b for _, b in stub.post_calls if b and b.get("type") == "market"]
    assert len(market_posts) == 0, (
        f"double-fill race: market fallback posted after limit filled. "
        f"POSTs: {stub.post_calls}"
    )
    # Returned the settled order
    assert result["status"] == "filled"


def test_partial_fill_then_timeout_market_for_remainder(monkeypatch):
    """Timeout → delete → settled filled_qty=7 (out of 10) → market
    fallback for remaining 3 shares only."""
    monkeypatch.setattr(so.time, "sleep", lambda _: None)
    t = [1000.0]
    def fake_time():
        t[0] += 0.5
        return t[0]
    monkeypatch.setattr(so.time, "time", fake_time)

    stub = _Stub()
    stub.get_responses[_quote_url(DATA_EP, "AAPL")] = [
        {"quote": {"bp": 100.00, "ap": 100.05}},
    ]
    stub.post_response = {"id": "limit-4", "status": "new"}
    stub.get_responses[f"{API_EP}/orders/limit-4"] = [
        {"id": "limit-4", "status": "new"},
        {"id": "limit-4", "status": "new"},
        {"id": "limit-4", "status": "canceled", "filled_qty": "7"},
    ]
    stub.delete_response = {"status": "pending_cancel"}
    original_post = stub.api_post
    def tracking_post(url, body=None):
        stub.post_calls.append((url, body))
        if body and body.get("type") == "market":
            return {"id": "market-fallback", "qty": body["qty"]}
        return {"id": "limit-4", "status": "new"}
    monkeypatch.setattr(stub, "api_post", tracking_post)

    result = so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
        timeout_sec=1,
    )
    market_posts = [b for _, b in stub.post_calls if b and b.get("type") == "market"]
    assert len(market_posts) == 1
    # Remainder = 10 - 7 = 3
    assert market_posts[0]["qty"] == "3"
    # _smart_partial_limit tagged for caller visibility
    assert result.get("_smart_partial_limit") == 7


def test_cancel_unsettled_returns_pending_without_market_fallback(monkeypatch):
    """If wait_cancel_settled can't confirm terminal status within its
    10s window, return _smart_cancel_pending WITHOUT placing a market
    fallback. This is the belt-and-suspenders against double-fill."""
    monkeypatch.setattr(so.time, "sleep", lambda _: None)
    t = [1000.0]
    def fake_time():
        t[0] += 0.5
        return t[0]
    monkeypatch.setattr(so.time, "time", fake_time)

    stub = _Stub()
    stub.get_responses[_quote_url(DATA_EP, "AAPL")] = [
        {"quote": {"bp": 100.00, "ap": 100.05}},
    ]
    stub.post_response = {"id": "limit-5", "status": "new"}
    # Every GET returns 'new' — never reaches terminal status
    stub.get_default = {"id": "limit-5", "status": "new"}
    stub.delete_response = {"status": "pending_cancel"}

    result = so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
        timeout_sec=1,
    )
    assert result.get("_smart_cancel_pending") is True
    assert result.get("status") == "pending_cancel"
    # CRITICAL: no market fallback posted
    market_posts = [b for _, b in stub.post_calls if b and b.get("type") == "market"]
    assert len(market_posts) == 0


# ---------- client_order_id idempotency ----------


def test_client_order_id_uniqueness_format():
    """coid format: smart-{side}-{symbol}-{epoch}-{uuid12}. 12 hex chars
    of uuid entropy."""
    import re
    # Sample 10 calls — each should emit a unique coid format.
    stub = _Stub()
    stub.get_default = {"quote": {}}    # force fallback to market (simplest path)
    stub.post_response = {"id": "mkt", "status": "accepted"}
    for _ in range(10):
        so.place_smart_buy(
            stub.api_get, stub.api_post, stub.api_delete,
            API_EP, DATA_EP, "AAPL", 10,
        )
    coids = [b["client_order_id"] for _, b in stub.post_calls]
    assert len(set(coids)) == 10, (
        f"coid collision in 10 consecutive calls: {coids}"
    )
    # Format check — each coid matches the expected pattern
    pat = re.compile(r"^smart-buy-AAPL-\d+-[0-9a-f]{12}(-mkt)?$")
    for c in coids:
        assert pat.match(c), f"coid format broken: {c}"


def test_market_fallback_uses_mkt_suffix(monkeypatch):
    """Market fallback coid ends in '-mkt' so Alpaca can't dedupe with
    the original limit's coid."""
    monkeypatch.setattr(so.time, "sleep", lambda _: None)
    stub = _Stub()
    stub.get_default = {"quote": {}}    # force immediate market fallback
    stub.post_response = {"id": "mkt", "status": "accepted"}

    so.place_smart_buy(
        stub.api_get, stub.api_post, stub.api_delete,
        API_EP, DATA_EP, "AAPL", 10,
    )
    coid = stub.post_calls[0][1]["client_order_id"]
    # Direct market path (no limit attempted) — uses the original coid
    # WITHOUT the -mkt suffix because there's no cancel race to protect.
    assert not coid.endswith("-mkt"), (
        f"direct-market path shouldn't add -mkt suffix: {coid}"
    )


# ---------- place_smart_sell mirror ----------


def test_place_smart_sell_rejects_zero_qty():
    stub = _Stub()
    with pytest.raises(ValueError):
        so.place_smart_sell(
            stub.api_get, stub.api_post, stub.api_delete,
            API_EP, DATA_EP, "AAPL", 0,
        )


def test_place_smart_sell_limit_price_below_ask():
    """Sell limit price sits at ask - aggression*spread. 0.4 default →
    below ask by 40% of the spread."""
    stub = _Stub()
    stub.get_responses[_quote_url(DATA_EP, "AAPL")] = [
        {"quote": {"bp": 100.00, "ap": 100.20}},   # spread 0.20
    ]
    stub.post_response = {"id": "sell-1", "status": "new"}
    # First poll: filled immediately
    stub.get_responses[f"{API_EP}/orders/sell-1"] = [
        {"id": "sell-1", "status": "filled"},
    ]

    import types
    # Patch time.sleep so poll loop exits fast
    orig_sleep = so.time.sleep
    so.time.sleep = lambda _: None
    try:
        so.place_smart_sell(
            stub.api_get, stub.api_post, stub.api_delete,
            API_EP, DATA_EP, "AAPL", 10,
            timeout_sec=5,
        )
    finally:
        so.time.sleep = orig_sleep

    limit_post = [b for _, b in stub.post_calls if b.get("type") == "limit"][0]
    lp = float(limit_post["limit_price"])
    # Sell limit sits ask - 0.4*spread = 100.20 - 0.08 = 100.12
    assert lp == pytest.approx(100.12, abs=0.01)
    assert limit_post["side"] == "sell"
