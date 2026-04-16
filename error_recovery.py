#!/usr/bin/env python3
"""
Error Recovery Script — Finds and fixes orphan positions, missing stop-losses,
and stale strategy files.

Checks:
1. Orphan position: has Alpaca position but no strategy file -> create trailing_stop strategy
2. Missing stop-loss: has position + strategy but no stop-loss order -> place stop-loss
3. Stale strategy: has strategy file but no position and status != "closed" -> mark closed

Run: python3 "/Users/kevinbell/Alpaca Trading/error_recovery.py"
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_DIR = os.path.join(BASE_DIR, "strategies")

API_ENDPOINT = "https://paper-api.alpaca.markets/v2"
API_KEY = ""
API_SECRET = ""
HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}


def api_get(url, timeout=15):
    """Make an authenticated GET request to Alpaca API."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def api_post(url, data, timeout=15):
    """Make an authenticated POST request to Alpaca API."""
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={**HEADERS, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def load_json(path):
    """Load a JSON file, returning None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, data):
    """Save data to a JSON file."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def list_strategy_files():
    """List all strategy JSON files and parse their contents."""
    strategies = {}
    if not os.path.isdir(STRATEGIES_DIR):
        return strategies

    for fname in os.listdir(STRATEGIES_DIR):
        if fname.endswith(".json"):
            path = os.path.join(STRATEGIES_DIR, fname)
            data = load_json(path)
            if data:
                strategies[fname] = {
                    "path": path,
                    "data": data,
                    "symbol": data.get("symbol"),
                    "strategy": data.get("strategy", ""),
                    "status": data.get("status", ""),
                }
    return strategies


def get_open_orders_for_symbol(symbol):
    """Get open orders for a specific symbol."""
    url = f"{API_ENDPOINT}/orders?status=open&symbols={symbol}&limit=50"
    result = api_get(url)
    if isinstance(result, list):
        return result
    return []


def has_stop_order(orders):
    """Check if any of the orders is a stop or trailing_stop order."""
    for o in orders:
        otype = o.get("type", "")
        if otype in ("stop", "stop_limit", "trailing_stop"):
            return True
    return False


def create_orphan_strategy(symbol, qty, current_price, avg_entry):
    """Create a basic trailing_stop strategy file for an orphan position."""
    stop_price = round(float(avg_entry) * 0.90, 2)  # 10% stop-loss
    strategy = {
        "symbol": symbol,
        "strategy": "trailing_stop",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "status": "active",
        "entry_price_estimate": float(avg_entry),
        "initial_qty": int(float(qty)),
        "auto_recovered": True,
        "recovery_note": "Created by error_recovery.py — orphan position found without strategy file",
        "rules": {
            "stop_loss_pct": 0.10,
            "trailing_activation_pct": 0.10,
            "trailing_distance_pct": 0.05,
            "ladder_in": [],
        },
        "state": {
            "entry_fill_price": float(avg_entry),
            "entry_order_id": None,
            "stop_order_id": None,
            "highest_price_seen": float(current_price),
            "trailing_activated": float(current_price) >= float(avg_entry) * 1.10,
            "current_stop_price": stop_price,
            "total_shares_held": int(float(qty)),
            "ladder_fills": [],
            "profit_takes": [],
        },
    }
    return strategy


def place_stop_loss_order(symbol, qty, stop_price):
    """Place a stop-loss order via Alpaca API."""
    order_data = {
        "symbol": symbol,
        "qty": str(int(float(qty))),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(stop_price, 2)),
        "time_in_force": "gtc",
    }
    result = api_post(f"{API_ENDPOINT}/orders", order_data)
    return result


