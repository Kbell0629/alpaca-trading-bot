#!/usr/bin/env python3
"""
Real-time price monitor for the trading bot.
Polls Alpaca every 10 seconds for active positions (vs 5 minutes for scheduled task).
Checks stop-loss conditions and triggers immediate sells.
Run: python3 realtime.py (runs continuously during market hours)
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import subprocess
import tempfile
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_DIR = os.path.join(BASE_DIR, "strategies")

# Load .env
def load_dotenv():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())
load_dotenv()

API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
DATA_ENDPOINT = os.environ.get("ALPACA_DATA_ENDPOINT", "https://data.alpaca.markets/v2")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

POLL_INTERVAL = 10  # seconds between price checks
GUARDRAILS_PATH = os.path.join(BASE_DIR, "guardrails.json")


def api_get(url, timeout=10):
    """Make a GET request to Alpaca API."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def api_post(url, data, timeout=10):
    """Make a POST request to Alpaca API."""
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={**HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def api_delete(url, timeout=10):
    """Make a DELETE request to Alpaca API."""
    req = urllib.request.Request(url, headers=HEADERS, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode()) if body else {}
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
    """Atomically save JSON to a file using a temp file + rename."""
    dir_name = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def notify(message, notify_type="info"):
    """Send a push notification via notify.py (non-blocking)."""
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "notify.py"), "--type", notify_type, message]
        )
    except Exception:
        pass


def is_market_open():
    """Check if the stock market is currently open via Alpaca clock endpoint."""
    result = api_get(f"{API_ENDPOINT}/clock")
    if isinstance(result, dict) and "error" not in result:
        return result.get("is_open", False)
    return False


def get_active_strategies():
    """Load all active strategy files and return dict of symbol -> strategy_data."""
    strategies = {}
    if not os.path.isdir(STRATEGIES_DIR):
        return strategies
    for fname in os.listdir(STRATEGIES_DIR):
        if not fname.endswith(".json"):
            continue
        data = load_json(os.path.join(STRATEGIES_DIR, fname))
        if not data:
            continue
        status = data.get("status", "")
        symbol = data.get("symbol")
        if status in ("active", "awaiting_fill") and symbol:
            strategies[symbol] = {**data, "_filename": fname}
    return strategies


def get_prices(symbols):
    """Fetch latest trade price for multiple symbols using the snapshots endpoint."""
    if not symbols:
        return {}
    prices = {}
    # Use the multi-snapshot endpoint for efficiency
    symbols_str = ",".join(symbols)
    url = f"{DATA_ENDPOINT}/stocks/snapshots?symbols={symbols_str}&feed=iex"
    result = api_get(url)
    if isinstance(result, dict) and "error" not in result:
        for sym, snap in result.items():
            trade = snap.get("latestTrade", {})
            prices[sym] = trade.get("p", 0)
    return prices


def check_stop_conditions(strategies, prices):
    """Check if any active strategy's stop-loss has been hit or trailing stop needs raising."""
    actions = []
    for symbol, strat in strategies.items():
        price = prices.get(symbol, 0)
        if not price:
            continue

        state = strat.get("state", {})
        stop_price = state.get("current_stop_price")
        shares = state.get("total_shares_held", 0)
        entry = state.get("entry_fill_price")

        if not stop_price or not shares or shares <= 0:
            continue

        # STOP HIT
        if price <= stop_price:
            actions.append(
                {
                    "type": "stop_triggered",
                    "symbol": symbol,
                    "price": price,
                    "stop_price": stop_price,
                    "shares": shares,
                    "entry": entry,
                    "strategy": strat.get("strategy", "unknown"),
                    "filename": strat.get("_filename"),
                }
            )

        # TRAILING STOP: Check if we need to raise the floor
        elif strat.get("strategy") in ("trailing_stop", "breakout"):
            highest = state.get("highest_price_seen", 0)
            trailing_active = state.get("trailing_activated", False)
            trail_pct = strat.get("rules", {}).get("trailing_distance_pct", 0.05)
            activation_pct = strat.get("rules", {}).get("trailing_activation_pct", 0.10)

            # Update highest price
            if price > (highest or 0):
                state["highest_price_seen"] = price
                highest = price

                # Check activation
                if entry and not trailing_active and highest >= entry * (1 + activation_pct):
                    state["trailing_activated"] = True
                    trailing_active = True

                # Raise the floor
                if trailing_active:
                    new_stop = round(highest * (1 - trail_pct), 2)
                    if new_stop > (stop_price or 0):
                        actions.append(
                            {
                                "type": "raise_stop",
                                "symbol": symbol,
                                "old_stop": stop_price,
                                "new_stop": new_stop,
                                "current_price": price,
                                "highest": highest,
                                "filename": strat.get("_filename"),
                            }
                        )

    return actions


