#!/usr/bin/env python3
"""
Gmail SMTP email sender — drains pending emails from every user's
`email_queue.json` and ships them via Gmail's SMTP gateway.

Called on a cadence by `cloud_scheduler.py` (every ~60s during market
hours, and immediately after daily close). Safe to call manually too.

Env vars (set on Railway):
    GMAIL_USER            — the Gmail address that will appear as "From"
    GMAIL_APP_PASSWORD    — a Google App Password (16-char, no spaces).
                            Generate at https://myaccount.google.com/apppasswords.
    SMTP_HOST             — optional, defaults to smtp.gmail.com
    SMTP_PORT             — optional, defaults to 587 (STARTTLS)
    EMAIL_SENDER_DISABLED — set to "1" to short-circuit all sends
                            (useful for local dev, or to pause email
                            without losing the queue)

Queue format (one file per user at DATA_DIR/users/{id}/email_queue.json):
    [{ "to": "...", "subject": "...", "body": "...",
       "sent": false, "timestamp": "...", "type": "trade|exit|..." }]

Design notes:
  - Per-user files are locked via `fcntl.flock` so a concurrent write from
    notify.py or auth_mixin.py doesn't race with the drain.
  - After sending, entries are marked `sent: true` rather than removed,
    so a transient SMTP failure can be retried without double-send.
  - Sent entries older than 24h are trimmed — keeps the file bounded.
  - Unsent entries older than 7d are dropped (dead-letter) with a WARN,
    so a permanently broken target address can't grow the queue forever.
  - One SMTP session reused for the entire drain pass — cheaper than
    reconnecting per message.
"""
from __future__ import annotations

import fcntl
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from et_time import now_et


# ===== Config =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
USERS_DIR = os.path.join(DATA_DIR, "users")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_TIMEOUT = 20  # seconds

# "Sent" entries older than this get trimmed from the queue file.
SENT_RETENTION = timedelta(hours=24)
# Unsent entries older than this become dead-letters (dropped + logged).
DEAD_LETTER_AGE = timedelta(days=7)


def _log(msg: str) -> None:
    print(f"[email_sender] {msg}", flush=True)


def _creds():
    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    return user, pw


def enabled() -> bool:
    """True if sender has creds AND isn't explicitly disabled."""
    if os.environ.get("EMAIL_SENDER_DISABLED") == "1":
        return False
    user, pw = _creds()
    return bool(user and pw)


def _parse_ts(ts: str):
    try:
        # ET timestamps use fromisoformat-friendly offsets; fall back to UTC.
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.now(timezone.utc)


# ===== SMTP session =====
def _open_session():
    user, pw = _creds()
    if not (user and pw):
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD not set")
    ctx = ssl.create_default_context()
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
    s.ehlo()
    s.starttls(context=ctx)
    s.ehlo()
    s.login(user, pw)
    return s


