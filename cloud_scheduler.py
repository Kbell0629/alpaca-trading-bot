#!/usr/bin/env python3
"""
Cloud-native scheduler for the Alpaca trading bot — MULTI-USER.
Runs as a background thread in server.py, replacing Claude Code scheduled tasks.
All trading logic runs on Railway 24/7 without needing user's laptop.

For each active user in the auth DB (with valid Alpaca creds), runs:
  - Screener (every 30 min during market hours)
  - Strategy monitor (every 60s during market hours)
  - Auto-deployer (weekdays 9:35 AM ET)
  - Daily close (weekdays 4:05 PM ET)
  - Weekly learning (Fridays 5:00 PM ET)
  - Friday risk reduction (Fridays 3:45 PM ET)
  - Monthly rebalance (first trading day, 9:45 AM ET)

Falls back to env-var single-user mode if auth module is unavailable.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent runtime data lives. On Railway, set to a volume
# mount path (e.g. /data). Locally defaults to BASE_DIR.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
STRATEGIES_DIR = os.path.join(DATA_DIR, "strategies")  # legacy / fallback

# Legacy env vars (used only when auth module is unavailable)
API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
DATA_ENDPOINT = os.environ.get("ALPACA_DATA_ENDPOINT", "https://data.alpaca.markets/v2")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")

try:
    import auth
    AUTH_AVAILABLE = True
except ImportError:
    AUTH_AVAILABLE = False

_scheduler_thread = None
_scheduler_running = False
_last_runs = {}
_recent_logs = []  # Circular buffer for dashboard display
_logs_lock = threading.Lock()

# ET is the canonical timezone for this app — the US markets run in ET,
# the user is in ET, and there is no reason for UTC to surface anywhere in
# logs, storage, or UI. zoneinfo handles EDT/EST transitions automatically.
try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone.utc  # safety fallback, extremely unlikely in modern Python


def now_et():
    """Timezone-aware ET datetime — the ONE canonical 'now' for this app.

    Stored ISO strings include an offset ("-04:00" in EDT, "-05:00" in EST)
    so they compare correctly against legacy UTC-stored ISO strings with
    "+00:00". Use this anywhere you previously reached for
    now_et() or datetime.utcnow().
    """
    return datetime.now(ET_TZ)


def log(msg, task="scheduler"):
    # ET-only. Railway log cross-reference still works because Railway's
    # own log timestamps are independent of the string we emit.
    now = now_et()
    et_ts = now.strftime("%-I:%M:%S %p ") + (now.tzname() or "ET")
    line = f"[{et_ts}] [{task}] {msg}"
    print(line, flush=True)
    with _logs_lock:
        _recent_logs.append({"ts": et_ts, "ts_iso": now.isoformat(), "task": task, "msg": msg})
        if len(_recent_logs) > 100:
            _recent_logs.pop(0)

# ============================================================================
# MULTI-USER CONTEXT HELPERS
# ============================================================================
def get_all_users_for_scheduling():
    """Return all active users with valid Alpaca credentials.
    Falls back to env var single-user mode if auth not available or no users.
    Each user dict has the private keys we need to run tasks on their behalf.
    """
    if AUTH_AVAILABLE:
        try:
            users = auth.list_active_users()
        except Exception as e:
            log(f"auth.list_active_users failed: {e}. Falling back to env mode.", "scheduler")
            users = []
        result = []
        for u in users:
            try:
                creds = auth.get_user_alpaca_creds(u["id"])
            except Exception as e:
                log(f"get_user_alpaca_creds failed for user {u.get('id')}: {e}", "scheduler")
                continue
            if not creds or not creds.get("key") or not creds.get("secret"):
                continue
            user_dir = auth.user_data_dir(u["id"])
            result.append({
                "id": u["id"],
                "username": u["username"],
                "_api_key": creds["key"],
                "_api_secret": creds["secret"],
                "_api_endpoint": creds.get("endpoint") or "https://paper-api.alpaca.markets/v2",
                "_data_endpoint": creds.get("data_endpoint") or "https://data.alpaca.markets/v2",
                "_ntfy_topic": creds.get("ntfy_topic") or f"alpaca-bot-{u['username'].lower()}",
                "_data_dir": user_dir,
                "_strategies_dir": os.path.join(user_dir, "strategies"),
            })
        if result:
            return result

    # Env-var fallback (single-user legacy mode)
    if API_KEY and API_SECRET:
        return [{
            "id": "env",
            "username": os.environ.get("DASHBOARD_USER", "admin"),
            "_api_key": API_KEY,
            "_api_secret": API_SECRET,
            "_api_endpoint": API_ENDPOINT,
            "_data_endpoint": DATA_ENDPOINT,
            "_ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
            "_data_dir": DATA_DIR,              # legacy uses DATA_DIR (was BASE_DIR)
            "_strategies_dir": STRATEGIES_DIR,  # legacy strategies dir
        }]
    return []

def _user_headers(user):
    return {
        "APCA-API-KEY-ID": user["_api_key"],
        "APCA-API-SECRET-KEY": user["_api_secret"],
    }

# Per-user circuit breaker for Alpaca. After CB_OPEN_THRESHOLD consecutive
# failures, the breaker OPENs for CB_OPEN_SECONDS — additional calls return
# the cached error immediately without touching the network. This prevents
# all users from generating a retry storm against an Alpaca outage and
# burning API quota / bandwidth. Reset on first successful response.
_cb_state = {}            # user_id -> {"fails": int, "open_until": timestamp}
_CB_OPEN_THRESHOLD = 5    # consecutive failures before tripping
_CB_OPEN_SECONDS = 300    # 5-minute cool-off once tripped


def _cb_key(user):
    return user.get("id", "env")


def _cb_blocked(user):
    st = _cb_state.get(_cb_key(user))
    if not st:
        return False
    if st.get("open_until", 0) > time.time():
        return True
    # Cool-off elapsed — reset and allow a probe
    _cb_state.pop(_cb_key(user), None)
    return False


def _cb_record_failure(user):
    key = _cb_key(user)
    st = _cb_state.setdefault(key, {"fails": 0, "open_until": 0})
    st["fails"] += 1
    if st["fails"] >= _CB_OPEN_THRESHOLD:
        st["open_until"] = time.time() + _CB_OPEN_SECONDS
        log(f"[{user.get('username','?')}] Alpaca circuit breaker OPEN "
            f"({st['fails']} consecutive failures). Cooling off "
            f"{_CB_OPEN_SECONDS}s.", "api")


def _cb_record_success(user):
    _cb_state.pop(_cb_key(user), None)


def user_api_get(user, url_path, timeout=10, retries=2):
    """GET from this user's Alpaca endpoint with exponential backoff on
    transient errors (502/503/504/timeout). Each retry waits 0.5s, 1s, 2s.
    Returns {"error": ...} after final retry failure.

    Per-user circuit breaker: after 5 consecutive failures, fast-fails for
    5 minutes so an Alpaca outage doesn't snowball into a retry storm
    against the same dead endpoint for every scheduler tick.
    """
    if _cb_blocked(user):
        return {"error": "circuit_breaker_open"}

    if url_path.startswith("http"):
        url = url_path
    else:
        if "/stocks/" in url_path or "/options/" in url_path or "/news" in url_path:
            url = user["_data_endpoint"] + url_path
        else:
            url = user["_api_endpoint"] + url_path
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=_user_headers(user))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _cb_record_success(user)
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # Retry on 5xx; don't retry on 4xx (bad request, auth, rate limit)
            if 500 <= e.code < 600 and attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                last_err = e
                continue
            if 500 <= e.code < 600:
                _cb_record_failure(user)
            return {"error": f"HTTP {e.code}"}
        except Exception as e:
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                last_err = e
                continue
            _cb_record_failure(user)
            return {"error": "Request failed"}
    _cb_record_failure(user)
    return {"error": "Request failed after retries"}

def user_api_post(user, url_path, data, timeout=10):
    if url_path.startswith("http"):
        url = url_path
    else:
        url = user["_api_endpoint"] + url_path
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={**_user_headers(user), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def user_api_delete(user, url_path, timeout=10):
    if url_path.startswith("http"):
        url = url_path
    else:
        url = user["_api_endpoint"] + url_path
    req = urllib.request.Request(url, headers=_user_headers(user), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode()) if body else {}
    except Exception as e:
        return {"error": str(e)}

def user_file(user, filename):
    """Return path to a user-scoped data file.

    CRITICAL: Migration from shared DATA_DIR is RESTRICTED to user_id=1
    (the bootstrap admin). Other users must never inherit another user's
    config — previously caused cross-user auto-trading on signup.
    """
    path = os.path.join(user["_data_dir"], filename)
    if not os.path.exists(path) and user.get("id") == 1:
        shared = os.path.join(DATA_DIR, filename)
        if os.path.exists(shared) and shared != path:
            try:
                import shutil
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                shutil.copy2(shared, path)
                log(f"[migration] Copied {filename} to bootstrap admin dir", "scheduler")
            except Exception as e:
                log(f"[migration] WARN failed to migrate {filename}: {e}", "scheduler")
    return path


def user_strategies_dir(user):
    d = user["_strategies_dir"]
    first_time = not os.path.isdir(d)
    os.makedirs(d, exist_ok=True)
    # Strategy seed ONLY for bootstrap admin (user_id=1). New users start
    # clean — no inherited strategies from other accounts.
    if first_time and user.get("id") == 1:
        try:
            shared = STRATEGIES_DIR
            if os.path.isdir(shared) and shared != d:
                import shutil
                for f in os.listdir(shared):
                    if f.endswith(".json"):
                        src = os.path.join(shared, f)
                        dst = os.path.join(d, f)
                        if os.path.isfile(src) and not os.path.exists(dst):
                            shutil.copy2(src, dst)
                log(f"[migration] Seeded strategies dir for bootstrap admin", "scheduler")
        except Exception as e:
            log(f"[migration] WARN strategies seed failed: {e}", "scheduler")
    return d

# ============================================================================
# GENERIC FILE HELPERS
# ============================================================================
def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        # Corrupt JSON file deserves attention — scheduler might be making
        # decisions off stale/missing state. Log loudly but don't crash.
        log(f"load_json: MALFORMED {path}: {e}. Returning None.", "scheduler")
        return None
    except Exception as e:
        log(f"load_json: unexpected error reading {path}: {e}", "scheduler")
        return None

def save_json(path, data):
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ============================================================================
# NOTIFICATIONS
# ============================================================================
def notify_user(user, message, notify_type="info"):
    """Send a notification tagged with the user's ntfy topic."""
    try:
        env = os.environ.copy()
        if user.get("_ntfy_topic"):
            env["NTFY_TOPIC"] = user["_ntfy_topic"]
        subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "notify.py"),
             "--type", notify_type, message],
            env=env,
        )
    except Exception as e:
        log(f"Notification failed: {e}")

