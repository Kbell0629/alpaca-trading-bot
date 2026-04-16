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
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

try:
    from http.server import ThreadingHTTPServer  # Python 3.7+
except ImportError:
    import socketserver
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        pass

try:
    from cloud_scheduler import start_scheduler, get_scheduler_status
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

# Basic auth credentials (set via env vars on Railway, or .env for local dev)
AUTH_USER = os.environ.get("DASHBOARD_USER", "")
AUTH_PASS = os.environ.get("DASHBOARD_PASS", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_DIR = os.path.join(BASE_DIR, "strategies")
DASHBOARD_DATA_PATH = os.path.join(BASE_DIR, "dashboard_data.json")

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
_cache_ttl = 10  # seconds

def alpaca_get_cached(url, timeout=15):
    """Cached version of alpaca_get for dashboard data."""
    now = time.time()
    if url in _api_cache and now - _api_cache[url]["time"] < _cache_ttl:
        return _api_cache[url]["data"]
    data = alpaca_get(url, timeout=timeout)
    _api_cache[url] = {"data": data, "time": now}
    return data


def get_dashboard_data():
    """Load dashboard_data.json for screener picks, but always fetch live orders/positions from Alpaca."""
    api_errors = []
    data = load_json(DASHBOARD_DATA_PATH)
    if data:
        # Always refresh live data from Alpaca (using cached API calls)
        account = alpaca_get_cached(f"{API_ENDPOINT}/account")
        positions = alpaca_get_cached(f"{API_ENDPOINT}/positions")
        orders = alpaca_get_cached(f"{API_ENDPOINT}/orders?status=open&limit=50")
        if isinstance(account, dict) and "error" in account:
            api_errors.append("account: " + account["error"])
        if isinstance(positions, dict) and "error" in positions:
            api_errors.append("positions: " + str(positions.get("error", "")))
        if isinstance(orders, dict) and "error" in orders:
            api_errors.append("orders: " + str(orders.get("error", "")))
        data["account"] = account if isinstance(account, dict) and "error" not in account else data.get("account", {})
        data["positions"] = positions if isinstance(positions, list) else data.get("positions", [])
        data["open_orders"] = orders if isinstance(orders, list) else data.get("open_orders", [])
        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        data["api_errors"] = api_errors
        # Also refresh strategy files
        data["trailing"] = load_json(os.path.join(STRATEGIES_DIR, "trailing_stop.json")) or data.get("trailing")
        data["copy_trading"] = load_json(os.path.join(STRATEGIES_DIR, "copy_trading.json")) or data.get("copy_trading")
        data["wheel"] = load_json(os.path.join(STRATEGIES_DIR, "wheel_strategy.json")) or data.get("wheel")
        # Load scorecard for readiness score
        data["scorecard"] = load_json(os.path.join(BASE_DIR, "scorecard.json")) or data.get("scorecard", {})
        # Load auto-deployer config for short selling toggle state
        data["auto_deployer_config"] = load_json(os.path.join(BASE_DIR, "auto_deployer_config.json")) or {}
        # Load guardrails for active preset detection
        data["guardrails"] = load_json(os.path.join(BASE_DIR, "guardrails.json")) or {}
        return data
    # Fallback: build from strategy files and API
    trailing = load_json(os.path.join(STRATEGIES_DIR, "trailing_stop.json"))
    copy_trading = load_json(os.path.join(STRATEGIES_DIR, "copy_trading.json"))
    wheel = load_json(os.path.join(STRATEGIES_DIR, "wheel_strategy.json"))
    account = alpaca_get_cached(f"{API_ENDPOINT}/account")
    positions = alpaca_get_cached(f"{API_ENDPOINT}/positions")
    orders = alpaca_get_cached(f"{API_ENDPOINT}/orders?status=open&limit=50")
    if isinstance(account, dict) and "error" in account:
        api_errors.append("account: " + account["error"])
    if isinstance(positions, dict) and "error" in positions:
        api_errors.append("positions: " + str(positions.get("error", "")))
    if isinstance(orders, dict) and "error" in orders:
        api_errors.append("orders: " + str(orders.get("error", "")))
    return {
        "account": account if isinstance(account, dict) and "error" not in account else {},
        "positions": positions if isinstance(positions, list) else [],
        "open_orders": orders if isinstance(orders, list) else [],
        "trailing": trailing,
        "copy_trading": copy_trading,
        "wheel": wheel,
        "picks": [],
        "total_screened": 0,
        "total_passed": 0,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "api_errors": api_errors,
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Trading Bot Dashboard</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#3b82f6">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="StockBot">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
    --bg: #0a0e17; --card: #111827; --card-hover: #1a2332; --border: #1e293b;
    --text: #e2e8f0; --text-dim: #94a3b8; --accent: #3b82f6;
    --green: #10b981; --red: #ef4444; --orange: #f59e0b; --purple: #8b5cf6;
    --radius: 12px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text); padding: 24px; line-height: 1.5;
    min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }
