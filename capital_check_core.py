"""Round-61 pt.34 — capital_check pure-math core.

Extracted from capital_check.py following the pt.7 pattern
(screener_core.py + scorecard_core.py): pure functions live here,
the subprocess-driven entry point in capital_check.py wraps them
with Alpaca + filesystem I/O.

Why extract:
  * capital_check.py is invoked exclusively as a subprocess from
    cloud_scheduler.run_auto_deployer, so pytest-cov never sees its
    line execution. Before pt.34, every invariant in the
    capital-sustainability math was untested at the unit level.
  * The math IS testable — no networking, no globals — once the
    Alpaca + DATA_DIR access is hoisted into the caller. This file
    does that hoisting.

Contract:
  * Every function in this module is pure given its arguments.
  * No filesystem reads, no environment variable reads, no network.
  * Float-internal is fine on the boundary; the call sites convert
    to JSON-friendly numbers before persisting.

The subprocess entry point in capital_check.py imports from this
module and injects production dependencies (`api_get_with_retry`,
`DATA_DIR`, `os.environ` for `CAPITAL_STATUS_PATH`).
"""
from __future__ import annotations

# Round-15: extracted pure helper so the fallback ladder (live quote →
# position avg cost → $1000/share conservative floor) can be unit-tested
# without needing network or module-level Alpaca endpoints. Security-
# critical: this is the code that prevents silent over-leverage when a
# live-quote fetch fails.
_LAST_RESORT_PRICE_PER_SHARE = 1000.0


def compute_reserved_by_orders(orders, position_avg_cost_by_sym, fetch_last):
    """Return the total dollar-amount reserved by pending BUY orders.
    Arguments are decoupled from Alpaca so tests can inject fakes.

    ``orders``: list of Alpaca order dicts.
    ``position_avg_cost_by_sym``: ``{SYMBOL: avg_entry_price}`` for
        open positions.
    ``fetch_last``: callable(symbol) -> float, returns the latest
        trade price or 0.0 on failure.

    Pricing ladder for orders that don't have an explicit
    limit/stop/notional:
      1. live last-trade quote via ``fetch_last(symbol)``
      2. our own avg entry price for any open position in the same symbol
      3. conservative $1000/share floor — we'd rather refuse an order
         than under-reserve capital and authorise an overleveraged trade.
    """
    reserved = 0.0
    for o in (orders or []):
        if o.get("side") != "buy":
            continue
        try:
            price = float(o.get("limit_price") or o.get("stop_price") or 0)
            qty = float(o.get("qty", 0))
        except (TypeError, ValueError):
            continue
        if price > 0:
            reserved += price * qty
            continue
        try:
            notional = float(o.get("notional") or 0)
        except (TypeError, ValueError):
            notional = 0
        if notional > 0:
            reserved += notional
            continue
        sym = (o.get("symbol") or "").upper()
        last = 0.0
        try:
            last = float(fetch_last(sym) or 0)
        except Exception:
            last = 0.0
        if last > 0:
            px = last
        elif position_avg_cost_by_sym.get(sym, 0) > 0:
            px = position_avg_cost_by_sym[sym]
        else:
            px = _LAST_RESORT_PRICE_PER_SHARE
        reserved += px * qty
    return reserved


def position_avg_cost_map(positions):
    """Build ``{SYMBOL: avg_entry_price}`` from an Alpaca positions
    list. Used by the fallback ladder in
    ``compute_reserved_by_orders``."""
    return {
        (p.get("symbol") or "").upper(): float(p.get("avg_entry_price") or 0)
        for p in (positions or [])
    }