def notify_user_global(message, notify_type="info"):
    """Global notification — routed to first user's ntfy topic (or env fallback)."""
    users = get_all_users_for_scheduling()
    if users:
        notify_user(users[0], message, notify_type)
    else:
        # No users — best effort with env-var topic
        try:
            subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, "notify.py"),
                 "--type", notify_type, message]
            )
        except Exception as e:
            log(f"Global notification failed: {e}")

def get_et_time():
    """Return current Eastern time as a naive datetime, handling DST correctly.

    Previous implementation used a month-boundary heuristic that was off by
    one hour during the DST transition gaps (Mar 1 to 2nd Sunday, and first
    Sunday of Nov to Nov 30). zoneinfo uses the tzdata rules and correctly
    handles DST changes.
    """
    try:
        from zoneinfo import ZoneInfo
        et = datetime.now(ZoneInfo("America/New_York"))
        # Strip tzinfo so existing callers comparing to tz-naive values still work.
        return et.replace(tzinfo=None)
    except Exception:
        # Fallback if zoneinfo/tzdata isn't available (very old Python or
        # stripped container). Month-boundary heuristic is ~99% right.
        now_utc = now_et()
        month = now_utc.month
        offset = -4 if 3 <= month <= 11 else -5
        return (now_utc + timedelta(hours=offset)).replace(tzinfo=None)

# ============================================================================
# TASK 1: SCREENER (per user)
# ============================================================================
def run_screener(user, max_age_seconds=0):
    """Run the stock screener for ONE user. Uses user's Alpaca creds via env vars
    passed to the update_dashboard.py subprocess. Output is written to the user's
    data dir (if update_dashboard.py supports DASHBOARD_DATA_PATH/_HTML_PATH env
    vars); otherwise it falls back to writing to BASE_DIR (legacy behavior).
    """
    key = f"screener_{user['id']}"
    if max_age_seconds > 0:
        last = _last_runs.get(key, 0)
        if isinstance(last, (int, float)) and time.time() - last < max_age_seconds:
            age = int(time.time() - last)
            log(f"[{user['username']}] Screener data is {age}s old (< {max_age_seconds}s). Skipping duplicate run.", "screener")
            return
    log(f"[{user['username']}] Starting screener...", "screener")
    env = os.environ.copy()
    env["ALPACA_API_KEY"] = user["_api_key"]
    env["ALPACA_API_SECRET"] = user["_api_secret"]
    env["ALPACA_ENDPOINT"] = user["_api_endpoint"]
    env["ALPACA_DATA_ENDPOINT"] = user["_data_endpoint"]
    # Pass preferred output paths; update_dashboard.py may or may not honor them.
    env["DASHBOARD_DATA_PATH"] = user_file(user, "dashboard_data.json")
    env["DASHBOARD_HTML_PATH"] = user_file(user, "dashboard.html")
    # Timeout: 600s (10 min) — Railway containers have slower network than local.
    # Screening 10k+ stocks in 22 batches of 500 can take 3-8 min on Railway vs ~60s local.
    # TODO: optimize screener to be faster (reduce symbols scanned, parallel batches)
    SCREENER_TIMEOUT = 600
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "update_dashboard.py")],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=SCREENER_TIMEOUT, env=env,
        )
        if result.returncode == 0:
            log(f"[{user['username']}] Screener completed", "screener")
            _last_runs[key] = time.time()
        else:
            log(f"[{user['username']}] Screener failed: {result.stderr[:200]}", "screener")
    except subprocess.TimeoutExpired:
        log(f"[{user['username']}] Screener timed out (>{SCREENER_TIMEOUT}s)", "screener")
    except Exception as e:
        log(f"[{user['username']}] Screener error: {e}", "screener")

# ============================================================================
# Journal writeback helper — CRITICAL for scorecard/readiness/learning
# ============================================================================
# Previously every exit (stop fill, target hit, short cover, profit ladder
# final sell) updated the per-symbol strategy file with exit details but
# NEVER wrote back to trade_journal.json. Result: update_scorecard never
# saw any closed trades → win_rate stayed 0, Sharpe ≈0, readiness capped
# at ~40, learn.py never adjusted weights.
#
# This helper finds the most recent OPEN journal entry for the symbol
# and marks it closed with P&L + exit details.
def record_trade_close(user, symbol, strategy, exit_price, pnl, exit_reason,
                        qty=None, side="sell"):
    """Mark the matching open journal entry as closed. Idempotent:
    if the entry is already closed, does nothing.
    """
    try:
        journal_path = user_file(user, "trade_journal.json")
        journal = load_json(journal_path) or {"trades": [], "daily_snapshots": []}
        trades = journal.get("trades", [])
        # Walk newest->oldest; find the most recent OPEN entry for this symbol+strategy
        for t in reversed(trades):
            if (t.get("symbol") == symbol
                    and t.get("strategy") == strategy
                    and t.get("status", "open") == "open"):
                t["status"] = "closed"
                t["exit_timestamp"] = now_et().isoformat()
                t["exit_price"] = round(float(exit_price), 4) if exit_price else None
                t["exit_reason"] = exit_reason
                try:
                    t["pnl"] = round(float(pnl), 2)
                except (TypeError, ValueError):
                    t["pnl"] = None
                # P&L % relative to entry price
                try:
                    entry_px = float(t.get("price") or 0)
                    t_qty = int(float(qty if qty is not None else t.get("qty") or 0))
                    if entry_px and t_qty:
                        if side == "sell":  # long close
                            t["pnl_pct"] = round((float(exit_price) / entry_px - 1) * 100, 2)
                        else:  # short cover
                            t["pnl_pct"] = round((entry_px / float(exit_price) - 1) * 100, 2)
                except Exception:
                    pass
                t["exit_side"] = side
                save_json(journal_path, journal)
                return True
        return False  # No matching open trade found
    except Exception as e:
        log(f"[{user.get('username','?')}] Journal close writeback failed for {symbol}: {e}", "monitor")
        return False


# ============================================================================
# TASK 2: STRATEGY MONITOR (per user)
# ============================================================================
def monitor_strategies(user):
    try:
        guardrails_path = user_file(user, "guardrails.json")
        guardrails = load_json(guardrails_path) or {}
        if guardrails.get("kill_switch"):
            return

        # Daily loss check
        account = user_api_get(user, "/account")
        if isinstance(account, dict) and "error" not in account:
            current_val = float(account.get("portfolio_value", 0))
            daily_start = guardrails.get("daily_starting_value")
            # Fallback: if auto-deployer never ran to set daily_starting_value
            # (disabled, kill switch was on, cooldown, etc.), set it now from
            # current value so the kill-switch safety is always armed.
            if not daily_start and current_val > 0:
                guardrails["daily_starting_value"] = current_val
                save_json(guardrails_path, guardrails)
                daily_start = current_val
            if daily_start:
                loss_pct = (daily_start - current_val) / daily_start
                if loss_pct > guardrails.get("daily_loss_limit_pct", 0.03):
                    guardrails["kill_switch"] = True
                    guardrails["kill_switch_triggered_at"] = now_et().isoformat()
                    guardrails["kill_switch_reason"] = f"Daily loss {loss_pct*100:.1f}%"
                    save_json(guardrails_path, guardrails)
                    user_api_delete(user, "/orders")
                    user_api_delete(user, "/positions")
                    notify_user(user, f"KILL SWITCH: Daily loss {loss_pct*100:.1f}% exceeded.", "kill")
                    log(f"[{user['username']}] KILL SWITCH triggered: {loss_pct*100:.1f}% loss", "monitor")
                    return

        sdir = user_strategies_dir(user)
        if not os.path.isdir(sdir):
            return
        for fname in os.listdir(sdir):
            if not fname.endswith(".json"):
                continue
            if fname in ("copy_trading.json", "wheel_strategy.json"):
                continue
            filepath = os.path.join(sdir, fname)
            strat = load_json(filepath)
            if not strat:
                continue
            status = strat.get("status", "")
            symbol = strat.get("symbol")
            if status not in ("active", "awaiting_fill") or not symbol:
                continue
            try:
                process_strategy_file(user, filepath, strat)
            except Exception as e:
                log(f"[{user['username']}] Error processing {fname}: {e}", "monitor")
    except Exception as e:
        log(f"[{user['username']}] Monitor error: {e}", "monitor")

