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
_last_fatal_notify_ts = 0.0  # rate-limit fatal-loop-error push notifications

# Round-12 audit: kill-switch atomicity. The flag-on-disk + per-symbol
# re-read pattern leaves a <100ms window where a multi-symbol deploy can
# place orders AFTER the switch has tripped (the current iteration reads
# the old value, places the order, then the next iteration reads the new
# value). _deploy_abort_event is a process-local threading.Event that
# kill-switch activation sets IMMEDIATELY; tight loops checking it will
# see the change without a disk-read round-trip.
_deploy_abort_event = threading.Event()


def request_deploy_abort():
    """Signal every in-progress deploy loop to bail out ASAP. Call this
    from the kill-switch activation path so running deploys stop BEFORE
    their next API call, not after their next per-symbol fs re-read."""
    _deploy_abort_event.set()


def clear_deploy_abort():
    """Re-arm the abort event after a kill-switch deactivation. Called
    by the deactivate flow once all state has been cleaned up."""
    _deploy_abort_event.clear()


def deploy_should_abort():
    """Tight-loop-safe check. Returns True if kill-switch just tripped
    and the current deploy loop should exit without placing more orders."""
    return _deploy_abort_event.is_set()
_staleness_last_alert = {}   # {task_key: ts} — prevent repeat alerts every tick
_last_heartbeat_ts = 0.0     # when we last emitted a heartbeat log line

# Persist _last_runs across container restarts. Before this was added,
# a Railway redeploy that happened to straddle 4:05 PM ET would wipe
# the in-memory dict and the daily close would either (a) be re-run
# spuriously or (b) silently skip, depending on timing. With on-disk
# persistence, `should_run_daily_at("daily_close_1", 16, 5)` correctly
# returns False if today's run already completed before the restart.
_LAST_RUNS_PATH = os.path.join(DATA_DIR, "scheduler_last_runs.json")
_last_runs_lock = threading.Lock()


def _load_last_runs():
    """Load the persisted _last_runs dict at scheduler startup. Silent
    best-effort — a missing or malformed file just means we start with
    an empty dict (same as pre-persistence behavior).

    Round-11: drop stale interval stamps (numeric last-run timestamps
    older than 7 days) so a long-idle container or deleted user doesn't
    leak monotonically-growing entries. Daily stamps (ISO dates older
    than 7 days) are also dropped — `should_run_daily_at` already handles
    them correctly but keeping them bloats the file forever.
    """
    global _last_runs
    try:
        with open(_LAST_RUNS_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _cutoff_ts = time.time() - 7 * 86400
            _today_naive = now_et().replace(tzinfo=None)
            _cleaned = {}
            _dropped = 0
            for k, v in data.items():
                # Numeric entries → unix ts from should_run_interval
                if isinstance(v, (int, float)):
                    if v >= _cutoff_ts:
                        _cleaned[k] = v
                    else:
                        _dropped += 1
                # String entries → date stamp from should_run_daily_at;
                # drop entries older than 7 days, keep everything newer.
                elif isinstance(v, str):
                    try:
                        _entry = datetime.strptime(v, "%Y-%m-%d")
                        if (_today_naive - _entry).days <= 7:
                            _cleaned[k] = v
                        else:
                            _dropped += 1
                    except ValueError:
                        # Unexpected format — keep conservatively
                        _cleaned[k] = v
                else:
                    _cleaned[k] = v
            with _last_runs_lock:
                _last_runs.update(_cleaned)
            log(f"Loaded {len(_cleaned)} persisted scheduler last_runs entries "
                f"(dropped {_dropped} stale)", "scheduler")
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"Could not load {_LAST_RUNS_PATH}: {e}", "scheduler")


def _save_last_runs():
    """Atomic write of _last_runs to disk. Called after every update
    to should_run_daily_at / should_run_interval so a restart picks up
    exactly where we were. tempfile+rename keeps the file consistent
    even if the process dies mid-write."""
    try:
        with _last_runs_lock:
            snapshot = dict(_last_runs)
        d = os.path.dirname(_LAST_RUNS_PATH) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(snapshot, f)
            os.rename(tmp, _LAST_RUNS_PATH)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise
    except Exception as e:
        # Persistence is best-effort — never fail a scheduler tick over it.
        log(f"_save_last_runs failed: {e}", "scheduler")


def _heartbeat_tick():
    """Emit a heartbeat log line every ~2 min so /healthz sees recent
    activity even when the scheduler is idle (after hours, weekends).
    Without this, healthz starts returning degraded after 5 minutes of
    silence, triggering Railway restarts for no real reason.

    Round-23: also snapshot _recent_logs to disk so the dashboard
    Activity pane survives Railway redeploys. Cheap — one tempfile+
    rename per ~2 min."""
    global _last_heartbeat_ts
    now = time.time()
    if now - _last_heartbeat_ts > 120:  # 2 min
        _last_heartbeat_ts = now
        log("heartbeat", task="scheduler")
        _save_recent_logs()

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


import logging as _stdlib_logging
_scheduler_logger = _stdlib_logging.getLogger(__name__)


def log(msg, task="scheduler"):
    # ET-only. Railway log cross-reference still works because Railway's
    # own log timestamps are independent of the string we emit.
    # Routes through the structured logger — the JSON envelope carries
    # its own timestamp, but we keep the ET wall-clock string for the
    # in-memory ring buffer so /healthz staleness checks still work.
    now = now_et()
    et_ts = now.strftime("%-I:%M:%S %p ") + (now.tzname() or "ET")
    _scheduler_logger.info(msg, extra={"task": task, "et_ts": et_ts})
    with _logs_lock:
        _recent_logs.append({"ts": et_ts, "ts_iso": now.isoformat(), "task": task, "msg": msg})
        if len(_recent_logs) > 500:
            _recent_logs.pop(0)


# Round-23: persist _recent_logs across Railway redeploys so the
# dashboard Activity pane doesn't reset to empty every time we ship a
# PR. Without this, the user sees "RECENT ACTIVITY (LAST N)" where N is
# however many heartbeats+events fired since the last container boot,
# typically 3-8 in the first few minutes. Persisting keeps the rolling
# 500-line buffer intact across restarts.
_RECENT_LOGS_PATH = os.path.join(DATA_DIR, "scheduler_recent_logs.json")


def _load_recent_logs():
    """Best-effort restore of the _recent_logs ring buffer on boot.
    Called once from start_scheduler. Silent no-op on any failure —
    we'd rather boot with an empty buffer than block the scheduler."""
    global _recent_logs
    try:
        if not os.path.exists(_RECENT_LOGS_PATH):
            return
        with open(_RECENT_LOGS_PATH) as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            # Safety cap — in case someone edits the file to something huge
            with _logs_lock:
                _recent_logs.extend(loaded[-500:])
    except Exception as e:
        # Log but don't crash — boot must not block on telemetry
        try:
            log(f"Could not restore recent_logs from disk: {e}", "scheduler")
        except Exception:
            pass


def _save_recent_logs():
    """Snapshot the ring buffer to disk atomically. Called periodically
    (every ~60s heartbeat) + on graceful shutdown via stop_scheduler."""
    try:
        with _logs_lock:
            snapshot = list(_recent_logs)[-500:]
        d = os.path.dirname(_RECENT_LOGS_PATH) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(snapshot, f)
            os.rename(tmp, _RECENT_LOGS_PATH)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise
    except Exception:
        # Best-effort; never fail a scheduler tick over telemetry I/O.
        pass

# ============================================================================
# MULTI-USER CONTEXT HELPERS
# ============================================================================
def _build_user_dict_for_mode(u, mode):
    """Round-45: construct a scheduler-shaped user dict for a specific
    mode ('paper' or 'live'). Returns None if the user doesn't have
    keys configured for that mode.
    """
    try:
        creds = auth.get_user_alpaca_creds(u["id"], mode=mode)
    except Exception as e:
        log(f"get_user_alpaca_creds({mode}) failed for user {u.get('id')}: {e}", "scheduler")
        return None
    if not creds or not creds.get("key") or not creds.get("secret"):
        return None
    user_dir = auth.user_data_dir(u["id"], mode=mode)
    return {
        "id": u["id"],
        "username": u["username"] + (" [live]" if mode == "live" else ""),
        "_raw_username": u["username"],
        "email": u.get("email"),
        "_mode": mode,
        "_api_key": creds["key"],
        "_api_secret": creds["secret"],
        "_api_endpoint": creds.get("endpoint") or (
            "https://api.alpaca.markets/v2" if mode == "live"
            else "https://paper-api.alpaca.markets/v2"),
        "_data_endpoint": creds.get("data_endpoint") or "https://data.alpaca.markets/v2",
        "_ntfy_topic": creds.get("ntfy_topic") or f"alpaca-bot-{u['username'].lower()}",
        "_notification_email": creds.get("notification_email") or u.get("email"),
        "_data_dir": user_dir,
        "_strategies_dir": os.path.join(user_dir, "strategies"),
        # Round-11 live-trading: propagate the mode + per-trade cap so
        # run_auto_deployer can enforce the live-mode max_position_dollars.
        "_live_mode": (mode == "live"),
        "_live_max_position_dollars": float(creds.get("live_max_position_dollars") or 500),
        "live_mode": (mode == "live"),
        "live_max_position_dollars": float(creds.get("live_max_position_dollars") or 500),
    }


def get_all_users_for_scheduling():
    """Return all active users with valid Alpaca credentials.
    Falls back to env var single-user mode if auth not available or no users.
    Each user dict has the private keys we need to run tasks on their behalf.

    Round-45 dual-mode: each user expands into ONE entry (paper) by default,
    or TWO entries (paper + live) if the user has `live_parallel_enabled=1`
    and live keys configured. The scheduler tasks loop `for user in users`
    and run once per mode per user — paper and live are fully isolated
    (separate state trees, separate Alpaca keys, separate journals).
    """
    if AUTH_AVAILABLE:
        try:
            users = auth.list_active_users()
        except Exception as e:
            log(f"auth.list_active_users failed: {e}. Falling back to env mode.", "scheduler")
            users = []
        result = []
        for u in users:
            paper_entry = _build_user_dict_for_mode(u, "paper")
            if paper_entry:
                result.append(paper_entry)
            # Round-45: only run the live tree if the user explicitly
            # opted in via Settings → Live Trading → Enable Live Parallel.
            # A user with live keys but no opt-in stays paper-only (so
            # you can configure live keys well ahead of actually running
            # live, then flip the switch when ready).
            if u.get("live_parallel_enabled"):
                live_entry = _build_user_dict_for_mode(u, "live")
                if live_entry:
                    result.append(live_entry)
        if result:
            return result

    # Env-var fallback (single-user legacy mode)
    if API_KEY and API_SECRET:
        return [{
            "id": "env",
            "username": os.environ.get("DASHBOARD_USER", "admin"),
            "email": os.environ.get("DASHBOARD_EMAIL", ""),
            "_api_key": API_KEY,
            "_api_secret": API_SECRET,
            "_api_endpoint": API_ENDPOINT,
            "_data_endpoint": DATA_ENDPOINT,
            "_ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
            "_notification_email": os.environ.get("NOTIFICATION_EMAIL", os.environ.get("DASHBOARD_EMAIL", "")),
            "_data_dir": DATA_DIR,              # legacy uses DATA_DIR (was BASE_DIR)
            "_strategies_dir": STRATEGIES_DIR,  # legacy strategies dir
        }]
    return []

# Round-17: Alpaca API helpers + circuit breaker + rate limiter were
# extracted into scheduler_api.py to slim this monolith. Re-exported
# here so existing call sites (`cs.user_api_get(...)`, `cs._cb_state`)
# keep working untouched. New code should import from scheduler_api
# directly.
from scheduler_api import (
    user_api_get, user_api_post, user_api_delete, user_api_patch,
    _user_headers, _cb_key, _cb_blocked, _cb_record_failure, _cb_record_success,
    _rl_acquire, _alert_alpaca_auth_failure,
    _cb_state, _cb_lock, _CB_OPEN_THRESHOLD, _CB_OPEN_SECONDS,
    _rl_state, _rl_lock, _RL_MAX, _RL_REFILL_PER_SEC,
    _auth_alert_dates, _auth_alert_lock,
)

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


# fcntl.flock-based advisory lock for strategy state files. Wheel strategies
# already had this via wheel_strategy._WheelLock; round-7 audit flagged that
# trailing/breakout/mean-rev state files had none, so a scheduler monitor
# tick's read-modify-write could race with a handler thread's pause/stop
# action and silently clobber the user's intent.
#
# Pattern: `with strategy_file_lock(filepath): state = load_json(filepath);
# mutate(state); save_json(filepath, state)`. The lock is released on exit.
try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — Windows
    _HAS_FCNTL = False

class _StrategyFileLock:
    """Exclusive advisory lock held against `path + '.lock'` for the duration
    of a read-modify-write cycle. No-op on systems without fcntl.

    Implementation cribbed from wheel_strategy._WheelLock — same hardening
    applied in round 6 (self.fh only set after flock succeeds).
    """
    def __init__(self, path):
        self.path = path
        self.fh = None
    def __enter__(self):
        if not _HAS_FCNTL:
            return self
        fh = None
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fh = open(self.path + ".lock", "w")
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
            self.fh = fh
        except Exception:
            if fh:
                try: fh.close()
                except Exception: pass
            self.fh = None
        return self
    def __exit__(self, *a):
        if self.fh:
            try: _fcntl.flock(self.fh.fileno(), _fcntl.LOCK_UN)
            except Exception: pass
            try: self.fh.close()
            except Exception: pass
            self.fh = None

def strategy_file_lock(path):
    """Public factory — callers use `with strategy_file_lock(path):` around
    their read-modify-write. Exported so server.py handlers can use the
    same lock file as the scheduler, which is what makes the serialization
    actually work (both threads need to contend on the same flock)."""
    return _StrategyFileLock(path)


# Interval-based tasks and their expected max gap. If _last_runs shows a
# gap bigger than _STALENESS_MULT × these values during market hours
# AND the gap has been observed for 2 consecutive checks, we fire a
# one-off push notification.
#
# Round-49: raised multiplier from 2× → 3× + added 2-observation
# debounce + suppress during 9:30-9:50 ET opening-bell congestion.
# Previously a single slow Alpaca response during the opening crush
# could push one tick past 120s and fire a false "scheduler may be
# stuck" alert (user reported this happened at 9:34 AM, 128s behind).
# The scheduler wasn't stuck — Alpaca was just slow — and the next
# tick caught up on its own.
#
# Daily tasks (auto_deployer_{uid}, wheel_deploy_{uid}, daily_close_{uid})
# omitted — "overdue by 2x the daily interval" = 2 days, not actionable.
_STALENESS_INTERVALS_SEC = {
    "monitor":        60,       # 3x → alert if > 3 min (was 2 min)
    "wheel_monitor":  15 * 60,  # 3x → alert if > 45 min (was 30 min)
    "screener":       30 * 60,  # 3x → alert if > 90 min (was 60 min)
}
_STALENESS_MULT = 3
# Debounce: key → count of consecutive overdue observations.
# Only alert on the 2nd overdue pass (transient bumps don't fire).
_staleness_overdue_count: dict = {}
# Opening-bell congestion window — Alpaca /quotes + /positions latency
# routinely spikes here, so per-user monitor ticks legitimately run
# slower than the usual 2-5s. Suppress the watchdog during this window;
# real stalls still get caught after 9:50 when API normalizes.
_STALENESS_SUPPRESS_START_HHMM = (9, 30)
_STALENESS_SUPPRESS_END_HHMM = (9, 50)


def _within_opening_bell_congestion():
    """Return True if now ET is in the 9:30-9:50 window where Alpaca
    API latency routinely spikes and monitor ticks legitimately slow
    past the staleness threshold."""
    try:
        t = now_et()
        start_h, start_m = _STALENESS_SUPPRESS_START_HHMM
        end_h, end_m = _STALENESS_SUPPRESS_END_HHMM
        hm = t.hour * 60 + t.minute
        return (start_h * 60 + start_m) <= hm < (end_h * 60 + end_m)
    except Exception:
        return False


def _check_task_staleness(users):
    """Fire a push notification if any interval-based per-user task
    has been overdue (> 3× its expected window) for TWO consecutive
    checks. One alert per (task, user) per hour to avoid spam during
    extended outages.

    Round-49: suppressed entirely during 9:30-9:50 ET opening-bell
    congestion + requires 2 consecutive overdue observations to filter
    transient Alpaca API slowdowns.
    """
    now = time.time()
    # Round-49: skip the whole check during opening-bell congestion
    if _within_opening_bell_congestion():
        return
    try:
        for user in users:
            uid = user.get("id")
            if uid is None:
                continue
            for prefix, interval in _STALENESS_INTERVALS_SEC.items():
                key = f"{prefix}_{uid}"
                last = _last_runs.get(key)
                if last is None or not isinstance(last, (int, float)):
                    _staleness_overdue_count.pop(key, None)
                    continue
                gap = now - last
                threshold = interval * _STALENESS_MULT
                if gap < threshold:
                    # Task caught up — reset debounce counter
                    _staleness_overdue_count.pop(key, None)
                    continue
                # Overdue. Round-49 debounce: only alert on 2nd
                # consecutive overdue observation. First overdue pass
                # just bumps the counter so transient spikes don't
                # page the user.
                prev_count = _staleness_overdue_count.get(key, 0)
                _staleness_overdue_count[key] = prev_count + 1
                if prev_count < 1:
                    continue  # first strike — wait for confirmation
                # Per-hour alert dedup
                if now - _staleness_last_alert.get(key, 0) < 3600:
                    continue
                _staleness_last_alert[key] = now
                msg = (
                    f"Task {prefix} for user {user.get('username','?')} is "
                    f"{int(gap)}s behind (expected every {interval}s, "
                    f"{_STALENESS_MULT}x threshold = {threshold}s). "
                    f"Scheduler may be stuck or Alpaca may be failing."
                )
                log(msg, "staleness")
                try:
                    notify_user(user, msg, "alert")
                except Exception:
                    pass
    except Exception as _e:
        # Watchdog must never crash the scheduler loop
        log(f"task-staleness watchdog error: {_e}", "staleness")

# ============================================================================
# NOTIFICATIONS
# ============================================================================
def notify_user(user, message, notify_type="info"):
    """Send a notification tagged with the user's ntfy topic.

    Round-45: live-mode trades get a [LIVE] prefix so the user can
    distinguish paper-account activity from real-money activity at a
    glance in ntfy / email. Paper mode (default) is unprefixed.

    Round-48 PRIVACY FIX: pass per-user NOTIFICATION_EMAIL + DATA_DIR
    into the notify.py subprocess env. Before this, notify.py wrote to
    a SHARED root email_queue.json with a HARDCODED se2login@gmail.com
    recipient, so every user's trade emails piled up in Kbell0629's
    inbox — the severe cross-user leak the user reported. Now the
    per-user queue file + per-user recipient are enforced at the
    subprocess boundary.
    """
    try:
        env = os.environ.copy()
        if user.get("_ntfy_topic"):
            env["NTFY_TOPIC"] = user["_ntfy_topic"]
        # Per-user email recipient — falls back to user.email if
        # notification_email wasn't set explicitly.
        _email = (user.get("_notification_email")
                  or user.get("notification_email")
                  or user.get("email") or "").strip()
        if _email:
            env["NOTIFICATION_EMAIL"] = _email
        else:
            # No recipient → make sure we don't inherit a stale
            # NOTIFICATION_EMAIL from the parent env of another user.
            env.pop("NOTIFICATION_EMAIL", None)
        # Per-user queue dir — notify.py writes email_queue.json under
        # DATA_DIR, so overriding DATA_DIR routes the queue to the
        # user's own dir (users/<id>/ or users/<id>/live/ for dual-mode).
        if user.get("_data_dir"):
            env["DATA_DIR"] = user["_data_dir"]
        if user.get("_mode") == "live" and not message.startswith("[LIVE]"):
            message = "[LIVE] " + message
        p = subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "notify.py"),
             "--type", notify_type, message],
            env=env,
        )
        _track_child(p)  # round-10: reap on SIGTERM
    except Exception as e:
        log(f"Notification failed: {e}")


def _flatten_all_user(user):
    """Emergency flatten helper — cancels orders, closes equity AND
    option positions. Used by auto kill-switch paths (daily loss,
    max drawdown). Previously these paths only called DELETE /orders +
    DELETE /positions; /positions closes equity but NOT options, so
    short puts and short calls were left unbounded.
    """
    try:
        user_api_delete(user, "/orders")
    except Exception as e:
        log(f"[{user.get('username','?')}] flatten: DELETE /orders failed: {e}", "monitor")
    # Close wheel option contracts BEFORE equity liquidation so their
    # BTC orders don't collide with cancellations.
    try:
        import wheel_strategy as ws
        for wpath in ws.list_wheel_files(user):
            try:
                wstate = load_json(wpath) or {}
                active = wstate.get("active_contract") or {}
                occ_sym = active.get("symbol")
                if not occ_sym:
                    continue
                # Buy-to-close at 2x the current ask (aggressive to
                # guarantee fill during a panic exit).
                try:
                    quote = ws.get_option_quote(user, occ_sym) or {}
                    ask = float(quote.get("ask") or quote.get("c") or 0.01)
                except Exception:
                    ask = 0.01
                limit_px = max(0.01, round(ask * 2, 2))
                user_api_post(user, "/orders", {
                    "symbol": occ_sym, "qty": "1",
                    "side": "buy", "type": "limit",
                    "limit_price": str(limit_px),
                    "time_in_force": "day",
                })
            except Exception as e:
                log(f"[{user.get('username','?')}] flatten: wheel close "
                    f"failed on {wpath}: {e}", "monitor")
    except Exception as e:
        log(f"[{user.get('username','?')}] flatten: wheel enumeration failed: {e}", "monitor")
    try:
        user_api_delete(user, "/positions")
    except Exception as e:
        log(f"[{user.get('username','?')}] flatten: DELETE /positions failed: {e}", "monitor")


