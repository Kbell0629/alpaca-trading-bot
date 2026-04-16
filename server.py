#!/usr/bin/env python3
"""
Alpaca Trading Bot — Interactive Web Dashboard Server
Serves a fully interactive dashboard at http://localhost:8888
with API endpoints for deploying strategies, managing orders/positions, and more.

NOTE: HTTPS termination is handled by Railway's edge proxy. All traffic between
the client and Railway is encrypted via TLS. The app itself listens on plain HTTP.
"""

import base64
import glob
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta

# ET is the canonical timezone for this app. All timestamps emitted to logs,
# stored in DB, or rendered in the UI use ET. UTC never appears in user-
# facing strings or in stored ISO strings written from this process.
try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone.utc  # safety fallback

def now_et():
    return datetime.now(ET_TZ)

try:
    from http.server import ThreadingHTTPServer  # Python 3.7+
except ImportError:
    import socketserver
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        pass

# Safety net: force every socket.create_connection() in this process to
# observe a hard timeout. Every urllib call in this codebase passes an
# explicit timeout, but this catches anything we missed (third-party libs
# getting added later, debug scripts, etc.) from hanging forever and
# wedging the scheduler thread. 30s is generous — individual call sites
# use 10s which will always trip first.
import socket as _socket
_socket.setdefaulttimeout(30)

try:
    from cloud_scheduler import start_scheduler, get_scheduler_status
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

# Load .env file for local development
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

# Multi-user auth module (SQLite-backed sessions, per-user encrypted creds)
import auth  # noqa: E402
auth.init_db()
auth.bootstrap_from_env()

# Diagnostic: report the distribution of credential cipher formats.
# Transparent upgrade happens on successful login (PLAIN → ENC → ENCv2 →
# ENCv3), so the older buckets should trend to zero as users log in.
# Useful signal for deciding when an older decrypt path can be removed.
try:
    _fmt_counts = auth.count_legacy_encrypted_rows()
    if isinstance(_fmt_counts, dict) and "error" not in _fmt_counts:
        print(f"[auth] credential cipher distribution: {_fmt_counts}", flush=True)
        _need_upgrade = (_fmt_counts.get("PLAIN", 0)
                         + _fmt_counts.get("ENC", 0)
                         + _fmt_counts.get("ENCv2", 0))
        if _need_upgrade == 0:
            print("[auth] all users on ENCv3 (HKDF). Older decrypt paths are safe to retire.",
                  flush=True)
        else:
            print(f"[auth] {_need_upgrade} user rows will be upgraded to ENCv3 on next login",
                  flush=True)
except Exception:
    pass

# Basic auth credentials (set via env vars on Railway, or .env for local dev)
# Kept only for backward-compat bootstrap; actual auth now goes through auth.py
AUTH_USER = os.environ.get("DASHBOARD_USER", "")
AUTH_PASS = os.environ.get("DASHBOARD_PASS", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent runtime data lives. On Railway, set to a volume
# mount path (e.g. /data). Locally defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

# Templates directory — dashboard/login/signup/forgot/reset HTML live here
# as standalone files that can be edited with any HTML editor, linted, and
# diffed properly. Previously these were 4000+ line Python string literals
# embedded in this file. Loaded once at import time; no per-request I/O.
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

def _load_template(name):
    """Read an HTML template from the templates/ directory at import time.

    Kept dead simple — no Jinja2, no env-var interpolation. If the bot ever
    needs server-side templating it can swap this for jinja2; the HTML
    files will not need to change because any `{{ }}` tokens in them today
    are JavaScript / CSS content, not template markers.
    """
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
STRATEGIES_DIR = os.path.join(DATA_DIR, "strategies")
DASHBOARD_DATA_PATH = os.path.join(DATA_DIR, "dashboard_data.json")

API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "")
DATA_ENDPOINT = os.environ.get("ALPACA_DATA_ENDPOINT", "")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}


def alpaca_request(method, url, body=None, timeout=15):
    """Make an authenticated request to Alpaca API."""
    headers = dict(HEADERS)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}: {err_body}"}
    except Exception as e:
        return {"error": str(e)}


def alpaca_get(url, timeout=15):
    return alpaca_request("GET", url, timeout=timeout)


def alpaca_post(url, body=None, timeout=15):
    return alpaca_request("POST", url, body=body, timeout=timeout)


def alpaca_delete(url, timeout=15):
    return alpaca_request("DELETE", url, timeout=timeout)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, data):
    """Atomic JSON write: write to temp file then rename to avoid corruption."""
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# Fix #14: Server-side API response caching
_api_cache = {}
_cache_ttl = 10  # seconds
_API_CACHE_MAX = 500  # sweep when exceeded to prevent unbounded growth

def alpaca_get_cached(url, timeout=15, headers=None):
    """Cached version of alpaca_get for dashboard data.

    If headers is provided, the cache is keyed by (url, key-id) so different
    users don't share each other's data. When headers is None, uses the
    module-level env-var headers (backward compat).
    """
    now = time.time()
    cache_key = (url, (headers or {}).get("APCA-API-KEY-ID", ""))
    # Periodic sweep of expired entries — was unbounded; long uptime with
    # many distinct URL variants (query strings) grew the dict forever.
    if len(_api_cache) > _API_CACHE_MAX:
        for k in list(_api_cache.keys()):
            if now - _api_cache[k]["time"] > _cache_ttl:
                _api_cache.pop(k, None)
    if cache_key in _api_cache and now - _api_cache[cache_key]["time"] < _cache_ttl:
        return _api_cache[cache_key]["data"]
    if headers:
        req_headers = dict(headers)
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                data = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            data = {"error": f"HTTP {e.code}: {err_body}"}
        except Exception as e:
            data = {"error": str(e)}
    else:
        data = alpaca_get(url, timeout=timeout)
    _api_cache[cache_key] = {"data": data, "time": now}
    return data


# ============================================================================
# Dashboard data assembly — decomposed into clear phases so each step can be
# reasoned about (and tested) independently.
# ============================================================================

def _resolve_user_paths(user_id):
    """Return (user_dir, strats_dir). Falls back to shared DATA_DIR only
    when user_id is None (legacy env-var mode)."""
    if user_id is not None:
        try:
            import auth as _auth
            user_dir = _auth.user_data_dir(user_id)
            strats_dir = os.path.join(user_dir, "strategies")
            os.makedirs(strats_dir, exist_ok=True)
            return user_dir, strats_dir
        except Exception:
            pass
    return DATA_DIR, STRATEGIES_DIR


def _fetch_live_alpaca_state(api_endpoint, api_headers):
    """Pull account / positions / open orders from Alpaca with per-call
    caching. Returns (account, positions, orders, errors_list) where the
    errors list surfaces upstream failures so the UI can display them
    (instead of silently showing stale data).
    """
    ep = api_endpoint or API_ENDPOINT
    errors = []

    account = alpaca_get_cached(f"{ep}/account", headers=api_headers)
    positions = alpaca_get_cached(f"{ep}/positions", headers=api_headers)
    orders = alpaca_get_cached(f"{ep}/orders?status=open&limit=50", headers=api_headers)

    if isinstance(account, dict) and "error" in account:
        errors.append("account: " + account["error"])
    if isinstance(positions, dict) and "error" in positions:
        errors.append("positions: " + str(positions.get("error", "")))
    if isinstance(orders, dict) and "error" in orders:
        errors.append("orders: " + str(orders.get("error", "")))

    return account, positions, orders, errors