def check_profit_ladder(user, filepath, strat, price, entry, shares):
    """Sell 25% at each profit target: +10%, +20%, +30%, +50%.

    Modifies strat['state']['profit_takes'] to track which levels have been hit.
    """
    if shares <= 0 or not entry:
        return

    profit_pct = (price / entry - 1) * 100
    state = strat.setdefault("state", {})  # ensure mutations persist in strat
    takes = state.get("profit_takes", []) or []
    symbol = strat["symbol"]
    initial_qty = strat.get("initial_qty") or shares

    targets = [
        {"level": 10, "pct": 0.25, "note": "First target: lock in early gains"},
        {"level": 20, "pct": 0.25, "note": "Second target: take more off the table"},
        {"level": 30, "pct": 0.25, "note": "Third target: secure majority profit"},
        {"level": 50, "pct": 0.25, "note": "Final target: let remainder ride"},
    ]

    for target in targets:
        level = target["level"]
        if level in takes:
            continue  # Already taken this level
        if profit_pct < level:
            continue

        # Sell 25% of ORIGINAL position at this level
        sell_qty = max(1, int(initial_qty * target["pct"]))
        sell_qty = min(sell_qty, shares)  # Can't sell more than we have

        if sell_qty < 1:
            continue

        # Idempotency: client_order_id lets Alpaca reject duplicate orders if
        # a prior attempt hit the server but the response was lost (timeout /
        # 504). Without this, the next monitor tick would re-enter and place
        # a second 25% sell at the same level — double-sell bug.
        # Uses ET trading-day so two rungs on the same session share a key.
        today_str = now_et().strftime("%Y%m%d")
        client_order_id = f"ladder-{symbol}-L{level}-{today_str}"
        order = user_api_post(user, "/orders", {
            "symbol": symbol, "qty": str(sell_qty), "side": "sell",
            "type": "market", "time_in_force": "day",
            "client_order_id": client_order_id,
        })

        # Alpaca returns 422 with "client order id already exists" if dedup
        # hit. Treat that as "this level was already taken on a prior tick"
        # — mark it in state so we don't keep retrying.
        if isinstance(order, dict) and "error" in order:
            err = str(order.get("error", "")).lower()
            if "client_order_id" in err or "already exists" in err:
                log(f"[{user['username']}] {symbol}: ladder level {level} already filled (idempotency dedup)", "monitor")
                takes.append(level)
                state["profit_takes"] = takes
                strat["state"] = state
                save_json(filepath, strat)
                continue  # skip; this level is already done

        if isinstance(order, dict) and "id" in order:
            takes.append(level)
            state["profit_takes"] = takes
            remaining = shares - sell_qty
            state["total_shares_held"] = remaining

            # CRITICAL: resize the protective stop to match remaining shares,
            # otherwise if the stop triggers it sells MORE than we hold and
            # Alpaca will reject or (with short-enabled accounts) open a short.
            #
            # Order: PLACE NEW FIRST, then cancel OLD on success. This avoids
            # a window where the position is unprotected between cancel and
            # re-place. If the new stop fails, we KEEP the old stop (which is
            # now oversized but still protective — Alpaca will just reject if
            # the remaining share qty is too low at trigger time).
            old_stop_id = state.get("stop_order_id")
            current_stop_price = state.get("current_stop_price")
            if old_stop_id and remaining > 0 and current_stop_price:
                new_stop = user_api_post(user, "/orders", {
                    "symbol": symbol, "qty": str(remaining), "side": "sell",
                    "type": "stop", "stop_price": str(current_stop_price),
                    "time_in_force": "gtc"
                })
                if isinstance(new_stop, dict) and "id" in new_stop:
                    # New stop placed — safe to cancel old
                    user_api_delete(user, f"/orders/{old_stop_id}")
                    state["stop_order_id"] = new_stop["id"]
                else:
                    # Keep old oversized stop as-is; log and retry next tick.
                    log(f"[{user['username']}] {symbol}: WARN stop resize failed after profit-take — keeping old oversized stop. Err: {new_stop}", "monitor")
            elif old_stop_id and remaining <= 0:
                user_api_delete(user, f"/orders/{old_stop_id}")
                state["stop_order_id"] = None

            # Ensure state is attached to strat before save (defensive)
            strat["state"] = state
            save_json(filepath, strat)
            log(f"[{user['username']}] {symbol}: Profit take level {level}% — sold {sell_qty} shares", "monitor")
            notify_user(user, f"Profit take on {symbol} at +{level}%: sold {sell_qty} shares. {remaining} still held.", "exit")
            return  # One level per check

def process_short_strategy(user, filepath, strat, state, rules):
    """Manage a short_sell position (inverse logic — we profit when price falls)."""
    symbol = strat["symbol"]

    # Check entry fill — shares_shorted is positive magnitude, Alpaca reports negative qty
    entry_order_id = state.get("entry_order_id")
    if entry_order_id and not state.get("entry_fill_price"):
        order = user_api_get(user, f"/orders/{entry_order_id}")
        if isinstance(order, dict) and order.get("status") == "filled":
            state["entry_fill_price"] = float(order.get("filled_avg_price", 0))
            qty = int(float(order.get("filled_qty", 0)))
            state["shares_shorted"] = qty
            state["total_shares_held"] = qty  # Keep magnitude for consistency
            strat["status"] = "active"
            log(f"[{user['username']}] {symbol}: SHORT entry filled at ${state['entry_fill_price']:.2f}", "monitor")

    entry = state.get("entry_fill_price")
    shares = state.get("shares_shorted", 0)
    if not entry or shares <= 0:
        save_json(filepath, strat)
        return

    # Get current price
    trade = user_api_get(user, f"/stocks/{symbol}/trades/latest?feed=iex")
    if not isinstance(trade, dict) or "trade" not in trade:
        save_json(filepath, strat)
        return
    price = trade["trade"].get("p", 0)
    if not price:
        save_json(filepath, strat)
        return

    # Place initial stop-buy (cover) ABOVE entry — closes short if price rises
    if not state.get("cover_order_id"):
        stop_pct = rules.get("stop_loss_pct", 0.08)
        stop_price = round(entry * (1 + stop_pct), 2)
        order = user_api_post(user, "/orders", {
            "symbol": symbol, "qty": str(shares), "side": "buy",
            "type": "stop", "stop_price": str(stop_price), "time_in_force": "gtc"
        })
        if isinstance(order, dict) and "id" in order:
            state["cover_order_id"] = order["id"]
            state["current_stop_price"] = stop_price
            log(f"[{user['username']}] {symbol}: SHORT cover-stop placed at ${stop_price}", "monitor")
            notify_user(user, f"Short cover-stop on {symbol} at ${stop_price:.2f}", "info")

    # Place profit target (limit buy below entry)
    if not state.get("target_order_id"):
        target_pct = rules.get("profit_target_pct", 0.15)
        target_price = round(entry * (1 - target_pct), 2)
        order = user_api_post(user, "/orders", {
            "symbol": symbol, "qty": str(shares), "side": "buy",
            "type": "limit", "limit_price": str(target_price), "time_in_force": "gtc"
        })
        if isinstance(order, dict) and "id" in order:
            state["target_order_id"] = order["id"]
            state["current_target_price"] = target_price
            log(f"[{user['username']}] {symbol}: SHORT target-buy placed at ${target_price}", "monitor")

    # Trailing stop for shorts: track LOWEST price, lower the stop as price falls
    lowest = state.get("lowest_price_seen") or entry
    if price < lowest:
        state["lowest_price_seen"] = price
        lowest = price
        activation_pct = rules.get("short_trail_activation_pct", 0.05)
        trail_pct = rules.get("short_trail_distance_pct", 0.05)
        if not state.get("trailing_activated") and lowest <= entry * (1 - activation_pct):
            state["trailing_activated"] = True
            log(f"[{user['username']}] {symbol}: SHORT trailing activated", "monitor")
        if state.get("trailing_activated"):
            new_stop = round(lowest * (1 + trail_pct), 2)
            current_stop = state.get("current_stop_price", 99999) or 99999
            # For shorts: stop moves DOWN as price falls (locks in gains)
            if new_stop < current_stop:
                old_id = state.get("cover_order_id")
                new_order = user_api_post(user, "/orders", {
                    "symbol": symbol, "qty": str(shares), "side": "buy",
                    "type": "stop", "stop_price": str(new_stop), "time_in_force": "gtc"
                })
                if isinstance(new_order, dict) and "id" in new_order:
                    if old_id:
                        user_api_delete(user, f"/orders/{old_id}")
                    state["cover_order_id"] = new_order["id"]
                    state["current_stop_price"] = new_stop
                    log(f"[{user['username']}] {symbol}: SHORT stop lowered ${current_stop:.2f} -> ${new_stop:.2f}", "monitor")
                    notify_user(user, f"Short stop tightened on {symbol}: ${current_stop:.2f} -> ${new_stop:.2f}", "info")

    # Check if cover (stop) triggered — loss scenario
    if state.get("cover_order_id"):
        order = user_api_get(user, f"/orders/{state['cover_order_id']}")
        if isinstance(order, dict) and order.get("status") == "filled":
            cover_price = float(order.get("filled_avg_price", state.get("current_stop_price", 0)))
            pnl = (entry - cover_price) * shares  # Short profit = entry - cover
            state["total_shares_held"] = 0
            state["shares_shorted"] = 0
            state["cover_order_id"] = None
            # Cancel target order too
            if state.get("target_order_id"):
                user_api_delete(user, f"/orders/{state['target_order_id']}")
                state["target_order_id"] = None
            state["exit_price"] = cover_price
            state["exit_reason"] = "short_stop_covered"
            strat["status"] = "closed"
            log(f"[{user['username']}] {symbol}: SHORT stopped out at ${cover_price}. P&L ${pnl:.2f}", "monitor")
            notify_user(user, f"{symbol} short covered at ${cover_price:.2f}. P&L: ${pnl:.2f}", "stop")
            # Writeback to journal so scorecard + learning see the close
            record_trade_close(user, symbol, strat.get("strategy", "short_sell"),
                                cover_price, pnl, "short_stop_covered", qty=shares, side="buy")
            # Record short loss for cooldown
            if pnl < 0:
                gpath = user_file(user, "guardrails.json")
                guardrails = load_json(gpath) or {}
                guardrails["last_short_loss_time"] = now_et().isoformat()
                save_json(gpath, guardrails)

    # Check if target hit — profit scenario
    elif state.get("target_order_id"):
        order = user_api_get(user, f"/orders/{state['target_order_id']}")
        if isinstance(order, dict) and order.get("status") == "filled":
            exit_price = float(order.get("filled_avg_price", state.get("current_target_price", 0)))
            pnl = (entry - exit_price) * shares
            state["total_shares_held"] = 0
            state["shares_shorted"] = 0
            state["target_order_id"] = None
            # Cancel cover stop
            if state.get("cover_order_id"):
                user_api_delete(user, f"/orders/{state['cover_order_id']}")
                state["cover_order_id"] = None
            state["exit_price"] = exit_price
            state["exit_reason"] = "short_target_hit"
            strat["status"] = "closed"
            log(f"[{user['username']}] {symbol}: SHORT target hit at ${exit_price}. P&L ${pnl:.2f}", "monitor")
            notify_user(user, f"Short profit on {symbol}: covered at ${exit_price:.2f}. P&L: ${pnl:.2f}", "exit")
            record_trade_close(user, symbol, strat.get("strategy", "short_sell"),
                                exit_price, pnl, "short_target_hit", qty=shares, side="buy")

    # Force cover after max hold days (prevent indefinite short exposure)
    max_hold = rules.get("max_hold_days", 14)
    try:
        created = strat.get("created", "")[:10]
        if created:
            age_days = (now_et().date() - datetime.strptime(created, "%Y-%m-%d").date()).days
            if age_days >= max_hold and shares > 0:
                log(f"[{user['username']}] {symbol}: SHORT held {age_days} days, forcing cover", "monitor")
                order = user_api_post(user, "/orders", {
                    "symbol": symbol, "qty": str(shares), "side": "buy",
                    "type": "market", "time_in_force": "day"
                })
                if isinstance(order, dict) and "id" in order:
                    # Cancel pending orders
                    if state.get("cover_order_id"):
                        user_api_delete(user, f"/orders/{state['cover_order_id']}")
                    if state.get("target_order_id"):
                        user_api_delete(user, f"/orders/{state['target_order_id']}")
                    strat["status"] = "closed"
                    state["exit_reason"] = "max_hold_exceeded"
                    pnl = (entry - price) * shares
                    state["exit_price"] = price
                    notify_user(user, f"Short on {symbol} force-covered after {age_days} days. P&L ~${pnl:.2f}", "info")
                    record_trade_close(user, symbol, strat.get("strategy", "short_sell"),
                                        price, pnl, "max_hold_exceeded", qty=shares, side="buy")
    except Exception as e:
        log(f"[{user['username']}] {symbol}: Short age check error: {e}", "monitor")

    save_json(filepath, strat)