def _build_message(entry: dict, from_addr: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = entry.get("to", "").strip()
    msg["Subject"] = entry.get("subject", "[Trading Bot] Notification")
    msg["Message-ID"] = make_msgid(domain="alpaca-bot.local")
    # Plain text body. Bot messages are short enough that HTML adds no value.
    body = entry.get("body") or ""
    ts = entry.get("timestamp") or ""
    footer = (
        "\n\n—\nStock Trading Bot • paper trading\n"
        f"Sent: {ts}\n"
        "To stop these emails, open the dashboard → profile → notifications."
    )
    msg.set_content(body + footer)
    return msg


def _send_one(s: smtplib.SMTP, entry: dict, from_addr: str) -> tuple[bool, str]:
    try:
        to = (entry.get("to") or "").strip()
        if not to:
            return False, "missing 'to'"
        msg = _build_message(entry, from_addr)
        s.send_message(msg)
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ===== Queue drain =====
def _drain_queue_file(path: str, s: smtplib.SMTP, from_addr: str) -> tuple[int, int, int]:
    """Return (sent, failed, trimmed)."""
    lock_path = path + ".lock"
    lock_fd = None
    sent = failed = trimmed = 0
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        if not os.path.exists(path):
            return 0, 0, 0
        try:
            with open(path) as f:
                queue = json.load(f)
        except (json.JSONDecodeError, OSError):
            _log(f"WARN: {path} unreadable, resetting")
            queue = []

        now = datetime.now(timezone.utc)
        next_queue: list[dict] = []

        for entry in queue:
            if not isinstance(entry, dict):
                continue

            ts = _parse_ts(entry.get("timestamp", ""))
            # Normalise tz for comparison
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = now - ts

            # Dead-letter old unsent entries — permanently bad recipient
            # or a bug; either way, don't let the queue grow forever.
            if not entry.get("sent") and age > DEAD_LETTER_AGE:
                _log(f"DEAD-LETTER (age {age}): to={entry.get('to')} subj={entry.get('subject')}")
                trimmed += 1
                continue

            # Trim sent entries past retention window
            if entry.get("sent") and age > SENT_RETENTION:
                trimmed += 1
                continue

            # Attempt send on unsent entries
            if not entry.get("sent"):
                ok, detail = _send_one(s, entry, from_addr)
                if ok:
                    entry["sent"] = True
                    entry["sent_at"] = now_et().isoformat()
                    sent += 1
                else:
                    entry["last_error"] = detail
                    entry["attempts"] = int(entry.get("attempts", 0)) + 1
                    failed += 1

            next_queue.append(entry)

        # Atomic write
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(next_queue, f, indent=2, default=str)
        os.replace(tmp, path)

    finally:
        if lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    return sent, failed, trimmed


def drain_all() -> dict:
    """Walk every user's queue + the root queue, send, return a summary."""
    summary = {"users": 0, "sent": 0, "failed": 0, "trimmed": 0, "skipped": False}

    if not enabled():
        summary["skipped"] = True
        summary["reason"] = "disabled or missing GMAIL_USER / GMAIL_APP_PASSWORD"
        return summary

    from_addr, _ = _creds()

    # Gather candidate queue files
    paths: list[str] = []
    root_q = os.path.join(DATA_DIR, "email_queue.json")
    if os.path.exists(root_q):
        paths.append(root_q)
    if os.path.isdir(USERS_DIR):
        for uid in os.listdir(USERS_DIR):
            p = os.path.join(USERS_DIR, uid, "email_queue.json")
            if os.path.exists(p):
                paths.append(p)

    if not paths:
        return summary

    try:
        s = _open_session()
    except Exception as e:
        _log(f"SMTP connect failed: {e}")
        summary["skipped"] = True
        summary["reason"] = f"smtp-connect: {e}"
        return summary

    try:
        for p in paths:
            try:
                sent, failed, trimmed = _drain_queue_file(p, s, from_addr)
                summary["users"] += 1
                summary["sent"] += sent
                summary["failed"] += failed
                summary["trimmed"] += trimmed
                if sent or failed or trimmed:
                    _log(f"{p}: sent={sent} failed={failed} trimmed={trimmed}")
            except Exception as e:
                _log(f"ERROR draining {p}: {e}")
                summary["failed"] += 1
    finally:
        try:
            s.quit()
        except Exception:
            pass

    return summary


def send_test(to_addr: str) -> tuple[bool, str]:
    """Fire a one-off test email (bypasses the queue). For CLI sanity check."""
    if not enabled():
        return False, "disabled or missing creds"
    from_addr, _ = _creds()
    try:
        s = _open_session()
    except Exception as e:
        return False, f"smtp-connect: {e}"
    try:
        ok, detail = _send_one(s, {
            "to": to_addr,
            "subject": "[Trading Bot] Test email",
            "body": "This is a test from the Alpaca paper-trading bot. If you got this, Gmail SMTP is wired correctly.",
            "timestamp": now_et().isoformat(),
        }, from_addr)
        return ok, detail
    finally:
        try:
            s.quit()
        except Exception:
            pass


if __name__ == "__main__":
    # Usage:
    #   python email_sender.py              → drain all queues once
    #   python email_sender.py test <addr>  → send a test email
    if len(sys.argv) >= 3 and sys.argv[1] == "test":
        ok, detail = send_test(sys.argv[2])
        print(("OK " if ok else "FAIL ") + detail)
        sys.exit(0 if ok else 1)
    result = drain_all()
    print(json.dumps(result, indent=2))