def notify_rich(user, short_message, notify_type="info",
                rich_subject=None, rich_body=None):
    """Send a push notification (short one-liner to ntfy) AND queue a
    detailed email with teaching-level context (rich_body).

    Short message goes to ntfy via notify.py --push-only so the auto-
    queued short email is skipped — we queue the rich one directly.
    If rich_body is None, falls back to standard notify_user() (email
    content = short message, same as before).
    """
    if not rich_body:
        return notify_user(user, short_message, notify_type)
    # 1. Push-only via ntfy (skips auto email queue)
    try:
        env = os.environ.copy()
        if user.get("_ntfy_topic"):
            env["NTFY_TOPIC"] = user["_ntfy_topic"]
        subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "notify.py"),
             "--type", notify_type, "--push-only", short_message],
            env=env,
        )
    except Exception as e:
        log(f"Push notification failed: {e}")
    # 2. Direct queue the rich email
    try:
        _queue_direct_email(
            user,
            subject=rich_subject or f"[Trading Bot] {short_message[:60]}",
            body=rich_body,
            notify_type=notify_type,
        )
    except Exception as e:
        log(f"Rich email queue failed: {e}")

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
        # Round-11 audit: previous fallback treated `now_et()` as UTC
        # and applied an ET offset on top — silent double-conversion
        # catastrophe for every wall-clock gate (auto-deployer 9:35,
        # daily close, market-hours check). `now_et()` ALREADY returns
        # ET, so just strip tz and return.
        return now_et().replace(tzinfo=None)

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
    # Round-36: pin LEARNED_WEIGHTS_PATH to the per-user file. learn.py
    # writes there every Friday; update_dashboard.py was reading from
    # the shared DATA_DIR path and never seeing per-user weights.
    env["LEARNED_WEIGHTS_PATH"] = user_file(user, "learned_weights.json")
    # Timeout: 600s (10 min) — Railway containers have slower network than local.
    # Screening 10k+ stocks in 22 batches of 500 can take 3-8 min on Railway vs ~60s local.
    # Round-11: run as Popen and poll in a small wait loop so the main
    # scheduler loop keeps its heartbeat alive. Previous subprocess.run
    # was synchronous — blocked the entire scheduler thread for up to
    # 10 min, causing /healthz to flip 503 (staleness) mid-run and
    # Railway to restart the container. Polling every 10s lets us call
    # `_heartbeat_tick()` so the log buffer stays fresh.
    SCREENER_TIMEOUT = 600
    try:
        p = subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "update_dashboard.py")],
            cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        _track_child(p)
        deadline = time.time() + SCREENER_TIMEOUT
        while time.time() < deadline:
            rc = p.poll()
            if rc is not None:
                break
            # Emit a heartbeat every ~60s so /healthz doesn't go stale
            # while a long screener is running.
            if int(time.time()) % 60 == 0:
                _heartbeat_tick()
            time.sleep(3)
        else:
            # Round-15: terminate() sends SIGTERM but a wedged child (e.g.,
            # stuck in a C-extension network call) may ignore it. Wait
            # briefly, then SIGKILL as a guaranteed backstop so we don't
            # leave zombies when Railway redeploys or when the scheduler
            # loop cycles.
            try:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass  # kernel will reap; we did our part
            except Exception:
                pass
            log(f"[{user['username']}] Screener timed out (>{SCREENER_TIMEOUT}s)", "screener")
            return
        try:
            stdout, stderr = p.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        if p.returncode == 0:
            log(f"[{user['username']}] Screener completed", "screener")
            _last_runs[key] = time.time()
        else:
            log(f"[{user['username']}] Screener failed: {(stderr or '')[:200]}", "screener")
    except Exception as e:
        log(f"[{user['username']}] Screener error: {e}", "screener")

# ============================================================================
# Journal writeback helper — CRITICAL for scorecard/readiness/learning
# ============================================================================
# Previously every exit (stop fill, target hit, short cover, profit ladder
# final sell) updated the per-symbol strategy file with exit details but
# NEVER wrote back to trade_journal.json. Result: update_scorecard never
# saw any closed trades → win_rate stayed 0, Sharpe ≈0, readiness capped
# Round-33: Counterpart to record_trade_close. Previously only the main
# run_auto_deployer path appended to the journal. Wheel puts and
# manual-deploy (via dashboard Deploy button) never wrote entries, so the
# scorecard's total_trades counter undercounted reality. Centralise the
# journal-append logic here + call from all three deploy paths so the
# scorecard has ground truth for win rate / profit factor / Sharpe.
def record_trade_open(user, symbol, strategy, price, qty, reason,
                       side="buy", deployer="unknown", extra=None):
    """Append an 'open' entry to the user's trade journal. Held under
    strategy_file_lock so it doesn't race with record_trade_close or
    update_scorecard's daily_snapshots append."""
    journal_path = user_file(user, "trade_journal.json")
    try:
        with strategy_file_lock(journal_path):
            journal = load_json(journal_path) or {"trades": [], "daily_snapshots": []}
            entry = {
                "timestamp": now_et().isoformat(),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": float(price) if price is not None else None,
                "strategy": strategy,
                "reason": reason,
                "deployer": deployer,
                "status": "open",
            }
            if extra and isinstance(extra, dict):
                entry.update(extra)
            journal.setdefault("trades", []).append(entry)
            save_json(journal_path, journal)
    except Exception as e:
        try:
            log(f"[{user.get('username','?')}] {symbol}: journal append failed: {e}",
                "journal")
        except Exception:
            pass


# at ~40, learn.py never adjusted weights.
#
# This helper finds the most recent OPEN journal entry for the symbol
# and marks it closed with P&L + exit details.
def record_trade_close(user, symbol, strategy, exit_price, pnl, exit_reason,
                        qty=None, side="sell"):
    """Mark the matching open journal entry as closed. Idempotent:
    if the entry is already closed, does nothing.

    Round-10 audit: the read-modify-write must hold strategy_file_lock
    around the whole sequence. Six exit paths call this (stop hit,
    target hit, PEAD 60d, PEAD pre-earnings, friday reduction,
    profit-ladder final sell) and update_scorecard.py also appends
    daily_snapshots into the same file. Two near-simultaneous exits
    without the lock silently drop one of the close writebacks —
    same class of bug the round-7 writeback fix was meant to prevent.

    Round-51: on the sell side (side=='sell' long close), also record
    the proceeds in the settled-funds ledger so cash accounts respect
    the T+1 rule on their next deploy. Best-effort — never raises.
    """
    # Round-51: per-sell settled-funds ledger entry (cash accounts only)
    # Short covers (side='buy') don't generate settled-funds proceeds.
    if side == "sell" and exit_price and qty:
        try:
            import settled_funds as _sf
            try:
                _proceeds = float(exit_price) * abs(float(qty))
            except (TypeError, ValueError):
                _proceeds = 0
            if _proceeds > 0:
                _sf.record_sale(user, symbol, _proceeds)
        except Exception:
            pass  # best-effort — ledger failure must not break trade close
    journal_path = user_file(user, "trade_journal.json")
    try:
        with strategy_file_lock(journal_path):
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
                    # Round-11: guard both sides against zero so a
                    # short-cover with exit_price=0 (Alpaca hiccup)
                    # doesn't silently skip pnl_pct via the outer
                    # `except: pass`. Logs a warning when skipped.
                    try:
                        entry_px = float(t.get("price") or 0)
                        exit_px = float(exit_price) if exit_price is not None else 0.0
                        t_qty = int(float(qty if qty is not None else t.get("qty") or 0))
                        if entry_px > 0 and t_qty and exit_px > 0:
                            if side == "sell":  # long close
                                t["pnl_pct"] = round((exit_px / entry_px - 1) * 100, 2)
                            else:  # short cover
                                t["pnl_pct"] = round((entry_px / exit_px - 1) * 100, 2)
                        else:
                            log(f"[{user.get('username','?')}] {symbol} pnl_pct skipped (entry={entry_px} exit={exit_px} qty={t_qty})", "monitor")
                    except Exception as _pp_e:
                        log(f"[{user.get('username','?')}] {symbol} pnl_pct calc failed: {_pp_e}", "monitor")
                    t["exit_side"] = side
                    save_json(journal_path, journal)
                    return True
            # Round-34: no matching open entry.  Before, this silently
            # returned False and the close DISAPPEARED — nothing in the
            # journal, scorecard undercounted, user had to guess from the
            # activity log what actually happened.  This is the exact
            # gap that caused `last_loss_time` to advance at 10:59 AM
            # with no visible record of which position closed.
            #
            # Fix: append a synthetic entry marked orphan_close=True so
            # the close IS recorded, flows into the scorecard, and shows
            # up in /api/data todays_closes.  The synthetic entry carries
            # as much info as we have — it can't know the original entry
            # price (so pnl_pct can't be computed), but symbol / strategy
            # / exit_price / pnl / exit_reason are all captured.
            synthetic = {
                "timestamp": now_et().isoformat(),
                "symbol": symbol,
                "side": "buy" if side == "sell" else "sell_short",
                "qty": qty,
                "price": None,  # unknown entry price
                "strategy": strategy,
                "reason": "synthetic open entry — original never journaled",
                "deployer": "record_trade_close_orphan",
                "status": "closed",
                "orphan_close": True,
                "exit_timestamp": now_et().isoformat(),
                "exit_price": round(float(exit_price), 4) if exit_price else None,
                "exit_reason": exit_reason,
                "exit_side": side,
                "pnl": None,
                "pnl_pct": None,
            }
            try:
                synthetic["pnl"] = round(float(pnl), 2)
            except (TypeError, ValueError):
                pass
            journal.setdefault("trades", []).append(synthetic)
            save_json(journal_path, journal)
            log(f"[{user.get('username','?')}] {symbol}: closed with no matching "
                f"open journal entry — recorded as orphan_close "
                f"(reason={exit_reason}, pnl=${synthetic.get('pnl')})", "monitor")
            return True
    except Exception as e:
        log(f"[{user.get('username','?')}] Journal close writeback failed for {symbol}: {e}", "monitor")
        return False


# ============================================================================
# TASK 2: STRATEGY MONITOR (per user)
# ============================================================================
# ============================================================================
# Round-61 pt.18: STEPPED TRAILING STOP (professional risk management)
# ============================================================================
# Classic flat-trail behaviour (8% below highest, move up only) gives back
# too much gain on a winner that reverses. Institutional systems use a
# stepped trail: wide cushion while the breakout is testing, then tight
# as profit accumulates, with an explicit break-even lock once +5% is
# reached. This function is the single source of truth for what the
# stop should be at any given profit level — both the long-trailing and
# short-trailing paths in monitor_strategies call it so the tier logic
# stays in one place.
#
# Tiers (profit % measured from entry using highest_seen for longs /
# lowest_seen for shorts):
#   Tier 1 (0 to +5%):   default trail (usually 8%) — breathing room
#                        for the breakout retest.
#   Tier 2 (+5 to +10%): STOP LOCKED TO ENTRY — no-loss guarantee.
#   Tier 3 (+10 to +20%): 6% trail — lock in some gain, allow pullbacks.
#   Tier 4 (+20%+):       4% trail — ride the big move tight.
#
# Opt-out: strategy file can set rules.stepped_trail=false to revert to
# the flat trail (e.g. for strategies where the stepped logic has not
# been validated yet). Default True for breakout / trailing_stop /
# short_sell / copy_trading / pead.
def _compute_stepped_stop(entry, extreme_price, default_trail, is_short=False):
    """Compute the protective stop price for a position given the
    current profit level.

    Args:
      entry:         avg entry price (float).
      extreme_price: highest_price_seen (long) or lowest_price_seen
                     (short). This is the "favorable" extreme the
                     trailing logic tracks.
      default_trail: fallback trail pct when in Tier 1 (e.g. 0.08).
                     Read from rules.trailing_distance_pct so users
                     can tune the tier-1 width per strategy without
                     changing the whole tier table.
      is_short:      True for short positions — stop is computed ABOVE
                     the lowest-seen price instead of below highest.

    Returns:
      (new_stop_price, tier_number, trail_pct_used)
      `trail_pct_used` is None in Tier 2 (stop is fixed at entry, not
      derived from a trail distance).
    """
    if entry is None or entry <= 0:
        # Defensive: no entry price → can't compute tiers. Fall back
        # to flat trail off the extreme so we still place SOMETHING.
        if is_short:
            return round(extreme_price * (1 + default_trail), 2), 1, default_trail
        return round(extreme_price * (1 - default_trail), 2), 1, default_trail

    if is_short:
        # Short profit = (entry - lowest) / entry. Positive when price
        # has dropped below entry (favorable for short).
        profit_pct = (entry - extreme_price) / entry
    else:
        profit_pct = (extreme_price - entry) / entry

    if profit_pct < 0.05:
        # Tier 1: let the position breathe. Default trail below/above
        # the favorable extreme.
        if is_short:
            return round(extreme_price * (1 + default_trail), 2), 1, default_trail
        return round(extreme_price * (1 - default_trail), 2), 1, default_trail

    if profit_pct < 0.10:
        # Tier 2: BREAK-EVEN lock. Stop at entry — if this trade
        # reverses from +5% back to +0%, you exit flat instead of
        # giving back the whole cushion.
        return round(entry, 2), 2, None

    if profit_pct < 0.20:
        # Tier 3: 6% trail. Lock in some gain while still allowing a
        # pullback to shake weak hands before the next leg up.
        trail = 0.06
        if is_short:
            return round(extreme_price * (1 + trail), 2), 3, trail
        return round(extreme_price * (1 - trail), 2), 3, trail

    # Tier 4: 4% trail. Big winner — protect the gain tightly because
    # the further it runs the more asymmetric the giveback is.
    trail = 0.04
    if is_short:
        return round(extreme_price * (1 + trail), 2), 4, trail
    return round(extreme_price * (1 - trail), 2), 4, trail


def monitor_strategies(user, extended_hours=False):
    """Monitor + manage existing positions.

    Round-55: `extended_hours=True` activates a stops-only mode for
    pre-market + post-market ticks. In this mode the function ONLY
    raises trailing stops based on after-hours price moves; it skips
    the daily-loss kill-switch check, profit-take ladder, and
    mean-reversion target fires (those exit decisions use regular-
    hours liquidity and shouldn't fire against thin extended-hours
    quotes). The raised stop prices take effect at next market open
    — locking in any post-market pop before the next morning's drop.

    Round-61 pt.28: narrow carve-out for unprotected shorts.
    ``process_short_strategy`` is now called in AH mode too, but
    internally it only runs the initial cover-stop placement path
    for positions with no live ``cover_order_id``. A GTC stop placed
    in AH sits idle at Alpaca (doesn't fire until regular hours), so
    the thin-book risk that motivated the original R55 skip doesn't
    apply to placement. This closes the gap where a Friday-close
    audit finding of HIGH missing_stop on a short (e.g. SOXL) had
    to wait for Monday's regular-hours monitor tick to get covered,
    leaving the position unprotected all weekend.
    """
    try:
        guardrails_path = user_file(user, "guardrails.json")
        # Round-10: hold the file lock across the read + subsequent
        # writes so a concurrent handler kill-switch set doesn't get
        # silently overwritten by our monitor-tick flush. We release
        # and re-acquire around the slower Alpaca calls.
        with strategy_file_lock(guardrails_path):
            guardrails = load_json(guardrails_path) or {}
        if guardrails.get("kill_switch"):
            return

        # Round-55: extended-hours opt-out. Default ON — users
        # benefit from post-market pop preservation automatically.
        # Set `extended_hours_trailing: false` in guardrails to
        # disable (e.g., if user wants overnight positions to run
        # at the prior day's stop level).
        if extended_hours and guardrails.get("extended_hours_trailing", True) is False:
            return

        # Daily loss check — SKIP in extended-hours mode. daily loss
        # math depends on portfolio_value which is unreliable in AH
        # (thin trades skew valuations), and flattening in AH would
        # get terrible fills.
        if extended_hours:
            account = user_api_get(user, "/account")
            # Skip everything except trailing-stop tightening.
            # Jump straight to per-strategy processing with a flag.
            try:
                import portfolio_calibration as _pc
                _tier = _pc.detect_tier(account) if isinstance(account, dict) else None
                if _tier:
                    user["_tier_cfg"] = _pc.apply_user_overrides(_tier, guardrails)
            except Exception:
                pass
            # Process strategy files in AH-only mode
            sdir = user.get("_strategies_dir") or os.path.join(
                user.get("_data_dir") or DATA_DIR, "strategies")
            if os.path.isdir(sdir):
                for fname in os.listdir(sdir):
                    if not fname.endswith(".json") or fname.startswith("wheel_"):
                        continue  # skip wheel files in AH; options too illiquid
                    filepath = os.path.join(sdir, fname)
                    try:
                        with strategy_file_lock(filepath):
                            strat = load_json(filepath)
                            if not strat:
                                continue
                            # Round-61 pt.28: shorts are no longer skipped
                            # at the loop level. process_strategy_file
                            # delegates to process_short_strategy with
                            # extended_hours=True, which ONLY places a
                            # protective cover-stop for unprotected shorts
                            # (GTC stops don't fire until regular hours,
                            # so there's no thin-book cover-fill risk).
                            # All other short-side branches remain gated
                            # on regular hours — see the pt.28 docstring.
                            if strat.get("paused"):
                                continue
                            process_strategy_file(user, filepath, strat,
                                                    extended_hours=True)
                    except Exception as _e:
                        log(f"[{user['username']}] AH monitor error on {fname}: {_e}",
                            "monitor")
            return

        # --- Regular hours: full monitor (below) ---
        # Daily loss check
        account = user_api_get(user, "/account")
        # Round-51: detect portfolio tier once per monitor tick so all
        # downstream exit decisions (profit ladder, stop, target) can
        # consult it. Stashed on `user` dict as `_tier_cfg`; cleared
        # before return so it doesn't leak across ticks.
        try:
            import portfolio_calibration as _pc
            _tier = _pc.detect_tier(account) if isinstance(account, dict) else None
            if _tier:
                user["_tier_cfg"] = _pc.apply_user_overrides(_tier, guardrails)
        except Exception:
            pass  # calibration is advisory; never block the monitor
        if isinstance(account, dict) and "error" not in account:
            current_val = float(account.get("portfolio_value", 0))
            daily_start = guardrails.get("daily_starting_value")
            stored_date = guardrails.get("daily_starting_value_date")
            today_et = get_et_time().strftime("%Y-%m-%d")
            # Fallback: set daily_starting_value if (a) never set, (b) stored
            # date doesn't match today's ET date (new trading day), or (c) the
            # auto-deployer never ran to set it (disabled, kill-switch cooldown).
            # Tagging with the ET date keeps this fallback in sync with the
            # auto-deployer's set-once logic in run_auto_deployer (line ~1076).
            if current_val > 0 and (not daily_start or stored_date != today_et):
                guardrails["daily_starting_value"] = current_val
                guardrails["daily_starting_value_date"] = today_et
                with strategy_file_lock(guardrails_path):
                    # Re-read under lock, merge our field set, write atomically.
                    _cur = load_json(guardrails_path) or {}
                    _cur.update(guardrails)
                    save_json(guardrails_path, _cur)
                daily_start = current_val
            # Round-10 audit: enforce max_drawdown (peak→trough) in
            # addition to the daily loss limit. The guardrails config
            # already carries `max_drawdown_pct: 0.10` but nothing was
            # reading it — a 9% slide over a week with <3% daily moves
            # would never trigger the kill switch. Check peak/current
            # and fire the same flatten path on breach.
            # Round-11: if peak is missing (pre-round-10 user), seed with
            # current value so the drawdown check is defined rather than
            # silently skipped (which would leave max_drawdown un-enforced).
            peak_val = guardrails.get("peak_portfolio_value")
            if not peak_val and current_val:
                with strategy_file_lock(guardrails_path):
                    _g = load_json(guardrails_path) or {}
                    _g.setdefault("peak_portfolio_value", current_val)
                    save_json(guardrails_path, _g)
                    peak_val = _g["peak_portfolio_value"]
            if peak_val and current_val and peak_val > 0:
                dd_pct = (peak_val - current_val) / peak_val
                if dd_pct > guardrails.get("max_drawdown_pct", 0.10):
                    guardrails["kill_switch"] = True
                    guardrails["kill_switch_triggered_at"] = now_et().isoformat()
                    reason_dd = (f"Max drawdown {dd_pct*100:.1f}% "
                                 f"(peak ${peak_val:,.0f} → current ${current_val:,.0f})")
                    guardrails["kill_switch_reason"] = reason_dd
                    with strategy_file_lock(guardrails_path):
                        _cur = load_json(guardrails_path) or {}
                        _cur.update(guardrails)
                        save_json(guardrails_path, _cur)
                    # Round-11 item 19: multi-channel critical alert
                    # (Sentry + ntfy + email). Kill switches should
                    # never be missed.
                    try:
                        from observability import critical_alert
                        critical_alert(
                            f"KILL SWITCH triggered for {user.get('username','?')}",
                            reason_dd,
                            tags={"event": "kill_switch", "kind": "max_drawdown"},
                            user=user,
                        )
                    except Exception:
                        pass
                    _flatten_all_user(user)  # cancels orders + positions + wheel options
                    try:
                        import notification_templates as _nt
                        _subj, _body = _nt.kill_switch(
                            reason=reason_dd,
                            portfolio_value=current_val,
                            daily_pnl=current_val - (daily_start or peak_val),
                        )
                    except Exception:
                        _subj = _body = None
                    notify_rich(user, f"KILL SWITCH: Max drawdown {dd_pct*100:.1f}% exceeded.",
                                "kill", rich_subject=_subj, rich_body=_body)
                    log(f"[{user['username']}] KILL SWITCH triggered: {dd_pct*100:.1f}% drawdown", "monitor")
                    return
            if daily_start:
                loss_pct = (daily_start - current_val) / daily_start
                if loss_pct > guardrails.get("daily_loss_limit_pct", 0.03):
                    guardrails["kill_switch"] = True
                    guardrails["kill_switch_triggered_at"] = now_et().isoformat()
                    reason = f"Daily loss {loss_pct*100:.1f}% (auto-trigger at {guardrails.get('daily_loss_limit_pct', 0.03)*100:.1f}% limit)"
                    guardrails["kill_switch_reason"] = reason
                    with strategy_file_lock(guardrails_path):
                        _cur = load_json(guardrails_path) or {}
                        _cur.update(guardrails)
                        save_json(guardrails_path, _cur)
                    _flatten_all_user(user)  # orders + wheel options + equity
                    try:
                        import notification_templates as _nt
                        daily_pnl = current_val - daily_start
                        _subj, _body = _nt.kill_switch(
                            reason=reason,
                            portfolio_value=current_val,
                            daily_pnl=daily_pnl,
                        )
                    except Exception:
                        _subj = _body = None
                    notify_rich(user,
                                f"KILL SWITCH: Daily loss {loss_pct*100:.1f}% exceeded.",
                                "kill", rich_subject=_subj, rich_body=_body)
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
            # Serialize the read-modify-write cycle. Without this lock, a
            # handler thread calling pause_strategy or stop_strategy can
            # write mid-tick, then the monitor's save_json clobbers the
            # status change. Lock file is path + ".lock" — same file is
            # acquired by handlers/strategy_mixin.py for the same reason.
            with strategy_file_lock(filepath):
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

        # Round-51: PDT guard — if user is on margin <$25k (PDT rules
        # apply) AND this would be a same-day entry-and-exit trade AND
        # day_trades_remaining is at/below the buffer, HOLD the
        # position overnight instead of firing the intraday sell. Saves
        # the emergency day-trade slot for kill-switch scenarios.
        _tier = (strat.get("_user_ref") or {}).get("_tier_cfg") if isinstance(strat, dict) else None
        # We don't have user on `strat`; fall back to local user binding
        _tier = user.get("_tier_cfg") if isinstance(user, dict) else None
        if _tier and _tier.get("pdt_applies"):
            try:
                import pdt_tracker as _pdt
                _opened = (strat.get("entered_at") or strat.get("opened_at")
                           or strat.get("deployed_at") or "")
                _is_same_day = _pdt.is_day_trade(_opened, now_et().isoformat())
                if _is_same_day:
                    _allow, _reason, _rem = _pdt.can_day_trade(_tier, buffer=1)
                    if not _allow:
                        log(f"[{user['username']}] {symbol}: profit-take "
                            f"skipped — {_reason}", "monitor")
                        continue
            except Exception:
                pass  # PDT check never blocks on error

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
                # Round-10 audit: use PATCH to atomically bump the qty
                # on the existing stop. The old "place new + cancel
                # old" path hits Alpaca's duplicate-order 403 (same
                # bug class as the trailing-stop raise). Fall back to
                # cancel-then-place if PATCH fails.
                patched = user_api_patch(user, f"/orders/{old_stop_id}",
                                          {"qty": str(remaining)})
                new_stop = patched if (isinstance(patched, dict)
                                        and "id" in patched) else None
                if not new_stop:
                    user_api_delete(user, f"/orders/{old_stop_id}")
                    new_stop = user_api_post(user, "/orders", {
                        "symbol": symbol, "qty": str(remaining), "side": "sell",
                        "type": "stop", "stop_price": str(current_stop_price),
                        "time_in_force": "gtc"
                    })
                if isinstance(new_stop, dict) and "id" in new_stop:
                    state["stop_order_id"] = new_stop["id"]
                else:
                    log(f"[{user['username']}] {symbol}: WARN stop resize failed after profit-take. Err: {new_stop}", "monitor")
            elif old_stop_id and remaining <= 0:
                user_api_delete(user, f"/orders/{old_stop_id}")
                state["stop_order_id"] = None

            # Ensure state is attached to strat before save (defensive)
            strat["state"] = state
            save_json(filepath, strat)
            log(f"[{user['username']}] {symbol}: Profit take level {level}% — sold {sell_qty} shares", "monitor")
            notify_user(user, f"Profit take on {symbol} at +{level}%: sold {sell_qty} shares. {remaining} still held.", "exit")
            return  # One level per check