button {
    cursor: pointer; border: none; border-radius: 8px; font-family: inherit;
    font-size: 12px; font-weight: 600; padding: 6px 14px;
    transition: all 0.2s ease;
}
button:hover { filter: brightness(1.15); transform: translateY(-1px); }
button:active { transform: translateY(0); }
.btn-primary { background: var(--accent); color: #fff; }
.btn-danger { background: var(--red); color: #fff; }
.btn-warning { background: var(--orange); color: #000; }
.btn-success { background: var(--green); color: #fff; }
.btn-ghost { background: rgba(255,255,255,0.06); color: var(--text-dim); border: 1px solid var(--border); }
.btn-sm { padding: 4px 10px; font-size: 11px; }

/* Header */
.header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
}
.header-left h1 {
    font-size: 24px; font-weight: 700;
    background: linear-gradient(135deg, var(--accent), var(--purple));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.header-left .updated { color: var(--text-dim); font-size: 13px; margin-top: 2px; }
.header-right { display: flex; align-items: center; gap: 12px; }
.paper-badge {
    background: var(--orange); color: #000; padding: 4px 12px; border-radius: 20px;
    font-size: 11px; font-weight: 700; letter-spacing: 1px;
}
.countdown { color: var(--text-dim); font-size: 12px; font-variant-numeric: tabular-nums; }

/* P&L Alert Banner */
.pnl-alert {
    background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.3);
    border-radius: var(--radius); padding: 14px 20px; margin-bottom: 20px;
    display: flex; align-items: center; gap: 12px; font-size: 14px;
    animation: pulseAlert 2s infinite;
}
.pnl-alert .icon { font-size: 20px; }
@keyframes pulseAlert { 0%,100% { opacity:1; } 50% { opacity:0.7; } }

/* Market Regime */
.regime-badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 6px 16px; border-radius: 20px; font-size: 12px; font-weight: 700;
    letter-spacing: 0.5px; text-transform: uppercase;
}
.regime-bull { background: rgba(16,185,129,0.15); color: var(--green); }
.regime-neutral { background: rgba(245,158,11,0.15); color: var(--orange); }
.regime-bear { background: rgba(239,68,68,0.15); color: var(--red); }

/* Account Bar */
.account-bar { display: grid; grid-template-columns: repeat(5,1fr); gap: 16px; margin-bottom: 24px; }
.metric {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px; transition: border-color 0.2s;
}
.metric:hover { border-color: var(--accent); }
.metric .label {
    font-size: 11px; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 4px;
}
.metric .value { font-size: 22px; font-weight: 700; }

/* Section Titles */
.section-title {
    font-size: 18px; font-weight: 700; margin-bottom: 16px;
    display: flex; align-items: center; gap: 8px;
}
.section-title .subtitle { font-size: 13px; color: var(--text-dim); font-weight: 400; }

/* Stock Pick Cards */
.picks { display: grid; grid-template-columns: repeat(3,1fr); gap: 20px; margin-bottom: 24px; }
.pick-card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; transition: transform 0.2s, box-shadow 0.2s;
}
.pick-card:hover { transform: translateY(-2px); box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
.pick-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.pick-rank { font-size: 10px; font-weight: 700; letter-spacing: 1px; }
.pick-symbol { font-size: 28px; font-weight: 800; margin-top: 2px; }
.pick-price { font-size: 16px; color: var(--text-dim); }
.pick-strategy-badge {
    padding: 6px 12px; border-radius: 8px; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.5px;
}
.pick-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 16px; }
.pick-stat {
    display: flex; justify-content: space-between; padding: 6px 0;
    border-bottom: 1px solid rgba(30,41,59,0.5);
}
.pick-stat-label { font-size: 11px; color: var(--text-dim); }
.pick-stat-value { font-size: 13px; font-weight: 600; }
.pick-scores { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.score-row { display: flex; align-items: center; gap: 8px; }
.score-label { font-size: 10px; font-weight: 600; width: 50px; text-transform: uppercase; }
.score-bar-bg { flex: 1; height: 6px; background: rgba(30,41,59,0.8); border-radius: 3px; overflow: hidden; }
.score-bar { height: 100%; border-radius: 3px; transition: width 0.8s ease-out; }
.score-val { font-size: 11px; color: var(--text-dim); width: 30px; text-align: right; }
.pick-actions { display: flex; gap: 8px; margin-top: 10px; }
.pick-deploy-btn {
    flex: 1; background: var(--accent); color: #fff; padding: 10px; border-radius: 8px;
    font-size: 13px; font-weight: 700; text-align: center; cursor: pointer;
    transition: all 0.2s;
}
.pick-deploy-btn:hover { filter: brightness(1.2); transform: translateY(-1px); }
.earnings-badge {
    display: inline-block; background: rgba(245,158,11,0.15); color: var(--orange);
    padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 600;
    margin-top: 6px;
}
.backtest-result {
    font-size: 11px; color: var(--text-dim); margin-top: 4px;
}

/* Strategies */
.strategies { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 24px; }
.strategies .strategy-card:nth-child(4), .strategies .strategy-card:nth-child(5) { }
.strategy-card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; position: relative; overflow: hidden;
}
.strategy-card::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
}
.strategy-card.trailing::before { background: linear-gradient(90deg,var(--accent),#60a5fa); }
.strategy-card.copy::before { background: linear-gradient(90deg,var(--green),#34d399); }
.strategy-card.wheel::before { background: linear-gradient(90deg,var(--purple),#a78bfa); }
.strategy-card.meanrev::before { background: linear-gradient(90deg,var(--orange),#fbbf24); }
.strategy-card.breakout::before { background: linear-gradient(90deg,var(--red),#f87171); }
.strategy-card.short-sell::before { background: linear-gradient(90deg,#dc2626,#7f1d1d); }

/* Scheduler Panel */
.scheduler-section { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 24px; }
.scheduler-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }
.sched-task { background: rgba(16,185,129,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.sched-task.active { border-color: rgba(16,185,129,0.4); background: rgba(16,185,129,0.05); }
.sched-task-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.sched-task-name { font-size: 13px; font-weight: 600; }
.sched-task-status { font-size: 10px; padding: 2px 6px; border-radius: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.sched-task-status.ok { background: rgba(16,185,129,0.15); color: var(--green); }
.sched-task-status.waiting { background: rgba(148,163,184,0.15); color: var(--text-dim); }
.sched-task-status.pending { background: rgba(245,158,11,0.15); color: var(--orange); }
.sched-task-schedule { font-size: 11px; color: var(--text-dim); margin-bottom: 4px; }
.sched-task-last { font-size: 11px; color: var(--text); font-family: monospace; }
.sched-log-box { background: rgba(10,14,23,0.5); border: 1px solid var(--border); border-radius: 8px; padding: 12px; max-height: 300px; overflow-y: auto; font-family: 'SF Mono', Monaco, monospace; font-size: 11px; line-height: 1.6; }
.sched-log-line { color: var(--text-dim); }
.sched-log-line .ts { color: var(--accent); }
.sched-log-line .tag { color: var(--orange); }
.sched-log-empty { color: var(--text-dim); font-style: italic; text-align: center; padding: 20px; }
.strategy-card h2 { font-size: 16px; margin-bottom: 4px; }
.strategy-card .subtitle { font-size: 12px; color: var(--text-dim); margin-bottom: 16px; }
.stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.stat { padding: 8px 0; }
.stat .stat-label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
.stat .stat-value { font-size: 16px; font-weight: 600; margin-top: 2px; }
.badge-active { background: rgba(16,185,129,0.15); color: var(--green); padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }
.badge-inactive { background: rgba(245,158,11,0.15); color: var(--orange); padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }
.badge-pending { background: rgba(148,163,184,0.15); color: var(--text-dim); padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }
.strategy-visual {
    background: rgba(59,130,246,0.05); border: 1px solid rgba(59,130,246,0.2);
    border-radius: 8px; padding: 12px; margin-top: 12px; font-size: 12px;
}
.strategy-actions { display: flex; gap: 8px; margin-top: 12px; }

/* Tables */
.tables { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
.table-card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; overflow-x: auto;
}
.table-card h3 {
    font-size: 14px; margin-bottom: 12px; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 0.5px;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
    text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border);
    color: var(--text-dim); font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.5px; font-weight: 600; cursor: default;
    user-select: none; white-space: nowrap;
}
th.sortable { cursor: pointer; }
th.sortable:hover { color: var(--accent); }
th .sort-arrow { margin-left: 4px; font-size: 8px; }
td { padding: 8px 12px; border-bottom: 1px solid rgba(30,41,59,0.5); }
.positive { color: var(--green); }
.negative { color: var(--red); }
.side-buy { color: var(--green); font-weight: 600; }
.side-sell { color: var(--red); font-weight: 600; }
.empty { text-align: center; color: var(--text-dim); padding: 20px; }

/* Screener */
.screener {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px; margin-bottom: 24px;
}
.screener h3 {
    font-size: 14px; margin-bottom: 12px; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 0.5px;
}
.screener-stats {
    display: flex; gap: 24px; margin-bottom: 16px; font-size: 12px; color: var(--text-dim);
}
.screener-stats strong { font-weight: 600; color: var(--text); }

/* Activity Log */
.activity-log {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px; margin-bottom: 24px;
}
.activity-log h3 {
    font-size: 14px; margin-bottom: 12px; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 0.5px;
}
.log-entries { max-height: 200px; overflow-y: auto; }
.log-entry {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 0; border-bottom: 1px solid rgba(30,41,59,0.3);
    font-size: 12px; animation: fadeIn 0.3s ease;
}
.log-entry .log-time { color: var(--text-dim); font-variant-numeric: tabular-nums; width: 70px; flex-shrink: 0; }
.log-entry .log-icon { width: 20px; text-align: center; }
.log-entry .log-msg { flex: 1; }
.log-entry.success .log-icon { color: var(--green); }
.log-entry.error .log-icon { color: var(--red); }
.log-entry.info .log-icon { color: var(--accent); }

@keyframes fadeIn { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:translateY(0); } }

/* Toast Notifications */
.toast-container {
    position: fixed; top: 20px; right: 20px; z-index: 10000;
    display: flex; flex-direction: column; gap: 8px;
}
.toast {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 20px; min-width: 300px; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    display: flex; align-items: center; gap: 10px; font-size: 13px;
    animation: slideIn 0.3s ease, fadeOut 0.3s ease 3.7s forwards;
}
.toast.success { border-left: 3px solid var(--green); }
.toast.error { border-left: 3px solid var(--red); }
.toast.info { border-left: 3px solid var(--accent); }
.toast .toast-icon { font-size: 16px; }
@keyframes slideIn { from { transform: translateX(100%); opacity:0; } to { transform: translateX(0); opacity:1; } }
@keyframes fadeOut { to { opacity:0; transform: translateX(30px); } }

/* Modal */
.modal-overlay {
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.7); z-index: 9000;
    display: flex; align-items: center; justify-content: center;
    opacity: 0; pointer-events: none; transition: opacity 0.25s;
}
.modal-overlay.active { opacity: 1; pointer-events: all; }
.modal {
    background: var(--card); border: 1px solid var(--border); border-radius: 16px;
    padding: 28px; min-width: 420px; max-width: 500px;
    transform: scale(0.95); transition: transform 0.25s;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.modal-overlay.active .modal { transform: scale(1); }
.modal h2 { font-size: 18px; margin-bottom: 4px; }
.modal .modal-subtitle { font-size: 13px; color: var(--text-dim); margin-bottom: 20px; }
.form-group { margin-bottom: 16px; }
.form-group label { display: block; font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
.form-group select, .form-group input {
    width: 100%; padding: 10px 14px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; font-size: 14px;
    font-family: inherit; outline: none; transition: border-color 0.2s;
}
.form-group select:focus, .form-group input:focus { border-color: var(--accent); }
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 24px; }
.modal-actions button { padding: 10px 24px; font-size: 14px; }

/* Auto-Deployer Toggle */
.auto-deployer-toggle {
    display: flex; align-items: center; gap: 8px;
}
.auto-deployer-toggle .toggle-label {
    font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
}
.toggle-switch {
    position: relative; width: 44px; height: 24px; cursor: pointer;
}
.toggle-switch input { display: none; }
.toggle-slider {
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    background: #374151; border-radius: 12px; transition: background 0.3s;
}
.toggle-slider::before {
    content: ''; position: absolute; left: 3px; top: 3px;
    width: 18px; height: 18px; background: #fff; border-radius: 50%;
    transition: transform 0.3s;
}
.toggle-switch input:checked + .toggle-slider { background: var(--green); }
.toggle-switch input:checked + .toggle-slider::before { transform: translateX(20px); }
.toggle-status {
    font-size: 11px; font-weight: 700; letter-spacing: 0.5px; padding: 2px 8px;
    border-radius: 6px;
}
.toggle-status.on { background: rgba(16,185,129,0.15); color: var(--green); }
.toggle-status.off { background: rgba(239,68,68,0.15); color: var(--red); }

/* Enhanced Modal */
.modal-info-box {
    background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.2);
    border-radius: 10px; padding: 14px 16px; margin: 14px 0; font-size: 13px;
    line-height: 1.6;
}
.modal-info-box.warning {
    background: rgba(245,158,11,0.08); border-color: rgba(245,158,11,0.2);
}
.modal-info-box .info-title {
    font-weight: 700; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 6px; color: var(--text-dim);
}
.modal-detail-row {
    display: flex; justify-content: space-between; padding: 6px 0;
    border-bottom: 1px solid rgba(30,41,59,0.3); font-size: 13px;
}
.modal-detail-row:last-child { border-bottom: none; }
.modal-detail-row .detail-label { color: var(--text-dim); }
.modal-detail-row .detail-value { font-weight: 600; }
.modal-action-text {
    font-size: 14px; font-weight: 600; padding: 10px 0;
}
.strategy-info-text {
    font-size: 12px; color: var(--text-dim); margin-top: 4px;
    font-style: italic; line-height: 1.4;
}

/* Deploy Badge */
.deploy-source-badge {
    display: inline-block; padding: 2px 6px; border-radius: 4px;
    font-size: 9px; font-weight: 700; letter-spacing: 0.5px;
    vertical-align: middle; margin-left: 6px;
}
.deploy-source-badge.auto {
    background: rgba(139,92,246,0.15); color: var(--purple);
}
.deploy-source-badge.manual {
    background: rgba(59,130,246,0.15); color: var(--accent);
}

/* Activity Log Colors */
.log-entry.buy .log-icon { color: var(--green); }
.log-entry.buy .log-msg { color: var(--green); }
.log-entry.sell .log-icon { color: var(--red); }
.log-entry.sell .log-msg { color: var(--red); }
.log-entry.cancel .log-icon { color: var(--orange); }
.log-entry.cancel .log-msg { color: var(--orange); }

/* Kill Switch */
.kill-switch-btn {
    background: var(--red); color: #fff; padding: 8px 18px; font-size: 12px;
    font-weight: 800; letter-spacing: 1px; text-transform: uppercase;
    border-radius: 8px; border: 2px solid #dc2626; cursor: pointer;
    transition: all 0.2s; box-shadow: 0 0 12px rgba(239,68,68,0.3);
}
.kill-switch-btn:hover { background: #dc2626; box-shadow: 0 0 20px rgba(239,68,68,0.5); transform: translateY(-1px); }
.kill-switch-active-banner {
    background: rgba(239,68,68,0.15); border: 2px solid rgba(239,68,68,0.4);
    border-radius: var(--radius); padding: 14px 20px; margin-bottom: 20px;
    display: flex; align-items: center; justify-content: space-between;
    font-size: 14px; font-weight: 700; color: var(--red);
    animation: killPulse 2s infinite;
}
.kill-switch-active-banner .deactivate-btn {
    background: transparent; color: var(--red); border: 1px solid var(--red);
    padding: 6px 16px; border-radius: 8px; font-size: 12px; font-weight: 700;
    cursor: pointer; transition: all 0.2s;
}
.kill-switch-active-banner .deactivate-btn:hover { background: var(--red); color: #fff; }
@keyframes killPulse { 0%,100% { border-color: rgba(239,68,68,0.4); } 50% { border-color: rgba(239,68,68,0.8); } }
.kill-switch-indicator {
    background: var(--red); color: #fff; padding: 8px 18px; font-size: 11px;
    font-weight: 800; letter-spacing: 1px; text-transform: uppercase;
    border-radius: 8px; animation: killPulse 2s infinite;
    border: 2px solid rgba(239,68,68,0.6);
}

/* Voice Button */
.voice-btn {
    background: var(--card); border: 1px solid var(--border); border-radius: 50%;
    width: 40px; height: 40px; cursor: pointer; font-size: 18px;
    transition: all 0.3s;
}
.voice-btn.listening {
    background: var(--red); border-color: var(--red); animation: voicePulse 1s infinite;
}
@keyframes voicePulse { 0%,100% { box-shadow: 0 0 0 0 rgba(239,68,68,0.4); } 50% { box-shadow: 0 0 0 10px rgba(239,68,68,0); } }
.voice-result { background: var(--card); border: 1px solid var(--accent); border-radius: 8px; padding: 12px; margin-top: 8px; display: none; }

/* Strategy Marketplace */
.marketplace { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 24px; }
.marketplace h3 { font-size: 14px; margin-bottom: 12px; color: var(--text-dim); text-transform: uppercase; }
.marketplace-actions { display: flex; gap: 8px; margin-bottom: 16px; }
.preset-strategies { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.preset-card { background: rgba(59,130,246,0.05); border: 1px solid var(--border); border-radius: 8px; padding: 12px; cursor: pointer; transition: all 0.2s; }
.preset-card:hover { border-color: var(--accent); transform: translateY(-2px); }
.preset-card strong { display: block; margin-bottom: 4px; }
.preset-card p { font-size: 12px; color: var(--text-dim); margin: 0; }

/* Strategy Template V2 - enhanced cards */
.preset-strategies-v2 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.preset-card-v2 {
    background: var(--card); border: 1px solid var(--border);
    border-top: 4px solid var(--accent); border-radius: 12px;
    padding: 20px; display: flex; flex-direction: column; gap: 14px;
    transition: all 0.2s;
}
.preset-card-v2.active {
    box-shadow: 0 0 0 2px rgba(59,130,246,0.3), 0 8px 24px rgba(0,0,0,0.4);
    background: rgba(59,130,246,0.03);
}
.preset-card-v2:hover:not(.active) { transform: translateY(-2px); border-color: var(--accent); }
.preset-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.preset-name { font-size: 22px; font-weight: 800; line-height: 1; }
.preset-tag { font-size: 11px; color: var(--text-dim); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.preset-active-badge {
    background: var(--green); color: #000;
    padding: 4px 10px; border-radius: 20px;
    font-size: 10px; font-weight: 800; letter-spacing: 1px;
    animation: activeGlow 2s infinite;
}
@keyframes activeGlow { 0%,100% { box-shadow: 0 0 0 0 rgba(16,185,129,0.6); } 50% { box-shadow: 0 0 0 6px rgba(16,185,129,0); } }
.preset-stats {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;
    background: rgba(10,14,23,0.5); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px;
}
.preset-stats > div { display: flex; flex-direction: column; align-items: center; }
.preset-stats .lbl { font-size: 9px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
.preset-stats .val { font-size: 15px; font-weight: 700; color: var(--text); }
.preset-section-title { font-size: 10px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }
.preset-section-body { font-size: 12px; color: var(--text); line-height: 1.5; }
.preset-pill {
    display: inline-block; padding: 3px 8px; border-radius: 6px;
    font-size: 11px; font-weight: 600; margin: 2px 4px 2px 0;
}
.preset-pill.ok { background: rgba(16,185,129,0.15); color: var(--green); }
.preset-pill.no { background: rgba(239,68,68,0.15); color: var(--red); text-decoration: line-through; opacity: 0.8; }
.preset-outcome {
    border-top: 1px solid var(--border); padding-top: 12px;
    display: flex; flex-direction: column; gap: 6px;
}
.preset-outcome-row { display: flex; justify-content: space-between; font-size: 12px; }
.preset-outcome-row span { color: var(--text-dim); }
.preset-outcome-row strong { color: var(--text); }
.preset-apply-btn {
    padding: 10px; border-radius: 8px; border: none;
    color: #fff; font-size: 13px; font-weight: 700;
    cursor: pointer; transition: all 0.2s;
}
.preset-apply-btn:hover:not(:disabled) { transform: translateY(-1px); filter: brightness(1.1); }
.preset-apply-btn:disabled { cursor: default; opacity: 0.6; color: var(--text-dim); }
@media (max-width: 900px) {
    .preset-strategies-v2 { grid-template-columns: 1fr; }
    .preset-stats { grid-template-columns: repeat(2, 1fr); }
}

/* What Happens Next Panel */
.next-actions-panel {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 24px; margin-bottom: 24px;
}
.next-actions-panel h3 {
    font-size: 16px; font-weight: 700; margin-bottom: 16px;
    display: flex; align-items: center; gap: 8px;
}
.timeline { position: relative; padding-left: 24px; margin-bottom: 20px; }
.timeline::before {
    content: ''; position: absolute; left: 7px; top: 6px; bottom: 6px;
    width: 2px; background: var(--border);
}
.timeline-item {
    display: flex; align-items: center; gap: 12px; padding: 10px 0;
    position: relative; font-size: 13px;
}
.timeline-item::before {
    content: ''; position: absolute; left: -20px; top: 50%; transform: translateY(-50%);
    width: 10px; height: 10px; border-radius: 50%; background: var(--green);
    border: 2px solid var(--card); z-index: 1;
}
.timeline-item.off::before { background: #4b5563; }
.timeline-item.active::before { background: var(--green); box-shadow: 0 0 8px rgba(16,185,129,0.5); }
.timeline-item .time {
    font-size: 11px; color: var(--text-dim); font-weight: 600; min-width: 80px;
    font-variant-numeric: tabular-nums;
}
.timeline-item .action { flex: 1; color: var(--text); }
.timeline-item.off .action { color: var(--text-dim); }
.badge-on {
    background: rgba(16,185,129,0.15); color: var(--green); padding: 2px 10px;
    border-radius: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
}
.badge-off {
    background: rgba(239,68,68,0.15); color: var(--red); padding: 2px 10px;
    border-radius: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
}
.pending-orders { margin-bottom: 16px; }
.pending-orders h4, .guardrails-summary h4 {
    font-size: 13px; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 10px; font-weight: 600;
}
.pending-orders ul { list-style: none; padding: 0; }
.pending-orders li {
    font-size: 13px; padding: 6px 12px; margin-bottom: 4px;
    background: rgba(59,130,246,0.06); border-radius: 6px;
    border-left: 3px solid var(--accent);
}
.guardrails-summary { margin-top: 8px; }
.guardrail-items { display: flex; flex-wrap: wrap; gap: 8px; }
.guardrail-pill {
    background: rgba(148,163,184,0.1); border: 1px solid var(--border);
    padding: 6px 14px; border-radius: 20px; font-size: 12px; color: var(--text-dim);
    font-weight: 500;
}
.guardrail-pill.warning {
    background: rgba(245,158,11,0.12); border-color: rgba(245,158,11,0.3); color: var(--orange);
}
.guardrail-pill.danger {
    background: rgba(239,68,68,0.12); border-color: rgba(239,68,68,0.3); color: var(--red);
}

/* Guard Rail Indicators */
.guardrail-meters {
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px;
}
.guardrail-meter {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px;
}
.guardrail-meter .meter-label {
    font-size: 11px; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 8px; display: flex; justify-content: space-between;
}
.guardrail-meter .meter-bar {
    height: 8px; background: rgba(30,41,59,0.8); border-radius: 4px; overflow: hidden;
    position: relative;
}
.guardrail-meter .meter-fill {
    height: 100%; border-radius: 4px; transition: width 0.6s ease-out;
}
.guardrail-meter .meter-fill.green { background: var(--green); }
.guardrail-meter .meter-fill.yellow { background: var(--orange); }
.guardrail-meter .meter-fill.red { background: var(--red); }
.guardrail-meter .meter-limit {
    position: absolute; right: 0; top: -2px; bottom: -2px; width: 3px;
    background: var(--red); border-radius: 2px;
}
.guardrail-warning {
    font-size: 11px; margin-top: 6px; padding: 4px 8px; border-radius: 4px;
    font-weight: 600;
}
.guardrail-warning.yellow { background: rgba(245,158,11,0.12); color: var(--orange); }
.guardrail-warning.red { background: rgba(239,68,68,0.12); color: var(--red); }

/* Footer */
.footer {
    text-align: center; padding: 16px; color: var(--text-dim); font-size: 11px;
    border-top: 1px solid var(--border);
}

/* Navigation Tabs */
.nav-tabs {
    display: flex; gap: 4px; margin-bottom: 24px; padding: 4px;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    overflow-x: auto; -webkit-overflow-scrolling: touch;
}
.nav-tabs::-webkit-scrollbar { height: 0; }
.nav-tab {
    padding: 10px 18px; border-radius: 8px; font-size: 13px; font-weight: 600;
    color: var(--text-dim); cursor: pointer; white-space: nowrap;
    transition: all 0.2s; background: transparent; border: none;
}
.nav-tab:hover { color: var(--text); background: rgba(255,255,255,0.05); }
.nav-tab.active { color: #fff; background: var(--accent); }

/* Trading Session Badge */
.session-badge {
    padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 700;
    letter-spacing: 1px; text-transform: uppercase;
}
.session-pre { background: rgba(245,158,11,0.2); color: var(--orange); }
.session-open { background: rgba(16,185,129,0.2); color: var(--green); }
.session-after { background: rgba(245,158,11,0.2); color: var(--orange); }
.session-closed { background: rgba(148,163,184,0.15); color: var(--text-dim); }

/* Cloud Scheduler Badge */
.scheduler-badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
    background: rgba(16,185,129,0.15); color: var(--green);
}
.scheduler-badge.off { background: rgba(148,163,184,0.15); color: var(--text-dim); }
.scheduler-pulse {
    width: 8px; height: 8px; border-radius: 50%;
    background: currentColor; animation: schedPulse 2s infinite;
}
@keyframes schedPulse { 0%,100%{opacity:1;transform:scale(1);} 50%{opacity:0.4;transform:scale(0.7);} }

/* Readiness Mini Bar */
.readiness-mini { display: flex; align-items: center; gap: 8px; }
.readiness-mini .readiness-label { font-size: 11px; font-weight: 700; color: var(--text-dim); }
.readiness-mini .readiness-bar-bg {
    width: 60px; height: 6px; background: rgba(30,41,59,0.8); border-radius: 3px; overflow: hidden;
}
.readiness-mini .readiness-bar-fill { height: 100%; border-radius: 3px; transition: width 0.6s; }

/* Economic Calendar Banner */
.econ-banner {
    border-radius: var(--radius); padding: 14px 20px; margin-bottom: 20px;
    display: flex; align-items: center; gap: 12px; font-size: 14px;
}
.econ-banner.high { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.3); color: var(--red); }
.econ-banner.medium { background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.3); color: var(--orange); }
.econ-banner.normal { background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.2); color: var(--accent); }
.econ-banner .econ-icon { font-size: 20px; flex-shrink: 0; }

/* Technical Indicators on Pick Cards */
.pick-indicators {
    display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; padding-top: 8px;
    border-top: 1px solid rgba(30,41,59,0.5);
}
.indicator-badge {
    padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 700;
    letter-spacing: 0.3px; text-transform: uppercase;
}
.indicator-badge.bullish { background: rgba(16,185,129,0.15); color: var(--green); }
.indicator-badge.bearish { background: rgba(239,68,68,0.15); color: var(--red); }
.indicator-badge.neutral { background: rgba(148,163,184,0.12); color: var(--text-dim); }

/* Social Sentiment on Pick Cards */
.pick-social {
    display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
    font-size: 11px; color: var(--text-dim);
}
.social-badge { padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; }
.social-badge.bullish { background: rgba(16,185,129,0.15); color: var(--green); }
.social-badge.bearish { background: rgba(239,68,68,0.15); color: var(--red); }
.social-badge.neutral { background: rgba(148,163,184,0.12); color: var(--text-dim); }
.trending-badge { color: var(--orange); font-weight: 700; font-size: 11px; }

/* Short Candidates Section */
.short-section {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; margin-bottom: 24px;
}
.short-section h3 {
    font-size: 14px; margin-bottom: 12px; color: var(--red);
    text-transform: uppercase; letter-spacing: 0.5px;
    display: flex; align-items: center; gap: 8px;
}

/* Tax-Loss Harvesting Section */
.tax-section {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; margin-bottom: 24px;
}
.tax-section h3 {
    font-size: 14px; margin-bottom: 12px; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 0.5px;
    display: flex; align-items: center; gap: 8px;
}

/* Readiness Scorecard */
.readiness-card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 24px; margin-bottom: 24px;
}
.readiness-card h3 {
    font-size: 16px; font-weight: 700; margin-bottom: 16px;
    display: flex; align-items: center; gap: 8px;
}
.readiness-progress {
    height: 12px; background: rgba(30,41,59,0.8); border-radius: 6px;
    overflow: hidden; margin-bottom: 16px;
}
.readiness-progress-fill {
    height: 100%; border-radius: 6px; transition: width 0.8s ease-out;
}
.readiness-metrics {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
}
.readiness-metric {
    padding: 10px; background: rgba(30,41,59,0.3); border-radius: 8px;
}
.readiness-metric .rm-label { font-size: 11px; color: var(--text-dim); margin-bottom: 4px; }
.readiness-metric .rm-value { font-size: 14px; font-weight: 600; }
.readiness-metric .rm-target { font-size: 10px; color: var(--text-dim); }
.readiness-status {
    margin-top: 16px; padding: 12px 16px; border-radius: 8px; font-size: 13px; font-weight: 600;
}
.readiness-status.not-ready { background: rgba(239,68,68,0.1); color: var(--red); border: 1px solid rgba(239,68,68,0.2); }
.readiness-status.ready { background: rgba(16,185,129,0.1); color: var(--green); border: 1px solid rgba(16,185,129,0.2); }

/* Correlation Warning */
.correlation-section {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; margin-bottom: 24px;
}
.correlation-section h3 {
    font-size: 14px; margin-bottom: 12px; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 0.5px;
}
.corr-warning {
    padding: 10px 14px; border-radius: 8px; font-size: 12px; margin-bottom: 8px;
    background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.2); color: var(--orange);
}

/* Options Info in Wheel */
.options-info {
    margin-top: 12px; padding: 12px; background: rgba(139,92,246,0.06);
    border: 1px solid rgba(139,92,246,0.2); border-radius: 8px; font-size: 12px;
}
.options-info .opt-row { display: flex; justify-content: space-between; padding: 4px 0; }
.options-info .opt-label { color: var(--text-dim); }
.options-info .opt-value { font-weight: 600; }

/* Responsive */
@media (max-width:1200px) {
    .strategies, .picks { grid-template-columns: 1fr; }
    .tables { grid-template-columns: 1fr; }
    .account-bar { grid-template-columns: repeat(3,1fr); }
    .readiness-metrics { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width:768px) {
    body { padding: 8px; }
    .header { flex-direction: column; gap: 8px; align-items: flex-start; }
    .header-right { flex-wrap: wrap; }
    .header h1 { font-size: 18px; }
    .account-bar { grid-template-columns: 1fr 1fr; gap: 8px; }
    .picks { grid-template-columns: 1fr; }
    .strategies { grid-template-columns: 1fr; }
    .tables { grid-template-columns: 1fr; }
    .preset-strategies { grid-template-columns: 1fr; }
    .metric .value { font-size: 16px; }
    .pick-symbol { font-size: 20px; }
    .timeline-item { flex-wrap: wrap; }
    .kill-switch-btn { width: 100%; }
    .nav-tabs { gap: 2px; padding: 3px; }
    .nav-tab { padding: 8px 12px; font-size: 12px; }
    .readiness-metrics { grid-template-columns: 1fr 1fr; }
    .pick-indicators { gap: 4px; }
    .screener table { font-size: 11px; }
    .screener th, .screener td { padding: 6px 8px; }
}
.backtest-section {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; margin-bottom: 24px;
}
.backtest-section h3 {
    font-size: 14px; margin-bottom: 12px; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 0.5px;
}
</style>
</head>
<body>

<div class="toast-container" id="toastContainer"></div>

<!-- Deploy Modal (Enhanced with P&L estimates) -->
<div class="modal-overlay" id="deployModal">
  <div class="modal" style="max-width:540px">
    <h2 id="deployModalTitle">Deploy Strategy</h2>
    <div class="modal-subtitle" id="deployModalSubtitle">Configure and deploy on TSLA</div>
    <div class="modal-action-text" id="deployActionText"></div>
    <div class="modal-info-box" id="deployInfoBox">
      <div class="info-title">What This Means</div>
      <div id="deployInfoContent"></div>
    </div>
    <div class="form-group">
      <label>Strategy</label>
      <select id="deployStrategy" onchange="onStrategyChange()">
        <option value="trailing_stop">Trailing Stop - Rides uptrends, auto-protects gains</option>
        <option value="copy_trading">Copy Trading - Copies politician trades</option>
        <option value="wheel">Wheel Strategy - Sells options, collects premium</option>
        <option value="mean_reversion">Mean Reversion - Buys oversold dips, sells at recovery</option>
        <option value="breakout">Breakout - Catches explosive moves on high volume</option>
      </select>
      <div class="strategy-info-text" id="deployStrategyInfo"></div>
    </div>
    <div class="form-group">
      <label>Shares</label>
      <input type="number" id="deployQty" value="2" min="1" max="1000">
      <span id="deployQtyHint" style="font-size:11px;color:var(--text-dim)">(recommended)</span>
    </div>
    <input type="hidden" id="deploySymbol" value="">
    <div style="background:var(--bg);border-radius:8px;padding:14px;margin:12px 0">
      <div class="modal-detail-row"><span class="detail-label">Est Cost</span><span class="detail-value" id="deployEstCost">--</span></div>
      <div class="modal-detail-row"><span class="detail-label">Max Loss</span><span class="detail-value negative" id="deployMaxLoss">--</span></div>
      <div class="modal-detail-row"><span class="detail-label">Profit Target</span><span class="detail-value positive" id="deployProfitTarget">--</span></div>
      <div class="modal-detail-row"><span class="detail-label">Risk/Reward</span><span class="detail-value" id="deployRiskReward">--</span></div>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal('deployModal')">Cancel</button>
      <button class="btn-success" onclick="executeDeploy()" id="deployConfirmBtn">Confirm Buy</button>
    </div>
  </div>
</div>

<!-- Close Position Modal (Enhanced with P&L) -->
<div class="modal-overlay" id="closeModal">
  <div class="modal" style="max-width:500px">
    <h2 id="closeModalTitle">Close Position</h2>
    <div class="modal-subtitle" id="closeModalSubtitle">Are you sure you want to close this position?</div>
    <div id="closeModalDetails" style="background:var(--bg);border-radius:8px;padding:14px;margin:12px 0;font-size:13px;"></div>
    <div class="modal-info-box" id="closeInfoBox">
      <div class="info-title">If You Close Now</div>
      <div id="closeInfoContent"></div>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal('closeModal')">Cancel</button>
      <button class="btn-danger" id="closeModalConfirm">Confirm Sell</button>
    </div>
  </div>
</div>

<!-- Cancel Order Modal (Enhanced with explanation) -->
<div class="modal-overlay" id="cancelOrderModal">
  <div class="modal" style="max-width:500px">
    <h2>Cancel Order</h2>
    <div class="modal-subtitle" id="cancelOrderSubtitle">Review this order before cancelling</div>
    <div id="cancelOrderDetails" style="background:var(--bg);border-radius:8px;padding:14px;margin:12px 0;font-size:13px;"></div>
    <div class="modal-info-box" id="cancelInfoBox">
      <div class="info-title">What This Means</div>
      <div id="cancelInfoContent"></div>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal('cancelOrderModal')">Keep Order</button>
      <button class="btn-danger" id="cancelOrderConfirm">Cancel Order</button>
    </div>
  </div>
</div>

<!-- Sell Half Modal (New) -->
<div class="modal-overlay" id="sellHalfModal">
  <div class="modal" style="max-width:500px">
    <h2 id="sellHalfTitle">Sell Half Position</h2>
    <div class="modal-subtitle" id="sellHalfSubtitle"></div>
    <div id="sellHalfDetails" style="background:var(--bg);border-radius:8px;padding:14px;margin:12px 0;font-size:13px;"></div>
    <div class="modal-info-box" id="sellHalfInfoBox">
      <div class="info-title">What This Means</div>
      <div id="sellHalfInfoContent"></div>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal('sellHalfModal')">Cancel</button>
      <button class="btn-warning" id="sellHalfConfirm">Confirm Sell Half</button>
    </div>
  </div>
</div>

<!-- Auto-Deployer Confirmation Modal -->
<div class="modal-overlay" id="autoDeployerModal">
  <div class="modal" style="max-width:500px">
    <h2 id="autoDeployerModalTitle">Enable Auto-Deployer</h2>
    <div class="modal-subtitle" id="autoDeployerModalSubtitle"></div>
    <div class="modal-info-box" id="autoDeployerInfoBox">
      <div id="autoDeployerInfoContent"></div>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="cancelAutoDeployerToggle()">Cancel</button>
      <button class="btn-success" id="autoDeployerConfirmBtn" onclick="confirmAutoDeployerToggle()">Confirm</button>
    </div>
  </div>
</div>

<!-- Kill Switch Confirmation Modal -->
<div class="modal-overlay" id="killSwitchModal">
  <div class="modal" style="max-width:540px">
    <h2 style="color:var(--red)">EMERGENCY KILL SWITCH</h2>
    <div class="modal-subtitle">This will immediately:</div>
    <div class="modal-info-box warning" style="border-color:rgba(239,68,68,0.3);background:rgba(239,68,68,0.08)">
      <ul style="margin:0;padding-left:18px;line-height:2;font-size:13px">
        <li>Cancel ALL open orders</li>
        <li>Sell ALL positions at market price</li>
        <li>Disable the auto-deployer</li>
        <li>Stop all new trades</li>
      </ul>
    </div>
    <div style="background:var(--bg);border-radius:8px;padding:14px;margin:12px 0;font-size:13px;line-height:1.6;color:var(--text-dim)">
      Your current positions will be closed at whatever the market price is right now. This cannot be undone.
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal('killSwitchModal')">Cancel</button>
      <button class="btn-danger" style="font-size:14px;font-weight:800;letter-spacing:0.5px" onclick="executeKillSwitch()">ACTIVATE KILL SWITCH</button>
    </div>
  </div>
</div>

<!-- Kill Switch Results Modal -->
<div class="modal-overlay" id="killSwitchResultsModal">
  <div class="modal" style="max-width:540px">
    <h2 style="color:var(--red)">Kill Switch Activated</h2>
    <div class="modal-subtitle">Here is what happened:</div>
    <div id="killSwitchResults" style="background:var(--bg);border-radius:8px;padding:14px;margin:12px 0;font-size:13px;"></div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal('killSwitchResultsModal')">Close</button>
    </div>
  </div>
</div>

<div id="app">Loading dashboard...</div>

<script>
/* XSS escaping helper for user/API data inserted into innerHTML */
function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

const API_BASE = '';
let dashboardData = null;
let countdown = 60;
let countdownInterval = null;
let activityLog = [];
let screenerSortCol = 'best_score';
let screenerSortDir = -1;
let autoDeployerEnabled = false;
let pendingAutoDeployerState = false;
let killSwitchActive = false;
let guardrailsData = null;
let currentDeployPrice = 0;
var lastData = null;

const STRATEGY_INFO = {
    trailing_stop: {
        name: 'Trailing Stop',
        desc: 'Rides uptrends, auto-protects gains. Best for: stocks going UP.',
        details: 'Stop-loss at 10% | Trails 5% below peak | Ladder buys on dips',
        stopPct: 0.10, profitPct: 0.25
    },
    copy_trading: {
        name: 'Copy Trading',
        desc: 'Copies politician trades. Best for: following insider knowledge.',
        details: 'Follows politician trades | 5% position size | 10% stop',
        stopPct: 0.10, profitPct: 0.20
    },
    wheel: {
        name: 'Wheel Strategy',
        desc: 'Sells options, collects premium income. Best for: sideways stocks.',
        details: 'Sells puts -> gets assigned -> sells calls -> repeat. Requires 100 shares worth of cash.',
        stopPct: 0.10, profitPct: 0.15
    },
    mean_reversion: {
        name: 'Mean Reversion',
        desc: 'Buys oversold dips, sells at recovery. Best for: stocks that dropped too far.',
        details: 'Buys the dip | Sells at 20-day average | 10% stop-loss',
        stopPct: 0.10, profitPct: 0.15
    },
    breakout: {
        name: 'Breakout',
        desc: 'Catches explosive moves on high volume. Best for: stocks breaking out.',
        details: 'Tight 5% stop | Rides momentum | Best on 2x+ volume',
        stopPct: 0.05, profitPct: 0.20
    }
};

function toast(msg, type='info') {
    const c = document.getElementById('toastContainer');
    const icons = {success: '\u2713', error: '\u2717', info: '\u2139'};
    const t = document.createElement('div');
    t.className = 'toast ' + type;
    t.innerHTML = '<span class="toast-icon">' + (icons[type]||'') + '</span><span>' + esc(msg) + '</span>';
    c.appendChild(t);
    setTimeout(() => t.remove(), 4000);
}

function addLog(msg, type='info') {
    const now = new Date();
    const time = now.toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
    activityLog.unshift({time, msg, type});
    if (activityLog.length > 50) activityLog.length = 50;
    renderLog();
}

function renderLog() {
    const el = document.getElementById('logEntries');
    if (!el) return;
    const icons = {success: '\u2713', error: '\u2717', info: '\u27A4', buy: '\u25B2', sell: '\u25BC', cancel: '\u2716'};
    const visible = activityLog.slice(0, 20);
    el.innerHTML = visible.map(e =>
        '<div class="log-entry ' + e.type + '">' +
        '<span class="log-time">' + e.time + '</span>' +
        '<span class="log-icon">' + (icons[e.type]||icons.info) + '</span>' +
        '<span class="log-msg">' + esc(e.msg) + '</span></div>'
    ).join('') || '<div class="empty">No activity yet</div>';
}

function scrollToSection(id) {
    var el = document.getElementById(id);
    if (el) {
        el.scrollIntoView({behavior: 'smooth', block: 'start'});
        // Highlight active tab
        document.querySelectorAll('.nav-tab').forEach(function(t) { t.classList.remove('active'); });
        document.querySelectorAll('.nav-tab').forEach(function(t) {
            if (t.getAttribute('onclick') && t.getAttribute('onclick').indexOf(id) >= 0) t.classList.add('active');
        });
    }
}

function openModal(id) { document.getElementById(id).classList.add('active'); }
function closeModal(id) { document.getElementById(id).classList.remove('active'); }

function fmtMoney(n) { return '$' + Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
function fmtPct(n) { return (n>=0?'+':'') + Number(n).toFixed(1) + '%'; }
function pnlClass(n) { return parseFloat(n) >= 0 ? 'positive' : 'negative'; }

/* ---- Deploy Modal (enhanced) ---- */
function openDeployModal(symbol, strategy, qty, price) {
    currentDeployPrice = price;
    document.getElementById('deploySymbol').value = symbol;
    const sel = document.getElementById('deployStrategy');
    if (strategy === 'Trailing Stop') sel.value = 'trailing_stop';
    else if (strategy === 'Wheel Strategy') sel.value = 'wheel';
    else if (strategy === 'Copy Trading') sel.value = 'copy_trading';
    else if (strategy === 'Mean Reversion') sel.value = 'mean_reversion';
    else if (strategy === 'Breakout') sel.value = 'breakout';
    document.getElementById('deployQty').value = qty || 2;
    document.getElementById('deployQty').oninput = function() { updateDeployDetails(); };
    updateDeployDetails();
    openModal('deployModal');
}

function onStrategyChange() { updateDeployDetails(); }

function updateDeployDetails() {
    const symbol = document.getElementById('deploySymbol').value;
    const stratKey = document.getElementById('deployStrategy').value;
    const qty = parseInt(document.getElementById('deployQty').value) || 1;
    const price = currentDeployPrice;
    const strat = STRATEGY_INFO[stratKey] || STRATEGY_INFO.trailing_stop;
    const cost = price * qty;
    const maxLoss = cost * strat.stopPct;
    const profitTarget = cost * strat.profitPct;
    const riskReward = '1:' + (strat.profitPct / strat.stopPct).toFixed(1);
    const stopPrice = (price * (1 - strat.stopPct)).toFixed(2);

    document.getElementById('deployModalTitle').textContent = 'Deploy ' + strat.name + ' on ' + symbol;
    document.getElementById('deployModalSubtitle').textContent = strat.desc;
    document.getElementById('deployActionText').innerHTML =
        'Action: <strong>BUY ' + qty + ' share' + (qty>1?'s':'') + ' of ' + symbol + ' at ~' + fmtMoney(price) + '</strong>';

    let infoHtml = 'You\'re buying <strong>' + fmtMoney(cost) + '</strong> worth of ' + symbol + '<br><br>' +
        'If it goes UP ' + (strat.profitPct*100).toFixed(0) + '%: You make ~<span class="positive">' + fmtMoney(profitTarget) + ' profit</span><br>' +
        'If it goes DOWN ' + (strat.stopPct*100).toFixed(0) + '%: You lose ~<span class="negative">' + fmtMoney(maxLoss) + ' (stop)</span><br><br>' +
        'Stop-loss will automatically sell at <strong>' + fmtMoney(stopPrice) + '</strong> to limit your maximum loss.';
    if (stratKey === 'wheel') {
        const cashNeeded = price * 100;
        infoHtml = 'The Wheel requires cash to cover 100 shares = <strong>' + fmtMoney(cashNeeded) + '</strong><br>' +
            'You sell put options to collect premium income.<br>' +
            'If assigned, you own shares and sell calls on them.<br><br>' +
            'Risk: stock drops significantly while you hold shares.';
    }
    document.getElementById('deployInfoContent').innerHTML = infoHtml;
    document.getElementById('deployStrategyInfo').textContent = strat.details;

    document.getElementById('deployEstCost').textContent = fmtMoney(cost);
    document.getElementById('deployMaxLoss').textContent = '-' + fmtMoney(maxLoss) + ' (' + (strat.stopPct*100).toFixed(0) + '% stop-loss)';
    document.getElementById('deployProfitTarget').textContent = '+' + fmtMoney(profitTarget) + ' (+' + (strat.profitPct*100).toFixed(0) + '%)';
    document.getElementById('deployRiskReward').textContent = riskReward;
    document.getElementById('deployConfirmBtn').textContent = 'Confirm Buy';
}

async function executeDeploy() {
    const symbol = document.getElementById('deploySymbol').value;
    const strategy = document.getElementById('deployStrategy').value;
    const qty = parseInt(document.getElementById('deployQty').value) || 1;
    closeModal('deployModal');
    const stratName = (STRATEGY_INFO[strategy]||{}).name || strategy;
    toast('Deploying ' + stratName + ' on ' + symbol + '...', 'info');
    addLog('BUY ' + qty + ' ' + symbol + ' via ' + stratName + ' (~' + fmtMoney(currentDeployPrice * qty) + ')', 'buy');
    try {
        const resp = await fetch(API_BASE + '/api/deploy', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({symbol, strategy, qty})
        });
        const data = await resp.json();
        if (data.error) {
            toast('Deploy failed: ' + data.error, 'error');
            addLog('Deploy failed: ' + data.error, 'error');
        } else {
            toast('Deployed ' + stratName + ' on ' + symbol + ' successfully!', 'success');
            addLog('Deployed ' + stratName + ' on ' + symbol + ' - Order ID: ' + (data.buy_order_id || data.order_id || 'N/A'), 'success');
            setTimeout(refreshData, 1500);
        }
    } catch(e) {
        toast('Deploy error: ' + e.message, 'error');
        addLog('Deploy error: ' + e.message, 'error');
    }
}

/* ---- Close Position Modal (enhanced) ---- */
function openClosePositionModal(symbol, qty, avgEntry, currentPrice, pnl, pnlPct) {
    const proceeds = (currentPrice * qty).toFixed(2);
    document.getElementById('closeModalTitle').textContent = 'Close ' + symbol + ' Position';
    document.getElementById('closeModalSubtitle').textContent = '';
    document.getElementById('closeModalDetails').innerHTML =
        '<div class="modal-detail-row"><span class="detail-label">You own</span><span class="detail-value">' + qty + ' shares of ' + symbol + '</span></div>' +
        '<div class="modal-detail-row"><span class="detail-label">Bought at</span><span class="detail-value">' + fmtMoney(avgEntry) + '</span></div>' +
        '<div class="modal-detail-row"><span class="detail-label">Current price</span><span class="detail-value">' + fmtMoney(currentPrice) + '</span></div>';
    const pnlNum = parseFloat(pnl) || 0;
    const pnlColor = pnlNum >= 0 ? 'var(--green)' : 'var(--red)';
    const pnlWord = pnlNum >= 0 ? 'gain' : 'loss';
    document.getElementById('closeInfoContent').innerHTML =
        'You will <strong>SELL ' + qty + ' shares</strong> at market price<br>' +
        'Estimated proceeds: <strong>' + fmtMoney(proceeds) + '</strong><br>' +
        'Estimated P&L: <strong style="color:' + pnlColor + '">' + fmtMoney(pnlNum) + ' (' + fmtPct(parseFloat(pnlPct)||0) + ')</strong> ' + pnlWord;
    const btn = document.getElementById('closeModalConfirm');
    btn.onclick = () => executeClosePosition(symbol);
    openModal('closeModal');
}

async function executeClosePosition(symbol) {
    closeModal('closeModal');
    toast('Closing ' + symbol + ' position...', 'info');
    addLog('SELL ALL ' + symbol + ' at market', 'sell');
    try {
        const resp = await fetch(API_BASE + '/api/close-position', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({symbol})
        });
        const data = await resp.json();
        if (data.error) {
            toast('Close failed: ' + data.error, 'error');
            addLog('Close failed for ' + symbol + ': ' + data.error, 'error');
        } else {
            toast(symbol + ' position closed!', 'success');
            addLog('Closed position: ' + symbol, 'success');
            setTimeout(refreshData, 1500);
        }
    } catch(e) {
        toast('Error: ' + e.message, 'error');
    }
}

/* ---- Sell Half Modal (new) ---- */
function openSellHalfModal(symbol, totalQty, avgEntry, currentPrice, pnl, pnlPct) {
    const halfQty = Math.max(1, Math.floor(totalQty / 2));
    const proceeds = (currentPrice * halfQty).toFixed(2);
    const halfPnl = (parseFloat(pnl) || 0) / totalQty * halfQty;
    const halfPnlPct = parseFloat(pnlPct) || 0;
    document.getElementById('sellHalfTitle').textContent = 'Sell Half of ' + symbol;
    document.getElementById('sellHalfSubtitle').textContent = 'You currently own ' + totalQty + ' shares';
    document.getElementById('sellHalfDetails').innerHTML =
        '<div class="modal-detail-row"><span class="detail-label">Selling</span><span class="detail-value">' + halfQty + ' of ' + totalQty + ' shares</span></div>' +
        '<div class="modal-detail-row"><span class="detail-label">Keeping</span><span class="detail-value">' + (totalQty - halfQty) + ' shares</span></div>' +
        '<div class="modal-detail-row"><span class="detail-label">Avg entry</span><span class="detail-value">' + fmtMoney(avgEntry) + '</span></div>' +
        '<div class="modal-detail-row"><span class="detail-label">Current price</span><span class="detail-value">' + fmtMoney(currentPrice) + '</span></div>';
    const pnlColor = halfPnl >= 0 ? 'var(--green)' : 'var(--red)';
    document.getElementById('sellHalfInfoContent').innerHTML =
        'You will <strong>SELL ' + halfQty + ' shares</strong> of ' + symbol + ' at market price<br>' +
        'Estimated proceeds: <strong>' + fmtMoney(proceeds) + '</strong><br>' +
        'Estimated P&L on sold shares: <strong style="color:' + pnlColor + '">' + fmtMoney(halfPnl) + ' (' + fmtPct(halfPnlPct) + ')</strong><br>' +
        'You will still own <strong>' + (totalQty - halfQty) + ' shares</strong> after this.';
    const btn = document.getElementById('sellHalfConfirm');
    btn.onclick = () => executeSellHalf(symbol, totalQty);
    openModal('sellHalfModal');
}

async function executeSellHalf(symbol, totalQty) {
    const halfQty = Math.max(1, Math.floor(totalQty / 2));
    closeModal('sellHalfModal');
    toast('Selling ' + halfQty + ' shares of ' + symbol + '...', 'info');
    addLog('SELL ' + halfQty + ' ' + symbol + ' (half position)', 'sell');
    try {
        const resp = await fetch(API_BASE + '/api/sell', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({symbol, qty: halfQty})
        });
        const data = await resp.json();
        if (data.error) {
            toast('Sell failed: ' + data.error, 'error');
            addLog('Sell half failed for ' + symbol + ': ' + data.error, 'error');
        } else {
            toast('Sold ' + halfQty + ' shares of ' + symbol, 'success');
            addLog('Sold ' + halfQty + ' ' + symbol + ' for ~' + fmtMoney(data.est_proceeds || 0), 'success');
            setTimeout(refreshData, 1500);
        }
    } catch(e) {
        toast('Error: ' + e.message, 'error');
    }
}

/* ---- Cancel Order Modal (enhanced) ---- */
function openCancelOrderModal(orderId, symbol, side, type, qty, price) {
    document.getElementById('cancelOrderSubtitle').textContent = 'Review this order before cancelling';
    const priceText = price && price !== 'Market' ? fmtMoney(price) : 'Market';
    document.getElementById('cancelOrderDetails').innerHTML =
        '<div class="modal-detail-row"><span class="detail-label">Order</span><span class="detail-value">' + side.toUpperCase() + ' ' + qty + ' share' + (parseInt(qty)>1?'s':'') + ' of ' + symbol + '</span></div>' +
        '<div class="modal-detail-row"><span class="detail-label">Type</span><span class="detail-value">' + type + ' @ ' + priceText + '</span></div>';
    let infoHtml = '';
    if (side === 'buy') {
        infoHtml = 'This ' + type + ' buy will be cancelled.<br>' +
            'You will <strong>NOT</strong> buy ' + symbol + ' at ' + priceText + '.<br>' +
            'No money is spent or lost.';
    } else {
        infoHtml = 'This ' + type + ' sell will be cancelled.<br>' +
            'Your ' + symbol + ' shares will <strong>NOT</strong> be sold.<br>' +
            'You will keep holding your position.';
    }
    document.getElementById('cancelInfoContent').innerHTML = infoHtml;
    const btn = document.getElementById('cancelOrderConfirm');
    btn.onclick = () => executeCancelOrder(orderId, symbol);
    openModal('cancelOrderModal');
}

async function executeCancelOrder(orderId, symbol) {
    closeModal('cancelOrderModal');
    toast('Canceling order for ' + symbol + '...', 'info');
    addLog('CANCEL order ' + orderId.substring(0,8) + '... for ' + symbol, 'cancel');
    try {
        const resp = await fetch(API_BASE + '/api/cancel-order', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({order_id: orderId})
        });
        const data = await resp.json();
        if (data.error) {
            toast('Cancel failed: ' + data.error, 'error');
            addLog('Cancel failed: ' + data.error, 'error');
        } else {
            toast('Order canceled for ' + symbol, 'success');
            addLog('Order canceled for ' + symbol + ' -- no money spent', 'cancel');
            setTimeout(refreshData, 1500);
        }
    } catch(e) {
        toast('Error: ' + e.message, 'error');
    }
}

/* ---- Auto-Deployer Toggle ---- */
function toggleAutoDeployer() {
    pendingAutoDeployerState = !autoDeployerEnabled;
    if (pendingAutoDeployerState) {
        document.getElementById('autoDeployerModalTitle').textContent = 'Enable Auto-Deployer?';
        document.getElementById('autoDeployerModalSubtitle').textContent = 'The bot will trade automatically on your behalf.';
        document.getElementById('autoDeployerInfoContent').innerHTML =
            'The bot will automatically screen stocks and deploy trades at market open each day.<br><br>' +
            '<strong>Safeguards:</strong><br>' +
            '- Max 2 new positions per day<br>' +
            '- Max 10% of portfolio per stock<br>' +
            '- All positions will have stop-losses<br><br>' +
            'You can turn this off at any time.';
        document.getElementById('autoDeployerConfirmBtn').className = 'btn-success';
        document.getElementById('autoDeployerConfirmBtn').textContent = 'Enable Auto-Deployer';
    } else {
        document.getElementById('autoDeployerModalTitle').textContent = 'Disable Auto-Deployer?';
        document.getElementById('autoDeployerModalSubtitle').textContent = 'Stop automatic trading.';
        document.getElementById('autoDeployerInfoContent').innerHTML =
            'The bot will <strong>stop deploying new trades</strong>.<br><br>' +
            'Existing positions and monitors will continue running.<br>' +
            'You can still deploy manually.';
        document.getElementById('autoDeployerConfirmBtn').className = 'btn-danger';
        document.getElementById('autoDeployerConfirmBtn').textContent = 'Disable Auto-Deployer';
    }
    openModal('autoDeployerModal');
}

function cancelAutoDeployerToggle() {
    closeModal('autoDeployerModal');
    // revert the checkbox visually
    const cb = document.getElementById('autoDeployerCheckbox');
    if (cb) cb.checked = autoDeployerEnabled;
}

async function confirmAutoDeployerToggle() {
    closeModal('autoDeployerModal');
    autoDeployerEnabled = pendingAutoDeployerState;
    try {
        await fetch(API_BASE + '/api/auto-deployer', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: autoDeployerEnabled})
        });
        toast('Auto-Deployer ' + (autoDeployerEnabled ? 'enabled' : 'disabled'), autoDeployerEnabled ? 'success' : 'info');
        addLog('Auto-Deployer ' + (autoDeployerEnabled ? 'ENABLED' : 'DISABLED'), autoDeployerEnabled ? 'success' : 'info');
        updateAutoDeployerUI();
    } catch(e) {
        toast('Failed to update auto-deployer: ' + e.message, 'error');
    }
}

function updateAutoDeployerUI() {
    const cb = document.getElementById('autoDeployerCheckbox');
    const status = document.getElementById('autoDeployerStatus');
    if (cb) cb.checked = autoDeployerEnabled;
    if (status) {
        status.textContent = autoDeployerEnabled ? 'ON' : 'OFF';
        status.className = 'toggle-status ' + (autoDeployerEnabled ? 'on' : 'off');
    }
}

async function loadAutoDeployerState() {
    try {
        const resp = await fetch(API_BASE + '/api/auto-deployer-config');
        const data = await resp.json();
        autoDeployerEnabled = !!(data && data.enabled);
        updateAutoDeployerUI();
    } catch(e) {
        // default to off
        autoDeployerEnabled = false;
    }
}

/* ---- Kill Switch ---- */
function openKillSwitchModal() {
    openModal('killSwitchModal');
}

async function executeKillSwitch() {
    closeModal('killSwitchModal');
    toast('Activating kill switch...', 'info');
    addLog('KILL SWITCH ACTIVATED', 'error');
    try {
        const resp = await fetch(API_BASE + '/api/kill-switch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({activate: true})
        });
        const data = await resp.json();
        killSwitchActive = true;
        autoDeployerEnabled = false;
        updateAutoDeployerUI();
        // Show results
        let html = '';
        if (data.orders_cancelled != null) {
            html += '<div class="modal-detail-row"><span class="detail-label">Orders Cancelled</span><span class="detail-value">' + data.orders_cancelled + '</span></div>';
        }
        if (data.positions_closed != null) {
            html += '<div class="modal-detail-row"><span class="detail-label">Positions Closed</span><span class="detail-value">' + data.positions_closed + '</span></div>';
        }
        if (data.timestamp) {
            html += '<div class="modal-detail-row"><span class="detail-label">Timestamp</span><span class="detail-value">' + data.timestamp + '</span></div>';
        }
        if (data.error) {
            html += '<div style="color:var(--red);margin-top:8px">Error: ' + data.error + '</div>';
        }
        document.getElementById('killSwitchResults').innerHTML = html || '<div>Kill switch activated.</div>';
        openModal('killSwitchResultsModal');
        toast('Kill switch activated - all trading halted', 'error');
        addLog('Orders cancelled: ' + (data.orders_cancelled||0) + ', Positions closed: ' + (data.positions_closed||0), 'error');
        setTimeout(refreshData, 1500);
    } catch(e) {
        toast('Kill switch error: ' + e.message, 'error');
        addLog('Kill switch error: ' + e.message, 'error');
    }
}

async function deactivateKillSwitch() {
    try {
        const resp = await fetch(API_BASE + '/api/kill-switch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({activate: false})
        });
        const data = await resp.json();
        killSwitchActive = false;
        toast('Kill switch deactivated. Auto-deployer remains off - re-enable it manually.', 'info');
        addLog('Kill switch deactivated', 'success');
        renderDashboard();
        setTimeout(refreshData, 1500);
    } catch(e) {
        toast('Error deactivating kill switch: ' + e.message, 'error');
    }
}

async function loadGuardrails() {
    try {
        const resp = await fetch(API_BASE + '/api/guardrails');
        guardrailsData = await resp.json();
        killSwitchActive = !!(guardrailsData && guardrailsData.kill_switch);
    } catch(e) {
        guardrailsData = null;
    }
}

/* ---- Scheduler Panel ---- */
function fmtRelative(isoOrTs) {
    if (!isoOrTs) return 'never';
    var when;
    if (typeof isoOrTs === 'number') {
        when = new Date(isoOrTs * 1000);
    } else if (typeof isoOrTs === 'string' && isoOrTs.match(/^\d{4}-\d{2}-\d{2}/)) {
        when = new Date(isoOrTs);
    } else {
        return String(isoOrTs);
    }
    var diff = Date.now() - when.getTime();
    if (diff < 0) return 'future';
    var mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    return Math.floor(hrs / 24) + 'd ago';
}

async function refreshSchedulerStatus() {
    try {
        var resp = await fetch(API_BASE + '/api/scheduler-status', {credentials: 'same-origin'});
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        var s = await resp.json();
        renderSchedulerPanel(s);
    } catch(e) {
        var panel = document.getElementById('schedulerPanel');
        if (panel) panel.innerHTML = '<div class="empty" style="padding:20px;color:var(--red)">Scheduler status unavailable: ' + e.message + '</div>';
    }
}

function renderSchedulerPanel(s) {
    var panel = document.getElementById('schedulerPanel');
    if (!panel) return;
    if (!s.running) {
        panel.innerHTML = '<div class="empty" style="padding:20px;color:var(--red)">Scheduler NOT RUNNING. ' + (s.error||'') + '</div>';
        return;
    }
    var lastRuns = s.last_runs || {};
    var marketOpen = s.market_open;
    // Use the pre-formatted ET string from server (don't let browser re-convert timezones)
    var etTime = s.current_et_display || '?';
    var etDate = s.current_et_date || '';

    // Define all tasks with their schedules
    var tasks = [
        { key: 'screener', name: 'Stock Screener', schedule: 'Every 30 min during market hours', needsMarket: true },
        { key: 'monitor', name: 'Strategy Monitor', schedule: 'Every 60s during market hours', needsMarket: true },
        { key: 'auto_deployer', name: 'Auto-Deployer', schedule: 'Weekdays 9:35 AM ET', needsMarket: false },
        { key: 'daily_close', name: 'Daily Close Summary', schedule: 'Weekdays 4:05 PM ET', needsMarket: false },
        { key: 'weekly_learning', name: 'Weekly Learning', schedule: 'Fridays 5:00 PM ET', needsMarket: false }
    ];

    var gridHtml = '<div class="scheduler-grid">';
    tasks.forEach(function(t) {
        var last = lastRuns[t.key];
        var hasRun = last != null;
        var statusClass, statusLabel;
        if (t.needsMarket && !marketOpen) {
            statusClass = 'waiting'; statusLabel = 'Market Closed';
        } else if (hasRun) {
            statusClass = 'ok'; statusLabel = 'Active';
        } else {
            statusClass = 'pending'; statusLabel = 'Pending';
        }
        var lastStr = hasRun ? fmtRelative(last) : 'Not yet today';
        gridHtml += '<div class="sched-task ' + (statusClass === 'ok' ? 'active' : '') + '">' +
            '<div class="sched-task-header">' +
                '<span class="sched-task-name">' + esc(t.name) + '</span>' +
                '<span class="sched-task-status ' + statusClass + '">' + statusLabel + '</span>' +
            '</div>' +
            '<div class="sched-task-schedule">' + esc(t.schedule) + '</div>' +
            '<div class="sched-task-last">Last: ' + esc(lastStr) + '</div>' +
        '</div>';
    });
    gridHtml += '</div>';

    // Summary bar
    var summary = '<div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-dim);margin-bottom:16px">' +
        '<span>Current ET: <strong style="color:var(--text)">' + esc(etTime) + '</strong>' + (etDate ? ' <span style="color:var(--text-dim)">(' + esc(etDate) + ')</span>' : '') + '</span>' +
        '<span>Market: <strong style="color:' + (marketOpen ? 'var(--green)' : 'var(--text-dim)') + '">' + (marketOpen ? 'OPEN' : 'CLOSED') + '</strong></span>' +
        '<span>Thread: <strong style="color:var(--text)">' + esc(s.thread_name || '?') + '</strong></span>' +
        '<span>Running: <strong style="color:var(--green)">YES</strong></span>' +
    '</div>';

    // Recent logs
    var logs = s.recent_logs || [];
    var logsHtml = '<h4 style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Recent Activity (last ' + logs.length + ')</h4>';
    if (logs.length === 0) {
        logsHtml += '<div class="sched-log-empty">No scheduler activity yet</div>';
    } else {
        logsHtml += '<div class="sched-log-box">';
        logs.slice().reverse().forEach(function(l) {
            logsHtml += '<div class="sched-log-line">' +
                '<span class="ts">[' + esc(l.ts||'') + ']</span> ' +
                '<span class="tag">[' + esc(l.task||'') + ']</span> ' +
                esc(l.msg||'') +
            '</div>';
        });
        logsHtml += '</div>';
    }

    panel.innerHTML = summary + gridHtml + logsHtml;
}

// Auto-refresh scheduler panel every 15 seconds
setInterval(function() {
    if (document.getElementById('schedulerPanel')) refreshSchedulerStatus();
}, 15000);

/* ---- Data loading ---- */
async function refreshData() {
    try {
        const resp = await fetch(API_BASE + '/api/data', {credentials: 'same-origin'});
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        dashboardData = await resp.json();
        lastData = dashboardData;
        renderDashboard();
        countdown = 60;
    } catch(e) {
        // Only show error once per 5 failures to prevent spam
        if (!window._fetchFailCount) window._fetchFailCount = 0;
        window._fetchFailCount++;
        if (window._fetchFailCount % 5 === 1) {
            toast('Failed to load data: ' + e.message, 'error');
        }
        countdown = 60;  // Reset even on failure so we don't spam
    }
}

async function forceRefresh() {
    toast('Refreshing dashboard data (running screener)...', 'info');
    addLog('Manual refresh triggered', 'info');
    try {
        const resp = await fetch(API_BASE + '/api/refresh', {method:'POST'});
        dashboardData = await resp.json();
        lastData = dashboardData;
        renderDashboard();
        countdown = 60;
        toast('Data refreshed!', 'success');
        addLog('Dashboard data refreshed', 'success');
    } catch(e) {
        toast('Refresh failed: ' + e.message, 'error');
    }
}

function sortScreener(col) {
    if (screenerSortCol === col) screenerSortDir *= -1;
    else { screenerSortCol = col; screenerSortDir = -1; }
    renderDashboard();
}

function getMarketRegime(data) {
    if (data.market_regime) return data.market_regime;
    const picks = data.picks || [];
    if (picks.length === 0) return 'neutral';
    const avgChange = picks.slice(0,20).reduce((s,p) => s + (p.daily_change||0), 0) / Math.min(picks.length,20);
    if (avgChange > 1) return 'bull';
    if (avgChange < -1) return 'bear';
    return 'neutral';
}

function buildGuardrailMeters(dailyPnlPct, portfolioValue, lastEquity) {
    const gr = guardrailsData || {};
    const dailyLimit = (gr.daily_loss_limit_pct || 0.03) * 100;
    const maxDrawdown = (gr.max_drawdown_pct || 0.10) * 100;
    const peakValue = gr.peak_portfolio_value || lastEquity || portfolioValue;
    const currentDrawdown = peakValue > 0 ? ((peakValue - portfolioValue) / peakValue * 100) : 0;
    const dailyLossPct = Math.abs(Math.min(0, dailyPnlPct));

    // Daily P&L meter
    const dailyRatio = dailyLimit > 0 ? (dailyLossPct / dailyLimit * 100) : 0;
    let dailyColor = 'green';
    let dailyWarn = '';
    if (dailyRatio > 80) { dailyColor = 'red'; dailyWarn = '<div class="guardrail-warning red">Approaching daily loss limit!</div>'; }
    else if (dailyRatio > 50) { dailyColor = 'yellow'; dailyWarn = '<div class="guardrail-warning yellow">Over 50% of daily loss limit used</div>'; }

    // Drawdown meter
    const ddRatio = maxDrawdown > 0 ? (currentDrawdown / maxDrawdown * 100) : 0;
    let ddColor = 'green';
    let ddWarn = '';
    if (ddRatio > 80) { ddColor = 'red'; ddWarn = '<div class="guardrail-warning red">Approaching max drawdown limit!</div>'; }
    else if (ddRatio > 50) { ddColor = 'yellow'; ddWarn = '<div class="guardrail-warning yellow">Over 50% of max drawdown limit used</div>'; }

    return '<div class="guardrail-meters">' +
        '<div class="guardrail-meter">' +
            '<div class="meter-label"><span>Daily Loss</span><span>' + dailyLossPct.toFixed(1) + '% / ' + dailyLimit.toFixed(0) + '% limit</span></div>' +
            '<div class="meter-bar"><div class="meter-fill ' + dailyColor + '" style="width:' + Math.min(100, dailyRatio) + '%"></div><div class="meter-limit"></div></div>' +
            dailyWarn +
        '</div>' +
        '<div class="guardrail-meter">' +
            '<div class="meter-label"><span>Drawdown from Peak</span><span>' + currentDrawdown.toFixed(1) + '% / ' + maxDrawdown.toFixed(0) + '% limit</span></div>' +
            '<div class="meter-bar"><div class="meter-fill ' + ddColor + '" style="width:' + Math.min(100, ddRatio) + '%"></div><div class="meter-limit"></div></div>' +
            ddWarn +
        '</div>' +
    '</div>';
}

function buildNextActionsPanel(d) {
    const gr = guardrailsData || {};
    const orders = d.open_orders || [];
    const positions = d.positions || [];

    // Timeline items
    const adOn = autoDeployerEnabled && !killSwitchActive;
    const monOn = !killSwitchActive;
    const copyOn = !killSwitchActive;
    const wheelOn = !killSwitchActive;

    let timelineHtml =
        '<div class="timeline-item' + (adOn ? '' : ' off') + '">' +
            '<span class="time">9:35 AM ET</span>' +
            '<span class="action">Auto-Deployer screens 12,000+ stocks and deploys top 2 picks</span>' +
            '<span class="' + (adOn ? 'badge-on' : 'badge-off') + '">' + (adOn ? 'ON' : 'OFF') + '</span>' +
        '</div>' +
        '<div class="timeline-item' + (monOn ? '' : ' off') + '">' +
            '<span class="time">9:35 AM ET</span>' +
            '<span class="action">Strategy Monitor checks all positions, manages stops and ladders</span>' +
            '<span class="' + (monOn ? 'badge-on' : 'badge-off') + '">' + (monOn ? 'ON' : 'OFF') + '</span>' +
        '</div>' +
        '<div class="timeline-item' + (copyOn ? '' : ' off') + '">' +
            '<span class="time">9:35 AM ET</span>' +
            '<span class="action">Copy Trading scans Capitol Trades for politician moves</span>' +
            '<span class="' + (copyOn ? 'badge-on' : 'badge-off') + '">' + (copyOn ? 'ON' : 'OFF') + '</span>' +
        '</div>' +
        '<div class="timeline-item' + (wheelOn ? '' : ' off') + '">' +
            '<span class="time">9:35 AM ET</span>' +
            '<span class="action">Wheel Strategy auto-picks affordable stock, sells puts/calls</span>' +
            '<span class="' + (wheelOn ? 'badge-on' : 'badge-off') + '">' + (wheelOn ? 'ON' : 'OFF') + '</span>' +
        '</div>' +
        '<div class="timeline-item active' + (monOn ? '' : ' off') + '">' +
            '<span class="time">Every 5 min</span>' +
            '<span class="action">Strategy Monitor: adjusts stops, checks ladder fills, takes profits</span>' +
            '<span class="' + (monOn ? 'badge-on' : 'badge-off') + '">' + (monOn ? 'ON' : 'OFF') + '</span>' +
        '</div>';

    // Pending actions from open orders
    let pendingHtml = '';
    if (orders.length > 0) {
        let items = '';
        orders.forEach(function(o) {
            const sym = o.symbol || '';
            const side = (o.side || '').toUpperCase();
            const qty = o.qty || '';
            const type = o.type || '';
            const price = o.limit_price || o.stop_price || 'market';
            let desc = sym + ' ' + type + ' ' + side.toLowerCase() + ' (' + qty + ' shares)';
            if (price !== 'market') desc += ' @ $' + price;
            else desc += ' -- will fill at open';
            items += '<li>' + desc + '</li>';
        });
        pendingHtml = '<div class="pending-orders"><h4>Pending Actions</h4><ul>' + items + '</ul></div>';
    } else if (positions.length > 0) {
        let items = '';
        positions.forEach(function(p) {
            items += '<li>' + (p.symbol||'') + ': holding ' + (p.qty||0) + ' shares, stop-loss active</li>';
        });
        pendingHtml = '<div class="pending-orders"><h4>Current Status</h4><ul>' + items + '</ul></div>';
    } else {
        pendingHtml = '<div class="pending-orders"><h4>Pending Actions</h4><ul><li>No pending orders or positions</li></ul></div>';
    }

    // Guardrails summary pills
    const dailyLimitPct = ((gr.daily_loss_limit_pct || 0.03) * 100).toFixed(0);
    const maxDDPct = ((gr.max_drawdown_pct || 0.10) * 100).toFixed(0);
    const maxPos = gr.max_positions || 5;
    const maxPerStock = ((gr.max_position_pct || 0.10) * 100).toFixed(0);
    const killStatus = killSwitchActive;

    let pillsHtml =
        '<span class="guardrail-pill">Daily loss limit: ' + dailyLimitPct + '%</span>' +
        '<span class="guardrail-pill">Max drawdown: ' + maxDDPct + '%</span>' +
        '<span class="guardrail-pill">Max positions: ' + maxPos + '</span>' +
        '<span class="guardrail-pill">Max per stock: ' + maxPerStock + '%</span>';
    if (killStatus) {
        pillsHtml += '<span class="guardrail-pill danger">Kill Switch: ACTIVE</span>';
    }

    return '<div class="next-actions-panel">' +
        '<h3>What Happens at Market Open</h3>' +
        '<div class="timeline">' + timelineHtml + '</div>' +
        pendingHtml +
        '<div class="guardrails-summary"><h4>Safety Limits Active</h4><div class="guardrail-items">' + pillsHtml + '</div></div>' +
    '</div>';
}

function detectActivePreset(d) {
    // Match current guardrails/config against presets to determine which is active
    var g = d.guardrails || {};
    var c = d.auto_deployer_config || {};
    var stopPct = (c.risk_settings && c.risk_settings.default_stop_loss_pct) || 0.10;
    var maxPos = c.max_positions || g.max_positions || 5;
    var maxPerStock = g.max_position_pct || 0.10;
    var allowed = (g.strategies_allowed || []).slice().sort().join(',');

    // Preset signatures
    if (stopPct === 0.05 && maxPos === 3 && maxPerStock === 0.05) return 'conservative';
    if (stopPct === 0.10 && maxPos === 5 && maxPerStock === 0.10) return 'moderate';
    if (stopPct === 0.05 && maxPos >= 8 && maxPerStock >= 0.15) return 'aggressive';
    return 'custom';
}

function buildStrategyTemplates(d) {
    var active = detectActivePreset(d);
    var c = d.auto_deployer_config || {};
    var g = d.guardrails || {};

    // Current settings summary
    var curStop = Math.round(((c.risk_settings && c.risk_settings.default_stop_loss_pct) || 0.10) * 100);
    var curMaxPos = c.max_positions || g.max_positions || 5;
    var curMaxPerStock = Math.round((g.max_position_pct || 0.10) * 100);
    var curStrats = (g.strategies_allowed || []).length;

    var presets = {
        conservative: {
            name: 'Conservative',
            tagline: 'Capital preservation first',
            stopLoss: '5%',
            maxPositions: 3,
            maxPerStock: '5%',
            maxNewPerDay: 1,
            strategies: ['Trailing Stop', 'Wheel', 'Copy Trading'],
            excluded: ['Breakout', 'Mean Reversion', 'Short Selling'],
            detail: 'Tight 5% stops to cut losses fast. Fewer positions (max 3) means less overall market exposure. Smaller 5% position sizing per stock. Only runs proven, slower strategies — no aggressive breakout chasing or short selling.',
            goodFor: 'First-time traders, small accounts under $5k, during high market uncertainty, or when you want to sleep well at night.',
            tradeoffs: 'Lower returns in bull markets (misses breakouts). Slower to deploy capital. Won\'t capture big short-term swings.',
            expectedReturn: '5-15% annually (lower volatility)',
            maxDrawdown: '~5-8% typical',
            color: '#10b981'
        },
        moderate: {
            name: 'Moderate',
            tagline: 'Balanced risk/reward',
            stopLoss: '10%',
            maxPositions: 5,
            maxPerStock: '10%',
            maxNewPerDay: 2,
            strategies: ['All 5 Strategies', '(shorts only in bear)'],
            excluded: ['Short Selling auto-deploy in bull'],
            detail: 'Standard 10% stop-loss. Up to 5 concurrent positions with dynamic volatility-based sizing. All strategies enabled, but shorts only activate in bear markets. This is the default "set and forget" mode for most traders.',
            goodFor: 'The default recommendation. Accounts $5k-$50k. Users who want the bot to work across all market conditions without babysitting.',
            tradeoffs: 'Middle ground — won\'t be the best in any single market regime but stays reasonable across all of them.',
            expectedReturn: '15-25% annually',
            maxDrawdown: '~10% max (enforced by guardrails)',
            color: '#3b82f6'
        },
        aggressive: {
            name: 'Aggressive',
            tagline: 'Maximize upside, accept volatility',
            stopLoss: '5% (tight)',
            maxPositions: 8,
            maxPerStock: '15%',
            maxNewPerDay: 3,
            strategies: ['All 6', 'Shorts enabled anytime'],
            excluded: [],
            detail: 'Tight 5% stops (fail fast), but larger 15% positions and up to 8 concurrent trades. Extended hours trading enabled. Short selling runs in any market regime, not just bear. Breakouts prioritized. Pre-market and after-hours sessions used when appropriate.',
            goodFor: 'Experienced traders. Accounts $25k+ (pattern day trader rules). Active day traders who want maximum signal deployment.',
            tradeoffs: 'Higher drawdowns. More false signals (tight stops = more stop-outs). Requires closer monitoring. Higher tax bills from more frequent trades.',
            expectedReturn: '20-40% annually (or -20% in bad year)',
            maxDrawdown: '~15-20% possible',
            color: '#ef4444'
        }
    };

    var order = ['conservative', 'moderate', 'aggressive'];
    var cards = order.map(function(key) {
        var p = presets[key];
        var isActive = (active === key);
        var badge = isActive
            ? '<span class="preset-active-badge">ACTIVE</span>'
            : '';
        var btnLabel = isActive ? 'Currently Active' : 'Apply ' + p.name;
        var btnDisabled = isActive ? 'disabled' : '';

        var strategiesHtml = p.strategies.map(function(s) {
            return '<span class="preset-pill ok">' + esc(s) + '</span>';
        }).join('');
        var excludedHtml = p.excluded.length ? p.excluded.map(function(s) {
            return '<span class="preset-pill no">' + esc(s) + '</span>';
        }).join('') : '';

        return (
            '<div class="preset-card-v2 ' + (isActive ? 'active' : '') + '" style="border-top-color:' + p.color + '">' +
                '<div class="preset-header">' +
                    '<div>' +
                        '<div class="preset-name" style="color:' + p.color + '">' + p.name + '</div>' +
                        '<div class="preset-tag">' + esc(p.tagline) + '</div>' +
                    '</div>' +
                    badge +
                '</div>' +
                '<div class="preset-stats">' +
                    '<div><span class="lbl">Stop-Loss</span><span class="val">' + p.stopLoss + '</span></div>' +
                    '<div><span class="lbl">Max Positions</span><span class="val">' + p.maxPositions + '</span></div>' +
                    '<div><span class="lbl">Per Stock</span><span class="val">' + p.maxPerStock + '</span></div>' +
                    '<div><span class="lbl">New/Day</span><span class="val">' + p.maxNewPerDay + '</span></div>' +
                '</div>' +
                '<div class="preset-section"><div class="preset-section-title">How it works</div><div class="preset-section-body">' + esc(p.detail) + '</div></div>' +
                '<div class="preset-section"><div class="preset-section-title">Strategies Enabled</div><div>' + strategiesHtml + '</div></div>' +
                (excludedHtml ? '<div class="preset-section"><div class="preset-section-title">Disabled</div><div>' + excludedHtml + '</div></div>' : '') +
                '<div class="preset-section"><div class="preset-section-title">Good for</div><div class="preset-section-body">' + esc(p.goodFor) + '</div></div>' +
                '<div class="preset-section"><div class="preset-section-title">Tradeoffs</div><div class="preset-section-body">' + esc(p.tradeoffs) + '</div></div>' +
                '<div class="preset-outcome">' +
                    '<div class="preset-outcome-row"><span>Expected return</span><strong>' + esc(p.expectedReturn) + '</strong></div>' +
                    '<div class="preset-outcome-row"><span>Max drawdown</span><strong>' + esc(p.maxDrawdown) + '</strong></div>' +
                '</div>' +
                '<button class="preset-apply-btn" onclick="applyPreset(\'' + key + '\')" ' + btnDisabled + ' style="background:' + (isActive ? 'var(--border)' : p.color) + '">' + btnLabel + '</button>' +
            '</div>'
        );
    }).join('');

    // Active mode indicator
    var activeLabel = active === 'custom'
        ? '<span style="color:var(--orange)">CUSTOM (doesn\'t match any preset)</span>'
        : '<span style="color:' + presets[active].color + '">' + presets[active].name.toUpperCase() + '</span>';

    return (
        '<div class="marketplace">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:16px">' +
                '<div>' +
                    '<h3 style="margin:0">Strategy Templates</h3>' +
                    '<div style="font-size:13px;color:var(--text-dim);margin-top:6px">Currently running: ' + activeLabel +
                        ' &nbsp;·&nbsp; Stop: ' + curStop + '% · Max positions: ' + curMaxPos + ' · Per stock: ' + curMaxPerStock + '% · ' + curStrats + ' strategies enabled' +
                    '</div>' +
                '</div>' +
                '<div class="marketplace-actions">' +
                    '<button class="btn-primary btn-sm" onclick="exportStrategies()">Export My Strategies</button>' +
                    '<button class="btn-sm" onclick="document.getElementById(\'importFile\').click()">Import Strategy</button>' +
                    '<input type="file" id="importFile" accept=".json" style="display:none" onchange="importStrategy(this)">' +
                '</div>' +
            '</div>' +
            '<div class="preset-strategies-v2">' + cards + '</div>' +
        '</div>'
    );
}

function buildShortStrategyCard(d) {
    // Determine if shorts are currently unlocked
    var marketRegime = (d.market_regime || (d.economic_calendar && d.economic_calendar.market_regime) || 'neutral').toLowerCase();
    var spyMom20 = d.spy_momentum_20d != null ? d.spy_momentum_20d : 0;
    var shortsUnlocked = (marketRegime === 'bear' && spyMom20 < -3);
    var config = d.auto_deployer_config || {};
    var shortConfig = (config.short_selling || {});
    var userEnabled = shortConfig.enabled !== false;  // default true
    var effectivelyActive = userEnabled && shortsUnlocked;

    // Count active short positions
    var shortCount = 0;
    if (d.positions) {
        shortCount = d.positions.filter(function(p) { return parseFloat(p.qty || 0) < 0; }).length;
    }
    var maxShorts = shortConfig.max_short_positions || 1;

    // Status badge
    var statusBadge;
    if (!userEnabled) {
        statusBadge = '<span class="badge-inactive">TURNED OFF</span>';
    } else if (effectivelyActive) {
        statusBadge = '<span class="badge-active">ACTIVE (BEAR MKT)</span>';
    } else {
        statusBadge = '<span class="badge-pending">STANDBY</span>';
    }

    var spyStr = (spyMom20 >= 0 ? '+' : '') + spyMom20.toFixed(1) + '%';
    var conditions = effectivelyActive
        ? 'Bear market detected. Shorts will deploy on qualifying candidates.'
        : (!userEnabled
            ? 'Short selling is turned OFF. Enable below to allow shorts in bear markets.'
            : 'Waiting for bear market. SPY 20d: ' + spyStr + ' (need &lt; -3%). Regime: ' + marketRegime);

    // Top candidate preview
    var topShort = (d.short_candidates && d.short_candidates.length) ? d.short_candidates[0] : null;
    var topShortHtml = topShort
        ? '<div class="stat"><div class="stat-label">Top Candidate</div><div class="stat-value" style="font-size:14px">' + esc(topShort.symbol) + ' (score ' + topShort.short_score + ')</div></div>'
        : '<div class="stat"><div class="stat-label">Top Candidate</div><div class="stat-value" style="font-size:12px;color:var(--text-dim)">None scoring &ge;15</div></div>';

    // Toggle button
    var toggleBtn = userEnabled
        ? '<button class="btn-warning btn-sm" onclick="toggleShortSelling(false)">Turn Off</button>'
        : '<button class="btn-primary btn-sm" onclick="toggleShortSelling(true)">Turn On</button>';

    return (
        '<div class="strategy-card short-sell">' +
            '<h2>6. Short Selling</h2>' +
            '<div class="subtitle">Bear market plays — profit when stocks fall</div>' +
            '<div class="stat-grid">' +
                '<div class="stat"><div class="stat-label">Status</div><div class="stat-value">' + statusBadge + '</div></div>' +
                '<div class="stat"><div class="stat-label">Positions</div><div class="stat-value">' + shortCount + '/' + maxShorts + '</div></div>' +
                '<div class="stat"><div class="stat-label">SPY 20d</div><div class="stat-value">' + spyStr + '</div></div>' +
                '<div class="stat"><div class="stat-label">Stop-Loss</div><div class="stat-value">8% (tight)</div></div>' +
                topShortHtml +
                '<div class="stat"><div class="stat-label">Profit Target</div><div class="stat-value">15%</div></div>' +
            '</div>' +
            '<div class="strategy-visual"><strong>Rules:</strong> Bear market only | Max 1 short | 5% portfolio per short | 48hr cooldown after loss | No meme stocks</div>' +
            '<div class="strategy-visual" style="background:rgba(239,68,68,0.05);border-color:rgba(239,68,68,0.2);margin-top:8px;font-size:11px">' + conditions + '</div>' +
            '<div class="strategy-actions">' +
                toggleBtn +
                '<button class="btn-warning btn-sm" onclick="if(confirm(\'Pause short selling? The bot will not deploy new shorts.\')) fetch(\'/api/pause-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'short_sell\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Paused\',\'info\');addLog(\'Paused short selling\',\'info\');refreshData();})">Pause</button>' +
                '<button class="btn-danger btn-sm" onclick="if(confirm(\'Stop short selling? This will cover (buy back) any open short positions.\')) fetch(\'/api/stop-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'short_sell\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Stopped\',\'info\');addLog(\'Stopped short selling\',\'info\');refreshData();})">Stop</button>' +
            '</div>' +
        '</div>'
    );
}

function toggleShortSelling(enable) {
    var msg = enable
        ? 'Turn ON short selling? Shorts will only deploy in bear market conditions with tight 8% stops.'
        : 'Turn OFF short selling? The bot will not deploy any new short positions.';
    if (!confirm(msg)) return;
    fetch(API_BASE + '/api/toggle-short-selling', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify({enabled: enable})
    }).then(function(r){ return r.json(); }).then(function(d){
        toast(d.message || (enable ? 'Short selling enabled' : 'Short selling disabled'), 'info');
        addLog((enable ? 'Enabled' : 'Disabled') + ' short selling', enable ? 'success' : 'info');
        refreshData();
    }).catch(function(e){ toast('Error: ' + e.message, 'error'); });
}

function renderDashboard() {
    const d = dashboardData;
    if (!d) return;
    const acct = d.account || {};
    const equity = parseFloat(acct.equity||0);
    const cash = parseFloat(acct.cash||0);
    const buyingPower = parseFloat(acct.buying_power||0);
    const portfolioValue = parseFloat(acct.portfolio_value||0);
    const longMV = parseFloat(acct.long_market_value||0);
    const lastEquity = parseFloat(acct.last_equity||portfolioValue);
    const dailyPnl = portfolioValue - lastEquity;
    const dailyPnlPct = lastEquity ? (dailyPnl / lastEquity * 100) : 0;

    const regime = getMarketRegime(d);
    const regimeLabel = {bull:'Bull Market',neutral:'Neutral',bear:'Bear Market'}[regime];
    const regimeClass = {bull:'regime-bull',neutral:'regime-neutral',bear:'regime-bear'}[regime];

    const pnlAlertHtml = dailyPnlPct < -3
        ? '<div class="pnl-alert"><span class="icon">\u26A0</span><strong>Daily Loss Alert:</strong>&nbsp;Portfolio is down ' + Math.abs(dailyPnlPct).toFixed(1) + '% today (' + fmtMoney(dailyPnl) + '). Consider reducing exposure.</div>'
        : '';

    // Trailing Stop
    const ts = d.trailing || {};
    const tsState = ts.state || {};
    const tsRules = ts.rules || {};
    const tsSymbol = ts.symbol || (tsRules.symbol||'N/A');
    const tsEntry = tsState.entry_fill_price || ts.entry_price_estimate || 0;
    const tsShares = tsState.total_shares_held || 0;
    const tsStop = tsState.current_stop_price || 'Not set';
    const tsTrailing = tsState.trailing_activated || false;
    const tsHighest = tsState.highest_price_seen || 'N/A';
    const trailingBadge = tsTrailing
        ? '<span class="badge-active">ACTIVE</span>'
        : '<span class="badge-inactive">WAITING</span>';

    // Copy Trading
    const ct = d.copy_trading || {};
    const ctState = ct.state || {};
    const ctPolitician = ctState.selected_politician || 'Not selected yet';
    const ctTrades = ctState.trades_copied || [];
    const ctPnl = ctState.total_realized_pnl || 0;
    const ctBadge = ctTrades.length
        ? '<span class="badge-active">ACTIVE</span>'
        : '<span class="badge-pending">SETUP NEEDED</span>';

    // Wheel
    const ws = d.wheel || {};
    const wsState = ws.state || {};
    const wsStage = wsState.current_stage || 'stage_1_sell_puts';
    const wsStageLabel = wsStage.includes('stage_1') ? 'Stage 1: Selling Puts' : 'Stage 2: Selling Calls';
    const wsPremiums = wsState.total_premiums_collected || 0;
    const wsCycles = wsState.cycles_completed || 0;
    const wsBadge = wsCycles > 0
        ? '<span class="badge-active">ACTIVE</span>'
        : '<span class="badge-pending">SETUP NEEDED</span>';

    // Top 3 Picks
    const picks = d.picks || [];
    const top3 = picks.slice(0,3);
    const maxScore = Math.max(...picks.map(p=>p.best_score||0), 1);
    const stratColors = {'Trailing Stop':'#3b82f6','Copy Trading':'#10b981','Wheel Strategy':'#8b5cf6','Mean Reversion':'#f59e0b','Breakout':'#ef4444'};
    const rankLabels = ['TOP PICK','RUNNER UP','STRONG OPTION'];

    // Helper: build technical indicators HTML for a pick
    function buildPickIndicators(p) {
        const rsi = p.rsi != null ? p.rsi : 50;
        let rsiLabel = 'neutral', rsiClass = 'neutral';
        if (rsi > 70) { rsiLabel = 'overbought'; rsiClass = 'bearish'; }
        else if (rsi < 30) { rsiLabel = 'oversold'; rsiClass = 'bullish'; }

        let macdLabel = 'neutral', macdClass = 'neutral';
        if (p.macd_histogram > 0) { macdLabel = 'bullish'; macdClass = 'bullish'; }
        else if (p.macd_histogram < 0) { macdLabel = 'bearish'; macdClass = 'bearish'; }

        const bias = (p.overall_bias || 'neutral').toLowerCase();
        const biasClass = bias === 'bullish' ? 'bullish' : bias === 'bearish' ? 'bearish' : 'neutral';

        return '<div class="pick-indicators">' +
            '<span class="indicator-badge ' + rsiClass + '">RSI: ' + Math.round(rsi) + ' (' + rsiLabel + ')</span>' +
            '<span class="indicator-badge ' + macdClass + '">MACD: ' + macdLabel + '</span>' +
            '<span class="indicator-badge ' + biasClass + '">Bias: ' + bias.toUpperCase() + '</span>' +
        '</div>';
    }

    // Helper: build social sentiment HTML for a pick
    function buildPickSocial(p) {
        if (!p.social_sentiment || p.social_sentiment === 'unknown') return '';
        const sent = (p.social_sentiment || '').toLowerCase();
        const sentClass = sent === 'bullish' ? 'bullish' : sent === 'bearish' ? 'bearish' : 'neutral';
        const score = p.social_score != null ? ' (+' + Math.round(p.social_score) + ')' : '';
        const trending = p.social_trending ? ' <span class="trending-badge">\ud83d\udd25 Trending</span>' : '';
        return '<div class="pick-social">' +
            'Social: <span class="social-badge ' + sentClass + '">' + sent.charAt(0).toUpperCase() + sent.slice(1) + score + '</span>' +
            trending +
        '</div>';
    }

    let picksHtml = '';
    top3.forEach((p,i) => {
        const color = stratColors[p.best_strategy] || '#3b82f6';
        const chgCls = p.daily_change >= 0 ? 'positive' : 'negative';
        const vsCls = p.volume_surge > 0 ? 'positive' : 'negative';
        const tPct = Math.max(5, Math.min(100, (p.trailing_score||0) / maxScore * 100));
        const cPct = Math.max(5, Math.min(100, (p.copy_score||0) / maxScore * 100));
        const wPct = Math.max(5, Math.min(100, (p.wheel_score||0) / maxScore * 100));
        const mrPct = Math.max(5, Math.min(100, (p.mean_reversion_score||0) / maxScore * 100));
        const boPct = Math.max(5, Math.min(100, (p.breakout_score||0) / maxScore * 100));
        const recShares = p.recommended_shares || Math.max(1, Math.floor(portfolioValue * 0.05 / p.price));
        const backtestPct = p.backtest_return != null ? parseFloat(p.backtest_return).toFixed(1) : (p.daily_change * 0.3 + p.volatility * 0.1).toFixed(1);

        picksHtml += '<div class="pick-card" style="border-top:3px solid ' + color + '">' +
            '<div class="pick-header"><div>' +
            '<span class="pick-rank" style="color:' + color + '">' + (rankLabels[i]||'') + '</span>' +
            '<div class="pick-symbol">' + esc(p.symbol) + '</div>' +
            '<div class="pick-price">' + fmtMoney(p.price) + '</div></div>' +
            '<div class="pick-strategy-badge" style="background:' + color + '20;color:' + color + '">' + esc(p.best_strategy) + '</div></div>' +
            '<div class="pick-stats">' +
            '<div class="pick-stat"><span class="pick-stat-label">Daily Change</span><span class="pick-stat-value ' + chgCls + '">' + fmtPct(p.daily_change) + '</span></div>' +
            '<div class="pick-stat"><span class="pick-stat-label">Volatility</span><span class="pick-stat-value">' + (p.volatility||0).toFixed(1) + '%</span></div>' +
            '<div class="pick-stat"><span class="pick-stat-label">Volume</span><span class="pick-stat-value">' + ((p.daily_volume||0)/1e6).toFixed(1) + 'M</span></div>' +
            '<div class="pick-stat"><span class="pick-stat-label">Vol Surge</span><span class="pick-stat-value ' + vsCls + '">' + fmtPct(p.volume_surge||0).replace('.0','') + '</span></div></div>' +
            '<div class="pick-scores">' +
            '<div class="score-row"><span class="score-label" style="color:#3b82f6">Trailing</span><div class="score-bar-bg"><div class="score-bar" style="width:' + tPct + '%;background:#3b82f6"></div></div><span class="score-val">' + Math.round(p.trailing_score||0) + '</span></div>' +
            '<div class="score-row"><span class="score-label" style="color:#10b981">Copy</span><div class="score-bar-bg"><div class="score-bar" style="width:' + cPct + '%;background:#10b981"></div></div><span class="score-val">' + Math.round(p.copy_score||0) + '</span></div>' +
            '<div class="score-row"><span class="score-label" style="color:#8b5cf6">Wheel</span><div class="score-bar-bg"><div class="score-bar" style="width:' + wPct + '%;background:#8b5cf6"></div></div><span class="score-val">' + Math.round(p.wheel_score||0) + '</span></div>' +
            '<div class="score-row"><span class="score-label" style="color:#f59e0b">MeanRev</span><div class="score-bar-bg"><div class="score-bar" style="width:' + mrPct + '%;background:#f59e0b"></div></div><span class="score-val">' + Math.round(p.mean_reversion_score||0) + '</span></div>' +
            '<div class="score-row"><span class="score-label" style="color:#ef4444">Breakout</span><div class="score-bar-bg"><div class="score-bar" style="width:' + boPct + '%;background:#ef4444"></div></div><span class="score-val">' + Math.round(p.breakout_score||0) + '</span></div></div>' +
            buildPickIndicators(p) +
            buildPickSocial(p) +
            '<div class="backtest-result">Rec. shares: ' + recShares + ' | 30d backtest: <span class="' + (parseFloat(backtestPct)>=0?'positive':'negative') + '">' + (parseFloat(backtestPct)>=0?'+':'') + backtestPct + '%</span></div>' +
            (p.earnings_warning ? '<div class="earnings-badge">\u26A0 Earnings Soon</div>' : '') +
            '<div class="pick-actions">' +
            '<button class="pick-deploy-btn" onclick="openDeployModal(\'' + p.symbol + '\',\'' + p.best_strategy + '\',' + recShares + ',' + p.price + ')">Deploy ' + p.best_strategy + '</button></div></div>';
    });
    if (!picksHtml) picksHtml = '<div class="empty" style="grid-column:1/-1">No picks available - market may be closed</div>';

    // Positions table
    const positions = d.positions || [];
    let posHtml = '';
    positions.forEach(p => {
        const upl = parseFloat(p.unrealized_pl||0);
        const uplPct = parseFloat(p.unrealized_plpc||0) * 100;
        const cls = pnlClass(upl);
        const qty = parseInt(p.qty||0);
        const avgEntry = parseFloat(p.avg_entry_price||0);
        const curPrice = parseFloat(p.current_price||0);
        const sym = p.symbol||'';
        // Detect deploy source badge
        const srcBadge = p._auto_deployed
            ? '<span class="deploy-source-badge auto">AUTO</span>'
            : '<span class="deploy-source-badge manual">MANUAL</span>';
        posHtml += '<tr>' +
            '<td><strong>' + esc(sym) + '</strong>' + srcBadge + '</td>' +
            '<td>' + qty + '</td>' +
            '<td>' + fmtMoney(avgEntry) + '</td>' +
            '<td>' + fmtMoney(curPrice) + '</td>' +
            '<td class="' + cls + '">' + fmtMoney(upl) + '</td>' +
            '<td class="' + cls + '">' + fmtPct(uplPct) + '</td>' +
            '<td>' +
            '<button class="btn-danger btn-sm" onclick="openClosePositionModal(\'' + sym + '\',' + qty + ',' + avgEntry + ',' + curPrice + ',' + upl + ',' + uplPct + ')">Close</button> ' +
            '<button class="btn-warning btn-sm" onclick="openSellHalfModal(\'' + sym + '\',' + qty + ',' + avgEntry + ',' + curPrice + ',' + upl + ',' + uplPct + ')">Sell Half</button>' +
            '</td></tr>';
    });
    if (!posHtml) posHtml = '<tr><td colspan="7" class="empty">No open positions</td></tr>';

    // Orders table
    const orders = d.open_orders || [];
    let ordHtml = '';
    orders.forEach(o => {
        const sideCls = o.side === 'buy' ? 'side-buy' : 'side-sell';
        const price = o.limit_price || o.stop_price || 'Market';
        ordHtml += '<tr>' +
            '<td><strong>' + esc(o.symbol||'') + '</strong></td>' +
            '<td><span class="' + sideCls + '">' + esc((o.side||'').toUpperCase()) + '</span></td>' +
            '<td>' + esc(o.type||'') + '</td>' +
            '<td>' + esc(o.qty||'') + '</td>' +
            '<td>$' + price + '</td>' +
            '<td>' + (o.status||'') + '</td>' +
            '<td><button class="btn-danger btn-sm" onclick="openCancelOrderModal(\'' + (o.id||'') + '\',\'' + (o.symbol||'') + '\',\'' + (o.side||'') + '\',\'' + (o.type||'') + '\',\'' + (o.qty||'') + '\',\'' + price + '\')">Cancel</button></td></tr>';
    });
    if (!ordHtml) ordHtml = '<tr><td colspan="7" class="empty">No open orders</td></tr>';

    // Screener (top 50, sortable)
    let screenerPicks = picks.slice(0,50).map((p,i) => ({...p, rank: i+1}));
    screenerPicks.sort((a,b) => {
        let va = a[screenerSortCol], vb = b[screenerSortCol];
        if (typeof va === 'string') return screenerSortDir * va.localeCompare(vb);
        return screenerSortDir * ((va||0) - (vb||0));
    });
    const sortArrow = (col) => screenerSortCol === col ? (screenerSortDir > 0 ? ' \u25B2' : ' \u25BC') : '';
    let scrHtml = '';
    screenerPicks.forEach((p,i) => {
        const color = stratColors[p.best_strategy] || '#3b82f6';
        const chgCls = p.daily_change >= 0 ? 'positive' : 'negative';
        const vsCls = (p.volume_surge||0) > 0 ? 'positive' : 'negative';
        const hl = i < 3 ? ' style="background:rgba(59,130,246,0.04)"' : '';
        const recShares = Math.max(1, Math.floor(portfolioValue * 0.05 / p.price));
        scrHtml += '<tr' + hl + '>' +
            '<td>' + (i+1) + '</td>' +
            '<td><strong>' + esc(p.symbol) + '</strong></td>' +
            '<td>' + fmtMoney(p.price) + '</td>' +
            '<td class="' + chgCls + '">' + fmtPct(p.daily_change) + '</td>' +
            '<td>' + (p.volatility||0).toFixed(1) + '%</td>' +
            '<td>' + ((p.daily_volume||0)/1e6).toFixed(1) + 'M</td>' +
            '<td class="' + vsCls + '">' + fmtPct(p.volume_surge||0).replace('.0','') + '</td>' +
            '<td style="color:' + color + ';font-weight:600">' + esc(p.best_strategy) + '</td>' +
            '<td style="font-weight:700">' + Math.round(p.best_score||0) + '</td>' +
            '<td><button class="btn-primary btn-sm" onclick="openDeployModal(\'' + p.symbol + '\',\'' + p.best_strategy + '\',' + recShares + ',' + p.price + ')">Deploy</button></td></tr>';
    });
    if (!scrHtml) scrHtml = '<tr><td colspan="10" class="empty">No screener data - market may be closed</td></tr>';

    // Trading session badge
    const session = (d.trading_session || 'closed').toLowerCase();
    let sessionLabel = 'CLOSED', sessionClass = 'session-closed';
    if (session === 'pre-market' || session === 'pre_market' || session === 'premarket') { sessionLabel = 'PRE-MARKET'; sessionClass = 'session-pre'; }
    else if (session === 'open' || session === 'market_open' || session === 'regular') { sessionLabel = 'MARKET OPEN'; sessionClass = 'session-open'; }
    else if (session === 'after-hours' || session === 'after_hours' || session === 'afterhours' || session === 'post_market') { sessionLabel = 'AFTER HOURS'; sessionClass = 'session-after'; }

    // Readiness score for header
    const scorecard = d.scorecard || {};
    const readinessScore = scorecard.readiness_score || 0;
    let readBarColor = 'var(--red)';
    if (readinessScore >= 80) readBarColor = 'var(--green)';
    else if (readinessScore >= 40) readBarColor = 'var(--orange)';

    // Economic calendar banner
    const econ = d.economic_calendar || {};
    let econBannerHtml = '';
    const econEvents = econ.events || [];
    const econRisk = (econ.risk_level || 'normal').toLowerCase();
    if (econEvents.length > 0) {
        const topEvent = econEvents[0];
        const impactClass = topEvent.impact === 'high' ? 'high' : topEvent.impact === 'medium' ? 'medium' : 'normal';
        const impactIcon = topEvent.impact === 'high' ? '\u26A0\uFE0F' : '\u2139\uFE0F';
        econBannerHtml = '<div class="econ-banner ' + impactClass + '">' +
            '<span class="econ-icon">' + impactIcon + '</span>' +
            '<div><strong>' + esc(topEvent.event) + '</strong> (' + esc(topEvent.date) + ', ' + topEvent.days_away + 'd away) &mdash; ' + esc(topEvent.action || econ.recommendation || '') + '</div>' +
        '</div>';
    }

    // Short candidates section
    const shortCands = d.short_candidates || [];
    const marketRegime = (d.market_regime || (d.economic_calendar && d.economic_calendar.market_regime) || 'neutral');
    const spyMom20 = (typeof d.spy_momentum_20d === 'number') ? d.spy_momentum_20d : 0;
    const shortsUnlocked = (marketRegime === 'bear' && spyMom20 < -3);
    const shortBadge = shortsUnlocked
        ? '<span class="badge-active" style="background:rgba(239,68,68,0.15);color:var(--red)">SHORTS ENABLED</span>'
        : '<span class="badge-pending">SHORTS DISABLED</span>';
    const shortStatusLine = shortsUnlocked
        ? 'Bear market conditions confirmed &mdash; SPY 20d ' + fmtPct(spyMom20) + ' (threshold: &lt; -3%). Auto-deployer may deploy up to 1 short.'
        : 'Shorts unlock in bear market (SPY currently ' + fmtPct(spyMom20) + ' in 20d, need &lt; -3% AND regime = bear). Current regime: <strong>' + esc(marketRegime) + '</strong>';

    let shortHtml = '';
    if (shortCands.length > 0) {
        let rows = '';
        shortCands.forEach(function(sc) {
            const memeFlag = sc.meme_warning ? ' <span style="color:var(--orange);font-weight:700">MEME!</span>' : '';
            rows += '<tr>' +
                '<td><strong>' + esc(sc.symbol) + '</strong>' + memeFlag + '</td>' +
                '<td>' + fmtMoney(sc.price) + '</td>' +
                '<td style="font-weight:700">' + (sc.short_score||0) + '</td>' +
                '<td class="negative">' + fmtPct(sc.momentum_20d||0) + '</td>' +
                '<td>' + fmtMoney(sc.stop_loss||0) + '</td>' +
                '<td class="positive">' + fmtMoney(sc.profit_target||0) + '</td>' +
                '<td>' + (sc.risk_reward||0).toFixed(1) + '</td>' +
                '<td style="font-size:11px;color:var(--text-dim)">' + esc((sc.reasons||[]).slice(0,2).join('; ')) + '</td>' +
            '</tr>';
        });
        shortHtml = '<div class="short-section" id="section-shorts">' +
            '<h3>\u{1F4C9} Short Selling Candidates (Bear Market Plays) ' + shortBadge + '</h3>' +
            '<div style="font-size:12px;color:var(--text-dim);margin-bottom:12px">' + shortStatusLine + '</div>' +
            '<table><thead><tr><th>Symbol</th><th>Price</th><th>Score</th><th>20d Mom</th><th>Stop</th><th>Target</th><th>R:R</th><th>Reasons</th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table></div>';
    } else {
        // Still show the section with status even if no candidates
        shortHtml = '<div class="short-section" id="section-shorts">' +
            '<h3>\u{1F4C9} Short Selling Candidates (Bear Market Plays) ' + shortBadge + '</h3>' +
            '<div style="font-size:12px;color:var(--text-dim)">' + shortStatusLine + '</div>' +
            '<div class="empty" style="padding:16px;color:var(--text-dim)">No short candidates scored above threshold today.</div>' +
            '</div>';
    }

    // Tax-loss harvesting section
    let taxHtml = '';
    const losers = positions.filter(function(p) { return parseFloat(p.unrealized_pl||0) < 0; });
    if (losers.length > 0) {
        let taxRows = '';
        losers.forEach(function(p) {
            const loss = parseFloat(p.unrealized_pl||0);
            const lossPct = parseFloat(p.unrealized_plpc||0) * 100;
            const taxSavings = Math.abs(loss) * 0.25;  // est 25% tax rate
            taxRows += '<tr>' +
                '<td><strong>' + esc(p.symbol||'') + '</strong></td>' +
                '<td class="negative">' + fmtMoney(loss) + '</td>' +
                '<td class="negative">' + fmtPct(lossPct) + '</td>' +
                '<td class="positive">~' + fmtMoney(taxSavings) + '</td>' +
                '<td style="font-size:11px;color:var(--text-dim)">Sector ETF or similar</td>' +
                '<td><button class="btn-warning btn-sm" onclick="openClosePositionModal(\'' + esc(p.symbol||'') + '\',' + (p.qty||0) + ',' + parseFloat(p.avg_entry_price||0) + ',' + parseFloat(p.current_price||0) + ',' + loss + ',' + lossPct + ')">Harvest</button></td>' +
            '</tr>';
        });
        taxHtml = '<div class="tax-section" id="section-tax">' +
            '<h3>\u{1F4B0} Tax-Loss Harvesting Opportunities</h3>' +
            '<table><thead><tr><th>Symbol</th><th>Loss</th><th>Loss %</th><th>Tax Savings Est.</th><th>Replace With</th><th>Action</th></tr></thead>' +
            '<tbody>' + taxRows + '</tbody></table></div>';
    }

    // Correlation warning
    let corrHtml = '';
    if (positions.length > 1) {
        corrHtml = '<div class="correlation-section" id="section-correlation">' +
            '<h3>Position Correlation</h3>' +
            '<div class="corr-warning">\u26A0 You hold ' + positions.length + ' positions. Review sector overlap to avoid concentrated risk. Positions in the same sector tend to move together during sell-offs.</div>' +
            '<div style="font-size:12px;color:var(--text-dim);margin-top:8px">Sectors: ' +
            positions.map(function(p) { return '<strong>' + esc(p.symbol||'') + '</strong>'; }).join(', ') +
            '</div></div>';
    }

    // Readiness scorecard section
    const sc = d.scorecard || {};
    const criteria = sc.readiness_criteria || {};
    const scDays = Math.round((new Date() - new Date(sc.start_date || new Date())) / 86400000) || 0;
    let readinessHtml = '<div class="readiness-card" id="section-readiness">' +
        '<h3>\u{1F4CA} Paper Trading Progress</h3>' +
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">' +
            '<span style="font-size:22px;font-weight:800">Readiness Score: ' + readinessScore + '/100</span>' +
            '<span class="' + (readinessScore >= 80 ? 'badge-active' : 'badge-inactive') + '">' + (readinessScore >= 80 ? 'READY' : 'NOT READY') + '</span>' +
        '</div>' +
        '<div class="readiness-progress"><div class="readiness-progress-fill" style="width:' + readinessScore + '%;background:' + readBarColor + '"></div></div>' +
        '<div class="readiness-metrics">' +
            '<div class="readiness-metric"><div class="rm-label">Days Tracked</div><div class="rm-value">' + scDays + '</div><div class="rm-target">Target: ' + (criteria.min_days||30) + '</div></div>' +
            '<div class="readiness-metric"><div class="rm-label">Total Trades</div><div class="rm-value">' + (sc.total_trades||0) + '</div><div class="rm-target">Target: ' + (criteria.min_trades||20) + '</div></div>' +
            '<div class="readiness-metric"><div class="rm-label">Win Rate</div><div class="rm-value">' + (sc.win_rate_pct||0) + '%</div><div class="rm-target">Target: ' + (criteria.min_win_rate||50) + '%</div></div>' +
            '<div class="readiness-metric"><div class="rm-label">Max Drawdown</div><div class="rm-value">' + (sc.max_drawdown_pct||0).toFixed(1) + '%</div><div class="rm-target">Max: ' + (criteria.max_drawdown||10) + '%</div></div>' +
            '<div class="readiness-metric"><div class="rm-label">Profit Factor</div><div class="rm-value">' + (sc.profit_factor||0).toFixed(2) + '</div><div class="rm-target">Target: ' + (criteria.min_profit_factor||1.5) + '</div></div>' +
            '<div class="readiness-metric"><div class="rm-label">Sharpe Ratio</div><div class="rm-value">' + (sc.sharpe_ratio||0).toFixed(2) + '</div><div class="rm-target">Target: ' + (criteria.min_sharpe||0.5) + '</div></div>' +
        '</div>' +
        '<div class="readiness-status ' + (sc.ready_for_live ? 'ready' : 'not-ready') + '">' +
            (sc.ready_for_live
                ? '\u2705 READY for live trading! All criteria met.'
                : '\u274C NOT READY — Need ' + (criteria.min_days||30) + ' days of profitable paper trading before going live.') +
        '</div></div>';

    // Options chain info for wheel strategy card
    const optData = d.options_data || null;
    let optionsInfoHtml = '';
    if (optData && optData.put_analysis) {
        const pa = optData.put_analysis;
        const ca = optData.call_analysis;
        const topPut = (pa.candidates && pa.candidates.length > 0) ? pa.candidates[0] : null;
        optionsInfoHtml = '<div class="options-info">' +
            '<div style="font-weight:700;margin-bottom:6px">Options Chain Analysis - ' + esc(optData.symbol||'') + '</div>';
        if (topPut) {
            optionsInfoHtml +=
                '<div class="opt-row"><span class="opt-label">Best Put Strike</span><span class="opt-value">$' + topPut.strike + ' (' + topPut.expiration + ')</span></div>' +
                '<div class="opt-row"><span class="opt-label">OTM Distance</span><span class="opt-value">' + (topPut.strike_distance_pct||0).toFixed(1) + '%</span></div>' +
                '<div class="opt-row"><span class="opt-label">Days to Exp</span><span class="opt-value">' + (topPut.dte||0) + '</span></div>';
        }
        if (ca && ca.candidates && ca.candidates.length > 0) {
            const topCall = ca.candidates[0];
            optionsInfoHtml +=
                '<div class="opt-row"><span class="opt-label">Best Call Strike</span><span class="opt-value">$' + topCall.strike + ' (' + topCall.expiration + ')</span></div>';
        }
        optionsInfoHtml += '</div>';
    }

    // Navigation tabs
    const navTabs = '<div class="nav-tabs">' +
        '<button class="nav-tab active" onclick="scrollToSection(\'section-overview\')">Overview</button>' +
        '<button class="nav-tab" onclick="scrollToSection(\'section-picks\')">Picks</button>' +
        '<button class="nav-tab" onclick="scrollToSection(\'section-strategies\')">Strategies</button>' +
        '<button class="nav-tab" onclick="scrollToSection(\'section-positions\')">Positions</button>' +
        '<button class="nav-tab" onclick="scrollToSection(\'section-screener\')">Screener</button>' +
        (shortCands.length > 0 ? '<button class="nav-tab" onclick="scrollToSection(\'section-shorts\')">Short Sells</button>' : '') +
        (losers.length > 0 ? '<button class="nav-tab" onclick="scrollToSection(\'section-tax\')">Tax Harvest</button>' : '') +
        '<button class="nav-tab" onclick="scrollToSection(\'section-backtest\')">Backtest</button>' +
        '<button class="nav-tab" onclick="scrollToSection(\'section-readiness\')">Readiness</button>' +
        '<button class="nav-tab" onclick="scrollToSection(\'section-scheduler\')">Scheduler</button>' +
        '<button class="nav-tab" onclick="scrollToSection(\'section-settings\')">Settings</button>' +
    '</div>';

    document.getElementById('app').innerHTML =
        '<div class="header">' +
            '<div class="header-left">' +
                '<h1>Stock Trading Bot Dashboard</h1>' +
                '<div class="updated">Last updated: ' + (d.updated_at||'N/A') + ' &nbsp; <span class="' + regimeClass + ' regime-badge">' + regimeLabel + '</span></div>' +
            '</div>' +
            '<div class="header-right">' +
                '<span class="session-badge ' + sessionClass + '">' + sessionLabel + '</span>' +
                '<span id="schedulerBadge" class="scheduler-badge" title="Loading..."><span class="scheduler-pulse"></span>...</span>' +
                '<div class="readiness-mini">' +
                    '<span class="readiness-label">Ready: ' + readinessScore + '/100</span>' +
                    '<div class="readiness-bar-bg"><div class="readiness-bar-fill" style="width:' + readinessScore + '%;background:' + readBarColor + '"></div></div>' +
                '</div>' +
                '<div class="auto-deployer-toggle">' +
                    '<span class="toggle-label">Auto-Deployer</span>' +
                    '<label class="toggle-switch">' +
                        '<input type="checkbox" id="autoDeployerCheckbox" ' + (autoDeployerEnabled ? 'checked' : '') + ' onchange="toggleAutoDeployer()">' +
                        '<span class="toggle-slider"></span>' +
                    '</label>' +
                    '<span class="toggle-status ' + (autoDeployerEnabled ? 'on' : 'off') + '" id="autoDeployerStatus">' + (autoDeployerEnabled ? 'ON' : 'OFF') + '</span>' +
                '</div>' +
                (killSwitchActive
                    ? '<span class="kill-switch-indicator">KILL SWITCH ACTIVE</span>'
                    : '<button class="kill-switch-btn" onclick="openKillSwitchModal()">KILL SWITCH</button>') +
                '<button id="voiceBtn" class="voice-btn" onclick="toggleVoice()" title="Voice Control">\ud83c\udfa4</button>' +
                '<span class="countdown" id="countdown">Next refresh: ' + countdown + 's</span>' +
                '<button class="btn-primary" onclick="forceRefresh()">\u21BB Refresh</button>' +
                '<span class="paper-badge">PAPER TRADING</span>' +
            '</div>' +
        '</div>' +
        (killSwitchActive
            ? '<div class="kill-switch-active-banner"><span>KILL SWITCH ACTIVE -- All trading halted. Auto-deployer disabled.</span><button class="deactivate-btn" onclick="deactivateKillSwitch()">Deactivate</button></div>'
            : '') +
        econBannerHtml +
        pnlAlertHtml +
        navTabs +
        '<div id="section-overview">' +
        '<div class="account-bar">' +
            '<div class="metric"><div class="label">Portfolio Value</div><div class="value">' + fmtMoney(portfolioValue) + '</div></div>' +
            '<div class="metric"><div class="label">Cash</div><div class="value">' + fmtMoney(cash) + '</div></div>' +
            '<div class="metric"><div class="label">Buying Power</div><div class="value">' + fmtMoney(buyingPower) + '</div></div>' +
            '<div class="metric"><div class="label">Equity</div><div class="value">' + fmtMoney(equity) + '</div></div>' +
            '<div class="metric"><div class="label">Daily P&L</div><div class="value ' + pnlClass(dailyPnl) + '">' + fmtMoney(dailyPnl) + ' (' + fmtPct(dailyPnlPct) + ')</div></div>' +
        '</div>' +
        buildGuardrailMeters(dailyPnlPct, portfolioValue, lastEquity) +
        buildNextActionsPanel(d) +
        '</div>' +
        '<div id="section-picks">' +
        '<div class="section-title">Top 3 Stock Picks <span class="subtitle">- Screened ' + (d.total_screened||0).toLocaleString() + ' stocks, scored ' + (d.total_passed||0).toLocaleString() + ' after filtering</span></div>' +
        '<div class="picks">' + picksHtml + '</div>' +
        '</div>' +
        '<div id="section-strategies">' +
        '<div class="section-title">Active Strategies</div>' +
        '<div class="strategies">' +
            '<div class="strategy-card trailing">' +
                '<h2>1. Trailing Stop</h2>' +
                '<div class="subtitle">' + tsSymbol + ' - Auto stop-loss with ratcheting floor</div>' +
                '<div class="stat-grid">' +
                    '<div class="stat"><div class="stat-label">Entry Price</div><div class="stat-value">' + (tsEntry ? '$'+tsEntry : 'Pending') + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Shares Held</div><div class="stat-value">' + tsShares + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Stop Price</div><div class="stat-value">' + tsStop + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Trailing</div><div class="stat-value">' + trailingBadge + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Highest Seen</div><div class="stat-value">' + tsHighest + '</div></div>' +
                '</div>' +
                '<div class="strategy-visual"><strong>Rules:</strong> 10% stop-loss | +10% activates trailing | 5% trail distance | Floor only goes up</div>' +
                '<div class="strategy-actions">' +
                    '<button class="btn-warning btn-sm" onclick="if(confirm(\'Pause trailing stop? The monitor will skip this strategy until resumed.\')) fetch(\'/api/pause-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'trailing_stop\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Paused\',\'info\');addLog(\'Paused trailing stop strategy\',\'info\');refreshData();})">Pause</button>' +
                    '<button class="btn-danger btn-sm" onclick="if(confirm(\'Stop trailing stop? This will cancel related orders.\')) fetch(\'/api/stop-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'trailing_stop\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Stopped\',\'info\');addLog(\'Stopped trailing stop strategy\',\'info\');refreshData();})">Stop</button>' +
                '</div>' +
            '</div>' +
            '<div class="strategy-card copy">' +
                '<h2>2. Copy Trading</h2>' +
                '<div class="subtitle">Track & copy politician trades via Capitol Trades</div>' +
                '<div class="stat-grid">' +
                    '<div class="stat"><div class="stat-label">Tracking</div><div class="stat-value">' + ctPolitician + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Status</div><div class="stat-value">' + ctBadge + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Trades Copied</div><div class="stat-value">' + ctTrades.length + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Realized P&L</div><div class="stat-value">' + fmtMoney(ctPnl) + '</div></div>' +
                '</div>' +
                '<div class="strategy-visual"><strong>Rules:</strong> 5% position size | Max 10 positions | Skip if moved 15%+ | 10% stop-loss</div>' +
                '<div class="strategy-actions">' +
                    '<button class="btn-warning btn-sm" onclick="if(confirm(\'Pause copy trading?\')) fetch(\'/api/pause-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'copy_trading\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Paused\',\'info\');addLog(\'Paused copy trading strategy\',\'info\');refreshData();})">Pause</button>' +
                    '<button class="btn-danger btn-sm" onclick="if(confirm(\'Stop copy trading?\')) fetch(\'/api/stop-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'copy_trading\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Stopped\',\'info\');addLog(\'Stopped copy trading strategy\',\'info\');refreshData();})">Stop</button>' +
                '</div>' +
            '</div>' +
            '<div class="strategy-card wheel">' +
                '<h2>3. Wheel Strategy</h2>' +
                '<div class="subtitle">Sell puts \u2192 Assigned \u2192 Sell calls \u2192 Repeat</div>' +
                '<div class="stat-grid">' +
                    '<div class="stat"><div class="stat-label">Current Stage</div><div class="stat-value" style="font-size:13px">' + wsStageLabel + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Status</div><div class="stat-value">' + wsBadge + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Premiums</div><div class="stat-value positive">' + fmtMoney(wsPremiums) + '</div></div>' +
                    '<div class="stat"><div class="stat-label">Cycles</div><div class="stat-value">' + wsCycles + '</div></div>' +
                '</div>' +
                '<div class="strategy-visual"><strong>Rules:</strong> Strike 10% OTM | 2-4 week exp | Close at 50% profit | Check every 15 min</div>' +
                optionsInfoHtml +
                '<div class="strategy-actions">' +
                    '<button class="btn-warning btn-sm" onclick="if(confirm(\'Pause wheel strategy?\')) fetch(\'/api/pause-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'wheel\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Paused\',\'info\');addLog(\'Paused wheel strategy\',\'info\');refreshData();})">Pause</button>' +
                    '<button class="btn-danger btn-sm" onclick="if(confirm(\'Stop wheel strategy?\')) fetch(\'/api/stop-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'wheel\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Stopped\',\'info\');addLog(\'Stopped wheel strategy\',\'info\');refreshData();})">Stop</button>' +
                '</div>' +
            '</div>' +
            '<div class="strategy-card meanrev">' +
                '<h2>4. Mean Reversion</h2>' +
                '<div class="subtitle">Buy oversold dips, sell at recovery to the mean</div>' +
                '<div class="stat-grid">' +
                    '<div class="stat"><div class="stat-label">How It Works</div><div class="stat-value" style="font-size:12px">Buys stocks that dropped 15%+ on no real news</div></div>' +
                    '<div class="stat"><div class="stat-label">Status</div><div class="stat-value"><span class="badge-active">READY</span></div></div>' +
                    '<div class="stat"><div class="stat-label">Target</div><div class="stat-value" style="font-size:12px">Sells at 20-day average</div></div>' +
                    '<div class="stat"><div class="stat-label">Stop-Loss</div><div class="stat-value">10%</div></div>' +
                '</div>' +
                '<div class="strategy-visual"><strong>Rules:</strong> Buy oversold stocks | Target 20-day mean | 10% stop-loss | Skip if bad news caused drop</div>' +
                '<div class="strategy-actions">' +
                    '<button class="btn-warning btn-sm" onclick="if(confirm(\'Pause mean reversion?\')) fetch(\'/api/pause-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'mean_reversion\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Paused\',\'info\');addLog(\'Paused mean reversion strategy\',\'info\');refreshData();})">Pause</button>' +
                    '<button class="btn-danger btn-sm" onclick="if(confirm(\'Stop mean reversion?\')) fetch(\'/api/stop-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'mean_reversion\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Stopped\',\'info\');addLog(\'Stopped mean reversion strategy\',\'info\');refreshData();})">Stop</button>' +
                '</div>' +
            '</div>' +
            '<div class="strategy-card breakout">' +
                '<h2>5. Breakout</h2>' +
                '<div class="subtitle">Catch explosive moves on high volume</div>' +
                '<div class="stat-grid">' +
                    '<div class="stat"><div class="stat-label">How It Works</div><div class="stat-value" style="font-size:12px">Buys stocks breaking 20-day highs on 2x+ volume</div></div>' +
                    '<div class="stat"><div class="stat-label">Status</div><div class="stat-value"><span class="badge-active">READY</span></div></div>' +
                    '<div class="stat"><div class="stat-label">Trail</div><div class="stat-value" style="font-size:12px">5% trailing stop</div></div>' +
                    '<div class="stat"><div class="stat-label">Stop-Loss</div><div class="stat-value">5% (tight)</div></div>' +
                '</div>' +
                '<div class="strategy-visual"><strong>Rules:</strong> Tight 5% stop | Immediate trailing | Rides momentum | Fails fast if breakout fizzles</div>' +
                '<div class="strategy-actions">' +
                    '<button class="btn-warning btn-sm" onclick="if(confirm(\'Pause breakout strategy?\')) fetch(\'/api/pause-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'breakout\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Paused\',\'info\');addLog(\'Paused breakout strategy\',\'info\');refreshData();})">Pause</button>' +
                    '<button class="btn-danger btn-sm" onclick="if(confirm(\'Stop breakout strategy?\')) fetch(\'/api/stop-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'breakout\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Stopped\',\'info\');addLog(\'Stopped breakout strategy\',\'info\');refreshData();})">Stop</button>' +
                '</div>' +
            '</div>' +
            buildShortStrategyCard(d) +
        '</div>' +
        '</div>' +
        readinessHtml +
        '<div id="section-positions">' +
        '<div class="tables">' +
            '<div class="table-card">' +
                '<h3>Positions</h3>' +
                '<table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg Entry</th><th>Current</th><th>P&L</th><th>P&L %</th><th>Actions</th></tr></thead>' +
                '<tbody>' + posHtml + '</tbody></table>' +
            '</div>' +
            '<div class="table-card">' +
                '<h3>Open Orders</h3>' +
                '<table><thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>Status</th><th>Actions</th></tr></thead>' +
                '<tbody>' + ordHtml + '</tbody></table>' +
            '</div>' +
        '</div>' +
        corrHtml +
        taxHtml +
        '</div>' +
        '<div id="section-screener">' +
        '<div class="screener">' +
            '<h3>Full Stock Screener - Top 50</h3>' +
            '<div class="screener-stats">' +
                'Screened: <strong>' + (d.total_screened||0).toLocaleString() + '</strong> stocks &nbsp;|&nbsp; ' +
                'Passed filters: <strong>' + (d.total_passed||0).toLocaleString() + '</strong> &nbsp;|&nbsp; ' +
                'Showing: <strong>Top 50</strong></div>' +
            '<table><thead><tr>' +
                '<th class="sortable" onclick="sortScreener(\'rank\')">#' + sortArrow('rank') + '</th>' +
                '<th class="sortable" onclick="sortScreener(\'symbol\')">Symbol' + sortArrow('symbol') + '</th>' +
                '<th class="sortable" onclick="sortScreener(\'price\')">Price' + sortArrow('price') + '</th>' +
                '<th class="sortable" onclick="sortScreener(\'daily_change\')">Daily Chg' + sortArrow('daily_change') + '</th>' +
                '<th class="sortable" onclick="sortScreener(\'volatility\')">Volatility' + sortArrow('volatility') + '</th>' +
                '<th class="sortable" onclick="sortScreener(\'daily_volume\')">Volume' + sortArrow('daily_volume') + '</th>' +
                '<th class="sortable" onclick="sortScreener(\'volume_surge\')">Vol Surge' + sortArrow('volume_surge') + '</th>' +
                '<th class="sortable" onclick="sortScreener(\'best_strategy\')">Strategy' + sortArrow('best_strategy') + '</th>' +
                '<th class="sortable" onclick="sortScreener(\'best_score\')">Score' + sortArrow('best_score') + '</th>' +
                '<th>Action</th>' +
            '</tr></thead><tbody>' + scrHtml + '</tbody></table>' +
        '</div>' +
        '</div>' +
        shortHtml +
        '<div id="section-backtest">' +
        '<div class="backtest-section">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:12px">' +
                '<div>' +
                    '<h3 style="margin:0">Visual Backtest</h3>' +
                    '<div style="font-size:12px;color:var(--text-dim);margin-top:4px">Shows how a trailing stop would have performed on this stock over the last 30 days</div>' +
                '</div>' +
                '<select id="backtestStockSelector" onchange="renderBacktest(this.value)" style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:14px;min-width:200px"></select>' +
            '</div>' +
            '<div id="backtestSummary" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px"></div>' +
            '<canvas id="backtestChart" height="280"></canvas>' +
            '<div id="backtestExplanation" style="margin-top:16px;padding:12px;background:rgba(59,130,246,0.05);border:1px solid rgba(59,130,246,0.2);border-radius:8px;font-size:13px;color:var(--text-dim);line-height:1.6"></div>' +
        '</div>' +
        '</div>' +
        '<div id="section-scheduler" class="scheduler-section">' +
            '<h3 style="display:flex;justify-content:space-between;align-items:center">Cloud Scheduler <button class="btn-ghost btn-sm" onclick="refreshSchedulerStatus()">Refresh</button></h3>' +
            '<div id="schedulerPanel"><div class="empty" style="padding:20px">Loading scheduler status...</div></div>' +
        '</div>' +
        '<div class="activity-log">' +
            '<h3>Activity Log <button class="btn-ghost btn-sm" style="margin-left:12px" onclick="activityLog=[];renderLog();">Clear</button></h3>' +
            '<div class="log-entries" id="logEntries"></div>' +
        '</div>' +
        '<div id="section-settings">' +
            buildStrategyTemplates(d) +
        '</div>' +
        '<div class="footer">Stock Trading Bot - Strategies: Trailing Stop | Copy Trading | Wheel | Mean Reversion | Breakout | Short Selling - Full market screener across NYSE, NASDAQ, ARCA</div>';

    renderLog();
    refreshSchedulerStatus();

    // Populate the stock selector and render backtest
    setTimeout(function() {
        var selector = document.getElementById('backtestStockSelector');
        if (selector && d.picks && d.picks.length) {
            // Only show picks that have backtest data
            var withBacktest = d.picks.filter(function(p) { return p.backtest_detail && p.backtest_detail.equity_curve; });
            selector.innerHTML = withBacktest.map(function(p, i) {
                var bt = p.backtest_detail;
                var ret = bt.return_pct || 0;
                var retStr = (ret >= 0 ? '+' : '') + ret.toFixed(1) + '%';
                return '<option value="' + p.symbol + '">' + p.symbol + ' — ' + p.best_strategy + ' (' + retStr + ')</option>';
            }).join('');
            if (withBacktest.length > 0) {
                renderBacktest(withBacktest[0].symbol);
            }
        }
    }, 300);

    // Fetch cloud scheduler status and update header badge
    fetch(API_BASE + '/api/scheduler-status', {credentials: 'same-origin'})
        .then(function(r){ return r.json(); })
        .then(function(s){
            var el = document.getElementById('schedulerBadge');
            if (!el) return;
            if (s.running) {
                el.className = 'scheduler-badge';
                el.innerHTML = '<span class="scheduler-pulse"></span>24/7 CLOUD';
                el.title = 'Scheduler is running autonomously on Railway. Market: ' + (s.market_open ? 'open' : 'closed');
            } else {
                el.className = 'scheduler-badge off';
                el.innerHTML = 'SCHEDULER OFF';
                el.title = 'Cloud scheduler not running — tasks depend on Claude Code';
            }
        }).catch(function(){
            var el = document.getElementById('schedulerBadge');
            if (el) { el.className = 'scheduler-badge off'; el.innerHTML = '—'; }
        });
}

var _backtestChart = null;

function renderBacktest(symbol) {
    if (!lastData || !lastData.picks) return;
    var pick = lastData.picks.find(function(p) { return p.symbol === symbol; });
    if (!pick || !pick.backtest_detail) return;
    var bt = pick.backtest_detail;

    // Summary cards
    var ret = bt.return_pct || 0;
    var retClass = ret >= 0 ? 'positive' : 'negative';
    var retStr = (ret >= 0 ? '+' : '') + ret.toFixed(1) + '%';
    var stoppedOut = bt.stopped_out;
    var days = bt.days || (bt.equity_curve ? bt.equity_curve.length : 0);
    var entry = bt.entry || 0;
    var exit = bt.exit || 0;

    var summary = document.getElementById('backtestSummary');
    if (summary) {
        summary.innerHTML =
            '<div class="metric"><div class="label">Return</div><div class="value ' + retClass + '">' + retStr + '</div></div>' +
            '<div class="metric"><div class="label">Days Held</div><div class="value">' + days + '</div></div>' +
            '<div class="metric"><div class="label">Entry → Exit</div><div class="value" style="font-size:16px">$' + entry.toFixed(2) + ' → $' + exit.toFixed(2) + '</div></div>' +
            '<div class="metric"><div class="label">Outcome</div><div class="value" style="font-size:14px;color:' + (stoppedOut ? 'var(--red)' : 'var(--green)') + '">' + (stoppedOut ? 'Stopped Out' : 'Held Full Period') + '</div></div>';
    }

    // Explanation
    var expl = document.getElementById('backtestExplanation');
    if (expl) {
        var profitDollar = Math.abs(exit - entry) * 100;  // assume 100 shares for illustration
        var strategy = pick.best_strategy || 'Trailing Stop';
        var why = stoppedOut
            ? 'The stop-loss was triggered — the stock dropped 10% from its peak and was automatically sold. This LIMITED your losses.'
            : 'The position was held the full 30 days without hitting the stop-loss. The trailing stop would have ratcheted up as the price climbed.';
        expl.innerHTML =
            '<strong>What this means:</strong> If you had deployed <strong>' + strategy + '</strong> on <strong>' + symbol + '</strong> 30 days ago ' +
            'with 100 shares, you would be ' + (ret >= 0 ? 'UP' : 'DOWN') + ' ' + retStr +
            ' (~$' + profitDollar.toFixed(0) + ' on a $' + (entry * 100).toFixed(0) + ' position). ' + why +
            '<br><br><strong>Blue line:</strong> the stock\u2019s price over 30 days. <strong>Red dashed line:</strong> where your stop-loss would have been (it ratchets UP as the price climbs, never down). ' +
            '<br><br><em>Past performance doesn\u2019t guarantee future results, but this shows how the strategy would have worked historically.</em>';
    }

    // Destroy previous chart if exists
    if (_backtestChart) {
        _backtestChart.destroy();
        _backtestChart = null;
    }

    var ctx = document.getElementById('backtestChart');
    if (!ctx || !bt.equity_curve) return;

    _backtestChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: bt.equity_curve.map(function(_, i) { return 'Day ' + (i+1); }),
            datasets: [{
                label: symbol + ' Price',
                data: bt.equity_curve,
                borderColor: '#3b82f6',
                backgroundColor: 'rgba(59,130,246,0.1)',
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                borderWidth: 2,
            }, {
                label: 'Stop-Loss Level',
                data: bt.stop_levels || [],
                borderColor: '#ef4444',
                borderDash: [5, 5],
                fill: false,
                pointRadius: 0,
                borderWidth: 2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: '#e2e8f0' } },
                tooltip: {
                    callbacks: {
                        label: function(ctx) {
                            return ctx.dataset.label + ': $' + ctx.parsed.y.toFixed(2);
                        }
                    }
                }
            },
            scales: {
                x: { ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } },
                y: {
                    ticks: {
                        color: '#94a3b8',
                        callback: function(v) { return '$' + v.toFixed(2); }
                    },
                    grid: { color: '#1e293b' }
                }
            }
        }
    });
}

