#!/usr/bin/env python3
"""
smart_orders.py — Limit-order placement with smart pricing + market fallback.

Round-11 expansion (item 3). Replaces the auto-deployer's market orders
with limit-at-mid with a 90s timeout, falling back to market if unfilled.
For paper trading slippage is irrelevant; for real money this saves
0.1-0.5% per round-trip — meaningful at scale.

Strategy:
  1. Fetch current bid/ask quote
  2. Compute limit price:
       BUY:  bid + 0.4 × spread (slightly above bid, pulls some liquidity)
       SELL: ask - 0.4 × spread (slightly below ask)
     Caps the spread we'll cross at 0.5% — wider spreads mean we'd
     overpay; just go market.
  3. Place limit order with client_order_id for idempotency
  4. Poll order status; after timeout_sec, cancel + place market

Public API:

    place_smart_buy(api_get, api_post, api_delete, api_endpoint, data_endpoint,
                     symbol, qty, headers,
                     timeout_sec=90, max_spread_pct=0.005,
                     client_order_id=None) -> dict
        Returns the final order dict (limit if filled, market if fallback).
        Raises on API errors; caller handles.

    place_smart_sell(...) -> dict  (same args)

    Both functions are HTTP-API agnostic — caller passes lambdas for
    GET/POST/DELETE so the same code works in cloud_scheduler (using
    user_api_get) and the dashboard handlers (using self.user_api_get).
"""
from __future__ import annotations
import time
import uuid


def _get_quote(api_get, data_endpoint, symbol, headers=None):
    """Fetch latest bid/ask from Alpaca data endpoint. Returns
    {bid, ask, mid, spread_pct} or None on error."""
    try:
        url = f"{data_endpoint}/stocks/{symbol}/quotes/latest?feed=iex"
        resp = api_get(url, headers=headers) if headers else api_get(url)
        q = (resp or {}).get("quote", {})
        bid = float(q.get("bp") or 0)
        ask = float(q.get("ap") or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        return {"bid": bid, "ask": ask, "mid": mid, "spread_pct": spread_pct}
    except Exception:
        return None


def _compute_limit_price(quote, side, aggression=0.4):
    """Where to place the limit. aggression 0..1: 0=passive (at bid for
    buy, at ask for sell), 1=aggressive (at ask for buy, at bid for
    sell). 0.4 = slightly aggressive — captures most fills without
    overpaying."""
    bid, ask = quote["bid"], quote["ask"]
    spread = ask - bid
    if side == "buy":
        return round(bid + aggression * spread, 2)
    else:
        return round(ask - aggression * spread, 2)


def _poll_order_filled(api_get, api_endpoint, order_id, timeout_sec, poll_interval=3):
    """Poll order status. Returns the final order dict OR None on
    timeout. Considers 'filled' and 'partially_filled' as success."""
    deadline = time.time() + timeout_sec
    last_status = None
    while time.time() < deadline:
        try:
            order = api_get(f"{api_endpoint}/orders/{order_id}")
            if not isinstance(order, dict):
                time.sleep(poll_interval)
                continue
            status = order.get("status", "")
            last_status = status
            if status in ("filled", "partially_filled"):
                return order
            if status in ("canceled", "rejected", "expired"):
                return order
        except Exception:
            pass
        time.sleep(poll_interval)
    return {"status": last_status or "timeout", "id": order_id, "_smart_timeout": True}


def place_smart_buy(api_get, api_post, api_delete,
                     api_endpoint, data_endpoint,
                     symbol, qty, headers=None,
                     timeout_sec=90, max_spread_pct=0.005,
                     client_order_id=None):
    """Place a limit buy at bid+0.4×spread; if not filled in
    `timeout_sec`, cancel and fall back to a market order.

    Returns the FINAL order dict (filled limit OR market fallback).
    Raises ValueError on bad input. Logs decisions via print().
    """
    if qty <= 0:
        raise ValueError(f"qty must be > 0, got {qty}")
    coid = client_order_id or f"smart-buy-{symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    # 1. Quote check
    quote = _get_quote(api_get, data_endpoint, symbol, headers)
    if not quote:
        print(f"[smart_orders] {symbol}: no quote → market order")
        return _market_order(api_post, api_endpoint, symbol, qty, "buy", coid)

    # 2. Spread check — wide spread means limit at mid would still be
    # paying near ask, so just go market.
    if quote["spread_pct"] > max_spread_pct:
        print(f"[smart_orders] {symbol}: spread {quote['spread_pct']*100:.2f}% > "
              f"{max_spread_pct*100:.2f}% → market order")
        return _market_order(api_post, api_endpoint, symbol, qty, "buy", coid)

    # 3. Place limit
    limit_price = _compute_limit_price(quote, "buy")
    print(f"[smart_orders] {symbol}: BUY {qty} @ ${limit_price} limit "
          f"(bid ${quote['bid']:.2f} / ask ${quote['ask']:.2f})")
    order_body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "limit_price": str(limit_price),
        "time_in_force": "day",
        "client_order_id": coid,
    }
    placed = api_post(f"{api_endpoint}/orders", body=order_body)
    if not isinstance(placed, dict) or "id" not in placed:
        print(f"[smart_orders] {symbol}: limit place failed ({placed}) → market fallback")
        return _market_order(api_post, api_endpoint, symbol, qty, "buy", coid + "-mkt")

    # 4. Poll for fill
    final = _poll_order_filled(api_get, api_endpoint, placed["id"], timeout_sec)
    if final.get("status") in ("filled", "partially_filled"):
        print(f"[smart_orders] {symbol}: limit FILLED at ${final.get('filled_avg_price', limit_price)}")
        return final

    # 5. Timeout — cancel + market fallback. Market for REMAINING qty
    # (in case limit got partial fill).
    try:
        api_delete(f"{api_endpoint}/orders/{placed['id']}")
    except Exception as e:
        print(f"[smart_orders] {symbol}: cancel after timeout failed: {e}")

    # Re-check filled qty
    try:
        check = api_get(f"{api_endpoint}/orders/{placed['id']}")
        filled_so_far = int(float(check.get("filled_qty") or 0))
    except Exception:
        filled_so_far = 0
    remaining = qty - filled_so_far
    if remaining <= 0:
        print(f"[smart_orders] {symbol}: limit fully filled before cancel landed")
        return check
    print(f"[smart_orders] {symbol}: timeout after {timeout_sec}s, market for {remaining} shares")
    market_result = _market_order(api_post, api_endpoint, symbol, remaining,
                                    "buy", coid + "-mkt")
    market_result["_smart_partial_limit"] = filled_so_far
    return market_result


