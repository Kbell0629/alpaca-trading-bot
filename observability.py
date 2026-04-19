#!/usr/bin/env python3
"""
observability.py — Sentry error tracking + critical-event alerting.

Round-11 expansion items 18-19. Two integrations:

ITEM 18: SENTRY ERROR TRACKING
  Captures unhandled exceptions in scheduler + handlers + screener.
  Free tier: 5K errors/month. Auto-init when SENTRY_DSN env var set;
  no-op otherwise (so paper trading users don't need to sign up).

ITEM 19: CRITICAL-EVENT ALERTING via ntfy.sh + email
  PagerDuty/Twilio cost money — skipped per user request.
  Instead: ntfy.sh push (already wired in notify.py) + Gmail email
  for things you can't afford to miss:
    * Kill switch trip
    * Scheduler down >5 min
    * Daily loss alert (-3%)
    * Order rejection cluster (3+ in 5 min)
    * yfinance circuit breaker open

Public API:

    init_sentry() -> bool
        Initializes the SDK if SENTRY_DSN env var is set.
        Idempotent — safe to call multiple times.

    capture_exception(exc, **context) -> None
        Send an exception with extra tags. Falls back to the structured
        logger (ERROR level, with traceback) if Sentry not initialized.

    critical_alert(title, body, tags=None, user=None) -> dict
        Multi-channel: Sentry (as message) + ntfy (push) + email.
        Returns {sentry: ok, ntfy: ok, email: ok}.

    install_exception_hook() -> None
        Wires sys.excepthook so unhandled top-level exceptions get
        captured. Call once at process startup.
"""
from __future__ import annotations
import logging
import os
import re
import sys
import traceback
from datetime import datetime

log = logging.getLogger(__name__)

_SENTRY_INITIALIZED = False
_SENTRY_AVAILABLE = False

# Patterns that look like credentials — we scrub these from any string sent
# to Sentry. Conservative: prefer false positives (scrubbing) over leaking.
_SCRUB_KEY_RE = re.compile(r"\b(PK|AK)[A-Z0-9]{14,}\b")     # Alpaca API keys
_SCRUB_SECRET_RE = re.compile(r"\b[A-Za-z0-9/+]{40,}\b")    # long base64-ish
_SCRUB_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
_SCRUB_HEADER_KEYS = {"apca-api-key-id", "apca-api-secret-key",
                      "authorization", "cookie", "set-cookie",
                      "x-csrf-token", "x-api-key"}


def _scrub_text(s):
    """Remove credentials / emails from a free-form string."""
    if not isinstance(s, str):
        return s
    s = _SCRUB_KEY_RE.sub("[REDACTED_KEY]", s)
    s = _SCRUB_EMAIL_RE.sub("[REDACTED_EMAIL]", s)
    # Only scrub long base64 tokens — short ones are likely file paths or
    # normal identifiers.
    if len(s) < 10000:  # avoid O(n^2) on huge payloads
        s = _SCRUB_SECRET_RE.sub("[REDACTED_SECRET]", s)
    return s


def _scrub_pii(event, hint=None):
    """Sentry `before_send` hook. Recursively scrubs sensitive substrings
    out of event messages, exception values, and request headers before
    the SDK transmits them."""
    try:
        # Exception chain values
        exc_info = event.get("exception") or {}
        for val in (exc_info.get("values") or []):
            if "value" in val:
                val["value"] = _scrub_text(val["value"])
        # Top-level message
        if "message" in event:
            event["message"] = _scrub_text(event.get("message"))
        # Request headers / cookies
        req = event.get("request") or {}
        hdrs = req.get("headers") or {}
        if isinstance(hdrs, dict):
            for k in list(hdrs.keys()):
                if k.lower() in _SCRUB_HEADER_KEYS:
                    hdrs[k] = "[REDACTED]"
        if "cookies" in req:
            req["cookies"] = "[REDACTED]"
        if "query_string" in req and isinstance(req["query_string"], str):
            req["query_string"] = _scrub_text(req["query_string"])
        # Breadcrumbs — often carry log lines with request bodies
        crumbs = (event.get("breadcrumbs") or {}).get("values") or []
        for c in crumbs:
            if "message" in c:
                c["message"] = _scrub_text(c.get("message"))
    except Exception:
        # Never break the Sentry pipeline on a scrub error — drop the
        # event instead so we don't accidentally send unscrubbed data.
        return None
    return event


def init_sentry():
    """Lazy-init Sentry SDK if SENTRY_DSN is set. Returns True on
    successful init, False if DSN missing or SDK not installed."""
    global _SENTRY_INITIALIZED, _SENTRY_AVAILABLE
    if _SENTRY_INITIALIZED:
        return True
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,  # no perf sampling — we just want errors
            profiles_sample_rate=0.0,
            environment=os.environ.get("RAILWAY_ENVIRONMENT", "production"),
            release=os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")[:8],
            send_default_pii=False,  # never send user data
            integrations=[LoggingIntegration(level=None, event_level=None)],
            before_send=_scrub_pii,
            ignore_errors=[
                # Client disconnects / broken pipes — noise from healthchecks
                "BrokenPipeError",
                "ConnectionResetError",
                "ConnectionAbortedError",
                # Transient network errors from yfinance / Alpaca / ntfy.
                # We retry these in-app and surface FINAL failures via
                # capture_exception with explicit context. Letting every
                # transient one through to Sentry burns the 5K/month free
                # quota on noise that doesn't need ops attention.
                "URLError",
                "TimeoutError",
                "socket.timeout",
                "ReadTimeoutError",
                "RemoteDisconnected",
            ],
        )
        _SENTRY_AVAILABLE = True
        _SENTRY_INITIALIZED = True
        log.info("Sentry initialized")
        return True
    except ImportError:
        log.warning("sentry-sdk not installed (pip install sentry-sdk)")
        return False
    except Exception as e:
        log.warning("Sentry init failed", extra={"error": str(e)})
        return False


