"""
scheduler_api.py — Alpaca per-user API helpers, circuit breaker, and
rate limiter.

Round-17 extraction from cloud_scheduler.py. The 3800-LOC scheduler
monolith mixed scheduling logic with low-level API plumbing; pulling
the plumbing out makes both halves easier to navigate and unit-test.

PUBLIC API (re-exported from cloud_scheduler for backwards compat):
  user_api_get(user, url_path, timeout=10, retries=2) -> dict
  user_api_post(user, url_path, data, timeout=10) -> dict
  user_api_delete(user, url_path, timeout=10) -> dict
  user_api_patch(user, url_path, data, timeout=10) -> dict

INTERNAL (also re-exported because tests / handlers reach into them):
  _cb_state, _cb_lock, _cb_blocked, _cb_record_failure, _cb_record_success
  _rl_state, _rl_lock, _rl_acquire
  _user_headers, _cb_key
  _CB_OPEN_THRESHOLD, _CB_OPEN_SECONDS
  _RL_MAX, _RL_REFILL_PER_SEC
  _auth_alert_dates, _auth_alert_lock, _alert_alpaca_auth_failure

Per-user circuit breaker
  After CB_OPEN_THRESHOLD consecutive failures, opens for CB_OPEN_SECONDS.
  Round-12 fix: only POP state when COMING OFF a real cool-off (open_until
  > 0 AND now past it). Previously the initial fail entry got popped on
  every non-open check, silently resetting the counter.

Per-user rate limiter
  Token bucket sized for Alpaca's 200 req/min/key limit. Default 180 to
  leave headroom for bursts.

Auth-failure alerting
  401/403 from Alpaca → critical_alert (ntfy + email + Sentry) once per
  user per ET day. Prevents the silent-credential-rot scenario.

Notify dependency
  _cb_record_failure historically called notify_user (which lives in
  cloud_scheduler) on the trip transition. We resolve via lazy import
  inside the trip path so the modules don't form an import cycle.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


# ===== Circuit breaker config =====
_CB_OPEN_THRESHOLD = 5    # consecutive failures before tripping
_CB_OPEN_SECONDS = 300    # 5-minute cool-off once tripped
_cb_state: dict = {}      # user_id -> {"fails": int, "open_until": ts}
_cb_lock = threading.Lock()


# ===== Rate limiter config =====
_RL_MAX = 180              # leave 10% headroom under the 200/min limit
_RL_REFILL_PER_SEC = _RL_MAX / 60.0
_rl_state: dict = {}       # user_id -> {"tokens": float, "updated": ts}
_rl_lock = threading.Lock()


# ===== Auth-failure alert dedup =====
_auth_alert_dates: dict = {}
_auth_alert_lock = threading.Lock()


def _user_headers(user):
    return {
        "APCA-API-KEY-ID": user["_api_key"],
        "APCA-API-SECRET-KEY": user["_api_secret"],
    }


def _cb_key(user):
    return user.get("id", "env")


def _cb_blocked(user):
    """True if circuit is open for this user.

    Round-12 fix: only POP state when we're coming off a cool-off
    (open_until > 0 AND now past it). Previously this popped whenever
    open_until <= now — including the initial {fails: N, open_until: 0}
    state, silently resetting the failure counter on every non-tripped
    check. Effectively meant the breaker never actually tripped.
    """
    key = _cb_key(user)
    with _cb_lock:
        st = _cb_state.get(key)
        if not st:
            return False
        if st.get("open_until", 0) > time.time():
            return True
        if st.get("open_until", 0) > 0:
            _cb_state.pop(key, None)
        return False


def _cb_record_failure(user):
    key = _cb_key(user)
    tripped = False
    fails = 0
    with _cb_lock:
        st = _cb_state.setdefault(key, {"fails": 0, "open_until": 0})
        st["fails"] += 1
        fails = st["fails"]
        if st["fails"] >= _CB_OPEN_THRESHOLD:
            if st.get("open_until", 0) <= time.time():
                tripped = True
            st["open_until"] = time.time() + _CB_OPEN_SECONDS
    if tripped:
        log.warning(
            f"[{user.get('username','?')}] Alpaca circuit breaker OPEN "
            f"({fails} consecutive failures). Cooling off "
            f"{_CB_OPEN_SECONDS}s."
        )
        # Lazy import: notify_user lives in cloud_scheduler. Doing this
        # at module load time would create a circular import.
        try:
            from cloud_scheduler import notify_user
            notify_user(
                user,
                f"Alpaca API has been failing for your account "
                f"({fails} consecutive errors). Trading is paused for "
                f"{_CB_OPEN_SECONDS // 60} minutes. Check Alpaca status "
                f"and your API credentials.",
                "alert",
            )
        except Exception as e:
            log.warning(f"CB-trip notification failed: {e}")


def _cb_record_success(user):
    key = _cb_key(user)
    with _cb_lock:
        _cb_state.pop(key, None)


def _rl_acquire(user, wait_max=2.0):
    """Consume 1 token from this user's bucket. Returns True if acquired,
    False if the bucket stayed empty for longer than wait_max seconds."""
    key = _cb_key(user)
    deadline = time.time() + wait_max
    while True:
        with _rl_lock:
            now = time.time()
            st = _rl_state.setdefault(key, {"tokens": _RL_MAX, "updated": now})
            elapsed = now - st["updated"]
            st["tokens"] = min(_RL_MAX, st["tokens"] + elapsed * _RL_REFILL_PER_SEC)
            st["updated"] = now
            if st["tokens"] >= 1:
                st["tokens"] -= 1
                return True
        if time.time() > deadline:
            return False
        time.sleep(0.15)


def _alert_alpaca_auth_failure(user, code, reason):
    """Round-15: 401/403 → critical_alert once per user per ET day.
    Lazy imports so this module stays free of et_time / observability
    coupling at load time."""
    try:
        try:
            from et_time import now_et
            today = now_et().strftime("%Y-%m-%d")
        except Exception:
            from datetime import datetime
            today = datetime.utcnow().strftime("%Y-%m-%d")
        uid = user.get("id")
        with _auth_alert_lock:
            if _auth_alert_dates.get(uid) == today:
                return
            _auth_alert_dates[uid] = today
        try:
            from observability import critical_alert
            critical_alert(
                f"Alpaca {code} — credentials rejected",
                f"User {user.get('username','?')}: Alpaca returned "
                f"HTTP {code} ({reason}). All trades blocked until "
                f"creds are refreshed. Settings → Alpaca API → re-enter "
                f"keys. This alert fires once per day to avoid spam.",
                tags={"code": code, "user": user.get("username", "?")},
                user=user,
            )
        except Exception:
            pass
    except Exception:
        pass


def user_api_get(user, url_path, timeout=10, retries=2):
    """GET from this user's Alpaca endpoint with exponential backoff on
    transient errors. Returns {"error": ...} after final retry failure.

    Per-user circuit breaker fast-fails after 5 consecutive failures.
    """
    if _cb_blocked(user):
        return {"error": "circuit_breaker_open"}
    if not _rl_acquire(user):
        return {"error": "rate_limited_local"}

    if url_path.startswith("http"):
        url = url_path
    else:
        if "/stocks/" in url_path or "/options/" in url_path or "/news" in url_path:
            url = user["_data_endpoint"] + url_path
        else:
            url = user["_api_endpoint"] + url_path
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=_user_headers(user))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _cb_record_success(user)
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                try:
                    ra = float(e.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    ra = 0
                time.sleep(max(ra, 0.5 * (2 ** attempt)))
                continue
            if 500 <= e.code < 600 and attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            if 500 <= e.code < 600:
                _cb_record_failure(user)
            return {"error": f"HTTP {e.code}"}
        except Exception:
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
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
    if _cb_blocked(user):
        return {"error": "circuit_breaker_open"}
    if not _rl_acquire(user, wait_max=5.0):
        return {"error": "rate_limited_local"}
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={**_user_headers(user), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            _cb_record_success(user)
            return body
    except urllib.error.HTTPError as he:
        if 400 <= he.code < 500:
            if he.code in (401, 403):
                _alert_alpaca_auth_failure(user, he.code, he.reason)
            return {"error": f"HTTP {he.code}: {he.reason}"}
        _cb_record_failure(user)
        return {"error": f"HTTP {he.code}: {he.reason}"}
    except Exception as e:
        _cb_record_failure(user)
        return {"error": str(e)}


def user_api_delete(user, url_path, timeout=10):
    if url_path.startswith("http"):
        url = url_path
    else:
        url = user["_api_endpoint"] + url_path
    if _cb_blocked(user):
        return {"error": "circuit_breaker_open"}
    # Round-19: DELETE counts against Alpaca's 200/min budget too.
    # Previously skipped the rate-limit gate, so a surge of
    # kill-switch cancels could blow past the budget and 429-spam.
    if not _rl_acquire(user, wait_max=2.0):
        return {"error": "rate_limited_local"}
    req = urllib.request.Request(url, headers=_user_headers(user), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            _cb_record_success(user)
            return json.loads(body.decode()) if body else {}
    except urllib.error.HTTPError as he:
        if 400 <= he.code < 500:
            # Round-19: 401/403 on DELETE means creds are bad. Same
            # dedup as POST so we don't spam on a cancel storm.
            if he.code in (401, 403):
                _alert_alpaca_auth_failure(user, he.code, he.reason)
            return {"error": f"HTTP {he.code}: {he.reason}"}
        _cb_record_failure(user)
        return {"error": f"HTTP {he.code}: {he.reason}"}
    except Exception as e:
        _cb_record_failure(user)
        return {"error": str(e)}


def user_api_patch(user, url_path, data, timeout=10):
    """Alpaca PATCH /orders/{id} — atomic modify of an existing order's
    stop_price / limit_price / qty. Used for trailing-stop raises."""
    if url_path.startswith("http"):
        url = url_path
    else:
        url = user["_api_endpoint"] + url_path
    if _cb_blocked(user):
        return {"error": "circuit_breaker_open"}
    # Round-19: PATCH counts against Alpaca's 200/min budget. A trailing-
    # stop raise pass touches every open position's stop order, so
    # without this gate a busy account can 429-spam during market opens.
    if not _rl_acquire(user, wait_max=2.0):
        return {"error": "rate_limited_local"}
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={**_user_headers(user), "Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            _cb_record_success(user)
            return body
    except urllib.error.HTTPError as he:
        if 400 <= he.code < 500:
            # Round-19: 401/403 on PATCH means creds are bad — alert
            # once per ET-day just like POST does.
            if he.code in (401, 403):
                _alert_alpaca_auth_failure(user, he.code, he.reason)
            return {"error": f"HTTP {he.code}: {he.reason}"}
        _cb_record_failure(user)
        return {"error": f"HTTP {he.code}: {he.reason}"}
    except Exception as e:
        _cb_record_failure(user)
        return {"error": str(e)}