def place_smart_sell(api_get, api_post, api_delete,
                      api_endpoint, data_endpoint,
                      symbol, qty, headers=None,
                      timeout_sec=90, max_spread_pct=0.005,
                      client_order_id=None):
    """Mirror of place_smart_buy. Place limit sell at ask-0.4×spread,
    fallback to market on timeout."""
    if qty <= 0:
        raise ValueError(f"qty must be > 0, got {qty}")
    coid = client_order_id or f"smart-sell-{symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    quote = _get_quote(api_get, data_endpoint, symbol, headers)
    if not quote:
        return _market_order(api_post, api_endpoint, symbol, qty, "sell", coid)

    if quote["spread_pct"] > max_spread_pct:
        return _market_order(api_post, api_endpoint, symbol, qty, "sell", coid)

    limit_price = _compute_limit_price(quote, "sell")
    print(f"[smart_orders] {symbol}: SELL {qty} @ ${limit_price} limit")
    order_body = {
        "symbol": symbol, "qty": str(qty), "side": "sell",
        "type": "limit", "limit_price": str(limit_price),
        "time_in_force": "day", "client_order_id": coid,
    }
    placed = api_post(f"{api_endpoint}/orders", body=order_body)
    if not isinstance(placed, dict) or "id" not in placed:
        return _market_order(api_post, api_endpoint, symbol, qty, "sell", coid + "-mkt")

    final = _poll_order_filled(api_get, api_endpoint, placed["id"], timeout_sec)
    if final.get("status") in ("filled", "partially_filled"):
        return final
    try:
        api_delete(f"{api_endpoint}/orders/{placed['id']}")
    except Exception:
        pass
    try:
        check = api_get(f"{api_endpoint}/orders/{placed['id']}")
        filled_so_far = int(float(check.get("filled_qty") or 0))
    except Exception:
        filled_so_far = 0
    remaining = qty - filled_so_far
    if remaining <= 0:
        return check
    market_result = _market_order(api_post, api_endpoint, symbol, remaining,
                                    "sell", coid + "-mkt")
    market_result["_smart_partial_limit"] = filled_so_far
    return market_result


def _market_order(api_post, api_endpoint, symbol, qty, side, coid):
    return api_post(f"{api_endpoint}/orders", body={
        "symbol": symbol, "qty": str(qty), "side": side,
        "type": "market", "time_in_force": "day",
        "client_order_id": coid,
    })


if __name__ == "__main__":
    print("smart_orders module — test by integration in cloud_scheduler")
