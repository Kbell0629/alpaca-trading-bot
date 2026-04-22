#!/usr/bin/env python3
"""
Alpaca Trading Bot — Interactive Web Dashboard Server
Serves a fully interactive dashboard at http://localhost:8888
with API endpoints for deploying strategies, managing orders/positions, and more.

NOTE: HTTPS termination is handled by Railway's edge proxy. All traffic between
the client and Railway is encrypted via TLS. The app itself listens on plain HTTP.
"""

# Semantic version fallback. /api/version prefers `git describe --tags` at
# request time; this constant is only used when neither git nor the Railway
# env var RAILWAY_GIT_COMMIT_SHA is available. Bump when cutting a release.
__version__ = "0.11.0"

# Structured logging — configure before any other import that might
# emit via the logging module, so all records get the JSON envelope.
import logging  # noqa: E402
import logging_setup  # noqa: E402
logging_setup.init()
log = logging.getLogger(__name__)

# Round-11 expansion item 18: Sentry observability — initialized as
# early as possible so subsequent imports' exceptions get captured.
# No-op if SENTRY_DSN env var isn't set (paper users don't need it).
try:
    import observability  # noqa: F401  side-effect init
    observability.install_exception_hook()
except Exception as _obs_err:
    log.warning("observability init skipped", extra={"error": str(_obs_err)})

import base64
import glob
import hmac
import html as _html
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
        log.info("credential cipher distribution", extra={"counts": _fmt_counts})
        _need_upgrade = (_fmt_counts.get("PLAIN", 0)
                         + _fmt_counts.get("ENC", 0)
                         + _fmt_counts.get("ENCv2", 0))
        if _need_upgrade == 0:
            log.info("all users on ENCv3 (HKDF); older decrypt paths safe to retire")
        else:
            log.info("users pending ENCv3 upgrade on next login",
                     extra={"pending": _need_upgrade})
except Exception as _diag_err:
    # Diagnostic only — don't block boot, but surface to Sentry so we notice
    # if the count function ever starts throwing silently.
    try:
        observability.capture_exception(_diag_err, source="auth.cipher_distribution")
    except Exception:
        pass

# Basic auth credentials (set via env vars on Railway, or .env for local dev)
# Kept only for backward-compat bootstrap; actual auth now goes through auth.py
AUTH_USER = os.environ.get("DASHBOARD_USER", "")
AUTH_PASS = os.environ.get("DASHBOARD_PASS", "")

# Round-11: record process-start time for /healthz warmup grace period.
# UptimeRobot was false-positive flagging the monitor "Down" during Railway
# redeploys because the scheduler thread hadn't logged its first entry yet
# (~30-90s window). /healthz now returns 200 in that warmup window.
_PROCESS_START_TIME = time.time()

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
    log.critical("DATA_DIR not writable — check volume mount",
                 extra={"data_dir": DATA_DIR})
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
        observability.capture_exception(e, source="alpaca_request", method=method)
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
            observability.capture_exception(e, source="alpaca_get_cached")
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

def _resolve_user_paths(user_id, mode="paper"):
    """Return (user_dir, strats_dir). Falls back to shared DATA_DIR only
    when user_id is None (legacy env-var mode).

    Round-46: `mode` honors the caller's session_mode so /api/data and
    other endpoints read from users/<id>/live/ when the session is live.
    Pre-round-46 callers that didn't pass mode default to 'paper' which
    is exact backward compatibility."""
    if user_id is not None:
        try:
            import auth as _auth
            user_dir = _auth.user_data_dir(user_id, mode=mode)
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
    """Delegates to per_user_isolation.load_with_shared_fallback, which
    was extracted in round-15 so the CRITICAL MIGRATION RULE (only
    user_id==1 may fall back to shared DATA_DIR) can be unit-tested
    without dragging in sqlite / auth / observability init.

    Kept as a thin wrapper so the long-standing call sites across the
    handlers continue to work unchanged.
    """
    from per_user_isolation import load_with_shared_fallback
    return load_with_shared_fallback(
        user_path, shared_path, user_id,
        capture_exc=observability.capture_exception,
    )


def _mark_auto_deployed(positions, strats_dir):
    """Round-11: annotate each position with `_auto_deployed` AND
    `_strategy` so the dashboard can show the correct AUTO/MANUAL
    badge plus which strategy the bot is actively running on this
    position. A position is considered AUTO if the auto-deployer
    (or wheel) dropped a strategy file for its symbol. For option
    positions (asset_class == us_option) we parse the OCC-format
    underlying from the symbol prefix and look for
    wheel_<underlying>.json.

    Sets on each position dict:
      _auto_deployed : bool  (True if any strategy file matches)
      _strategy      : str   (e.g. "trailing_stop", "wheel", "breakout",
                              "mean_reversion", "pead", "short"; "" if
                              no match)
    """
    if not isinstance(positions, list) or not positions or not strats_dir:
        return positions
    try:
        existing = set(os.listdir(strats_dir)) if os.path.isdir(strats_dir) else set()
    except OSError:
        existing = set()
    # Build a quick map: underlying-symbol -> strategy name.
    # Filenames look like: trailing_stop_SOXL.json, wheel_HIMS.json,
    # breakout_AAPL.json, mean_reversion_XYZ.json, pead_NVDA.json,
    # short_ABC.json. rsplit on "_" once isolates the SYMBOL from the
    # multi-word strategy prefix.
    symbol_to_strategy = {}
    for fname in existing:
        if not fname.endswith(".json"):
            continue
        stem = fname[:-5]  # strip .json
        if "_" not in stem:
            continue
        strat, _, sym = stem.rpartition("_")
        if not sym or not sym.isalnum() or not strat:
            continue
        sym_u = sym.upper()
        # If multiple strategies somehow share one symbol (shouldn't
        # happen in practice, but: breakout_ + trailing_stop_ side-by-
        # side during migration), prefer wheel > trailing_stop > the
        # actual entry strategy. That surfaces the "what's actively
        # managing this position right now" answer, not stale files.
        priority = {"wheel": 3, "trailing_stop": 2}.get(strat, 1)
        existing_prio = {"wheel": 3, "trailing_stop": 2}.get(symbol_to_strategy.get(sym_u), 0)
        if priority >= existing_prio:
            symbol_to_strategy[sym_u] = strat
    for p in positions:
        try:
            sym = (p.get("symbol") or "").upper()
            asset_class = (p.get("asset_class") or "").lower()
            if asset_class == "us_option":
                # OCC option symbol: <UNDERLYING><YYMMDD><C|P><STRIKE8>
                # Underlying is the leading letters (up to 6 chars before
                # the first digit run).
                m = re.match(r"^([A-Z]{1,6})\d", sym)
                lookup = m.group(1) if m else sym
            else:
                lookup = sym
            strat = symbol_to_strategy.get(lookup, "")
            p["_auto_deployed"] = bool(strat)
            p["_strategy"] = strat
        except Exception as e:
            observability.capture_exception(
                e, source="_mark_auto_deployed",
                symbol=(p.get("symbol") if isinstance(p, dict) else ""),
            )
            p["_auto_deployed"] = False
            p["_strategy"] = ""
    return positions


def _annotate_sector(positions):
    """Round-35 thin wrapper — real logic in position_sector.py so
    tests don't have to reload server (which drags auth + sqlite)."""
    from position_sector import annotate_sector
    return annotate_sector(positions)


