#!/usr/bin/env python3
"""
Cloud-native scheduler for the Alpaca trading bot.
Runs as a background thread in server.py, replacing Claude Code scheduled tasks.
All trading logic runs on Railway 24/7 without needing user's laptop.
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
STRATEGIES_DIR = os.path.join(BASE_DIR, "strategies")

API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
DATA_ENDPOINT = os.environ.get("ALPACA_DATA_ENDPOINT", "https://data.alpaca.markets/v2")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

_scheduler_thread = None
_scheduler_running = False
_last_runs = {}
_recent_logs = []  # Circular buffer for dashboard display
_logs_lock = threading.Lock()

def log(msg, task="scheduler"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    line = f"[{ts}] [{task}] {msg}"
    print(line, flush=True)
    with _logs_lock:
        _recent_logs.append({"ts": ts, "task": task, "msg": msg})
        if len(_recent_logs) > 100:
            _recent_logs.pop(0)

def api_get(url, timeout=10):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def api_post(url, data, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
        headers={**HEADERS, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def api_delete(url, timeout=10):
    req = urllib.request.Request(url, headers=HEADERS, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode()) if body else {}
    except Exception as e:
        return {"error": str(e)}

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return None

def save_json(path, data):
    dir_name = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except:
        try: os.unlink(tmp)
        except: pass
        raise

def notify(message, notify_type="info"):
    try:
        subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "notify.py"),
                         "--type", notify_type, message])
    except Exception as e:
        log(f"Notification failed: {e}")

def is_market_open():
    result = api_get(f"{API_ENDPOINT}/clock")
    if isinstance(result, dict) and "error" not in result:
        return result.get("is_open", False)
    return False

def get_et_time():
    # Rough ET — March to November is EDT (UTC-4), rest is EST (UTC-5)
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset = -4 if 3 <= month <= 11 else -5
    return now_utc + timedelta(hours=offset)

# ============================================================================
# TASK 1: SCREENER
# ============================================================================
def run_screener(max_age_seconds=0):
    """Run the stock screener. If max_age_seconds > 0, skip if last run was within that window."""
    if max_age_seconds > 0:
        last = _last_runs.get("screener", 0)
        if isinstance(last, (int, float)) and time.time() - last < max_age_seconds:
            age = int(time.time() - last)
            log(f"Screener data is {age}s old (< {max_age_seconds}s). Skipping duplicate run.", "screener")
            return
    log("Starting screener...", "screener")
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "update_dashboard.py")],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            log("Screener completed", "screener")
            _last_runs["screener"] = time.time()
        else:
            log(f"Screener failed: {result.stderr[:200]}", "screener")
    except subprocess.TimeoutExpired:
        log("Screener timed out (>180s)", "screener")
    except Exception as e:
        log(f"Screener error: {e}", "screener")

# ============================================================================
# TASK 2: STRATEGY MONITOR
# ============================================================================
def monitor_strategies():
    try:
        guardrails = load_json(os.path.join(BASE_DIR, "guardrails.json")) or {}
        if guardrails.get("kill_switch"):
            return

        # Daily loss check
        account = api_get(f"{API_ENDPOINT}/account")
        if isinstance(account, dict) and "error" not in account:
            current_val = float(account.get("portfolio_value", 0))
            daily_start = guardrails.get("daily_starting_value")
            # Fallback: if auto-deployer never ran to set daily_starting_value
            # (disabled, kill switch was on, cooldown, etc.), set it now from
            # current value so the kill-switch safety is always armed.
            if not daily_start and current_val > 0:
                guardrails["daily_starting_value"] = current_val
                save_json(os.path.join(BASE_DIR, "guardrails.json"), guardrails)
                daily_start = current_val
            if daily_start:
                loss_pct = (daily_start - current_val) / daily_start
                if loss_pct > guardrails.get("daily_loss_limit_pct", 0.03):
                    guardrails["kill_switch"] = True
                    guardrails["kill_switch_triggered_at"] = datetime.now(timezone.utc).isoformat()
                    guardrails["kill_switch_reason"] = f"Daily loss {loss_pct*100:.1f}%"
                    save_json(os.path.join(BASE_DIR, "guardrails.json"), guardrails)
                    api_delete(f"{API_ENDPOINT}/orders")
                    api_delete(f"{API_ENDPOINT}/positions")
                    notify(f"KILL SWITCH: Daily loss {loss_pct*100:.1f}% exceeded.", "kill")
                    log(f"KILL SWITCH triggered: {loss_pct*100:.1f}% loss", "monitor")
                    return

        if not os.path.isdir(STRATEGIES_DIR):
            return
        for fname in os.listdir(STRATEGIES_DIR):
            if not fname.endswith(".json"):
                continue
            if fname in ("copy_trading.json", "wheel_strategy.json"):
                continue
            filepath = os.path.join(STRATEGIES_DIR, fname)
            strat = load_json(filepath)
            if not strat:
                continue
            status = strat.get("status", "")
            symbol = strat.get("symbol")
            if status not in ("active", "awaiting_fill") or not symbol:
                continue
            try:
                process_strategy_file(filepath, strat)
            except Exception as e:
                log(f"Error processing {fname}: {e}", "monitor")
    except Exception as e:
        log(f"Monitor error: {e}", "monitor")

def check_profit_ladder(filepath, strat, price, entry, shares):
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

        order = api_post(f"{API_ENDPOINT}/orders", {
            "symbol": symbol, "qty": str(sell_qty), "side": "sell",
            "type": "market", "time_in_force": "day"
        })

        if isinstance(order, dict) and "id" in order:
            takes.append(level)
            state["profit_takes"] = takes
            remaining = shares - sell_qty
            state["total_shares_held"] = remaining

            # CRITICAL: resize the protective stop to match remaining shares,
            # otherwise if the stop triggers it sells MORE than we hold and
            # Alpaca will reject or (with short-enabled accounts) open a short.
            old_stop_id = state.get("stop_order_id")
            current_stop_price = state.get("current_stop_price")
            if old_stop_id and remaining > 0 and current_stop_price:
                api_delete(f"{API_ENDPOINT}/orders/{old_stop_id}")
                new_stop = api_post(f"{API_ENDPOINT}/orders", {
                    "symbol": symbol, "qty": str(remaining), "side": "sell",
                    "type": "stop", "stop_price": str(current_stop_price),
                    "time_in_force": "gtc"
                })
                if isinstance(new_stop, dict) and "id" in new_stop:
                    state["stop_order_id"] = new_stop["id"]
                else:
                    # Stop replacement failed — clear stop_order_id so next
                    # monitor tick will re-place a fresh stop.
                    state["stop_order_id"] = None
                    log(f"{symbol}: WARN stop resize failed after profit-take — will re-place next tick", "monitor")
            elif old_stop_id and remaining <= 0:
                api_delete(f"{API_ENDPOINT}/orders/{old_stop_id}")
                state["stop_order_id"] = None

            # Ensure state is attached to strat before save (defensive)
            strat["state"] = state
            save_json(filepath, strat)
            log(f"{symbol}: Profit take level {level}% — sold {sell_qty} shares", "monitor")
            notify(f"Profit take on {symbol} at +{level}%: sold {sell_qty} shares. {remaining} still held.", "exit")
            return  # One level per check

def process_strategy_file(filepath, strat):
    symbol = strat["symbol"]
    state = strat.setdefault("state", {})  # ensure mutations persist in strat
    rules = strat.get("rules", {})
    strategy_type = strat.get("strategy", "trailing_stop")

    # Check entry fill
    entry_order_id = state.get("entry_order_id")
    if entry_order_id and not state.get("entry_fill_price"):
        order = api_get(f"{API_ENDPOINT}/orders/{entry_order_id}")
        if isinstance(order, dict) and order.get("status") == "filled":
            state["entry_fill_price"] = float(order.get("filled_avg_price", 0))
            state["total_shares_held"] = int(float(order.get("filled_qty", 0)))
            strat["status"] = "active"
            log(f"{symbol}: Entry filled at ${state['entry_fill_price']:.2f}", "monitor")

    entry = state.get("entry_fill_price")
    shares = state.get("total_shares_held", 0)
    if not entry or shares <= 0:
        save_json(filepath, strat)
        return

    # Get current price
    trade = api_get(f"{DATA_ENDPOINT}/stocks/{symbol}/trades/latest?feed=iex")
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
        order = api_post(f"{API_ENDPOINT}/orders", {
            "symbol": symbol, "qty": str(shares), "side": "sell",
            "type": "stop", "stop_price": str(stop_price), "time_in_force": "gtc"
        })
        if isinstance(order, dict) and "id" in order:
            state["stop_order_id"] = order["id"]
            state["current_stop_price"] = stop_price
            log(f"{symbol}: Stop-loss placed at ${stop_price}", "monitor")
            notify(f"Stop-loss placed on {symbol} at ${stop_price:.2f}", "info")

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
            log(f"{symbol}: Trailing activated", "monitor")
        if state.get("trailing_activated"):
            new_stop = round(highest * (1 - trail), 2)
            current_stop = state.get("current_stop_price", 0) or 0
            if new_stop > current_stop:
                old_id = state.get("stop_order_id")
                # Place the NEW stop before canceling the old one, so the
                # position is never unprotected if either call fails.
                new_order = api_post(f"{API_ENDPOINT}/orders", {
                    "symbol": symbol, "qty": str(shares), "side": "sell",
                    "type": "stop", "stop_price": str(new_stop), "time_in_force": "gtc"
                })
                if isinstance(new_order, dict) and "id" in new_order:
                    if old_id:
                        api_delete(f"{API_ENDPOINT}/orders/{old_id}")
                    state["stop_order_id"] = new_order["id"]
                    state["current_stop_price"] = new_stop
                    log(f"{symbol}: Stop raised ${current_stop:.2f} -> ${new_stop:.2f}", "monitor")
                    notify(f"Stop raised on {symbol}: ${current_stop:.2f} -> ${new_stop:.2f}", "info")
                else:
                    # New stop placement failed — keep the old stop in place.
                    log(f"{symbol}: WARN stop raise failed, keeping prior stop at ${current_stop:.2f}. Err: {new_order}", "monitor")

    # Mean reversion target check
    if strategy_type == "mean_reversion":
        if price >= entry * 1.15:
            order = api_post(f"{API_ENDPOINT}/orders", {
                "symbol": symbol, "qty": str(shares), "side": "sell",
                "type": "market", "time_in_force": "day"
            })
            if isinstance(order, dict) and "id" in order:
                strat["status"] = "closed"
                state["exit_reason"] = "target_hit"
                pnl = (price - entry) * shares
                log(f"{symbol}: Target hit. P&L ${pnl:.2f}", "monitor")
                notify(f"Profit taken on {symbol}: sold at ${price:.2f} (+{((price/entry-1)*100):.1f}%)", "exit")

    # Feature 8: Partial profit taking
    check_profit_ladder(filepath, strat, price, entry, shares)
    # Refresh shares count in case profit ladder sold some
    shares = strat.get("state", {}).get("total_shares_held", shares)

    # Check stop triggered
    if state.get("stop_order_id"):
        order = api_get(f"{API_ENDPOINT}/orders/{state['stop_order_id']}")
        if isinstance(order, dict) and order.get("status") == "filled":
            exit_price = float(order.get("filled_avg_price", state.get("current_stop_price", 0)))
            pnl = (exit_price - entry) * shares
            state["total_shares_held"] = 0
            state["stop_order_id"] = None
            state["exit_price"] = exit_price
            state["exit_reason"] = "stop_triggered"
            strat["status"] = "closed"
            log(f"{symbol}: STOP TRIGGERED at ${exit_price}, P&L ${pnl:.2f}", "monitor")
            notify(f"{symbol} stopped out at ${exit_price:.2f}. P&L: ${pnl:.2f}", "stop")
            guardrails = load_json(os.path.join(BASE_DIR, "guardrails.json")) or {}
            guardrails["last_loss_time"] = datetime.now(timezone.utc).isoformat()
            save_json(os.path.join(BASE_DIR, "guardrails.json"), guardrails)

    save_json(filepath, strat)

# ============================================================================
# TASK 3: AUTO-DEPLOYER
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

def run_auto_deployer():
    log("Running auto-deployer...", "deployer")

    guardrails = load_json(os.path.join(BASE_DIR, "guardrails.json")) or {}
    if guardrails.get("kill_switch"):
        log("Kill switch active. Skipping.", "deployer")
        return

    config = load_json(os.path.join(BASE_DIR, "auto_deployer_config.json")) or {}
    if not config.get("enabled", True):
        log("Auto-deployer disabled. Skipping.", "deployer")
        return

    # Cooldown check
    last_loss = guardrails.get("last_loss_time")
    if last_loss:
        try:
            last_dt = datetime.fromisoformat(last_loss.replace("Z", "+00:00"))
            cooldown_min = guardrails.get("cooldown_after_loss_minutes", 60)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < cooldown_min * 60:
                log(f"In cooldown after recent loss. Skipping.", "deployer")
                return
        except:
            pass

    # Set daily starting value
    account = api_get(f"{API_ENDPOINT}/account")
    if isinstance(account, dict) and "error" not in account:
        guardrails["daily_starting_value"] = float(account.get("portfolio_value", 0))
        current = float(account.get("portfolio_value", 0))
        peak = guardrails.get("peak_portfolio_value", current)
        if current > peak:
            guardrails["peak_portfolio_value"] = current
        save_json(os.path.join(BASE_DIR, "guardrails.json"), guardrails)

    # Capital check
    try:
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "capital_check.py")],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=30)
        capital = load_json(os.path.join(BASE_DIR, "capital_status.json")) or {}
        if not capital.get("can_trade", True):
            log(f"Cannot trade: {capital.get('recommendation')}", "deployer")
            notify(f"Auto-deployer skipped: {capital.get('recommendation','insufficient capital')}", "info")
            return
    except Exception as e:
        log(f"Capital check error: {e}", "deployer")

    # Run screener to get fresh picks (skip if already ran in last 5 min to avoid duplicate API calls)
    run_screener(max_age_seconds=300)

    picks_data = load_json(os.path.join(BASE_DIR, "dashboard_data.json")) or {}
    top_picks = picks_data.get("picks", [])[:5]
    market_regime = picks_data.get("market_regime", "neutral")
    spy_mom = picks_data.get("spy_momentum_20d", 0)

    max_per_day = config.get("max_new_per_day", 2)
    deployed = 0

    positions = api_get(f"{API_ENDPOINT}/positions")
    existing_syms = set()
    if isinstance(positions, list):
        existing_syms = {p.get("symbol") for p in positions}

    for pick in top_picks:
        if deployed >= max_per_day:
            break
        symbol = pick.get("symbol")
        best_strat = pick.get("best_strategy", "").lower().replace(" ", "_")

        if symbol in existing_syms:
            continue
        if pick.get("earnings_warning"):
            log(f"{symbol}: Skipped (earnings warning)", "deployer")
            continue
        if best_strat not in ("trailing_stop", "breakout", "mean_reversion"):
            continue

        # Feature 10: Correlation check
        current_positions = api_get(f"{API_ENDPOINT}/positions") or []
        current_positions = current_positions if isinstance(current_positions, list) else []
        allowed, reason = check_correlation_allowed(symbol, current_positions)
        if not allowed:
            log(f"{symbol}: Skipped ({reason})", "deployer")
            continue

        # Do NOT use `or 1` here — if screener said recommended_shares=0
        # that means "don't buy". Treat missing (None) as skip too.
        rs = pick.get("recommended_shares")
        if rs is None:
            log(f"{symbol}: Skipped (no recommended_shares from screener)", "deployer")
            continue
        try:
            qty = int(rs)
        except (TypeError, ValueError):
            log(f"{symbol}: Skipped (bad recommended_shares: {rs!r})", "deployer")
            continue
        if qty < 1:
            log(f"{symbol}: Skipped (recommended_shares < 1)", "deployer")
            continue

        order = api_post(f"{API_ENDPOINT}/orders", {
            "symbol": symbol, "qty": str(qty), "side": "buy",
            "type": "market", "time_in_force": "day"
        })

        if isinstance(order, dict) and "id" in order:
            strat_file = {
                "symbol": symbol,
                "strategy": best_strat,
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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
            save_json(os.path.join(STRATEGIES_DIR, filename), strat_file)

            # Log to trade journal
            journal_path = os.path.join(BASE_DIR, "trade_journal.json")
            journal = load_json(journal_path) or {"trades": [], "daily_snapshots": []}
            _score = pick.get("best_score")
            _rsi = pick.get("rsi", 50)
            _score_str = f"{_score:.0f}" if isinstance(_score, (int, float)) else "n/a"
            _rsi_str = f"{_rsi:.0f}" if isinstance(_rsi, (int, float)) else "n/a"
            journal["trades"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol, "side": "buy", "qty": qty,
                "price": pick.get("price"),
                "strategy": best_strat,
                "reason": f"Auto-deployed. Score {_score_str}, RSI {_rsi_str}, Bias {pick.get('overall_bias','?')}",
                "deployer": "cloud_scheduler",
                "status": "open",
            })
            save_json(journal_path, journal)

            log(f"DEPLOYED: {best_strat} on {symbol} x {qty} @ ~${pick.get('price',0):.2f}", "deployer")
            notify(f"Deployed {best_strat} on {symbol}: {qty} shares @ ~${pick.get('price',0):.2f}", "trade")
            deployed += 1
        else:
            log(f"Order failed for {symbol}: {order}", "deployer")

    # Short selling if bear market
    short_config = config.get("short_selling", {})
    if short_config.get("enabled") and deployed < max_per_day:
        if market_regime == "bear" and spy_mom < short_config.get("require_spy_20d_below", -3):
            short_candidates = picks_data.get("short_candidates", [])
            min_score = short_config.get("min_short_score", 15)
            for sc in short_candidates[:1]:  # Max 1 short per run
                if sc.get("short_score", 0) < min_score:
                    continue
                if sc.get("meme_warning") and short_config.get("skip_if_meme_warning", True):
                    continue
                if sc.get("symbol") in existing_syms:
                    continue
                # TODO: implement short deploy (sell without position = short)
                log(f"Short candidate available: {sc.get('symbol')} (score {sc.get('short_score')})", "deployer")

    if deployed == 0:
        notify("Morning scan complete. No qualifying trades today.", "info")
    log(f"Auto-deployer done. Deployed {deployed} trades.", "deployer")

# ============================================================================
# TASK 4: DAILY CLOSE
# ============================================================================
def run_daily_close():
    log("Running daily close...", "close")
    try:
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "update_scorecard.py")],
                      cwd=BASE_DIR, capture_output=True, text=True, timeout=60)
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "error_recovery.py")],
                      cwd=BASE_DIR, capture_output=True, text=True, timeout=60)

        guardrails = load_json(os.path.join(BASE_DIR, "guardrails.json")) or {}
        guardrails["daily_starting_value"] = None
        account = api_get(f"{API_ENDPOINT}/account")
        if isinstance(account, dict) and "error" not in account:
            current = float(account.get("portfolio_value", 0))
            peak = guardrails.get("peak_portfolio_value", current)
            if current > peak:
                guardrails["peak_portfolio_value"] = current
        save_json(os.path.join(BASE_DIR, "guardrails.json"), guardrails)

        scorecard = load_json(os.path.join(BASE_DIR, "scorecard.json")) or {}
        value = scorecard.get("current_value", 0)
        win_rate = scorecard.get("win_rate_pct", 0)
        readiness = scorecard.get("readiness_score", 0)
        ready_flag = " READY FOR LIVE!" if readiness >= 80 else ""
        notify(f"Daily close: ${value:,.2f} | Win {win_rate:.0f}% | Ready {readiness}/100{ready_flag}", "daily")
        log("Daily close complete", "close")
    except Exception as e:
        log(f"Daily close error: {e}", "close")

# ============================================================================
# TASK 5: WEEKLY LEARNING
# ============================================================================
def run_weekly_learning():
    log("Running weekly learning...", "learn")
    try:
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "learn.py")],
                      cwd=BASE_DIR, capture_output=True, text=True, timeout=120)
        notify("Weekly learning engine completed", "learn")
    except Exception as e:
        log(f"Learning error: {e}", "learn")

# ============================================================================
# TASK 6: FRIDAY RISK REDUCTION
# ============================================================================
def run_friday_risk_reduction():
    """Scale out of profitable positions before weekend gap risk."""
    log("Running Friday risk reduction...", "friday")

    positions = api_get(f"{API_ENDPOINT}/positions")
    if not isinstance(positions, list):
        log(f"Could not fetch positions: {positions}", "friday")
        return

    actions_taken = 0
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

        half_qty = qty // 2
        log(f"{symbol}: +{unrealized_plpc:.1f}%, selling {half_qty}/{qty} before weekend", "friday")

        order = api_post(f"{API_ENDPOINT}/orders", {
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
            notify(f"Friday risk reduction: trimmed {half_qty} {symbol} locking in ~${profit:.2f}. {qty - half_qty} shares still held.", "exit")
        else:
            log(f"Failed to trim {symbol}: {order}", "friday")

    if actions_taken > 0:
        notify(f"Weekend prep complete: scaled out of {actions_taken} winning positions", "info")
    else:
        log("No positions met scale-out criteria", "friday")

# ============================================================================
# TASK 7: MONTHLY REBALANCE
# ============================================================================
def run_monthly_rebalance():
    """Monthly review: close long-underwater positions, free capital."""
    log("Running monthly rebalance...", "rebalance")

    positions = api_get(f"{API_ENDPOINT}/positions")
    if not isinstance(positions, list):
        return

    closed_count = 0
    for pos in positions:
        symbol = pos.get("symbol", "")
        qty = abs(int(float(pos.get("qty", 0))))
        unrealized_plpc = float(pos.get("unrealized_plpc", 0)) * 100

        # Check position age via strategy file
        strat_files = [f for f in os.listdir(STRATEGIES_DIR)
                      if f.endswith(".json") and symbol in f]

        too_old = False
        for sf in strat_files:
            strat = load_json(os.path.join(STRATEGIES_DIR, sf))
            if not strat:
                continue
            created = strat.get("created")
            if created:
                try:
                    # Accept both "YYYY-MM-DD" and full ISO timestamps
                    created_str = str(created)[:10]
                    age_days = (datetime.now(timezone.utc).date() -
                               datetime.strptime(created_str, "%Y-%m-%d").date()).days
                    if age_days >= 60:
                        too_old = True
                        break
                except Exception:
                    pass

        # Close if old AND losing
        if too_old and unrealized_plpc < -2:
            log(f"{symbol}: 60+ days old, {unrealized_plpc:.1f}% down — closing for rebalance", "rebalance")
            order = api_post(f"{API_ENDPOINT}/orders", {
                "symbol": symbol, "qty": str(qty),
                "side": "sell" if float(pos.get("qty", 0)) > 0 else "buy",
                "type": "market", "time_in_force": "day"
            })
            if isinstance(order, dict) and "id" in order:
                closed_count += 1
                notify(f"Monthly rebalance: closed underwater {symbol} (~{unrealized_plpc:.1f}% loss) to free capital", "info")

    if closed_count > 0:
        notify(f"Monthly rebalance: closed {closed_count} stale losing positions", "daily")
    else:
        notify("Monthly rebalance: all positions healthy, no changes", "info")

def is_first_trading_day_of_month():
    """Check if today is the first trading day of the month (using Alpaca clock)."""
    now_et = get_et_time()
    if now_et.day > 7:
        return False  # Definitely not first trading day
    # Check calendar via Alpaca
    start = now_et.replace(day=1).strftime("%Y-%m-%d")
    end = now_et.strftime("%Y-%m-%d")
    cal = api_get(f"{API_ENDPOINT}/calendar?start={start}&end={end}")
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

def scheduler_loop():
    global _scheduler_running
    log("Cloud scheduler loop started", "scheduler")
    notify("Cloud scheduler started — bot is autonomous on Railway", "info")

    while _scheduler_running:
        try:
            now_et = get_et_time()
            weekday = now_et.weekday()
            is_weekday = weekday < 5
            market_open = is_market_open() if is_weekday else False

            # Auto-deployer: weekdays 9:35 AM ET (skip on market holidays)
            if is_weekday and now_et.hour == 9 and now_et.minute >= 35 and market_open:
                if should_run_daily_at("auto_deployer", 9, 35):
                    run_auto_deployer()

            # Screener: every 30 min during market hours
            if market_open and should_run_interval("screener", 30 * 60):
                run_screener()

            # Strategy monitor: every 60s during market hours
            if market_open and should_run_interval("monitor", 60):
                monitor_strategies()

            # Daily close: weekdays 4:05 PM ET
            if is_weekday and now_et.hour == 16 and now_et.minute >= 5:
                if should_run_daily_at("daily_close", 16, 5):
                    run_daily_close()

            # Weekly learning: Fridays 5:00 PM ET
            if weekday == 4 and now_et.hour == 17:
                if should_run_daily_at("weekly_learning", 17, 0):
                    run_weekly_learning()

            # Feature 6: Friday risk reduction at 3:45 PM ET
            if weekday == 4 and now_et.hour == 15 and now_et.minute >= 45:
                if should_run_daily_at("friday_reduction", 15, 45):
                    run_friday_risk_reduction()

            # Feature 19: Monthly rebalance on first trading day at 9:45 AM ET
            if is_weekday and now_et.hour == 9 and now_et.minute >= 45:
                if is_first_trading_day_of_month():
                    if should_run_daily_at("monthly_rebalance", 9, 45):
                        run_monthly_rebalance()
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
    # Determine if we're in EDT or EST based on month
    month = datetime.now(timezone.utc).month
    tz_label = "EDT" if 3 <= month <= 11 else "EST"
    # Send as a PRE-FORMATTED string so the browser doesn't re-convert timezones
    et_display = et_now.strftime("%-I:%M:%S %p ") + tz_label
    et_date_display = et_now.strftime("%a %b %-d")
    with _logs_lock:
        logs = list(_recent_logs[-20:])
    return {
        "running": is_alive,
        "thread_name": _scheduler_thread.name if _scheduler_thread else None,
        "last_runs": dict(_last_runs),
        "current_et": et_now.isoformat(),       # kept for backward compat
        "current_et_display": et_display,       # use this in UI ("3:30:00 AM EDT")
        "current_et_date": et_date_display,     # e.g. "Thu Apr 16"
        "tz_label": tz_label,
        "market_open": is_market_open(),
        "recent_logs": logs,
    }

if __name__ == "__main__":
    start_scheduler()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        stop_scheduler()
