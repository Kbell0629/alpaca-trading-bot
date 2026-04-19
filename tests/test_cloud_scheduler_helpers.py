"""
Unit tests for cloud_scheduler.py helpers.

The scheduler is 3800 LOC; integration tests of full ticks require
an Alpaca mock + wall-clock-dependent setup. These tests target the
PURE helpers — rate limiter, circuit breaker, time-gate persistence —
that have thread-safety requirements but no network / file-system
dependencies beyond `_last_runs` persistence (which is mockable).

Coverage:

  * Rate limiter (`_rl_acquire`): token consumption, refill over time,
    multi-user isolation, exhaustion returns False after wait_max
  * Circuit breaker (`_cb_blocked` / `_cb_record_failure` /
    `_cb_record_success`): open threshold, success reset, cool-off
    window, per-user keys
  * Time gates (`should_run_interval` / `should_run_daily_at` /
    `_clear_daily_stamp`): TOCTOU serialisation, same-day dedupe,
    max_late_seconds window, stamp clearing
"""
from __future__ import annotations

import threading
import time

import pytest


# ---------- Rate limiter ----------


@pytest.fixture
def clean_rl_state():
    import cloud_scheduler as cs
    # Tests may run in any order; start each with a clean bucket.
    with cs._rl_lock:
        cs._rl_state.clear()
    yield cs
    with cs._rl_lock:
        cs._rl_state.clear()


def test_rl_acquire_first_call_succeeds(clean_rl_state):
    cs = clean_rl_state
    user = {"id": 1}
    assert cs._rl_acquire(user) is True
    # Bucket should now have (_RL_MAX - 1) tokens
    assert cs._rl_state[1]["tokens"] == pytest.approx(cs._RL_MAX - 1, abs=0.1)


def test_rl_acquire_per_user_isolation(clean_rl_state):
    cs = clean_rl_state
    u1, u2 = {"id": 1}, {"id": 2}
    # Drain u1 close-to-empty
    for _ in range(10):
        cs._rl_acquire(u1)
    # u2 should still have a full bucket
    assert cs._rl_acquire(u2) is True
    assert cs._rl_state[2]["tokens"] == pytest.approx(cs._RL_MAX - 1, abs=0.1)


def test_rl_acquire_returns_false_when_exhausted(clean_rl_state, monkeypatch):
    """Set the bucket to empty + fast-forward past wait_max — acquire
    should return False without sleeping forever."""
    cs = clean_rl_state
    user = {"id": 1}
    # Seed empty bucket
    with cs._rl_lock:
        cs._rl_state[1] = {"tokens": 0.0, "updated": time.time()}
    # Patch time.sleep to avoid the real delay
    monkeypatch.setattr(cs.time, "sleep", lambda _: None)
    # wait_max=0.01 + zero refill means False fast
    start = time.time()
    result = cs._rl_acquire(user, wait_max=0.01)
    elapsed = time.time() - start
    assert result is False
    assert elapsed < 2.0, f"waited too long ({elapsed:.2f}s)"


def test_rl_acquire_refills_over_time(clean_rl_state):
    cs = clean_rl_state
    user = {"id": 1}
    # Drain the bucket to 0.
    with cs._rl_lock:
        cs._rl_state[1] = {"tokens": 0.0, "updated": time.time() - 1.0}
    # After 1 second idle, refill rate should have deposited ~3 tokens
    # (default _RL_REFILL_PER_SEC = 3.0), enough for one acquire.
    assert cs._rl_acquire(user) is True


def test_rl_acquire_concurrent_cannot_leak(clean_rl_state):
    """20 threads race to consume from a fresh bucket. Total successes
    can't exceed _RL_MAX (plus a tiny refill-slop tolerance)."""
    cs = clean_rl_state
    user = {"id": 1}
    THREADS = 20
    successes = []
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def worker():
        barrier.wait()
        if cs._rl_acquire(user, wait_max=0.1):
            with lock:
                successes.append(True)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Fresh bucket has _RL_MAX tokens; all 20 threads succeed in this
    # test (20 << _RL_MAX=180). We just verify no double-count leaks.
    assert len(successes) == THREADS, (
        f"expected all {THREADS} threads to acquire; got {len(successes)}"
    )


# ---------- Circuit breaker ----------


@pytest.fixture
def clean_cb_state():
    import cloud_scheduler as cs
    with cs._cb_lock:
        cs._cb_state.clear()
    yield cs
    with cs._cb_lock:
        cs._cb_state.clear()


def test_cb_blocked_false_on_fresh_user(clean_cb_state):
    cs = clean_cb_state
    assert cs._cb_blocked({"id": 1}) is False