def process_short_strategy(user, filepath, strat, state, rules, extended_hours=False):
    """Manage a short_sell position (inverse logic — we profit when price falls).

    Round-61 pt.28: when ``extended_hours=True`` the function runs ONLY
    the initial cover-stop placement path for unprotected positions
    (``cover_order_id`` is ``None`` or has been reset by pt.24/25/26's
    liveness checks). Everything else — profit-target limit placement,
    trailing-stop tightening, cover-fill processing, force-cover on
    max-hold-exceeded — is gated on regular hours because it either
    requires thin-book-risky fills or assumes regular-hours liquidity.

    Placing a protective stop in AH is safe: stop orders we submit are
    ``time_in_force=gtc`` without ``extended_hours=true``, so Alpaca
    only triggers them when regular-hours price crosses. The stop
    sits idle over the weekend / overnight and fires at next open if
    the short has moved against us. This closes the pt.26 gap where
    an unprotected short (audit HIGH missing_stop) had to wait until
    the next regular-hours monitor tick — which on a Friday close
    means the position sits uncovered all weekend."""
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
    # Round-61 pt.19: adaptive stop distance so an already-underwater short
    # still gets an Alpaca-accepted stop. Previous logic
    # `entry * (1 + stop_pct)` placed the stop BELOW current market when the
    # short had moved significantly against us (user-reported: SOXL entry
    # $110.65 -> stop $121.72, but current $129.13 — Alpaca rejected every
    # placement attempt so no cover stop existed despite the strategy file
    # being in place). Mirror the error_recovery.create_orphan_strategy
    # formula: max(entry*(1+pct), current*1.05) so the stop is ALWAYS on
    # the protective side of current price.
    #
    # Round-61 pt.24: also verify the persisted cover_order_id is still
    # LIVE at Alpaca before trusting it. If a prior placement was
    # rejected (e.g. invalid-price) or later canceled, the id stays in
    # state but the order doesn't exist at the broker — monitor never
    # retried, position stayed unprotected forever. User-reported: SOXL
    # audit flagged missing BUY stop despite the cover_order_id being
    # set in the strategy file. Fix: query the order; if it's
    # canceled/rejected/expired, reset cover_order_id=None so this tick
    # places a fresh one.
    # Round-61 pt.25: fix pt.24's unreachable elif. Alpaca error
    # responses come back as dicts with an "error" key, so the old
    # `elif isinstance(existing_order, dict)` branch never fired
    # (first branch already matched). Restructure: check "error"
    # FIRST inside the dict branch.
    existing_cover_id = state.get("cover_order_id")
    if existing_cover_id:
        existing_order = user_api_get(user, f"/orders/{existing_cover_id}")
        if isinstance(existing_order, dict):
            if "error" in existing_order:
                # Alpaca returned an error looking up the order —
                # most commonly 404 (order id doesn't exist).
                log(f"[{user['username']}] {symbol}: cover_order_id "
                    f"{existing_cover_id} not found at Alpaca — "
                    "resetting.", "monitor")
                state["cover_order_id"] = None
            else:
                existing_status = str(existing_order.get("status") or "").lower()
                # Dead statuses — order never filled.
                if existing_status in ("canceled", "cancelled", "rejected",
                                        "expired", "replaced", "done_for_day"):
                    log(f"[{user['username']}] {symbol}: stale cover_order_id "
                        f"{existing_cover_id} is {existing_status} — resetting "
                        "so next placement can retry.", "monitor")
                    state["cover_order_id"] = None
    # Round-61 pt.26: aggressive cross-check against Alpaca's
    # open-orders list. The pt.24/pt.25 per-order status lookup works
    # for canceled/rejected/expired, but misses edge cases where the
    # cover_order_id in state points to an order that's in some
    # non-dead-but-also-not-open status (e.g. "accepted",
    # "pending_new" that later expired without our /orders/{id}
    # lookup catching it). If cover_order_id is set but NOT in the
    # /orders?status=open list for this symbol, reset + retry.
    if state.get("cover_order_id"):
        open_orders = user_api_get(user, f"/orders?status=open&symbols={symbol}&limit=50")
        if isinstance(open_orders, list):
            open_ids = {o.get("id") for o in open_orders if isinstance(o, dict)}
            if state["cover_order_id"] not in open_ids:
                log(f"[{user['username']}] {symbol}: cover_order_id "
                    f"{state['cover_order_id']} NOT in Alpaca open-orders "
                    f"list (found {len(open_ids)} open orders) — resetting.",
                    "monitor")
                state["cover_order_id"] = None
    if not state.get("cover_order_id"):
        stop_pct = rules.get("stop_loss_pct", 0.08)
        entry_stop = entry * (1 + stop_pct)
        current_stop = price * 1.05
        stop_price = round(max(entry_stop, current_stop), 2)
        log(f"[{user['username']}] {symbol}: placing SHORT cover-stop "
            f"qty={shares} entry=${entry:.2f} current=${price:.2f} "
            f"stop=${stop_price:.2f}", "monitor")
        order = user_api_post(user, "/orders", {
            "symbol": symbol, "qty": str(shares), "side": "buy",
            "type": "stop", "stop_price": str(stop_price), "time_in_force": "gtc"
        })
        if isinstance(order, dict) and "id" in order:
            state["cover_order_id"] = order["id"]
            state["current_stop_price"] = stop_price
            log(f"[{user['username']}] {symbol}: SHORT cover-stop placed at ${stop_price} (adaptive)", "monitor")
            notify_user(user, f"Short cover-stop on {symbol} at ${stop_price:.2f}", "info")
        else:
            # Round-61 pt.26: log placement failure loudly so the
            # audit's "missing_stop" finding has a diagnosable cause.
            err = (order.get("error") if isinstance(order, dict)
                   else str(order))
            log(f"[{user['username']}] {symbol}: SHORT cover-stop placement "
                f"FAILED — Alpaca returned: {err}", "monitor")

    # Round-61 pt.28: in AH mode, stop is the only thing we place.
    # Everything below — profit-target limit, trailing-stop tightening,
    # cover-fill processing, force-cover on max-hold — either depends
    # on regular-hours liquidity or risks thin-book fills. Persist the
    # state update (so newly-placed stop's cover_order_id + adaptive
    # stop price are written to disk) and return.
    if extended_hours:
        save_json(filepath, strat)
        return

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
    # Round-61 pt.18: stepped trailing (mirror of the long path). Short
    # profit = (entry - lowest) / entry; tier boundaries match (+5/+10/+20).
    lowest = state.get("lowest_price_seen") or entry
    if price < lowest:
        state["lowest_price_seen"] = price
        lowest = price
        activation_pct = rules.get("short_trail_activation_pct", 0.05)
        trail_pct = rules.get("short_trail_distance_pct", 0.05)
        stepped = rules.get("stepped_trail", True)
        if not state.get("trailing_activated") and lowest <= entry * (1 - activation_pct):
            state["trailing_activated"] = True
            log(f"[{user['username']}] {symbol}: SHORT trailing activated", "monitor")
        if state.get("trailing_activated"):
            if stepped:
                new_stop, tier, tier_trail = _compute_stepped_stop(
                    entry, lowest, default_trail=trail_pct, is_short=True,
                )
                prev_tier = state.get("profit_tier", 0)
                if tier != prev_tier:
                    state["profit_tier"] = tier
                    tier_msg = {
                        1: f"Tier 1 (short — {int(trail_pct*100)}% trail above lowest)",
                        2: "Tier 2 (SHORT BREAK-EVEN LOCKED — cover at entry)",
                        3: "Tier 3 (+10% short profit — tightened to 6% trail)",
                        4: "Tier 4 (+20% short profit — tightened to 4% trail)",
                    }.get(tier, f"Tier {tier}")
                    log(f"[{user['username']}] {symbol}: stepped trail {tier_msg}", "monitor")
                    if tier >= 2:
                        notify_user(user, f"{symbol}: {tier_msg}", "info")
                    if tier == 2 and not state.get("break_even_triggered"):
                        state["break_even_triggered"] = True
            else:
                new_stop = round(lowest * (1 + trail_pct), 2)
                tier = None
            current_stop = state.get("current_stop_price", 99999) or 99999
            # For shorts: stop moves DOWN as price falls (locks in gains)
            if new_stop < current_stop:
                old_id = state.get("cover_order_id")
                # Round-10: PATCH first, cancel-then-place fallback
                # (same pattern as the long trailing-stop fix).
                new_order = None
                if old_id:
                    patched = user_api_patch(user, f"/orders/{old_id}",
                                              {"stop_price": str(new_stop)})
                    if isinstance(patched, dict) and "id" in patched:
                        new_order = patched
                if not new_order:
                    if old_id:
                        user_api_delete(user, f"/orders/{old_id}")
                    new_order = user_api_post(user, "/orders", {
                        "symbol": symbol, "qty": str(shares), "side": "buy",
                        "type": "stop", "stop_price": str(new_stop),
                        "time_in_force": "gtc"
                    })
                if isinstance(new_order, dict) and "id" in new_order:
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
            # Record short loss for cooldown. Round-10: also set the
            # GENERAL last_loss_time so the global 60-min cooldown fires
            # for new long entries too — a short loss is still a signal
            # the regime is hostile to our edge.
            if pnl < 0:
                gpath = user_file(user, "guardrails.json")
                with strategy_file_lock(gpath):
                    guardrails = load_json(gpath) or {}
                    guardrails["last_short_loss_time"] = now_et().isoformat()
                    guardrails["last_loss_time"] = now_et().isoformat()
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
                # Round-10: cancel GTC cover-stop and target limit BEFORE
                # the market buy — otherwise up to 3 buys compete
                # (cover-stop, target-limit, market) and we can double-
                # cover (flip short to long).
                if state.get("cover_order_id"):
                    user_api_delete(user, f"/orders/{state['cover_order_id']}")
                    state["cover_order_id"] = None
                if state.get("target_order_id"):
                    user_api_delete(user, f"/orders/{state['target_order_id']}")
                    state["target_order_id"] = None
                order = user_api_post(user, "/orders", {
                    "symbol": symbol, "qty": str(shares), "side": "buy",
                    "type": "market", "time_in_force": "day"
                })
                if isinstance(order, dict) and "id" in order:
                    strat["status"] = "closed"
                    state["exit_reason"] = "max_hold_exceeded"
                    pnl = (entry - price) * shares
                    state["exit_price"] = price
                    # Round-61: force-cover is a position-closing event
                    # (same class as stop-triggered), so it must email the
                    # user — not just push. Changed from "info" → "exit"
                    # which puts it in notify.py EMAIL_TYPES.
                    notify_user(user, f"Short on {symbol} force-covered after {age_days} days. P&L ~${pnl:.2f}", "exit")
                    record_trade_close(user, symbol, strat.get("strategy", "short_sell"),
                                        price, pnl, "max_hold_exceeded", qty=shares, side="buy")
    except Exception as e:
        log(f"[{user['username']}] {symbol}: Short age check error: {e}", "monitor")

    save_json(filepath, strat)


