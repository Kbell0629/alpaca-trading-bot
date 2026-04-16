"""Cloud scheduler logic — circuit breaker, date handling, guardrail helpers."""
import time


def test_circuit_breaker_trips_after_threshold(isolated_data_dir):
    import cloud_scheduler as cs
    user = {"id": "test-cb"}

    # Reset any state
    cs._cb_state.pop(user["id"], None)
    assert not cs._cb_blocked(user)

    # Record failures up to threshold
    for _ in range(cs._CB_OPEN_THRESHOLD):
        cs._cb_record_failure(user)

    assert cs._cb_blocked(user), "breaker should open after threshold"

    # Success clears
    cs._cb_record_success(user)
    assert not cs._cb_blocked(user), "success should reset breaker"


def test_circuit_breaker_per_user_isolated(isolated_data_dir):
    import cloud_scheduler as cs
    a = {"id": "user-a"}
    b = {"id": "user-b"}
    for u in (a, b):
        cs._cb_state.pop(u["id"], None)

    # Fail user-a into open state, user-b should be unaffected
    for _ in range(cs._CB_OPEN_THRESHOLD):
        cs._cb_record_failure(a)

    assert cs._cb_blocked(a)
    assert not cs._cb_blocked(b), "breaker must be per-user, not global"


def test_correlation_check_blocks_same_sector(isolated_data_dir):
    """The sector-correlation guard reads from the shared constants.SECTOR_MAP."""
    import cloud_scheduler as cs

    # Two tech positions; add third → should block
    positions = [{"symbol": "AAPL"}, {"symbol": "MSFT"}]
    allowed, reason = cs.check_correlation_allowed("NVDA", positions)
    assert not allowed, f"3 Tech positions should be blocked, got allowed={allowed} reason={reason}"
    # Adding a Finance position should pass (only 1 Finance so far)
    allowed, _ = cs.check_correlation_allowed("JPM", positions)
    assert allowed, "JPM should be allowed — no Finance positions yet"


def test_scheduler_log_records_et_not_utc(isolated_data_dir):
    import cloud_scheduler as cs
    # Reset log buffer
    with cs._logs_lock:
        cs._recent_logs.clear()
    cs.log("test message", task="test")
    with cs._logs_lock:
        assert len(cs._recent_logs) >= 1
        entry = cs._recent_logs[-1]
    # Timestamp must include ET suffix, not UTC
    assert "ET" in entry["ts"] or "EDT" in entry["ts"] or "EST" in entry["ts"]
    assert "UTC" not in entry["ts"]
    # ISO string should carry ET offset
    assert entry["ts_iso"].endswith(("-04:00", "-05:00"))
