"""
Round-17 — pin the scheduler_api extraction contract.

cloud_scheduler.py used to be a 3800-LOC monolith mixing scheduling
logic with Alpaca API plumbing. Round-17 extracted the plumbing into
scheduler_api.py. cloud_scheduler still re-exports the same symbols
for backwards compat, so existing call sites (`cs.user_api_get`,
`cs._cb_state`, etc.) work unchanged.

These tests fail loudly if a future refactor breaks the re-export
surface. Doing so would silently break:
  * the dashboard handler's references to `cs._cb_state` / `cs._cb_blocked`
  * the round-12 cb-reset bug-fix test in test_cloud_scheduler_helpers.py
  * any module that does `import cloud_scheduler as cs`
"""
from __future__ import annotations


def test_user_api_helpers_reexported():
    """Public Alpaca helpers must be importable from cloud_scheduler."""
    import cloud_scheduler as cs
    for name in ("user_api_get", "user_api_post",
                 "user_api_delete", "user_api_patch"):
        assert callable(getattr(cs, name, None)), (
            f"cloud_scheduler.{name} missing — re-export broke. "
            f"All call sites that do `cs.{name}` will fail at runtime."
        )


def test_circuit_breaker_internals_reexported():
    """Tests + handlers reach into _cb_* internals; the re-exports must
    be the SAME objects (not copies) or state mutations diverge."""
    import cloud_scheduler as cs
    import scheduler_api
    assert cs._cb_state is scheduler_api._cb_state
    assert cs._cb_lock is scheduler_api._cb_lock
    assert callable(cs._cb_blocked)
    assert callable(cs._cb_record_failure)
    assert callable(cs._cb_record_success)
    assert cs._CB_OPEN_THRESHOLD == scheduler_api._CB_OPEN_THRESHOLD
    assert cs._CB_OPEN_SECONDS == scheduler_api._CB_OPEN_SECONDS


def test_rate_limiter_internals_reexported():
    import cloud_scheduler as cs
    import scheduler_api
    assert cs._rl_state is scheduler_api._rl_state
    assert cs._rl_lock is scheduler_api._rl_lock
    assert cs._RL_MAX == scheduler_api._RL_MAX


def test_auth_alert_dedup_state_reexported():
    """Round-15 401/403 dedup state must be ONE shared dict — duplicating
    via copy would let each module fire its own daily alert."""
    import cloud_scheduler as cs
    import scheduler_api
    assert cs._auth_alert_dates is scheduler_api._auth_alert_dates
    assert cs._auth_alert_lock is scheduler_api._auth_alert_lock
    assert callable(cs._alert_alpaca_auth_failure)


def test_cb_blocked_initial_state_does_not_pop():
    """Round-12 bug regression test — moved here in round-17.
    _cb_blocked() previously popped {fails:N, open_until:0} on every
    non-open check, silently resetting the failure counter so the
    breaker NEVER tripped. Pin: fails accumulate across calls."""
    import scheduler_api as sa
    user = {"id": 99}
    sa._cb_state.clear()
    # Record 3 failures — should NOT trip yet (threshold is 5)
    sa._cb_record_failure(user)
    sa._cb_record_failure(user)
    sa._cb_record_failure(user)
    # Calling _cb_blocked between failures must NOT reset the counter
    assert sa._cb_blocked(user) is False
    sa._cb_record_failure(user)
    sa._cb_record_failure(user)
    # 5 failures → threshold met → blocked
    assert sa._cb_blocked(user) is True
    sa._cb_state.clear()


def test_rate_limiter_acquires_token():
    """Sanity check on the token bucket."""
    import scheduler_api as sa
    user = {"id": 100}
    sa._rl_state.clear()
    # Should have full bucket on first acquire
    assert sa._rl_acquire(user) is True
    sa._rl_state.clear()


def test_user_headers_pure():
    """_user_headers is a pure transform of user dict → header dict."""
    from scheduler_api import _user_headers
    h = _user_headers({"_api_key": "PK123", "_api_secret": "S456"})
    assert h["APCA-API-KEY-ID"] == "PK123"
    assert h["APCA-API-SECRET-KEY"] == "S456"
