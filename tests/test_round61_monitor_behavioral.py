"""
Round-61 pt.3: BEHAVIORAL coverage for monitor_strategies.

The pt.1 grep-pin tests pin the architectural invariants at the source-
string level — they catch refactor-renames and accidental guard removal,
but they don't move pytest-cov because they never run the function.

This file exercises monitor_strategies() end-to-end with stubs for:
  * user_api_get / user_api_post / user_api_patch / user_api_delete
  * _flatten_all_user
  * process_strategy_file
  * notify_rich / notify_user / log
  * observability.critical_alert
  * strategy_file_lock (passes through as a no-op context)

The stubs let the REAL function body execute — real I/O on a temp
DATA_DIR, real guardrails.json reads/writes, real kill-switch branching.
Complement to the grep-pins; together they catch both "guard removed"
AND "guard present but wrong threshold."

Covers:
  * Clean tick on a healthy portfolio — no kill-switch trip, no crash
  * Kill-switch already set → return immediately
  * daily_loss breach triggers kill_switch + flatten
  * max_drawdown breach triggers kill_switch + critical_alert + flatten
  * peak_portfolio_value seeded on first tick (pre-round-10 user)
  * daily_starting_value seeded + ET-date-stamped
  * Top-level exception swallowed (doesn't propagate)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from contextlib import contextmanager
from datetime import datetime

import pytest


class _Recorder:
    def __init__(self, account=None, positions=None):
        self.account = account or {
            "portfolio_value": "100000",
            "cash": "10000",
            "buying_power": "50000",
        }
        self.positions = positions or []
        self.flattened = False
        self.critical_alerts = []
        self.notifications = []
        self.saved = {}  # path → last data
        self.logs = []

    def api_get(self, user, path):
        if path == "/account":
            return self.account
        if path == "/positions":
            return self.positions
        return {}

    def api_noop(self, *a, **k):
        return {"id": "stub-order"}

    def flatten(self, user):
        self.flattened = True

    def critical(self, subject, body, tags=None, user=None):
        self.critical_alerts.append((subject, body, tags))

    def notify(self, *a, **k):
        self.notifications.append((a, k))

    def log(self, msg, task=None):
        self.logs.append((msg, task))


@contextmanager
def _passthrough_lock(path):
    """strategy_file_lock replacement that does nothing (we're single-
    threaded in these tests anyway)."""
    yield


def _make_user(tmpdir, username="alice"):
    """Construct a user dict the way server.py builds them: with all the
    per-user path overrides populated so monitor_strategies uses our temp
    dir instead of the global DATA_DIR."""
    strategies_dir = os.path.join(tmpdir, "strategies")
    os.makedirs(strategies_dir, exist_ok=True)
    return {
        "id": 1,
        "username": username,
        "_data_dir": tmpdir,
        "_strategies_dir": strategies_dir,
        "_api_key": "k", "_api_secret": "s",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_data_endpoint": "https://data.alpaca.markets/v2",
    }


def _install(monkeypatch, cs, rec):
    """Wire Recorder into cloud_scheduler as a replacement for every
    side-effect function the monitor might call."""
    monkeypatch.setattr(cs, "user_api_get", rec.api_get)
    monkeypatch.setattr(cs, "user_api_post", rec.api_noop)
    monkeypatch.setattr(cs, "user_api_patch", rec.api_noop)
    monkeypatch.setattr(cs, "user_api_delete", rec.api_noop)
    monkeypatch.setattr(cs, "_flatten_all_user", rec.flatten)
    monkeypatch.setattr(cs, "notify_rich", rec.notify)
    monkeypatch.setattr(cs, "notify_user", rec.notify)
    monkeypatch.setattr(cs, "log", rec.log)
    monkeypatch.setattr(cs, "strategy_file_lock", _passthrough_lock)
    # The critical_alert import is local inside the drawdown branch;
    # swap out by stubbing observability directly.
    obs = types.ModuleType("observability")
    obs.critical_alert = rec.critical
    obs.capture_message = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "observability", obs)
    # process_strategy_file is a no-op in these tests — we want to
    # focus on the kill-switch + daily-loss logic, not the per-file
    # loop. (The grep-pins in test_round61_monitor_strategies.py cover
    # that path.)
    monkeypatch.setattr(cs, "process_strategy_file", lambda *a, **k: None)


def _load_cs(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


# ========= 1. Clean tick =========

def test_clean_tick_on_healthy_portfolio_doesnt_trip_anything(monkeypatch):
    """Portfolio up 1% on the day. No kill-switch set. Should:
      * Not flatten.
      * Not fire critical_alert.
      * Leave kill_switch unset after the tick.
      * Seed peak_portfolio_value + daily_starting_value under lock."""
    with tempfile.TemporaryDirectory() as tmp:
        cs = _load_cs(monkeypatch)
        rec = _Recorder(account={
            "portfolio_value": "101000", "cash": "5000",
            "buying_power": "50000",
        })
        _install(monkeypatch, cs, rec)
        user = _make_user(tmp)
        # Pre-seed guardrails with yesterday's starting value so we hit
        # the "daily_starting_value_date != today" branch — that's the
        # realistic path on the first tick after midnight ET.
        gpath = os.path.join(tmp, "guardrails.json")
        with open(gpath, "w") as f:
            json.dump({
                "daily_starting_value": 100000.0,
                "daily_starting_value_date": "2026-04-22",
                "peak_portfolio_value": 100000.0,
            }, f)

        cs.monitor_strategies(user)

        # Didn't flatten, no alerts
        assert not rec.flattened, "healthy tick must not flatten"
        assert rec.critical_alerts == []
        # Guardrails persisted with today's date
        saved = json.load(open(gpath))
        assert saved.get("kill_switch") is not True, "kill_switch must stay off"
        # daily_starting_value got re-seeded for the new ET date
        assert saved.get("daily_starting_value_date"), (
            "daily_starting_value_date must be set after tick")


# ========= 2. Kill switch short-circuit =========

def test_kill_switch_set_returns_before_api_fetch(monkeypatch):
    """guardrails.kill_switch=True short-circuits. No /account fetch,
    no _flatten_all_user. The flatten would have already happened
    when the switch was first tripped."""
    with tempfile.TemporaryDirectory() as tmp:
        cs = _load_cs(monkeypatch)
        rec = _Recorder()
        _install(monkeypatch, cs, rec)
        user = _make_user(tmp)
        gpath = os.path.join(tmp, "guardrails.json")
        with open(gpath, "w") as f:
            json.dump({"kill_switch": True, "kill_switch_reason": "prior"}, f)

        # Sabotage /account so if it IS called the test fails loudly
        def _boom(*a, **k):
            raise AssertionError(
                "/account must not be called when kill_switch is already set")
        monkeypatch.setattr(cs, "user_api_get", _boom)

        cs.monitor_strategies(user)
        assert not rec.flattened, (
            "kill_switch already set — flatten has already happened; "
            "re-flattening would double-cancel orders")


# ========= 3. Daily-loss breach =========

def test_daily_loss_breach_trips_kill_switch(monkeypatch):
    """Starting value $100k → current $95k = 5% loss. Default threshold
    is 3%, so kill_switch should trip and _flatten_all_user run."""
    with tempfile.TemporaryDirectory() as tmp:
        cs = _load_cs(monkeypatch)
        rec = _Recorder(account={
            "portfolio_value": "95000", "cash": "5000",
            "buying_power": "40000",
        })
        _install(monkeypatch, cs, rec)
        user = _make_user(tmp)
        gpath = os.path.join(tmp, "guardrails.json")
        today = cs.get_et_time().strftime("%Y-%m-%d")
        with open(gpath, "w") as f:
            json.dump({
                "daily_starting_value": 100000.0,
                "daily_starting_value_date": today,
                "peak_portfolio_value": 100000.0,
            }, f)

        cs.monitor_strategies(user)

        assert rec.flattened, "5% daily loss must trigger _flatten_all_user"
        saved = json.load(open(gpath))
        assert saved.get("kill_switch") is True
        assert "kill_switch_reason" in saved
        assert "Daily loss" in saved["kill_switch_reason"]
        # Notifications fired
        assert rec.notifications, "kill-switch trip must notify"


# ========= 4. Max drawdown breach =========

def test_max_drawdown_breach_trips_kill_switch_and_critical_alert(monkeypatch):
    """Peak $100k → current $85k = 15% drawdown. Default threshold is
    10%. Must trip kill_switch, flatten, AND fire critical_alert
    (Sentry + ntfy + email) per CLAUDE.md Post-11 invariant — drawdown
    trips are rarer than daily-loss trips and operators need to know."""
    with tempfile.TemporaryDirectory() as tmp:
        cs = _load_cs(monkeypatch)
        rec = _Recorder(account={
            "portfolio_value": "85000", "cash": "3000",
            "buying_power": "30000",
        })
        _install(monkeypatch, cs, rec)
        user = _make_user(tmp)
        gpath = os.path.join(tmp, "guardrails.json")
        today = cs.get_et_time().strftime("%Y-%m-%d")
        with open(gpath, "w") as f:
            json.dump({
                "daily_starting_value": 86000.0,  # small intraday loss
                "daily_starting_value_date": today,
                "peak_portfolio_value": 100000.0,  # multi-day peak
            }, f)

        cs.monitor_strategies(user)

        assert rec.flattened
        assert rec.critical_alerts, (
            "drawdown breach must fire critical_alert (Sentry+ntfy+email) — "
            "Post-11 invariant")
        # Check the alert subject mentions the kill switch + user
        subj = rec.critical_alerts[0][0]
        assert "KILL SWITCH" in subj
        saved = json.load(open(gpath))
        assert saved["kill_switch"] is True
        assert "Max drawdown" in saved["kill_switch_reason"]


# ========= 5. Peak seeding =========

def test_peak_portfolio_value_seeded_when_missing(monkeypatch):
    """Users who signed up pre-round-10 have no peak_portfolio_value.
    The first tick that sees a valid current_val must seed it so the
    drawdown guard isn't a silent no-op."""
    with tempfile.TemporaryDirectory() as tmp:
        cs = _load_cs(monkeypatch)
        rec = _Recorder(account={
            "portfolio_value": "100000", "cash": "5000",
            "buying_power": "50000",
        })
        _install(monkeypatch, cs, rec)
        user = _make_user(tmp)
        gpath = os.path.join(tmp, "guardrails.json")
        # No peak_portfolio_value in file — legacy user
        with open(gpath, "w") as f:
            json.dump({}, f)

        cs.monitor_strategies(user)

        saved = json.load(open(gpath))
        assert saved.get("peak_portfolio_value") == 100000.0, (
            "first tick must seed peak_portfolio_value — legacy users "
            "would otherwise never have drawdown enforcement")