def process_strategy_file(user, filepath, strat):
    symbol = strat["symbol"]
    state = strat.setdefault("state", {})  # ensure mutations persist in strat
    rules = strat.get("rules", {})
    strategy_type = strat.get("strategy", "trailing_stop")

    # Shorts have inverse logic — delegate to dedicated handler
    if strategy_type == "short_sell":
        process_short_strategy(user, filepath, strat, state, rules)
        return

    # Check entry fill
    entry_order_id = state.get("entry_order_id")
    if entry_order_id and not state.get("entry_fill_price"):
        order = user_api_get(user, f"/orders/{entry_order_id}")
        if isinstance(order, dict) and order.get("status") == "filled":
            state["entry_fill_price"] = float(order.get("filled_avg_price", 0))
            state["total_shares_held"] = int(float(order.get("filled_qty", 0)))
            strat["status"] = "active"
            log(f"[{user['username']}] {symbol}: Entry filled at ${state['entry_fill_price']:.2f}", "monitor")

    entry = state.get("entry_fill_price")
    shares = state.get("total_shares_held", 0)
    if not entry or shares <= 0:
        save_json(filepath, strat)
        return

    # Get current price
    trade = user_api_get(user, f"/stocks/{symbol}/trades/latest?feed=iex")
    if not isinstance(trade, dict) or "trade" not in trade:
        save_json(filepath, strat)
        return
    price = trade["trade"].get("p", 0)
    if not price:
        save_json(filepath, strat)
        return

    # Place initial stop
    if not state.get("stop_order_id"):
        stop_pct = rules.get("stop_loss_pct", 0.10)
        stop_price = round(entry * (1 - stop_pct), 2)
        order = user_api_post(user, "/orders", {
            "symbol": symbol, "qty": str(shares), "side": "sell",
            "type": "stop", "stop_price": str(stop_price), "time_in_force": "gtc"
        })
        if isinstance(order, dict) and "id" in order:
            state["stop_order_id"] = order["id"]
            state["current_stop_price"] = stop_price
            log(f"[{user['username']}] {symbol}: Stop-loss placed at ${stop_price}", "monitor")
            notify_user(user, f"Stop-loss placed on {symbol} at ${stop_price:.2f}", "info")

    # Trailing stop for trailing_stop and breakout strategies
    if strategy_type in ("trailing_stop", "breakout"):
        highest = state.get("highest_price_seen") or entry
        if price > highest:
            state["highest_price_seen"] = price
            highest = price
        activation = rules.get("trailing_activation_pct", 0.10 if strategy_type == "trailing_stop" else 0)
        trail = rules.get("trailing_distance_pct", 0.05)
        if not state.get("trailing_activated") and highest >= entry * (1 + activation):
            state["trailing_activated"] = True
            log(f"[{user['username']}] {symbol}: Trailing activated", "monitor")
        if state.get("trailing_activated"):
            new_stop = round(highest * (1 - trail), 2)
            current_stop = state.get("current_stop_price", 0) or 0
            if new_stop > current_stop:
                old_id = state.get("stop_order_id")
                # Place the NEW stop before canceling the old one, so the
                # position is never unprotected if either call fails.
                new_order = user_api_post(user, "/orders", {
                    "symbol": symbol, "qty": str(shares), "side": "sell",
                    "type": "stop", "stop_price": str(new_stop), "time_in_force": "gtc"
                })
                if isinstance(new_order, dict) and "id" in new_order:
                    if old_id:
                        user_api_delete(user, f"/orders/{old_id}")
                    state["stop_order_id"] = new_order["id"]
                    state["current_stop_price"] = new_stop
                    log(f"[{user['username']}] {symbol}: Stop raised ${current_stop:.2f} -> ${new_stop:.2f}", "monitor")
                    notify_user(user, f"Stop raised on {symbol}: ${current_stop:.2f} -> ${new_stop:.2f}", "info")
                else:
                    # New stop placement failed — keep the old stop in place.
                    log(f"[{user['username']}] {symbol}: WARN stop raise failed, keeping prior stop at ${current_stop:.2f}. Err: {new_order}", "monitor")

    # Mean reversion target check
    if strategy_type == "mean_reversion":
        if price >= entry * 1.15:
            order = user_api_post(user, "/orders", {
                "symbol": symbol, "qty": str(shares), "side": "sell",
                "type": "market", "time_in_force": "day"
            })
            if isinstance(order, dict) and "id" in order:
                strat["status"] = "closed"
                state["exit_reason"] = "target_hit"
                state["exit_price"] = price
                pnl = (price - entry) * shares
                log(f"[{user['username']}] {symbol}: Target hit. P&L ${pnl:.2f}", "monitor")
                notify_user(user, f"Profit taken on {symbol}: sold at ${price:.2f} (+{((price/entry-1)*100):.1f}%)", "exit")
                record_trade_close(user, symbol, strategy_type, price, pnl,
                                    "target_hit", qty=shares, side="sell")

    # Feature 8: Partial profit taking
    check_profit_ladder(user, filepath, strat, price, entry, shares)
    # Refresh shares count in case profit ladder sold some
    shares = strat.get("state", {}).get("total_shares_held", shares)

    # Check stop triggered
    if state.get("stop_order_id"):
        order = user_api_get(user, f"/orders/{state['stop_order_id']}")
        if isinstance(order, dict) and order.get("status") == "filled":
            exit_price = float(order.get("filled_avg_price", state.get("current_stop_price", 0)))
            pnl = (exit_price - entry) * shares
            state["total_shares_held"] = 0
            state["stop_order_id"] = None
            state["exit_price"] = exit_price
            state["exit_reason"] = "stop_triggered"
            strat["status"] = "closed"
            log(f"[{user['username']}] {symbol}: STOP TRIGGERED at ${exit_price}, P&L ${pnl:.2f}", "monitor")
            notify_user(user, f"{symbol} stopped out at ${exit_price:.2f}. P&L: ${pnl:.2f}", "stop")
            record_trade_close(user, symbol, strategy_type, exit_price, pnl,
                                "stop_triggered", qty=shares, side="sell")
            gpath = user_file(user, "guardrails.json")
            guardrails = load_json(gpath) or {}
            guardrails["last_loss_time"] = now_et().isoformat()
            save_json(gpath, guardrails)

    save_json(filepath, strat)

# ============================================================================
# TASK 3: AUTO-DEPLOYER (per user)
# ============================================================================
def check_correlation_allowed(new_symbol, existing_positions):
    """Check if adding this symbol would create dangerous correlation.
    Returns (allowed, reason).

    Uses the sector map to check if we'd have too many positions in same sector.
    """
    # Import the sector map from update_dashboard (has 80+ stocks mapped)
    try:
        from update_dashboard import SECTOR_MAP
    except ImportError:
        SECTOR_MAP = {}

    new_sector = SECTOR_MAP.get(new_symbol, "Other")

    # Count positions already in this sector
    same_sector_count = 0
    for pos in existing_positions:
        pos_symbol = pos.get("symbol", "")
        pos_sector = SECTOR_MAP.get(pos_symbol, "Other")
        if pos_sector == new_sector and pos_sector != "Other":
            same_sector_count += 1

    MAX_PER_SECTOR = 2
    if same_sector_count >= MAX_PER_SECTOR:
        return False, f"Already have {same_sector_count} positions in {new_sector} sector (max {MAX_PER_SECTOR})"

    # Also check concentration: total market value in same sector
    total_value = sum(float(p.get("market_value", 0)) for p in existing_positions)
    sector_value = sum(float(p.get("market_value", 0)) for p in existing_positions
                       if SECTOR_MAP.get(p.get("symbol", ""), "Other") == new_sector)

    if total_value > 0 and sector_value / total_value > 0.4:
        return False, f"{new_sector} sector already {sector_value/total_value*100:.0f}% of portfolio (max 40%)"

    return True, f"Sector diversification OK ({new_sector})"

