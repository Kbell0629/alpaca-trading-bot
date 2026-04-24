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
import os.path
import re
import subprocess
import sys
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
        # Narrow from bare except so KeyboardInterrupt / SystemExit
        # propagate instead of hitting the cleanup+re-raise branch.
        try: os.unlink(tmp_path)
        except OSError: pass
        raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent runtime data lives. On Railway, set to a volume mount
# path (e.g. /data). Locally defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
STRATEGIES_DIR = os.environ.get("STRATEGIES_DIR", os.path.join(DATA_DIR, "strategies"))
# Round-11: honor per-user env var same way update_scorecard.py does.
# Without this, cloud_scheduler.run_daily_close passed STRATEGIES_DIR
# in env but error_recovery silently wrote to the shared /data/strategies
# dir, leaving per-user orphan stops unrecovered and shared residue
# receiving stops under whichever user's env was current.

API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}


def api_get(url, timeout=15):
    """Make an authenticated GET request to Alpaca API (legacy, no retry)."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


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


# Round-25: OCC option-symbol helpers. Alpaca returns options with
# symbols like "CHWY260515P00025000" — 1-6 letters of underlying, then
# YYMMDD, then C/P, then 8-digit strike × 1000. A plain regex covers
# the shape well enough for the orphan-check path (we don't need to
# parse the date/strike/right, just recognise-and-split).
_OCC_OPTION_RE = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")


def _is_occ_option_symbol(sym):
    """Return True if sym matches the OCC option-symbol shape."""
    return bool(sym and _OCC_OPTION_RE.match(sym))


def _occ_underlying(sym):
    """Extract underlying equity symbol from an OCC option symbol.
    Returns None if sym isn't an OCC symbol."""
    if not sym:
        return None
    m = _OCC_OPTION_RE.match(sym)
    return m.group(1) if m else None


def list_strategy_files():
    """List all strategy JSON files and parse their contents.

    Round-61 pt.16: filter out strategy files whose status is closed
    / stopped / cancelled / exited / filled_and_closed. The dashboard's
    `_mark_auto_deployed` in server.py already skips these (round-61
    #110), so a position with ONLY a stale closed file on disk shows
    MANUAL in the UI — but error_recovery was returning the stale
    file from here, seeing it in `strategy_symbol_map`, and deciding
    the position was already managed. Result: user clicks "Adopt
    MANUAL -> AUTO" and gets told "No orphans found" even though the
    dashboard clearly shows MANUAL on their position. Match the
    dashboard's filter so the two code paths agree.
    """
    strategies = {}
    if not os.path.isdir(STRATEGIES_DIR):
        return strategies

    _CLOSED_STATUSES = {"closed", "stopped", "cancelled", "canceled",
                        "exited", "filled_and_closed"}
    for fname in os.listdir(STRATEGIES_DIR):
        if fname.endswith(".json"):
            path = os.path.join(STRATEGIES_DIR, fname)
            data = load_json(path)
            if data:
                status = str(data.get("status") or "").lower()
                if status in _CLOSED_STATUSES:
                    # Stale file — dashboard's _mark_auto_deployed
                    # ignores it too. Treat as not-a-strategy so the
                    # orphan scan adopts the live Alpaca position.
                    continue
                strategies[fname] = {
                    "path": path,
                    "data": data,
                    "symbol": data.get("symbol"),
                    "strategy": data.get("strategy", ""),
                    "status": status,
                }
    return strategies


def get_open_orders_for_symbol(symbol):
    """Get open orders for a specific symbol."""
    url = f"{API_ENDPOINT}/orders?status=open&symbols={symbol}&limit=50"
    result = api_get_with_retry(url)
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
    """Create a basic trailing_stop strategy file for an orphan position.
    Round-10: handles SHORT positions by inverting direction so we
    don't write a sell-stop BELOW entry on a short (which would close
    a winning short on a drop, wrong direction)."""
    qty_f = float(qty)
    is_short = qty_f < 0
    if is_short:
        stop_price = round(float(avg_entry) * 1.10, 2)  # 10% above entry
        strategy_name = "short_sell"
    else:
        stop_price = round(float(avg_entry) * 0.90, 2)
        strategy_name = "trailing_stop"
    strategy = {
        "symbol": symbol,
        "strategy": strategy_name,
        "created": now_et().strftime("%Y-%m-%d"),
        "status": "active",
        "entry_price_estimate": float(avg_entry),
        "initial_qty": int(abs(qty_f)),
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
            "stop_order_id": None if not is_short else None,
            "cover_order_id": None if is_short else None,
            "highest_price_seen": float(current_price) if not is_short else None,
            "lowest_price_seen": float(current_price) if is_short else None,
            "trailing_activated": (float(current_price) >= float(avg_entry) * 1.10)
                                   if not is_short
                                   else (float(current_price) <= float(avg_entry) * 0.90),
            "current_stop_price": stop_price,
            "total_shares_held": int(abs(qty_f)),
            "shares_shorted": int(abs(qty_f)) if is_short else 0,
            "ladder_fills": [],
            "profit_takes": [],
        },
    }
    return strategy


