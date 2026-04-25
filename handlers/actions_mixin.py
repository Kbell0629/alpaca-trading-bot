"""
Operational action handlers (refresh, cancel order, close position, kill
switch, auto-deployer toggle, force deploy). Mixed into DashboardHandler
via MRO.
"""
import json
import logging
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

log = logging.getLogger(__name__)
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


# Round-22: per-user throttle for /api/force-auto-deploy. In-memory
# because the endpoint is already expensive enough (subprocess screener
# + deploy) that we don't need cross-instance coordination — a single
# Railway instance is enough to bottleneck. Key = user_id, value = last
# invocation timestamp. 30s cooldown enforced in handler.
_FORCE_DEPLOY_LAST: dict[int, float] = {}
_FORCE_DEPLOY_LOCK = threading.Lock()


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
                # Round-45: scope to the session mode so the screener
                # subprocess reads/writes under the right state tree.
                udir = _auth.user_data_dir(user_id,
                                            mode=self.session_mode or "paper")
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
                log.warning("user env setup failed", extra={"error": str(e)})
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
                log.warning("update_dashboard.py nonzero exit",
                            extra={"stderr": result.stderr[:500]})
        except Exception as e:
            log.error("update_dashboard.py launch failed", extra={"error": str(e)})

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
            log.warning("audit: kill_switch log failed", extra={"error": str(_e)})
        timestamp = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
        guardrails_path = self._user_file("guardrails.json")
        guardrails = server.load_json(guardrails_path) or {}

        if activate:
            # Round-12 audit: signal ALL in-flight deploy loops to abort
            # BEFORE we cancel orders. Otherwise a mid-loop deploy can
            # keep placing orders for a few hundred ms after cancel-all
            # returns — those survive into the "halted" state.
            try:
                from cloud_scheduler import request_deploy_abort
                request_deploy_abort()
            except Exception as _e:
                log.warning("kill-switch: deploy abort signal failed",
                            extra={"error": str(_e)})

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
                log.warning("kill-switch: wheel close error (non-fatal)",
                            extra={"error": str(e)})

            log.warning("kill-switch ACTIVATED",
                        extra={"timestamp": timestamp,
                               "orders_cancelled": orders_cancelled,
                               "positions_closed": positions_closed})

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

            # Re-arm the deploy-abort event so subsequent deploys aren't
            # instantly aborted by the stale signal.
            try:
                from cloud_scheduler import clear_deploy_abort
                clear_deploy_abort()
            except Exception as _e:
                log.warning("kill-switch deactivate: clear abort failed",
                            extra={"error": str(_e)})

            log.warning("kill-switch DEACTIVATED", extra={"timestamp": timestamp})

            self.send_json({
                "success": True,
                "activated": False,
                "timestamp": timestamp,
                "message": "Kill switch deactivated. Auto-deployer remains off - re-enable manually.",
            })

    def handle_factor_bypass(self, body):
        """Round-11 escape hatch. Toggles factor_bypass in guardrails.json,
        which run_auto_deployer checks before applying the breadth gate,
        quality filter, RS ranking, sector rotation, IV rank, and bullish
        news prioritization. When ON, deploys fall back to raw screener
        scores (old round-10 behaviour).

        When to use: if all factor filters together somehow block every
        pick and you need to force a deploy. Should be temporary — turn
        back off once you know why the filters were blocking.

        Audit-logged on every toggle so we have forensic attribution.
        """
        enable = bool(body.get("enable", False))
        try:
            ip = self.client_address[0] if self.client_address else None
            server.auth.log_admin_action(
                "factor_bypass_enable" if enable else "factor_bypass_disable",
                actor=self.current_user,
                target_user_id=self.current_user.get("id") if self.current_user else None,
                ip_address=ip,
            )
        except Exception as _e:
            log.warning("audit: factor_bypass log failed", extra={"error": str(_e)})
        guardrails_path = self._user_file("guardrails.json")
        guardrails = server.load_json(guardrails_path) or {}
        guardrails["factor_bypass"] = enable
        guardrails["factor_bypass_changed_at"] = now_et().isoformat()
        server.save_json(guardrails_path, guardrails)
        self.send_json({
            "ok": True,
            "factor_bypass": enable,
            "message": (
                "Factor filters BYPASSED — deploys now use raw screener scores only. "
                "Turn back OFF once you've verified normal flow."
            ) if enable else "Factor filters re-enabled (breadth, quality, RS, sector, IV rank).",
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
        # Round-22: per-user rate limit. A deploy cycle is expensive
        # (hits Alpaca + places orders) and the endpoint had no guard
        # against someone hammering it. 30-second cooldown per user is
        # plenty — the real use-case is "I clicked once, I'd like to
        # see it run", not "I want to run it 10 times in 10 seconds".
        uid = self.current_user.get("id")
        now_ts = time.time()
        with _FORCE_DEPLOY_LOCK:
            last = _FORCE_DEPLOY_LAST.get(uid, 0.0)
            if now_ts - last < 30:
                wait = int(30 - (now_ts - last))
                return self.send_json(
                    {"error": f"Rate limited. Try again in {wait}s."}, 429)
            _FORCE_DEPLOY_LAST[uid] = now_ts
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
            # Round-45: build user dict scoped to the session's current
            # trading mode (paper or live). Paper unchanged — live routes
            # through users/<id>/live/ with live-keyed credentials.
            user = self.build_scoped_user_dict()
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
            # Round-45: respects session mode (paper or live).
            user = self.build_scoped_user_dict()
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

    def handle_close_ghost_strategies(self, body=None):
        """Round-61 pt.24: mark ghost strategy files as closed. A
        ghost = active strategy file for a symbol that has no matching
        Alpaca position. The audit endpoint detects these; this
        handler fixes them.

        Called from the "🧹 Clean Up Ghosts" button in the audit modal.
        Read-only audit endpoint stays pure; this is the mutating
        counterpart the user can opt into.

        Round-61 pt.25: removed the 10-min grace-period filter. The
        scheduled `error_recovery.py` Check 3 keeps its 10-min grace
        for autonomous safety (protects against races where a
        position just closed and the file is mid-update), but the
        manual button path is a human asserting explicit intent —
        they clicked the button looking at a specific audit finding,
        no grace needed. Prior version kept skipping CORZ/AXTI
        because error_recovery's periodic mtime-touch kept pushing
        them inside the grace window. Pending-sell check remains —
        a position mid-exit shouldn't be force-closed even on user
        click.
        """
        if not self.current_user:
            return self.send_json({"error": "Not authenticated"}, 401)
        try:
            import audit_core
            import server
            from et_time import now_et
            from constants import is_closed_status
            user = self.build_scoped_user_dict()
            api_endpoint = user.get("_api_endpoint") or ""
            api_headers = {
                "APCA-API-KEY-ID": user.get("_api_key") or "",
                "APCA-API-SECRET-KEY": user.get("_api_secret") or "",
            }
            _, positions, orders, _errors = server._fetch_live_alpaca_state(
                api_endpoint, api_headers,
            )
            strats_dir = user.get("_strategies_dir") or ""
            strategy_files = audit_core.load_strategy_files(strats_dir)
            position_symbols = set()
            if isinstance(positions, list):
                for p in positions:
                    if isinstance(p, dict):
                        s = (p.get("symbol") or "").upper()
                        if s:
                            position_symbols.add(s)
                            # OCC underlyings also count.
                            if (p.get("asset_class") or "").lower() == "us_option":
                                u = audit_core._occ_underlying(s)
                                if u:
                                    position_symbols.add(u)

            pending_sells = set()
            if isinstance(orders, list):
                for o in orders:
                    if (o.get("side") or "").lower() == "sell":
                        pending_sells.add((o.get("symbol") or "").upper())

            closed = []
            skipped = []
            import os
            import json as _json
            for fname, data in strategy_files.items():
                if not isinstance(data, dict):
                    continue
                if is_closed_status(data.get("status")):
                    continue
                if str(data.get("status") or "").lower() == "migrated":
                    continue
                prefix, fsym = audit_core._parse_strategy_filename(fname)
                sym = str(data.get("symbol") or fsym or "").upper()
                if not sym:
                    continue
                if sym in position_symbols:
                    continue  # not a ghost
                # Pt.25: NO grace period for user-initiated cleanup.
                # User clicked the button — honor their intent.
                # Skip only if there are pending sell orders (position
                # may be mid-exit, premature close would confuse
                # record_trade_close).
                if sym in pending_sells:
                    skipped.append({"file": fname, "reason": "pending_sell"})
                    continue
                # Mark closed.
                data["status"] = "closed"
                data["closed_reason"] = ("No position found — marked closed "
                                          "via /api/close-ghost-strategies "
                                          "(user-triggered, pt.25 no grace)")
                data["closed_at"] = now_et().isoformat()
                fpath = os.path.join(strats_dir, fname)
                try:
                    with open(fpath, "w") as f:
                        _json.dump(data, f, indent=2)
                    closed.append(fname)
                except OSError as e:
                    skipped.append({"file": fname, "reason": f"write_error:{e}"})
            self.send_json({
                "success": True,
                "closed": closed,
                "skipped": skipped,
                "message": (f"Closed {len(closed)} ghost file(s), "
                            f"skipped {len(skipped)}."),
            })
        except Exception as e:
            self._send_error_safe(e, 500, "close-ghost-strategies")

    def handle_state_audit(self, body=None):
        """Round-61 pt.21: run the state-consistency audit for the
        current user. Cross-checks Alpaca positions, open orders, the
        strategy-file directory, the trade journal, and the scorecard.
        Returns a structured finding list grouped by severity so the
        dashboard can surface issues in plain English without the user
        having to grep files.

        Pure read-only — does not place orders, does not modify files.
        Safe to call on-demand from a button click.
        """
        if not self.current_user:
            return self.send_json({"error": "Not authenticated"}, 401)
        try:
            import audit_core
            import server
            user = self.build_scoped_user_dict()
            # Fetch live Alpaca state via the same cached helpers the
            # dashboard uses so results match what the user sees.
            api_endpoint = user.get("_api_endpoint") or ""
            api_headers = {
                "APCA-API-KEY-ID": user.get("_api_key") or "",
                "APCA-API-SECRET-KEY": user.get("_api_secret") or "",
            }
            account, positions, orders, _errors = server._fetch_live_alpaca_state(
                api_endpoint, api_headers,
            )
            strats_dir = user.get("_strategies_dir") or ""
            user_dir = user.get("_data_dir") or ""
            strategy_files = audit_core.load_strategy_files(strats_dir)
            journal = server.load_json(
                self._user_file("trade_journal.json")) or {}
            scorecard = server.load_json(
                self._user_file("scorecard.json")) or {}
            report = audit_core.run_audit(
                positions=positions if isinstance(positions, list) else [],
                orders=orders if isinstance(orders, list) else [],
                strategy_files=strategy_files,
                journal=journal,
                scorecard=scorecard,
            )
            self.send_json({
                "success": True,
                "report": report,
                "user_dir": user_dir,
                "strategies_dir": strats_dir,
                "file_count": len(strategy_files),
                "position_count": len(positions) if isinstance(positions, list) else 0,
                "order_count": len(orders) if isinstance(orders, list) else 0,
            })
        except Exception as e:
            self._send_error_safe(e, 500, "state-audit")

    def handle_trades_view(self, body=None):
        """Round-61 pt.36: filterable + sortable view of the user's
        trade journal. Powers the new `/api/trades` endpoint and the
        Trades dashboard tab. Read-only.

        Filters (optional, all keys may be absent or empty):
            status           — 'open' / 'closed' / 'all' (default 'all')
            strategy         — list of strategy names to include
            win_loss         — 'win' / 'loss' / 'flat' / 'all'
            symbol           — case-insensitive substring (matches
                               base symbol AND OCC underlying)
            exit_reason      — list of reason codes
            side             — 'long' / 'short' / 'all'
            date_from        — ISO timestamp lower bound on entry time
            date_to          — ISO timestamp upper bound on entry time
            min_pnl          — numeric lower bound on realized P&L
            max_pnl          — numeric upper bound

        Sort (optional):
            sort_by          — any trade field; default 'exit_timestamp'
            descending       — bool, default True

        Returns the full payload from
        ``trades_analysis_core.build_trades_view``: filtered + sorted
        + per-strategy summary + overall summary.
        """
        if not self.current_user:
            return self.send_json({"error": "Not authenticated"}, 401)
        try:
            import server
            import trades_analysis_core as tac
            body = body or {}
            filters = body.get("filters") or {}
            sort_by = body.get("sort_by") or "exit_timestamp"
            descending = bool(body.get("descending", True))

            journal_path = self._user_file("trade_journal.json")
            journal = server.load_json(journal_path) or {
                "trades": [], "daily_snapshots": [],
            }

            view = tac.build_trades_view(
                journal,
                filters=filters,
                sort_by=sort_by,
                descending=descending,
            )
            self.send_json({"success": True, **view})
        except Exception as e:
            self._send_error_safe(e, 500, "trades-view")

    def handle_backtest_run(self, body=None):
        """Round-61 pt.37: 30-day backtest harness.

        Body:
            symbols      — optional list. Defaults to the user's
                           journal universe (sorted unique symbols
                           pulled from trade_journal closed trades),
                           falling back to the dashboard top-picks
                           universe if the journal is empty.
            strategies   — optional list of strategy names. Defaults
                           to all backtestable strategies.
            days         — int, default 30. Window size in trading
                           days; cache lookback is `days * 1.5 + 5`.
            params       — optional {strategy: {stop_pct,
                           target_pct, max_hold_days, ...}} overrides.
            force_refresh — bypass the OHLCV cache (slow).

        Returns:
            success      — True on completion (False = error string)
            by_strategy  — {strategy: {trades, summary, params}}
            overall_summary — pooled-across-strategies aggregates
            symbols_evaluated — list of symbols actually fetched
            symbols_missing  — list of symbols where bars couldn't
                               be obtained (no cache + network down)
            window_days  — echoes the days param for the dashboard
                           label
        """
        if not self.current_user:
            return self.send_json({"error": "Not authenticated"}, 401)
        try:
            import server
            import backtest_core as bc
            import backtest_data as bd
            body = body or {}
            days = int(body.get("days") or 30)
            symbols = body.get("symbols")
            strategies = body.get("strategies")
            params = body.get("params") or {}
            force_refresh = bool(body.get("force_refresh"))

            user_id = self.current_user.get("id")
            try:
                import auth as _auth
                user_dir = _auth.user_data_dir(
                    user_id, mode=self.session_mode or "paper")
            except Exception:
                user_dir = ""
            # Universe selection: explicit > journal > dashboard picks
            if not symbols:
                journal = server.load_json(
                    self._user_file("trade_journal.json")) or {}
                symbols = bd.universe_from_journal(journal)
                if not symbols:
                    dash = server.load_json(
                        self._user_file("dashboard_data.json")) or {}
                    symbols = bd.universe_from_dashboard_data(dash)
            if not symbols:
                return self.send_json({
                    "success": False,
                    "error": ("No symbols available — empty trade "
                              "journal AND no recent dashboard "
                              "snapshot. Add symbols explicitly via "
                              "the symbols field."),
                })

            bars_by_symbol = bd.fetch_bars_for_symbols(
                user_dir, symbols, days=int(days * 1.6) + 10,
                force_refresh=force_refresh,
            )
            symbols_missing = [s for s, b in bars_by_symbol.items()
                                if not b]
            valid_bars = {s: b for s, b in bars_by_symbol.items() if b}

            result = bc.run_multi_strategy_backtest(
                valid_bars, strategies=strategies,
                params_by_strategy=params,
            )
            self.send_json({
                "success": True,
                "by_strategy": result["by_strategy"],
                "overall_summary": result["overall_summary"],
                "strategies_run": result["strategies_run"],
                "symbols_evaluated": list(valid_bars.keys()),
                "symbols_missing": symbols_missing,
                "window_days": days,
            })
        except Exception as e:
            self._send_error_safe(e, 500, "backtest-run")

    def handle_force_orphan_adoption(self, body=None):
        """Round-61 pt.15: run orphan adoption on-demand. Synthesizes
        strategy files for every Alpaca position that has no matching
        file, so `monitor_strategies` starts placing + maintaining
        stops. User-triggered via the 'Adopt MANUAL Positions' button
        in the dashboard; also scheduled every 10 min during market
        hours inside cloud_scheduler's periodic loop.

        Runs synchronously so the caller sees the result immediately
        and the dashboard can refresh with fresh AUTO labels.
        """
        if not self.current_user:
            return self.send_json({"error": "Not authenticated"}, 401)
        try:
            import cloud_scheduler as cs
            user = self.build_scoped_user_dict()
            result = cs.run_orphan_adoption(user)
            if isinstance(result, dict) and result.get("error"):
                return self.send_json({
                    "error": f"Adoption failed: {result['error']}",
                }, 500)
            created = (result or {}).get("created", 0) if isinstance(result, dict) else 0
            if created > 0:
                msg = (f"Adopted {created} MANUAL position(s) into AUTO "
                       f"management. Refresh the dashboard to see the new "
                       f"AUTO labels + stop orders.")
            else:
                msg = ("No MANUAL positions found that need adoption. All "
                       "positions are already under AUTO management (or "
                       "are managed by a wheel strategy that intentionally "
                       "handles its own stops).")
            self.send_json({
                "success": True,
                "message": msg,
                "adopted": created,
            })
        except Exception as e:
            self._send_error_safe(e, 500, "force-orphan-adoption")