def run_auto_deployer(user):
    log(f"[{user['username']}] Running auto-deployer...", "deployer")

    gpath = user_file(user, "guardrails.json")
    guardrails = load_json(gpath) or {}
    if guardrails.get("kill_switch"):
        log(f"[{user['username']}] Kill switch active. Skipping.", "deployer")
        return

    config = load_json(user_file(user, "auto_deployer_config.json")) or {}
    if not config.get("enabled", True):
        log(f"[{user['username']}] Auto-deployer disabled. Skipping.", "deployer")
        return

    # Cooldown check
    last_loss = guardrails.get("last_loss_time")
    if last_loss:
        try:
            last_dt = datetime.fromisoformat(last_loss.replace("Z", "+00:00"))
            cooldown_min = guardrails.get("cooldown_after_loss_minutes", 60)
            if (now_et() - last_dt).total_seconds() < cooldown_min * 60:
                log(f"[{user['username']}] In cooldown after recent loss. Skipping.", "deployer")
                return
        except Exception as e:
            # If we can't parse last_loss we can't honour the cooldown. Fail
            # CLOSED (skip deploy) rather than silently bypassing a
            # financial guardrail — this is what bit us in Round 3 audit.
            log(f"[{user['username']}] Cooldown check failed to parse last_loss_time ({last_loss!r}): {e}. "
                f"Skipping deploy to be safe.", "deployer")
            return

    # Set daily starting value — ONCE per trading day. Previously this
    # unconditionally overwrote on every auto-deployer run (including
    # Force Deploy), which could:
    #   - Reset baseline to a lower value after an early drop, masking the
    #     drawdown for the daily-loss kill switch
    #   - Reset baseline to a higher value after an early rally, making a
    #     subsequent pullback look worse than it was
    # Now set only if unset OR the stored date doesn't match today's ET date.
    account = user_api_get(user, "/account")
    if isinstance(account, dict) and "error" not in account:
        today_et = get_et_time().strftime("%Y-%m-%d")
        last_reset_date = guardrails.get("daily_starting_value_date")
        if not guardrails.get("daily_starting_value") or last_reset_date != today_et:
            guardrails["daily_starting_value"] = float(account.get("portfolio_value", 0))
            guardrails["daily_starting_value_date"] = today_et
        current = float(account.get("portfolio_value", 0))
        peak = guardrails.get("peak_portfolio_value", current)
        if current > peak:
            guardrails["peak_portfolio_value"] = current
        save_json(gpath, guardrails)

    # Capital check — runs in BASE_DIR with user env so it reads the right account.
    try:
        env = os.environ.copy()
        env["ALPACA_API_KEY"] = user["_api_key"]
        env["ALPACA_API_SECRET"] = user["_api_secret"]
        env["ALPACA_ENDPOINT"] = user["_api_endpoint"]
        env["ALPACA_DATA_ENDPOINT"] = user["_data_endpoint"]
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "capital_check.py")],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=30, env=env)
        # capital_check.py writes to DATA_DIR/capital_status.json. Read from there,
        # falling back to BASE_DIR for backwards compat with older deploys.
        capital = load_json(os.path.join(DATA_DIR, "capital_status.json"))
        if not capital:
            capital = load_json(os.path.join(BASE_DIR, "capital_status.json")) or {}
        if not capital.get("can_trade", True):
            log(f"[{user['username']}] Cannot trade: {capital.get('recommendation')}", "deployer")
            notify_user(user, f"Auto-deployer skipped: {capital.get('recommendation','insufficient capital')}", "info")
            return
    except Exception as e:
        log(f"[{user['username']}] Capital check error: {e}", "deployer")

    # Run screener to get fresh picks (skip if already ran in last 5 min)
    run_screener(user, max_age_seconds=300)

    # Prefer per-user dashboard data; fall back to shared DATA_DIR (then BASE_DIR).
    picks_path = user_file(user, "dashboard_data.json")
    if not os.path.exists(picks_path):
        picks_path = os.path.join(DATA_DIR, "dashboard_data.json")
    if not os.path.exists(picks_path):
        picks_path = os.path.join(BASE_DIR, "dashboard_data.json")
    picks_data = load_json(picks_path) or {}
    # Expanded candidate pool: search up to top 20 picks. If guardrails block
    # the top picks (earnings warning, sector concentration, etc.) the deployer
    # will fall back to the next eligible candidate instead of giving up early.
    CANDIDATE_POOL = config.get("candidate_pool_size", 20)
    top_picks = picks_data.get("picks", [])[:CANDIDATE_POOL]
    market_regime = picks_data.get("market_regime", "neutral")
    spy_mom = picks_data.get("spy_momentum_20d", 0)

    max_per_day = config.get("max_new_per_day", 2)
    deployed = 0
    candidates_evaluated = 0
    skip_reasons = []

    positions = user_api_get(user, "/positions")
    existing_syms = set()
    existing_positions = []
    if isinstance(positions, list):
        existing_positions = positions
        existing_syms = {p.get("symbol") for p in positions}

    sdir = user_strategies_dir(user)

    log(f"[{user['username']}] Evaluating {len(top_picks)} candidates from filtered screener list", "deployer")

    for pick in top_picks:
        if deployed >= max_per_day:
            break
        symbol = pick.get("symbol")
        best_strat = pick.get("best_strategy", "").lower().replace(" ", "_")
        candidates_evaluated += 1

        if symbol in existing_syms:
            skip_reasons.append(f"{symbol}: already held")
            continue
        if pick.get("earnings_warning"):
            log(f"[{user['username']}] {symbol}: Skipped (earnings warning) — trying next pick", "deployer")
            skip_reasons.append(f"{symbol}: earnings warning")
            continue
        if best_strat not in ("trailing_stop", "breakout", "mean_reversion"):
            skip_reasons.append(f"{symbol}: unsupported strategy ({best_strat})")
            continue

        # Feature 10: Correlation check. Reuses positions fetched at the top
        # of run_auto_deployer (existing_positions) — previously this re-fetched
        # /positions on EVERY candidate, an N+1 pattern that for a 20-candidate
        # pool caused 20 extra API calls per run and up to 60s added latency.
        allowed, reason = check_correlation_allowed(symbol, existing_positions)
        if not allowed:
            log(f"[{user['username']}] {symbol}: Skipped ({reason}) — trying next pick", "deployer")
            skip_reasons.append(f"{symbol}: {reason}")
            continue

        # Do NOT use `or 1` here — if screener said recommended_shares=0
        # that means "don't buy". Treat missing (None) as skip too.
        rs = pick.get("recommended_shares")
        if rs is None:
            log(f"[{user['username']}] {symbol}: Skipped (no recommended_shares from screener)", "deployer")
            continue
        try:
            qty = int(rs)
        except (TypeError, ValueError):
            log(f"[{user['username']}] {symbol}: Skipped (bad recommended_shares: {rs!r})", "deployer")
            continue
        if qty < 1:
            log(f"[{user['username']}] {symbol}: Skipped (recommended_shares < 1)", "deployer")
            continue

        order = user_api_post(user, "/orders", {
            "symbol": symbol, "qty": str(qty), "side": "buy",
            "type": "market", "time_in_force": "day"
        })

        if isinstance(order, dict) and "id" in order:
            strat_file = {
                "symbol": symbol,
                "strategy": best_strat,
                "created": now_et().strftime("%Y-%m-%d"),
                "status": "awaiting_fill",
                "entry_price_estimate": pick.get("price"),
                "initial_qty": qty,
                "deployer": "cloud_scheduler",
                "rules": {
                    "stop_loss_pct": 0.05 if best_strat == "breakout" else 0.10,
                    "trailing_activation_pct": 0 if best_strat == "breakout" else 0.10,
                    "trailing_distance_pct": 0.05,
                },
                "state": {
                    "entry_fill_price": None,
                    "entry_order_id": order["id"],
                    "stop_order_id": None,
                    "highest_price_seen": None,
                    "trailing_activated": False,
                    "current_stop_price": None,
                    "total_shares_held": 0,
                },
                "reasoning": {
                    "best_score": pick.get("best_score"),
                    "momentum_20d": pick.get("momentum_20d"),
                    "rsi": pick.get("rsi"),
                    "bias": pick.get("overall_bias"),
                    "backtest_return": pick.get("backtest_return"),
                }
            }
            filename = f"{best_strat}_{symbol}.json"
            save_json(os.path.join(sdir, filename), strat_file)

            # Log to trade journal (per-user)
            journal_path = user_file(user, "trade_journal.json")
            journal = load_json(journal_path) or {"trades": [], "daily_snapshots": []}
            _score = pick.get("best_score")
            _rsi = pick.get("rsi", 50)
            _score_str = f"{_score:.0f}" if isinstance(_score, (int, float)) else "n/a"
            _rsi_str = f"{_rsi:.0f}" if isinstance(_rsi, (int, float)) else "n/a"
            journal["trades"].append({
                "timestamp": now_et().isoformat(),
                "symbol": symbol, "side": "buy", "qty": qty,
                "price": pick.get("price"),
                "strategy": best_strat,
                "reason": f"Auto-deployed. Score {_score_str}, RSI {_rsi_str}, Bias {pick.get('overall_bias','?')}",
                "deployer": "cloud_scheduler",
                "status": "open",
            })
            save_json(journal_path, journal)

            log(f"[{user['username']}] DEPLOYED: {best_strat} on {symbol} x {qty} @ ~${pick.get('price',0):.2f}", "deployer")
            notify_user(user, f"Deployed {best_strat} on {symbol}: {qty} shares @ ~${pick.get('price',0):.2f}", "trade")
            deployed += 1
            # Optimistically add this symbol to existing_positions so subsequent
            # correlation checks in this same run account for it. Synthetic
            # position record with just enough fields for check_correlation.
            existing_positions.append({
                "symbol": symbol, "qty": str(qty),
                "market_value": str(qty * (pick.get("price") or 0)),
            })
            existing_syms.add(symbol)
        else:
            log(f"[{user['username']}] Order failed for {symbol}: {order}", "deployer")
            skip_reasons.append(f"{symbol}: order API error")

    # Summary if nothing deployed — shows full fallback chain
    if deployed == 0 and candidates_evaluated > 0:
        log(f"[{user['username']}] No deploys after evaluating {candidates_evaluated} candidates. Skip chain: "
            + " | ".join(skip_reasons[:10]), "deployer")
        notify_user(user,
            f"Auto-deployer found no eligible picks (evaluated {candidates_evaluated}, all blocked by guardrails). "
            f"Top reasons: {'; '.join(skip_reasons[:3])}",
            "info")

    # Short selling if bear market
    short_config = config.get("short_selling", {})
    if short_config.get("enabled") and deployed < max_per_day:
        if market_regime == "bear" and spy_mom < short_config.get("require_spy_20d_below", -3):
            # Check existing shorts count
            current_shorts = sum(1 for p in existing_positions if float(p.get("qty", 0)) < 0)
            max_shorts = short_config.get("max_short_positions", 1)

            # Short cooldown check
            last_short_loss = guardrails.get("last_short_loss_time")
            if last_short_loss:
                try:
                    last_dt = datetime.fromisoformat(last_short_loss.replace("Z", "+00:00"))
                    cooldown_hrs = guardrails.get("short_selling_cooldown_hours", 48)
                    if (now_et() - last_dt).total_seconds() < cooldown_hrs * 3600:
                        log(f"[{user['username']}] Short cooldown active ({cooldown_hrs}hr), skipping", "deployer")
                        current_shorts = max_shorts  # Force skip below
                except Exception as e:
                    log(f"[{user['username']}] Short cooldown parse error: {e}", "deployer")

            short_candidates = picks_data.get("short_candidates", [])
            min_score = short_config.get("min_short_score", 15)
            stop_pct = short_config.get("stop_loss_pct", 0.08)
            target_pct = short_config.get("profit_target_pct", 0.15)
            max_pct = short_config.get("max_portfolio_pct_per_short", 0.05)

            for sc in short_candidates:
                if current_shorts >= max_shorts:
                    break
                if sc.get("short_score", 0) < min_score:
                    continue
                if sc.get("meme_warning") and short_config.get("skip_if_meme_warning", True):
                    continue

                short_symbol = sc.get("symbol")
                if not short_symbol or short_symbol in existing_syms:
                    continue

                # Correlation check for shorts too
                short_allowed, short_reason = check_correlation_allowed(short_symbol, existing_positions)
                if not short_allowed:
                    log(f"[{user['username']}] {short_symbol}: Short skipped ({short_reason})", "deployer")
                    continue

                # Position sizing: max 5% of portfolio
                short_price = float(sc.get("price", 0))
                if short_price <= 0:
                    continue
                portfolio_val = float(account.get("portfolio_value", 0)) if isinstance(account, dict) else 0
                max_dollars = portfolio_val * max_pct
                short_qty = min(int(max_dollars / short_price), 100)
                if short_qty < 1:
                    log(f"[{user['username']}] {short_symbol}: Short skipped (price ${short_price} too high for 5% sizing)", "deployer")
                    continue

                # Place short (sell without existing position = short sell)
                log(f"[{user['username']}] Deploying SHORT: {short_symbol} x{short_qty} @ ~${short_price}", "deployer")
                short_order = user_api_post(user, "/orders", {
                    "symbol": short_symbol, "qty": str(short_qty), "side": "sell",
                    "type": "market", "time_in_force": "day"
                })

                if isinstance(short_order, dict) and "id" in short_order:
                    # Write short strategy file
                    stop_price = round(short_price * (1 + stop_pct), 2)
                    target_price = round(short_price * (1 - target_pct), 2)
                    short_strat = {
                        "symbol": short_symbol,
                        "strategy": "short_sell",
                        "created": now_et().strftime("%Y-%m-%d"),
                        "status": "awaiting_fill",
                        "entry_price_estimate": short_price,
                        "initial_qty": short_qty,
                        "deployer": "cloud_scheduler",
                        "rules": {
                            "stop_loss_pct": stop_pct,
                            "profit_target_pct": target_pct,
                            "max_hold_days": 14,
                            "reason": "Bear market deploy"
                        },
                        "state": {
                            "entry_fill_price": None,
                            "entry_order_id": short_order["id"],
                            "cover_order_id": None,
                            "target_order_id": None,
                            "shares_shorted": 0,  # Will be negative (short) after fill
                            "total_shares_held": 0,
                            "current_stop_price": stop_price,
                            "current_target_price": target_price,
                            "lowest_price_seen": short_price,
                        },
                        "reasoning": {
                            "short_score": sc.get("short_score"),
                            "market_regime": "bear",
                            "spy_momentum_20d": spy_mom,
                            "reasons": sc.get("reasons", []),
                        }
                    }
                    short_filename = f"short_sell_{short_symbol}.json"
                    save_json(os.path.join(sdir, short_filename), short_strat)

                    # Log to per-user journal
                    journal_path = user_file(user, "trade_journal.json")
                    journal = load_json(journal_path) or {"trades": [], "daily_snapshots": []}
                    journal["trades"].append({
                        "timestamp": now_et().isoformat(),
                        "symbol": short_symbol, "side": "sell_short", "qty": short_qty,
                        "price": short_price,
                        "strategy": "short_sell",
                        "reason": f"Bear market short. SPY 20d: {spy_mom:.1f}%. Score: {sc.get('short_score')}",
                        "deployer": "cloud_scheduler",
                        "status": "open",
                    })
                    save_json(journal_path, journal)

                    notify_user(user, f"SHORT deployed: sold {short_qty} {short_symbol} @ ~${short_price:.2f}. "
                           f"Stop ${stop_price}, Target ${target_price}. Bear market play.", "trade")
                    deployed += 1
                    current_shorts += 1
                    break  # Only 1 short per run
                else:
                    log(f"[{user['username']}] Short order failed for {short_symbol}: {short_order}", "deployer")

    if deployed == 0:
        notify_user(user, "Morning scan complete. No qualifying trades today.", "info")
    log(f"[{user['username']}] Auto-deployer done. Deployed {deployed} trades.", "deployer")