def test_cb_record_failure_increments_counter(clean_cb_state):
    cs = clean_cb_state
    user = {"id": 1}
    cs._cb_record_failure(user)
    with cs._cb_lock:
        assert cs._cb_state[1]["fails"] == 1


def test_cb_opens_at_threshold(clean_cb_state):
    """Default _CB_OPEN_THRESHOLD=5. Fifth failure trips the breaker."""
    cs = clean_cb_state
    user = {"id": 1}
    for _ in range(cs._CB_OPEN_THRESHOLD - 1):
        cs._cb_record_failure(user)
    # Not blocked yet — 4 failures
    assert cs._cb_blocked(user) is False
    cs._cb_record_failure(user)
    # 5th failure: open_until should be set in the future
    with cs._cb_lock:
        assert cs._cb_state[1]["open_until"] > time.time()
    assert cs._cb_blocked(user) is True


def test_cb_preserves_counter_across_intervening_blocked_checks(clean_cb_state):
    """Regression guard for the round-12 audit bug: _cb_blocked() was
    POPping the entire state key whenever open_until <= now, which
    included the initial {fails: N, open_until: 0} state that every
    failing call creates. Net effect: fails never accumulated past 1
    because _cb_blocked() between each failing call reset the counter.

    This test simulates the real call sequence: check-blocked →
    record-failure → check-blocked → record-failure ... and asserts the
    counter actually accumulates."""
    cs = clean_cb_state
    user = {"id": 42}
    for i in range(cs._CB_OPEN_THRESHOLD):
        # Every call to user_api_get does this dance:
        assert cs._cb_blocked(user) is False, (
            f"at iteration {i}: breaker tripped earlier than expected"
        )
        cs._cb_record_failure(user)
    # After THRESHOLD failures, breaker should be open.
    assert cs._cb_blocked(user) is True, (
        "breaker failed to trip after THRESHOLD consecutive failures — "
        "the _cb_blocked POP bug is back"
    )


def test_cb_success_resets_state(clean_cb_state):
    """Any success pops the state key — counter back to 0."""
    cs = clean_cb_state
    user = {"id": 1}
    cs._cb_record_failure(user)
    cs._cb_record_failure(user)
    assert cs._cb_state[1]["fails"] == 2
    cs._cb_record_success(user)
    assert 1 not in cs._cb_state


def test_cb_per_user_isolation(clean_cb_state):
    cs = clean_cb_state
    u1, u2 = {"id": 1}, {"id": 2}
    for _ in range(cs._CB_OPEN_THRESHOLD):
        cs._cb_record_failure(u1)
    assert cs._cb_blocked(u1) is True
    assert cs._cb_blocked(u2) is False


def test_cb_cool_off_elapses_resets(clean_cb_state):
    """Once open_until has passed, _cb_blocked pops the state and
    returns False — the next request is allowed as a probe."""
    cs = clean_cb_state
    user = {"id": 1}
    # Trip the breaker
    for _ in range(cs._CB_OPEN_THRESHOLD):
        cs._cb_record_failure(user)
    # Force open_until into the past
    with cs._cb_lock:
        cs._cb_state[1]["open_until"] = time.time() - 1.0
    # Now _cb_blocked should return False AND clear the state
    assert cs._cb_blocked(user) is False
    assert 1 not in cs._cb_state


def test_cb_env_key_for_env_mode_user(clean_cb_state):
    """Legacy env-var mode passes a user dict without 'id'. _cb_key
    falls back to the literal 'env' string."""
    cs = clean_cb_state
    user = {}
    cs._cb_record_failure(user)
    with cs._cb_lock:
        assert "env" in cs._cb_state


# ---------- Time gates ----------


@pytest.fixture
def clean_last_runs():
    import cloud_scheduler as cs
    with cs._last_runs_lock:
        snapshot = dict(cs._last_runs)
        cs._last_runs.clear()
    yield cs
    with cs._last_runs_lock:
        cs._last_runs.clear()
        cs._last_runs.update(snapshot)


def test_should_run_interval_first_call_fires(clean_last_runs, monkeypatch):
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    assert cs.should_run_interval("test_task", 60) is True


def test_should_run_interval_debounces_within_window(clean_last_runs, monkeypatch):
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    assert cs.should_run_interval("tk", 60) is True
    # Immediate re-call — inside the 60s window
    assert cs.should_run_interval("tk", 60) is False


def test_should_run_interval_fires_again_after_window(clean_last_runs, monkeypatch):
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    assert cs.should_run_interval("tk", 60) is True
    # Rewind last-run 61 seconds into the past
    with cs._last_runs_lock:
        cs._last_runs["tk"] = time.time() - 61
    assert cs.should_run_interval("tk", 60) is True


