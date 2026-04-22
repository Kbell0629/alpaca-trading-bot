"""
Round-46 fixes for round-45 dual-mode. Audit surfaced four real issues:

  1. get_dashboard_data / _resolve_user_paths didn't take `mode` — the
     live-view dashboard was silently reading the paper state tree.
  2. _wheel_deploy_in_flight dedup in cloud_scheduler used plain
     user_id, so paper + live wheel-deploy ticks collided.
  3. Alpaca auth-failure alert dedup in scheduler_api didn't include
     mode, so a paper-creds-expired alert silenced the live-creds
     alert for the same day.
  4. _cb_state / _rl_state circuit-breaker + rate-limiter shared one
     bucket per user across paper + live (two separate Alpaca
     backends with separate rate budgets).

These tests pin the fixes so round-47+ can't silently regress them.
"""
from __future__ import annotations

import importlib
import sys as _sys


def _reload():
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler  # noqa
    import scheduler_api  # noqa
    importlib.reload(scheduler_api)
    importlib.reload(cloud_scheduler)
    return cloud_scheduler, scheduler_api


# ---------- _resolve_user_paths + get_dashboard_data mode plumbing ----------


def test_resolve_user_paths_honors_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "c" * 64)
    _sys.modules.pop("server", None)
    _sys.modules.pop("auth", None)
    import server
    paper_dir, _ = server._resolve_user_paths(7, mode="paper")
    live_dir, _ = server._resolve_user_paths(7, mode="live")
    assert paper_dir != live_dir
    assert paper_dir.endswith("/users/7")
    assert live_dir.endswith("/users/7/live")


def test_resolve_user_paths_default_is_paper(tmp_path, monkeypatch):
    """Pre-round-46 callers that don't pass mode must stay on paper
    (backward compat with existing state files)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "c" * 64)
    _sys.modules.pop("server", None)
    _sys.modules.pop("auth", None)
    import server
    d_default, _ = server._resolve_user_paths(7)
    d_paper, _ = server._resolve_user_paths(7, mode="paper")
    assert d_default == d_paper


# ---------- _wheel_deploy_in_flight dedup mode awareness ----------


def test_wheel_deploy_in_flight_uid_includes_mode_for_live(tmp_path, monkeypatch):
    """Pin: run_wheel_auto_deploy's dedup key for a live user must
    differ from a paper user with the same id, so paper + live
    wheel-deploy ticks don't block each other."""
    cs, _ = _reload()
    src = open(cs.__file__).read()
    # The fix lives inside run_wheel_auto_deploy. Grep-level pin:
    assert 'f"{user[\'id\']}:{_mode}" if _mode == "live"' in src, (
        "run_wheel_auto_deploy must scope its _wheel_deploy_in_flight "
        "key by mode. If this assertion fires, the round-46 fix was "
        "reverted and paper+live wheel deploys can collide.")


# ---------- scheduler_api auth-failure alert dedup ----------


def test_alpaca_auth_failure_dedup_includes_mode(tmp_path, monkeypatch):
    _, sa = _reload()
    src = open(sa.__file__).read()
    # Should see our mode-aware dedup — otherwise paper alert would
    # silence live alert for the same day.
    assert "_mode = user.get(\"_mode\"" in src
    assert "f\"{user.get('id')}:{_mode}\"" in src


# ---------- _cb_key (circuit breaker + rate limiter) ----------


def test_cb_key_paper_is_plain_user_id(tmp_path, monkeypatch):
    """Paper keeps the plain-id key for backward-compat with any
    pre-round-46 in-memory state."""
    _, sa = _reload()
    user_paper = {"id": 42, "_mode": "paper"}
    user_default = {"id": 42}  # no _mode field
    assert sa._cb_key(user_paper) == 42
    assert sa._cb_key(user_default) == 42  # defaults to paper


def test_cb_key_live_is_scoped(tmp_path, monkeypatch):
    """Live gets a separate bucket so paper + live (different Alpaca
    backends, different rate-limit budgets) don't share the CB/RL
    state."""
    _, sa = _reload()
    user_live = {"id": 42, "_mode": "live"}
    assert sa._cb_key(user_live) == "42:live"


def test_cb_key_paper_and_live_are_distinct(tmp_path, monkeypatch):
    _, sa = _reload()
    p = sa._cb_key({"id": 1, "_mode": "paper"})
    live = sa._cb_key({"id": 1, "_mode": "live"})
    assert p != live, (
        "Circuit-breaker + rate-limiter buckets MUST differ between "
        "paper and live or a paper CB trip would block live, and paper "
        "rate-limit exhaustion would throttle live unnecessarily.")