# ============================================================================
# TASK 4: DAILY CLOSE (per user)
# ============================================================================
def run_daily_close(user):
    log(f"[{user['username']}] Running daily close...", "close")
    try:
        env = os.environ.copy()
        env["ALPACA_API_KEY"] = user["_api_key"]
        env["ALPACA_API_SECRET"] = user["_api_secret"]
        env["ALPACA_ENDPOINT"] = user["_api_endpoint"]
        env["ALPACA_DATA_ENDPOINT"] = user["_data_endpoint"]
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "update_scorecard.py")],
                      cwd=BASE_DIR, capture_output=True, text=True, timeout=60, env=env)
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "error_recovery.py")],
                      cwd=BASE_DIR, capture_output=True, text=True, timeout=60, env=env)

        gpath = user_file(user, "guardrails.json")
        guardrails = load_json(gpath) or {}
        guardrails["daily_starting_value"] = None
        account = user_api_get(user, "/account")
        if isinstance(account, dict) and "error" not in account:
            current = float(account.get("portfolio_value", 0))
            peak = guardrails.get("peak_portfolio_value", current)
            if current > peak:
                guardrails["peak_portfolio_value"] = current
        save_json(gpath, guardrails)

        # Try per-user scorecard first, fall back to shared (DATA_DIR then BASE_DIR)
        scorecard_path = user_file(user, "scorecard.json")
        if not os.path.exists(scorecard_path):
            scorecard_path = os.path.join(DATA_DIR, "scorecard.json")
        if not os.path.exists(scorecard_path):
            scorecard_path = os.path.join(BASE_DIR, "scorecard.json")
        scorecard = load_json(scorecard_path) or {}
        value = scorecard.get("current_value", 0)
        win_rate = scorecard.get("win_rate_pct", 0)
        readiness = scorecard.get("readiness_score", 0)
        ready_flag = " READY FOR LIVE!" if readiness >= 80 else ""
        notify_user(user, f"Daily close: ${value:,.2f} | Win {win_rate:.0f}% | Ready {readiness}/100{ready_flag}", "daily")
        log(f"[{user['username']}] Daily close complete", "close")
    except Exception as e:
        log(f"[{user['username']}] Daily close error: {e}", "close")

# ============================================================================
# TASK 5: WEEKLY LEARNING (per user)
# ============================================================================
def run_weekly_learning(user):
    log(f"[{user['username']}] Running weekly learning...", "learn")
    try:
        env = os.environ.copy()
        env["ALPACA_API_KEY"] = user["_api_key"]
        env["ALPACA_API_SECRET"] = user["_api_secret"]
        env["ALPACA_ENDPOINT"] = user["_api_endpoint"]
        env["ALPACA_DATA_ENDPOINT"] = user["_data_endpoint"]
        # CRITICAL per-user paths — otherwise learn.py reads/writes the
        # shared trade_journal + learned_weights and every user's weekly
        # run overwrites the others.
        env["TRADE_JOURNAL_PATH"] = user_file(user, "trade_journal.json")
        env["LEARNED_WEIGHTS_PATH"] = user_file(user, "learned_weights.json")
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "learn.py")],
                      cwd=BASE_DIR, capture_output=True, text=True, timeout=120, env=env)
        notify_user(user, "Weekly learning engine completed", "learn")
    except Exception as e:
        log(f"[{user['username']}] Learning error: {e}", "learn")