def capture_exception(exc=None, **context):
    """Send an exception to Sentry with optional context tags.
    Falls back to the structured logger (ERROR + traceback) if Sentry
    isn't initialized."""
    if not _SENTRY_AVAILABLE:
        # No Sentry configured — still get the traceback into the log stream
        # so the JSON envelope (and any downstream aggregator) has it.
        log.error("exception captured (no Sentry)",
                  extra={"context": context} if context else None,
                  exc_info=exc if isinstance(exc, BaseException) else None)
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in (context or {}).items():
                scope.set_tag(k, str(v)[:200])
            sentry_sdk.capture_exception(exc)
    except Exception as e:
        log.warning("capture_exception failed", extra={"error": str(e)})


def capture_message(message, level="info", **context):
    """Send a non-exception message to Sentry. Useful for key events
    (kill switch trip, large gain/loss, etc.) we want visible in the
    Sentry feed alongside errors."""
    if not _SENTRY_AVAILABLE:
        # Route through the same logger level so the message still shows
        # up in the JSON stream / aggregator even without Sentry.
        lvl_map = {
            "debug": log.debug,
            "info": log.info,
            "warning": log.warning,
            "error": log.error,
            "fatal": log.critical,
        }
        lvl_map.get(level.lower(), log.info)(message, extra=context or None)
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in (context or {}).items():
                scope.set_tag(k, str(v)[:200])
            sentry_sdk.capture_message(message, level=level)
    except Exception as e:
        log.warning("capture_message failed", extra={"error": str(e)})


def critical_alert(title, body, tags=None, user=None):
    """Multi-channel critical-event alert.
      - Sentry: capture_message at 'error' level (keeps history)
      - ntfy.sh: push notification (instant phone alert)
      - email: queued via existing email_sender if user has email set

    Returns {sentry, ntfy, email} success flags."""
    result = {"sentry": False, "ntfy": False, "email": False}
    # 1. Sentry
    try:
        capture_message(f"{title}: {body}", level="error", **(tags or {}))
        result["sentry"] = _SENTRY_AVAILABLE
    except Exception:
        pass
    # 2. ntfy push
    try:
        import urllib.request
        topic = (user or {}).get("ntfy_topic") if user else os.environ.get("NTFY_TOPIC")
        if topic:
            req_body = (title + "\n" + body).encode("utf-8")[:4000]
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=req_body,
                headers={
                    "Title": title.encode("ascii", "ignore").decode()[:200],
                    "Priority": "5",
                    "Tags": "warning,trading-bot",
                },
            )
            urllib.request.urlopen(req, timeout=5)
            result["ntfy"] = True
    except Exception as e:
        # Round-15: log exception class only. str(e) on a urllib error
        # includes the full URL (with ntfy topic), which would leak the
        # topic to anyone reading the Railway log and let them subscribe.
        log.warning("ntfy critical alert failed",
                    extra={"error": type(e).__name__})
    # 3. Email (queue via existing module if available)
    try:
        if user and user.get("notification_email"):
            # queue_email lives in notify.py and signs as
            # (subject, body, notify_type) — not (user, type, subject=, body=).
            # Prior to round-13 we were importing from email_sender (wrong
            # module) AND calling with the wrong signature, so every
            # critical-alert email failed with ImportError + TypeError, both
            # swallowed by the outer except. Kill-switch trips never emailed.
            from notify import queue_email
            from et_time import now_et
            queue_email(
                f"⚠ {title}",
                f"{title}\n\n{body}\n\nTimestamp: {now_et().isoformat()}",
                notify_type="alert",
            )
            result["email"] = True
    except Exception as e:
        log.warning("critical email failed", extra={"error": str(e)})
    return result


def install_exception_hook():
    """Install sys.excepthook so unhandled top-level exceptions get
    captured by Sentry. Idempotent."""
    if not _SENTRY_AVAILABLE:
        init_sentry()
    if not _SENTRY_AVAILABLE:
        return  # nothing to install
    _orig_hook = sys.excepthook

    def _hook(exc_type, exc_value, tb):
        try:
            capture_exception(exc_value, exc_type=exc_type.__name__)
        except Exception:
            pass
        _orig_hook(exc_type, exc_value, tb)

    sys.excepthook = _hook


# Auto-init on import — quiet no-op if SENTRY_DSN missing
init_sentry()


if __name__ == "__main__":
    init_sentry()
    print(f"Sentry available: {_SENTRY_AVAILABLE}")
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        try:
            raise RuntimeError("Sentry test event from observability.py")
        except Exception as e:
            capture_exception(e, test_run="manual")
            print("Test exception sent (if Sentry initialized)")
