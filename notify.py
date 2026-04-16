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

def send_notification(message, notify_type="info"):
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

    # Queue email for important notification types
    if notify_type in EMAIL_TYPES:
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "to": EMAIL_RECIPIENT,
            "subject": f"[Trading Bot] {subject}",
            "body": body,
            "type": notify_type,
            "sent": False
        })

        # Keep last 50 queued emails
        queue = queue[-50:]

        # Atomic write
        safe_save_json(queue_file, queue)
    finally:
        if lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
    print(f"Email queued for {EMAIL_RECIPIENT}: {subject}")


# Also keep the queue file for backup/logging
def log_notification(message, notify_type="info"):
    log_file = os.path.join(DATA_DIR, "notification_log.json")
    log = []
    if os.path.exists(log_file):
        try:
            with open(log_file) as f:
                log = json.load(f)
        except (OSError, json.JSONDecodeError):
            log = []

    log.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": notify_type,
        "message": message
    })

    # Keep last 100 notifications
    log = log[-100:]

    safe_save_json(log_file, log)

if __name__ == "__main__":
    notify_type = "info"
    args = sys.argv[1:]
    if "--type" in args:
        idx = args.index("--type")
        notify_type = args[idx + 1]
        args = args[:idx] + args[idx+2:]

    message = " ".join(args) if args else "Test notification from trading bot"
    send_notification(message, notify_type)
    log_notification(message, notify_type)
