#!/usr/bin/env python3
"""
Capital sustainability checker for the trading bot.
Ensures there's enough free capital to continue trading and flags when we're overextended.
"""
import json
import os
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from et_time import now_et


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


def safe_save_json(path, data):
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp_path, path)
    except Exception:
        # Narrow to Exception so KeyboardInterrupt / SystemExit aren't
        # swallowed during shutdown. Inner unlink stays narrow to OSError
        # (the only thing os.unlink legitimately raises when cleaning up
        # a temp file that may already be gone).
        try: os.unlink(tmp_path)
        except OSError: pass
        raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent runtime data lives. On Railway, set to a volume mount
# path (e.g. /data). Locally defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
# Market data endpoint (separate from trading endpoint) for quotes/trades.
DATA_ENDPOINT = os.environ.get("ALPACA_DATA_ENDPOINT", "https://data.alpaca.markets/v2")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}


def api_get_with_retry(url, max_retries=3, timeout=15):
    """Make an authenticated GET request with retry logic for 429/5xx."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"error": str(e)}


# Round-61 pt.34: pure math extracted to capital_check_core.py so it
# can be unit-tested without subprocess execution. Re-exported here
# for backwards-compat with any caller that imported the helper
# directly (kept the underscore-prefix alias for round-15 callers).
from capital_check_core import (
    _LAST_RESORT_PRICE_PER_SHARE,
    compute_reserved_by_orders,
    compute_capital_metrics,
)
_compute_reserved_by_orders = compute_reserved_by_orders


def check_capital():
    """Subprocess entry-point wrapper.

    Round-61 pt.34 thinned this from a 130-line monolith to a
    coordinator: fetch Alpaca data + read guardrails (the I/O), then
    delegate every line of math to capital_check_core. Saves the
    result via the round-10 per-user CAPITAL_STATUS_PATH override.
    """
    account = api_get_with_retry(f"{API_ENDPOINT}/account")
    positions = api_get_with_retry(f"{API_ENDPOINT}/positions")
    orders = api_get_with_retry(f"{API_ENDPOINT}/orders?status=open")

    # Read guardrails
    guardrails = {}
    try:
        with open(os.path.join(DATA_DIR, "guardrails.json")) as f:
            guardrails = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    def _fetch_last(sym):
        if not sym:
            return 0.0
        trade = api_get_with_retry(
            f"{DATA_ENDPOINT}/stocks/{sym}/trades/latest?feed=iex"
        )
        try:
            return float((trade or {}).get("trade", {}).get("p") or 0)
        except Exception:
            return 0.0

    result = compute_capital_metrics(
        account=account,
        positions=positions if isinstance(positions, list) else [],
        orders=orders if isinstance(orders, list) else [],
        guardrails=guardrails,
        fetch_last=_fetch_last,
        now_iso=now_et().isoformat(),
    )

    if "error" in result:
        return result

    # Save to file (atomic write). Round-10: honor CAPITAL_STATUS_PATH
    # env var so per-user scheduler invocations land in each user's
    # dir instead of the shared /data/capital_status.json (which
    # previously leaked state across users — user B would read user A's
    # can_trade / free-cash).
    _cap_path = os.environ.get("CAPITAL_STATUS_PATH",
                                os.path.join(DATA_DIR, "capital_status.json"))
    safe_save_json(_cap_path, result)

    return result

if __name__ == "__main__":
    result = check_capital()
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"Capital Check:")
        print(f"  Portfolio: ${result['portfolio_value']:,.2f}")
        print(f"  Invested: {result['pct_invested']}% | Reserved: {result['pct_reserved']}% | Free: {result['pct_free']}%")
        print(f"  Positions: {result['num_positions']}/{result['max_positions']}")
        print(f"  Additional trades possible: {result['additional_trades_possible']}")
        print(f"  Sustainability: {result['sustainability_score']}/100")
        print(f"  Recommendation: {result['recommendation']}")
        for w in result.get("warnings", []):
            print(f"  WARNING: {w}")