# ============================================================================
# TASK 6: FRIDAY RISK REDUCTION (per user)
# ============================================================================
def run_friday_risk_reduction(user):
    """Scale out of profitable positions before weekend gap risk."""
    log(f"[{user['username']}] Running Friday risk reduction...", "friday")

    positions = user_api_get(user, "/positions")
    if not isinstance(positions, list):
        log(f"[{user['username']}] Could not fetch positions: {positions}", "friday")
        return

    actions_taken = 0
    sdir = user_strategies_dir(user)
    for pos in positions:
        symbol = pos.get("symbol", "")
        qty = abs(int(float(pos.get("qty", 0))))
        unrealized_plpc = float(pos.get("unrealized_plpc", 0)) * 100
        avg_entry = float(pos.get("avg_entry_price", 0))
        current = float(pos.get("current_price", 0))

        # Only scale out of profitable positions (20%+ gain)
        if unrealized_plpc < 20:
            continue
        if qty < 2:  # Can't sell half of 1 share
            continue

        # Never trim wheel-owned shares from Friday — the wheel state machine
        # manages its own exits via covered calls / expiration.
        try:
            wheel_sf = os.path.join(sdir, f"wheel_{symbol}.json")
            if os.path.exists(wheel_sf):
                wstate = load_json(wheel_sf) or {}
                if wstate.get("stage", "").startswith("stage_2_"):
                    log(f"[{user['username']}] {symbol}: skipping Friday trim — wheel-owned in stage {wstate.get('stage')}", "friday")
                    continue
        except Exception:
            pass

        half_qty = qty // 2
        log(f"[{user['username']}] {symbol}: +{unrealized_plpc:.1f}%, selling {half_qty}/{qty} before weekend", "friday")

        order = user_api_post(user, "/orders", {
            "symbol": symbol,
            "qty": str(half_qty),
            "side": "sell" if float(pos.get("qty", 0)) > 0 else "buy",
            "type": "market",
            "time_in_force": "day"
        })

        if isinstance(order, dict) and "id" in order:
            actions_taken += 1
            # Signed position size: long>0, short<0. Profit direction depends on side.
            raw_qty = float(pos.get("qty", 0))
            if raw_qty > 0:
                profit = (current - avg_entry) * half_qty
            else:
                # Short: profit when current < entry
                profit = (avg_entry - current) * half_qty

            # Update the matching strategy file so next monitor tick doesn't
            # try to re-place stops sized for the OLD quantity. Resize any
            # open stop order to match the remaining qty.
            try:
                for sf in os.listdir(sdir):
                    if not (sf.endswith(f"_{symbol}.json") and not sf.startswith("wheel_")):
                        continue
                    sf_path = os.path.join(sdir, sf)
                    strat = load_json(sf_path) or {}
                    state = strat.get("state", {})
                    remaining = qty - half_qty
                    state["total_shares_held"] = remaining
                    state.setdefault("friday_trims", []).append({
                        "ts": now_et().isoformat(),
                        "sold_qty": half_qty,
                        "remaining_qty": remaining,
                        "estimated_profit": round(profit, 2),
                    })
                    # Resize stop order if one exists
                    old_stop = state.get("stop_order_id")
                    stop_price = state.get("current_stop_price")
                    if old_stop and remaining > 0 and stop_price:
                        new_stop_side = "sell" if raw_qty > 0 else "buy"
                        new_stop_resp = user_api_post(user, "/orders", {
                            "symbol": symbol, "qty": str(remaining), "side": new_stop_side,
                            "type": "stop", "stop_price": str(stop_price), "time_in_force": "gtc",
                        })
                        if isinstance(new_stop_resp, dict) and "id" in new_stop_resp:
                            user_api_delete(user, f"/orders/{old_stop}")
                            state["stop_order_id"] = new_stop_resp["id"]
                        # else: keep old oversized stop — still protective
                    strat["state"] = state
                    save_json(sf_path, strat)
            except Exception as e:
                log(f"[{user['username']}] WARN Friday strategy-file update failed for {symbol}: {e}", "friday")

            notify_user(user, f"Friday risk reduction: trimmed {half_qty} {symbol} locking in ~${profit:.2f}. {qty - half_qty} shares still held.", "exit")
        else:
            log(f"[{user['username']}] Failed to trim {symbol}: {order}", "friday")

    if actions_taken > 0:
        notify_user(user, f"Weekend prep complete: scaled out of {actions_taken} winning positions", "info")
    else:
        log(f"[{user['username']}] No positions met scale-out criteria", "friday")

# ============================================================================
# TASK 7: MONTHLY REBALANCE (per user)
# ============================================================================
def run_monthly_rebalance(user):
    """Monthly review: close long-underwater positions, free capital."""
    log(f"[{user['username']}] Running monthly rebalance...", "rebalance")

    positions = user_api_get(user, "/positions")
    if not isinstance(positions, list):
        return

    sdir = user_strategies_dir(user)
    closed_count = 0
    for pos in positions:
        symbol = pos.get("symbol", "")
        qty = abs(int(float(pos.get("qty", 0))))
        unrealized_plpc = float(pos.get("unrealized_plpc", 0)) * 100

        # Check position age via strategy file. Match exact "_{SYMBOL}.json"
        # suffix — `symbol in f` would cross-match (e.g. "AI" matches "AIG").
        # Also SKIP wheel-owned shares: the wheel state machine manages its
        # own exits. Monthly rebalance closing wheel shares would break it.
        try:
            sf_all = [f for f in os.listdir(sdir) if f.endswith(".json")]
            strat_files = [f for f in sf_all if f.endswith(f"_{symbol}.json")]
            # Check if there's an active wheel on this symbol
            wheel_file = f"wheel_{symbol}.json"
            if wheel_file in sf_all:
                wstate = load_json(os.path.join(sdir, wheel_file)) or {}
                if wstate.get("stage", "").startswith("stage_2_"):
                    log(f"[{user['username']}] {symbol}: skipping rebalance — wheel-owned in stage {wstate.get('stage')}", "rebalance")
                    continue
        except FileNotFoundError:
            strat_files = []

        too_old = False
        for sf in strat_files:
            # Defensive: never evaluate a wheel file's age for rebalance
            if sf.startswith("wheel_"):
                continue
            strat = load_json(os.path.join(sdir, sf))
            if not strat:
                continue
            created = strat.get("created")
            if created:
                try:
                    # Accept both "YYYY-MM-DD" and full ISO timestamps
                    created_str = str(created)[:10]
                    age_days = (now_et().date() -
                               datetime.strptime(created_str, "%Y-%m-%d").date()).days
                    if age_days >= 60:
                        too_old = True
                        break
                except Exception:
                    pass

        # Close if old AND losing
        if too_old and unrealized_plpc < -2:
            log(f"[{user['username']}] {symbol}: 60+ days old, {unrealized_plpc:.1f}% down — closing for rebalance", "rebalance")
            order = user_api_post(user, "/orders", {
                "symbol": symbol, "qty": str(qty),
                "side": "sell" if float(pos.get("qty", 0)) > 0 else "buy",
                "type": "market", "time_in_force": "day"
            })
            if isinstance(order, dict) and "id" in order:
                closed_count += 1
                notify_user(user, f"Monthly rebalance: closed underwater {symbol} (~{unrealized_plpc:.1f}% loss) to free capital", "info")

    if closed_count > 0:
        notify_user(user, f"Monthly rebalance: closed {closed_count} stale losing positions", "daily")
    else:
        notify_user(user, "Monthly rebalance: all positions healthy, no changes", "info")

def is_first_trading_day_of_month(user):
    """Check if today is the first trading day of the month (using Alpaca clock)."""
    now_et = get_et_time()
    if now_et.day > 7:
        return False  # Definitely not first trading day
    # Check calendar via Alpaca
    start = now_et.replace(day=1).strftime("%Y-%m-%d")
    end = now_et.strftime("%Y-%m-%d")
    cal = user_api_get(user, f"/calendar?start={start}&end={end}")
    if not isinstance(cal, list) or not cal:
        return False
    first_trading_date = cal[0].get("date", "")
    today_str = now_et.strftime("%Y-%m-%d")
    return first_trading_date == today_str

# ============================================================================
# SCHEDULER LOOP
# ============================================================================
def should_run_interval(task_name, interval_seconds):
    now = time.time()
    last = _last_runs.get(task_name, 0)
    if now - last >= interval_seconds:
        _last_runs[task_name] = now
        return True
    return False

def should_run_daily_at(task_name, hour_et, minute_et):
    now_et = get_et_time()
    target = now_et.replace(hour=hour_et, minute=minute_et, second=0, microsecond=0)
    last_date = _last_runs.get(task_name, "")
    today_str = now_et.strftime("%Y-%m-%d")
    if last_date != today_str and now_et >= target and (now_et - target).total_seconds() < 600:
        _last_runs[task_name] = today_str
        return True
    return False

# ============================================================================
# TASK 8: WHEEL AUTO-DEPLOY (per user) — fires at 9:40 AM ET weekdays.
# Picks the top wheel candidate from the screener and sells a cash-secured put.
# All safety checks live in wheel_strategy.py (options level, cash coverage,
# earnings avoidance, concurrent-wheels cap, price range, premium yield, etc).
# ============================================================================
_wheel_deploy_lock = threading.Lock()
_wheel_deploy_in_flight = set()  # set of user ids currently deploying


def run_wheel_auto_deploy(user):
    try:
        import wheel_strategy as ws
    except Exception as e:
        log(f"[{user['username']}] wheel_strategy import failed: {e}", "wheel")
        return

    # Dedup: if a wheel deploy is already running for this user (from the
    # 9:40 AM scheduler tick OR Force Deploy OR both), skip. Otherwise two
    # concurrent calls can both place short-put orders on the same symbol
    # before the first writes state (count_active_wheels returns 0 for both).
    uid = user.get("id")
    with _wheel_deploy_lock:
        if uid in _wheel_deploy_in_flight:
            log(f"[{user['username']}] Wheel auto-deploy already running — skipping concurrent invocation", "wheel")
            return
        _wheel_deploy_in_flight.add(uid)
    try:
        _run_wheel_auto_deploy_inner(user)
    finally:
        with _wheel_deploy_lock:
            _wheel_deploy_in_flight.discard(uid)


