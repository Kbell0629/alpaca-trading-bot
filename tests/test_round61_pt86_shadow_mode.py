"""Round-61 pt.86 — live-mode dry-run / shadow mode.

Paper validation ends ~May 15. After that the user can flip the
live-mode toggle (with the pt.72 promotion gate as a check), and
real money starts trading. Shadow mode is a per-user opt-in flag
that runs the auto-deployer through every gate but RECORDS the
deploy intent into a per-user shadow log INSTEAD of POSTing the
order — letting the user "watch what live would do" without
committing capital.

Tests cover:
  * is_shadow_mode_active resolution (guardrails > env > False)
  * record_shadow_event persistence + log capping
  * get_shadow_log read-back ordering
  * summarize_shadow_log aggregations
  * Pure-module discipline
  * Wiring source-pin into auto-deployer
"""
from __future__ import annotations

import json
import os
import tempfile


# ============================================================================
# is_shadow_mode_active
# ============================================================================

def test_inactive_by_default():
    import shadow_mode as sm
    assert sm.is_shadow_mode_active({}, {}, env={}) is False
    assert sm.is_shadow_mode_active(None, None, env={}) is False


def test_guardrails_override_wins():
    import shadow_mode as sm
    # Even if env says off, guardrails True wins.
    assert sm.is_shadow_mode_active(
        {}, {"live_shadow_mode": True}, env={"LIVE_SHADOW_MODE": "0"}
    ) is True


def test_guardrails_override_can_disable():
    """Per-user opt-OUT wins over deployment-wide opt-in."""
    import shadow_mode as sm
    assert sm.is_shadow_mode_active(
        {}, {"live_shadow_mode": False}, env={"LIVE_SHADOW_MODE": "1"}
    ) is False


def test_env_var_enables_globally():
    import shadow_mode as sm
    for raw in ("1", "true", "TRUE", "yes", "on"):
        assert sm.is_shadow_mode_active(
            {}, {}, env={"LIVE_SHADOW_MODE": raw}) is True
    for raw in ("0", "false", "no", "", "off"):
        assert sm.is_shadow_mode_active(
            {}, {}, env={"LIVE_SHADOW_MODE": raw}) is False


# ============================================================================
# record_shadow_event + get_shadow_log
# ============================================================================

def _make_user():
    tmp = tempfile.mkdtemp(prefix="pt86-")
    return {"_data_dir": tmp}


def test_record_shadow_event_writes_log():
    import shadow_mode as sm
    user = _make_user()
    event = sm.record_shadow_event(
        user, "would_deploy",
        symbol="AAPL", strategy="breakout", qty=10, price=200)
    assert event["action"] == "would_deploy"
    assert event["symbol"] == "AAPL"
    log = sm.get_shadow_log(user)
    assert len(log) == 1
    assert log[0]["symbol"] == "AAPL"


def test_record_shadow_event_appends_in_order():
    import shadow_mode as sm
    user = _make_user()
    sm.record_shadow_event(user, "would_deploy", symbol="A")
    sm.record_shadow_event(user, "would_deploy", symbol="B")
    sm.record_shadow_event(user, "would_deploy", symbol="C")
    # get_shadow_log returns newest first
    log = sm.get_shadow_log(user)
    assert [e["symbol"] for e in log] == ["C", "B", "A"]


def test_log_capped_at_max_entries():
    import shadow_mode as sm
    user = _make_user()
    # Force a low cap by patching MAX_LOG_ENTRIES.
    original = sm.MAX_LOG_ENTRIES
    sm.MAX_LOG_ENTRIES = 5
    try:
        for i in range(10):
            sm.record_shadow_event(user, "would_deploy", symbol=f"S{i}")
        log = sm.get_shadow_log(user, limit=100)
        assert len(log) == 5
        # Should keep the LAST 5 (S5..S9)
        assert log[0]["symbol"] == "S9"
        assert log[-1]["symbol"] == "S5"
    finally:
        sm.MAX_LOG_ENTRIES = original


def test_get_shadow_log_handles_missing_file():
    import shadow_mode as sm
    user = _make_user()
    assert sm.get_shadow_log(user) == []


def test_get_shadow_log_handles_corrupt_json():
    import shadow_mode as sm
    user = _make_user()
    path = os.path.join(user["_data_dir"], "shadow_log.json")
    with open(path, "w") as fh:
        fh.write("not valid json{{{")
    assert sm.get_shadow_log(user) == []


def test_record_event_handles_missing_data_dir():
    """No `_data_dir` → returns the event but doesn't write."""
    import shadow_mode as sm
    event = sm.record_shadow_event({}, "would_deploy", symbol="X")
    assert event["action"] == "would_deploy"   # still returned
    # No assertion on file system — just shouldn't raise.


def test_get_shadow_log_limit_respected():
    import shadow_mode as sm
    user = _make_user()
    for i in range(20):
        sm.record_shadow_event(user, "would_deploy", symbol=f"S{i}")
    log = sm.get_shadow_log(user, limit=5)
    assert len(log) == 5


# ============================================================================
# summarize_shadow_log
# ============================================================================

def test_summarize_empty_log():
    import shadow_mode as sm
    out = sm.summarize_shadow_log([])
    assert out["total"] == 0
    assert out["by_action"] == {}


def test_summarize_groups_by_action_symbol_strategy():
    import shadow_mode as sm
    events = [
        {"action": "would_deploy", "symbol": "AAPL", "strategy": "breakout"},
        {"action": "would_deploy", "symbol": "AAPL", "strategy": "wheel"},
        {"action": "would_deploy", "symbol": "MSFT", "strategy": "breakout"},
        {"action": "would_close",  "symbol": "AAPL"},
    ]
    out = sm.summarize_shadow_log(events)
    assert out["total"] == 4
    assert out["by_action"]["would_deploy"] == 3
    assert out["by_action"]["would_close"] == 1
    assert out["by_symbol"]["AAPL"] == 3
    assert out["by_symbol"]["MSFT"] == 1
    assert out["by_strategy"]["breakout"] == 2
    assert out["by_strategy"]["wheel"] == 1


def test_summarize_handles_bad_input():
    import shadow_mode as sm
    out = sm.summarize_shadow_log("not a list")
    assert out["total"] == 0


# ============================================================================
# Wiring source-pin: cloud_scheduler short-circuits on shadow mode
# ============================================================================

def test_cloud_scheduler_imports_shadow_mode():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    assert "import shadow_mode as _sm" in src
    assert "is_shadow_mode_active" in src
    assert "record_shadow_event" in src


def test_cloud_scheduler_shadow_branch_short_circuits():
    """Pt.86: when shadow mode is on, the deploy loop must
    `continue` (skip the order POST) after recording the event."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    idx = src.find("is_shadow_mode_active(user")
    assert idx > 0
    block = src[idx:idx + 1000]
    assert "record_shadow_event" in block
    assert "continue" in block


def test_cloud_scheduler_shadow_branch_documented():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    assert "pt.86" in src
    assert "shadow" in src.lower()


# ============================================================================
# Pure-module discipline
# ============================================================================

def test_shadow_mode_pure_module():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "shadow_mode.py").read_text()
    forbidden = ("import cloud_scheduler", "from cloud_scheduler",
                  "import auth\n", "from auth import",
                  "import server", "from server import")
    for f in forbidden:
        assert f not in src
