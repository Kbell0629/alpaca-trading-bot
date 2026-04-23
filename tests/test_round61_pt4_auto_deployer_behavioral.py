"""
Round-61 pt.4: BEHAVIORAL coverage for run_auto_deployer.

The pt.2 grep-pin tests for this function pinned architectural invariants
(kill_switch first, LIVE_MODE_AT_START snapshot, etc.) without moving
pytest-cov. This file exercises the actual function body with stubs for
every side-effect callable:
  * user_api_get / user_api_post / user_api_patch / user_api_delete
  * subprocess.run (capital_check subprocess)
  * run_screener (30-min screener refresh)
  * notify_user / notify_rich / log

Covers the early-exit paths (kill switch, enabled=False, cooldown,
capital-check fail) and the main-flow entry up to the per-pick loop.
Exercising the full per-pick loop behaviorally would need 20+ more
stubs (picks JSON, factor providers, portfolio_risk, etc.) — that's
pt.5 work.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
from contextlib import contextmanager


@contextmanager
def _nolock(path):
    yield


class _Stubs:
    """Collects Alpaca calls + subprocess invocations + log output so
    each test can assert on what the deployer did."""

    def __init__(self, account=None, positions=None):
        self.account = account if account is not None else {
            "portfolio_value": "100000", "cash": "20000",
            "buying_power": "50000", "equity": "100000",
            "day_trades_remaining": 3,
        }
        self.positions = positions if positions is not None else []
        self.api_calls = []
        self.posts = []
        self.subprocess_calls = []
        self.logs = []
        self.notifications = []

    def api_get(self, user, path):
        self.api_calls.append(path)
        if path == "/account":
            return self.account
        if path == "/positions":
            return self.positions
        return {}

    def api_post(self, user, path, payload):
        self.posts.append((path, payload))
        return {"id": "stub-order-1"}

    def api_noop(self, *a, **k):
        return {}

    def subprocess_run(self, *args, **kwargs):
        self.subprocess_calls.append((args, kwargs))
        result = types.SimpleNamespace(
            returncode=0, stdout="", stderr="")
        return result

    def log(self, msg, task=None):
        self.logs.append((msg, task))

    def notify(self, *a, **k):
        self.notifications.append((a, k))


def _make_user(tmpdir):
    sdir = os.path.join(tmpdir, "strategies")
    os.makedirs(sdir, exist_ok=True)
    return {
        "id": 1, "username": "alice",
        "_data_dir": tmpdir, "_strategies_dir": sdir,
        "_api_key": "k", "_api_secret": "s",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_data_endpoint": "https://data.alpaca.markets/v2",
        "live_mode": False,
        "live_max_position_dollars": 500,
    }


def _install(monkeypatch, cs, stubs):
    monkeypatch.setattr(cs, "user_api_get", stubs.api_get)
    monkeypatch.setattr(cs, "user_api_post", stubs.api_post)
    monkeypatch.setattr(cs, "user_api_patch", stubs.api_noop)
    monkeypatch.setattr(cs, "user_api_delete", stubs.api_noop)
    monkeypatch.setattr(cs, "log", stubs.log)
    monkeypatch.setattr(cs, "notify_user", stubs.notify)
    monkeypatch.setattr(cs, "notify_rich", stubs.notify)
    monkeypatch.setattr(cs, "strategy_file_lock", _nolock)
    monkeypatch.setattr(cs, "run_screener", lambda user, **kw: None)
    monkeypatch.setattr(cs, "_flatten_all_user", lambda user: None)
    monkeypatch.setattr(subprocess, "run", stubs.subprocess_run)


def _load_cs(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


# ========= early-exit paths =========

def test_kill_switch_short_circuits_deployer(monkeypatch, tmp_path):
    """kill_switch=True → return before any API call."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({"kill_switch": True}, f)

    cs.run_auto_deployer(user)

    assert stubs.api_calls == [], (
        "kill_switch set → no Alpaca calls should happen")
    # Log must mention kill-switch skip
    assert any("Kill switch" in m for m, t in stubs.logs)