def execute_actions(actions):
    """Execute stop triggers and stop raises."""
    for action in actions:
        if action["type"] == "stop_triggered":
            sym = action["symbol"]
            shares = action["shares"]
            print(f"  STOP TRIGGERED: {sym} at ${action['price']:.2f} (stop was ${action['stop_price']:.2f})")

            # Place market sell
            order = api_post(
                f"{API_ENDPOINT}/orders",
                {
                    "symbol": sym,
                    "qty": str(shares),
                    "side": "sell",
                    "type": "market",
                    "time_in_force": "day",
                },
            )

            if "error" not in (order if isinstance(order, dict) else {}):
                print(f"    Sell order placed for {shares} shares")
                notify(
                    f"STOP TRIGGERED: {sym} sold {shares} shares at ~${action['price']:.2f}. Entry was ${action.get('entry', '?')}",
                    "stop",
                )

                # Update strategy file
                filepath = os.path.join(STRATEGIES_DIR, action["filename"])
                strat = load_json(filepath)
                if strat:
                    strat["state"]["total_shares_held"] = 0
                    strat["state"]["current_stop_price"] = None
                    strat["state"]["stop_order_id"] = None
                    strat["status"] = "closed"
                    strat["state"]["exit_price"] = action["price"]
                    strat["state"]["exit_reason"] = "realtime_stop_triggered"
                    save_json(filepath, strat)

                # Update guardrails cooldown
                guardrails = load_json(GUARDRAILS_PATH)
                if guardrails:
                    guardrails["last_loss_time"] = datetime.now(timezone.utc).isoformat()
                    save_json(GUARDRAILS_PATH, guardrails)
            else:
                print(f"    ERROR placing sell: {order}")

        elif action["type"] == "raise_stop":
            sym = action["symbol"]
            print(
                f"  RAISE STOP: {sym} ${action['old_stop']:.2f} -> ${action['new_stop']:.2f} (price: ${action['current_price']:.2f})"
            )

            # Cancel old stop order and place new one
            filepath = os.path.join(STRATEGIES_DIR, action["filename"])
            strat = load_json(filepath)
            if strat:
                old_stop_id = strat["state"].get("stop_order_id")
                if old_stop_id:
                    api_delete(f"{API_ENDPOINT}/orders/{old_stop_id}")

                # Place new stop
                shares = strat["state"].get("total_shares_held", 0)
                if shares > 0:
                    new_order = api_post(
                        f"{API_ENDPOINT}/orders",
                        {
                            "symbol": sym,
                            "qty": str(shares),
                            "side": "sell",
                            "type": "stop",
                            "stop_price": str(action["new_stop"]),
                            "time_in_force": "gtc",
                        },
                    )
                    if isinstance(new_order, dict) and "id" in new_order:
                        strat["state"]["stop_order_id"] = new_order["id"]
                        strat["state"]["current_stop_price"] = action["new_stop"]
                        print(f"    New stop placed at ${action['new_stop']:.2f}")
                        notify(
                            f"Stop raised on {sym}: ${action['old_stop']:.2f} -> ${action['new_stop']:.2f}",
                            "info",
                        )

                strat["state"]["highest_price_seen"] = action["highest"]
                save_json(filepath, strat)


def check_daily_loss():
    """Check if daily loss limit has been breached and trigger kill switch if so."""
    guardrails = load_json(GUARDRAILS_PATH)
    if not guardrails:
        return False

    daily_start = guardrails.get("daily_starting_value")
    if not daily_start:
        return False

    account = api_get(f"{API_ENDPOINT}/account")
    if isinstance(account, dict) and "error" not in account:
        current = float(account.get("portfolio_value", 0))
        if daily_start > 0:
            loss_pct = (daily_start - current) / daily_start
        else:
            return False
        if loss_pct > guardrails.get("daily_loss_limit_pct", 0.03):
            print(f"  DAILY LOSS LIMIT BREACHED: {loss_pct * 100:.1f}% loss")
            guardrails["kill_switch"] = True
            guardrails["kill_switch_triggered_at"] = datetime.now(timezone.utc).isoformat()
            guardrails["kill_switch_reason"] = (
                f"Daily loss {loss_pct * 100:.1f}% exceeded {guardrails['daily_loss_limit_pct'] * 100}% limit"
            )
            save_json(GUARDRAILS_PATH, guardrails)

            # Cancel all orders, close all positions
            api_delete(f"{API_ENDPOINT}/orders")
            api_delete(f"{API_ENDPOINT}/positions")
            notify(
                f"KILL SWITCH: Daily loss {loss_pct * 100:.1f}% exceeded limit. All positions closed.",
                "kill",
            )
            return True
    return False


def main():
    print("Real-time price monitor starting...")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    print(f"  API: {API_ENDPOINT}")

    while True:
        try:
            # Check kill switch
            guardrails = load_json(GUARDRAILS_PATH)
            if guardrails and guardrails.get("kill_switch"):
                print("Kill switch active. Sleeping 60s...")
                time.sleep(60)
                continue

            # Check market hours
            if not is_market_open():
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Market closed. Sleeping 60s...")
                time.sleep(60)
                continue

            # Load active strategies
            strategies = get_active_strategies()
            symbols = list(strategies.keys())

            if not symbols:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] No active strategies. Sleeping 30s...")
                time.sleep(30)
                continue

            # Get prices
            prices = get_prices(symbols)

            # Check conditions
            actions = check_stop_conditions(strategies, prices)

            if actions:
                execute_actions(actions)

            # Check daily loss (every 5th cycle = every ~50 seconds)
            if int(time.time()) % 50 < POLL_INTERVAL:
                check_daily_loss()

            # Status line
            price_str = " | ".join(f"{s}:${prices.get(s, 0):.2f}" for s in symbols[:5])
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {price_str} ({len(actions)} actions)")

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\nShutting down real-time monitor.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