def _load_with_shared_fallback(user_path, shared_path, user_id):
    """Load a JSON overlay file from the per-user path.

    CRITICAL MIGRATION RULE: only user_id==1 (bootstrap admin) may fall
    back to the shared DATA_DIR copy. OTHER users must never inherit
    shared files, or they'd get Kevin's active strategies / guardrails /
    auto-deployer config and start auto-trading on their own Alpaca
    account the instant they sign up. This was a round-3 audit find
    and any regression here is a real financial-harm bug.
    """
    val = load_json(user_path)
    if val:
        return val
    if user_id == 1:
        val = load_json(shared_path)
        if val:
            try:
                import shutil
                os.makedirs(os.path.dirname(user_path) or ".", exist_ok=True)
                shutil.copy2(shared_path, user_path)
            except Exception:
                pass
        return val
    return None


def _load_overlay_files(user_dir, strats_dir, user_id):
    """Load per-user strategy + config files. Returns a dict ready to be
    merged into the response. Uses the share-fallback rule above."""
    return {
        "trailing": _load_with_shared_fallback(
            os.path.join(strats_dir, "trailing_stop.json"),
            os.path.join(STRATEGIES_DIR, "trailing_stop.json"),
            user_id,
        ),
        "copy_trading": _load_with_shared_fallback(
            os.path.join(strats_dir, "copy_trading.json"),
            os.path.join(STRATEGIES_DIR, "copy_trading.json"),
            user_id,
        ),
        "wheel": _load_with_shared_fallback(
            os.path.join(strats_dir, "wheel_strategy.json"),
            os.path.join(STRATEGIES_DIR, "wheel_strategy.json"),
            user_id,
        ),
        "scorecard": _load_with_shared_fallback(
            os.path.join(user_dir, "scorecard.json"),
            os.path.join(DATA_DIR, "scorecard.json"),
            user_id,
        ) or {},
        "auto_deployer_config": _load_with_shared_fallback(
            os.path.join(user_dir, "auto_deployer_config.json"),
            os.path.join(DATA_DIR, "auto_deployer_config.json"),
            user_id,
        ) or {},
        "guardrails": _load_with_shared_fallback(
            os.path.join(user_dir, "guardrails.json"),
            os.path.join(DATA_DIR, "guardrails.json"),
            user_id,
        ) or {},
    }


def get_dashboard_data(api_endpoint=None, api_headers=None, user_id=None):
    """Assemble the dashboard payload for a user.

    Pipeline:
      1. Resolve user paths (enforces multi-user isolation)
      2. Load screener picks from dashboard_data.json (per-user, never shared
         to non-admin users)
      3. Fetch live Alpaca state (account/positions/orders)
      4. Layer in strategy + guardrail overlays

    Falls back to a build-from-scratch response if no dashboard_data.json
    exists yet (new user before first screener run).
    """
    user_dir, strats_dir = _resolve_user_paths(user_id)

    # Load screener picks. CRITICAL: do NOT fall back to the shared
    # DASHBOARD_DATA_PATH for non-admin users — that would leak another
    # user's screener output.
    data = None
    if user_id is not None:
        data = load_json(os.path.join(user_dir, "dashboard_data.json"))
        if not data and user_id == 1:
            data = load_json(DASHBOARD_DATA_PATH)
    else:
        data = load_json(DASHBOARD_DATA_PATH)  # env-mode legacy

    account, positions, orders, api_errors = _fetch_live_alpaca_state(
        api_endpoint, api_headers,
    )

    if data:
        data["account"] = account if isinstance(account, dict) and "error" not in account else data.get("account", {})
        data["positions"] = positions if isinstance(positions, list) else data.get("positions", [])
        data["open_orders"] = orders if isinstance(orders, list) else data.get("open_orders", [])
        data["updated_at"] = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
        data["api_errors"] = api_errors
        overlays = _load_overlay_files(user_dir, strats_dir, user_id)
        for key, value in overlays.items():
            data[key] = value if value is not None else data.get(key, {})
        return data

    # No screener run yet — minimal fallback payload so the dashboard
    # renders "no picks yet" instead of erroring.
    overlays = _load_overlay_files(user_dir, strats_dir, user_id)
    return {
        "account": account if isinstance(account, dict) and "error" not in account else {},
        "positions": positions if isinstance(positions, list) else [],
        "open_orders": orders if isinstance(orders, list) else [],
        "trailing": overlays["trailing"],
        "copy_trading": overlays["copy_trading"],
        "wheel": overlays["wheel"],
        "picks": [],
        "total_screened": 0,
        "total_passed": 0,
        "updated_at": now_et().strftime("%Y-%m-%d %I:%M:%S %p ET"),
        "api_errors": api_errors,
    }


DASHBOARD_HTML = _load_template("dashboard.html")


# ============================================================================
# Auth page HTML (login, signup, forgot password, reset password)
# ============================================================================

LOGIN_HTML = _load_template("login.html")

SIGNUP_HTML = _load_template("signup.html")

FORGOT_HTML = _load_template("forgot.html")

RESET_HTML = _load_template("reset.html")


# ============================================================================
# Security hardening: login rate limiting + Alpaca endpoint allowlist
# ============================================================================

# Whitelist of Alpaca endpoints users may configure. Prevents SSRF via the
# signup/settings flow where `alpaca_endpoint` was previously an arbitrary URL
# that the server would hit with user-controlled headers.
ALLOWED_ALPACA_ENDPOINTS = {
    "https://paper-api.alpaca.markets/v2",
    "https://api.alpaca.markets/v2",
}
ALLOWED_ALPACA_DATA_ENDPOINTS = {
    "https://data.alpaca.markets/v2",
    "https://data.alpaca.markets/v1beta1",
}

def is_valid_ntfy_topic(topic):
    """ntfy.sh is unauthenticated pub/sub — anyone who knows a topic name
    can publish to it. Previously topic was accepted with no validation,
    which allowed:
      (a) URL breakout via `?`/`#`/`/` in the topic
      (b) Admin-panel disclosure -> another user learns topic -> spoof
          push notifications to victim's phone
    Restrict to alphanumeric + underscore + hyphen, 4-64 chars. Empty is
    allowed (means "use auto-generated default").
    """
    if not topic:
        return True
    return bool(re.match(r'^[A-Za-z0-9_-]{4,64}$', topic))


def is_allowed_alpaca_endpoint(url, data=False):
    """Return True if the URL is on the Alpaca allowlist."""
    if not isinstance(url, str):
        return False
    url = url.strip().rstrip("/")
    pool = ALLOWED_ALPACA_DATA_ENDPOINTS if data else ALLOWED_ALPACA_ENDPOINTS
    return url in pool

# Login rate limit is now BACKED BY SQLite (auth.login_attempts table)
# so rate-limit state survives Railway redeploys. Previously attacker
# could force a restart to reset the counter.
# Thin wrapper functions below delegate to auth.py.

# Per-user cooldown for /api/refresh. Spawns a 10-min subprocess if abused.
_refresh_cooldowns = {}  # user_id -> last_refresh_ts

def _login_rate_limited(ip, username):
    """Persistent rate limit check (delegates to auth.is_login_locked)."""
    return auth.is_login_locked(ip, username)