def _scan_todays_closes(user_dir, user_id):
    """Round-34 thin wrapper — the real logic lives in todays_closes.py
    so tests don't have to reload server (which drags auth + sqlite)."""
    journal = _load_with_shared_fallback(
        os.path.join(user_dir, "trade_journal.json"),
        os.path.join(DATA_DIR, "trade_journal.json"),
        user_id,
    ) or {}
    from todays_closes import scan_todays_closes
    return scan_todays_closes(journal)


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
        "todays_closes": _scan_todays_closes(user_dir, user_id),
    }


def get_dashboard_data(api_endpoint=None, api_headers=None, user_id=None, mode="paper"):
    """Assemble the dashboard payload for a user.

    Pipeline:
      1. Resolve user paths (enforces multi-user isolation)
      2. Load screener picks from dashboard_data.json (per-user, never shared
         to non-admin users)
      3. Fetch live Alpaca state (account/positions/orders)
      4. Layer in strategy + guardrail overlays

    Falls back to a build-from-scratch response if no dashboard_data.json
    exists yet (new user before first screener run).

    Round-46: `mode` scopes reads to the caller's session tree. Without
    this, a live-view session was reading paper's state files while the
    Alpaca endpoint correctly pointed at live — so the dashboard showed
    paper's positions with live account data in the header (mismatch).
    """
    user_dir, strats_dir = _resolve_user_paths(user_id, mode=mode)

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

    # Annotate AUTO/MANUAL on each position based on strategy-file presence.
    positions = _mark_auto_deployed(positions, strats_dir)
    # Round-35: annotate sector + underlying so the Position Correlation
    # panel can render a real sector breakdown with $ allocation.
    positions = _annotate_sector(positions)

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
                log.warning("auth GC failed", extra={"gc_fn": _fn, "error": str(_e)})
                observability.capture_exception(_e, source="auth.gc", gc_fn=_fn)