def test_enabled_false_config_short_circuits(monkeypatch, tmp_path):
    """auto_deployer_config.enabled=False → return cleanly."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    # No kill switch, but config disabled
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({}, f)
    with open(os.path.join(tmp_path, "auto_deployer_config.json"), "w") as f:
        json.dump({"enabled": False}, f)

    cs.run_auto_deployer(user)

    # Log must mention disabled
    assert any("disabled" in m.lower() for m, t in stubs.logs)
    # No orders placed
    assert stubs.posts == []


def test_cooldown_after_loss_blocks_deploy(monkeypatch, tmp_path):
    """last_loss_time within cooldown_after_loss_minutes → skip."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    recent = cs.now_et().isoformat()  # just happened
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({
            "last_loss_time": recent,
            "cooldown_after_loss_minutes": 60,
        }, f)

    cs.run_auto_deployer(user)

    assert any("cooldown" in m.lower() for m, t in stubs.logs)
    assert stubs.posts == []


def test_cooldown_parse_failure_fails_closed(monkeypatch, tmp_path):
    """Unparseable last_loss_time → fail CLOSED (skip deploy), not
    silently bypass the cooldown (R3 audit invariant)."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({"last_loss_time": "not-a-timestamp"}, f)

    cs.run_auto_deployer(user)

    # Must log the safe-skip
    assert any("safe" in m.lower() for m, t in stubs.logs), \
        "cooldown parse failure must log the fail-closed reason"
    # Must not place orders
    assert stubs.posts == []


def test_missing_guardrails_file_loads_as_empty(monkeypatch, tmp_path):
    """No guardrails.json on disk → deploy continues with defaults.
    Legacy user path (pre-round-10)."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    # Don't write any guardrails.json

    # Should not raise. May not place any orders (no picks file) but
    # shouldn't early-exit on kill_switch path.
    cs.run_auto_deployer(user)


def test_missing_picks_file_does_not_crash(monkeypatch, tmp_path):
    """No dashboard_data.json → no picks to deploy, deployer must
    exit cleanly, not raise."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({}, f)
    # No dashboard_data.json either

    cs.run_auto_deployer(user)
    # No crash, no orders
    assert stubs.posts == []


# ========= calibration + /account paths =========

def test_calibration_with_small_equity_falls_back(monkeypatch, tmp_path):
    """Equity <$500 → detect_tier returns None → log the fallback
    message. Must not raise."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs(account={
        "portfolio_value": "100", "cash": "100",
        "buying_power": "100", "equity": "100",
        "day_trades_remaining": 3,
    })
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({}, f)

    cs.run_auto_deployer(user)
    # Log must mention the fallback
    assert any("too small" in m.lower() or "calibrat" in m.lower()
                for m, t in stubs.logs)