def process_strategy_file(user, filepath, strat, extended_hours=False):
    """Round-55: when extended_hours=True, only run the trailing-stop
    raise path. Skip entry fill checks, profit-take ladder, mean-
    reversion target, earnings exit — those all require regular-hours
    liquidity. The trailing-stop raise just calls PATCH/POST on
    Alpaca's stop order API; the order still fires at next market
    open if the price crosses, locking in any after-hours pop."""
    symbol = strat["symbol"]
    state = strat.setdefault("state", {})  # ensure mutations persist in strat
    rules = strat.get("rules", {})
    strategy_type = strat.get("strategy", "trailing_stop")

    # Shorts have inverse logic — delegate to dedicated handler.
    # Round-61 pt.28: don't hard-skip AH for shorts. Instead, pass
    # ``extended_hours`` through so ``process_short_strategy`` can run
    # ONLY the initial cover-stop placement path for unprotected
    # positions (``cover_order_id`` is None or got reset by pt.24/25/26
    # liveness checks). The thin-book-risky paths (profit-target
    # limit, trailing tighten, cover-fill processing, force-cover)
    # stay regular-hours-only. See ``process_short_strategy`` docstring.
    if strategy_type == "short_sell":
        process_short_strategy(user, filepath, strat, state, rules,
                               extended_hours=extended_hours)
        return

    # Check entry fill (skip in AH — new fills are regular-hours only)
    entry_order_id = state.get("entry_order_id")
    if not extended_hours and entry_order_id and not state.get("entry_fill_price"):
        order = user_api_get(user, f"/orders/{entry_order_id}")
        if isinstance(order, dict) and order.get("status") == "filled":
            state["entry_fill_price"] = float(order.get("filled_avg_price", 0))
            filled_qty = int(float(order.get("filled_qty", 0)))
            state["total_shares_held"] = filled_qty
            # Reconcile initial_qty with the ACTUAL fill. The profit ladder
            # uses initial_qty to compute each rung's sell size
            # (initial_qty * 25%). If the intended buy was 100 shares but
            # only 75 filled, keeping initial_qty=100 would make each rung
            # sell 25 — exhausting the position after 3 rungs instead of 4.
            # Trade journal P&L also keys off initial_qty for returns math.
            intended_qty = strat.get("initial_qty")
            if filled_qty > 0 and filled_qty != intended_qty:
                log(f"[{user['username']}] {symbol}: partial entry fill "
                    f"({filled_qty}/{intended_qty}). Reconciling initial_qty → {filled_qty}.",
                    "monitor")
                strat["initial_qty"] = filled_qty
                strat["intended_qty"] = intended_qty  # keep the original for audit
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

    # Place initial stop (regular-hours only — don't spam AH quotes)
    # Round-61 pt.19: adaptive stop distance for longs too. A long whose
    # current price has dropped BELOW entry*(1-stop_pct) would produce a
    # sell-stop ABOVE current market, which Alpaca rejects (or would
    # trigger immediately if not rejected). Mirror the short-side +
    # error_recovery formula: min(entry*(1-pct), current*0.95).
    # Round-61 pt.24: verify stop_order_id is still live at Alpaca
    # before trusting it. Same pattern as the short-side cover fix —
    # a rejected/canceled stop leaves a stale id in state that
    # prevents re-placement. Reset when dead so the next block
    # places fresh.
    # Round-61 pt.25: normalized error-check structure (same shape
    # as the short-side cover fix) — check "error" FIRST to avoid
    # the gotcha where an error-dict has empty status and could be
    # missed in the wrong conditional order.
    existing_stop_id = state.get("stop_order_id")
    if existing_stop_id and not extended_hours:
        existing = user_api_get(user, f"/orders/{existing_stop_id}")
        if isinstance(existing, dict):
            if "error" in existing:
                log(f"[{user['username']}] {symbol}: stop_order_id "
                    f"{existing_stop_id} not found at Alpaca — resetting.",
                    "monitor")
                state["stop_order_id"] = None
            else:
                ex_status = str(existing.get("status") or "").lower()
                if ex_status in ("canceled", "cancelled", "rejected",
                                  "expired", "replaced", "done_for_day"):
                    log(f"[{user['username']}] {symbol}: stale stop_order_id "
                        f"{existing_stop_id} is {ex_status} — resetting.",
                        "monitor")
                    state["stop_order_id"] = None
    # Pt.26: aggressive cross-check (same as the short-side).
    if state.get("stop_order_id") and not extended_hours:
        open_orders_long = user_api_get(user, f"/orders?status=open&symbols={symbol}&limit=50")
        if isinstance(open_orders_long, list):
            open_ids_long = {o.get("id") for o in open_orders_long
                              if isinstance(o, dict)}
            if state["stop_order_id"] not in open_ids_long:
                log(f"[{user['username']}] {symbol}: stop_order_id "
                    f"{state['stop_order_id']} NOT in Alpaca open-orders "
                    "list — resetting.", "monitor")
                state["stop_order_id"] = None
    if not extended_hours and not state.get("stop_order_id"):
        stop_pct = rules.get("stop_loss_pct", 0.10)
        entry_stop = entry * (1 - stop_pct)
        current_stop = price * 0.95
        stop_price = round(min(entry_stop, current_stop), 2)
        log(f"[{user['username']}] {symbol}: placing LONG sell-stop "
            f"qty={shares} entry=${entry:.2f} current=${price:.2f} "
            f"stop=${stop_price:.2f}", "monitor")
        order = user_api_post(user, "/orders", {
            "symbol": symbol, "qty": str(shares), "side": "sell",
            "type": "stop", "stop_price": str(stop_price), "time_in_force": "gtc"
        })
        if isinstance(order, dict) and "id" in order:
            state["stop_order_id"] = order["id"]
            state["current_stop_price"] = stop_price
            log(f"[{user['username']}] {symbol}: Stop-loss placed at ${stop_price} (adaptive)", "monitor")
            notify_user(user, f"Stop-loss placed on {symbol} at ${stop_price:.2f}", "info")
        else:
            err = (order.get("error") if isinstance(order, dict)
                   else str(order))
            log(f"[{user['username']}] {symbol}: LONG sell-stop placement "
                f"FAILED — Alpaca returned: {err}", "monitor")

    # Trailing-stop exit — applied to every non-wheel entry strategy.
    # Round-10 architecture: trailing_stop is an exit policy, not an
    # entry, so breakout / pead / copy_trading / legacy-trailing_stop
    # all share the same floor-raising logic.
    # Round-61 pt.18: stepped trailing — the flat `highest * (1 - trail)`
    # formula is replaced by `_compute_stepped_stop` which returns a
    # tier-aware stop (wide at entry, break-even at +5%, tighter as
    # profit accumulates). Opt-out via rules.stepped_trail=false.
    if strategy_type in ("trailing_stop", "breakout", "copy_trading", "pead"):
        highest = state.get("highest_price_seen") or entry
        if price > highest:
            state["highest_price_seen"] = price
            highest = price
        activation = rules.get("trailing_activation_pct", 0.10 if strategy_type != "breakout" else 0)
        trail = rules.get("trailing_distance_pct", 0.05)
        stepped = rules.get("stepped_trail", True)
        if not state.get("trailing_activated") and highest >= entry * (1 + activation):
            state["trailing_activated"] = True
            log(f"[{user['username']}] {symbol}: Trailing activated", "monitor")
        if state.get("trailing_activated"):
            if stepped:
                new_stop, tier, tier_trail = _compute_stepped_stop(
                    entry, highest, default_trail=trail, is_short=False,
                )
                # First time we enter a new tier, emit an audit log +
                # user-visible notification. The subsequent Alpaca
                # PATCH/place still fires only when new_stop raises
                # above current_stop — tier transitions that DON'T
                # change the stop price stay quiet.
                prev_tier = state.get("profit_tier", 0)
                if tier != prev_tier:
                    state["profit_tier"] = tier
                    tier_msg = {
                        1: f"Tier 1 (breathing room, {int(trail*100)}% trail)",
                        2: "Tier 2 (BREAK-EVEN LOCKED — stop moved to entry)",
                        3: "Tier 3 (+10% profit — tightened to 6% trail)",
                        4: "Tier 4 (+20% profit — tightened to 4% trail)",
                    }.get(tier, f"Tier {tier}")
                    log(f"[{user['username']}] {symbol}: stepped trail {tier_msg}", "monitor")
                    if tier >= 2:
                        notify_user(user, f"{symbol}: {tier_msg}", "info")
                    if tier == 2 and not state.get("break_even_triggered"):
                        state["break_even_triggered"] = True
            else:
                new_stop = round(highest * (1 - trail), 2)
                tier = None
            current_stop = state.get("current_stop_price", 0) or 0
            if new_stop > current_stop:
                old_id = state.get("stop_order_id")
                # Round-10 audit: Alpaca rejects "place new, cancel old"
                # with HTTP 403 because having TWO sell-stops for X
                # shares when you only hold X shares is a duplicate
                # order. The correct pattern is PATCH /orders/{id} to
                # atomically bump the stop_price on the existing order.
                # Fall back to cancel-then-replace only if PATCH fails
                # (e.g. old_id is stale or order already filled).
                new_order = None
                if old_id:
                    patched = user_api_patch(user, f"/orders/{old_id}",
                                              {"stop_price": str(new_stop)})
                    if isinstance(patched, dict) and "id" in patched:
                        new_order = patched
                if not new_order:
                    # PATCH failed (or no old_id) — fall back to
                    # cancel-then-place. Cancel FIRST this time so
                    # Alpaca doesn't reject the new order as duplicate.
                    # ~200ms unprotected window is acceptable vs the
                    # current "stuck forever at deploy-time stop" bug.
                    if old_id:
                        user_api_delete(user, f"/orders/{old_id}")
                    new_order = user_api_post(user, "/orders", {
                        "symbol": symbol, "qty": str(shares), "side": "sell",
                        "type": "stop", "stop_price": str(new_stop),
                        "time_in_force": "gtc"
                    })
                if isinstance(new_order, dict) and "id" in new_order:
                    state["stop_order_id"] = new_order["id"]
                    state["current_stop_price"] = new_stop
                    _session_tag = "AH" if extended_hours else "market"
                    log(f"[{user['username']}] {symbol}: Stop raised ${current_stop:.2f} -> ${new_stop:.2f} ({_session_tag})", "monitor")
                    notify_user(user, f"Stop raised on {symbol}: ${current_stop:.2f} -> ${new_stop:.2f}", "info")
                else:
                    _session_tag = "AH" if extended_hours else "market"
                    log(f"[{user['username']}] {symbol}: WARN stop raise failed, keeping prior stop at ${current_stop:.2f}. Err: {new_order} ({_session_tag})", "monitor")
                    # Round-57: surface AH raise failures to Sentry so the
                    # operator can debug "why didn't my stop tighten at
                    # 4:30 PM?" via the Sentry UI, not just scrolling logs.
                    # Regular-hours failures also flow through here — they
                    # were previously silent too, which is wrong when a
                    # trailing stop can't be tightened during a fast rally.
                    try:
                        from observability import capture_message
                        capture_message(
                            f"trailing_stop_raise_failed: {symbol}",
                            level="warning",
                            event="trailing_stop_raise_failed",
                            session=_session_tag,
                            strategy=strategy_type,
                            symbol=symbol,
                            user_id=user.get("id"),
                            current_stop=current_stop,
                            attempted_new_stop=new_stop,
                            alpaca_response=str(new_order)[:200],
                        )
                    except Exception:
                        pass  # breadcrumb is best-effort; never block trading

    # Mean reversion target check (skip in AH — market-sell fills too
    # thin overnight; the raised trailing stop will capture the exit)
    if not extended_hours and strategy_type == "mean_reversion":
        if price >= entry * 1.15:
            # Round-10 audit: cancel the live GTC stop FIRST. If we post
            # the market sell while the stop is open, the stop remains
            # orphaned in Alpaca after shares are gone — on a short-
            # enabled account it can even open a new short equal to the
            # sold qty. Same class as SOXL orphan + trailing-stop-403.
            old_stop_id = state.get("stop_order_id")
            if old_stop_id:
                user_api_delete(user, f"/orders/{old_stop_id}")
                state["stop_order_id"] = None
            order = user_api_post(user, "/orders", {
                "symbol": symbol, "qty": str(shares), "side": "sell",
                "type": "market", "time_in_force": "day"
            })
            if isinstance(order, dict) and "id" in order:
                strat["status"] = "closed"
                state["exit_reason"] = "target_hit"
                state["exit_price"] = price
                pnl = (price - entry) * shares
                pnl_pct = ((price / entry - 1) * 100) if entry else 0
                log(f"[{user['username']}] {symbol}: Target hit. P&L ${pnl:.2f}", "monitor")
                try:
                    import notification_templates as _nt
                    _subj, _body = _nt.profit_target_hit(
                        symbol=symbol, strategy=strategy_type,
                        entry_price=entry, exit_price=price,
                        shares=shares, pnl=pnl, pnl_pct=pnl_pct,
                        reason="target_hit",
                    )
                except Exception:
                    _subj = _body = None
                notify_rich(user,
                            f"Profit taken on {symbol}: sold at ${price:.2f} (+{pnl_pct:.1f}%)",
                            "exit", rich_subject=_subj, rich_body=_body)
                record_trade_close(user, symbol, strategy_type, price, pnl,
                                    "target_hit", qty=shares, side="sell")

    # PEAD time-based exit + earnings-event guard.
    # PEAD's edge is the 30-60 day post-earnings drift; holding past
    # the window risks giving back gains AND running into the next
    # earnings event (which would re-roll the dice on a different SUE).
    # Two exit triggers:
    #   1. max_hold_days reached (default 60d) → close at market
    #   2. next earnings within exit_before_next_earnings_days (5d) →
    #      close to avoid event risk (signal recorded at deploy time
    #      from yfinance; refreshed on each PEAD scan)
    if not extended_hours and strategy_type == "pead":
        try:
            created_str = strat.get("created") or ""
            # Created is "YYYY-MM-DD" (no tz). Compare to ET date.
            created_dt = datetime.strptime(created_str, "%Y-%m-%d").date()
            days_held = (get_et_time().date() - created_dt).days
            max_hold = int(rules.get("max_hold_days", 60))
            should_exit_time = days_held >= max_hold
            should_exit_earnings = False
            sig = rules.get("pead_signal") or {}
            next_e = sig.get("next_earnings_date")
            if next_e:
                try:
                    next_dt = datetime.strptime(next_e, "%Y-%m-%d").date()
                    days_to_earnings = (next_dt - get_et_time().date()).days
                    buffer_days = int(rules.get("exit_before_next_earnings_days", 5))
                    should_exit_earnings = 0 < days_to_earnings <= buffer_days
                except Exception:
                    pass
            if should_exit_time or should_exit_earnings:
                reason = ("pead_window_complete" if should_exit_time
                          else "pre_earnings_exit")
                # Cancel the live GTC stop FIRST (see mean-reversion
                # target block for the orphan-stop rationale).
                old_stop_id = state.get("stop_order_id")
                if old_stop_id:
                    user_api_delete(user, f"/orders/{old_stop_id}")
                    state["stop_order_id"] = None
                order = user_api_post(user, "/orders", {
                    "symbol": symbol, "qty": str(shares), "side": "sell",
                    "type": "market", "time_in_force": "day"
                })
                if isinstance(order, dict) and "id" in order:
                    strat["status"] = "closed"
                    state["exit_reason"] = reason
                    state["exit_price"] = price
                    pnl = (price - entry) * shares
                    pnl_pct = ((price / entry - 1) * 100) if entry else 0
                    log(f"[{user['username']}] {symbol}: PEAD exit ({reason}, "
                        f"held {days_held}d). P&L ${pnl:.2f} ({pnl_pct:+.1f}%)", "monitor")
                    notify_user(user,
                                f"PEAD exit on {symbol}: ${price:.2f} "
                                f"({pnl_pct:+.1f}% in {days_held}d, {reason})",
                                "exit")
                    record_trade_close(user, symbol, strategy_type, price,
                                        pnl, reason, qty=shares, side="sell")
        except Exception as _e:
            log(f"[{user['username']}] {symbol}: PEAD timer check failed: {_e}", "monitor")

    # Round-29: universal pre-earnings exit for non-PEAD equity strategies.
    # PEAD has its own rule above (uses deploy-time pead_signal). Wheel
    # is deliberately excluded — short puts through earnings capture IV
    # crush, which is the wheel's profit engine. Trailing / breakout /
    # mean-reversion / copy are momentum plays where earnings is a
    # random-walk shock; we sidestep it.
    if strategy_type in ("trailing_stop", "breakout", "mean_reversion", "copy_trading"):
        try:
            gpath = user_file(user, "guardrails.json")
            _gr = load_json(gpath) or {}
            if not _gr.get("earnings_exit_disabled", False):
                days_before = int(_gr.get("earnings_exit_days_before", 1))
                import earnings_exit
                should_exit, reason, days_to = earnings_exit.should_exit_for_earnings(
                    symbol, strategy_type, days_before=days_before,
                )
                if should_exit and strat.get("status") != "closed":
                    old_stop_id = state.get("stop_order_id")
                    if old_stop_id:
                        user_api_delete(user, f"/orders/{old_stop_id}")
                        state["stop_order_id"] = None
                    order = user_api_post(user, "/orders", {
                        "symbol": symbol, "qty": str(shares), "side": "sell",
                        "type": "market", "time_in_force": "day",
                    })
                    if isinstance(order, dict) and "id" in order:
                        strat["status"] = "closed"
                        state["exit_reason"] = reason
                        state["exit_price"] = price
                        pnl = (price - entry) * shares
                        pnl_pct = ((price / entry - 1) * 100) if entry else 0
                        log(f"[{user['username']}] {symbol}: pre-earnings exit "
                            f"({reason}). P&L ${pnl:.2f} ({pnl_pct:+.1f}%)", "monitor")
                        notify_rich(
                            user,
                            f"Pre-earnings exit on {symbol}: sold at ${price:.2f} "
                            f"(${pnl:+.2f}, {pnl_pct:+.1f}%, {days_to}d to earnings)",
                            "exit",
                            rich_subject=f"Pre-earnings exit: {symbol}",
                            rich_body=(
                                f"Strategy {strategy_type} auto-closed {symbol} "
                                f"{days_to} day(s) before earnings to sidestep "
                                f"event risk.\n\n"
                                f"Entry: ${entry:.2f}\nExit: ${price:.2f}\n"
                                f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)\n\n"
                                f"Configure via guardrails.json:\n"
                                f"  earnings_exit_days_before (default 1)\n"
                                f"  earnings_exit_disabled (default false)"
                            ),
                        )
                        record_trade_close(user, symbol, strategy_type, price,
                                            pnl, reason, qty=shares, side="sell")
                    else:
                        log(f"[{user['username']}] {symbol}: WARN pre-earnings "
                            f"exit order failed: {order}", "monitor")
        except Exception as _e:
            log(f"[{user['username']}] {symbol}: earnings-exit check failed: {_e}", "monitor")

    # Feature 8: Partial profit taking (skip in AH — market-sell
    # rungs hit thin books, terrible fills)
    if not extended_hours:
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
            pnl_pct = ((exit_price / entry - 1) * 100) if entry else 0
            log(f"[{user['username']}] {symbol}: STOP TRIGGERED at ${exit_price}, P&L ${pnl:.2f}", "monitor")
            try:
                import notification_templates as _nt
                _subj, _body = _nt.stop_loss_triggered(
                    symbol=symbol, strategy=strategy_type,
                    entry_price=entry, exit_price=exit_price,
                    shares=shares, pnl=pnl, pnl_pct=pnl_pct,
                )
            except Exception:
                _subj = _body = None
            notify_rich(user,
                        f"{symbol} stopped out at ${exit_price:.2f}. P&L: ${pnl:.2f}",
                        "stop", rich_subject=_subj, rich_body=_body)
            record_trade_close(user, symbol, strategy_type, exit_price, pnl,
                                "stop_triggered", qty=shares, side="sell")
            # Round-57: hold the guardrails lock across RMW. Before this,
            # a concurrent handler POST (e.g. kill_switch toggle, calibration
            # override) could load guardrails, we overwrite with just the
            # last_loss_time field, and the handler's write is lost. Same
            # pattern as the monitor's daily_starting_value writer at ~1141.
            gpath = user_file(user, "guardrails.json")
            with strategy_file_lock(gpath):
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
    # Shared sector map — constants.py. Previously imported from
    # update_dashboard which silently fell back to {} on ImportError,
    # disabling the correlation guard entirely if the screener module
    # ever failed to import. With the dedicated constants module, the
    # import is near-impossible to fail (stdlib only), and a hard crash
    # here is preferable to a silent guardrail bypass.
    from constants import SECTOR_MAP

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

    # Also check concentration: total market value in same sector.
    # Round-10 audit: "Other" is a catch-all bucket for tickers not in
    # SECTOR_MAP. Applying the same 40% cap as real sectors was overly
    # conservative — multiple unrelated stocks (MARA crypto, HIMS
    # healthcare, TAL education) all landing in "Other" would block
    # entries that have no real correlation. Raised Other-only cap to
    # 60% while keeping real sectors at 40%. SECTOR_MAP is also being
    # expanded this round so fewer tickers end up in "Other" to begin with.
    total_value = sum(float(p.get("market_value", 0)) for p in existing_positions)
    sector_value = sum(float(p.get("market_value", 0)) for p in existing_positions
                       if SECTOR_MAP.get(p.get("symbol", ""), "Other") == new_sector)

    max_pct = 0.6 if new_sector == "Other" else 0.4
    if total_value > 0 and sector_value / total_value > max_pct:
        return False, (f"{new_sector} sector already "
                       f"{sector_value/total_value*100:.0f}% of portfolio "
                       f"(max {int(max_pct*100)}%)")

    return True, f"Sector diversification OK ({new_sector})"