def compute_capital_metrics(account, positions, orders, guardrails,
                              fetch_last, now_iso):
    """Pure version of the capital sustainability check. Caller injects
    Alpaca data + the fetch_last callable so this fn has no side
    effects.

    ``account``: Alpaca /account dict (or {"error": ...}).
    ``positions``: list of Alpaca position dicts.
    ``orders``: list of open Alpaca order dicts.
    ``guardrails``: parsed guardrails.json dict (may be empty).
    ``fetch_last``: callable(symbol) -> float for the price-fallback
        ladder used by ``compute_reserved_by_orders``.
    ``now_iso``: ISO-8601 timestamp for the result["timestamp"] field
        (caller computes via et_time.now_et so this fn stays tz-pure).

    Returns either ``{"error": ...}`` (when account is an error dict)
    or the full status dict matching capital_check.check_capital's
    historical schema. Saving to disk is the caller's job.
    """
    if isinstance(account, dict) and "error" in account:
        return {"error": account["error"]}

    portfolio_value = float(account.get("portfolio_value", 0))
    cash = float(account.get("cash", 0))
    buying_power = float(account.get("buying_power", 0))

    positions = positions if isinstance(positions, list) else []
    orders = orders if isinstance(orders, list) else []

    total_position_value = sum(
        float(p.get("market_value", 0)) for p in positions
    )
    num_positions = len(positions)

    avg_cost_map = position_avg_cost_map(positions)
    reserved_by_orders = compute_reserved_by_orders(
        orders, avg_cost_map, fetch_last
    )

    pct_invested = (
        (total_position_value / portfolio_value * 100)
        if portfolio_value > 0 else 0
    )
    pct_reserved = (
        (reserved_by_orders / portfolio_value * 100)
        if portfolio_value > 0 else 0
    )
    free_cash = cash - reserved_by_orders
    pct_free = (
        (free_cash / portfolio_value * 100)
        if portfolio_value > 0 else 0
    )

    max_positions = guardrails.get("max_positions", 5)
    max_position_pct = guardrails.get("max_position_pct", 0.10)

    # Can we afford another trade?
    min_trade_size = portfolio_value * 0.03  # Minimum 3% position
    can_trade = (free_cash >= min_trade_size
                  and num_positions < max_positions)

    # How many more trades can we afford?
    avg_position_size = portfolio_value * max_position_pct
    additional_trades_possible = (
        int(free_cash / avg_position_size)
        if avg_position_size > 0 else 0
    )
    additional_trades_possible = min(
        additional_trades_possible, max_positions - num_positions
    )

    warnings = _build_warnings(
        pct_invested=pct_invested,
        pct_free=pct_free,
        free_cash=free_cash,
        min_trade_size=min_trade_size,
        num_positions=num_positions,
        max_positions=max_positions,
        reserved_by_orders=reserved_by_orders,
        cash=cash,
        pct_reserved=pct_reserved,
    )
    sustainability = _compute_sustainability(
        pct_invested=pct_invested,
        num_positions=num_positions,
        max_positions=max_positions,
        free_cash=free_cash,
        min_trade_size=min_trade_size,
    )
    recommendation = _build_recommendation(
        sustainability=sustainability,
        additional_trades_possible=additional_trades_possible,
        free_cash=free_cash,
    )

    return {
        "timestamp": now_iso,
        "portfolio_value": portfolio_value,
        "cash": cash,
        "buying_power": buying_power,
        "total_position_value": total_position_value,
        "reserved_by_orders": reserved_by_orders,
        "free_cash": free_cash,
        "pct_invested": round(pct_invested, 1),
        "pct_reserved": round(pct_reserved, 1),
        "pct_free": round(pct_free, 1),
        "num_positions": num_positions,
        "max_positions": max_positions,
        "additional_trades_possible": additional_trades_possible,
        "can_trade": can_trade,
        "sustainability_score": sustainability,
        "warnings": warnings,
        "recommendation": recommendation,
    }


def _build_warnings(pct_invested, pct_free, free_cash, min_trade_size,
                     num_positions, max_positions, reserved_by_orders,
                     cash, pct_reserved):
    """Build the human-readable warnings list. Pure given its args.
    Order matches the legacy capital_check.check_capital so the
    caller's existing log/email format stays stable."""
    warnings = []
    if pct_invested > 80:
        warnings.append(
            f"HIGH EXPOSURE: {pct_invested:.0f}% of portfolio is "
            f"invested. Consider reducing positions."
        )
    elif pct_invested > 60:
        warnings.append(
            f"MODERATE EXPOSURE: {pct_invested:.0f}% invested. "
            f"{pct_free:.0f}% free cash remaining."
        )
    if free_cash < min_trade_size:
        warnings.append(
            f"LOW CAPITAL: Only ${free_cash:,.2f} free cash. "
            f"Not enough for a new position."
        )
    if num_positions >= max_positions:
        warnings.append(
            f"MAX POSITIONS REACHED: {num_positions}/{max_positions}. "
            f"Close a position before opening new ones."
        )
    if reserved_by_orders > cash * 0.5:
        warnings.append(
            f"HEAVY ORDER BOOK: ${reserved_by_orders:,.2f} reserved "
            f"by open orders ({pct_reserved:.0f}% of portfolio)."
        )
    return warnings


def _compute_sustainability(pct_invested, num_positions, max_positions,
                             free_cash, min_trade_size):
    """Sustainability score (0-100). Pure given its args."""
    sustainability = 100
    if pct_invested > 50:
        sustainability -= (pct_invested - 50)
    if num_positions >= max_positions:
        sustainability -= 20
    if free_cash < min_trade_size:
        sustainability -= 30
    return max(0, min(100, sustainability))


def _build_recommendation(sustainability, additional_trades_possible,
                           free_cash):
    """Recommendation string keyed off the sustainability score."""
    if sustainability >= 80:
        return (f"Healthy. {additional_trades_possible} more trades "
                f"possible. Free cash: ${free_cash:,.2f}")
    if sustainability >= 50:
        return (f"Caution. Consider tightening stops or taking "
                f"profits. Free cash: ${free_cash:,.2f}")
    return (f"Critical. Reduce exposure before opening new positions. "
            f"Free cash: ${free_cash:,.2f}")