def place_stop_loss_order(symbol, qty, stop_price, side="sell"):
    """Place a stop-loss order via Alpaca API.
    Round-10: caller passes `side` so a short orphan gets a buy-stop
    ABOVE entry (correct). Also idempotent via client_order_id so a
    timeout-retry in the outer caller doesn't duplicate."""
    order_data = {
        "symbol": symbol,
        "qty": str(int(float(qty))),
        "side": side,
        "type": "stop",
        "stop_price": str(round(stop_price, 2)),
        "time_in_force": "gtc",
        "client_order_id": f"recovery-stop-{symbol}-{side}-{now_et().strftime('%Y%m%d')}",
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
    positions = api_get_with_retry(f"{API_ENDPOINT}/positions")
    if isinstance(positions, dict) and "error" in positions:
        print(f"  ERROR: Could not fetch positions: {positions['error']}")
        print("  Cannot proceed without position data. Exiting.")
        return
    if not isinstance(positions, list):
        positions = []
    print(f"  Found {len(positions)} open positions")

    # Fetch all open orders once for grace period checks
    print("Fetching open orders...")
    all_open_orders = api_get_with_retry(f"{API_ENDPOINT}/orders?status=open&limit=500")
    if not isinstance(all_open_orders, list):
        all_open_orders = []

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
    orphans_found = []
    for sym, pos in position_map.items():
        # Round-25: if this is an option contract (OCC symbol like
        # CHWY260515P00025000), the matching wheel strategy file is
        # keyed off the UNDERLYING (CHWY), not the OCC symbol. Without
        # this mapping, every short-put / covered-call position looks
        # like an orphan and the bot emails a false-positive alert.
        lookup_sym = sym
        if _is_occ_option_symbol(sym):
            underlying = _occ_underlying(sym)
            if underlying and underlying in strategy_symbol_map:
                # Wheel file exists for the underlying — not an orphan.
                continue
            # No wheel file either — keep lookup as underlying so the
            # "orphan" path at least reports a sensible symbol.
            if underlying:
                lookup_sym = underlying
        if lookup_sym not in strategy_symbol_map:
            # Grace period: skip if there are pending buy orders for this symbol
            recent_orders = [o for o in all_open_orders if o.get("symbol") == sym and o.get("side") == "buy"]
            if recent_orders:
                print(f"  {sym}: Has pending buy orders, skipping orphan check")
                continue
            orphans.append((sym, pos))

    if not orphans:
        print("  No orphan positions found.")
    else:
        for sym, pos in orphans:
            issues_found += 1
            orphans_found.append(sym)
            qty = pos.get("qty", 0)
            avg_entry = float(pos.get("avg_entry_price", 0))
            current_price = float(pos.get("current_price", 0))
            qty_f = float(qty)
            is_short = qty_f < 0

            # Round-10: skip if a wheel strategy already manages this
            # symbol's shares. Creating a trailing_stop alongside would
            # race the wheel's covered-call logic on the same 100 shares.
            wheel_fname = f"wheel_{sym}.json"
            wheel_fpath = os.path.join(STRATEGIES_DIR, wheel_fname)
            if os.path.exists(wheel_fpath):
                try:
                    with open(wheel_fpath) as f:
                        import json as _json
                        wstate = _json.load(f)
                    if str(wstate.get("stage", "")).startswith("stage_2_"):
                        print(f"\n  {sym}: Skipping orphan — wheel owns these shares (stage_2)")
                        continue
                except Exception:
                    pass

            print(f"\n  ORPHAN: {sym} ({qty} shares, entry ${avg_entry:.2f}, current ${current_price:.2f})")
            strategy = create_orphan_strategy(sym, qty, current_price, avg_entry)
            strat_prefix = "short_sell" if is_short else "trailing_stop"
            fname = f"{strat_prefix}_{sym}.json"
            fpath = os.path.join(STRATEGIES_DIR, fname)
            safe_save_json(fpath, strategy)
            print(f"    FIXED: Created {fname} with stop at ${strategy['state']['current_stop_price']:.2f}")
            # Round-61 user-reported: a SOXL orphan recovery created the
            # strategy file but NOT a trade-journal open entry. When the
            # position later closed via stop-trigger, record_trade_close
            # couldn't find a matching open → fell into the synthetic
            # orphan_close branch → dashboard tagged the close as
            # "[orphan]". Fix: append an open journal entry alongside
            # the strategy file so the close can be paired. Best-effort;
            # never block recovery on a journal-write failure.
            try:
                journal_path = os.path.join(DATA_DIR, "trade_journal.json")
                _journal = {}
                if os.path.exists(journal_path):
                    try:
                        with open(journal_path) as _jf:
                            _journal = json.load(_jf) or {}
                    except (OSError, ValueError):
                        _journal = {}
                if not isinstance(_journal, dict):
                    _journal = {}
                _journal.setdefault("trades", [])
                _journal["trades"].append({
                    "timestamp": now_et().isoformat(),
                    "symbol": sym,
                    "side": "sell_short" if is_short else "buy",
                    "qty": int(abs(float(qty))),
                    "price": float(avg_entry),
                    "strategy": strat_prefix,
                    "reason": ("Backfilled by error_recovery — position "
                                "existed in Alpaca without a journal entry "
                                "(likely from a pre-R61 deploy or a manual "
                                "fill). Tagged auto_recovered=True."),
                    "deployer": "error_recovery",
                    "status": "open",
                    "auto_recovered": True,
                })
                safe_save_json(journal_path, _journal)
                print(f"    JOURNAL: recorded open entry for {sym} so a "
                      f"later close can pair (prevents future [orphan] tag)")
            except Exception as _je:
                print(f"    Warning: journal open-write failed for {sym}: {_je}")
            issues_fixed += 1
            strategy_symbol_map[sym] = (fname, {
                "path": fpath,
                "data": strategy,
                "symbol": sym,
                "strategy": strat_prefix,
                "status": "active",
            })

    # Send notification for orphans found
    if orphans_found:
        try:
            subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "notify.py"), "--type", "alert",
                f"Error recovery found {len(orphans_found)} orphan positions: {', '.join(orphans_found)}"])
        except Exception as e:
            print(f"  Warning: Could not send orphan notification: {e}")

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

            is_short = qty < 0
            if is_short:
                stop_price = round(avg_entry * (1 + stop_loss_pct), 2)
                side = "buy"
                arrow = "above entry (short cover)"
            else:
                stop_price = round(avg_entry * (1 - stop_loss_pct), 2)
                side = "sell"
                arrow = "below entry"
            print(f"\n  MISSING STOP: {sym} ({int(abs(qty))} shares {'short' if is_short else 'long'}, entry ${avg_entry:.2f})")
            print(f"    Placing stop-loss at ${stop_price:.2f} ({stop_loss_pct*100:.0f}% {arrow})")

            result = place_stop_loss_order(sym, abs(qty), stop_price, side=side)
            if isinstance(result, dict) and "error" not in result:
                order_id = result.get("id", "unknown")
                print(f"    FIXED: Stop-loss order placed (order_id: {order_id})")

                # Update strategy file with stop order ID
                if strat_data.get("state"):
                    strat_data["state"]["stop_order_id"] = order_id
                    strat_data["state"]["current_stop_price"] = stop_price
                    safe_save_json(info["path"], strat_data)
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
                # Grace period: don't mark as stale if file was modified in the last 10 minutes
                filepath = info["path"]
                try:
                    file_mtime = os.path.getmtime(filepath)
                    if time.time() - file_mtime < 600:  # 10 minutes
                        print(f"  {sym}: Strategy file recently modified, skipping stale check")
                        continue
                except OSError:
                    pass

                # Check if there are open sell orders (position might have just been sold)
                sell_orders = [o for o in all_open_orders if o.get("symbol") == sym and o.get("side") == "sell"]
                if sell_orders:
                    print(f"  {sym}: Has pending sell orders, skipping stale check")
                    continue

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
            strat_data["closed_at"] = now_et().isoformat()
            safe_save_json(info["path"], strat_data)
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
