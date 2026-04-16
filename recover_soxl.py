#!/usr/bin/env python3
"""
SOXL orphan recovery script.

Railway redeploys wiped the strategy file for the live SOXL position
(117 shares @ ~$85.11, stop at $76.60). This recreates the strategy file
so the cloud_scheduler monitor can manage it again.

Reads the current position and open stop order from Alpaca, then writes
the strategy JSON to DATA_DIR/strategies/ (or the appropriate per-user
strategies dir when running under auth).

Safe to run multiple times — exits without change if the file already
exists.

Usage:
    python3 "/Users/kevinbell/Alpaca Trading/recover_soxl.py"
    python3 "/Users/kevinbell/Alpaca Trading/recover_soxl.py" --force   # overwrite
"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone


def load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())


load_dotenv()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}


def safe_save_json(path, data):
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
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


def api_get(path):
    url = API_ENDPOINT + path
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}


def discover_strategies_dir():
    """Pick the right strategies directory.

    If the auth module finds a user that owns the SOXL position we'd use
    their per-user strategies dir. For simplicity, we default to
    DATA_DIR/strategies — that matches the legacy/env-var single-user mode
    which is how the production Railway service currently runs (or did
    before the volume existed). The caller is free to pass --user-id.
    """
    # Prefer auth-backed single user if one exists
    try:
        import auth  # noqa: F401
        auth.init_db()
        users = auth.list_active_users()
        if len(users) == 1:
            d = auth.user_data_dir(users[0]["id"])
            return os.path.join(d, "strategies")
    except Exception:
        pass
    return os.path.join(DATA_DIR, "strategies")


def find_open_stop_order(symbol):
    orders = api_get(f"/orders?status=open&symbols={symbol}&limit=50")
    if not isinstance(orders, list):
        return None
    for o in orders:
        if o.get("type") in ("stop", "stop_limit") and o.get("side") == "sell":
            return o
    return None


def find_position(symbol):
    pos = api_get(f"/positions/{symbol}")
    if isinstance(pos, dict) and "error" not in pos:
        return pos
    return None


def main():
    force = "--force" in sys.argv
    symbol = "SOXL"

    if not API_KEY or not API_SECRET:
        print("ERROR: ALPACA_API_KEY / ALPACA_API_SECRET not set in environment")
        return 1

    strategies_dir = discover_strategies_dir()
    os.makedirs(strategies_dir, exist_ok=True)
    filename = f"trailing_stop_{symbol}.json"
    filepath = os.path.join(strategies_dir, filename)

    print(f"Strategies dir: {strategies_dir}")
    print(f"Target file:    {filepath}")

    if os.path.exists(filepath) and not force:
        print(f"\nStrategy file already exists. Use --force to overwrite. Exiting.")
        return 0

    print("\nFetching SOXL position from Alpaca...")
    pos = find_position(symbol)
    if not pos:
        print("WARNING: No SOXL position found via Alpaca. Falling back to known values.")
        qty = 117
        avg_entry = 85.11
        current_price = avg_entry
    else:
        qty = int(float(pos.get("qty", 0)))
        avg_entry = float(pos.get("avg_entry_price", 85.11))
        current_price = float(pos.get("current_price", avg_entry))
        print(f"  Found: {qty} shares @ ${avg_entry:.2f} (current ${current_price:.2f})")

    print("\nFetching open stop order for SOXL...")
    stop_order = find_open_stop_order(symbol)
    stop_order_id = None
    current_stop_price = 76.60
    if stop_order:
        stop_order_id = stop_order.get("id")
        try:
            current_stop_price = float(stop_order.get("stop_price") or current_stop_price)
        except (TypeError, ValueError):
            pass
        print(f"  Found stop order {stop_order_id} @ ${current_stop_price:.2f}")
    else:
        print("  No open stop order found — leaving stop_order_id null so the")
        print("  monitor will place a fresh stop on next tick.")

    strategy = {
        "symbol": symbol,
        "strategy": "trailing_stop",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "status": "active",
        "entry_price_estimate": round(avg_entry, 2),
        "initial_qty": qty,
        "deployer": "cloud_scheduler_recovered",
        "rules": {
            "stop_loss_pct": 0.10,
            "trailing_activation_pct": 0.10,
            "trailing_distance_pct": 0.05,
        },
        "state": {
            "entry_fill_price": round(avg_entry, 2),
            "entry_order_id": None,
            "stop_order_id": stop_order_id,
            "highest_price_seen": None,
            "trailing_activated": False,
            "current_stop_price": round(current_stop_price, 2),
            "total_shares_held": qty,
            "profit_takes": [],
        },
        "reasoning": {
            "recovered": True,
            "note": "Recreated from Alpaca position after Railway redeploy wiped strategy file",
        },
    }

    safe_save_json(filepath, strategy)
    print(f"\nWrote {filepath}")
    print("Monitor will pick this up on next tick and manage the position.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