def _run_wheel_auto_deploy_inner(user):
    try:
        import wheel_strategy as ws
    except Exception as e:
        log(f"[{user['username']}] wheel_strategy import failed: {e}", "wheel")
        return

    log(f"[{user['username']}] Running wheel auto-deploy...", "wheel")

    # Respect kill switch and auto-deployer config
    guardrails = load_json(user_file(user, "guardrails.json")) or {}
    if guardrails.get("kill_switch"):
        log(f"[{user['username']}] Kill switch active — skipping wheel auto-deploy", "wheel")
        return
    config = load_json(user_file(user, "auto_deployer_config.json")) or {}
    if not config.get("enabled", True):
        log(f"[{user['username']}] Auto-deployer disabled — skipping wheel auto-deploy", "wheel")
        return
    # Per-strategy wheel toggle (default enabled)
    wheel_cfg = config.get("wheel", {})
    if wheel_cfg.get("enabled", True) is False:
        log(f"[{user['username']}] Wheel strategy disabled in auto_deployer_config — skipping", "wheel")
        return

    # Make sure we have fresh screener data (skip if already ran in last 5 min)
    run_screener(user, max_age_seconds=300)

    picks_path = user_file(user, "dashboard_data.json")
    if not os.path.exists(picks_path):
        picks_path = os.path.join(DATA_DIR, "dashboard_data.json")
    picks_data = load_json(picks_path) or {}

    # Search up to top 20 picks (matches main auto-deployer fallback pool).
    # Most will be filtered out by safety rails (price range, earnings, concurrent cap).
    candidates = ws.find_wheel_candidates(picks_data, max_candidates=20)
    if not candidates:
        log(f"[{user['username']}] No wheel candidates in screener output", "wheel")
        return

    log(f"[{user['username']}] Wheel candidates: {[c.get('symbol') for c in candidates]}", "wheel")

    deployed = 0
    max_per_day = int(wheel_cfg.get("max_new_per_day", 1))
    for pick in candidates:
        if deployed >= max_per_day:
            break
        success, msg, _ = ws.open_short_put(user, pick)
        if success:
            log(f"[{user['username']}] WHEEL DEPLOYED: {msg}", "wheel")
            notify_user(user, f"Wheel opened on {pick.get('symbol')}: {msg}", "trade")
            deployed += 1
        else:
            log(f"[{user['username']}] {pick.get('symbol')}: wheel skipped — {msg}", "wheel")

    if deployed == 0:
        log(f"[{user['username']}] No wheels deployed after evaluating {len(candidates)} candidates", "wheel")
    else:
        log(f"[{user['username']}] Wheel auto-deploy done — {deployed} new wheel(s)", "wheel")


# ============================================================================
# TASK 9: WHEEL MONITOR (per user) — every 15 min during market hours.
# Iterates every wheel_*.json file and advances the state machine:
#   - Check fill on pending open orders
#   - Check expiration / assignment for active contracts
#   - Buy to close at 50% profit
#   - Sell covered calls once shares are assigned
# ============================================================================
def run_wheel_monitor(user):
    try:
        import wheel_strategy as ws
    except Exception as e:
        log(f"[{user['username']}] wheel_strategy import failed: {e}", "wheel")
        return

    wheels = ws.list_wheel_files(user)
    if not wheels:
        return  # Nothing to monitor

    for fname, state in wheels:
        try:
            stage_before = state.get("stage")
            events = ws.advance_wheel_state(user, state)
            for ev in events:
                log(f"[{user['username']}] {ev}", "wheel")
                notify_user(user, ev, "info")

            # Stage 2 auto-pilot: once shares are owned, proactively sell a call
            if state.get("stage") == "stage_2_shares_owned" and not state.get("active_contract"):
                ok, msg = ws.open_covered_call(user, state)
                if ok:
                    log(f"[{user['username']}] {state['symbol']}: {msg}", "wheel")
                    notify_user(user, f"Covered call opened on {state['symbol']}: {msg}", "trade")
                else:
                    log(f"[{user['username']}] {state['symbol']}: covered call skipped — {msg}", "wheel")
        except Exception as e:
            log(f"[{user['username']}] Wheel monitor error on {fname}: {e}", "wheel")


# ============================================================================
# TASK 10: DAILY BACKUP — shared across all users (one snapshot covers DB)
# Runs at 3:00 AM ET when market is closed. Creates a tar.gz with users.db
# and per-user directories, keeps last 14 days, rotates older ones.
# ============================================================================
def run_daily_backup():
    try:
        import backup as _backup
        path, size, err = _backup.create_backup()
        if err:
            log(f"Daily backup FAILED: {err}", "backup")
            notify_user_global(f"⚠️ Daily backup FAILED: {err}", "alert")
            return
        size_mb = size / 1024 / 1024
        log(f"Daily backup created: {os.path.basename(path)} ({size_mb:.2f}MB)", "backup")
        # Only notify user on big changes or first successful backup — silent
        # success keeps ntfy clean (ran every day at 3 AM ET).
    except Exception as e:
        log(f"Daily backup error: {e}", "backup")


def scheduler_loop():
    global _scheduler_running
    log("Cloud scheduler loop started (multi-user)", "scheduler")
    notify_user_global("Cloud scheduler started — autonomous bot running on Railway", "info")

    while _scheduler_running:
        try:
            now_et = get_et_time()
            weekday = now_et.weekday()
            is_weekday = weekday < 5

            users = get_all_users_for_scheduling()
            if not users:
                # No users configured — sleep longer and retry
                time.sleep(60)
                continue

            # Check market once per tick (shared across all users — same clock).
            # Try each user's credentials until one succeeds — previously we
            # only tried users[0], so if that user had revoked their Alpaca
            # keys, market_open_flag stayed False and NO user in the system
            # would trade that day.
            market_open_flag = False
            if is_weekday:
                try:
                    clock = None
                    for _u in users:
                        result = user_api_get(_u, "/clock")
                        if isinstance(result, dict) and "error" not in result:
                            clock = result
                            break
                    if isinstance(clock, dict):
                        market_open_flag = clock.get("is_open", False)
                except Exception:
                    pass

            for user in users:
                try:
                    uid = user["id"]

                    # Auto-deployer: weekdays 9:35 AM ET (skip on market holidays)
                    if is_weekday and now_et.hour == 9 and now_et.minute >= 35 and market_open_flag:
                        if should_run_daily_at(f"auto_deployer_{uid}", 9, 35):
                            run_auto_deployer(user)

                    # Wheel auto-deploy: weekdays 9:40 AM ET (5 min after regular deployer)
                    # Sells cash-secured puts on top wheel candidates.
                    if is_weekday and now_et.hour == 9 and now_et.minute >= 40 and market_open_flag:
                        if should_run_daily_at(f"wheel_deploy_{uid}", 9, 40):
                            run_wheel_auto_deploy(user)

                    # Wheel monitor: every 15 min during market hours
                    # Manages assignment, expiration, buy-to-close, stage transitions.
                    if market_open_flag and should_run_interval(f"wheel_monitor_{uid}", 15 * 60):
                        run_wheel_monitor(user)

                    # Screener: every 30 min during market hours
                    if market_open_flag and should_run_interval(f"screener_{uid}", 30 * 60):
                        run_screener(user)

                    # Strategy monitor: every 60s during market hours
                    if market_open_flag and should_run_interval(f"monitor_{uid}", 60):
                        monitor_strategies(user)

                    # Daily close: weekdays 4:05 PM ET
                    if is_weekday and now_et.hour == 16 and now_et.minute >= 5:
                        if should_run_daily_at(f"daily_close_{uid}", 16, 5):
                            run_daily_close(user)

                    # Weekly learning: Fridays 5:00 PM ET
                    if weekday == 4 and now_et.hour == 17:
                        if should_run_daily_at(f"weekly_learning_{uid}", 17, 0):
                            run_weekly_learning(user)

                    # Feature 6: Friday risk reduction at 3:45 PM ET
                    if weekday == 4 and now_et.hour == 15 and now_et.minute >= 45 and market_open_flag:
                        if should_run_daily_at(f"friday_reduction_{uid}", 15, 45):
                            run_friday_risk_reduction(user)

                    # Feature 19: Monthly rebalance on first trading day at 9:45 AM ET
                    if is_weekday and now_et.hour == 9 and now_et.minute >= 45:
                        if is_first_trading_day_of_month(user):
                            if should_run_daily_at(f"monthly_rebalance_{uid}", 9, 45):
                                run_monthly_rebalance(user)
                except Exception as e:
                    log(f"[{user.get('username','?')}] Per-user scheduler error: {e}", "scheduler")

            # Daily backup — runs ONCE (not per-user) at 3 AM ET.
            # 3 AM is well after market close and well before any pre-market
            # activity, minimizing contention on the volume.
            if now_et.hour == 3 and now_et.minute >= 0:
                if should_run_daily_at("daily_backup_all", 3, 0):
                    run_daily_backup()
        except Exception as e:
            log(f"Scheduler loop error: {e}", "scheduler")

        time.sleep(30)

def start_scheduler():
    global _scheduler_thread, _scheduler_running
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_running = True
    _scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True, name="CloudScheduler")
    _scheduler_thread.start()
    log("Scheduler thread started", "scheduler")

def stop_scheduler():
    global _scheduler_running
    _scheduler_running = False
    log("Scheduler stop requested", "scheduler")

def get_scheduler_status():
    is_alive = _scheduler_running and _scheduler_thread is not None and _scheduler_thread.is_alive()
    et_now = get_et_time()
    # Use zoneinfo to determine EDT vs EST — handles DST boundaries correctly.
    try:
        from zoneinfo import ZoneInfo
        _aware = datetime.now(ZoneInfo("America/New_York"))
        tz_label = _aware.tzname() or "ET"  # e.g. "EDT" or "EST"
    except Exception:
        month = now_et().month
        tz_label = "EDT" if 3 <= month <= 11 else "EST"
    # Send as a PRE-FORMATTED string so the browser doesn't re-convert timezones
    et_display = et_now.strftime("%-I:%M:%S %p ") + tz_label
    et_date_display = et_now.strftime("%a %b %-d")

    users = get_all_users_for_scheduling()
    user_info = []
    for u in users:
        user_info.append({
            "id": u["id"],
            "username": u["username"],
            "endpoint": u["_api_endpoint"],
        })

    with _logs_lock:
        logs = list(_recent_logs[-20:])

    # Check market once via first user (all users share the same market clock)
    market_open = False
    if users:
        try:
            clock = user_api_get(users[0], "/clock")
            if isinstance(clock, dict):
                market_open = clock.get("is_open", False)
        except Exception:
            pass

    return {
        "running": is_alive,
        "thread_name": _scheduler_thread.name if _scheduler_thread else None,
        "last_runs": dict(_last_runs),
        "current_et": et_now.isoformat(),       # kept for backward compat
        "current_et_display": et_display,       # use this in UI ("3:30:00 AM EDT")
        "current_et_date": et_date_display,     # e.g. "Thu Apr 16"
        "tz_label": tz_label,
        "market_open": market_open,
        "recent_logs": logs,
        "users": user_info,
        "user_count": len(users),
    }

if __name__ == "__main__":
    start_scheduler()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        stop_scheduler()