# Signup invite code gate. Set SIGNUP_DISABLED=1 to block all new signups.
# Otherwise a `provided_code` must validate EITHER:
#   (a) the SIGNUP_INVITE_CODE env var (a single shared code for admin
#       convenience), OR
#   (b) a single-use DB-backed invite (auth.check_invite — round-26).
# When neither is set and SIGNUP_DISABLED isn't 1, signup is open.
def signup_allowed(provided_code):
    if os.environ.get("SIGNUP_DISABLED") == "1":
        return False, "Signup is disabled on this deployment."
    code = (provided_code or "").strip()
    env_expected = os.environ.get("SIGNUP_INVITE_CODE", "").strip()
    if env_expected and code and hmac.compare_digest(code, env_expected):
        return True, None
    # Round-26: try the DB invite table. If the code matches an unused,
    # unexpired invite, accept.
    if code:
        try:
            ok, _reason = auth.check_invite(code)
            if ok:
                return True, None
        except Exception:
            pass  # DB error shouldn't block signup when env code is set
    # If SIGNUP_INVITE_CODE is set but the provided code doesn't match
    # either path, reject.
    if env_expected:
        return False, "Invalid invite code."
    # No env gate + no DB invite match — fall through to open signup.
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
        """Route HTTP access logs through the structured logger. BaseHTTPRequestHandler's
        default goes to stderr with no timestamp; ours lands in the JSON stream."""
        log.info("http", extra={"access": args[0] if args else ""})

    # Instance defaults populated by check_auth()
    current_user = None
    user_api_key = ""
    user_api_secret = ""
    user_api_endpoint = ""
    user_data_endpoint = ""
    # Round-45 dual-mode: which state tree this request is reading from.
    # Defaults to 'paper' (unchanged behavior for everyone pre-round-45).
    # Set to 'live' if the session has opted into the live view.
    session_mode = "paper"
    # Session token for this request (populated by get_current_user) — used
    # by the /api/switch-mode endpoint to update mode without re-auth.
    _session_token = None

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
                # Round-45: stash the token so the mode-switch endpoint
                # can update the session without another cookie round-trip
                self._session_token = session_token
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
            # Round-45: honor the session's view mode when loading creds +
            # data dir. If the session is paper (default) this matches
            # pre-round-45 behavior exactly. If live, we load live keys +
            # scope subsequent data-dir lookups to users/<id>/live/.
            self.session_mode = user.get("session_mode") or "paper"
            # If the session is 'live' but the user hasn't configured live
            # keys yet, fall back to paper silently so the dashboard still
            # loads (Settings tab lets them add live keys from there).
            if self.session_mode == "live" and not (
                user.get("alpaca_live_key_encrypted")
                and user.get("alpaca_live_secret_encrypted")
            ):
                self.session_mode = "paper"
            # Load per-user Alpaca credentials (decrypted) for this mode.
            creds = auth.get_user_alpaca_creds(user["id"], mode=self.session_mode)
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

    def build_scoped_user_dict(self, mode=None):
        """Round-45: build a cloud_scheduler-compatible user dict scoped
        to a specific trading mode. Use this instead of inline dict
        literals throughout the handlers. Defaults to the request's
        session_mode so existing code paths get the right tree
        automatically.

        Returns a dict with `_mode`, `_data_dir`, `_strategies_dir`,
        `_api_key`, `_api_secret`, `_api_endpoint`, `_data_endpoint`,
        plus `id` and `username` for downstream logging.
        """
        mode = mode or self.session_mode or "paper"
        creds = auth.get_user_alpaca_creds(self.current_user["id"], mode=mode) or {}
        udir = auth.user_data_dir(self.current_user["id"], mode=mode)
        return {
            "id": self.current_user["id"],
            "username": self.current_user["username"],
            "_mode": mode,
            "_api_key": creds.get("key", ""),
            "_api_secret": creds.get("secret", ""),
            "_api_endpoint": creds.get("endpoint") or API_ENDPOINT,
            "_data_endpoint": creds.get("data_endpoint") or DATA_ENDPOINT,
            "_ntfy_topic": self.current_user.get("ntfy_topic", "")
                           or f"alpaca-bot-{self.current_user['username'].lower()}",
            "_data_dir": udir,
            "_strategies_dir": os.path.join(udir, "strategies"),
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
        """Return the per-user data directory, or DATA_DIR for legacy env-mode.

        Round-45: respects the session's current view mode, so a request
        that sets session_mode='live' reads/writes under users/<id>/live/.
        """
        if self.current_user and self.current_user.get("id") is not None:
            try:
                return auth.user_data_dir(self.current_user["id"],
                                           mode=self.session_mode or "paper")
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
                    log.info("migration: seeded strategies dir for bootstrap admin")
            except Exception as e:
                log.warning("migration: strategies seed failed", extra={"error": str(e)})
                observability.capture_exception(e, source="_user_strategies_dir.seed")
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
                    log.info("migration: copied file to bootstrap admin user dir",
                             extra={"filename": filename})
                except Exception as e:
                    log.warning("migration: file copy failed",
                                extra={"filename": filename, "error": str(e)})
                    observability.capture_exception(
                        e, source="_user_file.migrate", filename=filename,
                    )
        return user_path

    def _send_error_safe(self, exc, status=500, context=""):
        """Log full exception detail server-side and return a generic
        response to the client. Prevents leaking stack traces, DB paths,
        or secret fragments through API error messages.
        Returns a short correlation ID the user can share to help debug.

        Also forwards to Sentry via observability.capture_exception so the
        error shows up in the error tracker alongside the stdout log.
        """
        import uuid
        correlation_id = uuid.uuid4().hex[:12]
        label = context or "internal"
        try:
            log.error(
                "handler error",
                extra={
                    "correlation_id": correlation_id,
                    "context": label,
                    "exc_type": type(exc).__name__,
                    "exc_msg": str(exc),
                },
            )
        except Exception:
            pass
        try:
            observability.capture_exception(
                exc, context=label, correlation_id=correlation_id,
            )
        except Exception:
            pass
        self.send_json({
            "error": "Internal error. Please retry.",
            "correlation_id": correlation_id,
        }, status)

    def _load_full_journal(self, filename="trade_journal.json"):
        """Load the user's trade journal MERGED with its archive sibling.

        Introduced with the round-12 trade-journal trimming PR. Callers that
        need lifetime history (tax lots, /api/tax-report, trades.csv) must
        use this helper so trimming can't silently erase old trades from
        their view. Callers that only need recent activity (heatmap,
        scheduler tick) can keep using plain `load_json` on the live file.

        Returns a journal dict with `trades` = archive + live. Live-only keys
        (daily_snapshots, custom metadata) are preserved from the live file.
        """
        live_path = self._user_file(filename)
        live = load_json(live_path) or {}
        try:
            import trade_journal as _tj
            all_trades = _tj.load_all_trades(live_path)
            if all_trades:
                live = {**live, "trades": all_trades}
        except Exception as e:
            observability.capture_exception(e, source="_load_full_journal")
        return live

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
        log.warning("alpaca upstream error",
                    extra={"http_code": http_code, "body": body})
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
            log.warning("user_api_get failed",
                        extra={"exc_type": type(e).__name__, "exc_msg": str(e)})
            observability.capture_exception(e, source="user_api_get")
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
            log.warning("user_api_post failed",
                        extra={"exc_type": type(e).__name__, "exc_msg": str(e)})
            observability.capture_exception(e, source="user_api_post")
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
            log.warning("user_api_delete failed",
                        extra={"exc_type": type(e).__name__, "exc_msg": str(e)})
            observability.capture_exception(e, source="user_api_delete")
            return {"error": "Request failed"}

    def _cors_origin(self):
        """Return the allowed CORS origin (same-origin by default, configurable via env)."""
        return os.environ.get("CORS_ORIGIN", "")

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        # HSTS — tell the browser to only talk HTTPS to us. Harmless when
        # we're behind Railway's TLS-terminating proxy; actively protects
        # against SSL-stripping if users ever hit an http:// link.
        self.send_header("Strict-Transport-Security",
                         "max-age=31536000; includeSubDomains")
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
        self.send_header("Strict-Transport-Security",
                         "max-age=31536000; includeSubDomains")
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

    def handle_csv_export(self, path):
        """Generate a CSV for the given export route + stream it as a
        download. Rows depend on path:
          /api/export/positions.csv  — current open positions (live from Alpaca)
          /api/export/orders.csv     — current open orders
          /api/export/trades.csv     — full trade journal (closed + open)
          /api/export/picks.csv      — latest screener top-50
          /api/export/tax-lots.csv   — FIFO tax lots (closed only)
        """
        import io, csv
        if not self.current_user:
            return self.send_json({"error": "Auth required"}, 401)
        buf = io.StringIO()
        w = csv.writer(buf)
        filename = path.rsplit("/", 1)[-1]
        try:
            if path == "/api/export/positions.csv":
                positions = self.user_api_get(f"{self.user_api_endpoint}/positions")
                positions = positions if isinstance(positions, list) else []
                w.writerow(["Symbol", "Qty", "AvgEntry", "Current", "MarketValue",
                            "UnrealizedPL", "UnrealizedPLPct", "AssetClass"])
                for p in positions:
                    w.writerow([
                        p.get("symbol", ""), p.get("qty", ""),
                        p.get("avg_entry_price", ""), p.get("current_price", ""),
                        p.get("market_value", ""), p.get("unrealized_pl", ""),
                        p.get("unrealized_plpc", ""), p.get("asset_class", ""),
                    ])
            elif path == "/api/export/orders.csv":
                orders = self.user_api_get(f"{self.user_api_endpoint}/orders?status=open")
                orders = orders if isinstance(orders, list) else []
                w.writerow(["Symbol", "Side", "Type", "Qty", "LimitPrice",
                            "StopPrice", "Status", "CreatedAt"])
                for o in orders:
                    w.writerow([
                        o.get("symbol", ""), o.get("side", ""),
                        o.get("type", ""), o.get("qty", ""),
                        o.get("limit_price", ""), o.get("stop_price", ""),
                        o.get("status", ""), o.get("created_at", ""),
                    ])
            elif path == "/api/export/trades.csv":
                # Lifetime export — include archived trades.
                journal = self._load_full_journal("trade_journal.json")
                trades = journal.get("trades", [])
                w.writerow(["Timestamp", "Symbol", "Side", "Qty", "Price",
                            "Strategy", "Status", "PnL", "Reason"])
                for t in trades:
                    w.writerow([
                        t.get("timestamp", ""), t.get("symbol", ""),
                        t.get("side", ""), t.get("qty", ""), t.get("price", ""),
                        t.get("strategy", ""), t.get("status", ""),
                        t.get("pnl", ""), t.get("reason", ""),
                    ])
            elif path == "/api/export/picks.csv":
                data = get_dashboard_data(
                    api_endpoint=self.user_api_endpoint,
                    api_headers=self.user_headers(),
                    user_id=self.current_user.get("id"),
                )
                picks = (data or {}).get("picks", [])[:50]
                w.writerow(["Rank", "Symbol", "Price", "Volume", "Volatility",
                            "BestStrategy", "BestScore", "QualityTier", "RS",
                            "SectorETF", "HVRank"])
                for i, p in enumerate(picks, 1):
                    w.writerow([
                        i, p.get("symbol", ""), p.get("price", ""),
                        p.get("daily_volume", ""), p.get("volatility", ""),
                        p.get("best_strategy", ""), p.get("best_score", ""),
                        p.get("quality_tier", ""), p.get("rs_composite", ""),
                        p.get("sector_etf", ""), p.get("hv_rank", ""),
                    ])
            elif path == "/api/export/tax-lots.csv":
                try:
                    import tax_lots
                    # Tax lots must include archived trades — Form 8949 needs
                    # every disposition since inception.
                    journal = self._load_full_journal("trade_journal.json")
                    report = tax_lots.compute_tax_lots(journal)
                    w.writerow(["Symbol", "Qty", "AcquiredDate", "SoldDate",
                                "HoldingDays", "Term", "CostBasis", "Proceeds",
                                "GainLoss", "Strategy"])
                    for lot in report.get("lots", []):
                        w.writerow([
                            lot["symbol"], lot["qty"], lot["acquired_date"],
                            lot["sold_date"], lot["holding_days"], lot["term"],
                            lot["cost_basis"], lot["proceeds"], lot["gain_loss"],
                            lot.get("strategy", ""),
                        ])
                except Exception as e:
                    observability.capture_exception(e, source="csv-export.tax-lots")
                    w.writerow(["error", str(e)])
        except Exception as e:
            self._send_error_safe(e, 500, "csv-export")
            return
        csv_text = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(csv_text.encode("utf-8"))

    def serve_track_record(self, user_id_str):
        """Public read-only track-record page. Round-11 LIVE-BATCH 3.
        Only renders if the user has opted in (track_record_public=1).
        NO auth required — this is intentionally public. NO credentials,
        positions, or PII are included — only aggregate performance stats.
        """
        # Load template
        try:
            tpl_path = os.path.join(BASE_DIR, "templates", "track_record.html")
            with open(tpl_path) as f:
                tpl = f.read()
        except Exception as e:
            observability.capture_exception(e, source="track_record.template")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Template error: {e}".encode())
            return
        # Validate user ID
        try:
            uid = int(user_id_str.strip("/"))
        except (ValueError, TypeError):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        # Load user; abort if not opted in
        user = auth.get_user_by_id(uid)
        if not user or not user.get("track_record_public"):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Track record not available")
            return
        # Load scorecard + journal for this user
        user_dir = auth.user_data_dir(uid)
        scorecard = load_json(os.path.join(user_dir, "scorecard.json")) or {}
        journal = load_json(os.path.join(user_dir, "trade_journal.json")) or {}
        # Build equity curve from daily snapshots
        snapshots = journal.get("daily_snapshots", []) or []
        equity_series = [
            {"date": s.get("date", ""),
             "equity": float(s.get("portfolio_value") or s.get("equity") or 0)}
            for s in snapshots if s.get("date")
        ]
        # Basic stats
        total_pnl = float(scorecard.get("total_pnl") or 0)
        total_return_pct = scorecard.get("total_return_pct")
        if total_return_pct is None:
            total_return_pct = scorecard.get("daily_pnl_pct") or 0
        try:
            total_return_pct = float(total_return_pct)
        except (ValueError, TypeError):
            total_return_pct = 0
        win_rate = scorecard.get("win_rate")
        total_trades = int(scorecard.get("total_trades") or 0)
        sharpe = scorecard.get("sharpe_ratio") or 0
        max_dd = scorecard.get("max_drawdown_pct") or 0
        profit_factor = scorecard.get("profit_factor") or 0
        days_tracked = len(snapshots)
        # Strategy breakdown rows
        strat_rows_html = ""
        breakdown = scorecard.get("strategy_breakdown", {}) or {}
        for strat, s in sorted(breakdown.items(),
                                key=lambda kv: float(kv[1].get("pnl", 0) or 0),
                                reverse=True):
            trades_n = int(s.get("trades", 0) or 0)
            if trades_n == 0:
                continue
            wins_n = int(s.get("wins", 0) or 0)
            pnl = float(s.get("pnl", 0) or 0)
            avg_pnl = pnl / trades_n if trades_n else 0
            wr = (wins_n / trades_n * 100) if trades_n else 0
            pnl_cls = "positive" if pnl > 0 else "negative" if pnl < 0 else "neutral"
            # Round-14: html.escape strategy name. _normalize_strategy_name
            # restricts to [a-z0-9_] so practical XSS is unlikely, but
            # defence in depth against future producers (e.g. user-supplied
            # custom strategy names) is the right pattern.
            from html import escape as _html_escape
            strat_rows_html += (
                f"<tr>"
                f"<td><strong>{_html_escape(str(strat))}</strong></td>"
                f"<td>{trades_n} ({wins_n}W)</td>"
                f"<td>{wr:.1f}%</td>"
                f"<td>${avg_pnl:,.0f}</td>"
                f"<td class='{pnl_cls}'>${pnl:,.0f}</td>"
                f"</tr>"
            )
        if not strat_rows_html:
            strat_rows_html = ('<tr><td colspan="5" style="text-align:center;'
                                'color:var(--text-dim);padding:20px">'
                                'No closed trades yet</td></tr>')
        # Render
        import json as _json
        mode_badge = (
            '<span class="badge mode live">🔴 LIVE</span>'
            if user.get("live_mode")
            else '<span class="badge mode">📝 Paper</span>'
        )
        started = user.get("created_at", "")[:10] if user.get("created_at") else "—"
        try:
            pnl_cls = "positive" if total_pnl > 0 else "negative" if total_pnl < 0 else "neutral"
        except Exception as e:
            observability.capture_exception(e, source="track_record.pnl_cls")
            pnl_cls = "neutral"
        ret_cls = "positive" if total_return_pct > 0 else "negative" if total_return_pct < 0 else "neutral"
        # Round-41 XSS hardening: track_record.html is public (shareable URL).
        # Escape username before interpolating — usernames are validated at
        # signup but defense-in-depth matters when rendering into HTML.
        rendered = (tpl
            .replace("{{USERNAME}}", _html.escape(user.get("username", "User")))
            .replace("{{MODE_BADGE}}", mode_badge)
            .replace("{{STARTED_DATE}}", started)
            .replace("{{TOTAL_RETURN_PCT}}", f"{total_return_pct:+.2f}%")
            .replace("{{TOTAL_PNL}}", f"${total_pnl:,.0f}")
            .replace("{{WIN_RATE}}", f"{win_rate:.1f}%" if win_rate is not None else "—")
            .replace("{{TOTAL_TRADES}}", str(total_trades))
            .replace("{{SHARPE}}", f"{float(sharpe):.2f}" if sharpe else "—")
            .replace("{{MAX_DRAWDOWN}}", f"{float(max_dd):.1f}%" if max_dd else "—")
            .replace("{{PROFIT_FACTOR}}", f"{float(profit_factor):.2f}" if profit_factor else "—")
            .replace("{{DAYS_TRACKED}}", str(days_tracked))
            .replace("{{RETURN_CLASS}}", ret_cls)
            .replace("{{PNL_CLASS}}", pnl_cls)
            .replace("{{STRATEGY_ROWS}}", strat_rows_html)
            .replace("{{EQUITY_JSON}}", _json.dumps(equity_series))
            .replace("{{GENERATED_AT}}", now_et().strftime("%Y-%m-%d %I:%M %p ET"))
        )
        # Public HTML — no auth but still security headers + CSP
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "public, max-age=300")  # 5-min cache OK
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors 'none';"
        )
        self.end_headers()
        self.wfile.write(rendered.encode("utf-8"))

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

    def do_HEAD(self):
        """HEAD request support. Python's stdlib http.server returns 501 Not
        Implemented for HEAD by default, which broke UptimeRobot (it uses
        HEAD for health checks to save bandwidth). We forward HEAD to GET
        logic but suppress the body — standard HTTP HEAD semantics."""
        # Mark the response as HEAD so any downstream `self.wfile.write`
        # calls in do_GET become no-ops via the _head_mode flag we check
        # before writing. Simplest correct implementation: run the normal
        # GET path and let the client discard the body (wastes a bit of
        # bandwidth but guarantees headers match exactly what GET returns).
        self.do_GET()

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

                # Round-11 fix: startup grace period. Railway redeploys
                # briefly return 503 between "new container started" and
                # "scheduler thread logged its first entry" — a ~30-90s
                # window. UptimeRobot's 5-min polls sometimes catch this
                # gap and flag the monitor as Down. Treat the first 120s
                # after process start as "warming up" — return 200 if
                # thread is alive even if log_count=0 during that window.
                uptime_sec = int(time.time() - _PROCESS_START_TIME)
                warming_up = uptime_sec < 120
                if warming_up and thread_alive and last_log_count == 0:
                    healthy = True  # grace period — scheduler still starting
                else:
                    healthy = thread_alive and last_log_count > 0 and not log_stale

                # Deep-health probes: DB + Alpaca connectivity. Keep
                # each probe very cheap + fast-fail so /healthz still
                # returns in <2s when any backing service is flapping.
                # Failures DEGRADE (not fail) the response so UptimeRobot
                # doesn't page for transient Alpaca / DB hiccups —
                # scheduler-alive + fresh logs is still the primary
                # signal.
                db_ok = None
                try:
                    import sqlite3 as _sq
                    import auth as _a
                    _c = _sq.connect(_a.DB_PATH, timeout=1.0)
                    _c.execute("SELECT 1").fetchone()
                    _c.close()
                    db_ok = True
                except Exception:
                    db_ok = False

                alpaca_ok = None
                # Only probe Alpaca during market hours — midnight probes
                # succeed fine but add no signal; skip to keep 429 budget
                # for trade traffic.
                try:
                    from datetime import datetime as _dt
                    from zoneinfo import ZoneInfo
                    _now_et = _dt.now(ZoneInfo("America/New_York"))
                    _mkt_window = (
                        _now_et.weekday() < 5
                        and 9 <= _now_et.hour < 16
                    )
                    if _mkt_window and API_KEY and API_SECRET:
                        _req = urllib.request.Request(
                            (API_ENDPOINT or "https://paper-api.alpaca.markets/v2") + "/clock",
                            headers={
                                "APCA-API-KEY-ID": API_KEY,
                                "APCA-API-SECRET-KEY": API_SECRET,
                            },
                        )
                        with urllib.request.urlopen(_req, timeout=2.0) as _r:
                            alpaca_ok = _r.status == 200
                except Exception:
                    alpaca_ok = False

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
                    "uptime_sec": uptime_sec,
                    "warming_up": warming_up,
                    "db_ok": db_ok,
                    "alpaca_ok": alpaca_ok,
                }).encode())
            except Exception as e:
                observability.capture_exception(e, source="healthz")
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
            # Version sources, in priority order:
            #   1. `git describe --tags --always` — readable "v1.2.3-N-gSHA" if
            #      tags exist, otherwise the short SHA.
            #   2. RAILWAY_GIT_COMMIT_SHA — env var Railway injects during
            #      build. Used when the container doesn't ship the .git dir.
            #   3. __version__ constant — final fallback when neither git nor
            #      Railway env vars are available (e.g. packaged build).
            info = {"bot_version": __version__}
            try:
                import subprocess as _sp
                r = _sp.run(
                    ["git", "describe", "--tags", "--always", "--dirty"],
                    cwd=BASE_DIR, capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0 and r.stdout.strip():
                    info["bot_version"] = r.stdout.strip()
                r2 = _sp.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=BASE_DIR, capture_output=True, text=True, timeout=2,
                )
                if r2.returncode == 0:
                    info["commit"] = r2.stdout.strip()[:12]
            except Exception:
                pass
            # Railway env-var fallback when .git isn't in the container
            if "commit" not in info:
                rw_sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()
                if rw_sha:
                    info["commit"] = rw_sha[:12]
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
        # Round-11 LIVE-BATCH 3: public track-record page, opt-in per user.
        # Route: /track-record/<user_id>. No auth. Only renders if
        # user.track_record_public = 1. Shows equity curve + stats only,
        # zero credentials, zero positions, zero PII.
        if path.startswith("/track-record/"):
            self.serve_track_record(path[len("/track-record/"):])
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
                "user_id": u.get("id"),
                "username": u.get("username", ""),
                "email": u.get("email", ""),
                "is_admin": bool(u.get("is_admin", 0)),
                "alpaca_endpoint": u.get("alpaca_endpoint", ""),
                "alpaca_data_endpoint": u.get("alpaca_data_endpoint", ""),
                "ntfy_topic": u.get("ntfy_topic", ""),
                "notification_email": u.get("notification_email", ""),
                "has_alpaca_key": bool(self.user_api_key),
                # Round-11 live-trading fields
                "has_paper_keys": bool(u.get("alpaca_key_encrypted") and u.get("alpaca_secret_encrypted")),
                "has_live_keys": bool(u.get("alpaca_live_key_encrypted") and u.get("alpaca_live_secret_encrypted")),
                "live_mode": bool(u.get("live_mode")),
                "live_enabled_at": u.get("live_enabled_at"),
                "live_max_position_dollars": float(u.get("live_max_position_dollars") or 500),
                "track_record_public": bool(u.get("track_record_public")),
                "scorecard_email_enabled": bool(u.get("scorecard_email_enabled")),
            })

        elif path == "/api/calibration":
            # Round-50: portfolio auto-calibration summary. Fetches
            # the caller's /v2/account, detects tier, merges user
            # overrides from guardrails.json, returns a JSON summary
            # the dashboard renders in the new Account Calibration tab.
            try:
                import portfolio_calibration as pc
                # Fetch Alpaca /account for THIS user's mode
                account = self.user_api_get(f"{self.user_api_endpoint}/account")
                tier = pc.detect_tier(account)
                if tier is None:
                    return self.send_json({
                        "detected": False,
                        "reason": "Equity below $500 or Alpaca /account returned no data",
                        "raw_account": account,
                    })
                # Pull the caller's guardrails overrides
                import json as _json
                gr_path = os.path.join(self._user_dir(), "guardrails.json")
                guardrails = {}
                if os.path.exists(gr_path):
                    try:
                        with open(gr_path) as f:
                            guardrails = _json.load(f) or {}
                    except (OSError, ValueError):
                        guardrails = {}
                merged = pc.apply_user_overrides(tier, guardrails)
                summary = pc.calibration_summary(merged)
                # Also include the detected state fields so the UI can show them
                summary["raw"] = {
                    "equity": merged.get("_detected_equity"),
                    "cash": merged.get("_detected_cash"),
                    "cash_withdrawable": merged.get("_detected_cash_withdrawable"),
                    "buying_power": merged.get("_detected_buying_power"),
                    "multiplier": merged.get("_detected_multiplier"),
                    "pattern_day_trader": merged.get("_detected_pattern_day_trader"),
                    "shorting_enabled": merged.get("_detected_shorting_enabled"),
                    "day_trades_remaining": merged.get("_detected_day_trades_remaining"),
                }
                self.send_json(summary)
            except Exception as e:
                self._send_error_safe(e, 500, "calibration")

        elif path == "/api/data":
            data = get_dashboard_data(
                api_endpoint=self.user_api_endpoint,
                api_headers=self.user_headers(),
                user_id=self.current_user.get("id") if self.current_user else None,
                # Round-46: respect session mode so live view reads from
                # users/<id>/live/ state tree.
                mode=self.session_mode or "paper",
            )
            # Round-45: expose the session's current mode + whether the
            # user has live keys configured so the dashboard can render
            # the mode-toggle button with the correct state.
            if isinstance(data, dict) and self.current_user:
                data["session_mode"] = self.session_mode or "paper"
                data["has_live_keys"] = bool(
                    self.current_user.get("alpaca_live_key_encrypted")
                    and self.current_user.get("alpaca_live_secret_encrypted")
                )
                data["live_parallel_enabled"] = bool(
                    self.current_user.get("live_parallel_enabled"))
            self.send_json(data)

        elif path == "/api/chart-bars":
            # Round-39: lightweight native charts (Tier B).
            # ?symbol=SOXL&days=60&timeframe=1Day
            # Returns {bars:[{t, o, h, l, c, v}, ...]} for Chart.js
            # plus overlay lines (entry + stop) derived from the user's
            # current position if they hold the symbol.
            import urllib.parse as _up
            _qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            q = _up.parse_qs(_qs)
            symbol = (q.get("symbol", [""])[0] or "").upper()
            try:
                days = int(q.get("days", ["60"])[0])
            except (TypeError, ValueError):
                days = 60
            # Clamp to reasonable bounds so an attacker can't ask for
            # decades of data.
            days = max(5, min(days, 365))
            timeframe = q.get("timeframe", ["1Day"])[0]
            if timeframe not in ("1Day", "1Hour", "15Min"):
                timeframe = "1Day"
            # Validate symbol — a-z, digits, dot (BF.B etc). OCC option
            # symbols contain digits but we still want them accepted
            # since users might want to chart the underlying.
            import re as _re_chart
            if not symbol or not _re_chart.match(r"^[A-Z0-9.]{1,20}$", symbol):
                self.send_json({"error": "invalid symbol"}, 400)
                return
            # For options: chart the underlying stock, not the contract
            _m = _re_chart.match(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$", symbol)
            chart_symbol = _m.group(1) if _m else symbol

            # Fetch bars from Alpaca (uses user's creds)
            from datetime import timedelta as _td
            end = now_et()
            start = end - _td(days=days + 5)  # slack for weekends/holidays
            # RFC-3339 UTC with 'Z' suffix — Alpaca is strict
            start_iso = start.astimezone(
                __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            end_iso = end.astimezone(
                __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (f"{self.user_data_endpoint}/stocks/{chart_symbol}/bars"
                   f"?timeframe={timeframe}&start={start_iso}&end={end_iso}"
                   f"&limit={days + 10}&feed=iex")
            result = self.user_api_get(url)
            if isinstance(result, dict) and "error" in result:
                self.send_json({"error": result["error"]}, 502)
                return
            bars_raw = (result or {}).get("bars", []) or []
            bars = [{
                "t": b.get("t"),
                "o": b.get("o"),
                "h": b.get("h"),
                "l": b.get("l"),
                "c": b.get("c"),
                "v": b.get("v"),
            } for b in bars_raw]

            # Overlay: if user holds this symbol, pull entry + current stop
            overlay = {"entry": None, "stop": None, "strategy": None}
            try:
                positions = self.user_api_get(
                    f"{self.user_api_endpoint}/positions") or []
                if isinstance(positions, list):
                    for p in positions:
                        sym_u = (p.get("symbol") or "").upper()
                        if sym_u == symbol or sym_u == chart_symbol:
                            try:
                                overlay["entry"] = float(p.get("avg_entry_price") or 0) or None
                            except (TypeError, ValueError):
                                overlay["entry"] = None
                            break
                # Stop price: scan open orders for a GTC sell-stop on this symbol
                orders = self.user_api_get(
                    f"{self.user_api_endpoint}/orders?status=open&limit=50") or []
                if isinstance(orders, list):
                    for o in orders:
                        if ((o.get("symbol") or "").upper() in (symbol, chart_symbol)
                                and o.get("order_type") == "stop"):
                            try:
                                overlay["stop"] = float(o.get("stop_price") or 0) or None
                            except (TypeError, ValueError):
                                overlay["stop"] = None
                            break
            except Exception:
                pass  # overlays are best-effort; never fail the chart
            self.send_json({
                "symbol": chart_symbol,
                "timeframe": timeframe,
                "bars": bars,
                "overlay": overlay,
            })
            return

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
                # Round-39: filter by caller's username so non-admins
                # don't see cross-user scheduler/screener/deploy events.
                #
                # Round-48 privacy fix: ADMINS are also filtered to their
                # own activity by default. Round-39 made `is_admin=True`
                # show the unfiltered log, which was the reason the
                # dashboard still showed `[godguruselfone]` entries on
                # Kbell0629's screen (Kbell0629 is the bootstrap admin).
                # Privacy-by-default: admins now see only their own
                # activity unless they explicitly pass `?all=1`. The
                # admin panel's "See all activity" drill-down uses that
                # query param.
                _cu = self.current_user or {}
                _qs = urllib.parse.urlparse(self.path).query or ""
                _params = urllib.parse.parse_qs(_qs)
                _see_all = _params.get("all", ["0"])[0] == "1"
                _show_unfiltered = bool(_cu.get("is_admin")) and _see_all
                self.send_json(get_scheduler_status(
                    filter_username=_cu.get("username"),
                    is_admin=_show_unfiltered,
                ))
            else:
                self.send_json({"running": False, "error": "Scheduler module not loaded"})

        elif path == "/api/tax-report":
            # Round-11 expansion: per-lot tax reporting (FIFO basis,
            # short/long-term split, wash-sale warnings). Pulls from
            # the user's trade journal.
            try:
                import tax_lots
                # Lifetime history required for accurate cost basis.
                journal = self._load_full_journal("trade_journal.json")
                qs = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(qs)
                method = (params.get("method") or ["FIFO"])[0].upper()
                if method not in ("FIFO", "LIFO"):
                    method = "FIFO"
                report = tax_lots.compute_tax_lots(journal, basis_method=method)
                self.send_json(report)
            except Exception as e:
                self._send_error_safe(e, 500, "tax-report")

        elif path == "/api/tax-report.csv":
            # Form 8949 CSV download. Returns text/csv with Content-Disposition
            # so the browser triggers a save dialog.
            try:
                import tax_lots, io, csv
                # Lifetime history required for Form 8949.
                journal = self._load_full_journal("trade_journal.json")
                report = tax_lots.compute_tax_lots(journal, basis_method="FIFO")
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["Description (a)", "Date Acquired (b)", "Date Sold (c)",
                            "Proceeds (d)", "Cost Basis (e)", "Gain/Loss (h)",
                            "Term", "Strategy"])
                for term in ("short", "long"):
                    for lot in [l for l in report["lots"] if l["term"] == term]:
                        w.writerow([
                            f"{lot['qty']} sh {lot['symbol']}",
                            lot["acquired_date"], lot["sold_date"],
                            f"{lot['proceeds']:.2f}", f"{lot['cost_basis']:.2f}",
                            f"{lot['gain_loss']:.2f}",
                            "Long-term" if term == "long" else "Short-term",
                            lot.get("strategy", ""),
                        ])
                csv_text = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", 'attachment; filename="form_8949.csv"')
                self.end_headers()
                self.wfile.write(csv_text.encode("utf-8"))
            except Exception as e:
                self._send_error_safe(e, 500, "tax-report-csv")

        elif path == "/api/perf-attribution":
            # Round-11 expansion: dollars-per-strategy attribution.
            # Reads from the user's scorecard (already computed by
            # update_scorecard.py — strategy_breakdown has trades/wins/pnl).
            # This endpoint just shapes the data + adds derived fields.
            try:
                scorecard = load_json(self._user_file("scorecard.json")) or {}
                breakdown = scorecard.get("strategy_breakdown", {}) or {}
                rows = []
                for strat, s in breakdown.items():
                    trades = int(s.get("trades", 0) or 0)
                    wins = int(s.get("wins", 0) or 0)
                    pnl = float(s.get("pnl", 0) or 0)
                    if trades == 0 and pnl == 0:
                        continue  # skip empty buckets
                    win_rate = (wins / trades * 100) if trades > 0 else 0
                    avg_pnl = (pnl / trades) if trades > 0 else 0
                    rows.append({
                        "strategy": strat,
                        "trades": trades,
                        "wins": wins,
                        "losses": max(0, trades - wins),
                        "total_pnl": round(pnl, 2),
                        "avg_pnl": round(avg_pnl, 2),
                        "win_rate": round(win_rate, 1),
                    })
                rows.sort(key=lambda r: r["total_pnl"], reverse=True)
                total_pnl = sum(r["total_pnl"] for r in rows)
                best = rows[0] if rows else None
                worst = rows[-1] if rows and rows[-1]["total_pnl"] < 0 else None
                self.send_json({
                    "rows": rows,
                    "total_pnl": round(total_pnl, 2),
                    "best_strategy": best,
                    "worst_strategy": worst,
                    "scorecard_updated_at": scorecard.get("updated_at"),
                })
            except Exception as e:
                self._send_error_safe(e, 500, "perf-attribution")

        elif path in ("/api/export/positions.csv", "/api/export/orders.csv",
                        "/api/export/trades.csv", "/api/export/picks.csv",
                        "/api/export/tax-lots.csv"):
            # Round-11 LIVE-BATCH 4: CSV exports for every major table.
            # Each pulls from the user's current state + streams CSV with
            # proper Content-Disposition so the browser downloads instead
            # of rendering. Admin-auth only; same session cookie.
            self.handle_csv_export(path)
            return

        elif path == "/api/factor-health":
            # Round-11 visibility: exposes the state of the new factor
            # modules so the dashboard can render a "Factor Health"
            # panel. Reads cache files + live yfinance_budget stats.
            # Safe to call frequently — all state reads, no mutation.
            result = {"computed_at": now_et().isoformat()}
            # Market breadth
            try:
                import market_breadth as _mb
                breadth = _mb.get_breadth_pct(data_dir=DATA_DIR)
                result["breadth"] = breadth
            except Exception as e:
                observability.capture_exception(e, source="factor-health.breadth")
                result["breadth"] = {"error": str(e), "breadth_pct": None}
            # Sector rankings
            try:
                import factor_enrichment as _fe
                rankings = _fe.rank_sectors_by_momentum(data_dir=DATA_DIR) or {}
                # Shape as a ranked list for display
                ranked = sorted(
                    [{"etf": k, **v} for k, v in rankings.items()],
                    key=lambda x: x.get("rank", 99)
                )
                result["sectors"] = ranked
            except Exception as e:
                observability.capture_exception(e, source="factor-health.sectors")
                result["sectors"] = {"error": str(e)}
            # Cache ages (quality + iv_rank are per-symbol)
            try:
                import json as _json
                qpath = os.path.join(DATA_DIR, "quality_cache.json")
                if os.path.exists(qpath):
                    with open(qpath) as f:
                        qdata = _json.load(f) or {}
                    result["quality_cache_size"] = len(qdata)
                else:
                    result["quality_cache_size"] = 0
            except Exception:
                result["quality_cache_size"] = 0
            try:
                iv_path = os.path.join(DATA_DIR, "iv_rank_cache.json")
                if os.path.exists(iv_path):
                    with open(iv_path) as f:
                        iv_data = json.load(f) or {}
                    result["iv_rank_cache_size"] = len(iv_data)
                else:
                    result["iv_rank_cache_size"] = 0
            except Exception:
                result["iv_rank_cache_size"] = 0
            # yfinance budget stats (live, not cached)
            try:
                import yfinance_budget as _yb
                result["yfinance_budget"] = _yb.stats()
            except Exception as e:
                observability.capture_exception(e, source="factor-health.yfinance")
                result["yfinance_budget"] = {"error": str(e)}
            # Factor bypass flag (BATCH 3 — read from guardrails)
            try:
                guardrails = load_json(self._user_file("guardrails.json")) or {}
                result["factor_bypass"] = bool(guardrails.get("factor_bypass"))
            except Exception:
                result["factor_bypass"] = False
            self.send_json(result)

        elif path.startswith("/static/"):
            # Round-11 expansion item 16: serve PWA static assets
            # (manifest.json, service-worker.js, icons). Strict path
            # safety: only single-segment files allowed under /static/.
            import re as _re
            fname = path[len("/static/"):]
            if not _re.match(r"^[a-zA-Z0-9._-]+$", fname):
                self.send_json({"error": "invalid static path"}, 400)
                return
            local_path = os.path.join(BASE_DIR, "static", fname)
            if not os.path.exists(local_path):
                self.send_json({"error": "not found"}, 404)
                return
            content_type = "text/plain"
            if fname.endswith(".json"): content_type = "application/json"
            elif fname.endswith(".js"): content_type = "application/javascript"
            elif fname.endswith(".svg"): content_type = "image/svg+xml"
            elif fname.endswith(".png"): content_type = "image/png"
            elif fname.endswith(".css"): content_type = "text/css"
            try:
                with open(local_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=3600")
                # Service worker needs this to register at root scope
                if fname == "service-worker.js":
                    self.send_header("Service-Worker-Allowed", "/")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                observability.capture_exception(e, source="static", path=fname)
                self.send_json({"error": f"read failed: {e}"}, 500)
            return

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
                observability.capture_exception(e, source="readme")
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
            #
            # Round-11 fix: ONLY fire the overlay on actual trading days.
            # On weekends/holidays, Alpaca's `last_equity` is still the
            # prior trading day's pre-open value (it only rolls over at
            # market open), so `portfolio_value - last_equity` returns
            # the most-recent trading day's entire gain again — getting
            # double-counted as a fake weekend "day" in the heatmap.
            try:
                today_str = now_et().strftime("%Y-%m-%d")
                # Weekend guard: Saturday (5) or Sunday (6) is never a
                # trading day.
                _is_weekday = now_et().weekday() < 5
                # Holiday / half-day check via Alpaca's own clock —
                # is_open during market hours is the cleanest signal.
                # When market is closed mid-week (holiday) we still want
                # to allow the overlay (today's snapshot is valid) BUT
                # only if Alpaca's calendar agrees this is a trading day.
                _is_trading_day = _is_weekday
                if _is_trading_day:
                    try:
                        _cal = self.user_api_get(f"{self.user_api_endpoint}/calendar?start={today_str}&end={today_str}")
                        if isinstance(_cal, list):
                            _is_trading_day = bool(_cal)  # empty list = holiday
                    except Exception:
                        pass  # fall through to weekday guard only
                if _is_trading_day:
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
                log.warning("heatmap live-today overlay failed", extra={"error": str(_e)})
                observability.capture_exception(_e, source="heatmap.live_overlay")

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
                # Round-45: respects session mode so live-view users see
                # live wheels, paper-view users see paper wheels.
                if self.current_user:
                    user = self.build_scoped_user_dict()
                else:
                    user = {"_data_dir": DATA_DIR,
                            "_strategies_dir": os.path.join(DATA_DIR, "strategies"),
                            "_api_key": "", "_api_secret": "",
                            "_api_endpoint": API_ENDPOINT, "_data_endpoint": DATA_ENDPOINT}
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

        elif path == "/api/news-alerts":
            # Round-24: surface the news_alerts.json written by the
            # Alpaca real-time news websocket (started in round-23).
            # Returns alerts from the last N minutes (default 60) so the
            # dashboard can render a 🚨 Breaking line on pick cards +
            # a dedicated breaking-news section. Empty list on error or
            # when the file doesn't exist yet (websocket hasn't received
            # a scoreable alert).
            if not self.current_user:
                return self.send_json({"error": "Not authenticated"}, 401)
            try:
                import news_websocket
                # Round-45: scope to the session's view mode
                udir = auth.user_data_dir(self.current_user["id"],
                                            mode=self.session_mode or "paper")
                minutes = 60
                try:
                    qs = urllib.parse.urlparse(self.path).query or ""
                    if "minutes=" in qs:
                        minutes = int(qs.split("minutes=")[-1].split("&")[0])
                    minutes = max(1, min(720, minutes))  # clamp 1 min to 12 hr
                except (ValueError, AttributeError, IndexError):
                    minutes = 60
                alerts = news_websocket.get_recent_alerts(udir, max_age_minutes=minutes)
                self.send_json({"alerts": alerts, "window_minutes": minutes})
            except Exception as e:
                self._send_error_safe(e, 500, "news-alerts")

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

        elif path == "/api/admin/invites":
            # Round-26: admin-only list of signup invites they created.
            self.handle_admin_list_invites()

        elif path == "/api/admin/export-user-data":
            # Round-40: GDPR-style data export. Returns a ZIP.
            self.handle_admin_export_user_data()

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
        if path == "/api/test-alpaca-keys":
            self.handle_test_alpaca_keys(body)
            return
        if path == "/api/save-alpaca-keys":
            self.handle_save_alpaca_keys(body)
            return
        if path == "/api/toggle-live-mode":
            self.handle_toggle_live_mode(body)
            return
        if path == "/api/toggle-track-record-public":
            self.handle_toggle_track_record_public(body)
            return
        if path == "/api/toggle-scorecard-email":
            self.handle_toggle_scorecard_email(body)
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
        if path == "/api/admin/invites":
            # Round-26: POST creates a single-use signup invite.
            self.handle_admin_create_invite(body)
            return
        if path == "/api/admin/revoke-invite":
            # Round-36: admin revokes an unused invite.
            self.handle_admin_revoke_invite(body)
            return
        if path == "/api/admin/set-admin":
            # Round-36: admin promotes/demotes another user.
            self.handle_admin_set_admin(body)
            return
        if path == "/api/admin/update-user":
            # Round-37: admin edits another user's email / username.
            self.handle_admin_update_user(body)
            return
        if path == "/api/admin/delete-user":
            # Round-40: admin permanent-deletes a user (cascades).
            self.handle_admin_delete_user(body)
            return
        if path == "/api/admin/backfill-journal":
            # Round-40: one-shot backfill of the caller's trade journal.
            self.handle_admin_backfill_journal(body)
            return

        if path == "/api/set-live-parallel":
            # Round-45: toggle the user's live_parallel_enabled flag.
            # When true + live keys present, the scheduler runs both
            # paper + live trees on each tick.
            if not self.current_user:
                return self.send_json({"error": "Not authenticated"}, 401)
            enabled = bool(body.get("enabled"))
            if enabled and not (
                self.current_user.get("alpaca_live_key_encrypted")
                and self.current_user.get("alpaca_live_secret_encrypted")
            ):
                return self.send_json({
                    "error": "Live keys not configured. Save them on Settings → Alpaca API first."
                }, 400)
            if not auth.set_live_parallel_enabled(self.current_user["id"], enabled):
                return self.send_json({"error": "Failed to update"}, 500)
            return self.send_json({"success": True, "enabled": enabled})

        if path == "/api/switch-mode":
            # Round-45 dual-mode: change which state tree the dashboard
            # reads from. Paper or live only. Updates the session row so
            # the mode persists across page reloads / auto-refresh ticks.
            new_mode = (body.get("mode") or "").lower()
            if new_mode not in ("paper", "live"):
                return self.send_json({"error": "mode must be 'paper' or 'live'"}, 400)
            if new_mode == "live":
                # Require live keys to be configured before allowing the
                # switch. Otherwise the dashboard would load an empty
                # live tree and the user would think something's broken.
                if not (self.current_user.get("alpaca_live_key_encrypted") and
                        self.current_user.get("alpaca_live_secret_encrypted")):
                    return self.send_json({
                        "error": "Live keys not configured. Settings → Live Trading tab."
                    }, 400)
            if not self._session_token:
                return self.send_json({"error": "Session missing"}, 400)
            if not auth.set_session_mode(self._session_token, new_mode):
                return self.send_json({"error": "Failed to update session"}, 500)
            return self.send_json({"success": True, "mode": new_mode})

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

        elif path == "/api/factor-bypass":
            self.handle_factor_bypass(body)

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
    log.info("shutdown signal received; stopping scheduler + server",
             extra={"reason": reason})
    try:
        if SCHEDULER_AVAILABLE:
            stop_scheduler()
    except Exception as e:
        log.error("scheduler stop error during shutdown", extra={"error": str(e)})
        observability.capture_exception(e, source="shutdown.scheduler_stop")
    try:
        httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass
    log.info("shutdown: clean exit")


def main():
    # Round-41 hardening: guard against a malformed PORT env var (non-numeric
    # or negative). Previously a typo like PORT=abc on Railway would crash
    # the process on boot with a bare ValueError and no helpful log.
    _port_env = os.environ.get("PORT", "8888")
    try:
        port = int(_port_env)
        if port <= 0 or port > 65535:
            raise ValueError(f"out of range: {port}")
    except (TypeError, ValueError) as e:
        log.warning(f"invalid PORT env var {_port_env!r} ({e}); falling back to 8888")
        port = 8888

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
        log.info("SQLite WAL + busy_timeout enabled")
    except Exception as e:
        log.error("WAL setup failed", extra={"error": str(e)})
        observability.capture_exception(e, source="init.wal_setup")

    # Round-23: boot-time config visibility. These env vars are
    # OPTIONAL (bot works without them) but silently degrade functionality
    # if missing. Surface that at boot so the operator knows which
    # features are dormant and how to turn them on. No WARN for ones
    # that ARE set — keep boot noise down.
    _optional_features = [
        ("GEMINI_API_KEY",
         "🤖 LLM sentiment scoring on pick cards will be DISABLED. "
         "Fix: set GEMINI_API_KEY on Railway (see https://aistudio.google.com/apikey — free tier is plenty)."),
        ("SENTRY_DSN",
         "📡 Exception tracking is DISABLED — errors log to stdout only, no Sentry feed. "
         "Fix: set SENTRY_DSN on Railway (free tier = 5K events/month). "
         "Without this, a silent bug could run for days before you notice."),
        ("NTFY_TOPIC",
         "🔔 Critical-alert ntfy push notifications DISABLED. Email still works if configured. "
         "Fix: set NTFY_TOPIC on Railway to any unique string (e.g. 'alpaca-bot-yourname'), "
         "then subscribe to that topic in the ntfy mobile app."),
    ]
    for env_var, consequence in _optional_features:
        if not os.environ.get(env_var):
            log.warning(f"boot: {env_var} not set — {consequence}")

    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    log.info("dashboard listening", extra={"port": port})
    log.info("ready; SIGINT/SIGTERM triggers graceful shutdown")

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
            log.info("cloud scheduler started; bot running autonomously")
        except Exception as e:
            log.critical("could not start cloud scheduler; exiting for restart",
                         extra={"error": str(e)})
            observability.capture_exception(e, source="main.start_scheduler")
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
        # Round-11: also wire SIGINT through the same path so Ctrl+C
        # drains in-flight requests instead of just raising
        # KeyboardInterrupt (which skips the scheduler's stop hook on
        # some threading paths).
        _signal.signal(_signal.SIGINT, _sigterm_handler)
    except (ValueError, AttributeError):
        pass  # Not in main thread / not supported

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _graceful_shutdown(server, reason="KeyboardInterrupt")


if __name__ == "__main__":
    main()
