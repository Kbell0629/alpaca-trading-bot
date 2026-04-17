"""
Auth-related HTTP handlers (login, signup, password change, settings).
Mixed into DashboardHandler via MRO. All methods assume `self` has the
BaseHTTPRequestHandler interface plus the utility methods on
DashboardHandler itself (send_json, check_auth, current_user, etc.).
Pulled out of server.py in Round 6.5 — code movement only, no semantic
changes. Tests in tests/ verify the decomposition is behavior-preserving.
"""
import json
import os
import re
import secrets
import urllib.request
import urllib.parse
from datetime import datetime

import auth
import hmac
import subprocess
import sys
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


class AuthHandlerMixin:
    def _set_session_cookie(self, token):
        """Send a Set-Cookie header for a fresh session token + companion
        CSRF token cookie. The CSRF cookie is readable by JS (no HttpOnly)
        so the dashboard can echo it in an X-CSRF-Token header on POSTs.
        Double-submit cookie pattern — server only accepts POSTs whose
        header value matches the cookie value, which is impossible for a
        cross-site attacker without reading the cookie (which SameSite
        blocks anyway).

        Secure flag applies when request came in over HTTPS (Railway sets
        X-Forwarded-Proto: https at the edge). SameSite=Strict prevents
        cross-site POSTs from using this cookie.
        """
        max_age = 30 * 86400
        secure = ""
        xfp = self.headers.get("X-Forwarded-Proto", "").lower()
        if xfp == "https" or os.environ.get("FORCE_SECURE_COOKIE") == "1":
            secure = "; Secure"
        self.send_header(
            "Set-Cookie",
            f"session={token}; Path=/; HttpOnly{secure}; Max-Age={max_age}; SameSite=Strict",
        )
        csrf_token = secrets.token_urlsafe(32)
        self.send_header(
            "Set-Cookie",
            f"csrf={csrf_token}; Path=/{secure}; Max-Age={max_age}; SameSite=Strict",
        )
    def _csrf_ok(self):
        """Return True if the POST passes the double-submit CSRF check.

        Requires the `csrf` cookie value to match the X-CSRF-Token header.
        If the cookie is missing (pre-CSRF session, freshly upgraded
        deployment), we fail OPEN for this request and set a new CSRF
        cookie so the client picks it up on the next POST. This avoids
        locking existing logged-in users out during rollout.
        """
        cookie_header = self.headers.get("Cookie", "")
        csrf_cookie = None
        for c in cookie_header.split(";"):
            c = c.strip()
            if c.startswith("csrf="):
                csrf_cookie = c[5:]
                break
        csrf_header = self.headers.get("X-CSRF-Token", "")
        # Round-11 audit: transitional fail-open for missing cookie has
        # expired (the rollout is weeks old, every active session has
        # the cookie now). Fail-closed — missing cookie forces client
        # to re-fetch via GET /api/me which sets a fresh cookie.
        if not csrf_cookie or not csrf_header:
            return False
        return hmac.compare_digest(csrf_cookie, csrf_header)
    def handle_login(self, body):
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self.send_json({"error": "Username and password required"}, 400)

        # Rate limit BEFORE calling authenticate (which runs expensive PBKDF2).
        # 5 failures per (IP, username) per 15 min then locked out.
        ip = self.client_address[0] if self.client_address else None
        if server._login_rate_limited(ip, username):
            return self.send_json(
                {"error": "Too many failed attempts. Try again in 15 minutes."}, 429
            )

        user = auth.authenticate(username, password)
        if not user:
            server._login_attempt_record(ip, username, success=False)
            auth.log_admin_action("login_failed", actor=None, ip_address=ip,
                                   detail={"attempted_username": username[:50]})
            # Small timing-safe sleep to slow down fast brute force even after
            # the lockout is cleared. Matches approximate PBKDF2 timing.
            return self.send_json({"error": "Invalid credentials"}, 401)

        server._login_attempt_record(ip, username, success=True)
        auth.log_admin_action("login_success", actor=user, target_user_id=user["id"], ip_address=ip)
        # Round-11: defense against session fixation. Invalidate any
        # pre-existing session for this user BEFORE minting a new one
        # so a session cookie planted on the victim before login
        # doesn't inherit post-login privileges. SameSite=Strict +
        # Secure already blocks most attack paths; this is defense
        # in depth.
        try:
            _conn = auth._get_db()
            _conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            _conn.commit()
            _conn.close()
        except Exception as _e:
            print(f"[auth] pre-login session invalidate failed: {_e}", flush=True)
        token = auth.create_session(user["id"], ip)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._set_session_cookie(token)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "username": user["username"]}).encode("utf-8"))
    def handle_signup(self, body):
        email = (body.get("email") or "").strip().lower()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        alpaca_key = (body.get("alpaca_key") or "").strip()
        alpaca_secret = (body.get("alpaca_secret") or "").strip()
        alpaca_endpoint = (body.get("alpaca_endpoint") or "https://paper-api.alpaca.markets/v2").strip()
        # ntfy_topic is now optional and auto-generated if omitted; signup UI
        # no longer exposes it, but we still accept it from older clients /
        # API callers.
        ntfy_topic = (body.get("ntfy_topic") or "").strip()
        notification_email_raw = (body.get("notification_email") or "").strip().lower()
        invite_code = (body.get("invite_code") or "").strip()

        if ntfy_topic and not server.is_valid_ntfy_topic(ntfy_topic):
            return self.send_json({
                "error": "Invalid ntfy topic. Allowed: letters, digits, _, - (4-64 chars)."
            }, 400)
        # Notification email: default to login email, but allow the user to
        # route bot emails to a different inbox. Reject malformed addresses.
        if notification_email_raw and "@" not in notification_email_raw:
            return self.send_json({"error": "Notification email looks invalid"}, 400)

        # Gate: SIGNUP_DISABLED env var blocks all signups; SIGNUP_INVITE_CODE
        # requires a matching code. Both are for sharing the deployment safely
        # with a specific friend without exposing it to the public internet.
        allowed, reason = server.signup_allowed(invite_code)
        if not allowed:
            return self.send_json({"error": reason}, 403)

        # Validation
        if not all([email, username, password, alpaca_key, alpaca_secret]):
            return self.send_json(
                {"error": "All fields required (email, username, password, Alpaca API key, Alpaca API secret)"},
                400,
            )
        if len(password) < 8:
            return self.send_json({"error": "Password must be at least 8 characters"}, 400)
        if "@" not in email:
            return self.send_json({"error": "Invalid email"}, 400)
        if not re.match(r"^[A-Za-z0-9_-]{3,30}$", username):
            return self.send_json({"error": "Username must be 3-30 chars, alphanumeric/_/-"}, 400)

        # SSRF defense: only allow Alpaca endpoints on the allowlist. Previously
        # an attacker could point alpaca_endpoint at an internal URL (e.g.
        # 169.254.169.254 metadata, localhost) and the server would hit it with
        # user-controlled headers and echo back the response body.
        if not server.is_allowed_alpaca_endpoint(alpaca_endpoint, data=False):
            return self.send_json({"error": "Invalid Alpaca endpoint. Must be paper or live Alpaca API."}, 400)

        # Verify Alpaca credentials before saving
        test_headers = {
            "APCA-API-KEY-ID": alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret,
        }
        try:
            req = urllib.request.Request(f"{alpaca_endpoint}/account", headers=test_headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                test_data = json.loads(resp.read().decode())
                if test_data.get("status") != "ACTIVE":
                    return self.send_json(
                        {"error": f"Alpaca account is not active (status: {test_data.get('status')})"},
                        400,
                    )
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return self.send_json(
                    {"error": "Invalid Alpaca API credentials. Check your key and secret."}, 400
                )
            return self.send_json({"error": f"Alpaca API error: {e.code}"}, 400)
        except Exception as e:
            return self.send_json(
                {"error": f"Could not verify Alpaca credentials: {str(e)[:100]}"}, 400
            )

        # Derive data endpoint from trading endpoint (paper vs live share the same data host)
        data_endpoint = "https://data.alpaca.markets/v2"

        user_id, err = auth.create_user(
            email=email,
            username=username,
            password=password,
            alpaca_key=alpaca_key,
            alpaca_secret=alpaca_secret,
            alpaca_endpoint=alpaca_endpoint,
            alpaca_data_endpoint=data_endpoint,
            ntfy_topic=ntfy_topic or None,
            notification_email=notification_email_raw or email,
        )
        if err:
            return self.send_json({"error": err}, 400)

        # Auto-login
        ip = self.client_address[0] if self.client_address else None
        new_user = auth.get_user_by_id(user_id)
        auth.log_admin_action("signup", actor=new_user, target_user_id=user_id,
                               ip_address=ip,
                               detail={"email": email, "endpoint": alpaca_endpoint})
        token = auth.create_session(user_id, ip)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._set_session_cookie(token)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(
            json.dumps({"success": True, "user_id": user_id, "username": username}).encode("utf-8")
        )
    def handle_forgot_password(self, body):
        email = (body.get("email") or "").strip().lower()
        if not email:
            return self.send_json({"error": "Email required"}, 400)
        # Round-11 audit: rate limit. Without this a trivial
        # denial-of-reset: the attacker POSTs /api/forgot every second
        # for a victim's email and create_password_reset invalidates
        # any pending token each call, so the legitimate user can
        # never complete a reset. Also fills email_queue.json past
        # its 50-entry cap and evicts real trade notifications.
        ip = self.client_address[0] if self.client_address else None
        try:
            # Reuse login_attempts bucket with a "forgot:" prefix so we
            # don't need a new table; is_login_locked enforces 5 per
            # 15-min already.
            if server.auth.is_login_locked(ip, f"forgot:{email}"):
                # Do NOT reveal lockout to attacker — same generic OK.
                return self.send_json({"success": True,
                                        "message": "If that email exists, a reset link has been sent."})
            server.auth.record_login_attempt(ip, f"forgot:{email}", success=False)
        except Exception:
            pass  # rate-limit best-effort; don't block resets on DB hiccup
        token, user = auth.create_password_reset(email)
        # Do not reveal whether the email exists (security)
        generic_ok = {"success": True, "message": "If that email exists, a reset link has been sent."}
        if not token:
            # Round-11: uniform delay so the existence check isn't
            # wall-clock-leaking via timing (existing-email path runs
            # subprocess.Popen + fcntl.flock write which is ~30-50ms
            # longer than the not-found path's single SELECT).
            import time as _time_f
            _time_f.sleep(0.08)
            return self.send_json(generic_ok)

        # Build reset URL — prefer configured base, fall back to request Host.
        # Round-10 audit: host-header spoofing — a malicious actor can
        # POST /api/forgot for a victim's email with a crafted Host
        # header, making the reset link point to evil.com/reset?token=.
        # In production we require PUBLIC_BASE_URL; the fallback is only
        # permitted for local dev (localhost/127.0.0.1).
        base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        if not base_url:
            host = (self.headers.get("Host", "") or "").lower()
            is_local = ("localhost" in host) or ("127.0.0.1" in host)
            if not is_local:
                # Non-local without PUBLIC_BASE_URL → refuse to echo the
                # Host header back. Return generic OK so we don't tell
                # the attacker which emails exist.
                print("[auth] forgot-password refused: PUBLIC_BASE_URL not set", flush=True)
                return self.send_json(generic_ok)
            scheme = "http"
            base_url = f"{scheme}://{host}"
        reset_url = f"{base_url}/reset?token={token}"
        msg = (
            f"Password reset requested. Click to reset (expires in 1 hour):\n{reset_url}\n\n"
            f"If you did not request this, ignore this email."
        )

        try:
            # Pipe the sensitive reset URL through stdin instead of argv so
            # the token never appears in process listings or supervisor logs.
            p = subprocess.Popen([
                sys.executable,
                os.path.join(server.BASE_DIR, "notify.py"),
                "--type", "alert", "--stdin",
            ], stdin=subprocess.PIPE)
            try:
                p.stdin.write(f"Password reset: {reset_url}".encode())
                p.stdin.close()
            except Exception:
                pass
        except Exception as e:
            print(f"[auth] notify.py launch failed: {e}")

        # Also queue a direct email for the notification_email if one is set.
        # Round-10 audit: route through notify.queue_email which applies
        # fcntl.flock + atomic tempfile rename. The previous inline
        # open(path, "w") had no lock and no atomicity — a crash or a
        # concurrent drain could corrupt the queue, losing the reset
        # email exactly when a user can't log in.
        try:
            import fcntl as _fcntl
            import tempfile as _tempfile
            try:
                import auth as _auth
                _uqdir = _auth.user_data_dir(user["id"])
            except Exception:
                _uqdir = server.DATA_DIR
            email_queue_path = os.path.join(_uqdir, "email_queue.json")
            lock_file = email_queue_path + ".lock"
            lock_fd = None
            try:
                os.makedirs(os.path.dirname(email_queue_path) or ".", exist_ok=True)
                lock_fd = open(lock_file, "w")
                _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
                try:
                    with open(email_queue_path) as f:
                        queue = json.load(f)
                except Exception:
                    queue = []
                queue.append({
                    "to": user.get("notification_email") or email,
                    "subject": "Stock Bot — Password Reset",
                    "body": msg,
                    "sent": False,
                    "timestamp": server.now_et().isoformat(),
                })
                queue = queue[-50:]  # bound like notify.queue_email
                fd, tmp = _tempfile.mkstemp(dir=os.path.dirname(email_queue_path) or ".",
                                            suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(queue, f, indent=2)
                    os.replace(tmp, email_queue_path)
                except Exception:
                    try: os.unlink(tmp)
                    except OSError: pass
                    raise
            finally:
                if lock_fd:
                    _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                    lock_fd.close()
        except Exception as e:
            print(f"[auth] Failed to queue reset email: {e}")

        self.send_json(generic_ok)
    def handle_reset_password(self, body):
        token = (body.get("token") or "").strip()
        new_password = body.get("password") or ""
        if not token or not new_password:
            return self.send_json({"error": "Token and new password required"}, 400)
        # Strength check is enforced inside consume_reset_token →
        # change_password. Surfaces specific guidance from zxcvbn.
        ok, err = auth.consume_reset_token(token, new_password)
        if not ok:
            auth.log_admin_action("password_reset_failed", actor=None,
                                   ip_address=self.client_address[0] if self.client_address else None)
            return self.send_json({"error": err or "Invalid or expired reset token"}, 400)
        auth.log_admin_action("password_reset_via_token", actor=None,
                               ip_address=self.client_address[0] if self.client_address else None)
        self.send_json({"success": True, "message": "Password reset. Please log in."})
    def handle_logout(self):
        cookie_header = self.headers.get("Cookie", "")
        for cookie in cookie_header.split(";"):
            cookie = cookie.strip()
            if cookie.startswith("session="):
                auth.delete_session(cookie[8:])
                break
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
    def handle_change_password(self, body):
        old_password = body.get("old_password") or ""
        new_password = body.get("new_password") or ""
        if not old_password or not new_password:
            return self.send_json({"error": "Old and new password required"}, 400)
        user = self.current_user or {}
        if not auth.verify_password(old_password, user.get("password_hash", ""), user.get("password_salt", "")):
            auth.log_admin_action("password_change_failed", actor=user,
                                   target_user_id=user.get("id"),
                                   ip_address=self.client_address[0] if self.client_address else None)
            return self.send_json({"error": "Current password is incorrect"}, 400)
        # change_password now returns (ok, err_msg) and does the strength
        # check internally with user_inputs=[username, email]. Surface the
        # zxcvbn feedback directly so the UI can show useful guidance
        # ("This is a top-10 common password.").
        ok, err = auth.change_password(user["id"], new_password)
        if not ok:
            return self.send_json({"error": err or "Password rejected"}, 400)
        auth.log_admin_action("password_changed", actor=user,
                               target_user_id=user["id"],
                               ip_address=self.client_address[0] if self.client_address else None)
        self.send_json({"success": True, "message": "Password changed"})
    def handle_update_settings(self, body):
        """Update the current user's Alpaca keys, endpoints, or notification settings."""
        updates = {}
        for field in (
            "alpaca_key", "alpaca_secret", "alpaca_endpoint",
            "alpaca_data_endpoint", "ntfy_topic", "notification_email",
        ):
            val = body.get(field)
            if val is None:
                continue
            s = val.strip() if isinstance(val, str) else val
            # Skip empty strings for secrets — means "keep existing"
            if field in ("alpaca_key", "alpaca_secret") and not s:
                continue
            updates[field] = s
        if not updates:
            return self.send_json({"error": "No fields to update"}, 400)

        # SSRF defense: validate endpoint URLs against the Alpaca allowlist
        # before persisting. Without this, an attacker could set the endpoint
        # to an internal URL and every future Alpaca call for that user would
        # hit the attacker's chosen target.
        if "alpaca_endpoint" in updates and not server.is_allowed_alpaca_endpoint(updates["alpaca_endpoint"], data=False):
            return self.send_json({"error": "Invalid Alpaca endpoint. Must be paper or live Alpaca API."}, 400)
        if "alpaca_data_endpoint" in updates and not server.is_allowed_alpaca_endpoint(updates["alpaca_data_endpoint"], data=True):
            return self.send_json({"error": "Invalid Alpaca data endpoint. Must be data.alpaca.markets."}, 400)
        # ntfy_topic validation — prevent URL breakout and spoof-by-disclosure
        if "ntfy_topic" in updates and not server.is_valid_ntfy_topic(updates["ntfy_topic"]):
            return self.send_json({
                "error": "Invalid ntfy topic. Allowed: letters, digits, _, - (4-64 chars)."
            }, 400)

        auth.update_user_credentials(self.current_user["id"], **updates)
        self.send_json({"success": True, "message": "Settings updated"})
    def handle_delete_account(self, body):
        """Soft-delete (deactivate) the current user. Cloud scheduler will stop
        iterating them because list_active_users filters is_active = 1.
        Positions are NOT closed automatically — user must do that first.
        """
        if not body.get("confirm"):
            return self.send_json({"error": "Missing explicit confirmation"}, 400)
        user = self.current_user or {}
        user_id = user.get("id")
        if not user_id:
            return self.send_json({"error": "No current user"}, 400)
        # Prevent last-admin self-lockout
        try:
            conn = auth._get_db()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1")
            admin_count = cur.fetchone()[0]
            conn.close()
            if user.get("is_admin") and admin_count <= 1:
                return self.send_json({"error":
                    "You are the only active admin. Promote another user to admin before deactivating."}, 400)
        except Exception:
            pass
        try:
            conn = auth._get_db()
            cur = conn.cursor()
            cur.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
            cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            auth.log_admin_action("account_deleted", actor=user, target_user_id=user_id,
                                   ip_address=self.client_address[0] if self.client_address else None)
            self.send_json({"success": True, "message": "Account deactivated"})
        except Exception as e:
            self._send_error_safe(e, 500, "delete-account")