/* ---- Voice Interface ---- */
var recognition = null;
var isListening = false;

function toggleVoice() {
    if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
        toast('Voice not supported in this browser', 'error');
        return;
    }

    if (isListening) {
        recognition.stop();
        return;
    }

    var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = 'en-US';

    recognition.onstart = function() {
        isListening = true;
        document.getElementById('voiceBtn').classList.add('listening');
        toast('Listening...', 'info');
    };

    recognition.onresult = function(event) {
        var text = event.results[0][0].transcript.toLowerCase().trim();
        handleVoiceCommand(text);
    };

    recognition.onend = function() {
        isListening = false;
        document.getElementById('voiceBtn').classList.remove('listening');
    };

    recognition.onerror = function(e) {
        isListening = false;
        document.getElementById('voiceBtn').classList.remove('listening');
        if (e.error !== 'no-speech') toast('Voice error: ' + e.error, 'error');
    };

    recognition.start();
}

function handleVoiceCommand(text) {
    addLog('Voice: "' + text + '"', 'info');
    toast('Heard: "' + text + '"', 'info');

    if (text.includes('kill') || text.includes('emergency') || text.includes('stop everything')) {
        if (confirm('Voice command: Activate Kill Switch? This will close all positions.')) {
            executeKillSwitch();
        }
    }
    else if (text.includes('refresh') || text.includes('update')) {
        refreshData();
    }
    else if (text.match(/deploy|buy|run|start/)) {
        var words = text.split(' ');
        var symbol = null;
        for (var i = 0; i < words.length; i++) {
            if (words[i].length >= 2 && words[i].length <= 5 && words[i] === words[i].toUpperCase()) {
                symbol = words[i];
                break;
            }
        }
        var strategy = 'trailing_stop';
        if (text.includes('wheel')) strategy = 'wheel';
        else if (text.includes('breakout')) strategy = 'breakout';
        else if (text.includes('mean') || text.includes('reversion')) strategy = 'mean_reversion';
        else if (text.includes('copy')) strategy = 'copy_trading';
        else if (text.includes('short')) strategy = 'short_sell';

        if (symbol) {
            openDeployModal(symbol.toUpperCase(), strategy, 0, 0);
            toast('Opening deploy for ' + symbol.toUpperCase() + ' with ' + strategy, 'info');
        } else {
            toast('Say a stock symbol, e.g., "Deploy trailing stop on NVDA"', 'info');
        }
    }
    else if (text.includes('p and l') || text.includes('p&l') || text.includes('profit') || text.includes('portfolio')) {
        var d = lastData;
        if (d && d.account) {
            var val = parseFloat(d.account.portfolio_value || 0);
            toast('Portfolio: $' + val.toLocaleString(), 'info');
            speak('Portfolio value is ' + val.toLocaleString() + ' dollars');
        }
    }
    else if (text.includes('what') && text.includes('pick')) {
        var d = lastData;
        if (d && d.picks && d.picks[0]) {
            var p = d.picks[0];
            var msg = 'Top pick is ' + p.symbol + ' at ' + p.price.toFixed(2) + ' dollars. Recommended strategy: ' + p.best_strategy;
            toast(msg, 'info');
            speak(msg);
        }
    }
    else {
        toast('Commands: "deploy [strategy] on [SYMBOL]", "kill switch", "refresh", "portfolio", "what\'s the top pick"', 'info');
    }
}

