#!/usr/bin/env python3
"""
Capital sustainability checker for the trading bot.
Ensures there's enough free capital to continue trading and flags when we're overextended.
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_ENDPOINT = "https://paper-api.alpaca.markets/v2"
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

def api_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def check_capital():
    account = api_get(f"{API_ENDPOINT}/account")
    positions = api_get(f"{API_ENDPOINT}/positions")
    orders = api_get(f"{API_ENDPOINT}/orders?status=open")

    if "error" in account:
        return {"error": account["error"]}

    portfolio_value = float(account.get("portfolio_value", 0))
    cash = float(account.get("cash", 0))
    buying_power = float(account.get("buying_power", 0))
    equity = float(account.get("equity", 0))

    positions = positions if isinstance(positions, list) else []
    orders = orders if isinstance(orders, list) else []

    # Calculate capital in use
    total_position_value = sum(float(p.get("market_value", 0)) for p in positions)
    num_positions = len(positions)

    # Calculate capital reserved by open orders
    reserved_by_orders = 0
    for o in orders:
        if o.get("side") == "buy":
            price = float(o.get("limit_price") or o.get("stop_price") or 0)
            qty = float(o.get("qty", 0))
            reserved_by_orders += price * qty

    # Key metrics
    pct_invested = (total_position_value / portfolio_value * 100) if portfolio_value > 0 else 0
    pct_reserved = (reserved_by_orders / portfolio_value * 100) if portfolio_value > 0 else 0
    free_cash = cash - reserved_by_orders
    pct_free = (free_cash / portfolio_value * 100) if portfolio_value > 0 else 0

    # Read guardrails
    guardrails = {}
    try:
        with open(os.path.join(BASE_DIR, "guardrails.json")) as f:
            guardrails = json.load(f)
    except:
        pass

    max_positions = guardrails.get("max_positions", 5)
    max_position_pct = guardrails.get("max_position_pct", 0.10)

    # Can we afford another trade?
    min_trade_size = portfolio_value * 0.03  # Minimum 3% position
    can_trade = free_cash >= min_trade_size and num_positions < max_positions

    # How many more trades can we afford?
    avg_position_size = portfolio_value * max_position_pct
    additional_trades_possible = int(free_cash / avg_position_size) if avg_position_size > 0 else 0
    additional_trades_possible = min(additional_trades_possible, max_positions - num_positions)

    # Warnings
    warnings = []
    if pct_invested > 80:
        warnings.append(f"HIGH EXPOSURE: {pct_invested:.0f}% of portfolio is invested. Consider reducing positions.")
    if pct_invested > 60:
        warnings.append(f"MODERATE EXPOSURE: {pct_invested:.0f}% invested. {pct_free:.0f}% free cash remaining.")
    if free_cash < min_trade_size:
        warnings.append(f"LOW CAPITAL: Only ${free_cash:,.2f} free cash. Not enough for a new position.")
    if num_positions >= max_positions:
        warnings.append(f"MAX POSITIONS REACHED: {num_positions}/{max_positions}. Close a position before opening new ones.")
    if reserved_by_orders > cash * 0.5:
        warnings.append(f"HEAVY ORDER BOOK: ${reserved_by_orders:,.2f} reserved by open orders ({pct_reserved:.0f}% of portfolio).")

    # Sustainability score (0-100)
    sustainability = 100
    if pct_invested > 50: sustainability -= (pct_invested - 50)
    if num_positions >= max_positions: sustainability -= 20
    if free_cash < min_trade_size: sustainability -= 30
    sustainability = max(0, min(100, sustainability))

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        "recommendation": ""
    }

    # Generate recommendation
    if sustainability >= 80:
        result["recommendation"] = f"Healthy. {additional_trades_possible} more trades possible. Free cash: ${free_cash:,.2f}"
    elif sustainability >= 50:
        result["recommendation"] = f"Caution. Consider tightening stops or taking profits. Free cash: ${free_cash:,.2f}"
    else:
        result["recommendation"] = f"Critical. Reduce exposure before opening new positions. Free cash: ${free_cash:,.2f}"

    # Save to file
    with open(os.path.join(BASE_DIR, "capital_status.json"), "w") as f:
        json.dump(result, f, indent=2)

    return result

if __name__ == "__main__":
    result = check_capital()
    print(f"Capital Check:")
    print(f"  Portfolio: ${result['portfolio_value']:,.2f}")
    print(f"  Invested: {result['pct_invested']}% | Reserved: {result['pct_reserved']}% | Free: {result['pct_free']}%")
    print(f"  Positions: {result['num_positions']}/{result['max_positions']}")
    print(f"  Additional trades possible: {result['additional_trades_possible']}")
    print(f"  Sustainability: {result['sustainability_score']}/100")
    print(f"  Recommendation: {result['recommendation']}")
    for w in result.get("warnings", []):
        print(f"  ⚠ {w}")
