"""
Strategy deployment and lifecycle handlers (deploy, pause, stop, preset).
Mixed into DashboardHandler via MRO.
"""
import json
import os
import re
import urllib.request
from datetime import timedelta

from et_time import now_et
import glob
# Lazy server-module proxy: resolves `server.X` references at first
# *call* time, not import time. Required because server.py is launched
# as `python3 server.py` which makes it __main__, so `import server`
# at mixin import time re-executes server.py and crashes on the
# circular import (server -> mixin -> server). By then server.py has
# finished loading so the attribute lookup succeeds.
import sys as _sys
class _ServerProxy:
    def __getattr__(self, name):
        s = _sys.modules.get("server") or _sys.modules.get("__main__")
        if s is None:
            import server as _s
            s = _s
        return getattr(s, name)
server = _ServerProxy()


class StrategyHandlerMixin:
    def handle_deploy(self, body):
        """Deploy a strategy on a symbol."""
        symbol = body.get("symbol", "").upper()
        strategy = body.get("strategy", "trailing_stop")
        try:
            qty = int(body.get("qty", 2))
        except (TypeError, ValueError):
            return self.send_json({"error": "Invalid quantity."}, 400)

        if not symbol:
            self.send_json({"error": "Missing symbol"}, 400)
            return
        # Validate symbol is alphanumeric (1-10 chars)
        if not re.match(r'^[A-Z]{1,10}$', symbol):
            return self.send_json({"error": "Invalid symbol format"}, 400)

        if qty < 1 or qty > 1000:
            return self.send_json({"error": "Invalid quantity. Must be 1-1000."}, 400)

        if strategy == "trailing_stop":
            self.deploy_trailing_stop(symbol, qty)
        elif strategy == "wheel":
            self.deploy_wheel(symbol, qty)
        elif strategy == "copy_trading":
            self.deploy_copy_trading(symbol, qty)
        elif strategy == "mean_reversion":
            self.deploy_mean_reversion(symbol, qty)
        elif strategy == "breakout":
            self.deploy_breakout(symbol, qty)
        else:
            self.send_json({"error": f"Unknown strategy: {strategy}"}, 400)
    def deploy_trailing_stop(self, symbol, qty):
        """Deploy trailing stop strategy: buy shares, set stop loss, place ladder buys."""
        # 1. Get current price
        snap_url = f"{self.user_data_endpoint}/stocks/{symbol}/snapshot?feed=iex"
        snap = self.user_api_get(snap_url)
        if "error" in snap:
            # Try SIP feed
            snap_url = f"{self.user_data_endpoint}/stocks/{symbol}/snapshot"
            snap = self.user_api_get(snap_url)
        price = 0
        if isinstance(snap, dict):
            lt = snap.get("latestTrade", {})
            price = lt.get("p", 0)
        if not price:
            self.send_json({"error": f"Could not get price for {symbol}"}, 400)
            return

        # 2. Market buy
        buy_order = self.user_api_post(f"{self.user_api_endpoint}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })
        if isinstance(buy_order, dict) and "error" in buy_order:
            self.send_json({"error": f"Buy order failed: {buy_order['error']}"}, 400)
            return

        buy_order_id = buy_order.get("id", "")

        # NOTE: Stop-loss is NOT placed here. The strategy-monitor will place
        # the stop-loss AFTER the buy order fills (checks state.stop_pending).
        stop_price = round(price * 0.90, 2)

        # 3. Ladder buy orders at -12%, -20%, -30%, -40%
        # Check buying power first so we don't place ladders we can't afford
        acct = self.user_api_get(f"{self.user_api_endpoint}/account")
        buying_power = float(acct.get("buying_power", 0)) if isinstance(acct, dict) else 0
        ladder_levels = [
            {"drop_pct": 0.12, "qty": max(1, qty // 2), "note": "re-entry just below stop-out"},
            {"drop_pct": 0.20, "qty": qty, "note": "meaningful pullback"},
            {"drop_pct": 0.30, "qty": qty + 1, "note": "deep correction"},
            {"drop_pct": 0.40, "qty": qty * 2 + 1, "note": "crash territory, go heavy"},
        ]
        # Calculate worst-case cost (all ladders fill) and skip some if insufficient buying power
        cumulative_cost = 0
        affordable_levels = []
        for level in ladder_levels:
            ladder_price = round(price * (1 - level["drop_pct"]), 2)
            cost = ladder_price * level["qty"]
            if cumulative_cost + cost <= buying_power:
                cumulative_cost += cost
                affordable_levels.append(level)
        ladder_levels = affordable_levels

        ladder_orders = []
        for level in ladder_levels:
            ladder_price = round(price * (1 - level["drop_pct"]), 2)
            ladder_order = self.user_api_post(f"{self.user_api_endpoint}/orders", {
                "symbol": symbol,
                "qty": str(level["qty"]),
                "side": "buy",
                "type": "limit",
                "limit_price": str(ladder_price),
                "time_in_force": "gtc",
            })
            order_id = ladder_order.get("id", "") if isinstance(ladder_order, dict) else ""
            ladder_orders.append({
                "level": len(ladder_orders) + 1,
                "drop_pct": level["drop_pct"],
                "price": ladder_price,
                "qty": level["qty"],
                "order_id": order_id,
                "note": level["note"],
            })

        # 5. Save strategy state
        strategy_data = {
            "symbol": symbol,
            "strategy": "trailing_stop_with_ladder",
            "created": now_et().strftime("%Y-%m-%d"),
            "entry_price_estimate": price,
            "initial_qty": qty,
            "status": "awaiting_fill",
            "rules": {
                "stop_loss_pct": 0.10,
                "trailing_activation_pct": 0.10,
                "trailing_distance_pct": 0.05,
                "ladder_in": ladder_orders,
            },
            "state": {
                "entry_fill_price": None,
                "entry_order_id": buy_order_id,
                "stop_order_id": None,
                "stop_pending": True,
                "highest_price_seen": None,
                "trailing_activated": False,
                "current_stop_price": stop_price,
                "total_shares_held": 0,
                "ladder_fills": [],
            },
        }
        # Per-symbol file so multiple trailing stops don't overwrite each other (and per-user)
        server.save_json(os.path.join(self._user_strategies_dir(), f"trailing_stop_{symbol}.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "trailing_stop",
            "symbol": symbol,
            "buy_order_id": buy_order_id,
            "stop_price": stop_price,
            "ladder_orders": len(ladder_orders),
            "price": price,
            "note": "Stop-loss will be placed by strategy-monitor after buy fills.",
        })
    def deploy_wheel(self, symbol, qty):
        """Deploy wheel strategy: check cash for 100 shares, place first put."""
        snap_url = f"{self.user_data_endpoint}/stocks/{symbol}/snapshot?feed=iex"
        snap = self.user_api_get(snap_url)
        if "error" in snap:
            snap_url = f"{self.user_data_endpoint}/stocks/{symbol}/snapshot"
            snap = self.user_api_get(snap_url)
        price = 0
        if isinstance(snap, dict):
            lt = snap.get("latestTrade", {})
            price = lt.get("p", 0)
        if not price:
            self.send_json({"error": f"Could not get price for {symbol}"}, 400)
            return

        # Check account for enough cash for 100 shares
        acct = self.user_api_get(f"{self.user_api_endpoint}/account")
        cash = float(acct.get("cash", 0)) if isinstance(acct, dict) else 0
        needed = price * 100
        if cash < needed:
            self.send_json({
                "error": f"Insufficient cash for wheel. Need ${needed:,.2f} for 100 shares of {symbol} at ${price:.2f}, have ${cash:,.2f}"
            }, 400)
            return

        # Save strategy state (wheel requires options which paper may not support fully)
        strategy_data = {
            "strategy": "wheel_strategy",
            "created": now_et().strftime("%Y-%m-%d"),
            "symbol": symbol,
            "status": "active",
            "rules": {
                "put_strike_pct_below": 0.10,
                "call_strike_pct_above": 0.10,
                "expiration_weeks": [2, 3, 4],
                "early_close_profit_pct": 0.50,
                "check_interval_minutes": 15,
                "never_sell_put_without_cash": True,
                "never_sell_call_below_cost_basis": True,
            },
            "state": {
                "current_stage": "stage_1_sell_puts",
                "shares_owned": 0,
                "cost_basis": None,
                "active_contract": None,
                "cycles_completed": 0,
                "total_premiums_collected": 0,
                "total_stock_gains": 0,
                "history": [],
            },
        }
        server.save_json(os.path.join(self._user_strategies_dir(), "wheel_strategy.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "wheel",
            "symbol": symbol,
            "price": price,
            "cash_available": cash,
            "cash_needed": needed,
            "message": f"Wheel strategy initialized for {symbol}. Stage 1: Ready to sell puts.",
        })
    def deploy_copy_trading(self, symbol, qty):
        """Deploy copy trading strategy: start tracking."""
        strategy_data = {
            "strategy": "copy_trading",
            "created": now_et().strftime("%Y-%m-%d"),
            "status": "active",
            "source": "capitol_trades",
            "source_url": "https://www.capitoltrades.com",
            "rules": {
                "politician": None,
                "selection_criteria": "highest_recent_returns_and_active",
                "trade_delay_max_days": 7,
                "position_size_pct": 0.05,
                "max_positions": 10,
                "skip_if_price_moved_pct": 0.15,
                "stop_loss_pct": 0.10,
            },
            "state": {
                "selected_politician": None,
                "selection_reason": None,
                "last_scan": now_et().strftime("%Y-%m-%d %I:%M:%S %p ET"),
                "trades_copied": [],
                "active_positions": [],
                "total_premium_collected": 0,
                "total_realized_pnl": 0,
            },
        }
        server.save_json(os.path.join(self._user_strategies_dir(), "copy_trading.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "copy_trading",
            "symbol": symbol,
            "message": "Copy trading strategy initialized. Awaiting politician selection and trade signals.",
        })
    def deploy_mean_reversion(self, symbol, qty):
        """Deploy mean reversion: buy shares, set limit sell at 20-day avg estimate, set stop-loss."""
        snap_url = f"{self.user_data_endpoint}/stocks/{symbol}/snapshot?feed=iex"
        snap = self.user_api_get(snap_url)
        if "error" in snap:
            snap_url = f"{self.user_data_endpoint}/stocks/{symbol}/snapshot"
            snap = self.user_api_get(snap_url)
        price = 0
        if isinstance(snap, dict):
            lt = snap.get("latestTrade", {})
            price = lt.get("p", 0)
        if not price:
            self.send_json({"error": f"Could not get price for {symbol}"}, 400)
            return

        # 1. Market buy
        buy_order = self.user_api_post(f"{self.user_api_endpoint}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })
        if isinstance(buy_order, dict) and "error" in buy_order:
            self.send_json({"error": f"Buy order failed: {buy_order['error']}"}, 400)
            return
        buy_order_id = buy_order.get("id", "")

        # NOTE: No limit sell placed here. The strategy-monitor handles the
        # profit target by checking price vs 20-day average each cycle.
        # NOTE: Stop-loss is NOT placed here. The strategy-monitor will place
        # the stop-loss AFTER the buy order fills (checks state.stop_pending).
        target_price = round(price * 1.15, 2)
        stop_price = round(price * 0.90, 2)

        # 2. Save strategy state
        strategy_data = {
            "symbol": symbol,
            "strategy": "mean_reversion",
            "created": now_et().strftime("%Y-%m-%d"),
            "entry_price_estimate": price,
            "initial_qty": qty,
            "target_price": target_price,
            "stop_price": stop_price,
            "status": "awaiting_fill",
            "state": {
                "entry_order_id": buy_order_id,
                "sell_order_id": None,
                "stop_order_id": None,
                "stop_pending": True,
                "entry_fill_price": None,
            },
        }
        server.save_json(os.path.join(self._user_strategies_dir(), f"mean_reversion_{symbol}.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "mean_reversion",
            "symbol": symbol,
            "buy_order_id": buy_order_id,
            "target_price": target_price,
            "stop_price": stop_price,
            "price": price,
            "note": "Stop-loss and profit target managed by strategy-monitor after buy fills.",
        })
    def deploy_breakout(self, symbol, qty):
        """Deploy breakout: buy shares, set tight 5% stop-loss, trailing stop."""
        snap_url = f"{self.user_data_endpoint}/stocks/{symbol}/snapshot?feed=iex"
        snap = self.user_api_get(snap_url)
        if "error" in snap:
            snap_url = f"{self.user_data_endpoint}/stocks/{symbol}/snapshot"
            snap = self.user_api_get(snap_url)
        price = 0
        if isinstance(snap, dict):
            lt = snap.get("latestTrade", {})
            price = lt.get("p", 0)
        if not price:
            self.send_json({"error": f"Could not get price for {symbol}"}, 400)
            return

        # 1. Market buy
        buy_order = self.user_api_post(f"{self.user_api_endpoint}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })
        if isinstance(buy_order, dict) and "error" in buy_order:
            self.send_json({"error": f"Buy order failed: {buy_order['error']}"}, 400)
            return
        buy_order_id = buy_order.get("id", "")

        # NOTE: No sell orders placed here. The strategy-monitor will place
        # a trailing stop (trail_percent=5) AFTER the buy order fills
        # (checks state.stop_pending). This avoids double sell orders and
        # ensures the stop is placed at the correct filled price.
        stop_price = round(price * 0.95, 2)

        # 2. Save strategy state
        strategy_data = {
            "symbol": symbol,
            "strategy": "breakout",
            "created": now_et().strftime("%Y-%m-%d"),
            "entry_price_estimate": price,
            "initial_qty": qty,
            "stop_price": stop_price,
            "trail_pct": 5,
            "status": "awaiting_fill",
            "state": {
                "entry_order_id": buy_order_id,
                "stop_order_id": None,
                "trail_order_id": None,
                "stop_pending": True,
                "entry_fill_price": None,
            },
        }
        server.save_json(os.path.join(self._user_strategies_dir(), f"breakout_{symbol}.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "breakout",
            "symbol": symbol,
            "buy_order_id": buy_order_id,
            "stop_price": stop_price,
            "price": price,
            "note": "Trailing stop will be placed by strategy-monitor after buy fills.",
        })
    def _find_strategy_files(self, strategy_key):
        """Find strategy JSON files matching the given strategy key."""
        patterns = {
            "trailing_stop": "trailing_stop*.json",
            "copy_trading": "copy_trading.json",
            "wheel": "wheel_strategy.json",
            "mean_reversion": "mean_reversion_*.json",
            "breakout": "breakout_*.json",
        }
        # Reject unknown strategy keys to prevent glob injection via user input
        # (e.g. strategy="../*" matching files outside the strategies dir).
        if strategy_key not in patterns:
            return []
        pattern = patterns[strategy_key]
        return glob.glob(os.path.join(self._user_strategies_dir(), pattern))
    def handle_pause_strategy(self, body):
        """Pause a strategy by setting its status to 'paused'."""
        strategy = body.get("strategy", "")
        if not strategy:
            return self.send_json({"error": "Missing strategy"}, 400)

        files = self._find_strategy_files(strategy)
        if not files:
            return self.send_json({"error": f"No strategy files found for {strategy}"}, 404)

        paused = []
        # Round-7 fix: hold the strategy_file_lock across read-modify-write.
        # Without it, the scheduler's monitor tick (also reading + writing
        # these files every 60s) could overwrite the "paused" status we
        # just set, silently reactivating the strategy the user asked to
        # pause. Same lock file (path + ".lock") used by cloud_scheduler.
        import cloud_scheduler as _cs
        for fpath in files:
            with _cs.strategy_file_lock(fpath):
                data = server.load_json(fpath)
                if data:
                    data["status"] = "paused"
                    data["paused_at"] = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
                    server.save_json(fpath, data)
                    paused.append(os.path.basename(fpath))

        self.send_json({
            "success": True,
            "message": f"Paused {strategy}: {', '.join(paused)}",
            "files_updated": paused,
        })
    def handle_stop_strategy(self, body):
        """Stop a strategy: set status to 'stopped' and cancel related orders."""
        strategy = body.get("strategy", "")
        if not strategy:
            return self.send_json({"error": "Missing strategy"}, 400)

        files = self._find_strategy_files(strategy)
        if not files:
            return self.send_json({"error": f"No strategy files found for {strategy}"}, 404)

        stopped = []
        orders_cancelled = 0
        # Round-7: lock around the read-modify-write so the monitor tick
        # can't reactivate what we're stopping. Same pattern as pause above.
        import cloud_scheduler as _cs
        for fpath in files:
            with _cs.strategy_file_lock(fpath):
                data = server.load_json(fpath)
                if data:
                    # Cancel any open orders for this symbol
                    sym = data.get("symbol", "")
                    state = data.get("state", {})
                    order_ids = []
                    for key in ["stop_order_id", "trail_order_id", "sell_order_id", "entry_order_id"]:
                        oid = state.get(key)
                        if oid:
                            order_ids.append(oid)
                    # Cancel ladder orders too
                    for rule_key in ["rules"]:
                        rules = data.get(rule_key, {})
                        for ladder in rules.get("ladder_in", []):
                            oid = ladder.get("order_id")
                            if oid:
                                order_ids.append(oid)

                    for oid in order_ids:
                        result = self.user_api_delete(f"{self.user_api_endpoint}/orders/{oid}")
                        if not (isinstance(result, dict) and "error" in result):
                            orders_cancelled += 1

                    data["status"] = "stopped"
                    data["stopped_at"] = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
                    server.save_json(fpath, data)
                    stopped.append(os.path.basename(fpath))

        self.send_json({
            "success": True,
            "message": f"Stopped {strategy}: {', '.join(stopped)}. Cancelled {orders_cancelled} orders.",
            "files_updated": stopped,
            "orders_cancelled": orders_cancelled,
        })
    def handle_apply_preset(self, body):
        """Apply a strategy preset (conservative/moderate/aggressive).

        Validates all inputs to bounded ranges to prevent a malicious or
        buggy client from writing absurd values into guardrails.json (e.g.,
        max_positions=9999999) that could cause downstream misbehavior.
        """
        if not isinstance(body, dict):
            return self.send_json({"error": "Invalid request body"}, 400)
        preset_name = body.get("preset", "unknown")
        allowed_presets = {"conservative", "moderate", "aggressive", "custom"}
        if preset_name not in allowed_presets:
            return self.send_json({"error": f"Unknown preset: {preset_name}"}, 400)

        settings = body.get("settings", {})
        if not isinstance(settings, dict):
            return self.send_json({"error": "settings must be an object"}, 400)

        # Whitelist strategy names to prevent arbitrary strings being saved
        allowed_strategies = {
            "trailing_stop", "copy_trading", "wheel", "mean_reversion",
            "breakout", "short_sell",
        }
        raw_strats = settings.get("strategies", [])
        if not isinstance(raw_strats, list):
            raw_strats = []
        strategies = [s for s in raw_strats if isinstance(s, str) and s in allowed_strategies]

        # Bounded numeric validation
        def _num(key, default, lo, hi):
            v = settings.get(key, default)
            try:
                v = float(v)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        max_positions = int(_num("max_positions", 5, 1, 20))
        max_position_pct = _num("max_position_pct", 0.10, 0.01, 0.50)
        stop_loss_pct = _num("stop_loss_pct", 0.10, 0.01, 0.50)

        # Per-user guardrails and config — presets are a per-user setting
        guardrails_path = self._user_file("guardrails.json")
        guardrails = server.load_json(guardrails_path) or {}
        guardrails["max_positions"] = max_positions
        guardrails["max_position_pct"] = max_position_pct
        if strategies:
            guardrails["strategies_allowed"] = strategies
        server.save_json(guardrails_path, guardrails)

        config_path = self._user_file("auto_deployer_config.json")
        config = server.load_json(config_path) or {}
        config["risk_settings"] = config.get("risk_settings", {})
        config["risk_settings"]["default_stop_loss_pct"] = stop_loss_pct
        config["max_positions"] = max_positions
        server.save_json(config_path, config)

        self.send_json({"message": f"Preset applied: {preset_name}", "settings": {
            "max_positions": max_positions,
            "max_position_pct": max_position_pct,
            "stop_loss_pct": stop_loss_pct,
            "strategies": strategies,
        }})
    def handle_toggle_short_selling(self, body):
        """Toggle short selling ON/OFF in auto_deployer_config.json.

        Requires body to contain an explicit 'enabled' boolean — defaulting
        to True on missing body would silently enable shorts on any empty POST.
        """
        if not isinstance(body, dict) or "enabled" not in body:
            return self.send_json({"error": "Missing 'enabled' field in request body"}, 400)
        raw = body.get("enabled")
        if not isinstance(raw, bool):
            return self.send_json({"error": "'enabled' must be a boolean"}, 400)
        enabled = raw
        # Per-user short-selling toggle
        config_path = self._user_file("auto_deployer_config.json")
        config = server.load_json(config_path) or {}
        if "short_selling" not in config:
            config["short_selling"] = {
                "enabled": enabled,
                "only_in_bear_market": True,
                "max_short_positions": 1,
                "min_short_score": 15,
                "max_portfolio_pct_per_short": 0.05,
                "stop_loss_pct": 0.08,
                "profit_target_pct": 0.15,
                "require_spy_20d_below": -3,
                "skip_if_meme_warning": True,
            }
        else:
            config["short_selling"]["enabled"] = enabled
        config["short_selling"]["last_toggled"] = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
        server.save_json(config_path, config)
        msg = "Short selling ENABLED — will deploy in bear markets" if enabled else "Short selling DISABLED — no new shorts will deploy"
        self.send_json({"message": msg, "enabled": enabled})