def run_auto_deployer(user):
    log(f"[{user['username']}] Running auto-deployer...", "deployer")

    gpath = user_file(user, "guardrails.json")
    guardrails = load_json(gpath) or {}
    if guardrails.get("kill_switch"):
        log(f"[{user['username']}] Kill switch active. Skipping.", "deployer")
        return

    # Atomic snapshot of trading-mode + per-trade cap for this deployer
    # run. The HTTP handler thread can flip user.live_mode mid-run via
    # the Live Trading toggle; if we re-read user.get("live_mode") in
    # the per-pick loop, a paper-sized position can get routed to the
    # live account halfway through. Freeze both here and pass down so
    # every order uses the mode that was active when the deployer
    # started.
    LIVE_MODE_AT_START = bool(user.get("live_mode"))
    LIVE_MAX_DOLLARS_AT_START = float(
        user.get("live_max_position_dollars")
        or guardrails.get("live_max_position_dollars")
        or 500
    )

    config = load_json(user_file(user, "auto_deployer_config.json")) or {}
    if not config.get("enabled", True):
        log(f"[{user['username']}] Auto-deployer disabled. Skipping.", "deployer")
        return

    # Round-50: portfolio auto-calibration. Fetch Alpaca /account to
    # detect the user's true account state (cash vs margin, equity
    # tier, PDT flag, shorting enabled). Calibrates default
    # max_positions / max_position_pct / strategy mix / fractional
    # feature flags. User overrides in guardrails.json still win.
    TIER_CFG = None
    try:
        import portfolio_calibration as pc
        _acct = user_api_get(user, "/account")
        if isinstance(_acct, dict) and "error" not in _acct:
            _tier = pc.detect_tier(_acct)
            if _tier:
                TIER_CFG = pc.apply_user_overrides(_tier, guardrails)
                # Round-52: only log on state change. Deployer runs
                # 1x/day per user in normal op but Force Deploy +
                # retries can fire more often. Spamming "Calibrated
                # tier: Cash Standard..." every tick at DEBUG level
                # is pure noise — only log when the tier name
                # actually differs from the last-seen value.
                _tier_key = f"tier_last_{user.get('id')}:{user.get('_mode', 'paper')}"
                _last_tier = _last_runs.get(_tier_key)
                _current_tier_str = TIER_CFG.get("name", "?")
                if _last_tier != _current_tier_str:
                    log(f"[{user['username']}] Calibrated tier: "
                        f"{TIER_CFG.get('display')} — equity ${TIER_CFG.get('_detected_equity', 0):.0f} "
                        f"(strategies: {','.join(TIER_CFG.get('strategies_enabled', []))})",
                        "deployer")
                    _last_runs[_tier_key] = _current_tier_str
            else:
                log(f"[{user['username']}] Equity too small to auto-calibrate "
                    f"(need ≥ $500). Falling back to config defaults.",
                    "deployer")
    except Exception as _e:
        log(f"[{user['username']}] Calibration error ({_e}) — falling back to "
            "config defaults. Trades unaffected.", "deployer")

    # Round-50: fill in any MISSING guardrails values from the calibrated
    # defaults. User overrides in guardrails.json ALWAYS win — we only
    # populate keys the user hasn't explicitly set. This lets new users
    # get sensible tier-appropriate sizing without any Settings config.
    if TIER_CFG:
        for _key in ("max_positions", "max_position_pct", "min_stock_price"):
            if _key not in guardrails and _key in TIER_CFG:
                guardrails[_key] = TIER_CFG[_key]

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

    # Round-11 expansion items 11-12: Beta-adjusted exposure +
    # drawdown sizing. Initialised to open-gate defaults; the actual
    # computation happens AFTER positions + account + factor_bypass
    # are populated below (round-12 audit: this block was previously
    # placed here and referenced those vars before they existed,
    # causing every run_auto_deployer call to hit a NameError that
    # the outer `except Exception` swallowed — so the risk gate was
    # silently disabled in production for months).
    beta_exposure = {"regime": "unknown", "block_high_beta": False, "block_all": False}
    drawdown_mult = 1.0

    # Round-11 Tier 1: Market breadth gate. If fewer than 40% of S&P 500
    # components are above their 50dma, breakouts fail ~80% of the time.
    # Skip breakout + PEAD deploys in weak-breadth regimes (mean_reversion
    # and wheel strategies work fine in weak breadth — MR buys the dip,
    # wheel sells premium on range-bound names).
    # Round-11 escape hatch: factor_bypass flag in guardrails disables
    # all factor gates (breadth, RS, sector rotation, IV rank, quality,
    # bullish prioritization). Deploys fall back to raw screener scores.
    factor_bypass = bool(guardrails.get("factor_bypass"))
    weak_breadth = False
    breadth_pct_val = None
    if factor_bypass:
        log(f"[{user['username']}] FACTOR BYPASS active — skipping breadth/quality/"
            "RS/sector/IV-rank gates. Raw screener scores only.", "deployer")
    else:
        try:
            import market_breadth as _mb
            _b = _mb.get_breadth_pct(data_dir=DATA_DIR)
            breadth_pct_val = _b.get("breadth_pct")
            if breadth_pct_val is not None and breadth_pct_val < 40:
                weak_breadth = True
                log(f"[{user['username']}] Market breadth {breadth_pct_val:.0f}% < 40% — "
                    f"pausing BREAKOUT and PEAD deploys (MR + Wheel still run)", "deployer")
        except Exception as _e:
            # Breadth is a nice-to-have — never block a deploy on its error.
            log(f"[{user['username']}] breadth check failed: {_e}. Continuing without it.", "deployer")

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
    # Round-10: pipe CAPITAL_STATUS_PATH so the subprocess writes to
    # the per-user capital_status.json instead of the shared file
    # (which leaked can_trade / free-cash numbers across users).
    try:
        env = os.environ.copy()
        env["ALPACA_API_KEY"] = user["_api_key"]
        env["ALPACA_API_SECRET"] = user["_api_secret"]
        env["ALPACA_ENDPOINT"] = user["_api_endpoint"]
        env["ALPACA_DATA_ENDPOINT"] = user["_data_endpoint"]
        capital_path = user_file(user, "capital_status.json")
        env["CAPITAL_STATUS_PATH"] = capital_path
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "capital_check.py")],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=30, env=env)
        # Read from the per-user file first, falling back to shared for
        # backwards compat with pre-round-10 deploys.
        capital = load_json(capital_path)
        if not capital:
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

    # Round-12 audit fix: beta-exposure + drawdown-sizing gate, moved
    # here from earlier in the function where it was dead code (referenced
    # factor_bypass/existing_positions/portfolio_value before they were
    # defined). This is the FIRST place all three are available.
    if not factor_bypass:
        try:
            from portfolio_risk import (
                beta_adjusted_exposure, drawdown_size_multiplier,
            )
            account_d = user_api_get(user, "/account") or {}
            portfolio_value_live = float(account_d.get("portfolio_value", 0) or 0)
            beta_exposure = beta_adjusted_exposure(
                existing_positions, portfolio_value_live
            )
            log(f"[{user['username']}] Beta exposure: {beta_exposure['beta_weighted_pct']}% "
                f"beta-weighted (β={beta_exposure['portfolio_beta']}, regime={beta_exposure['regime']})",
                "deployer")
            if beta_exposure["block_all"]:
                log(f"[{user['username']}] EXTREME beta exposure — pausing all new entries", "deployer")
                # Don't return — per-pick block below enforces it.
            # Drawdown sizing
            try:
                journal = load_json(user_file(user, "trade_journal.json")) or {}
                snapshots = journal.get("daily_snapshots", [])
                drawdown_mult = drawdown_size_multiplier(snapshots)
                if drawdown_mult < 1.0:
                    log(f"[{user['username']}] Drawdown sizing active: {drawdown_mult:.2f}x "
                        "(reducing position sizes after recent losses)", "deployer")
            except Exception:
                pass
        except Exception as _re:
            log(f"[{user['username']}] portfolio_risk failed: {_re}. Continuing.", "deployer")

    sdir = user_strategies_dir(user)

    # BURN_DOWN #1: news signals integration. Build a {symbol: signal_dir}
    # map from the screener's post-market news scan output. Each entry in
    # `news_signals.actionable` has shape {symbol, score, direction}
    # where direction is "bullish" or "bearish". Used below to (a) skip
    # picks flagged bearish by strong news, and (b) bump pick priority
    # for picks flagged bullish. The existing per-pick `news_sentiment`
    # already catches most of this but the actionable list is higher-signal
    # (deeper scoring, post-market scan vs. just pick-time snapshot).
    news_map = {}
    try:
        actionable = (picks_data.get("news_signals") or {}).get("actionable") or []
        for item in actionable:
            sym = (item.get("symbol") or "").upper()
            direction = (item.get("direction") or "").lower()
            if sym and direction in ("bullish", "bearish"):
                news_map[sym] = direction
        if news_map:
            log(f"[{user['username']}] News signals in play: "
                f"{sum(1 for d in news_map.values() if d == 'bullish')} bullish, "
                f"{sum(1 for d in news_map.values() if d == 'bearish')} bearish", "deployer")
    except Exception as _e:
        log(f"[{user['username']}] news_signals parse failed: {_e}. Continuing without it.", "deployer")

    # Round-11 expansion item 6: Premarket gappers get TOP priority.
    # Reads premarket_picks.json saved by the 8:30 AM scanner. Picks
    # listed there bubble up to the front of top_picks regardless of
    # screener score (their gap+volume is real-time signal that
    # outranks yesterday's daily-bar score).
    if not factor_bypass:
        try:
            from premarket_scanner import load_premarket_picks
            udir = user["_data_dir"]
            pm_picks = load_premarket_picks(udir)
            pm_symbols = {p["symbol"] for p in pm_picks if p.get("symbol")}
            if pm_symbols:
                # Reorder top_picks so premarket gappers come first
                pm_in_picks = [p for p in top_picks if p.get("symbol") in pm_symbols]
                others = [p for p in top_picks if p.get("symbol") not in pm_symbols]
                top_picks = pm_in_picks + others
                log(f"[{user['username']}] Premarket gappers prioritized: "
                    f"{len(pm_in_picks)}/{len(pm_symbols)} matched today's screener", "deployer")
        except Exception as _pe:
            log(f"[{user['username']}] premarket prioritization failed: {_pe}", "deployer")

    # Round-11 Tier 2: Prioritize picks with bullish news catalysts. The
    # screener already added has_bullish_catalyst to each pick. Sort so
    # bullish-catalyst names are evaluated FIRST within their strategy
    # tier — the deployer stops at `max_per_day` so ordering matters.
    # Skipped when factor_bypass is active.
    if not factor_bypass:
        try:
            _bull_count = sum(1 for p in top_picks if p.get("has_bullish_catalyst"))
            if _bull_count > 0:
                top_picks = sorted(top_picks,
                                    key=lambda p: (not p.get("has_bullish_catalyst", False),
                                                   -(p.get("best_score", 0) or 0)))
                log(f"[{user['username']}] {_bull_count} candidates with bullish news "
                    f"catalysts — prioritized in queue", "deployer")
        except Exception as _e:
            log(f"[{user['username']}] bullish-news prioritization failed: {_e}", "deployer")

    log(f"[{user['username']}] Evaluating {len(top_picks)} candidates from filtered screener list", "deployer")

    for pick in top_picks:
        if deployed >= max_per_day:
            break
        # Round-12 audit fix: atomic abort on kill-switch trip. Previous
        # per-symbol disk re-read left a window where orders could still
        # fire after the switch activated; threading.Event is race-free.
        if deploy_should_abort():
            log(f"[{user['username']}] Auto-deployer ABORT: kill-switch tripped mid-loop "
                f"({deployed}/{max_per_day} deployed, {candidates_evaluated} evaluated)",
                "deployer")
            break
        symbol = pick.get("symbol")
        best_strat = pick.get("best_strategy", "").lower().replace(" ", "_")
        candidates_evaluated += 1

        if symbol in existing_syms:
            skip_reasons.append(f"{symbol}: already held")
            continue
        # Round-10 audit: PEAD explicitly wants stocks that just beat
        # earnings — blocking on earnings_warning (which fires whenever
        # the news feed contains "earnings"/"Q1 results"/etc.) would
        # make PEAD permanently undeployable. Only skip for non-PEAD.
        if pick.get("earnings_warning") and best_strat != "pead":
            log(f"[{user['username']}] {symbol}: Skipped (earnings warning) — trying next pick", "deployer")
            skip_reasons.append(f"{symbol}: earnings warning")
            continue
        # BURN_DOWN #1: bearish news → skip. A strong bearish news signal
        # is a harder stop than per-pick sentiment (higher bar to qualify
        # as "actionable" in the screener). Long strategies should not
        # ignore it.
        #
        # Round-10 architecture note: accepted entries are
        # {breakout, mean_reversion, pead}. Trailing Stop is an EXIT
        # policy attached via `exit_policy`. Wheel runs in its own
        # scheduler path. Copy Trading is currently disabled — no
        # free data provider — see update_dashboard.COPY_TRADING_ENABLED.
        accepted_entries = ("breakout", "mean_reversion", "pead")
        # Round-11: PEAD picks REQUIRE a recent earnings event. News
        # around earnings often scores bearish on single-word matches
        # ("missed guidance" / "miss" / "lowered") even when the stock
        # gapped UP on the beat. Skipping PEAD on bearish news negates
        # the strategy. The earnings_warning filter already has the
        # same PEAD carve-out; extend that here too.
        if (best_strat in accepted_entries
                and best_strat != "pead"
                and news_map.get(symbol) == "bearish"):
            log(f"[{user['username']}] {symbol}: Skipped (bearish news signal) — trying next pick", "deployer")
            skip_reasons.append(f"{symbol}: bearish news signal")
            continue
        if best_strat not in accepted_entries:
            skip_reasons.append(f"{symbol}: unsupported strategy ({best_strat})")
            continue
        # Round-20 trade-quality gates: avoid chasing tops + avoid extreme
        # volatility. Derived from the paper-trading backtest analysis
        # where EVERY top-scored Breakout pick stopped out because the
        # bot was buying the breakout day's peak and getting whipsawed.
        #
        # Gate A: don't chase picks already up >8% today. A breakout that
        # already ran 8%+ intraday is typically at the top of the move;
        # waiting for tomorrow's retest gives a better entry. Mean_
        # reversion is exempt (it wants over-extensions to fade).
        try:
            _daily_change = float(pick.get("daily_change", 0) or 0)
        except (TypeError, ValueError):
            _daily_change = 0.0
        if best_strat in ("breakout", "pead") and _daily_change > 8.0:
            log(f"[{user['username']}] {symbol}: Skipped ({best_strat} "
                f"already +{_daily_change:.1f}% today — don't chase). "
                f"Waiting for retest.", "deployer")
            skip_reasons.append(f"{symbol}: chase-block +{_daily_change:.1f}% intraday")
            continue

        # Gate B: volatility > 20% → skip. These are meme/pump-tier
        # names where a 10% stop gets hit by normal noise. The backtest
        # showed INFQ-class picks (vol 33%) stopping out in 2-3 days.
        # 20% is the cutoff for "tradable momentum" vs "unmanageable
        # noise". Mean_reversion is exempt — it profits from high vol.
        try:
            _vol = float(pick.get("volatility", 0) or 0)
        except (TypeError, ValueError):
            _vol = 0.0
        if best_strat in ("breakout", "pead") and _vol > 20.0:
            log(f"[{user['username']}] {symbol}: Skipped ({best_strat} "
                f"volatility {_vol:.1f}% > 20% threshold — too noisy "
                f"for a fixed-%-stop strategy).", "deployer")
            skip_reasons.append(f"{symbol}: volatility {_vol:.1f}% > 20%")
            continue

        # Round-11 Tier 1: breadth gate — block breakout + PEAD in weak
        # breadth regimes. MR and wheel continue since they're not
        # dependent on broad-market momentum.
        if weak_breadth and best_strat in ("breakout", "pead"):
            log(f"[{user['username']}] {symbol}: Skipped ({best_strat} in weak-breadth regime, breadth={breadth_pct_val:.0f}%)", "deployer")
            skip_reasons.append(f"{symbol}: weak breadth for {best_strat}")
            continue

        # Round-11 expansion item 11: beta-exposure gate.
        # If portfolio is already beta-extreme, skip ALL new entries.
        # If beta-high, skip only high-beta candidates (β > 1.5).
        if beta_exposure.get("block_all"):
            log(f"[{user['username']}] {symbol}: Skipped (portfolio at extreme beta-weighted exposure)", "deployer")
            skip_reasons.append(f"{symbol}: beta-extreme portfolio")
            continue
        if beta_exposure.get("block_high_beta"):
            try:
                from portfolio_risk import is_high_beta_candidate
                if is_high_beta_candidate(symbol):
                    log(f"[{user['username']}] {symbol}: Skipped (high-beta candidate during high beta-exposure regime)", "deployer")
                    skip_reasons.append(f"{symbol}: high-beta blocked")
                    continue
            except Exception:
                pass

        # Round-11 expansion item 13: correlation gate. Block if avg
        # correlation with existing positions > 0.7. Skipped during
        # factor bypass.
        if not factor_bypass and existing_positions:
            try:
                from portfolio_risk import should_block_correlation
                # Quick bars fetch: pick.bars or last 30d for symbol +
                # each position. We use whatever bars are already in
                # the picks_data — no extra API calls.
                _bars_map = {}
                _all_picks = picks_data.get("picks") or []
                for _p in _all_picks:
                    _sym = _p.get("symbol")
                    if _sym and _p.get("bars"):
                        _bars_map[_sym] = _p["bars"]
                pos_syms = [p.get("symbol", "").upper() for p in existing_positions
                             if p.get("symbol")]
                # Only run if we have bars for at least one position
                if any(s in _bars_map for s in pos_syms):
                    block, reason = should_block_correlation(
                        symbol, pos_syms, _bars_map, max_avg_corr=0.75
                    )
                    if block:
                        log(f"[{user['username']}] {symbol}: Skipped ({reason})", "deployer")
                        skip_reasons.append(f"{symbol}: high correlation")
                        continue
            except Exception:
                pass

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
        # Round-11 expansion item 12: drawdown-adaptive sizing.
        # Apply the multiplier computed at deployer start. 0.25..1.0.
        if drawdown_mult < 1.0:
            qty = max(1, int(qty * drawdown_mult))

        # Round-11 live-trading: hard cap on position dollars when live_mode.
        # Protects against sizing bugs + single-trade catastrophic losses during
        # the sensitive first weeks of real-money trading.
        if LIVE_MODE_AT_START:
            max_live_dollars = LIVE_MAX_DOLLARS_AT_START
            try:
                symbol_price = float(pick.get("price") or 0)
            except (TypeError, ValueError):
                symbol_price = 0.0
            # NaN/inf would silently pass the `> 0` check on inf side and
            # silently fail it on NaN side — either way, bypassing the
            # live-mode dollar cap. Reject both explicitly so yfinance
            # returning bad data for a delisted/halted ticker can't size
            # a live position past the per-trade safety limit.
            if symbol_price != symbol_price or symbol_price in (float("inf"), float("-inf")):
                log(f"[{user['username']}] {symbol}: LIVE skipped — invalid price "
                    f"({pick.get('price')!r})", "deployer")
                continue
            if symbol_price > 0:
                max_qty_by_cap = int(max_live_dollars / symbol_price)
                if max_qty_by_cap < qty:
                    log(f"[{user['username']}] {symbol}: LIVE cap {qty} → {max_qty_by_cap} shares "
                        f"(max ${max_live_dollars:.0f} per live position)", "deployer")
                    qty = max_qty_by_cap

        if qty < 1:
            log(f"[{user['username']}] {symbol}: Skipped (recommended_shares < 1)", "deployer")
            continue

        # Re-check kill_switch immediately before placing each order. A
        # concurrent run_guardrails_check() can trip the switch mid-run
        # (e.g. daily-loss limit crossed by another fill); without this
        # check the deployer keeps firing new orders past the limit.
        # Re-read from disk so we see writes from the guardrails thread.
        _gr_now = load_json(gpath) or {}
        if _gr_now.get("kill_switch"):
            log(f"[{user['username']}] {symbol}: kill_switch tripped mid-run "
                f"({_gr_now.get('kill_switch_reason','?')}) — aborting deployer", "deployer")
            return

        # Round-51: settled-funds gate for cash accounts. Block deploys
        # that would exhaust settled cash → Good Faith Violation risk.
        # Skipped for margin accounts (no T+1 constraint).
        _pick_price = float(pick.get("current_price") or pick.get("price") or 0)
        _desired_spend = _pick_price * qty if _pick_price > 0 else 0
        if TIER_CFG and _desired_spend > 0 and TIER_CFG.get("settled_funds_required"):
            try:
                import settled_funds as _sf
                _total_cash = float(TIER_CFG.get("_detected_cash") or 0)
                _ok, _usable, _reason = _sf.can_deploy(
                    user, _desired_spend, _total_cash, tier_cfg=TIER_CFG)
                if not _ok:
                    log(f"[{user['username']}] {symbol}: Skipped — "
                        f"{_reason}", "deployer")
                    skip_reasons.append(f"{symbol}: settled-funds insufficient")
                    continue
            except Exception as _sf_e:
                log(f"[{user['username']}] settled-funds check error "
                    f"({_sf_e}) — allowing deploy", "deployer")

        # Round-51: fractional routing. When the calibrated tier enables
        # fractional AND the symbol is fractionable, re-size the position
        # as a fractional qty (e.g. 0.1234 shares) so small accounts can
        # participate in any stock regardless of price.
        _use_fractional = False
        if TIER_CFG and TIER_CFG.get("fractional_default") and _pick_price > 0:
            try:
                import fractional as _fr
                _target = _pick_price * qty
                _size_result = _fr.size_position(
                    symbol, target_dollars=_target, price=_pick_price,
                    user=user, tier_cfg=TIER_CFG,
                    api_get_fn=lambda _u, _p: user_api_get(_u, _p))
                if _size_result.get("qty", 0) > 0 and _size_result.get("fractional"):
                    qty = _size_result["qty"]
                    _use_fractional = True
                    log(f"[{user['username']}] {symbol}: fractional sizing "
                        f"{qty} shares (${_size_result['notional']:.2f})",
                        "deployer")
            except Exception as _fr_e:
                log(f"[{user['username']}] fractional routing error "
                    f"({_fr_e}) — falling back to whole shares", "deployer")

        # Round-11: smart limit-at-mid order with 90s timeout + market
        # fallback. Saves 0.1-0.5% slippage per round-trip when going
        # live. Set SMART_ORDERS=0 in Railway env to disable (defaults
        # to enabled).
        if os.environ.get("SMART_ORDERS", "1") == "1":
            try:
                from smart_orders import place_smart_buy
                _ep = user.get("_api_endpoint") or API_ENDPOINT
                _data_ep = user.get("_data_endpoint") or DATA_ENDPOINT
                # The existing user_api_* helpers accept full URLs too,
                # so wrap them as the HTTP-style functions smart_orders expects
                _ag = lambda u, **kw: user_api_get(user, u)
                _ap = lambda u, body=None: user_api_post(user, u, body)
                _ad = lambda u: user_api_delete(user, u)
                order = place_smart_buy(
                    _ag, _ap, _ad, _ep, _data_ep,
                    symbol, qty, headers=None,
                    timeout_sec=int(os.environ.get("SMART_ORDER_TIMEOUT", "90")),
                    max_spread_pct=float(os.environ.get("SMART_MAX_SPREAD", "0.005")),
                    client_order_id=f"deploy-{symbol}-{now_et().strftime('%Y%m%d%H%M%S')}",
                    fractional=_use_fractional,  # round-51: market-only for fractional
                )
            except Exception as _smart_err:
                log(f"[{user['username']}] {symbol}: smart-order failed ({_smart_err}) — market fallback", "deployer")
                order = user_api_post(user, "/orders", {
                    "symbol": symbol, "qty": str(qty), "side": "buy",
                    "type": "market", "time_in_force": "day"
                })
        else:
            order = user_api_post(user, "/orders", {
                "symbol": symbol, "qty": str(qty), "side": "buy",
                "type": "market", "time_in_force": "day"
            })

        if isinstance(order, dict) and "id" in order:
            # Round-10 architecture: every non-Wheel entry uses a
            # trailing-stop exit. The `exit_policy` field documents
            # that; the monitor already raises the floor on any state
            # with `stop_order_id` + `highest_price_seen`, so nothing
            # in the monitor needs to know which entry strategy opened
            # the position — the exit logic is shared.
            # Per-strategy tuning:
            #   breakout       — tight 5% stop, immediate trail (high
            #                     conviction, fails fast if it fails)
            #   mean_reversion — wider 10% stop, +10% trail trigger
            #                     (volatile setup, give it room)
            #   pead           — 8% stop, +8% trail trigger, 8% trail
            #                     distance, 60-day max hold (PEAD drift
            #                     window — Bernard & Thomas 1989)
            is_breakout = best_strat == "breakout"
            is_pead = best_strat == "pead"
            # Round-11 Tier 1: volatility-aware stops via ATR. If the
            # screener attached atr_pct to the pick (bars had ≥15 days
            # of history), use 2.5× ATR% as the stop distance clamped
            # to [5%, 15%]. Falls back to the strategy's legacy fixed
            # stop when ATR isn't available (fresh IPOs, illiquid names).
            _atr_pct_val = float(pick.get("atr_pct", 0) or 0)
            _atr_stop_pct = None
            if _atr_pct_val > 0:
                try:
                    from risk_sizing import atr_based_stop_pct as _abs
                    # multiplier 2.5 is standard; breakouts use tighter
                    # 2.0 to fail fast on failed breakouts, PEAD uses
                    # 2.5 to ride the full 30-60d drift.
                    mult = 2.0 if is_breakout else 2.5
                    floor = 0.05 if is_breakout else 0.06
                    # Re-compute from bars would be cleanest but we
                    # already have atr_pct; recover stop from it.
                    raw_stop = mult * _atr_pct_val
                    _atr_stop_pct = round(max(floor, min(0.15, raw_stop)), 4)
                except Exception:
                    _atr_stop_pct = None
            if is_pead:
                rules = {
                    "stop_loss_pct": _atr_stop_pct if _atr_stop_pct else 0.08,
                    "trailing_activation_pct": 0.08,
                    "trailing_distance_pct": _atr_stop_pct if _atr_stop_pct else 0.08,
                    "exit_policy": "trailing_stop",
                    "max_hold_days": 60,  # PEAD drift window
                    "exit_before_next_earnings_days": 5,
                    "pead_signal": pick.get("pead_signal"),
                    "atr_pct": _atr_pct_val,  # for audit/debug
                    "stop_source": "atr" if _atr_stop_pct else "fixed",
                }
            else:
                _fallback = 0.05 if is_breakout else 0.10
                _stop = _atr_stop_pct if _atr_stop_pct else _fallback
                rules = {
                    "stop_loss_pct": _stop,
                    "trailing_activation_pct": 0 if is_breakout else 0.10,
                    "trailing_distance_pct": min(_stop, 0.08),  # trail tighter than initial stop
                    "exit_policy": "trailing_stop",
                    "atr_pct": _atr_pct_val,
                    "stop_source": "atr" if _atr_stop_pct else "fixed",
                }
            strat_file = {
                "symbol": symbol,
                "strategy": best_strat,
                "created": now_et().strftime("%Y-%m-%d"),
                "status": "awaiting_fill",
                "entry_price_estimate": pick.get("price"),
                "initial_qty": qty,
                "deployer": "cloud_scheduler",
                "rules": rules,
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
            # Rich email with strategy explainer + what-happens-next
            # context, short push for mobile alert.
            try:
                import notification_templates as _nt
                entry_px = float(pick.get("price", 0) or 0)
                stop_pct = rules.get("stop_loss_pct", 0.05)
                _subj, _body = _nt.position_opened(
                    symbol=symbol, strategy=best_strat, shares=qty,
                    entry_price=entry_px,
                    stop_price=round(entry_px * (1 - stop_pct), 2) if entry_px else None,
                    reasoning={"best_score": pick.get("best_score"),
                               "momentum_20d": pick.get("momentum_20d")},
                )
            except Exception as _e:
                log(f"[{user['username']}] Template build failed: {_e}", "deployer")
                _subj = _body = None
            notify_rich(user,
                        f"Deployed {best_strat} on {symbol}: {qty} shares @ ~${entry_px:.2f}",
                        "trade", rich_subject=_subj, rich_body=_body)
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
    # Round-52 CRITICAL fix: gate on calibrated tier's short_enabled too.
    # Before this, a cash-account user with short_selling.enabled=true in
    # auto_deployer_config.json could trigger short-sell orders that
    # Alpaca then rejected server-side (shorts require margin + ≥$2k
    # equity per Alpaca rule). Wasted deploy slot + noisy errors.
    # Fail-closed locally: if tier says shorts disabled, skip the block.
    if TIER_CFG is not None and not TIER_CFG.get("short_enabled", False):
        if short_config.get("enabled"):
            log(f"[{user['username']}] Short selling skipped — "
                f"{TIER_CFG.get('display', 'current tier')} does not permit "
                "shorts (Alpaca rule: margin account + ≥$2k equity required)",
                "deployer")
        short_config = {}  # disable for the rest of this deploy run
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
def run_orphan_adoption(user):
    """Round-61 pt.15: per-user orphan adoption.

    Finds Alpaca positions that have no matching strategy file and
    synthesizes one so `monitor_strategies` starts managing them
    (stops, profit-take ladders, etc.). Long positions get a
    `trailing_stop_<SYM>.json` with a 10% stop below entry; shorts
    get a `short_sell_<SYM>.json` with a 10% stop above entry. See
    `error_recovery.create_orphan_strategy` for the schema.

    Previously this only ran once per day inside `run_daily_close`.
    That left a gap of up to 23.5 hours during which a position
    opened outside the bot (or whose strategy file got cleaned up /
    renamed by an earlier close path) would sit as MANUAL with no
    stop-management. Now it runs every 10 min during market hours +
    on-demand via the dashboard's 'Adopt MANUAL Positions' button.

    Runs as a subprocess so the scheduler loop keeps its heartbeat
    alive even if error_recovery.py hangs on a slow Alpaca response.
    Per-user isolation via env vars (ALPACA_* + DATA_DIR +
    STRATEGIES_DIR), same pattern as run_daily_close.
    """
    try:
        env = os.environ.copy()
        env["ALPACA_API_KEY"] = user["_api_key"]
        env["ALPACA_API_SECRET"] = user["_api_secret"]
        env["ALPACA_ENDPOINT"] = user["_api_endpoint"]
        env["ALPACA_DATA_ENDPOINT"] = user["_data_endpoint"]
        env["DATA_DIR"] = user.get("_data_dir") or DATA_DIR
        env["STRATEGIES_DIR"] = user.get("_strategies_dir") or os.path.join(
            user.get("_data_dir") or DATA_DIR, "strategies"
        )
        env["JOURNAL_PATH"] = user_file(user, "trade_journal.json")
        result = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "error_recovery.py")],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=60, env=env,
        )
        # Parse "FIXED: Created <file>" lines from stdout so we can
        # log a tight summary instead of dumping the whole subprocess
        # output into the scheduler log on every 10-min tick.
        created = [ln for ln in (result.stdout or "").splitlines()
                   if "FIXED: Created" in ln]
        if created:
            log(f"[{user['username']}] orphan_adoption: "
                f"adopted {len(created)} position(s) to AUTO — "
                f"{', '.join(ln.split('Created ')[-1].split(' ')[0] for ln in created)}",
                "recovery")
            notify_user(user, f"Adopted {len(created)} MANUAL position(s) into "
                              "AUTO management. Stops placed by the bot.",
                        "info")
        return {"created": len(created), "lines": created,
                "stdout": (result.stdout or "")[:4000]}
    except subprocess.TimeoutExpired:
        log(f"[{user['username']}] orphan_adoption: subprocess timed out (60s)",
            "recovery")
        return {"error": "timeout"}
    except Exception as e:
        log(f"[{user['username']}] orphan_adoption failed: {e}", "recovery")
        return {"error": str(e)}


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
        # Round-9 fix: tell the subprocess WHICH user's scorecard +
        # trade_journal to write. Without these env vars the subprocess
        # fell through to the shared /data/scorecard.json and /data/
        # trade_journal.json legacy paths, which is what caused the
        # divergence I observed today (shared file fresh, per-user file
        # stale by over an hour). update_scorecard.py reads these from
        # os.environ at import time.
        env["SCORECARD_PATH"] = user_file(user, "scorecard.json")
        env["JOURNAL_PATH"] = user_file(user, "trade_journal.json")
        env["STRATEGIES_DIR"] = user.get("_strategies_dir") or os.path.join(
            user.get("_data_dir") or DATA_DIR, "strategies"
        )
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "update_scorecard.py")],
                      cwd=BASE_DIR, capture_output=True, text=True, timeout=60, env=env)
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "error_recovery.py")],
                      cwd=BASE_DIR, capture_output=True, text=True, timeout=60, env=env)

        # Round-57: fetch /account OUTSIDE the lock (slow network call,
        # 100-500ms; holding a flock that long blocks the monitor +
        # concurrent handlers). Then do the RMW under the lock so the
        # daily_starting_value clear + peak update is atomic vs.
        # concurrent handler writes.
        account = user_api_get(user, "/account")
        gpath = user_file(user, "guardrails.json")
        with strategy_file_lock(gpath):
            guardrails = load_json(gpath) or {}
            daily_starting_value = guardrails.get("daily_starting_value")
            guardrails["daily_starting_value"] = None
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

        # Short push for mobile (ntfy). --push-only so the auto-queued
        # email is skipped — we queue a much richer email below.
        short = f"Daily close: ${value:,.2f} | Win {win_rate:.0f}% | Ready {readiness}/100{ready_flag}"
        try:
            push_env = os.environ.copy()
            if user.get("_ntfy_topic"):
                push_env["NTFY_TOPIC"] = user["_ntfy_topic"]
            subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, "notify.py"),
                 "--type", "daily", "--push-only", short],
                env=push_env,
            )
        except Exception as _e:
            log(f"[{user['username']}] Daily close push failed: {_e}", "close")

        # Rich email report — gathered from account, positions, orders,
        # trade journal, strategies, guardrails. Queued directly to the
        # user's notification_email so users get a useful end-of-day
        # digest instead of a single-line scoreboard.
        try:
            report_body = _build_daily_close_report(
                user=user,
                account=account if isinstance(account, dict) else {},
                scorecard=scorecard,
                guardrails=guardrails,
                daily_starting_value=daily_starting_value,
            )
            _queue_direct_email(
                user,
                subject=f"[Trading Bot] Daily Close — {get_et_time().strftime('%a %b %d')}",
                body=report_body,
                notify_type="daily",
            )
        except Exception as _e:
            log(f"[{user['username']}] Rich daily-close email build failed: {_e}", "close")

        log(f"[{user['username']}] Daily close complete", "close")
    except Exception as e:
        log(f"[{user['username']}] Daily close error: {e}", "close")