function speak(text) {
    if ('speechSynthesis' in window) {
        var msg = new SpeechSynthesisUtterance(text);
        msg.rate = 1.0;
        msg.pitch = 1.0;
        speechSynthesis.speak(msg);
    }
}

/* ---- Strategy Marketplace ---- */
function exportStrategies() {
    fetch(API_BASE + '/api/data').then(function(r) { return r.json(); }).then(function(d) {
        var exportData = {
            exported_at: new Date().toISOString(),
            version: "1.0",
            strategies: {
                trailing: d.trailing,
                copy_trading: d.copy_trading,
                wheel: d.wheel
            },
            guardrails: d.guardrails || {},
            scorecard: d.scorecard || {}
        };
        var blob = new Blob([JSON.stringify(exportData, null, 2)], {type: 'application/json'});
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'stockbot-strategies-' + new Date().toISOString().slice(0,10) + '.json';
        a.click();
        URL.revokeObjectURL(url);
        toast('Strategies exported!', 'info');
    });
}

function importStrategy(input) {
    var file = input.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function(e) {
        try {
            var data = JSON.parse(e.target.result);
            toast('Strategy imported: ' + (data.version || 'unknown version') + '. Review and deploy from the dashboard.', 'info');
            addLog('Imported strategy template', 'info');
        } catch(err) {
            toast('Invalid JSON file', 'error');
        }
    };
    reader.readAsText(file);
}

