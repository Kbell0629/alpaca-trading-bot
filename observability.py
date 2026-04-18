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
        Send an exception with extra tags. Falls back to print() if
        Sentry not initialized.

    critical_alert(title, body, tags=None, user=None) -> dict
        Multi-channel: Sentry (as message) + ntfy (push) + email.
        Returns {sentry: ok, ntfy: ok, email: ok}.

    install_exception_hook() -> None
        Wires sys.excepthook so unhandled top-level exceptions get
        captured. Call once at process startup.
"""
from __future__ import annotations
import os
import sys
import traceback
from datetime import datetime


_SENTRY_INITIALIZED = False
_SENTRY_AVAILABLE = False


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
            ignore_errors=[
                # Ignore client disconnects / broken pipes — noise
                "BrokenPipeError",
                "ConnectionResetError",
                "ConnectionAbortedError",
            ],
        )
        _SENTRY_AVAILABLE = True
        _SENTRY_INITIALIZED = True
        print("[observability] Sentry initialized", flush=True)
        return True
    except ImportError:
        print("[observability] sentry-sdk not installed (pip install sentry-sdk)", flush=True)
        return False
    except Exception as e:
        print(f"[observability] Sentry init failed: {e}", flush=True)
        return False


def capture_exception(exc=None, **context):
    """Send an exception to Sentry with optional context tags.
    Falls back to print() if Sentry isn't initialized."""
    if not _SENTRY_AVAILABLE:
        # Plain stderr fallback
        print(f"[observability] EXCEPTION: {exc!r}", file=sys.stderr, flush=True)
        if context:
            print(f"  context: {context}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in (context or {}).items():
                scope.set_tag(k, str(v)[:200])
            sentry_sdk.capture_exception(exc)
    except Exception as e:
        print(f"[observability] capture_exception failed: {e}", file=sys.stderr)


def capture_message(message, level="info", **context):
    """Send a non-exception message to Sentry. Useful for key events
    (kill switch trip, large gain/loss, etc.) we want visible in the
    Sentry feed alongside errors."""
    if not _SENTRY_AVAILABLE:
        print(f"[observability] {level.upper()}: {message}", flush=True)
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in (context or {}).items():
                scope.set_tag(k, str(v)[:200])
            sentry_sdk.capture_message(message, level=level)
    except Exception as e:
        print(f"[observability] capture_message failed: {e}", file=sys.stderr)


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
        print(f"[observability] ntfy critical alert failed: {e}", flush=True)
    # 3. Email (queue via existing module if available)
    try:
        if user and user.get("notification_email"):
            from email_sender import queue_email
            queue_email(
                user, "trading-bot-alert",
                subject=f"⚠ {title}",
                body=f"{title}\n\n{body}\n\nTimestamp: {datetime.utcnow().isoformat()}Z",
            )
            result["email"] = True
    except Exception as e:
        print(f"[observability] critical email failed: {e}", flush=True)
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