def _login_attempt_record(ip, username, success):
    """Persistent attempt record (delegates to auth.record_login_attempt).

    Opportunistically GCs four tables from the same hook so none of them
    require a separate scheduler task:
      - login_attempts (24hr)
      - sessions (expired rows — prior audits defined cleanup but never wired
        it in, so the table grew unbounded)
      - admin_audit_log (90d)
      - password_resets (expired/used)
    Ratio ~1.2%, safe to miss occasional ticks; the probability was deliberately
    kept the same as before.
    """
    auth.record_login_attempt(ip, username, success)
    if secrets.token_bytes(1)[0] < 3:  # ~1.2% chance
        for _fn in ("gc_login_attempts", "cleanup_expired_sessions",
                    "gc_audit_log", "gc_password_resets"):
            try:
                _gc = getattr(auth, _fn, None)
                if _gc:
                    _gc()
            except Exception as _e:
                print(f"[auth] GC {_fn} failed: {_e}", flush=True)

# Signup invite code gate. Set SIGNUP_INVITE_CODE env var on Railway to require
# the code for new signups. Set SIGNUP_DISABLED=1 to block all new signups.
def signup_allowed(provided_code):
    if os.environ.get("SIGNUP_DISABLED") == "1":
        return False, "Signup is disabled on this deployment."
    expected = os.environ.get("SIGNUP_INVITE_CODE", "").strip()
    if expected:
        if not hmac.compare_digest((provided_code or "").strip(), expected):
            return False, "Invalid invite code."
    return True, None


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard server."""

    def log_message(self, format, *args):
        """Override to add timestamp prefix."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")

    # Instance defaults populated by check_auth()
    current_user = None
    user_api_key = ""
    user_api_secret = ""
    user_api_endpoint = ""
    user_data_endpoint = ""

    def get_current_user(self):
        """Return current logged-in user dict, or None.

        Checks session cookie only. Basic Auth is OFF by default — previously
        it was an un-rate-limited brute-force surface. To re-enable for CI or
        API clients, set env var ENABLE_BASIC_AUTH=1 (rate-limited via
        _LOGIN_ATTEMPTS below).
        """
        cookie_header = self.headers.get("Cookie", "")
        session_token = None
        for cookie in cookie_header.split(";"):
            cookie = cookie.strip()
            if cookie.startswith("session="):
                session_token = cookie[8:]
                break
        if session_token:
            user = auth.validate_session(session_token)
            if user:
                return user
        # Optional Basic Auth (disabled by default)
        if os.environ.get("ENABLE_BASIC_AUTH") == "1":
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                    username, password = decoded.split(":", 1)
                    # Rate-limit: use the same tracker as /api/login
                    if _login_rate_limited(self.client_address[0], username):
                        return None
                    user = auth.authenticate(username, password)
                    if user:
                        _login_attempt_record(self.client_address[0], username, success=True)
                        return user
                    _login_attempt_record(self.client_address[0], username, success=False)
                except Exception:
                    pass
        return None

    def check_auth(self):
        """Check auth and set self.current_user. Returns True if authenticated.

        On failure: redirects HTML requests to /login, returns 401 JSON for /api/* requests.
        """
        user = self.get_current_user()
        if user:
            self.current_user = user
            # Load per-user Alpaca credentials (decrypted) for this request
            creds = auth.get_user_alpaca_creds(user["id"])
            self.user_api_key = creds["key"] if creds else ""
            self.user_api_secret = creds["secret"] if creds else ""
            self.user_api_endpoint = (creds.get("endpoint") if creds else None) or API_ENDPOINT
            self.user_data_endpoint = (creds.get("data_endpoint") if creds else None) or DATA_ENDPOINT
            return True
        # Not authenticated — decide response by path
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b'{"error": "Not authenticated"}')
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        return False

    def user_headers(self):
        """Return Alpaca auth headers for the current user (falls back to env vars)."""
        return {
            "APCA-API-KEY-ID": self.user_api_key or API_KEY,
            "APCA-API-SECRET-KEY": self.user_api_secret or API_SECRET,
        }

    # ========================================================================
    # Per-user file isolation helpers
    #
    # Before multi-user: every strategy file, guardrails.json, auto_deployer
    # config, trade_journal, etc. lived in the shared DATA_DIR / STRATEGIES_DIR.
    # With a second user, that meant user B's deploys overwrite user A's
    # strategy files and user A's kill switch halts user B's trading.
    # These helpers route every per-user file access through the user's own
    # data dir inside DATA_DIR/users/{user_id}/, with a fallback to DATA_DIR
    # for the env-var legacy mode where there's no SQLite user.
    # ========================================================================
    def _user_dir(self):
        """Return the per-user data directory, or DATA_DIR for legacy env-mode."""
        if self.current_user and self.current_user.get("id") is not None:
            try:
                return auth.user_data_dir(self.current_user["id"])
            except Exception:
                pass
        return DATA_DIR

    def _user_strategies_dir(self):
        """Return the per-user strategies directory (creates it if needed).

        CRITICAL: Strategy file seed from shared STRATEGIES_DIR is RESTRICTED
        to the bootstrap admin (user_id=1). Previously this copied Kevin's
        active strategy files (trailing_stop_SOXL.json, wheel_strategy.json)
        into every new user's dir, causing the monitor to attempt trades on
        symbols that don't exist in their Alpaca account.
        """
        d = os.path.join(self._user_dir(), "strategies")
        first_time = not os.path.isdir(d)
        os.makedirs(d, exist_ok=True)
        if (first_time
                and self.current_user
                and self.current_user.get("id") == 1):
            try:
                if os.path.isdir(STRATEGIES_DIR) and STRATEGIES_DIR != d:
                    import shutil
                    for f in os.listdir(STRATEGIES_DIR):
                        if f.endswith(".json"):
                            src = os.path.join(STRATEGIES_DIR, f)
                            dst = os.path.join(d, f)
                            if os.path.isfile(src) and not os.path.exists(dst):
                                shutil.copy2(src, dst)
                    print(f"[migration] Seeded strategies dir for bootstrap admin", flush=True)
            except Exception as e:
                print(f"[migration] WARN strategies seed failed: {e}", flush=True)
        return d

    def _user_file(self, filename):
        """Return an absolute path to a per-user data file.

        CRITICAL: Migration from shared DATA_DIR is RESTRICTED to the legacy
        bootstrap admin (user_id=1). New users must never inherit another
        user's config — previously this caused Kevin's auto_deployer_config
        (enabled=True, short_selling=enabled) to be copied into friend's
        dir on first signup, auto-trading without consent.
        """
        user_path = os.path.join(self._user_dir(), filename)
        if (not os.path.exists(user_path)
                and self.current_user
                and self.current_user.get("id") == 1):
            shared_path = os.path.join(DATA_DIR, filename)
            if os.path.exists(shared_path) and shared_path != user_path:
                try:
                    import shutil
                    os.makedirs(os.path.dirname(user_path) or ".", exist_ok=True)
                    shutil.copy2(shared_path, user_path)
                    print(f"[migration] Copied {filename} to bootstrap admin user dir", flush=True)
                except Exception as e:
                    print(f"[migration] WARN failed to migrate {filename}: {e}", flush=True)
        return user_path

    def _send_error_safe(self, exc, status=500, context=""):
        """Log full exception detail server-side and return a generic
        response to the client. Prevents leaking stack traces, DB paths,
        or secret fragments through API error messages.
        Returns a short correlation ID the user can share to help debug.
        """
        import uuid
        correlation_id = uuid.uuid4().hex[:12]
        label = context or "internal"
        try:
            print(f"[ERROR {correlation_id}] ({label}) {type(exc).__name__}: {exc}", flush=True)
        except Exception:
            pass
        self.send_json({
            "error": "Internal error. Please retry.",
            "correlation_id": correlation_id,
        }, status)

    def _sanitize_alpaca_error(self, http_code, err_body):
        """Return a dict the dashboard JS can safely render. Maps upstream
        errors to user-friendly categories instead of leaking raw Alpaca
        detail (which can include rate-limit state, key IDs, internal codes).
        The full error is still logged server-side via print().
        """
        body = (err_body or "")[:200]
        body = re.sub(r'PK[A-Z0-9]{18,}', '[redacted]', body)
        msg = None
        try:
            parsed = json.loads(err_body)
            if isinstance(parsed, dict) and parsed.get("message"):
                msg = str(parsed["message"])[:120]
        except Exception:
            pass
        # Categorize by HTTP code for a safe generic user message
        if http_code == 401 or http_code == 403:
            safe = "Alpaca authentication failed. Check API credentials in Settings."
        elif http_code == 422:
            safe = msg or "Invalid order parameters"
        elif http_code == 429:
            safe = "Alpaca rate limited — please retry in a few seconds"
        elif 500 <= http_code < 600:
            safe = "Alpaca service temporarily unavailable"
        else:
            safe = msg or f"Request failed ({http_code})"
        # Log the full detail server-side for debugging
        print(f"[alpaca-error] HTTP {http_code}: {body}", flush=True)
        return {"error": safe}

    def user_api_get(self, url, timeout=15):
        """GET an Alpaca API URL using the current user's credentials."""
        req = urllib.request.Request(url, headers=self.user_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            return self._sanitize_alpaca_error(e.code, err_body)
        except Exception as e:
            print(f"[user_api_get] {type(e).__name__}: {e}", flush=True)
            return {"error": "Request failed"}

    def user_api_post(self, url, body=None, timeout=15):
        """POST to an Alpaca API URL using the current user's credentials."""
        headers = dict(self.user_headers())
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            return self._sanitize_alpaca_error(e.code, err_body)
        except Exception as e:
            print(f"[user_api_post] {type(e).__name__}: {e}", flush=True)
            return {"error": "Request failed"}

    def user_api_delete(self, url, timeout=15):
        """DELETE an Alpaca API URL using the current user's credentials."""
        req = urllib.request.Request(url, headers=self.user_headers(), method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            return self._sanitize_alpaca_error(e.code, err_body)
        except Exception as e:
            print(f"[user_api_delete] {type(e).__name__}: {e}", flush=True)
            return {"error": "Request failed"}

    def _cors_origin(self):
        """Return the allowed CORS origin (same-origin by default, configurable via env)."""
        return os.environ.get("CORS_ORIGIN", "")

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        cors = self._cors_origin()
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def send_html(self, html, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # Security headers — defense in depth
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")  # prevent clickjacking
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(self), camera=()")
        # CSP — locks down what can execute. `unsafe-inline` allowed because
        # dashboard uses many inline onclick handlers + inline <script>; Chart.js
        # comes from jsdelivr.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_icon_placeholder(self, size):
        """Generate a simple PNG icon placeholder (solid blue square)."""
        import struct
        import zlib
        # Create a minimal valid PNG: solid #3b82f6 square
        width = height = size
        # Build raw image data: filter byte + RGB pixels per row
        raw = b''
        r, g, b = 0x3b, 0x82, 0xf6
        row = bytes([0] + [r, g, b] * width)
        raw = row * height
        # Compress
        compressed = zlib.compress(raw)
        # Build PNG
        def chunk(ctype, data):
            c = ctype + data
            crc = struct.pack('>I', zlib.crc32(c) & 0xffffffff)
            return struct.pack('>I', len(data)) + c + crc
        sig = b'\x89PNG\r\n\x1a\n'
        ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
        png = sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(png)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > 1_000_000:  # 1MB max body size
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        cors = self._cors_origin()
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        # Health check for Railway / monitoring. Returns 200 if the scheduler
        # thread is alive AND recent enough. Returns 503 if the bot is hung.
        if path == "/healthz" or path == "/health" or path == "/ping":
            try:
                import cloud_scheduler as _cs
                thread_alive = (_cs._scheduler_thread is not None
                                 and _cs._scheduler_thread.is_alive())
                # STALENESS CHECK: it's not enough for the thread to be alive and
                # for _recent_logs to be non-empty. A thread that logged once at
                # startup and then hung would pass both checks indefinitely. Use
                # the ISO timestamp (tz-aware ET) on the most recent log to
                # prove the loop is still ticking. Scheduler sleeps 30s between
                # ticks so a gap > 5 min means the loop is wedged.
                with _cs._logs_lock:
                    last_log_count = len(_cs._recent_logs)
                    last_ts_iso = _cs._recent_logs[-1].get("ts_iso") if _cs._recent_logs else None
                seconds_since_last_log = None
                log_stale = False
                if last_ts_iso:
                    try:
                        last = datetime.fromisoformat(last_ts_iso)
                        now = _cs.now_et()
                        seconds_since_last_log = int((now - last).total_seconds())
                        log_stale = seconds_since_last_log > 300  # 5 min
                    except Exception:
                        pass
                healthy = thread_alive and last_log_count > 0 and not log_stale
                status = 200 if healthy else 503
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "ok" if healthy else "degraded",
                    "scheduler_alive": thread_alive,
                    "log_count": last_log_count,
                    "seconds_since_last_log": seconds_since_last_log,
                    "stale": log_stale,
                }).encode())
            except Exception as e:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "detail": str(e)[:120]}).encode())
            return

        # PUBLIC routes — no auth required
        if path == "/login":
            self.send_html(LOGIN_HTML)
            return
        if path == "/signup":
            self.send_html(SIGNUP_HTML)
            return
        if path == "/forgot":
            self.send_html(FORGOT_HTML)
            return
        if path == "/reset":
            self.send_html(RESET_HTML)
            return
        if path == "/manifest.json":
            manifest_path = os.path.join(BASE_DIR, "manifest.json")
            manifest = load_json(manifest_path)
            if manifest:
                self.send_json(manifest)
            else:
                self.send_json({
                    "name": "Stock Trading Bot",
                    "short_name": "StockBot",
                    "start_url": "/",
                    "display": "standalone",
                    "background_color": "#0a0e17",
                    "theme_color": "#3b82f6",
                    "orientation": "any"
                })
            return
        if path == "/sw.js":
            sw = "self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));"
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(sw.encode())
            return
        if path in ("/icon-192.png", "/icon-512.png"):
            size = 192 if "192" in path else 512
            self._serve_icon_placeholder(size)
            return
        # NOTE: /api/logout intentionally NOT handled via GET. Previously an
        # attacker could CSRF-log-out a user via <img src="/api/logout">.
        # The user-menu JS now calls /api/logout via POST + CSRF header.

        # AUTH required below this line
        if not self.check_auth():
            return

        if path == "/":
            self.send_html(DASHBOARD_HTML)

        elif path == "/api/me":
            u = self.current_user or {}
            self.send_json({
                "username": u.get("username", ""),
                "email": u.get("email", ""),
                "is_admin": bool(u.get("is_admin", 0)),
                "alpaca_endpoint": u.get("alpaca_endpoint", ""),
                "alpaca_data_endpoint": u.get("alpaca_data_endpoint", ""),
                "ntfy_topic": u.get("ntfy_topic", ""),
                "notification_email": u.get("notification_email", ""),
                "has_alpaca_key": bool(self.user_api_key),
            })

        elif path == "/api/data":
            data = get_dashboard_data(
                api_endpoint=self.user_api_endpoint,
                api_headers=self.user_headers(),
                user_id=self.current_user.get("id") if self.current_user else None,
            )
            self.send_json(data)

        elif path == "/api/account":
            result = self.user_api_get(f"{self.user_api_endpoint}/account")
            self.send_json(result)

        elif path == "/api/positions":
            result = self.user_api_get(f"{self.user_api_endpoint}/positions")
            self.send_json(result if isinstance(result, list) else [])

        elif path == "/api/orders":
            result = self.user_api_get(f"{self.user_api_endpoint}/orders?status=open&limit=50")
            self.send_json(result if isinstance(result, list) else [])

        elif path == "/api/auto-deployer-config":
            config = load_json(self._user_file("auto_deployer_config.json"))
            self.send_json(config if config else {"enabled": False})

        elif path == "/api/guardrails":
            guardrails = load_json(self._user_file("guardrails.json"))
            self.send_json(guardrails if guardrails else {"kill_switch": False})

        elif path == "/api/scheduler-status":
            if SCHEDULER_AVAILABLE:
                self.send_json(get_scheduler_status())
            else:
                self.send_json({"running": False, "error": "Scheduler module not loaded"})

        elif path == "/api/readme":
            readme_path = os.path.join(BASE_DIR, "README.md")
            try:
                with open(readme_path, encoding="utf-8") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "private, max-age=60")
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))
            except Exception as e:
                self.send_json({"error": f"Could not read README: {e}"}, 500)

        elif path == "/api/compute-backtest":
            # On-demand backtest for any symbol (even ones the screener didn't
            # enrich — the dropdown now shows all top-50 picks so the user
            # can pick any of them).
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            symbol = (params.get("symbol") or [""])[0].strip().upper()
            strategy = (params.get("strategy") or ["Trailing Stop"])[0].strip()
            if not re.match(r"^[A-Z.]{1,10}$", symbol):
                self.send_json({"error": "Invalid symbol"}, 400)
                return
            try:
                # Fetch 30d daily bars from the caller's data endpoint
                from datetime import timedelta as _td
                end = now_et().date()
                start = end - _td(days=45)
                url = (f"{self.user_data_endpoint}/stocks/{symbol}/bars"
                       f"?timeframe=1Day&start={start}T00:00:00Z&end={end}T00:00:00Z"
                       f"&limit=45&adjustment=split")
                bars_resp = self.user_api_get(url)
                bars = (bars_resp or {}).get("bars", [])
                if not isinstance(bars, list) or len(bars) < 5:
                    self.send_json({"error": "Not enough price history"}, 404)
                    return
                # Take the most recent 30 trading days
                bars = bars[-30:]
                # Use OHLC for realistic stop detection — stops trigger on
                # intraday LOWS, not daily closes. Previously the backtest
                # overstated performance on gap-down days (close $97 looked
                # fine but the day's low $90 had already triggered the stop).
                ohlc = []
                for b in bars:
                    o = float(b.get("o") or 0)
                    h = float(b.get("h") or 0)
                    l = float(b.get("l") or 0)
                    c = float(b.get("c") or 0)
                    if c:
                        ohlc.append({"o": o or c, "h": h or c, "l": l or c, "c": c})
                if len(ohlc) < 5:
                    self.send_json({"error": "Price data incomplete"}, 404)
                    return
                entry = ohlc[0]["c"]
                peak = entry
                stop = entry * 0.90
                equity = []
                stop_levels = []
                stopped_out = False
                exit_price = ohlc[-1]["c"]
                trailing_active = False
                for i, bar in enumerate(ohlc):
                    # Trailing activation uses high (highest seen today)
                    if not trailing_active and bar["h"] >= entry * 1.10:
                        trailing_active = True
                    if trailing_active:
                        if bar["h"] > peak:
                            peak = bar["h"]
                        stop = max(stop, peak * 0.95)
                    equity.append(round(bar["c"], 2))
                    stop_levels.append(round(stop, 2))
                    # Stop triggered if day's LOW breached the stop level.
                    # Fill price = max(open, stop) to model gap-down fills
                    # where the open is below the stop.
                    if bar["l"] <= stop:
                        stopped_out = True
                        exit_price = max(bar["o"], stop) if bar["o"] < stop else stop
                        while len(equity) < len(ohlc):
                            equity.append(exit_price)
                            stop_levels.append(stop)
                        break
                return_pct = round((exit_price / entry - 1) * 100, 2) if entry else 0
                self.send_json({
                    "symbol": symbol,
                    "strategy": strategy,
                    "backtest_detail": {
                        "entry": round(entry, 2),
                        "exit": round(exit_price, 2),
                        "return_pct": return_pct,
                        "days": len(equity),
                        "stopped_out": stopped_out,
                        "equity_curve": equity,
                        "stop_curve": stop_levels,
                    }
                })
            except Exception as e:
                self._send_error_safe(e, 500, "compute-backtest")

        elif path == "/api/trade-heatmap":
            # Per-user trade journal — each user has their own heatmap
            journal = load_json(self._user_file("trade_journal.json")) or {}
            snapshots = journal.get("daily_snapshots", [])
            trades = journal.get("trades", [])

            # Build daily P&L map
            daily_pnl = {}
            for snap in snapshots:
                date = snap.get("date", "")
                if date:
                    daily_pnl[date] = {
                        "date": date,
                        "pnl": snap.get("daily_pnl", 0),
                        "pnl_pct": snap.get("daily_pnl_pct", 0),
                        "trades": snap.get("closed_today", 0),
                        "wins": snap.get("wins_today", 0),
                        "losses": snap.get("losses_today", 0),
                        "weekday": datetime.strptime(date, "%Y-%m-%d").strftime("%A") if date else "",
                    }

            # Analyze patterns
            by_weekday = {}
            for d in daily_pnl.values():
                wd = d.get("weekday", "")
                if wd:
                    if wd not in by_weekday:
                        by_weekday[wd] = {"total_pnl": 0, "days": 0, "wins": 0}
                    by_weekday[wd]["total_pnl"] += d.get("pnl", 0)
                    by_weekday[wd]["days"] += 1
                    if d.get("pnl", 0) > 0:
                        by_weekday[wd]["wins"] += 1

            for wd in by_weekday:
                d = by_weekday[wd]
                d["avg_pnl"] = d["total_pnl"] / d["days"] if d["days"] > 0 else 0
                d["win_rate"] = d["wins"] / d["days"] * 100 if d["days"] > 0 else 0

            self.send_json({
                "daily_pnl": list(daily_pnl.values()),
                "by_weekday": by_weekday,
                "total_days": len(daily_pnl),
                "total_trades": len(trades),
            })

        elif path == "/api/wheel-status":
            # Return all active wheel strategies for the current user with
            # enough state detail for the dashboard wheel card to render.
            try:
                import wheel_strategy as ws
                user = {
                    "_api_key": self.user_api_key,
                    "_api_secret": self.user_api_secret,
                    "_api_endpoint": self.user_api_endpoint,
                    "_data_endpoint": self.user_data_endpoint,
                    "_data_dir": auth.user_data_dir(self.current_user["id"]) if self.current_user else DATA_DIR,
                    "_strategies_dir": os.path.join(
                        auth.user_data_dir(self.current_user["id"]) if self.current_user else DATA_DIR,
                        "strategies"
                    ),
                }
                wheels = ws.list_wheel_files(user)
                result = []
                total_premium = 0
                total_pnl = 0
                for fname, state in wheels:
                    total_premium += state.get("total_premium_collected", 0)
                    total_pnl += state.get("total_realized_pnl", 0)
                    result.append({
                        "symbol": state.get("symbol"),
                        "stage": state.get("stage"),
                        "shares_owned": state.get("shares_owned", 0),
                        "cost_basis": state.get("cost_basis"),
                        "cycles_completed": state.get("cycles_completed", 0),
                        "total_premium_collected": state.get("total_premium_collected", 0),
                        "total_realized_pnl": state.get("total_realized_pnl", 0),
                        "active_contract": state.get("active_contract"),
                        "created": state.get("created"),
                        "updated": state.get("updated"),
                        "history": state.get("history", [])[-5:],  # Last 5 events
                    })
                self.send_json({
                    "wheels": result,
                    "total_active": len(result),
                    "total_premium_collected": round(total_premium, 2),
                    "total_realized_pnl": round(total_pnl, 2),
                    "safety_rails": {
                        "min_stock_price": ws.MIN_STOCK_PRICE,
                        "max_stock_price": ws.MAX_STOCK_PRICE,
                        "min_dte": ws.MIN_DTE,
                        "max_dte": ws.MAX_DTE,
                        "put_strike_pct_below": ws.PUT_STRIKE_PCT_BELOW,
                        "call_strike_pct_above": ws.CALL_STRIKE_PCT_ABOVE,
                        "profit_close_pct": ws.PROFIT_CLOSE_PCT,
                        "max_concurrent_wheels": ws.MAX_CONCURRENT_WHEELS,
                        "earnings_avoid_days": ws.EARNINGS_AVOID_DAYS,
                    }
                })
            except Exception as e:
                self._send_error_safe(e, 500, "wheel-status")

        elif path == "/api/admin/users":
            # Admin only: list all users (active + inactive) with metadata.
            if not self.current_user or not self.current_user.get("is_admin"):
                self.send_json({"error": "Admin only"}, 403)
                return
            try:
                conn = auth._get_db()
                cur = conn.cursor()
                # NOTE: do NOT return ntfy_topic. Admin could publish to another
                # user's topic (ntfy.sh is public pub/sub) to spoof notifications.
                # Return only has_ntfy_topic boolean.
                cur.execute("""
                    SELECT id, username, email, is_admin, is_active,
                           created_at, last_login, login_count,
                           alpaca_endpoint,
                           CASE WHEN ntfy_topic IS NOT NULL AND ntfy_topic != ''
                                THEN 1 ELSE 0 END AS has_ntfy_topic
                    FROM users ORDER BY id ASC
                """)
                users = [dict(r) for r in cur.fetchall()]
                conn.close()
                self.send_json({"users": users})
            except Exception as e:
                self._send_error_safe(e, 500, "admin-users")

        elif path == "/api/admin/audit-log":
            # Admin-only: return recent audit log entries with optional filters
            if not self.current_user or not self.current_user.get("is_admin"):
                self.send_json({"error": "Admin only"}, 403)
                return
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            try:
                limit = min(int((params.get("limit") or ["200"])[0]), 1000)
            except (ValueError, TypeError):
                limit = 200
            action_filter = (params.get("action") or [None])[0]
            user_filter_raw = (params.get("user_id") or [None])[0]
            user_filter = None
            if user_filter_raw:
                try:
                    user_filter = int(user_filter_raw)
                except ValueError:
                    pass
            try:
                entries = auth.list_audit_log(limit=limit,
                                              action_filter=action_filter,
                                              user_id_filter=user_filter)
                self.send_json({"entries": entries, "count": len(entries)})
            except Exception as e:
                self._send_error_safe(e, 500, "audit-log")

        elif path == "/api/admin/list-backups":
            # Admin-only: list backup archives available on the Railway volume
            if not self.current_user or not self.current_user.get("is_admin"):
                self.send_json({"error": "Admin only"}, 403)
                return
            try:
                import backup as _backup
                backups = _backup.list_backups()
                # Strip absolute filesystem paths from the client response
                public = [{
                    "name": b["name"],
                    "size_bytes": b["size_bytes"],
                    "size_mb": round(b["size_bytes"] / 1024 / 1024, 2),
                    "created_at": b["created_at"],
                } for b in backups]
                self.send_json({"backups": public, "count": len(public),
                                 "retention_days": _backup.RETENTION_DAYS})
            except Exception as e:
                self._send_error_safe(e, 500, "list-backups")

        elif path == "/api/admin/download-backup":
            # Admin-only: stream a backup archive to the caller. Validates
            # the requested name matches the timestamped filename format
            # ("YYYY-MM-DD_HHMMSS" + "ET" or legacy "UTC" suffix) to prevent
            # any path traversal.
            if not self.current_user or not self.current_user.get("id") or not self.current_user.get("is_admin"):
                self.send_json({"error": "Admin only"}, 403)
                return
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            name = (params.get("name") or [""])[0]
            if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}(ET|UTC)\.tar\.gz$", name):
                self.send_json({"error": "Invalid backup name"}, 400)
                return
            try:
                import backup as _backup
                path_full = os.path.join(_backup.BACKUP_DIR, name)
                if not os.path.isfile(path_full):
                    self.send_json({"error": "Backup not found"}, 404)
                    return
                # Record the download — backups contain encrypted credentials
                # so who-downloaded-when matters for audit.
                auth.log_admin_action("backup_downloaded",
                                       actor=self.current_user,
                                       ip_address=self.client_address[0] if self.client_address else None,
                                       detail={"backup_name": name})
                size = os.path.getsize(path_full)
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(path_full, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception as e:
                self._send_error_safe(e, 500, "download-backup")

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self.read_body()

        # PUBLIC auth endpoints (no auth required)
        if path == "/api/login":
            self.handle_login(body)
            return
        if path == "/api/signup":
            self.handle_signup(body)
            return
        if path == "/api/forgot":
            self.handle_forgot_password(body)
            return
        if path == "/api/reset":
            self.handle_reset_password(body)
            return
        if path == "/api/logout":
            self.handle_logout()
            return

        # AUTH required below this line
        if not self.check_auth():
            return

        # CSRF defense: double-submit cookie. Require every state-changing POST
        # to carry an X-CSRF-Token header matching the csrf cookie set at
        # login. SameSite=Strict on the session cookie is the primary defense
        # but the CSRF token adds a second independent layer.
        if not self._csrf_ok():
            return self.send_json({"error": "CSRF token missing or invalid"}, 403)

        if path == "/api/change-password":
            self.handle_change_password(body)
            return
        if path == "/api/update-settings":
            self.handle_update_settings(body)
            return
        if path == "/api/delete-account":
            self.handle_delete_account(body)
            return
        if path == "/api/admin/set-active":
            self.handle_admin_set_active(body)
            return
        if path == "/api/admin/reset-password":
            self.handle_admin_reset_password(body)
            return
        if path == "/api/admin/create-backup":
            self.handle_admin_create_backup()
            return

        if path == "/api/refresh":
            self.handle_refresh()

        elif path == "/api/deploy":
            self.handle_deploy(body)

        elif path == "/api/cancel-order":
            self.handle_cancel_order(body)

        elif path == "/api/close-position":
            self.handle_close_position(body)

        elif path == "/api/sell":
            self.handle_sell(body)

        elif path == "/api/auto-deployer":
            self.handle_auto_deployer(body)

        elif path == "/api/kill-switch":
            self.handle_kill_switch(body)

        elif path == "/api/pause-strategy":
            self.handle_pause_strategy(body)

        elif path == "/api/stop-strategy":
            self.handle_stop_strategy(body)

        elif path == "/api/apply-preset":
            self.handle_apply_preset(body)

        elif path == "/api/toggle-short-selling":
            self.handle_toggle_short_selling(body)

        elif path == "/api/force-auto-deploy":
            self.handle_force_auto_deploy()

        else:
            self.send_json({"error": "Not found"}, 404)

    # ===== Auth handlers =====

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
        if not csrf_cookie:
            # Transitional: pre-CSRF session. Accept this request but rotate
            # the cookie so the next POST requires matching.
            return True
        if not csrf_header:
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
        if _login_rate_limited(ip, username):
            return self.send_json(
                {"error": "Too many failed attempts. Try again in 15 minutes."}, 429
            )

        user = auth.authenticate(username, password)
        if not user:
            _login_attempt_record(ip, username, success=False)
            auth.log_admin_action("login_failed", actor=None, ip_address=ip,
                                   detail={"attempted_username": username[:50]})
            # Small timing-safe sleep to slow down fast brute force even after
            # the lockout is cleared. Matches approximate PBKDF2 timing.
            return self.send_json({"error": "Invalid credentials"}, 401)

        _login_attempt_record(ip, username, success=True)
        auth.log_admin_action("login_success", actor=user, target_user_id=user["id"], ip_address=ip)
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
        ntfy_topic = (body.get("ntfy_topic") or "").strip()
        invite_code = (body.get("invite_code") or "").strip()

        if not is_valid_ntfy_topic(ntfy_topic):
            return self.send_json({
                "error": "Invalid ntfy topic. Allowed: letters, digits, _, - (4-64 chars)."
            }, 400)

        # Gate: SIGNUP_DISABLED env var blocks all signups; SIGNUP_INVITE_CODE
        # requires a matching code. Both are for sharing the deployment safely
        # with a specific friend without exposing it to the public internet.
        allowed, reason = signup_allowed(invite_code)
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
        if not is_allowed_alpaca_endpoint(alpaca_endpoint, data=False):
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
            notification_email=email,
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
        token, user = auth.create_password_reset(email)
        # Do not reveal whether the email exists (security)
        generic_ok = {"success": True, "message": "If that email exists, a reset link has been sent."}
        if not token:
            return self.send_json(generic_ok)

        # Build reset URL — prefer configured base, fall back to request Host
        base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        if not base_url:
            host = self.headers.get("Host", "")
            # Assume HTTPS on Railway (edge TLS), HTTP locally
            scheme = "https" if host and "localhost" not in host and "127.0.0.1" not in host else "http"
            base_url = f"{scheme}://{host}" if host else "https://stockbott.up.railway.app"
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
                os.path.join(BASE_DIR, "notify.py"),
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
        # Per-user queue file keeps password-reset emails isolated so one user
        # can't see another user's queue, and concurrent writes don't race on
        # a single shared file.
        try:
            try:
                import auth as _auth
                _uqdir = _auth.user_data_dir(user["id"])
            except Exception:
                _uqdir = DATA_DIR
            email_queue_path = os.path.join(_uqdir, "email_queue.json")
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
                "timestamp": now_et().isoformat(),
            })
            with open(email_queue_path, "w") as f:
                json.dump(queue, f, indent=2)
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
        if "alpaca_endpoint" in updates and not is_allowed_alpaca_endpoint(updates["alpaca_endpoint"], data=False):
            return self.send_json({"error": "Invalid Alpaca endpoint. Must be paper or live Alpaca API."}, 400)
        if "alpaca_data_endpoint" in updates and not is_allowed_alpaca_endpoint(updates["alpaca_data_endpoint"], data=True):
            return self.send_json({"error": "Invalid Alpaca data endpoint. Must be data.alpaca.markets."}, 400)
        # ntfy_topic validation — prevent URL breakout and spoof-by-disclosure
        if "ntfy_topic" in updates and not is_valid_ntfy_topic(updates["ntfy_topic"]):
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

    def handle_admin_set_active(self, body):
        """Admin-only: set is_active on any user."""
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        target_id = body.get("user_id")
        is_active = body.get("is_active")
        if target_id is None or is_active is None:
            return self.send_json({"error": "user_id and is_active required"}, 400)
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return self.send_json({"error": "user_id must be integer"}, 400)

        # Block last-admin deactivation
        if not is_active:
            try:
                conn = auth._get_db()
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*) FROM users
                    WHERE is_admin = 1 AND is_active = 1 AND id != ?
                """, (target_id,))
                others = cur.fetchone()[0]
                cur.execute("SELECT is_admin FROM users WHERE id = ?", (target_id,))
                row = cur.fetchone()
                conn.close()
                if row and row[0] and others == 0:
                    return self.send_json({"error":
                        "Cannot deactivate the last active admin"}, 400)
            except Exception:
                pass

        try:
            conn = auth._get_db()
            cur = conn.cursor()
            cur.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if is_active else 0, target_id))
            if not is_active:
                cur.execute("DELETE FROM sessions WHERE user_id = ?", (target_id,))
            conn.commit()
            conn.close()
            auth.log_admin_action(
                "reactivate_user" if is_active else "deactivate_user",
                actor=self.current_user,
                target_user_id=target_id,
                ip_address=self.client_address[0] if self.client_address else None,
            )
            self.send_json({"success": True})
        except Exception as e:
            self._send_error_safe(e, 500, "admin-op")

    def handle_admin_reset_password(self, body):
        """Admin-only: force a password reset for any user.
        An admin CANNOT reset another admin's password (prevents insider
        takeover). Admins who want to change their own password use the
        standard /api/change-password flow. Only self-reset allowed here.
        """
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        target_id = body.get("user_id")
        new_password = body.get("new_password") or ""
        if target_id is None:
            return self.send_json({"error": "user_id required"}, 400)
        if len(new_password) < 8:
            return self.send_json({"error": "Password must be at least 8 characters"}, 400)
        try:
            target_id = int(target_id)
            # Block cross-admin reset unless target == actor (self-reset OK)
            if target_id != self.current_user.get("id"):
                target_user = auth.get_user_by_id(target_id)
                if target_user and target_user.get("is_admin"):
                    return self.send_json({
                        "error": "Cannot reset another admin's password. "
                                 "The target admin must use password-reset themselves."
                    }, 403)
            ok, err = auth.change_password(target_id, new_password)
            if not ok:
                return self.send_json({"error": err or "Password rejected"}, 400)
            # Invalidate all sessions for that user so they're forced to re-login
            conn = auth._get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM sessions WHERE user_id = ?", (target_id,))
            conn.commit()
            conn.close()
            auth.log_admin_action(
                "admin_reset_password",
                actor=self.current_user,
                target_user_id=target_id,
                ip_address=self.client_address[0] if self.client_address else None,
            )
            self.send_json({"success": True})
        except Exception as e:
            self._send_error_safe(e, 500, "admin-op")

    def handle_admin_create_backup(self):
        """Admin-only: create an on-demand backup of the Railway volume."""
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        try:
            import backup as _backup
            path, size, err = _backup.create_backup()
            if err:
                return self.send_json({"error": f"Backup failed: {err}"}, 500)
            auth.log_admin_action("backup_created_manual",
                                   actor=self.current_user,
                                   ip_address=self.client_address[0] if self.client_address else None,
                                   detail={"backup_path": os.path.basename(path),
                                           "size_mb": round(size / 1024 / 1024, 2)})
            self.send_json({
                "success": True,
                "name": os.path.basename(path),
                "size_mb": round(size / 1024 / 1024, 2),
            })
        except Exception as e:
            self._send_error_safe(e, 500, "create-backup")

    def handle_refresh(self):
        """Run update_dashboard.py with current user's credentials and return fresh data."""
        # Rate limit: each user's refresh spawns a 10-min-capable subprocess.
        # Without a lock, rapid clicks spawn N parallel screener runs and DoS
        # Alpaca + CPU. 30-second cooldown per user.
        user_id = self.current_user.get("id") if self.current_user else None
        if user_id is not None:
            now_ts = time.time()
            last = _refresh_cooldowns.get(user_id, 0)
            if now_ts - last < 30:
                wait = int(30 - (now_ts - last))
                return self.send_json({
                    "error": f"Refresh cooling down — try again in {wait}s"
                }, 429)
            _refresh_cooldowns[user_id] = now_ts

        script_path = os.path.join(BASE_DIR, "update_dashboard.py")
        env = os.environ.copy()
        if user_id is not None:
            try:
                import auth as _auth
                udir = _auth.user_data_dir(user_id)
                env["ALPACA_API_KEY"] = self.user_api_key
                env["ALPACA_API_SECRET"] = self.user_api_secret
                env["ALPACA_ENDPOINT"] = self.user_api_endpoint
                env["ALPACA_DATA_ENDPOINT"] = self.user_data_endpoint
                env["DASHBOARD_DATA_PATH"] = os.path.join(udir, "dashboard_data.json")
                env["DASHBOARD_HTML_PATH"] = os.path.join(udir, "dashboard.html")
            except Exception as e:
                print(f"user env setup failed: {e}")
        try:
            result = subprocess.run(
                ["python3", script_path],
                cwd=BASE_DIR,
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
        data = get_dashboard_data(
            api_endpoint=self.user_api_endpoint,
            api_headers=self.user_headers(),
            user_id=user_id,
        )
        self.send_json(data)

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
        save_json(os.path.join(self._user_strategies_dir(), f"trailing_stop_{symbol}.json"), strategy_data)

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
        save_json(os.path.join(self._user_strategies_dir(), "wheel_strategy.json"), strategy_data)

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
        save_json(os.path.join(self._user_strategies_dir(), "copy_trading.json"), strategy_data)

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
        save_json(os.path.join(self._user_strategies_dir(), f"mean_reversion_{symbol}.json"), strategy_data)

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
        save_json(os.path.join(self._user_strategies_dir(), f"breakout_{symbol}.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "breakout",
            "symbol": symbol,
            "buy_order_id": buy_order_id,
            "stop_price": stop_price,
            "price": price,
            "note": "Trailing stop will be placed by strategy-monitor after buy fills.",
        })

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
        config = load_json(config_path)
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
        save_json(config_path, config)
        self.send_json({"success": True, "enabled": config["enabled"]})

    def handle_kill_switch(self, body):
        """Activate or deactivate the kill switch FOR THE CURRENT USER ONLY.
        Each user has their own guardrails.json and auto_deployer_config.json —
        one user's kill switch must not halt another user's trading.
        """
        activate = body.get("activate", False)
        timestamp = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
        guardrails_path = self._user_file("guardrails.json")
        guardrails = load_json(guardrails_path) or {}

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
            save_json(guardrails_path, guardrails)

            # 4. Set enabled: false in THIS USER's auto_deployer_config.json
            ad_config_path = self._user_file("auto_deployer_config.json")
            ad_config = load_json(ad_config_path) or {}
            ad_config["enabled"] = False
            ad_config["last_toggled"] = timestamp
            save_json(ad_config_path, ad_config)

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
            subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "notify.py"), "--type", "kill", f"Cancelled {orders_cancelled} orders, closed {positions_closed} positions. All trading halted."], cwd=BASE_DIR)

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
            save_json(guardrails_path, guardrails)

            print(f"[KILL SWITCH] Deactivated at {timestamp}")

            self.send_json({
                "success": True,
                "activated": False,
                "timestamp": timestamp,
                "message": "Kill switch deactivated. Auto-deployer remains off - re-enable manually.",
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
        for fpath in files:
            data = load_json(fpath)
            if data:
                data["status"] = "paused"
                data["paused_at"] = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
                save_json(fpath, data)
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
        for fpath in files:
            data = load_json(fpath)
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
                save_json(fpath, data)
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
        guardrails = load_json(guardrails_path) or {}
        guardrails["max_positions"] = max_positions
        guardrails["max_position_pct"] = max_position_pct
        if strategies:
            guardrails["strategies_allowed"] = strategies
        save_json(guardrails_path, guardrails)

        config_path = self._user_file("auto_deployer_config.json")
        config = load_json(config_path) or {}
        config["risk_settings"] = config.get("risk_settings", {})
        config["risk_settings"]["default_stop_loss_pct"] = stop_loss_pct
        config["max_positions"] = max_positions
        save_json(config_path, config)

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
        config = load_json(config_path) or {}
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
        save_json(config_path, config)
        msg = "Short selling ENABLED — will deploy in bear markets" if enabled else "Short selling DISABLED — no new shorts will deploy"
        self.send_json({"message": msg, "enabled": enabled})

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


def _graceful_shutdown(httpd, reason="signal"):
    """Called on SIGTERM (Railway redeploy) or Ctrl+C. Tries to drain in-flight
    work before the Python process exits, so orders aren't abandoned mid-POST.
    """
    print(f"\n[shutdown] {reason} received — stopping scheduler + server...", flush=True)
    try:
        if SCHEDULER_AVAILABLE:
            stop_scheduler()
    except Exception as e:
        print(f"[shutdown] scheduler stop error: {e}", flush=True)
    try:
        httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass
    print("[shutdown] clean exit", flush=True)


def main():
    port = int(os.environ.get("PORT", 8888))

    # Secure the SQLite DB file: 0o600 so nothing else on the container
    # (or on a misconfigured shared volume) can read the encrypted Alpaca
    # credentials + password hashes. Best-effort — chmod may not persist
    # on Railway's volume but at least tightens local dev.
    try:
        db_path = os.path.join(DATA_DIR, "users.db")
        if os.path.exists(db_path):
            os.chmod(db_path, 0o600)
    except Exception:
        pass

    # Enable SQLite WAL mode for concurrent readers+writer (scheduler thread
    # + HTTP handler threads + backup .backup API all touch the DB). Without
    # WAL, writers block readers via rollback journal → SQLITE_BUSY under load.
    try:
        _c = auth._get_db()
        _c.execute("PRAGMA journal_mode=WAL;")
        _c.execute("PRAGMA busy_timeout=5000;")
        _c.execute("PRAGMA synchronous=NORMAL;")
        _c.commit()
        _c.close()
        print("[init] SQLite WAL + busy_timeout enabled", flush=True)
    except Exception as e:
        print(f"[init] WAL setup failed: {e}", flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop")

    # Start cloud scheduler (makes bot autonomous 24/7 on Railway)
    if SCHEDULER_AVAILABLE and os.environ.get("ENABLE_CLOUD_SCHEDULER", "true").lower() == "true":
        try:
            start_scheduler()
            print("[INFO] Cloud scheduler started — bot running autonomously", flush=True)
        except Exception as e:
            print(f"[WARN] Could not start cloud scheduler: {e}", flush=True)

    # Register SIGTERM (Railway redeploy) + SIGINT (Ctrl+C) handlers for
    # graceful shutdown. Without these, Python exits immediately and any
    # in-flight order POSTs become orphan positions.
    import signal as _signal
    def _sigterm_handler(signum, frame):
        _graceful_shutdown(server, reason=f"signal {signum}")
        sys.exit(0)
    try:
        _signal.signal(_signal.SIGTERM, _sigterm_handler)
    except (ValueError, AttributeError):
        pass  # Not in main thread / not supported

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _graceful_shutdown(server, reason="KeyboardInterrupt")


if __name__ == "__main__":
    main()