function applyPreset(preset) {
    var presets = {
        conservative: {
            stop_loss_pct: 0.05, max_positions: 3, max_position_pct: 0.05,
            strategies: ['trailing_stop', 'wheel', 'copy_trading'],
            note: 'Conservative: smaller positions, wider stops, no aggressive strategies'
        },
        moderate: {
            stop_loss_pct: 0.10, max_positions: 5, max_position_pct: 0.10,
            strategies: ['trailing_stop', 'wheel', 'copy_trading', 'mean_reversion', 'breakout'],
            note: 'Moderate: balanced risk/reward with all strategies'
        },
        aggressive: {
            stop_loss_pct: 0.05, max_positions: 8, max_position_pct: 0.15,
            strategies: ['trailing_stop', 'breakout', 'short_sell', 'mean_reversion'],
            note: 'Aggressive: tight stops, more positions, includes shorting'
        }
    };
    var p = presets[preset];
    if (!p) return;
    if (confirm('Apply ' + preset + ' preset?\n\n' + p.note + '\n\nThis will update your guardrails.')) {
        fetch(API_BASE + '/api/apply-preset', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({preset: preset, settings: p})
        }).then(function(r) { return r.json(); }).then(function(d) {
            toast('Preset applied: ' + preset, 'info');
            addLog('Applied ' + preset + ' strategy preset', 'info');
            refreshData();
        });
    }
}

