#!/usr/bin/env python3
"""
Push notification sender for the trading bot.
Uses ntfy.sh — free, no account needed.
Install the ntfy app on your phone and subscribe to your topic.

Also queues email notifications for se2login@gmail.com.
The scheduled task picks up pending emails and sends them via Gmail MCP.
"""
import fcntl
import json
import sys
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from et_time import now_et


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


def safe_save_json(path, data):
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp_path, path)
    except:
        try: os.unlink(tmp_path)
        except: pass
        raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent runtime data lives. On Railway, set to a volume mount
# path (e.g. /data). Locally defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "alpaca-bot-" + os.environ.get("USER", "default"))
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
EMAIL_RECIPIENT = "se2login@gmail.com"

# Emoji/priority mapping
TYPE_CONFIG = {
    "trade": {"priority": "default", "tags": "chart_with_upwards_trend", "title": "Trade Placed"},
    "exit": {"priority": "default", "tags": "money_bag", "title": "Position Closed"},
    "stop": {"priority": "high", "tags": "rotating_light", "title": "Stop-Loss Triggered"},
    "alert": {"priority": "urgent", "tags": "warning", "title": "Bot Alert"},
    "kill": {"priority": "max", "tags": "octagonal_sign", "title": "KILL SWITCH ACTIVATED"},
    "daily": {"priority": "low", "tags": "bar_chart", "title": "Daily Summary"},
    "learn": {"priority": "low", "tags": "brain", "title": "Bot Learning Update"},
    "info": {"priority": "min", "tags": "information_source", "title": "Bot Info"},
}

# Types that should also trigger an email notification
EMAIL_TYPES = {"trade", "exit", "stop", "alert", "kill", "daily"}

def send_notification(message, notify_type="info", push_only=False):
    config = TYPE_CONFIG.get(notify_type, TYPE_CONFIG["info"])

    # Send push notification via ntfy
    data = message.encode("utf-8")
    req = urllib.request.Request(NTFY_URL, data=data, method="POST")
    req.add_header("Title", config["title"])
    req.add_header("Priority", config["priority"])
    req.add_header("Tags", config["tags"])

    push_ok = False
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Push notification sent: [{notify_type}] {message}")
            push_ok = True
    except Exception as e:
        print(f"Failed to send push notification: {e}")

    # Queue email for important notification types — UNLESS the caller
    # asked for push-only. The daily-close path uses --push-only so the
    # scheduler can send the short summary via ntfy while queueing its
    # own much richer email body separately (see cloud_scheduler
    # run_daily_close).
    if notify_type in EMAIL_TYPES and not push_only:
        queue_email(config["title"], message, notify_type)

    return push_ok


def queue_email(subject, body, notify_type="info"):
    """Queue an email notification with file locking for concurrency safety."""
    queue_file = os.path.join(DATA_DIR, "email_queue.json")
    lock_file = queue_file + ".lock"
    lock_fd = None
    try:
        lock_fd = open(lock_file, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Read existing queue
        queue = []
        if os.path.exists(queue_file):
            try:
                with open(queue_file) as f:
                    queue = json.load(f)
            except (json.JSONDecodeError, OSError):
                queue = []

        queue.append({
            "timestamp": now_et().isoformat(),
            "to": EMAIL_RECIPIENT,
            "subject": f"[Trading Bot] {subject}",
            "body": body,
            "type": notify_type,
            "sent": False
        })

        # Keep last 50 queued emails. Move overflow entries to a
        # dead-letter file rather than silently dropping them — ops
        # can diff the DLQ to recover lost trade notifications.
        if len(queue) > 50:
            overflow = queue[:-50]
            dlq_file = os.path.join(DATA_DIR, "email_queue_dlq.json")
            dlq_ok = False
            try:
                dlq = []
                if os.path.exists(dlq_file):
                    try:
                        with open(dlq_file) as f:
                            dlq = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        dlq = []
                dlq.extend(overflow)
                # Cap DLQ at 500 so a wedged sender can't eat the volume.
                if len(dlq) > 500:
                    dlq = dlq[-500:]
                safe_save_json(dlq_file, dlq)
                dlq_ok = True
                print(f"[notify] WARN: email queue overflow — moved {len(overflow)} "
                      f"entries to email_queue_dlq.json. Check the email sender.",
                      flush=True)
            except Exception as _dlq_e:
                # DLQ write failed — DON'T drop the overflow. Keep it in the
                # main queue (may grow beyond 50) so we don't lose trade
                # notifications. Next iteration will retry the DLQ move.
                print(f"[notify] WARN: email queue overflow — DLQ write failed "
                      f"({_dlq_e}); keeping {len(overflow)} overflow entries "
                      f"in main queue to avoid data loss.", flush=True)
            # Only trim the main queue when DLQ succeeded.
            if dlq_ok:
                queue = queue[-50:]
            else:
                # Round-14: DLQ failed but we're not silently losing data.
                # Hard cap the main queue at 200 entries as a memory-safety
                # net so a permanently-wedged DLQ writer can't grow the
                # queue file unboundedly. 200 = 4× the soft cap, gives the
                # DLQ writer plenty of retries to recover before any drop.
                if len(queue) > 200:
                    dropped = len(queue) - 200
                    queue = queue[-200:]
                    print(f"[notify] WARN: queue exceeded 200 entries with "
                          f"DLQ stuck — dropping {dropped} oldest. Investigate "
                          f"DLQ writer immediately.", flush=True)

        # Atomic write
        safe_save_json(queue_file, queue)
    finally:
        if lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
    print(f"Email queued for {EMAIL_RECIPIENT}: {subject}")


# Also keep the queue file for backup/logging
def log_notification(message, notify_type="info"):
    """Append to notification_log.json under a fcntl lock so concurrent
    callers (HTTP handler + scheduler thread) don't lose entries via the
    classic read-modify-write race."""
    log_file = os.path.join(DATA_DIR, "notification_log.json")
    lock_file = log_file + ".lock"
    lock_fd = None
    try:
        lock_fd = open(lock_file, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        log = []
        if os.path.exists(log_file):
            try:
                with open(log_file) as f:
                    log = json.load(f)
            except (OSError, json.JSONDecodeError):
                log = []

        log.append({
            "timestamp": now_et().isoformat(),
            "type": notify_type,
            "message": message,
        })

        # Keep last 100 notifications
        log = log[-100:]

        safe_save_json(log_file, log)
    finally:
        if lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

if __name__ == "__main__":
    notify_type = "info"
    args = sys.argv[1:]
    if "--type" in args:
        idx = args.index("--type")
        notify_type = args[idx + 1]
        args = args[:idx] + args[idx+2:]

    # --push-only skips the email queue entirely. Used by paths that
    # will queue their own richer email directly (daily close report).
    push_only = False
    if "--push-only" in args:
        args.remove("--push-only")
        push_only = True

    # Sensitive messages (password resets) can be piped via stdin with --stdin
    # flag so the plaintext URL never appears in argv (which is readable via
    # /proc on the host and may be logged by process supervisors).
    if "--stdin" in args:
        args.remove("--stdin")
        message = sys.stdin.read().strip() or (" ".join(args) if args else "Notification")
    else:
        message = " ".join(args) if args else "Test notification from trading bot"
    send_notification(message, notify_type, push_only=push_only)
    log_notification(message, notify_type)