def test_should_run_interval_is_thread_safe(clean_last_runs, monkeypatch):
    """20 concurrent callers on the same task — exactly one should see
    True; the rest see False. This is the TOCTOU bug the round-9 fix
    addressed. If the lock is wrong, multiple threads see True."""
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    THREADS = 20
    fires = []
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def worker():
        barrier.wait()
        result = cs.should_run_interval("racing_task", 3600)
        with lock:
            fires.append(result)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    true_count = sum(1 for r in fires if r)
    assert true_count == 1, (
        f"TOCTOU race: {true_count}/{THREADS} threads saw fire=True; expected exactly 1"
    )


def test_should_run_daily_at_fires_once_per_day(clean_last_runs, monkeypatch):
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    # Mock get_et_time to return a fixed datetime well past 9:35 AM ET
    from datetime import datetime, timezone, timedelta
    ET = timezone(timedelta(hours=-4))
    fixed_et = datetime(2026, 5, 1, 9, 40, 0, tzinfo=ET)
    monkeypatch.setattr(cs, "get_et_time", lambda: fixed_et)

    # First call at 9:40 AM — inside the 30-min window after 9:35 target
    assert cs.should_run_daily_at("daily_task", 9, 35) is True
    # Second call same day — dedupe should block
    assert cs.should_run_daily_at("daily_task", 9, 35) is False


def test_should_run_daily_at_blocks_before_target_time(clean_last_runs, monkeypatch):
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    from datetime import datetime, timezone, timedelta
    ET = timezone(timedelta(hours=-4))
    # 9:20 AM ET — 15 min before 9:35 target
    early = datetime(2026, 5, 1, 9, 20, 0, tzinfo=ET)
    monkeypatch.setattr(cs, "get_et_time", lambda: early)
    assert cs.should_run_daily_at("daily_task", 9, 35) is False


def test_should_run_daily_at_blocks_past_max_late_window(clean_last_runs, monkeypatch):
    """Default max_late_seconds=1800 (30 min). At 10:10 AM (35 min past
    the 9:35 target), the gate should BLOCK firing — the window is
    blown and we don't want to trade on stale screener data."""
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    from datetime import datetime, timezone, timedelta
    ET = timezone(timedelta(hours=-4))
    too_late = datetime(2026, 5, 1, 10, 10, 0, tzinfo=ET)
    monkeypatch.setattr(cs, "get_et_time", lambda: too_late)
    assert cs.should_run_daily_at("daily_task", 9, 35) is False


def test_should_run_daily_at_custom_max_late(clean_last_runs, monkeypatch):
    """daily_close uses max_late_seconds=4hr so a Railway redeploy
    at 4:46 PM (41 min past 4:05 target) can still fire the close."""
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    from datetime import datetime, timezone, timedelta
    ET = timezone(timedelta(hours=-4))
    late_redeploy = datetime(2026, 5, 1, 16, 46, 0, tzinfo=ET)
    monkeypatch.setattr(cs, "get_et_time", lambda: late_redeploy)
    # 4:05 PM target, 4-hour lateness tolerance → should still fire
    assert cs.should_run_daily_at("daily_close", 16, 5, max_late_seconds=4*3600) is True


def test_clear_daily_stamp_allows_retry(clean_last_runs, monkeypatch):
    """Round-10 fix — after a task raises inside its run, clearing the
    stamp lets the next tick retry it."""
    cs = clean_last_runs
    monkeypatch.setattr(cs, "_save_last_runs", lambda: None)
    from datetime import datetime, timezone, timedelta
    ET = timezone(timedelta(hours=-4))
    fixed = datetime(2026, 5, 1, 9, 40, 0, tzinfo=ET)
    monkeypatch.setattr(cs, "get_et_time", lambda: fixed)
    # First run stamps the task
    assert cs.should_run_daily_at("retry_task", 9, 35) is True
    assert cs.should_run_daily_at("retry_task", 9, 35) is False
    # Clear the stamp — next check fires again
    cs._clear_daily_stamp("retry_task")
    assert cs.should_run_daily_at("retry_task", 9, 35) is True


# ---------- Deploy-abort event (smoke-level — extends round-12 tests) ----------


def test_deploy_abort_event_is_module_level():
    """Regression guard: the _deploy_abort_event primitive must stay at
    module scope so kill-switch + deploy loops can share it without
    passing references around."""
    import cloud_scheduler as cs
    assert hasattr(cs, "_deploy_abort_event")
    assert hasattr(cs, "request_deploy_abort")
    assert hasattr(cs, "clear_deploy_abort")
    assert hasattr(cs, "deploy_should_abort")