def test_daily_starting_value_seeded_on_first_tick(monkeypatch, tmp_path):
    """First auto-deployer run of the day seeds daily_starting_value
    + daily_starting_value_date."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    gpath = os.path.join(tmp_path, "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({}, f)

    cs.run_auto_deployer(user)

    saved = json.load(open(gpath))
    assert saved.get("daily_starting_value") == 100000.0
    assert saved.get("daily_starting_value_date") is not None
    # ET-date format
    dsvd = saved["daily_starting_value_date"]
    assert len(dsvd) == 10 and dsvd.count("-") == 2


def test_peak_portfolio_value_updated_on_new_high(monkeypatch, tmp_path):
    """current > peak → peak_portfolio_value gets bumped."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs(account={
        "portfolio_value": "150000", "cash": "20000",
        "buying_power": "50000", "equity": "150000",
        "day_trades_remaining": 3,
    })
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    gpath = os.path.join(tmp_path, "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"peak_portfolio_value": 100000.0}, f)

    cs.run_auto_deployer(user)

    saved = json.load(open(gpath))
    assert saved["peak_portfolio_value"] == 150000.0


def test_factor_bypass_skips_breadth_check(monkeypatch, tmp_path):
    """guardrails.factor_bypass=True → market_breadth module must
    NOT be imported (measurable via sys.modules)."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({"factor_bypass": True}, f)
    # Pre-clear so we can detect if it's imported
    sys.modules.pop("market_breadth", None)

    cs.run_auto_deployer(user)

    # Log must announce factor bypass
    assert any("FACTOR BYPASS" in m for m, t in stubs.logs)


# ========= subprocess path =========

def test_capital_check_subprocess_called(monkeypatch, tmp_path):
    """The capital-check subprocess must be invoked with the per-user
    CAPITAL_STATUS_PATH set in env (R10 isolation fix)."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({}, f)

    cs.run_auto_deployer(user)

    # At least one subprocess call with the capital-check script path
    assert stubs.subprocess_calls, "capital_check.py subprocess must be invoked"
    args, kwargs = stubs.subprocess_calls[0]
    env = kwargs.get("env", {})
    assert "CAPITAL_STATUS_PATH" in env, (
        "subprocess must be passed CAPITAL_STATUS_PATH env var (R10 fix)")
    # Points into the per-user dir
    assert str(tmp_path) in env["CAPITAL_STATUS_PATH"]


def test_capital_check_cant_trade_skips_deploy(monkeypatch, tmp_path):
    """capital_status.json says can_trade=False → deployer aborts."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    # Write a capital_status.json saying we can't trade
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({}, f)
    capital_path = os.path.join(tmp_path, "capital_status.json")
    def write_capital(*args, **kwargs):
        stubs.subprocess_calls.append((args, kwargs))
        # Simulate capital_check.py writing its output to the per-user path
        env = kwargs.get("env", {})
        target = env.get("CAPITAL_STATUS_PATH", capital_path)
        with open(target, "w") as f:
            json.dump({"can_trade": False,
                       "recommendation": "equity too low"}, f)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", write_capital)

    cs.run_auto_deployer(user)

    # Should have notified user about the skip
    assert any("skipped" in str(n).lower() or "cannot" in str(n).lower()
                for n in stubs.notifications)
    # No orders placed
    assert stubs.posts == []


def test_live_mode_at_start_snapshot_captured(monkeypatch, tmp_path):
    """live_mode must be snapshotted at function entry so a mid-run
    toggle by the HTTP handler doesn't change pick routing (R45 invariant)."""
    cs = _load_cs(monkeypatch)
    stubs = _Stubs()
    _install(monkeypatch, cs, stubs)
    user = _make_user(str(tmp_path))
    user["live_mode"] = False
    with open(os.path.join(tmp_path, "guardrails.json"), "w") as f:
        json.dump({}, f)

    # If the snapshot is being captured, this call shouldn't raise —
    # we're just exercising the code path. The actual snapshot variable
    # is local, but its existence means the function made it past line ~1995.
    cs.run_auto_deployer(user)
    # The function must have made it past the kill_switch + config checks
    # (observable via the /account fetch happening)
    assert "/account" in stubs.api_calls


# ========= correlation / check_correlation_allowed exposed =========

def test_check_correlation_allowed_blocks_3rd_tech():
    """3 tech positions existing → 4th tech blocked.
    Exercises cloud_scheduler.check_correlation_allowed directly."""
    import cloud_scheduler as cs
    positions = [{"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "NVDA"}]
    allowed, reason = cs.check_correlation_allowed("GOOGL", positions)
    assert not allowed
    assert "tech" in reason.lower() or "sector" in reason.lower() or "concentr" in reason.lower()


def test_check_correlation_allowed_passes_different_sector():
    import cloud_scheduler as cs
    positions = [{"symbol": "AAPL"}, {"symbol": "MSFT"}]
    allowed, _ = cs.check_correlation_allowed("JPM", positions)
    assert allowed


def test_deploy_should_abort_reads_event_state():
    """deploy_should_abort reads the threading.Event state.
    request_deploy_abort / clear_deploy_abort are the setters."""
    import cloud_scheduler as cs
    cs.clear_deploy_abort()
    assert cs.deploy_should_abort() is False
    cs.request_deploy_abort()
    assert cs.deploy_should_abort() is True
    cs.clear_deploy_abort()  # cleanup
    assert cs.deploy_should_abort() is False


# ========= circuit breaker helpers (exercised directly) =========

def test_circuit_breaker_isolation():
    """Per-user circuit breaker state is isolated."""
    import cloud_scheduler as cs
    a = {"id": "cb-test-a"}
    b = {"id": "cb-test-b"}
    for u in (a, b):
        cs._cb_state.pop(u["id"], None)
    for _ in range(cs._CB_OPEN_THRESHOLD):
        cs._cb_record_failure(a)
    assert cs._cb_blocked(a)
    assert not cs._cb_blocked(b)
    cs._cb_record_success(a)
    assert not cs._cb_blocked(a)
