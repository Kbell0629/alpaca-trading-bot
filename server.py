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
from handlers.auth_mixin import AuthHandlerMixin
from handlers.admin_mixin import AdminHandlerMixin
from handlers.strategy_mixin import StrategyHandlerMixin
from handlers.actions_mixin import ActionsHandlerMixin
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
    from cloud_scheduler import start_scheduler, stop_scheduler, get_scheduler_status
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
# Round-11: fail-loud at boot if the mount is read-only. If Railway's
# volume fails to attach, /data still EXISTS (container root), so
# makedirs returns cleanly — but subsequent writes silently fail and
# /healthz stays green while the bot is effectively dead. Better to
# crash hard and let Railway restart than zombify.
if not os.access(DATA_DIR, os.W_OK):
    import sys as _sys_boot
    print(f"[FATAL] DATA_DIR ({DATA_DIR!r}) not writable — check volume mount.",
          flush=True)
    _sys_boot.exit(1)

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
_api_cache_lock = threading.Lock()
_cache_ttl = 10  # seconds
_API_CACHE_MAX = 500  # sweep when exceeded to prevent unbounded growth


def alpaca_get_cached(url, timeout=15, headers=None):
    """Cached version of alpaca_get for dashboard data.

    If headers is provided, the cache is keyed by (url, key-id) so different
    users don't share each other's data. When headers is None, uses the
    module-level env-var headers (backward compat).

    Thread safety: ThreadingHTTPServer spawns one thread per request. Without
    _api_cache_lock, the sweep-then-write pattern below could (1) raise
    RuntimeError from concurrent dict iteration and (2) race on
    _api_cache[cache_key] assignment. The lock is taken for the fast lookup
    + sweep + write path. The actual network call is OUTSIDE the lock so
    one slow request doesn't serialize unrelated cache reads.
    """
    now = time.time()
    cache_key = (url, (headers or {}).get("APCA-API-KEY-ID", ""))

    # Fast path: hit under the lock, return cached value without touching
    # the network.
    with _api_cache_lock:
        entry = _api_cache.get(cache_key)
        if entry and now - entry["time"] < _cache_ttl:
            return entry["data"]

    # Miss. Fetch without holding the lock.
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

    # Write back under the lock. Sweep expired entries here (not in the
    # read path) so the sweep is serialized with writes and can't race
    # against concurrent iteration.
    with _api_cache_lock:
        _api_cache[cache_key] = {"data": data, "time": now}
        if len(_api_cache) > _API_CACHE_MAX:
            now2 = time.time()
            for k in list(_api_cache.keys()):
                if now2 - _api_cache[k]["time"] > _cache_ttl:
                    _api_cache.pop(k, None)
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

    # Compute trading_session LIVE on every request, not from the stale
    # value baked into dashboard_data.json by the last screener run.
    # The screener only runs every 30 min during market hours, so after
    # 4:00 PM close the stored value stays at "market" all evening.
    # Dashboard UI reads `trading_session` to decide the header badge
    # (MARKET OPEN / AFTER HOURS / CLOSED). Overwrite here so it's
    # always current with wall-clock ET.
    try:
        from extended_hours import get_trading_session as _live_session
        current_session = _live_session()
    except Exception:
        current_session = "unknown"

    if data:
        data["account"] = account if isinstance(account, dict) and "error" not in account else data.get("account", {})
        data["positions"] = positions if isinstance(positions, list) else data.get("positions", [])
        data["open_orders"] = orders if isinstance(orders, list) else data.get("open_orders", [])
        data["updated_at"] = now_et().strftime("%Y-%m-%d %I:%M:%S %p ET")
        data["api_errors"] = api_errors
        data["trading_session"] = current_session
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
        "trading_session": current_session,
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
_refresh_cooldowns_lock = threading.Lock()  # serialize compare-and-set

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


