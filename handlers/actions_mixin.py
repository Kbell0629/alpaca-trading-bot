"""
Operational action handlers (refresh, cancel order, close position, kill
switch, auto-deployer toggle, force deploy). Mixed into DashboardHandler
via MRO.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import timedelta

from et_time import now_et
import re
import threading
import auth
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


class ActionsHandlerMixin:
    def handle_refresh(self):
        """Run update_dashboard.py with current user's credentials and return fresh data."""
        # Rate limit: each user's refresh spawns a 10-min-capable subprocess.
        # Without a lock, rapid clicks spawn N parallel screener runs and DoS
        # Alpaca + CPU. 30-second cooldown per user.
        user_id = self.current_user.get("id") if self.current_user else None
        if user_id is not None:
            now_ts = time.time()
            # Round-9 fix: atomic compare-and-set on _refresh_cooldowns.
            # Without the lock, two clicks arriving in the same
            # millisecond could both pass the check and both spawn a
            # screener subprocess.
            with server._refresh_cooldowns_lock:
                last = server._refresh_cooldowns.get(user_id, 0)
                if now_ts - last < 30:
                    wait = int(30 - (now_ts - last))
                    return self.send_json({
                        "error": f"Refresh cooling down — try again in {wait}s"
                    }, 429)
                server._refresh_cooldowns[user_id] = now_ts

        script_path = os.path.join(server.BASE_DIR, "update_dashboard.py")
        env = os.environ.copy()
        if user_id is not None:
            try:
                import auth as _auth
                udir = _auth.user_data_dir(user_id)
                env["ALPACA_API_KEY"] = self.user_api_key
                env["ALPACA_API_SECRET"] = self.user_api_secret
                env["ALPACA_ENDPOINT"] = self.user_api_endpoint
                env["ALPACA_DATA_ENDPOINT"] = self.user_data_endpoint
                # Round-11: was `server.DASHBOARD_DATA_PATH` (bogus
                # prefix); update_dashboard.py reads `DASHBOARD_DATA_PATH`
                # so the subprocess was writing to the shared file and
                # cross-user-leaking picks.
                env["DASHBOARD_DATA_PATH"] = os.path.join(udir, "dashboard_data.json")
                # Per-user strategies + journal + scorecard so Refresh
                # doesn't cross-contaminate state either.
                env["STRATEGIES_DIR"] = os.path.join(udir, "strategies")
                env["JOURNAL_PATH"] = os.path.join(udir, "trade_journal.json")
                env["SCORECARD_PATH"] = os.path.join(udir, "scorecard.json")
                env["CAPITAL_STATUS_PATH"] = os.path.join(udir, "capital_status.json")
                env["DASHBOARD_HTML_PATH"] = os.path.join(udir, "dashboard.html")
            except Exception as e:
                print(f"user env setup failed: {e}")
        try:
            result = subprocess.run(
                ["python3", script_path],
                cwd=server.BASE_DIR,
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )
            if result.returncode != 0:
                print(f"update_dashboard.py stderr: {result.stderr[:500]}")
        except Exception as e:
            print(f"Error running update_dashboard.py: {e}")

        # Return fresh data regardless
        data = server.get_dashboard_data(
            api_endpoint=self.user_api_endpoint,
            api_headers=self.user_headers(),
            user_id=user_id,
        )
        self.send_json(data)
    def handle_cancel_order(self, body):
        """Cancel an open order."""
        order_id = body.get("order_id", "")
        if not order_id:
            self.send_json({"error": "Missing order_id"}, 400)
            return
        # Validate order_id is a UUID to prevent path traversal
        if not re.match(r'^[0-9a-f\-]{36}$', order_id):
            return self.send_json({"error": "Invalid order_id format"}, 400)

        result = self.user_api_delete(f"{self.user_api_endpoint}/orders/{order_id}")
        if isinstance(result, dict) and "error" in result:
            self.send_json({"error": result["error"]}, 400)
        else:
            self.send_json({"success": True, "order_id": order_id})
    def handle_close_position(self, body):
        """Close a position."""
        symbol = body.get("symbol", "").upper()
        if not symbol:
            self.send_json({"error": "Missing symbol"}, 400)
            return
        # Validate symbol is alphanumeric (1-10 chars) to prevent path traversal
        if not re.match(r'^[A-Z]{1,10}$', symbol):
            return self.send_json({"error": "Invalid symbol format"}, 400)

        result = self.user_api_delete(f"{self.user_api_endpoint}/positions/{symbol}")
        if isinstance(result, dict) and "error" in result:
            self.send_json({"error": result["error"]}, 400)
        else:
            self.send_json({"success": True, "symbol": symbol, "order": result})
    def handle_sell(self, body):
        """Place a market sell order."""
        symbol = body.get("symbol", "").upper()
        try:
            qty = int(body.get("qty", 1))
        except (TypeError, ValueError):
            return self.send_json({"error": "Invalid quantity."}, 400)
        if not symbol:
            self.send_json({"error": "Missing symbol"}, 400)
            return
        if qty < 1 or qty > 10000:
            return self.send_json({"error": "Invalid quantity. Must be 1-10000."}, 400)

        result = self.user_api_post(f"{self.user_api_endpoint}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        })
        if isinstance(result, dict) and "error" in result:
            self.send_json({"error": result["error"]}, 400)
        else:
            self.send_json({"success": True, "symbol": symbol, "qty": qty, "order": result})
    def handle_auto_deployer(self, body):
        """Toggle the auto-deployer on/off by updating the current user's config file."""
        enabled = body.get("enabled", False)
        config_path = self._user_file("auto_deployer_config.json")
        config = server.load_json(config_path)
        if not config:
            config = {
                "enabled": False,
                "max_new_positions_per_day": 2,
                "max_portfolio_pct_per_stock": 0.10,
                "strategies": ["trailing_stop", "mean_reversion", "breakout"],
                "require_stop_loss": True,
            }
        config["enabled"] = bool(enabled)
        config["last_toggled"] = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
        server.save_json(config_path, config)
        self.send_json({"success": True, "enabled": config["enabled"]})
    def handle_kill_switch(self, body):
        """Activate or deactivate the kill switch FOR THE CURRENT USER ONLY.
        Each user has their own guardrails.json and auto_deployer_config.json —
        one user's kill switch must not halt another user's trading.
        Round-11: audit-log every kill-switch toggle so forensic review
        can attribute a halt to a specific user/session/IP.
        """
        activate = body.get("activate", False)
        try:
            ip = self.client_address[0] if self.client_address else None
            server.auth.log_admin_action(
                "kill_switch_activate" if activate else "kill_switch_deactivate",
                actor=self.current_user,
                target_user_id=self.current_user.get("id") if self.current_user else None,
                ip_address=ip,
            )
        except Exception as _e:
            print(f"[audit] kill_switch log failed: {_e}", flush=True)
        timestamp = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
        guardrails_path = self._user_file("guardrails.json")
        guardrails = server.load_json(guardrails_path) or {}

        if activate:
            # 1. Cancel ALL open orders in one atomic bulk call
            orders_before = self.user_api_get(f"{self.user_api_endpoint}/orders?status=open")
            orders_cancelled = len(orders_before) if isinstance(orders_before, list) else 0
            self.user_api_delete(f"{self.user_api_endpoint}/orders")

            # 2. Close ALL positions (Alpaca supports closing all with one call)
            positions_closed = 0
            positions_before = self.user_api_get(f"{self.user_api_endpoint}/positions")
            if isinstance(positions_before, list):
                positions_closed = len(positions_before)
            close_result = self.user_api_delete(f"{self.user_api_endpoint}/positions")

            # 3. Set kill_switch: true in guardrails.json
            guardrails["kill_switch"] = True
            guardrails["kill_switch_triggered_at"] = timestamp
            guardrails["kill_switch_reason"] = "Manual activation via dashboard"
            server.save_json(guardrails_path, guardrails)

            # 4. Set enabled: false in THIS USER's auto_deployer_config.json
            ad_config_path = self._user_file("auto_deployer_config.json")
            ad_config = server.load_json(ad_config_path) or {}
            ad_config["enabled"] = False
            ad_config["last_toggled"] = timestamp
            server.save_json(ad_config_path, ad_config)

            # 5. Close all open wheel options for this user — kill switch must
            # also flatten short option exposure (bulk /positions DELETE above
            # only closes equity positions, leaving short puts/calls open).
            try:
                import wheel_strategy as ws
                user_shim = {
                    "_api_key": self.user_api_key, "_api_secret": self.user_api_secret,
                    "_api_endpoint": self.user_api_endpoint, "_data_endpoint": self.user_data_endpoint,
                    "_data_dir": self._user_dir(), "_strategies_dir": self._user_strategies_dir(),
                }
                for fname, wstate in ws.list_wheel_files(user_shim):
                    ac = wstate.get("active_contract") or {}
                    if ac.get("contract_symbol") and ac.get("status") in ("active", "pending"):
                        # LIMIT buy-to-close at 2× current ask (safety ceiling
                        # so an illiquid OTM option with bid=0.05/ask=0.80
                        # doesn't fill at 10× the original premium). If the
                        # limit doesn't fill, the order sits until next
                        # monitor tick — but we're in kill-switch state so
                        # no further harm is done.
                        contract_sym = ac["contract_symbol"]
                        qty = ac.get("quantity", 1)
                        try:
                            quote = ws.get_option_quote(user_shim, contract_sym)
                            ask = (quote or {}).get("ask", 0) or ac.get("limit_price_used", 1.0)
                            # 2x ceiling, round to nearest nickel, minimum $0.05
                            limit_px = max(0.05, round((ask * 2) * 20) / 20)
                            order_payload = {
                                "symbol": contract_sym,
                                "qty": str(qty),
                                "side": "buy",
                                "type": "limit",
                                "limit_price": f"{limit_px:.2f}",
                                "time_in_force": "day",
                                "order_class": "simple",
                            }
                        except Exception:
                            # Worst case — fall back to market order but only
                            # as last resort.
                            order_payload = {
                                "symbol": contract_sym, "qty": str(qty),
                                "side": "buy", "type": "market",
                                "time_in_force": "day",
                            }
                        self.user_api_post(f"{self.user_api_endpoint}/orders", order_payload)
                        # Mark the wheel state as killed
                        wstate["stage"] = "killed_by_kill_switch"
                        wstate["active_contract"] = None
                        ws.save_wheel_state(user_shim, wstate)
            except Exception as e:
                print(f"[KILL SWITCH] Wheel close error (non-fatal): {e}")

            print(f"[KILL SWITCH] Activated at {timestamp}: {orders_cancelled} orders cancelled, {positions_closed} positions closed")

            # Send push notification via ntfy.sh (fire-and-forget, don't block HTTP response)
            subprocess.Popen([sys.executable, os.path.join(server.BASE_DIR, "notify.py"), "--type", "kill", f"Cancelled {orders_cancelled} orders, closed {positions_closed} positions. All trading halted."], cwd=server.BASE_DIR)

            self.send_json({
                "success": True,
                "activated": True,
                "orders_cancelled": orders_cancelled,
                "positions_closed": positions_closed,
                "timestamp": timestamp,
            })
        else:
            # Deactivate: set kill_switch: false, do NOT re-enable auto-deployer
            guardrails["kill_switch"] = False
            guardrails["kill_switch_triggered_at"] = None
            guardrails["kill_switch_reason"] = None
            server.save_json(guardrails_path, guardrails)

            print(f"[KILL SWITCH] Deactivated at {timestamp}")

            self.send_json({
                "success": True,
                "activated": False,
                "timestamp": timestamp,
                "message": "Kill switch deactivated. Auto-deployer remains off - re-enable manually.",
            })

    def handle_force_auto_deploy(self):
        """Admin: force the FULL morning deploy cycle to run NOW for the current user.
        Bypasses the once-per-day lock so you can see it execute on demand.
        Guardrails (kill switch, daily loss, capital check, correlation, etc) still apply.

        Runs three tasks in sequence (not parallel — they share state files):
          1. run_auto_deployer      (trailing stop / breakout / mean reversion picks)
          2. run_wheel_auto_deploy  (sells cash-secured puts on wheel candidates)
          3. run_wheel_monitor      (advances any existing wheel state machines)
        """
        if not self.current_user:
            return self.send_json({"error": "Not authenticated"}, 401)
        # Round-11: audit the force-deploy so we have a record if it
        # was triggered off-hours or outside the normal 9:35 window.
        try:
            ip = self.client_address[0] if self.client_address else None
            server.auth.log_admin_action(
                "force_auto_deploy",
                actor=self.current_user,
                target_user_id=self.current_user.get("id"),
                ip_address=ip,
            )
        except Exception:
            pass
        try:
            import cloud_scheduler as cs
            # Build the user dict in the format cloud_scheduler expects
            user = {
                "id": self.current_user["id"],
                "username": self.current_user["username"],
                "_api_key": self.user_api_key,
                "_api_secret": self.user_api_secret,
                "_api_endpoint": self.user_api_endpoint,
                "_data_endpoint": self.user_data_endpoint,
                "_ntfy_topic": self.current_user.get("ntfy_topic", "") or f"alpaca-bot-{self.current_user['username'].lower()}",
                "_data_dir": auth.user_data_dir(self.current_user["id"]),
                "_strategies_dir": os.path.join(auth.user_data_dir(self.current_user["id"]), "strategies"),
            }
            # DO NOT clear the daily lock. Previously we popped _last_runs
            # keys so force-deploy could re-run, but that caused the 9:35 AM
            # scheduler tick to ALSO fire (it sees no lock → runs again)
            # → two concurrent auto_deployers. Instead we rely on the
            # dedup inside run_wheel_auto_deploy (_wheel_deploy_in_flight)
            # and the once-per-tick idempotency checks inside run_auto_deployer
            # (existing_syms, correlation check).
            uid = user["id"]
            # Still clear the interval-based locks (non-daily) so screener
            # and monitor run fresh — those aren't susceptible to the race
            # because they're idempotent by design.
            for key in (f"wheel_monitor_{uid}", f"screener_{uid}"):
                cs._last_runs.pop(key, None)

            # Run in a background thread so the request returns quickly.
            # Guard against rapid double-clicks with an in-flight set.
            deploy_key = f"force_deploy_{uid}"
            with cs._wheel_deploy_lock:
                if deploy_key in cs._wheel_deploy_in_flight:
                    return self.send_json({
                        "error": "Force Deploy already running — wait for it to complete."
                    }, 429)
                cs._wheel_deploy_in_flight.add(deploy_key)
            def _run():
                try:
                    cs.log(f"[{user['username']}] FORCE DEPLOY: starting full cycle", "deployer")
                    cs.run_auto_deployer(user)
                    cs.log(f"[{user['username']}] FORCE DEPLOY: regular deployer done, starting wheel", "deployer")
                    cs.run_wheel_auto_deploy(user)
                    cs.log(f"[{user['username']}] FORCE DEPLOY: wheel deploy done, running monitor", "deployer")
                    cs.run_wheel_monitor(user)
                    cs.log(f"[{user['username']}] FORCE DEPLOY: all three tasks complete", "deployer")
                except Exception as e:
                    cs.log(f"Force-deploy error for {user['username']}: {e}", "deployer")
                finally:
                    with cs._wheel_deploy_lock:
                        cs._wheel_deploy_in_flight.discard(deploy_key)
            threading.Thread(target=_run, daemon=True, name=f"ForceDeploy-{user['id']}").start()
            self.send_json({
                "success": True,
                "message": (
                    f"Full deploy cycle triggered for {user['username']}:\n"
                    "  1. Regular auto-deployer (trailing/breakout/mean-rev)\n"
                    "  2. Wheel auto-deploy (sell cash-secured puts)\n"
                    "  3. Wheel monitor (advance any active cycles)\n"
                    "Watch the Scheduler tab for live results (takes ~60-90 seconds)."
                ),
            })
        except Exception as e:
            self.send_json({"error": f"Failed to start force-deploy: {e}"}, 500)

    def handle_force_daily_close(self):
        """Manually fire run_daily_close for the current user. Needed when
        a container restart caused the scheduled 4:05 PM task to skip
        (in-memory _last_runs loses state across restarts, so if the
        restart crosses the scheduled time, daily close may not fire in
        the first tick of the new container — persistence fix landed in
        the same commit addresses the root cause going forward).

        Writes scorecard.json + logs daily PnL notification exactly as
        the scheduled task would. Sets _last_runs so the scheduler
        won't double-fire it.
        """
        if not self.current_user:
            return self.send_json({"error": "Not authenticated"}, 401)
        try:
            import cloud_scheduler as cs
            user = {
                "id": self.current_user["id"],
                "username": self.current_user["username"],
                "_api_key": self.user_api_key,
                "_api_secret": self.user_api_secret,
                "_api_endpoint": self.user_api_endpoint,
                "_data_endpoint": self.user_data_endpoint,
                "_ntfy_topic": self.current_user.get("ntfy_topic", "") or f"alpaca-bot-{self.current_user['username'].lower()}",
                "_data_dir": auth.user_data_dir(self.current_user["id"]),
                "_strategies_dir": os.path.join(auth.user_data_dir(self.current_user["id"]), "strategies"),
            }
            # Run synchronously so the caller sees it completed and can
            # read fresh scorecard values. Daily close is fast (~1-3s).
            cs.run_daily_close(user)
            # Mark it done for today so the scheduler doesn't re-fire.
            # Round-9 fix: mutation must be inside cs._last_runs_lock to
            # avoid racing with the scheduler thread's snapshot in
            # _save_last_runs (and with should_run_daily_at's own
            # compare-and-set path).
            from et_time import now_et
            today_str = now_et().strftime("%Y-%m-%d")
            try:
                with cs._last_runs_lock:
                    cs._last_runs[f"daily_close_{user['id']}"] = today_str
            except AttributeError:
                # Pre-round-8 cloud_scheduler (no lock exposed). Fall
                # back to unsafe direct assignment.
                cs._last_runs[f"daily_close_{user['id']}"] = today_str
            try:
                if hasattr(cs, "_save_last_runs"):
                    cs._save_last_runs()
            except Exception:
                pass
            self.send_json({
                "success": True,
                "message": f"Daily close complete for {user['username']}. Scorecard updated.",
            })
        except Exception as e:
            self._send_error_safe(e, 500, "force-daily-close")
