#!/usr/bin/env python3
"""
Push notification sender for the trading bot.
Uses ntfy.sh — free, no account needed.
Install the ntfy app on your phone and subscribe to topic: alpaca-trading-bot-kevin
"""
import json
import sys
import os
import urllib.request
from datetime import datetime, timezone

NTFY_TOPIC = "alpaca-trading-bot-kevin"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

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

def send_notification(message, notify_type="info"):
    config = TYPE_CONFIG.get(notify_type, TYPE_CONFIG["info"])

    data = message.encode("utf-8")
    req = urllib.request.Request(NTFY_URL, data=data, method="POST")
    req.add_header("Title", config["title"])
    req.add_header("Priority", config["priority"])
    req.add_header("Tags", config["tags"])
    req.add_header("Email", "se2login@gmail.com")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Notification sent: [{notify_type}] {message}")
            return True
    except Exception as e:
        print(f"Failed to send notification: {e}")
        return False

# Also keep the queue file for backup/logging
def log_notification(message, notify_type="info"):
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notification_log.json")
    log = []
    if os.path.exists(log_file):
        try:
            with open(log_file) as f:
                log = json.load(f)
        except:
            log = []

    log.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": notify_type,
        "message": message
    })

    # Keep last 100 notifications
    log = log[-100:]

    with open(log_file, "w") as f:
        json.dump(log, f, indent=2)

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