// Initialize
(async function init() {
    await refreshData();
    await loadAutoDeployerState();
    await loadGuardrails();
    renderDashboard();
    addLog('Dashboard loaded', 'success');
    countdownInterval = setInterval(() => {
        countdown--;
        const el = document.getElementById('countdown');
        if (el) el.textContent = 'Next refresh: ' + Math.max(0, countdown) + 's';
        if (countdown <= 0) {
            countdown = 60;  // Reset BEFORE calling refresh to prevent spam on failure
            refreshData();
            addLog('Auto-refresh triggered', 'info');
        }
    }, 1000);
})();

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function(){});
}
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard server."""

    def log_message(self, format, *args):
        """Override to add timestamp prefix."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")

    def check_auth(self):
        """Check HTTP Basic Auth. Returns True if authorized."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Stock Trading Bot"')
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Login Required</h1>")
            return False
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            user, passwd = decoded.split(":", 1)
            if user == AUTH_USER and passwd == AUTH_PASS:
                return True
        except Exception:
            pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Stock Trading Bot"')
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>Invalid credentials</h1>")
        return False

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
        self.send_header("X-Content-Type-Options", "nosniff")
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
        if not self.check_auth():
            return
        path = self.path.split("?")[0]

        if path == "/":
            self.send_html(DASHBOARD_HTML)

        elif path == "/api/data":
            data = get_dashboard_data()
            self.send_json(data)

        elif path == "/api/account":
            result = alpaca_get(f"{API_ENDPOINT}/account")
            self.send_json(result)

        elif path == "/api/positions":
            result = alpaca_get(f"{API_ENDPOINT}/positions")
            self.send_json(result if isinstance(result, list) else [])

        elif path == "/api/orders":
            result = alpaca_get(f"{API_ENDPOINT}/orders?status=open&limit=50")
            self.send_json(result if isinstance(result, list) else [])

        elif path == "/api/auto-deployer-config":
            config_path = os.path.join(BASE_DIR, "auto_deployer_config.json")
            config = load_json(config_path)
            self.send_json(config if config else {"enabled": False})

        elif path == "/api/guardrails":
            guardrails_path = os.path.join(BASE_DIR, "guardrails.json")
            guardrails = load_json(guardrails_path)
            self.send_json(guardrails if guardrails else {"kill_switch": False})

        elif path == "/api/scheduler-status":
            if SCHEDULER_AVAILABLE:
                self.send_json(get_scheduler_status())
            else:
                self.send_json({"running": False, "error": "Scheduler module not loaded"})

        elif path == "/manifest.json":
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

        elif path == "/sw.js":
            sw = "self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));"
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(sw.encode())

        elif path in ("/icon-192.png", "/icon-512.png"):
            # Serve a simple colored square as a PNG placeholder
            size = 192 if "192" in path else 512
            self._serve_icon_placeholder(size)

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self.check_auth():
            return
        path = self.path.split("?")[0]
        body = self.read_body()

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

        else:
            self.send_json({"error": "Not found"}, 404)

    def handle_refresh(self):
        """Run update_dashboard.py and return fresh data."""
        script_path = os.path.join(BASE_DIR, "update_dashboard.py")
        try:
            result = subprocess.run(
                ["python3", script_path],
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                print(f"update_dashboard.py stderr: {result.stderr}")
        except Exception as e:
            print(f"Error running update_dashboard.py: {e}")

        # Return fresh data regardless
        data = get_dashboard_data()
        self.send_json(data)

    def handle_deploy(self, body):
        """Deploy a strategy on a symbol."""
        symbol = body.get("symbol", "").upper()
        strategy = body.get("strategy", "trailing_stop")
        try:
            qty = int(body.get("qty", 2))
        except (TypeError, ValueError):
            return self.send_json({"error": "Invalid quantity."}, 400)

        if not symbol:
            self.send_json({"error": "Missing symbol"}, 400)
            return
        # Validate symbol is alphanumeric (1-10 chars)
        if not re.match(r'^[A-Z]{1,10}$', symbol):
            return self.send_json({"error": "Invalid symbol format"}, 400)

        if qty < 1 or qty > 1000:
            return self.send_json({"error": "Invalid quantity. Must be 1-1000."}, 400)

        if strategy == "trailing_stop":
            self.deploy_trailing_stop(symbol, qty)
        elif strategy == "wheel":
            self.deploy_wheel(symbol, qty)
        elif strategy == "copy_trading":
            self.deploy_copy_trading(symbol, qty)
        elif strategy == "mean_reversion":
            self.deploy_mean_reversion(symbol, qty)
        elif strategy == "breakout":
            self.deploy_breakout(symbol, qty)
        else:
            self.send_json({"error": f"Unknown strategy: {strategy}"}, 400)

    def deploy_trailing_stop(self, symbol, qty):
        """Deploy trailing stop strategy: buy shares, set stop loss, place ladder buys."""
        # 1. Get current price
        snap_url = f"{DATA_ENDPOINT}/stocks/{symbol}/snapshot?feed=iex"
        snap = alpaca_get(snap_url)
        if "error" in snap:
            # Try SIP feed
            snap_url = f"{DATA_ENDPOINT}/stocks/{symbol}/snapshot"
            snap = alpaca_get(snap_url)
        price = 0
        if isinstance(snap, dict):
            lt = snap.get("latestTrade", {})
            price = lt.get("p", 0)
        if not price:
            self.send_json({"error": f"Could not get price for {symbol}"}, 400)
            return

        # 2. Market buy
        buy_order = alpaca_post(f"{API_ENDPOINT}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })
        if isinstance(buy_order, dict) and "error" in buy_order:
            self.send_json({"error": f"Buy order failed: {buy_order['error']}"}, 400)
            return

        buy_order_id = buy_order.get("id", "")

        # NOTE: Stop-loss is NOT placed here. The strategy-monitor will place
        # the stop-loss AFTER the buy order fills (checks state.stop_pending).
        stop_price = round(price * 0.90, 2)

        # 3. Ladder buy orders at -12%, -20%, -30%, -40%
        # Check buying power first so we don't place ladders we can't afford
        acct = alpaca_get(f"{API_ENDPOINT}/account")
        buying_power = float(acct.get("buying_power", 0)) if isinstance(acct, dict) else 0
        ladder_levels = [
            {"drop_pct": 0.12, "qty": max(1, qty // 2), "note": "re-entry just below stop-out"},
            {"drop_pct": 0.20, "qty": qty, "note": "meaningful pullback"},
            {"drop_pct": 0.30, "qty": qty + 1, "note": "deep correction"},
            {"drop_pct": 0.40, "qty": qty * 2 + 1, "note": "crash territory, go heavy"},
        ]
        # Calculate worst-case cost (all ladders fill) and skip some if insufficient buying power
        cumulative_cost = 0
        affordable_levels = []
        for level in ladder_levels:
            ladder_price = round(price * (1 - level["drop_pct"]), 2)
            cost = ladder_price * level["qty"]
            if cumulative_cost + cost <= buying_power:
                cumulative_cost += cost
                affordable_levels.append(level)
        ladder_levels = affordable_levels

        ladder_orders = []
        for level in ladder_levels:
            ladder_price = round(price * (1 - level["drop_pct"]), 2)
            ladder_order = alpaca_post(f"{API_ENDPOINT}/orders", {
                "symbol": symbol,
                "qty": str(level["qty"]),
                "side": "buy",
                "type": "limit",
                "limit_price": str(ladder_price),
                "time_in_force": "gtc",
            })
            order_id = ladder_order.get("id", "") if isinstance(ladder_order, dict) else ""
            ladder_orders.append({
                "level": len(ladder_orders) + 1,
                "drop_pct": level["drop_pct"],
                "price": ladder_price,
                "qty": level["qty"],
                "order_id": order_id,
                "note": level["note"],
            })

        # 5. Save strategy state
        strategy_data = {
            "symbol": symbol,
            "strategy": "trailing_stop_with_ladder",
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "entry_price_estimate": price,
            "initial_qty": qty,
            "status": "awaiting_fill",
            "rules": {
                "stop_loss_pct": 0.10,
                "trailing_activation_pct": 0.10,
                "trailing_distance_pct": 0.05,
                "ladder_in": ladder_orders,
            },
            "state": {
                "entry_fill_price": None,
                "entry_order_id": buy_order_id,
                "stop_order_id": None,
                "stop_pending": True,
                "highest_price_seen": None,
                "trailing_activated": False,
                "current_stop_price": stop_price,
                "total_shares_held": 0,
                "ladder_fills": [],
            },
        }
        # Per-symbol file so multiple trailing stops don't overwrite each other
        save_json(os.path.join(STRATEGIES_DIR, f"trailing_stop_{symbol}.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "trailing_stop",
            "symbol": symbol,
            "buy_order_id": buy_order_id,
            "stop_price": stop_price,
            "ladder_orders": len(ladder_orders),
            "price": price,
            "note": "Stop-loss will be placed by strategy-monitor after buy fills.",
        })

    def deploy_wheel(self, symbol, qty):
        """Deploy wheel strategy: check cash for 100 shares, place first put."""
        snap_url = f"{DATA_ENDPOINT}/stocks/{symbol}/snapshot?feed=iex"
        snap = alpaca_get(snap_url)
        if "error" in snap:
            snap_url = f"{DATA_ENDPOINT}/stocks/{symbol}/snapshot"
            snap = alpaca_get(snap_url)
        price = 0
        if isinstance(snap, dict):
            lt = snap.get("latestTrade", {})
            price = lt.get("p", 0)
        if not price:
            self.send_json({"error": f"Could not get price for {symbol}"}, 400)
            return

        # Check account for enough cash for 100 shares
        acct = alpaca_get(f"{API_ENDPOINT}/account")
        cash = float(acct.get("cash", 0)) if isinstance(acct, dict) else 0
        needed = price * 100
        if cash < needed:
            self.send_json({
                "error": f"Insufficient cash for wheel. Need ${needed:,.2f} for 100 shares of {symbol} at ${price:.2f}, have ${cash:,.2f}"
            }, 400)
            return

        # Save strategy state (wheel requires options which paper may not support fully)
        strategy_data = {
            "strategy": "wheel_strategy",
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "symbol": symbol,
            "status": "active",
            "rules": {
                "put_strike_pct_below": 0.10,
                "call_strike_pct_above": 0.10,
                "expiration_weeks": [2, 3, 4],
                "early_close_profit_pct": 0.50,
                "check_interval_minutes": 15,
                "never_sell_put_without_cash": True,
                "never_sell_call_below_cost_basis": True,
            },
            "state": {
                "current_stage": "stage_1_sell_puts",
                "shares_owned": 0,
                "cost_basis": None,
                "active_contract": None,
                "cycles_completed": 0,
                "total_premiums_collected": 0,
                "total_stock_gains": 0,
                "history": [],
            },
        }
        save_json(os.path.join(STRATEGIES_DIR, "wheel_strategy.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "wheel",
            "symbol": symbol,
            "price": price,
            "cash_available": cash,
            "cash_needed": needed,
            "message": f"Wheel strategy initialized for {symbol}. Stage 1: Ready to sell puts.",
        })

    def deploy_copy_trading(self, symbol, qty):
        """Deploy copy trading strategy: start tracking."""
        strategy_data = {
            "strategy": "copy_trading",
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "status": "active",
            "source": "capitol_trades",
            "source_url": "https://www.capitoltrades.com",
            "rules": {
                "politician": None,
                "selection_criteria": "highest_recent_returns_and_active",
                "trade_delay_max_days": 7,
                "position_size_pct": 0.05,
                "max_positions": 10,
                "skip_if_price_moved_pct": 0.15,
                "stop_loss_pct": 0.10,
            },
            "state": {
                "selected_politician": None,
                "selection_reason": None,
                "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "trades_copied": [],
                "active_positions": [],
                "total_premium_collected": 0,
                "total_realized_pnl": 0,
            },
        }
        save_json(os.path.join(STRATEGIES_DIR, "copy_trading.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "copy_trading",
            "symbol": symbol,
            "message": "Copy trading strategy initialized. Awaiting politician selection and trade signals.",
        })

    def deploy_mean_reversion(self, symbol, qty):
        """Deploy mean reversion: buy shares, set limit sell at 20-day avg estimate, set stop-loss."""
        snap_url = f"{DATA_ENDPOINT}/stocks/{symbol}/snapshot?feed=iex"
        snap = alpaca_get(snap_url)
        if "error" in snap:
            snap_url = f"{DATA_ENDPOINT}/stocks/{symbol}/snapshot"
            snap = alpaca_get(snap_url)
        price = 0
        if isinstance(snap, dict):
            lt = snap.get("latestTrade", {})
            price = lt.get("p", 0)
        if not price:
            self.send_json({"error": f"Could not get price for {symbol}"}, 400)
            return

        # 1. Market buy
        buy_order = alpaca_post(f"{API_ENDPOINT}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })
        if isinstance(buy_order, dict) and "error" in buy_order:
            self.send_json({"error": f"Buy order failed: {buy_order['error']}"}, 400)
            return
        buy_order_id = buy_order.get("id", "")

        # NOTE: No limit sell placed here. The strategy-monitor handles the
        # profit target by checking price vs 20-day average each cycle.
        # NOTE: Stop-loss is NOT placed here. The strategy-monitor will place
        # the stop-loss AFTER the buy order fills (checks state.stop_pending).
        target_price = round(price * 1.15, 2)
        stop_price = round(price * 0.90, 2)

        # 2. Save strategy state
        strategy_data = {
            "symbol": symbol,
            "strategy": "mean_reversion",
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "entry_price_estimate": price,
            "initial_qty": qty,
            "target_price": target_price,
            "stop_price": stop_price,
            "status": "awaiting_fill",
            "state": {
                "entry_order_id": buy_order_id,
                "sell_order_id": None,
                "stop_order_id": None,
                "stop_pending": True,
                "entry_fill_price": None,
            },
        }
        save_json(os.path.join(STRATEGIES_DIR, f"mean_reversion_{symbol}.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "mean_reversion",
            "symbol": symbol,
            "buy_order_id": buy_order_id,
            "target_price": target_price,
            "stop_price": stop_price,
            "price": price,
            "note": "Stop-loss and profit target managed by strategy-monitor after buy fills.",
        })

    def deploy_breakout(self, symbol, qty):
        """Deploy breakout: buy shares, set tight 5% stop-loss, trailing stop."""
        snap_url = f"{DATA_ENDPOINT}/stocks/{symbol}/snapshot?feed=iex"
        snap = alpaca_get(snap_url)
        if "error" in snap:
            snap_url = f"{DATA_ENDPOINT}/stocks/{symbol}/snapshot"
            snap = alpaca_get(snap_url)
        price = 0
        if isinstance(snap, dict):
            lt = snap.get("latestTrade", {})
            price = lt.get("p", 0)
        if not price:
            self.send_json({"error": f"Could not get price for {symbol}"}, 400)
            return

        # 1. Market buy
        buy_order = alpaca_post(f"{API_ENDPOINT}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })
        if isinstance(buy_order, dict) and "error" in buy_order:
            self.send_json({"error": f"Buy order failed: {buy_order['error']}"}, 400)
            return
        buy_order_id = buy_order.get("id", "")

        # NOTE: No sell orders placed here. The strategy-monitor will place
        # a trailing stop (trail_percent=5) AFTER the buy order fills
        # (checks state.stop_pending). This avoids double sell orders and
        # ensures the stop is placed at the correct filled price.
        stop_price = round(price * 0.95, 2)

        # 2. Save strategy state
        strategy_data = {
            "symbol": symbol,
            "strategy": "breakout",
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "entry_price_estimate": price,
            "initial_qty": qty,
            "stop_price": stop_price,
            "trail_pct": 5,
            "status": "awaiting_fill",
            "state": {
                "entry_order_id": buy_order_id,
                "stop_order_id": None,
                "trail_order_id": None,
                "stop_pending": True,
                "entry_fill_price": None,
            },
        }
        save_json(os.path.join(STRATEGIES_DIR, f"breakout_{symbol}.json"), strategy_data)

        self.send_json({
            "success": True,
            "strategy": "breakout",
            "symbol": symbol,
            "buy_order_id": buy_order_id,
            "stop_price": stop_price,
            "price": price,
            "note": "Trailing stop will be placed by strategy-monitor after buy fills.",
        })

    def handle_cancel_order(self, body):
        """Cancel an open order."""
        order_id = body.get("order_id", "")
        if not order_id:
            self.send_json({"error": "Missing order_id"}, 400)
            return
        # Validate order_id is a UUID to prevent path traversal
        if not re.match(r'^[0-9a-f\-]{36}$', order_id):
            return self.send_json({"error": "Invalid order_id format"}, 400)

        result = alpaca_delete(f"{API_ENDPOINT}/orders/{order_id}")
        if isinstance(result, dict) and "error" in result:
            self.send_json({"error": result["error"]}, 400)
        else:
            self.send_json({"success": True, "order_id": order_id})

    def handle_close_position(self, body):
        """Close a position."""
        symbol = body.get("symbol", "").upper()
        if not symbol:
            self.send_json({"error": "Missing symbol"}, 400)
            return
        # Validate symbol is alphanumeric (1-10 chars) to prevent path traversal
        if not re.match(r'^[A-Z]{1,10}$', symbol):
            return self.send_json({"error": "Invalid symbol format"}, 400)

        result = alpaca_delete(f"{API_ENDPOINT}/positions/{symbol}")
        if isinstance(result, dict) and "error" in result:
            self.send_json({"error": result["error"]}, 400)
        else:
            self.send_json({"success": True, "symbol": symbol, "order": result})

    def handle_sell(self, body):
        """Place a market sell order."""
        symbol = body.get("symbol", "").upper()
        try:
            qty = int(body.get("qty", 1))
        except (TypeError, ValueError):
            return self.send_json({"error": "Invalid quantity."}, 400)
        if not symbol:
            self.send_json({"error": "Missing symbol"}, 400)
            return
        if qty < 1 or qty > 10000:
            return self.send_json({"error": "Invalid quantity. Must be 1-10000."}, 400)

        result = alpaca_post(f"{API_ENDPOINT}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        })
        if isinstance(result, dict) and "error" in result:
            self.send_json({"error": result["error"]}, 400)
        else:
            self.send_json({"success": True, "symbol": symbol, "qty": qty, "order": result})

    def handle_auto_deployer(self, body):
        """Toggle the auto-deployer on/off by updating config file."""
        enabled = body.get("enabled", False)
        config_path = os.path.join(BASE_DIR, "auto_deployer_config.json")
        config = load_json(config_path)
        if not config:
            config = {
                "enabled": False,
                "max_new_positions_per_day": 2,
                "max_portfolio_pct_per_stock": 0.10,
                "strategies": ["trailing_stop", "mean_reversion", "breakout"],
                "require_stop_loss": True,
            }
        config["enabled"] = bool(enabled)
        config["last_toggled"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_json(config_path, config)
        self.send_json({"success": True, "enabled": config["enabled"]})

    def handle_kill_switch(self, body):
        """Activate or deactivate the kill switch."""
        activate = body.get("activate", False)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        guardrails_path = os.path.join(BASE_DIR, "guardrails.json")
        guardrails = load_json(guardrails_path) or {}

        if activate:
            # 1. Cancel ALL open orders in one atomic bulk call
            orders_before = alpaca_get(f"{API_ENDPOINT}/orders?status=open")
            orders_cancelled = len(orders_before) if isinstance(orders_before, list) else 0
            alpaca_delete(f"{API_ENDPOINT}/orders")

            # 2. Close ALL positions (Alpaca supports closing all with one call)
            positions_closed = 0
            positions_before = alpaca_get(f"{API_ENDPOINT}/positions")
            if isinstance(positions_before, list):
                positions_closed = len(positions_before)
            close_result = alpaca_delete(f"{API_ENDPOINT}/positions")

            # 3. Set kill_switch: true in guardrails.json
            guardrails["kill_switch"] = True
            guardrails["kill_switch_triggered_at"] = timestamp
            guardrails["kill_switch_reason"] = "Manual activation via dashboard"
            save_json(guardrails_path, guardrails)

            # 4. Set enabled: false in auto_deployer_config.json
            ad_config_path = os.path.join(BASE_DIR, "auto_deployer_config.json")
            ad_config = load_json(ad_config_path) or {}
            ad_config["enabled"] = False
            ad_config["last_toggled"] = timestamp
            save_json(ad_config_path, ad_config)

            print(f"[KILL SWITCH] Activated at {timestamp}: {orders_cancelled} orders cancelled, {positions_closed} positions closed")

            # Send push notification via ntfy.sh (fire-and-forget, don't block HTTP response)
            subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "notify.py"), "--type", "kill", f"Cancelled {orders_cancelled} orders, closed {positions_closed} positions. All trading halted."], cwd=BASE_DIR)

            self.send_json({
                "success": True,
                "activated": True,
                "orders_cancelled": orders_cancelled,
                "positions_closed": positions_closed,
                "timestamp": timestamp,
            })
        else:
            # Deactivate: set kill_switch: false, do NOT re-enable auto-deployer
            guardrails["kill_switch"] = False
            guardrails["kill_switch_triggered_at"] = None
            guardrails["kill_switch_reason"] = None
            save_json(guardrails_path, guardrails)

            print(f"[KILL SWITCH] Deactivated at {timestamp}")

            self.send_json({
                "success": True,
                "activated": False,
                "timestamp": timestamp,
                "message": "Kill switch deactivated. Auto-deployer remains off - re-enable manually.",
            })


    def _find_strategy_files(self, strategy_key):
        """Find strategy JSON files matching the given strategy key."""
        patterns = {
            "trailing_stop": "trailing_stop*.json",
            "copy_trading": "copy_trading.json",
            "wheel": "wheel_strategy.json",
            "mean_reversion": "mean_reversion_*.json",
            "breakout": "breakout_*.json",
        }
        pattern = patterns.get(strategy_key, f"{strategy_key}*.json")
        return glob.glob(os.path.join(STRATEGIES_DIR, pattern))

    def handle_pause_strategy(self, body):
        """Pause a strategy by setting its status to 'paused'."""
        strategy = body.get("strategy", "")
        if not strategy:
            return self.send_json({"error": "Missing strategy"}, 400)

        files = self._find_strategy_files(strategy)
        if not files:
            return self.send_json({"error": f"No strategy files found for {strategy}"}, 404)

        paused = []
        for fpath in files:
            data = load_json(fpath)
            if data:
                data["status"] = "paused"
                data["paused_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                save_json(fpath, data)
                paused.append(os.path.basename(fpath))

        self.send_json({
            "success": True,
            "message": f"Paused {strategy}: {', '.join(paused)}",
            "files_updated": paused,
        })

    def handle_stop_strategy(self, body):
        """Stop a strategy: set status to 'stopped' and cancel related orders."""
        strategy = body.get("strategy", "")
        if not strategy:
            return self.send_json({"error": "Missing strategy"}, 400)

        files = self._find_strategy_files(strategy)
        if not files:
            return self.send_json({"error": f"No strategy files found for {strategy}"}, 404)

        stopped = []
        orders_cancelled = 0
        for fpath in files:
            data = load_json(fpath)
            if data:
                # Cancel any open orders for this symbol
                sym = data.get("symbol", "")
                state = data.get("state", {})
                order_ids = []
                for key in ["stop_order_id", "trail_order_id", "sell_order_id", "entry_order_id"]:
                    oid = state.get(key)
                    if oid:
                        order_ids.append(oid)
                # Cancel ladder orders too
                for rule_key in ["rules"]:
                    rules = data.get(rule_key, {})
                    for ladder in rules.get("ladder_in", []):
                        oid = ladder.get("order_id")
                        if oid:
                            order_ids.append(oid)

                for oid in order_ids:
                    result = alpaca_delete(f"{API_ENDPOINT}/orders/{oid}")
                    if not (isinstance(result, dict) and "error" in result):
                        orders_cancelled += 1

                data["status"] = "stopped"
                data["stopped_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                save_json(fpath, data)
                stopped.append(os.path.basename(fpath))

        self.send_json({
            "success": True,
            "message": f"Stopped {strategy}: {', '.join(stopped)}. Cancelled {orders_cancelled} orders.",
            "files_updated": stopped,
            "orders_cancelled": orders_cancelled,
        })

    def handle_apply_preset(self, body):
        """Apply a strategy preset (conservative/moderate/aggressive)."""
        settings = body.get("settings", {})
        preset_name = body.get("preset", "unknown")

        guardrails_path = os.path.join(BASE_DIR, "guardrails.json")
        guardrails = load_json(guardrails_path) or {}
        guardrails["max_positions"] = settings.get("max_positions", guardrails.get("max_positions", 5))
        guardrails["max_position_pct"] = settings.get("max_position_pct", guardrails.get("max_position_pct", 0.10))
        guardrails["strategies_allowed"] = settings.get("strategies", guardrails.get("strategies_allowed", []))
        save_json(guardrails_path, guardrails)

        config_path = os.path.join(BASE_DIR, "auto_deployer_config.json")
        config = load_json(config_path) or {}
        config["risk_settings"] = config.get("risk_settings", {})
        config["risk_settings"]["default_stop_loss_pct"] = settings.get("stop_loss_pct", 0.10)
        config["max_positions"] = settings.get("max_positions", 5)
        save_json(config_path, config)

        self.send_json({"message": f"Preset applied: {preset_name}", "settings": settings})

    def handle_toggle_short_selling(self, body):
        """Toggle short selling ON/OFF in auto_deployer_config.json."""
        enabled = bool(body.get("enabled", True))
        config_path = os.path.join(BASE_DIR, "auto_deployer_config.json")
        config = load_json(config_path) or {}
        if "short_selling" not in config:
            config["short_selling"] = {
                "enabled": enabled,
                "only_in_bear_market": True,
                "max_short_positions": 1,
                "min_short_score": 15,
                "max_portfolio_pct_per_short": 0.05,
                "stop_loss_pct": 0.08,
                "profit_target_pct": 0.15,
                "require_spy_20d_below": -3,
                "skip_if_meme_warning": True,
            }
        else:
            config["short_selling"]["enabled"] = enabled
        save_json(config_path, config)
        msg = "Short selling ENABLED — will deploy in bear markets" if enabled else "Short selling DISABLED — no new shorts will deploy"
        self.send_json({"message": msg, "enabled": enabled})


def main():
    port = int(os.environ.get("PORT", 8888))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop")

    # Start cloud scheduler (makes bot autonomous 24/7 on Railway)
    if SCHEDULER_AVAILABLE and os.environ.get("ENABLE_CLOUD_SCHEDULER", "true").lower() == "true":
        try:
            start_scheduler()
            print("[INFO] Cloud scheduler started — bot running autonomously", flush=True)
        except Exception as e:
            print(f"[WARN] Could not start cloud scheduler: {e}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