def main():
    print("=" * 60)
    print("ERROR RECOVERY")
    print("=" * 60)

    issues_found = 0
    issues_fixed = 0

    # Fetch current positions
    print("\nFetching current positions...")
    positions = api_get(f"{API_ENDPOINT}/positions")
    if isinstance(positions, dict) and "error" in positions:
        print(f"  ERROR: Could not fetch positions: {positions['error']}")
        print("  Cannot proceed without position data. Exiting.")
        return
    if not isinstance(positions, list):
        positions = []
    print(f"  Found {len(positions)} open positions")

    # Build position map: symbol -> position data
    position_map = {}
    for p in positions:
        sym = p.get("symbol", "")
        if sym:
            position_map[sym] = p
            print(f"    {sym}: {p.get('qty', 0)} shares @ ${float(p.get('avg_entry_price', 0)):.2f}")

    # Load strategy files
    print("\nLoading strategy files...")
    strategies = list_strategy_files()
    print(f"  Found {len(strategies)} strategy files")

    # Build strategy symbol map
    strategy_symbol_map = {}  # symbol -> (filename, strategy_data)
    for fname, info in strategies.items():
        sym = info["symbol"]
        if sym:
            strategy_symbol_map[sym] = (fname, info)
            print(f"    {fname}: {sym} ({info['strategy']}, status={info['status']})")
        else:
            print(f"    {fname}: no symbol assigned ({info['strategy']}, status={info['status']})")

    # ---- Check 1: Orphan Positions ----
    print("\n--- Check 1: Orphan Positions ---")
    print("  (Position exists but no strategy file)")
    orphans = []
    for sym, pos in position_map.items():
        if sym not in strategy_symbol_map:
            orphans.append((sym, pos))

    if not orphans:
        print("  No orphan positions found.")
    else:
        for sym, pos in orphans:
            issues_found += 1
            qty = pos.get("qty", 0)
            avg_entry = float(pos.get("avg_entry_price", 0))
            current_price = float(pos.get("current_price", 0))
            print(f"\n  ORPHAN: {sym} ({qty} shares, entry ${avg_entry:.2f}, current ${current_price:.2f})")

            # Create strategy file
            strategy = create_orphan_strategy(sym, qty, current_price, avg_entry)
            fname = f"trailing_stop_{sym}.json"
            fpath = os.path.join(STRATEGIES_DIR, fname)
            save_json(fpath, strategy)
            print(f"    FIXED: Created {fname} with 10% stop-loss at ${strategy['state']['current_stop_price']:.2f}")
            issues_fixed += 1

            # Also check if it needs a stop-loss order (will be caught in Check 2)
            strategy_symbol_map[sym] = (fname, {
                "path": fpath,
                "data": strategy,
                "symbol": sym,
                "strategy": "trailing_stop",
                "status": "active",
            })

    # ---- Check 2: Missing Stop-Loss ----
    print("\n--- Check 2: Missing Stop-Loss Orders ---")
    print("  (Position + strategy exists but no stop order)")
    missing_stops = []

    for sym, pos in position_map.items():
        if sym in strategy_symbol_map:
            fname, info = strategy_symbol_map[sym]
            status = info["status"]
            if status in ("closed", "waiting_for_auto_deployer"):
                continue

            # Check for open stop orders
            print(f"  Checking orders for {sym}...")
            orders = get_open_orders_for_symbol(sym)
            if not has_stop_order(orders):
                missing_stops.append((sym, pos, info))

    if not missing_stops:
        print("  All positions have stop-loss orders (or are in setup state).")
    else:
        for sym, pos, info in missing_stops:
            issues_found += 1
            qty = float(pos.get("qty", 0))
            avg_entry = float(pos.get("avg_entry_price", 0))

            # Determine stop price from strategy rules
            strat_data = info["data"]
            stop_loss_pct = 0.10
            if strat_data.get("rules"):
                stop_loss_pct = strat_data["rules"].get("stop_loss_pct", 0.10)
            elif strat_data.get("strategy") == "breakout":
                stop_loss_pct = 0.05

            stop_price = round(avg_entry * (1 - stop_loss_pct), 2)
            print(f"\n  MISSING STOP: {sym} ({int(qty)} shares, entry ${avg_entry:.2f})")
            print(f"    Placing stop-loss at ${stop_price:.2f} ({stop_loss_pct*100:.0f}% below entry)")

            result = place_stop_loss_order(sym, qty, stop_price)
            if isinstance(result, dict) and "error" not in result:
                order_id = result.get("id", "unknown")
                print(f"    FIXED: Stop-loss order placed (order_id: {order_id})")

                # Update strategy file with stop order ID
                if strat_data.get("state"):
                    strat_data["state"]["stop_order_id"] = order_id
                    strat_data["state"]["current_stop_price"] = stop_price
                    save_json(info["path"], strat_data)
                    print(f"    Updated strategy file with stop order ID")
                issues_fixed += 1
            else:
                err = result.get("error", "Unknown error") if isinstance(result, dict) else str(result)
                print(f"    ERROR: Could not place stop-loss: {err}")

    # ---- Check 3: Stale Strategies ----
    print("\n--- Check 3: Stale Strategy Files ---")
    print("  (Strategy file exists but no position, and status is not 'closed')")
    stale = []

    for sym, (fname, info) in strategy_symbol_map.items():
        if sym and sym not in position_map:
            status = info["status"]
            if status not in ("closed", "waiting_for_auto_deployer"):
                stale.append((sym, fname, info))

    # Also check strategy files with no symbol assigned
    for fname, info in strategies.items():
        if not info["symbol"]:
            status = info["status"]
            if status not in ("closed", "waiting_for_auto_deployer"):
                # These are template/setup files - skip them
                print(f"  Skipping {fname}: no symbol assigned (template/setup file)")

    if not stale:
        print("  No stale strategy files found.")
    else:
        for sym, fname, info in stale:
            issues_found += 1
            print(f"\n  STALE: {fname} (symbol: {sym}, status: {info['status']})")
            print(f"    No position found for {sym} — marking as closed")

            strat_data = info["data"]
            strat_data["status"] = "closed"
            strat_data["closed_reason"] = "No position found — marked closed by error_recovery.py"
            strat_data["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            save_json(info["path"], strat_data)
            print(f"    FIXED: Marked {fname} as closed")
            issues_fixed += 1

    # Summary
    print("\n" + "=" * 60)
    print("ERROR RECOVERY SUMMARY")
    print("=" * 60)
    print(f"  Positions checked:   {len(positions)}")
    print(f"  Strategy files:      {len(strategies)}")
    print(f"  Issues found:        {issues_found}")
    print(f"  Issues fixed:        {issues_fixed}")

    if issues_found == 0:
        print("\n  All clear — no issues detected.")
    elif issues_fixed == issues_found:
        print(f"\n  All {issues_found} issues were fixed successfully.")
    else:
        print(f"\n  WARNING: {issues_found - issues_fixed} issues could not be fixed automatically.")

    print("=" * 60)


if __name__ == "__main__":
    main()