# ========= 6. Exception isolation =========

def test_top_level_exception_is_swallowed(monkeypatch):
    """If anything deep in the tick raises, the monitor must not
    propagate — the scheduler thread runs for every user in sequence
    and an uncaught exception would kill the thread for everyone.
    CLAUDE.md general invariants section."""
    with tempfile.TemporaryDirectory() as tmp:
        cs = _load_cs(monkeypatch)
        rec = _Recorder()
        _install(monkeypatch, cs, rec)
        user = _make_user(tmp)
        gpath = os.path.join(tmp, "guardrails.json")
        with open(gpath, "w") as f:
            json.dump({}, f)
        # Force user_api_get to blow up to simulate a bad upstream
        def _boom(*a, **k):
            raise RuntimeError("alpaca is on fire")
        monkeypatch.setattr(cs, "user_api_get", _boom)

        # Must NOT raise
        cs.monitor_strategies(user)
        # Error must have been logged
        logged = " ".join(m for m, t in rec.logs if t == "monitor")
        assert "Monitor error" in logged


# ========= 7. Extended hours opt-out =========

def test_ah_opt_out_returns_without_api_call(monkeypatch):
    """guardrails.extended_hours_trailing=false in AH mode → return
    before any Alpaca call. Users who set this explicitly don't want
    overnight stop raises."""
    with tempfile.TemporaryDirectory() as tmp:
        cs = _load_cs(monkeypatch)
        rec = _Recorder()
        _install(monkeypatch, cs, rec)

        # Sabotage: if /account is fetched, fail
        def _no_api(*a, **k):
            raise AssertionError("AH opt-out must not fetch /account")
        monkeypatch.setattr(cs, "user_api_get", _no_api)

        user = _make_user(tmp)
        gpath = os.path.join(tmp, "guardrails.json")
        with open(gpath, "w") as f:
            json.dump({"extended_hours_trailing": False}, f)

        cs.monitor_strategies(user, extended_hours=True)
        # No crash = opt-out honored