class DashboardHandler(
    AuthHandlerMixin,
    AdminHandlerMixin,
    StrategyHandlerMixin,
    ActionsHandlerMixin,
    BaseHTTPRequestHandler,
):
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
                # STALENESS CHECK: thread-alive + log-count-positive passed
                # forever once the thread logged anything, even if it hung.
                # The recent-log timestamp proves the loop is still ticking.
                # Scheduler sleeps 30s per tick so a gap > 5 min means wedged.
                # Both _recent_logs snapshot and the [-1] access are inside
                # the lock — without it, concurrent list mutation from the
                # scheduler thread could raise IndexError on the handler.
                last_log_count = 0
                last_ts_iso = None
                with _cs._logs_lock:
                    if _cs._recent_logs:
                        last_log_count = len(_cs._recent_logs)
                        last_entry = _cs._recent_logs[-1]
                        last_ts_iso = last_entry.get("ts_iso") if isinstance(last_entry, dict) else None
                seconds_since_last_log = None
                log_stale = False
                if last_ts_iso:
                    try:
                        last = datetime.fromisoformat(last_ts_iso)
                        now = _cs.now_et()
                        seconds_since_last_log = int((now - last).total_seconds())
                        # Negative seconds (clock skew) should not be flagged stale
                        log_stale = seconds_since_last_log is not None and seconds_since_last_log > 300
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

        # /api/version — public endpoint returning which commit is running
        # and some lightweight health stats. Useful for confirming a Railway
        # deploy actually swapped the process (sometimes a failed deploy
        # leaves the old container running, and there was previously no way
        # to tell from outside).
        if path == "/api/version":
            info = {"bot_version": "round-11"}
            try:
                import subprocess as _sp
                r = _sp.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=BASE_DIR, capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0:
                    info["commit"] = r.stdout.strip()[:12]
            except Exception:
                pass
            info["python"] = sys.version.split()[0]
            try:
                import cloud_scheduler as _cs
                info["scheduler_alive"] = (_cs._scheduler_thread is not None
                                            and _cs._scheduler_thread.is_alive())
            except Exception:
                info["scheduler_alive"] = None
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(info).encode())
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

            # Override TODAY's entry with live Alpaca /account data so the
            # heatmap's Total P&L matches the Overview's Daily P&L. Without
            # this, the snapshot is whatever portfolio_value was when
            # update_scorecard last ran (manual daily close, actual 4:05 PM
            # close, etc.), which can drift hours behind the live value
            # as after-hours feeds revise close prices. Past days stay
            # locked to their snapshots — only the current day recomputes.
            try:
                today_str = now_et().strftime("%Y-%m-%d")
                live_acct = self.user_api_get(f"{self.user_api_endpoint}/account")
                if isinstance(live_acct, dict) and "error" not in live_acct:
                    pv = float(live_acct.get("portfolio_value") or 0)
                    le = float(live_acct.get("last_equity") or pv)
                    live_pnl = pv - le
                    live_pnl_pct = (live_pnl / le * 100) if le else 0
                    existing = daily_pnl.get(today_str, {})
                    daily_pnl[today_str] = {
                        "date": today_str,
                        "pnl": round(live_pnl, 2),
                        "pnl_pct": round(live_pnl_pct, 2),
                        "trades": existing.get("trades", 0),
                        "wins": existing.get("wins", 0),
                        "losses": existing.get("losses", 0),
                        "weekday": datetime.strptime(today_str, "%Y-%m-%d").strftime("%A"),
                        "live": True,  # flag for UI so it can mark "as of now"
                    }
            except Exception as _e:
                # Live overlay is a nice-to-have — fall back to snapshot
                # if the Alpaca call fails.
                print(f"[heatmap] live-today overlay failed: {_e}", flush=True)

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

        elif path == "/api/force-daily-close":
            self.handle_force_daily_close()

        else:
            self.send_json({"error": "Not found"}, 404)

    # Handler implementations live in handlers/*.py mixins (round-6.5
    # decomposition). DashboardHandler is now HTTP routing + shared
    # utilities only; the ~2300 lines of endpoint bodies moved out.














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

    # Start cloud scheduler (makes bot autonomous 24/7 on Railway).
    # Round-10 audit: on a scheduler-start failure we USED to just log
    # a warning and keep the HTTP server running — that produced a
    # zombie state where /healthz returned 503 forever (staleness
    # threshold tripped), Railway restarted 10 times, then gave up
    # and let the container hold idle. Exit with a non-zero code
    # instead so Railway redeploys us cleanly.
    if SCHEDULER_AVAILABLE and os.environ.get("ENABLE_CLOUD_SCHEDULER", "true").lower() == "true":
        try:
            start_scheduler()
            print("[INFO] Cloud scheduler started — bot running autonomously", flush=True)
        except Exception as e:
            print(f"[FATAL] Could not start cloud scheduler: {e}. Exiting for restart.", flush=True)
            import sys as _sys_exit
            _sys_exit.exit(1)

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