# ============================================================================
# Daily-close rich report
# ============================================================================
def _fmt_money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "$—"


def _fmt_pct(v, decimals=2):
    try:
        return f"{float(v):+.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_signed_money(v):
    try:
        f = float(v)
        sign = "+" if f >= 0 else "−"
        return f"{sign}${abs(f):,.2f}"
    except (TypeError, ValueError):
        return "$—"


def _queue_direct_email(user, subject, body, notify_type="daily"):
    """Append one email entry to the user's email_queue.json with fcntl
    locking so we don't race with notify.queue_email or the drain task.
    """
    import fcntl
    queue_file = user_file(user, "email_queue.json")
    lock_file = queue_file + ".lock"
    to_addr = (user.get("_notification_email")
               or user.get("notification_email")
               or user.get("email") or "").strip()
    if not to_addr:
        log(f"[{user.get('username','?')}] No notification_email — skipping rich email", "close")
        return
    lock_fd = None
    try:
        os.makedirs(os.path.dirname(queue_file) or ".", exist_ok=True)
        lock_fd = open(lock_file, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        queue = []
        if os.path.exists(queue_file):
            try:
                with open(queue_file) as f:
                    queue = json.load(f)
            except (json.JSONDecodeError, OSError):
                queue = []
        queue.append({
            "timestamp": now_et().isoformat(),
            "to": to_addr,
            "subject": subject,
            "body": body,
            "type": notify_type,
            "sent": False,
        })
        queue = queue[-50:]  # bound the queue
        tmp = queue_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(queue, f, indent=2, default=str)
        os.replace(tmp, queue_file)
    finally:
        if lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


def _build_daily_close_report(user, account, scorecard, guardrails,
                              daily_starting_value=None):
    """Render a plain-text end-of-day digest for the user's email.

    Sections: portfolio snapshot, positions breakdown, today's activity,
    strategy health, readiness, tomorrow's context, bot health. Gracefully
    degrades each section when upstream data is missing.
    """
    et = get_et_time()
    divider = "━" * 44
    lines = []

    # ===== Header =====
    lines.append(f"DAILY CLOSE SUMMARY")
    lines.append(f"{et.strftime('%A, %B %d, %Y')} — market closed at 4:00 PM ET")
    lines.append("")

    # ===== Portfolio =====
    lines.append(divider)
    lines.append("PORTFOLIO")
    lines.append(divider)
    close_val = float(account.get("portfolio_value", scorecard.get("current_value", 0)) or 0)
    last_equity = float(account.get("last_equity", 0) or 0)
    # Round-61 pt.27: prefer Alpaca's `last_equity` (yesterday's close)
    # as the "Today" baseline. User-reported: email showed
    # "Today: +$39.29" on a day when total unrealized was +$750
    # — clearly wrong. Root cause: `daily_starting_value` gets
    # captured by the monitor's FIRST tick of the day, which can be
    # WELL AFTER market open (e.g. after a deploy/restart crossing
    # the bell). That misses pre-capture intraday movement.
    # `last_equity` is Alpaca's canonical yesterday-close value and
    # always reflects the full trading day. Fall back to
    # daily_starting_value only if last_equity is missing
    # (defensive — Alpaca always populates it).
    start_val = last_equity if last_equity else (
        float(daily_starting_value) if daily_starting_value else 0.0)
    day_chg = close_val - start_val if start_val else 0.0
    day_pct = (day_chg / start_val * 100) if start_val else 0.0
    peak = float(guardrails.get("peak_portfolio_value", close_val) or 0)
    dd_pct = ((close_val - peak) / peak * 100) if peak else 0.0

    lines.append(f"Closing value:     {_fmt_money(close_val)}")
    lines.append(f"Today:             {_fmt_signed_money(day_chg)} ({_fmt_pct(day_pct)})")
    lines.append(f"Peak portfolio:    {_fmt_money(peak)}")
    if dd_pct < 0:
        lines.append(f"Drawdown:          {_fmt_pct(dd_pct)} from peak")
    lines.append(f"Cash available:    {_fmt_money(account.get('cash', 0))}")
    lines.append(f"Buying power:      {_fmt_money(account.get('buying_power', 0))}")
    lines.append("")

    # ===== Positions =====
    positions = user_api_get(user, "/positions")
    if not isinstance(positions, list):
        positions = []
    total_unrealized = sum(float(p.get("unrealized_pl", 0) or 0) for p in positions)
    lines.append(divider)
    lines.append(f"POSITIONS HELD ({len(positions)})")
    lines.append(divider)
    if positions:
        # Round-56: OCC option symbols ("HIMS260508P00027000") are 17-18
        # chars. Previously `{sym:<6}` truncated visually + labelled them
        # "sh" (shares) when they're actually contracts. And short positions
        # printed with a bare negative qty ("-1 sh") which looks like bad
        # data. Display the underlying + expiry + strike + right,
        # label contracts correctly, and prefix shorts with "short".
        from error_recovery import _is_occ_option_symbol, _occ_underlying
        import re as _re_occ

        def _display_label(sym, qty):
            """Return (display_sym, qty_suffix).
            Examples:
              ("AAPL", 100)                → ("AAPL",                 "100 sh")
              ("AAPL", -10)                → ("AAPL",                 "short 10 sh")
              ("HIMS260508P00027000", -1)  → ("HIMS put 260508 $27", "short 1 contract")
              ("SOXL", 117)                → ("SOXL",                 "117 sh")
            """
            try:
                q = float(qty)
            except (TypeError, ValueError):
                q = 0
            abs_q_int = abs(int(q)) if q == int(q) else abs(q)
            if _is_occ_option_symbol(sym):
                u = _occ_underlying(sym) or "?"
                m = _re_occ.match(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$",
                                   sym or "")
                if m:
                    expiry = m.group(2)
                    right = "call" if m.group(3) == "C" else "put"
                    try:
                        strike = int(m.group(4)) / 1000.0
                        strike_s = f"${strike:g}"
                    except ValueError:
                        strike_s = "?"
                    disp = f"{u} {right} {expiry} {strike_s}"
                else:
                    disp = u
                noun = "contract" if abs_q_int == 1 else "contracts"
                prefix = "short " if q < 0 else ""
                return disp, f"{prefix}{abs_q_int} {noun}"
            # Equity
            noun = "sh"
            prefix = "short " if q < 0 else ""
            return sym, f"{prefix}{abs_q_int} {noun}"

        scored = []
        for p in positions:
            try:
                pnl_pct = float(p.get("unrealized_plpc", 0) or 0) * 100
                pnl_abs = float(p.get("unrealized_pl", 0) or 0)
                scored.append((pnl_pct, pnl_abs, p.get("symbol", "?"), p.get("qty", 0)))
            except (TypeError, ValueError):
                continue
        scored.sort(reverse=True)  # high to low
        winners = [s for s in scored if s[0] > 0][:3]
        losers = [s for s in reversed(scored) if s[0] < 0][:3]
        if winners:
            lines.append("Top winners:")
            for pct, abs_pnl, sym, qty in winners:
                disp, qty_s = _display_label(sym, qty)
                lines.append(f"  • {disp:<22} {_fmt_pct(pct)}  {_fmt_signed_money(abs_pnl)}  ({qty_s})")
        if losers:
            lines.append("Top losers:")
            for pct, abs_pnl, sym, qty in losers:
                disp, qty_s = _display_label(sym, qty)
                lines.append(f"  • {disp:<22} {_fmt_pct(pct)}  {_fmt_signed_money(abs_pnl)}  ({qty_s})")
        lines.append(f"Total unrealized:  {_fmt_signed_money(total_unrealized)}")
    else:
        lines.append("No open positions at close.")
    lines.append("")

    # ===== Today's activity (from Alpaca closed orders) =====
    lines.append(divider)
    lines.append("TODAY'S ACTIVITY")
    lines.append(divider)
    today_str = et.strftime("%Y-%m-%d")
    # Round-11: build midnight-ET-in-UTC boundary from zoneinfo so the
    # daily-close report correctly includes today's orders during BOTH
    # EDT (midnight ET = 04:00Z) AND EST (midnight ET = 05:00Z). The
    # prior hardcoded `T04:00:00Z` was correct Apr–Nov and off by one
    # day Nov–Mar (pulled yesterday's orders in too).
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt, time as _time, timezone as _tz
        _midnight_et = _dt.combine(et.date(), _time(0), tzinfo=ZoneInfo("America/New_York"))
        _after_iso = _midnight_et.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        _after_iso = f"{today_str}T04:00:00Z"  # fall back to prior behavior
    try:
        orders = user_api_get(user, f"/orders?status=closed&after={_after_iso}&limit=500")
    except Exception:
        orders = None
    if not isinstance(orders, list):
        orders = []
    filled = [o for o in orders if o.get("status") == "filled"]
    buys = sum(1 for o in filled if (o.get("side") or "").lower() == "buy")
    sells = sum(1 for o in filled if (o.get("side") or "").lower() == "sell")
    buys_label = f"{buys} buy" + ("" if buys == 1 else "s")
    sells_label = f"{sells} sell" + ("" if sells == 1 else "s")
    lines.append(f"Trades filled:     {len(filled)}  ({buys_label}, {sells_label})")

    # Realized P&L from today's closed journal entries
    try:
        journal = load_json(user_file(user, "trade_journal.json")) or {}
        trades = journal.get("trades", [])
        today_closes = [t for t in trades
                        if t.get("status") == "closed"
                        and (t.get("exit_timestamp") or "").startswith(today_str)]
        realized = sum(float(t.get("pnl", 0) or 0) for t in today_closes)
        wins = sum(1 for t in today_closes if float(t.get("pnl", 0) or 0) > 0)
        losses = sum(1 for t in today_closes if float(t.get("pnl", 0) or 0) < 0)
        lines.append(f"Realized P&L:      {_fmt_signed_money(realized)}")
        if today_closes:
            wr = (wins / len(today_closes) * 100) if today_closes else 0
            lines.append(f"Closed trades:     {wins}W / {losses}L  ({wr:.0f}% win rate)")
        else:
            lines.append("Closed trades:     none today")
    except Exception as _e:
        lines.append(f"Realized P&L:      (journal unavailable: {_e})")
    lines.append("")

    # ===== Strategies =====
    lines.append(divider)
    lines.append("STRATEGY HEALTH")
    lines.append(divider)
    try:
        sdir = user_strategies_dir(user)
        strat_counts = {}
        active = 0
        if os.path.isdir(sdir):
            for fn in os.listdir(sdir):
                if not fn.endswith(".json"):
                    continue
                try:
                    s = load_json(os.path.join(sdir, fn)) or {}
                    if s.get("status") in (None, "active", "open"):
                        active += 1
                    stype = s.get("strategy") or s.get("type") or "unknown"
                    strat_counts[stype] = strat_counts.get(stype, 0) + 1
                except Exception:
                    continue
        if strat_counts:
            mix = ", ".join(f"{v} {k}" for k, v in sorted(strat_counts.items()))
            lines.append(f"Active strategies: {active} ({mix})")
        else:
            lines.append("Active strategies: none")
    except Exception as _e:
        lines.append(f"Active strategies: (read failed: {_e})")
    kill = guardrails.get("kill_switch_active") or guardrails.get("kill_switch") or False
    lines.append(f"Kill switch:       {'ON — all trading halted' if kill else 'off'}")
    cooldowns = guardrails.get("cooldowns") or {}
    if cooldowns:
        active_cd = [sym for sym, ts in cooldowns.items() if ts and ts > time.time()]
        if active_cd:
            lines.append(f"Cooldowns active:  {len(active_cd)} ({', '.join(active_cd[:6])})")
    lines.append("")

    # ===== Readiness =====
    lines.append(divider)
    lines.append("READINESS FOR LIVE TRADING")
    lines.append(divider)
    win_rate = float(scorecard.get("win_rate_pct", 0) or 0)
    readiness = int(scorecard.get("readiness_score", 0) or 0)
    total_trades = int(scorecard.get("total_trades", 0) or 0)
    days_tracked = int(scorecard.get("days_tracked", 0) or 0)
    lines.append(f"Score:             {readiness} / 100")
    lines.append(f"Win rate:          {win_rate:.0f}% ({total_trades} total trades)")
    lines.append(f"Days tracked:      {days_tracked}")
    if readiness >= 80:
        lines.append("✅ READY FOR LIVE — review the 30-day checklist before going live.")
    else:
        gap = 80 - readiness
        lines.append(f"{gap} points away from GREEN. Keep the paper run going.")
    lines.append("")

    # ===== Open orders going into tomorrow =====
    lines.append(divider)
    lines.append("GOING INTO TOMORROW")
    lines.append(divider)
    try:
        open_orders = user_api_get(user, "/orders?status=open&limit=200")
    except Exception:
        open_orders = None
    if not isinstance(open_orders, list):
        open_orders = []
    if open_orders:
        lines.append(f"Open orders carried over: {len(open_orders)}")
        for o in open_orders[:8]:
            sym = o.get("symbol", "?")
            side = (o.get("side") or "").lower()
            typ = (o.get("type") or "market").lower()
            qty = o.get("qty") or o.get("notional") or "?"
            px = o.get("limit_price") or o.get("stop_price") or "market"
            px_fmt = _fmt_money(px) if px != "market" else "market"
            lines.append(f"  • {sym:<6} {side:<4} {typ:<6} {qty} @ {px_fmt}")
        if len(open_orders) > 8:
            lines.append(f"  … +{len(open_orders) - 8} more")
    else:
        lines.append("No open orders — fresh start tomorrow.")
    lines.append("")

    # ===== Bot health =====
    lines.append(divider)
    lines.append("BOT HEALTH")
    lines.append(divider)
    last_beat = _last_runs.get("heartbeat")
    if last_beat:
        try:
            secs = max(0, int(time.time() - float(last_beat)))
            lines.append(f"Scheduler:         ✓ running  (heartbeat {secs}s ago)")
        except Exception:
            lines.append("Scheduler:         ✓ running")
    else:
        lines.append("Scheduler:         ✓ running")
    lines.append("")

    # ===== Footer =====
    dash = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    if dash:
        lines.append(f"View full dashboard: {dash}")
    lines.append("To stop these emails, open Settings → Notifications and clear the address.")

    return "\n".join(lines)

# ============================================================================
# ROUND-11 LIVE-BATCH 4: DAILY SCORECARD EMAIL DIGEST (per user, 4:30 PM ET)
# ============================================================================
def run_scorecard_digest(user):
    """Send the opt-in daily scorecard email. Skip if user hasn't opted in."""
    # Only run for users who opted in
    try:
        import auth as _auth
        u = _auth.get_user_by_id(user["id"]) or {}
        if not u.get("scorecard_email_enabled"):
            return  # silent skip — not opted in
    except Exception as e:
        log(f"[{user['username']}] scorecard_digest: user lookup failed: {e}", "digest")
        return

    log(f"[{user['username']}] Building scorecard digest...", "digest")
    # Load scorecard + journal
    udir = user["_data_dir"]
    scorecard = load_json(os.path.join(udir, "scorecard.json")) or {}
    journal = load_json(os.path.join(udir, "trade_journal.json")) or {}
    # Today's trades (filter by today's ET date)
    today = now_et().strftime("%Y-%m-%d")
    today_trades = [
        t for t in (journal.get("trades") or [])
        if (t.get("timestamp") or "")[:10] == today
    ]
    # Live positions + account
    account = user_api_get(user, "/account")
    positions = user_api_get(user, "/positions")
    if not isinstance(positions, list):
        positions = []

    # Build digest
    try:
        import notification_templates as _nt
        subject, body = _nt.scorecard_digest(
            user["username"], scorecard, today_trades, positions, account
        )
    except Exception as e:
        log(f"[{user['username']}] scorecard_digest: template failed: {e}", "digest")
        return

    # Queue email. Uses same pattern as other rich emails.
    try:
        _queue_direct_email(user, subject, body)
        log(f"[{user['username']}] Scorecard digest queued to {user.get('_notification_email')}", "digest")
    except Exception as e:
        log(f"[{user['username']}] scorecard_digest: queue failed: {e}", "digest")


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

    # Round-10: respect kill switch. Without this, Friday trim would
    # transact on positions the auto-kill flatten missed or the user
    # re-opened.
    gpath = user_file(user, "guardrails.json")
    guardrails = load_json(gpath) or {}
    if guardrails.get("kill_switch"):
        log(f"[{user['username']}] Friday trim skipped (kill switch active)", "friday")
        return

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

        # Round-11: idempotency key so a timeout-retry doesn't double-trim.
        _today_str = get_et_time().strftime("%Y%m%d")
        order = user_api_post(user, "/orders", {
            "symbol": symbol,
            "qty": str(half_qty),
            "side": "sell" if float(pos.get("qty", 0)) > 0 else "buy",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": f"friday-{symbol}-{_today_str}",
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
            # Round-61 pt.8 audit fix: RMW on the strategy file MUST hold
            # `strategy_file_lock` so the 60s monitor loop can't interleave
            # a read between our load and save (clobbers `friday_trims`
            # history + `total_shares_held` reset). Friday trim fires once
            # a week at 3:45 PM ET, so holding the lock across the Alpaca
            # PATCH/DELETE is acceptable throughput-wise (~200ms, once).
            try:
                for sf in os.listdir(sdir):
                    if not (sf.endswith(f"_{symbol}.json") and not sf.startswith("wheel_")):
                        continue
                    sf_path = os.path.join(sdir, sf)
                    with strategy_file_lock(sf_path):
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
                            # Round-10: PATCH qty atomically; fall back to
                            # cancel-then-place. Previous path hit the
                            # duplicate-order 403 on every Friday trim.
                            patched = user_api_patch(user, f"/orders/{old_stop}",
                                                      {"qty": str(remaining)})
                            new_stop_resp = patched if (isinstance(patched, dict)
                                                        and "id" in patched) else None
                            if not new_stop_resp:
                                user_api_delete(user, f"/orders/{old_stop}")
                                new_stop_resp = user_api_post(user, "/orders", {
                                    "symbol": symbol, "qty": str(remaining),
                                    "side": new_stop_side,
                                    "type": "stop", "stop_price": str(stop_price),
                                    "time_in_force": "gtc",
                                })
                            if isinstance(new_stop_resp, dict) and "id" in new_stop_resp:
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

    # Round-10: respect kill switch.
    gpath = user_file(user, "guardrails.json")
    guardrails = load_json(gpath) or {}
    if guardrails.get("kill_switch"):
        log(f"[{user['username']}] Monthly rebalance skipped (kill switch active)", "rebalance")
        return

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
            # Round-10: cancel any open stop on this symbol FIRST so we
            # don't orphan a GTC stop in Alpaca after the shares are gone
            # (same orphan-stop class as SOXL / mean-reversion target).
            # Round-61 pt.8 audit fix: RMW on the strategy file MUST hold
            # `strategy_file_lock`. Without it the 60s monitor loop can
            # read between load and save, then write back with
            # `stop_order_id` still set — next tick tries to re-cancel the
            # same order (harmless) or, worse, overrides a user-triggered
            # pause that landed in the gap. Monthly rebalance fires once
            # a month so holding the lock across the Alpaca DELETE is fine.
            try:
                for sf in os.listdir(sdir):
                    if not (sf.endswith(f"_{symbol}.json") and not sf.startswith("wheel_")):
                        continue
                    sf_path = os.path.join(sdir, sf)
                    with strategy_file_lock(sf_path):
                        strat_for_close = load_json(sf_path) or {}
                        sid = (strat_for_close.get("state") or {}).get("stop_order_id")
                        if sid:
                            user_api_delete(user, f"/orders/{sid}")
                            strat_for_close["state"]["stop_order_id"] = None
                            save_json(sf_path, strat_for_close)
            except Exception as _e:
                log(f"[{user['username']}] rebalance cancel-stop failed for {symbol}: {_e}", "rebalance")
            # Round-11: idempotency so a timeout-retry doesn't double-close.
            _mr_today = get_et_time().strftime("%Y%m")
            order = user_api_post(user, "/orders", {
                "symbol": symbol, "qty": str(qty),
                "side": "sell" if float(pos.get("qty", 0)) > 0 else "buy",
                "type": "market", "time_in_force": "day",
                "client_order_id": f"monthly-{symbol}-{_mr_today}",
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
    # Round-11: use a local name that does not shadow the module-level
    # `now_et` import. Previous `now_et = get_et_time()` shadow worked
    # by luck but confused static analysis and any future caller that
    # expected now_et to remain callable inside this scope.
    et_now = get_et_time()
    if et_now.day > 7:
        return False  # Definitely not first trading day
    # Check calendar via Alpaca
    start = et_now.replace(day=1).strftime("%Y-%m-%d")
    end = et_now.strftime("%Y-%m-%d")
    cal = user_api_get(user, f"/calendar?start={start}&end={end}")
    if not isinstance(cal, list) or not cal:
        return False
    first_trading_date = cal[0].get("date", "")
    today_str = et_now.strftime("%Y-%m-%d")
    return first_trading_date == today_str

# ============================================================================
# SCHEDULER LOOP
# ============================================================================
def should_run_interval(task_name, interval_seconds):
    # Round-9 fix: acquire the lock BEFORE the read so the TOCTOU window
    # between check and write can't let two threads both think they
    # should run the task. The scheduler is single-threaded in practice
    # but handler threads (force-daily-close, future endpoints) can also
    # mutate _last_runs, so lock discipline matters.
    now = time.time()
    with _last_runs_lock:
        last = _last_runs.get(task_name, 0)
        if now - last >= interval_seconds:
            _last_runs[task_name] = now
            fire = True
        else:
            fire = False
    if fire:
        _save_last_runs()  # persist outside the lock (no re-entry risk)
    return fire

def should_run_daily_at(task_name, hour_et, minute_et, max_late_seconds=1800):
    """Fire a daily task exactly once per day.

    `max_late_seconds` is the tolerance window after the target time
    within which the task will still fire if it hasn't run yet today.
    The previous hard-coded 600 (10 min) was too tight — a Railway
    redeploy at ~4:05 PM today wiped in-memory state and restarted at
    4:46 PM, 41 min past the daily-close target, so daily_close silently
    skipped even though it hadn't run that day. Default is now 30 min
    (catches typical redeploy windows); tasks that are safe to run hours
    late (daily_close, weekly_learning, monthly_rebalance) can pass a
    larger value. Auto-deployer/wheel-deploy keep the default because
    firing those hours late = trading on stale screener data.
    """
    # Round-9 fix: acquire lock BEFORE the read. See should_run_interval
    # comment — same TOCTOU bug applied here before.
    # Round-11: renamed local from `now_et` -> `et_now` so we don't shadow
    # the module-level `now_et` import.
    et_now = get_et_time()
    target = et_now.replace(hour=hour_et, minute=minute_et, second=0, microsecond=0)
    today_str = et_now.strftime("%Y-%m-%d")
    with _last_runs_lock:
        last_date = _last_runs.get(task_name, "")
        if last_date != today_str and et_now >= target and (et_now - target).total_seconds() < max_late_seconds:
            _last_runs[task_name] = today_str
            fire = True
        else:
            fire = False
    if fire:
        _save_last_runs()
    return fire


def _clear_daily_stamp(task_name):
    """Round-10 audit helper: revert a daily stamp so the task is
    eligible to retry later today. Call this from the except block of
    any daily task that raised — without it the stamp-before-run
    pattern silently skips the task for the rest of the day even on
    recoverable errors (yfinance rate limit, SMTP blip, etc.)."""
    with _last_runs_lock:
        _last_runs.pop(task_name, None)
    try:
        _save_last_runs()
    except Exception:
        pass

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
    #
    # Round-46 fix: dedup key MUST include the trading mode. Previously a
    # user with live_parallel_enabled=1 could have paper and live wheel
    # deploys collide in _wheel_deploy_in_flight — first one in blocks
    # the second (different-mode) entry, so whichever tick fires second
    # silently skips. That's the inverse of the actual bug: both modes'
    # deploys should run independently. Scope the key the same way the
    # main scheduler loop scopes its _last_runs cache (line 3562).
    _mode = user.get("_mode", "paper")
    uid = f"{user['id']}:{_mode}" if _mode == "live" else user.get("id")
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
        # Round-12 audit fix: atomic abort on kill-switch trip (wheel
        # deploys can place multiple puts in one tick; each put involves
        # two API calls, so kill-switch mid-loop is a real window).
        if deploy_should_abort():
            log(f"[{user['username']}] Wheel auto-deploy ABORT: kill-switch tripped mid-loop",
                "wheel")
            break
        success, msg, _ = ws.open_short_put(user, pick)
        if success:
            log(f"[{user['username']}] WHEEL DEPLOYED: {msg}", "wheel")
            # Parse the contract OCC symbol + premium out of the msg so
            # the rich email has structured data. Example msg:
            #   "Sold-to-open HIMS260508P00027000 @ $2.05 (premium $205.00 if filled). Stage: put active."
            _subj = _body = None
            try:
                import re as _re, notification_templates as _nt
                occ_match = _re.search(r"Sold-to-open\s+([A-Z]+)(\d{6})P(\d{8})\s+@\s+\$([0-9.]+)",
                                        msg)
                if occ_match:
                    sym = occ_match.group(1)
                    expiry_raw = occ_match.group(2)  # YYMMDD
                    strike_raw = int(occ_match.group(3))
                    premium = float(occ_match.group(4))
                    strike = strike_raw / 1000.0
                    expiration = f"20{expiry_raw[0:2]}-{expiry_raw[2:4]}-{expiry_raw[4:6]}"
                    _subj, _body = _nt.wheel_put_sold(
                        symbol=sym, strike=strike, premium=premium,
                        expiration=expiration, contracts=1,
                    )
            except Exception as _e:
                log(f"[{user['username']}] Wheel rich-email build failed: {_e}", "wheel")
            notify_rich(user,
                        f"Wheel opened on {pick.get('symbol')}: {msg}",
                        "trade", rich_subject=_subj, rich_body=_body)
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
    # Round-10: respect kill switch on wheel monitor too. Doesn't block
    # BTC (buy-to-close) orders — those are exits, always safe — but
    # skips the "maybe sell a covered call" leg that would open new
    # risk while halted.
    gpath = user_file(user, "guardrails.json")
    guardrails = load_json(gpath) or {}
    if guardrails.get("kill_switch"):
        log(f"[{user['username']}] Wheel monitor skipped (kill switch active)", "wheel")
        return
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

    # Round-44: auto-fix any [orphan] wheel closes whose entry price can be
    # recovered from the wheel state history[]. Idempotent + cheap (walks
    # wheel files + journal; no Alpaca calls). Runs at the tail of every
    # wheel monitor tick so new orphans get paired with their opens within
    # one cycle — no manual admin button needed.
    try:
        import wheel_open_backfill as _wob
        _res = _wob.backfill_wheel_opens(user)
        if _res.get("patched", 0) > 0:
            log(f"[{user['username']}] wheel-open backfill: patched "
                f"{_res['patched']} orphan close(s) with recovered entry price",
                "wheel")
    except Exception as e:
        log(f"[{user['username']}] wheel-open backfill error: {e}", "wheel")


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
    # Load persisted _last_runs so we don't re-fire tasks that already
    # completed before a container restart. Round-8+ fix.
    _load_last_runs()
    # Round-23: also restore the in-memory Activity ring buffer from disk
    # so the dashboard Activity pane doesn't reset to empty on every
    # Railway redeploy. Best-effort; failure doesn't block boot.
    _load_recent_logs()
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
                    # Round-45 dual-mode: make the dedup key include the
                    # trading mode so paper and live don't stomp each
                    # other's daily-stamps / interval caches. For users
                    # who only run paper (default), uid == str(id)
                    # unchanged, so existing stamps remain compatible.
                    _mode = user.get("_mode", "paper")
                    uid = f"{user['id']}:{_mode}" if _mode == "live" else user["id"]

                    # Round-11 expansion item 6: Pre-market scanner.
                    # Weekdays at 8:30 AM ET — scans top-100 by liquidity
                    # for >2% gaps + meaningful pre-market volume. Saves
                    # premarket_picks.json which the auto-deployer
                    # prioritizes at 9:45 AM. No new positions opened
                    # here — just identifies overnight movers worth
                    # deploying as soon as the market opens.
                    if is_weekday and now_et.hour == 8 and now_et.minute >= 30:
                        if should_run_daily_at(f"premarket_scan_{uid}", 8, 30,
                                                max_late_seconds=2*3600):
                            try:
                                from premarket_scanner import (
                                    scan_premarket, save_premarket_picks
                                )
                                udir = user["_data_dir"]
                                # Use the user's existing top-100 universe
                                # from dashboard_data.json (cached top-50
                                # by liquidity from yesterday).
                                _dpath = os.path.join(udir, "dashboard_data.json")
                                _ddata = (load_json(_dpath) or {})
                                _top_syms = [p.get("symbol") for p in (_ddata.get("picks") or [])
                                             if p.get("symbol")][:100]
                                if _top_syms:
                                    # Bind user to default-arg so the lambda
                                    # doesn't capture the mutable loop variable
                                    # (ruff B023 — defensive even though
                                    # scan_premarket is called synchronously).
                                    _ag = lambda u, _user=user, **kw: user_api_get(_user, u)
                                    pm_picks = scan_premarket(
                                        _ag,
                                        user.get("_api_endpoint") or API_ENDPOINT,
                                        user.get("_data_endpoint") or DATA_ENDPOINT,
                                        _top_syms,
                                    )
                                    save_premarket_picks(udir, pm_picks)
                                    log(f"[{user['username']}] Premarket scan: "
                                        f"{len(pm_picks)} gappers found", "premarket")
                            except Exception as _pe:
                                log(f"[{user['username']}] premarket_scan failed: {_pe}", "premarket")
                                _clear_daily_stamp(f"premarket_scan_{uid}")

                    # Auto-deployer: weekdays 9:45 AM ET (skip opening chop).
                    # Round-11 Tier 1: shifted from 9:35 → 9:45 to dodge
                    # the first-15-min opening volatility. Published data:
                    # spreads/slippage ~3× normal in the opening window.
                    # Round-10: clear the daily stamp on exception so a
                    # transient Alpaca / screener / network error doesn't
                    # silently skip today's deploy — next tick retries.
                    if is_weekday and now_et.hour == 9 and now_et.minute >= 45 and market_open_flag:
                        if should_run_daily_at(f"auto_deployer_{uid}", 9, 45):
                            try:
                                run_auto_deployer(user)
                            except Exception as _e:
                                log(f"[{user['username']}] auto_deployer failed: {_e} — retrying next tick", "scheduler")
                                _clear_daily_stamp(f"auto_deployer_{uid}")
                                raise

                    # Wheel auto-deploy: weekdays 9:40 AM ET (5 min after regular deployer)
                    # Sells cash-secured puts on top wheel candidates.
                    if is_weekday and now_et.hour == 9 and now_et.minute >= 40 and market_open_flag:
                        if should_run_daily_at(f"wheel_deploy_{uid}", 9, 40):
                            try:
                                run_wheel_auto_deploy(user)
                            except Exception as _e:
                                log(f"[{user['username']}] wheel_deploy failed: {_e} — retrying next tick", "scheduler")
                                _clear_daily_stamp(f"wheel_deploy_{uid}")
                                raise

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

                    # Round-61 pt.15: orphan adoption — every 10 min
                    # during market hours. Synthesizes strategy files
                    # for Alpaca positions that have no matching file
                    # on disk so `monitor_strategies` starts placing
                    # + maintaining stops. Previously this only fired
                    # once per day inside run_daily_close, which left
                    # positions unmanaged (labeled MANUAL in the
                    # dashboard) for up to 23.5 hours.
                    if market_open_flag and should_run_interval(f"adopt_orphans_{uid}", 600):
                        run_orphan_adoption(user)

                    # Round-55: AFTER-HOURS trailing-stop monitor.
                    # Runs every 5 min in pre-market (4-9:30 AM ET) +
                    # post-market (4-8 PM ET) ONLY when regular market
                    # is closed. Stops-only mode — doesn't fire new
                    # buys or profit-take sells. Purpose: lock in any
                    # after-hours pop before the overnight drop by
                    # raising the trailing stop to the new high.
                    # Opt-out via guardrails.extended_hours_trailing=false.
                    if not market_open_flag:
                        try:
                            from extended_hours import get_trading_session
                            _session = get_trading_session()
                            if _session in ("pre_market", "after_hours"):
                                if should_run_interval(f"monitor_eh_{uid}", 300):
                                    monitor_strategies(user, extended_hours=True)
                        except Exception as _eh_e:
                            log(f"[{user['username']}] AH monitor error: {_eh_e}", "monitor")

                    # Daily close: weekdays 4:05 PM ET — check anywhere
                    # from 4:05 PM to 8:00 PM so a container restart
                    # crossing the target still recovers. The previous
                    # gate `hour == 16` locked this to a 55-min window,
                    # so a 5:00 PM+ restart would silently skip today's
                    # close. should_run_daily_at's max_late_seconds=4hr
                    # enforces the same bound with its time math.
                    if is_weekday and (now_et.hour == 16 or (now_et.hour >= 17 and now_et.hour < 20)):
                        if should_run_daily_at(f"daily_close_{uid}", 16, 5, max_late_seconds=4*3600):
                            try:
                                run_daily_close(user)
                            except Exception as _e:
                                log(f"[{user['username']}] daily_close failed: {_e} — retrying", "scheduler")
                                _clear_daily_stamp(f"daily_close_{uid}")
                                raise

                    # Round-11 LIVE-BATCH 4: Daily scorecard email digest,
                    # weekdays 4:30 PM ET. Opt-in per user (scorecard_email_enabled=1).
                    # Sends the full scorecard + today's trades + positions
                    # snapshot to the user's notification_email via Gmail SMTP.
                    if is_weekday and now_et.hour == 16 and now_et.minute >= 30:
                        if should_run_daily_at(f"scorecard_digest_{uid}", 16, 30,
                                                max_late_seconds=4*3600):
                            try:
                                run_scorecard_digest(user)
                            except Exception as _e:
                                log(f"[{user['username']}] scorecard_digest failed: {_e}", "scheduler")
                                _clear_daily_stamp(f"scorecard_digest_{uid}")

                    # Weekly learning: Fridays 5:00 PM ET
                    if weekday == 4 and now_et.hour == 17:
                        if should_run_daily_at(f"weekly_learning_{uid}", 17, 0):
                            try:
                                run_weekly_learning(user)
                            except Exception as _e:
                                log(f"[{user['username']}] weekly_learning failed: {_e} — retrying", "scheduler")
                                _clear_daily_stamp(f"weekly_learning_{uid}")
                                raise

                    # Feature 6: Friday risk reduction at 3:45 PM ET
                    if weekday == 4 and now_et.hour == 15 and now_et.minute >= 45 and market_open_flag:
                        if should_run_daily_at(f"friday_reduction_{uid}", 15, 45):
                            try:
                                run_friday_risk_reduction(user)
                            except Exception as _e:
                                log(f"[{user['username']}] friday_reduction failed: {_e} — retrying", "scheduler")
                                _clear_daily_stamp(f"friday_reduction_{uid}")
                                raise

                    # Feature 19: Monthly rebalance on first trading day at 9:45 AM ET
                    if is_weekday and now_et.hour == 9 and now_et.minute >= 45:
                        if is_first_trading_day_of_month(user):
                            if should_run_daily_at(f"monthly_rebalance_{uid}", 9, 45):
                                try:
                                    run_monthly_rebalance(user)
                                except Exception as _e:
                                    log(f"[{user['username']}] monthly_rebalance failed: {_e} — retrying", "scheduler")
                                    _clear_daily_stamp(f"monthly_rebalance_{uid}")
                                    raise
                except Exception as e:
                    log(f"[{user.get('username','?')}] Per-user scheduler error: {e}", "scheduler")

            # Email queue drain — walks every user's email_queue.json and
            # ships pending entries via Gmail SMTP. System task (not per-user)
            # so one SMTP session covers the whole drain pass. Runs every 60s
            # so trade/alert emails land within a minute of being queued.
            # No-op if GMAIL_USER / GMAIL_APP_PASSWORD aren't set — the
            # queue keeps growing until the creds are added, at which point
            # the backlog flushes.
            if should_run_interval("email_drain", 60):
                try:
                    import email_sender
                    result = email_sender.drain_all()
                    if result.get("sent") or result.get("failed"):
                        log(f"Email drain: {result}", "scheduler")
                except Exception as e:
                    log(f"Email drain error: {e}", "scheduler")

            # Daily backup — runs ONCE (not per-user) at 3 AM ET.
            # 3 AM is well after market close and well before any pre-market
            # activity, minimizing contention on the volume.
            if now_et.hour == 3 and now_et.minute >= 0:
                if should_run_daily_at("daily_backup_all", 3, 0):
                    try:
                        run_daily_backup()
                    except Exception as _e:
                        log(f"daily_backup_all failed: {_e} — retrying next tick", "scheduler")
                        _clear_daily_stamp("daily_backup_all")

            # Trade-journal trim — runs ONCE/day per user at 3:15 AM ET, right
            # after the daily backup so the archive move has a clean snapshot
            # to recover from if anything goes sideways.
            if now_et.hour == 3 and now_et.minute >= 15:
                if should_run_daily_at("trade_journal_trim_all", 3, 15):
                    try:
                        import trade_journal as _tj
                        for _user in (users or []):
                            _uid = _user.get("id")
                            if _uid is None:
                                continue
                            _jpath = user_file(_user, "trade_journal.json")
                            _res = _tj.trim_journal(_jpath)
                            if _res.get("moved"):
                                log(
                                    f"[{_user.get('username','?')}] journal trimmed: "
                                    f"moved {_res['moved']} → archive "
                                    f"(live={_res['live_count']}, archive={_res['archive_count']})",
                                    "scheduler",
                                )
                    except Exception as _e:
                        log(f"trade_journal_trim_all failed: {_e} — retrying next tick", "scheduler")
                        _clear_daily_stamp("trade_journal_trim_all")

            # DB housekeeping — runs daily at 3:30 AM ET, right after backup
            # + journal trim. The probabilistic (~1.2%) GC trigger from
            # record_login_attempt() is unreliable on quiet deployments
            # (would skip cleanup for months on low-traffic accounts); this
            # deterministic daily run keeps the sessions + password_resets
            # tables bounded.
            if now_et.hour == 3 and now_et.minute >= 30:
                if should_run_daily_at("db_housekeeping_all", 3, 30):
                    try:
                        if AUTH_AVAILABLE:
                            purged_sess = auth.cleanup_expired_sessions() or 0
                            purged_reset = auth.gc_password_resets() or 0
                            purged_attempts = auth.gc_login_attempts() or 0
                            purged_audit = auth.gc_audit_log() or 0
                            log(
                                f"db housekeeping: sessions={purged_sess} "
                                f"resets={purged_reset} login_attempts={purged_attempts} "
                                f"audit={purged_audit}",
                                "scheduler",
                            )
                    except Exception as _e:
                        log(f"db_housekeeping_all failed: {_e} — retrying next tick", "scheduler")
                        _clear_daily_stamp("db_housekeeping_all")

            # Capitol Trades refresh DISABLED — no working free data
            # provider as of 2026. The nightly task below is preserved
            # for re-enable when a source returns (see
            # update_dashboard.COPY_TRADING_ENABLED).

            # PEAD refresh — runs ONCE (not per-user) at 6 AM ET.
            # Yfinance scrapes Yahoo for recent earnings (actuals,
            # estimates, surprise %). 6 AM is after most overnight
            # earnings reports are published and well before the
            # 9:35 AM auto-deployer needs the cache. Universe is
            # ~120 large/mid-caps; ~50s with 0.4s spacing per Yahoo
            # call. No-ops silently if yfinance isn't installed.
            #
            # Round-10 audit: widen the outer hour gate from just
            # `hour == 6` to `6 <= hour < 10` so a Railway restart
            # between 7 AM and 9:35 AM still catches up the morning
            # refresh before the auto-deployer needs the cache.
            # should_run_daily_at's max_late_seconds=4h enforces the
            # upper bound with its own time math (same pattern as
            # daily_close, fixed in round 8).
            if now_et.hour >= 6 and now_et.hour < 10:
                if should_run_daily_at("pead_refresh", 6, 0,
                                        max_late_seconds=4*3600):
                    try:
                        import pead_strategy
                        result = pead_strategy.refresh_cache()
                        _sig_count = result.get('signal_count', 0)
                        log(f"PEAD refresh: scanned {result.get('universe_size', 0)} symbols, "
                            f"found {_sig_count} signals",
                            "pead")
                        # Round-11: track an empty-streak so we know if
                        # yfinance has gone dark (scraping broke, network
                        # egress blocked, earnings calendar empty). Alert
                        # the admin after 5 consecutive empty days —
                        # earnings happen daily during US reporting weeks,
                        # so 5 zeros in a row = broken pipe, not "no news."
                        _streak_path = os.path.join(DATA_DIR, "pead_empty_streak.json")
                        try:
                            _streak = 0
                            if os.path.exists(_streak_path):
                                with open(_streak_path) as _sf:
                                    _streak = int(json.load(_sf).get("streak", 0))
                            if _sig_count == 0:
                                _streak += 1
                            else:
                                _streak = 0
                            with open(_streak_path, "w") as _sf:
                                json.dump({"streak": _streak,
                                           "last_update": now_et().isoformat()}, _sf)
                            if _streak == 5:
                                # Alert only on the threshold crossing to
                                # avoid daily spam during a long outage.
                                for _u in users:
                                    notify_user(_u,
                                                f"PEAD empty streak = {_streak} days. "
                                                "yfinance scrape may be broken.",
                                                "warning")
                        except Exception as _se:
                            log(f"PEAD empty-streak tracker error: {_se}", "pead")
                    except Exception as e:
                        log(f"PEAD refresh failed: {e} — will retry on next tick", "pead")
                        _clear_daily_stamp("pead_refresh")  # allow retry today

            # Task-staleness watchdog — alert if an interval-based task is
            # overdue during market hours. Each alert fires at most once
            # per hour per task, so a persistent outage doesn't spam.
            # Only meaningful during market hours; outside market hours
            # interval tasks are expected to be idle.
            if market_open_flag:
                _check_task_staleness(users)

            # Heartbeat: /healthz checks that _recent_logs has a log line
            # within 5 min. During market hours tasks log frequently, but
            # after hours the scheduler sleeps silently and healthz starts
            # returning "stale". Emit a heartbeat every ~2 min (every 4
            # iterations of the 30s-sleep loop) to keep healthz green
            # whenever the loop is actually running.
            _heartbeat_tick()
        except Exception as e:
            log(f"Scheduler loop error: {e}", "scheduler")
            # Rate-limited notification on scheduler catastrophes. If the
            # outer loop is failing, the user won't notice unless they
            # happen to open the dashboard. One push per hour is enough to
            # signal the problem without spamming if the loop fails every
            # 30s for an extended outage.
            global _last_fatal_notify_ts
            _now = time.time()
            if _now - _last_fatal_notify_ts > 3600:
                _last_fatal_notify_ts = _now
                try:
                    notify_user_global(
                        f"Scheduler loop hit an unhandled exception: "
                        f"{type(e).__name__}: {str(e)[:200]}. "
                        f"Bot continues but check Railway logs.",
                        "alert",
                    )
                except Exception:
                    pass

        time.sleep(30)

def start_scheduler():
    global _scheduler_thread, _scheduler_running
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_running = True
    _scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True, name="CloudScheduler")
    _scheduler_thread.start()
    log("Scheduler thread started", "scheduler")
    # Round-24: watchdog that fires critical_alert if the scheduler
    # thread dies unexpectedly. Without this, an unhandled exception
    # in scheduler_loop kills the thread but leaves the HTTP server
    # up — the bot would silently stop trading with no notification.
    # Runs in its own daemon thread, polls is_alive() every 60s, and
    # fires at most one alert per process lifetime (_alerted latch).
    try:
        threading.Thread(target=_scheduler_death_watchdog,
                         daemon=True,
                         name="SchedulerWatchdog").start()
    except Exception as _e:
        log(f"scheduler watchdog thread failed to start: {_e}", "scheduler")
    # Round-16: boot-time state-recovery sweep. Reads wheel JSON state
    # + journal vs Alpaca positions for every user; surfaces any
    # discrepancy via Sentry capture_message. Doesn't auto-fix — purely
    # diagnostic. Wrapped in try/except so a reconcile bug never blocks
    # the scheduler from starting.
    try:
        threading.Thread(target=_run_state_reconcile_safely,
                         daemon=True,
                         name="StateReconcile").start()
    except Exception as _e:
        log(f"state-reconcile thread failed to start: {_e}", "scheduler")
    # Round-23: Alpaca real-time news stream. Opt-in via env var
    # (defaults to "true" since websocket-client is in requirements.txt).
    # Streams breaking news for each user's watched symbols and writes
    # actionable alerts to news_alerts.json. Doesn't auto-trade — it's
    # informational + ntfy push. Fails soft (broken import, bad creds,
    # etc. all just log and return).
    if os.environ.get("ENABLE_NEWS_WEBSOCKET", "true").lower() == "true":
        try:
            threading.Thread(target=_start_news_streams_safely,
                             daemon=True,
                             name="NewsStreamBoot").start()
        except Exception as _e:
            log(f"news-stream boot thread failed to start: {_e}", "scheduler")


def _start_news_streams_safely():
    """Kick off news_websocket for the bootstrap user with their
    current watched symbols (union of open positions + auto-deployed
    strategy symbols). Single-stream design — if multi-user comes
    later we'd need to refactor news_websocket's module-level globals.

    Runs in a short-lived thread so a slow first fetch of positions
    doesn't block scheduler startup."""
    try:
        import news_websocket
        # Only start for user_id == 1 (bootstrap admin) for now —
        # news_websocket uses module-level globals and can't run
        # multiple streams concurrently without refactor.
        users = list(get_all_users_for_scheduling())
        admin = next((u for u in users if u.get("id") == 1), None)
        if not admin:
            log("news-stream: no admin user found, skipping", "scheduler")
            return
        # Union of watched symbols: open positions + active strategy files
        symbols = set()
        try:
            positions = user_api_get(admin, "/positions")
            if isinstance(positions, list):
                for p in positions:
                    sym = (p or {}).get("symbol", "")
                    # Skip options — news stream is for underlying equities
                    if sym and len(sym) <= 6 and sym.isalpha():
                        symbols.add(sym.upper())
        except Exception as _e:
            log(f"news-stream: positions fetch failed: {_e}", "scheduler")
        try:
            udir = admin.get("_data_dir") or ""
            strat_dir = os.path.join(udir, "strategies")
            if os.path.isdir(strat_dir):
                for fname in os.listdir(strat_dir):
                    # Strategy files are named "breakout_TSLA.json" etc.
                    if fname.endswith(".json") and "_" in fname:
                        sym = fname.rsplit(".json", 1)[0].split("_", 1)[-1]
                        if sym and sym.isalpha() and len(sym) <= 6:
                            symbols.add(sym.upper())
        except Exception as _e:
            log(f"news-stream: strategy enumeration failed: {_e}", "scheduler")
        if not symbols:
            log("news-stream: no watched symbols, skipping boot "
                "(will retry on next reconcile)", "scheduler")
            return
        # Surface the Alpaca credentials the websocket uses (its
        # on_open handler authenticates with these).
        admin_copy = dict(admin)
        admin_copy["api_key"] = admin.get("_api_key", "")
        admin_copy["api_secret"] = admin.get("_api_secret", "")
        thread = news_websocket.start_news_stream(admin_copy, list(symbols))
        if thread:
            log(f"news-stream: connected for user_id=1, "
                f"{len(symbols)} symbols", "scheduler")
        else:
            log("news-stream: start_news_stream returned None "
                "(pip package or creds missing)", "scheduler")
    except Exception as e:
        log(f"news-stream boot failed: {type(e).__name__}: {e}",
            "scheduler")


def _run_state_reconcile_safely():
    """Run state_recovery.reconcile_user for every active user. Logs
    warnings via observability; never raises.

    Round-20: also runs migrations.run_all_migrations for the same
    user list, so the boot-time sweep covers both state-consistency
    checks AND idempotent config upgrades (e.g. max_position_pct
    0.10 → 0.07 for users still on the round-13 default)."""
    try:
        import state_recovery
        users = list(get_all_users_for_scheduling())
        # Round-20 migrations — idempotent, stamped with
        # `_migrations_applied` so re-runs are no-ops.
        try:
            import migrations
            # Round-51: account_fetcher enables calibration-adopt
            # migration to query Alpaca /account per user.
            def _fetch_account(_u):
                try:
                    return user_api_get(_u, "/account")
                except Exception:
                    return None
            summary = migrations.run_all_migrations(
                users, user_file, account_fetcher=_fetch_account)
            for uid, actions in (summary or {}).items():
                action = actions.get("round20_position_cap")
                if action and action not in ("already_applied", "no_file"):
                    log(f"[user_id={uid}] migration round20_position_cap: "
                        f"{action}", "scheduler")
                # Round-51: log calibration-adopt outcomes so the user
                # sees in the activity log when their tier was detected
                # and defaults were applied.
                action51 = actions.get("round51_calibration_adopt")
                if action51 and action51 not in ("already_applied", "no_file"):
                    log(f"[user_id={uid}] migration round51_calibration_adopt: "
                        f"{action51}", "scheduler")
        except Exception as _me:
            log(f"migration sweep failed: {type(_me).__name__}: {_me}",
                "scheduler")

        for u in users:
            try:
                udir = u.get("_data_dir") or ""
                wheel_dir = os.path.join(udir, "strategies")
                journal_path = os.path.join(udir, "trade_journal.json")

                def _fetch_positions(_user=u):
                    res = user_api_get(_user, "/positions")
                    return res if isinstance(res, list) else []

                result = state_recovery.reconcile_user(
                    u, wheel_dir, journal_path,
                    fetch_positions=_fetch_positions,
                )
                state_recovery.report_to_observability(u, result)
            except Exception as _e:
                log(f"state-reconcile {u.get('username','?')} failed: "
                    f"{type(_e).__name__}", "scheduler")
    except Exception as _e:
        log(f"state-reconcile sweep failed: {type(_e).__name__}", "scheduler")


def _scheduler_death_watchdog():
    """Runs for the lifetime of the process. Every 60s checks that the
    scheduler thread is still alive while _scheduler_running is True.
    If the thread has died, fires critical_alert (ntfy + Sentry + email)
    exactly once per process so the operator knows the bot is no
    longer trading even though the HTTP server is still responding.

    `_alerted` latches after the first firing — repeated alerts would
    spam the user every 60s, and once the thread is dead one alert is
    enough to demand attention.

    Round-24: zombie-subprocess counter also checked here so we piggy-
    back on the same minute-polling loop and don't spin up another
    thread just for that.
    """
    _alerted = False
    _last_zombie_alert_ts = 0.0
    while True:
        try:
            time.sleep(60)
            # Primary check: scheduler liveness
            if _scheduler_running and (
                    not _scheduler_thread or not _scheduler_thread.is_alive()):
                if not _alerted:
                    _alerted = True
                    try:
                        import observability
                        observability.critical_alert(
                            title="🚨 Scheduler thread died",
                            body=("The cloud_scheduler background thread "
                                  "is no longer running. HTTP server is "
                                  "still up but the bot has STOPPED "
                                  "trading. Railway will redeploy on "
                                  "next /healthz 503; if that doesn't "
                                  "recover, manually restart the "
                                  "service."),
                            tags={"source": "scheduler_watchdog"},
                        )
                    except Exception as _e:
                        log(f"watchdog: critical_alert failed: {_e}",
                            "scheduler")
            # Secondary check: subprocess zombie accumulation. Screener
            # subprocesses can linger if SIGKILL also fails. Poll for
            # zombies owned by this process and alert if we exceed 5.
            # The function RETURNS the updated last-alert timestamp so
            # our rate-limit tracker actually advances — the round-24
            # first-cut passed the local variable by value, which meant
            # the 1-alert-per-hour limit never kicked in (it'd fire
            # every 60s once zombies > 5).
            try:
                _last_zombie_alert_ts = _check_subprocess_zombies(_last_zombie_alert_ts)
            except Exception as _e:
                # Watchdog must never crash — log and continue.
                log(f"watchdog: zombie check failed: {_e}", "scheduler")
        except Exception as _e:
            # The watchdog itself must never die.
            try:
                log(f"watchdog tick error: {_e}", "scheduler")
            except Exception:
                pass


def _check_subprocess_zombies(_last_alert_ts):
    """Count zombie children of this process and fire critical_alert
    if we exceed 5. Rate-limited to one alert per hour so a persistent
    zombie buildup doesn't spam — ntfy caps hourly anyway.

    Returns the updated last-alert timestamp (may equal the input if
    no alert fired this tick). Caller is expected to assign the return
    value back so the rate-limit actually advances.

    Uses os.waitpid(-1, os.WNOHANG) to reap finished-but-not-waited
    children first; the zombie count is then whatever remains."""
    # Best-effort reap. Round-15 added SIGKILL fallback but doesn't
    # always call waitpid — reap any we can here so they don't
    # accumulate as zombies.
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except (ChildProcessError, OSError):
            break
    # Count remaining zombies by reading /proc/self/task/*/children
    # (Linux only — best-effort). On macOS or non-proc systems this
    # silently returns 0.
    zombie_count = 0
    try:
        proc_dir = f"/proc/{os.getpid()}/task"
        if os.path.isdir(proc_dir):
            for tid in os.listdir(proc_dir):
                chld_path = f"{proc_dir}/{tid}/children"
                if os.path.isfile(chld_path):
                    with open(chld_path) as f:
                        children = f.read().strip().split()
                    for cpid in children:
                        # Check if zombie
                        stat_path = f"/proc/{cpid}/stat"
                        if os.path.isfile(stat_path):
                            try:
                                with open(stat_path) as f:
                                    stat_line = f.read()
                                # Field 3 is state: Z = zombie
                                parts = stat_line.rsplit(")", 1)
                                if len(parts) == 2:
                                    state = parts[1].split()[0]
                                    if state == "Z":
                                        zombie_count += 1
                            except (OSError, IndexError):
                                continue
    except Exception:
        return _last_alert_ts  # Best-effort; never alert on inspection failure
    if zombie_count > 5:
        # Rate-limit to 1 alert per hour
        now_ts = time.time()
        if now_ts - _last_alert_ts < 3600:
            return _last_alert_ts
        try:
            import observability
            observability.critical_alert(
                title=f"⚠️ {zombie_count} subprocess zombies",
                body=(f"Accumulated {zombie_count} zombie children "
                      f"of the scheduler process. Likely SIGKILL "
                      f"fallback didn't reap them. Railway restart "
                      f"will clear — monitor for repeated alerts."),
                tags={"source": "zombie_watchdog",
                      "count": str(zombie_count)},
            )
            return now_ts  # rate-limit advances only on a successful alert
        except Exception as _e:
            log(f"watchdog: zombie critical_alert failed: {_e}",
                "scheduler")
            return _last_alert_ts
    return _last_alert_ts


def stop_scheduler():
    """Flip the running flag AND force-flush _last_runs + reap any
    subprocess children before returning so SIGTERM shutdown is clean.
    Round-10 audit fix — previously an in-flight _last_runs update
    could be lost on Railway redeploy (Popen children orphaned, stamp
    not persisted)."""
    global _scheduler_running
    _scheduler_running = False
    try:
        _save_last_runs()
    except Exception as _e_lr:
        log(f"stop_scheduler _save_last_runs failed: {_e_lr}", "scheduler")
    # Round-23: also flush the Activity ring buffer so whatever was
    # logged since the last heartbeat-save isn't lost on SIGTERM.
    try:
        _save_recent_logs()
    except Exception as _e:
        log(f"stop_scheduler _save_recent_logs failed: {_e}", "scheduler")
    # Reap any tracked subprocess children.
    # Round-15: terminate() alone doesn't guarantee the child dies.
    # Issue SIGTERM, wait briefly, then SIGKILL if still alive. Keeps
    # Railway's SIGTERM-to-exit window clean of zombies.
    try:
        for p in list(_tracked_children):
            try:
                if p.poll() is None:  # still running
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        try:
                            p.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            pass
            except Exception:
                pass
    except Exception:
        pass
    log("Scheduler stop requested", "scheduler")


# Round-10: track subprocess.Popen children so SIGTERM can terminate
# them. subprocess.run() is synchronous so it self-reaps; the long-
# running Popens (notify.py push sends) are what we care about.
import threading as _tracked_threading
_tracked_children = set()
_tracked_children_lock = _tracked_threading.Lock()


def _track_child(popen):
    try:
        with _tracked_children_lock:
            _tracked_children.add(popen)
        # Prune finished entries periodically (cheap — one O(n) scan).
        with _tracked_children_lock:
            for p in list(_tracked_children):
                if p.poll() is not None:
                    _tracked_children.discard(p)
    except Exception:
        pass

# Round-39: match `[username]` tags in log messages so we can filter
# the activity ring per-user. Username chars per auth.py are
# [A-Za-z0-9_-]{3,30}. Generic scheduler events (heartbeat, boot) have
# no tag and are shared context.
import re as _re_log
_USER_TAG_RE = _re_log.compile(r"\[[A-Za-z0-9_-]{3,30}\]")
# Prefix tags the log() calls add — these are task types, not usernames.
_TASK_TAGS = frozenset([
    "[scheduler]", "[screener]", "[monitor]", "[deployer]", "[learn]",
    "[friday]", "[news-stream]", "[journal]", "[wheel]", "[pead]",
    "[error]", "[kill-switch]", "[auth]", "[migration]",
])


def _has_user_tag(msg):
    """Return True if `msg` contains a `[username]` tag (not a task tag)."""
    if not msg:
        return False
    for m in _USER_TAG_RE.findall(msg):
        if m not in _TASK_TAGS:
            return True
    return False


def get_scheduler_status(filter_username=None, is_admin=False):
    """Return scheduler health + recent activity.

    Round-39 privacy fix: non-admin callers pass their own username
    via `filter_username` so the activity ring buffer is filtered to
    entries that belong to THEM (either tagged `[username]` or
    generic scheduler-level events). Before this, every authenticated
    user saw every other user's scheduler + screener + monitor
    events, which leaked activity metadata across accounts.

    Admins see everything unfiltered.
    """
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
    # Round-39: non-admins don't see the list of other users. They see
    # only their own row (for the "who am I" indicator). Admins get
    # the full roster as before.
    user_info = []
    for u in users:
        if not is_admin and filter_username and u.get("username") != filter_username:
            continue
        user_info.append({
            "id": u["id"],
            "username": u["username"],
            "endpoint": u["_api_endpoint"],
        })

    with _logs_lock:
        # Round-22: surface up to 200 recent log lines instead of 20.
        # Dashboard activity pane is scrollable (max-height + overflow-y)
        # so the extra depth doesn't hurt first-paint; user sees more
        # history on scroll + doesn't have to tail Railway logs.
        all_logs = list(_recent_logs[-500:])

    # Round-39: filter log entries by username for non-admins.
    # Log messages are tagged `[username]` after the task prefix for
    # user-specific events (screener, monitor, deploy, kill-switch,
    # admin audit). Generic scheduler events (heartbeat, boot, etc.)
    # have no username tag and are shared context — safe to show.
    # Matching is case-sensitive because usernames are case-sensitive
    # at the auth layer.
    if is_admin or not filter_username:
        logs = all_logs[-200:]
    else:
        tag = f"[{filter_username}]"
        filtered = []
        for entry in all_logs:
            msg = entry.get("msg", "")
            has_any_user_tag = _has_user_tag(msg)
            # Include if it's a generic (no user tag) event, OR it's
            # tagged with our user.
            if tag in msg or not has_any_user_tag:
                filtered.append(entry)
        logs = filtered[-200:]

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
