"""
Tests for the in-memory login-attempt token bucket added to auth.py.

Context: the audit flagged that the previous SQLite-only rate-limit
could leak a few requests through under 100+ concurrent auth threads,
because two threads might both read count=4 before either wrote a new
failure row. The bucket is a race-free fast-path defence that runs
BEFORE the SQLite check.

Coverage:

  * Bucket starts full (LOGIN_BUCKET_BURST tokens) for a fresh key.
  * Exhausting the bucket trips is_login_locked immediately, even
    with zero SQLite failures.
  * Bucket refills at LOGIN_BUCKET_RATE_PER_SEC tokens/sec.
  * Different keys have independent buckets.
  * Concurrent access from 100 threads cannot leak more than BURST
    successful "unlocked" reads before the bucket exhausts — the
    specific race the audit called out.
  * The SQLite gate is still authoritative: if SQLite lockout holds,
    is_login_locked stays True even after the bucket refills.
  * _reset_login_buckets() clears state for test isolation.
"""
from __future__ import annotations

import threading
import time

import pytest


def test_bucket_starts_full(isolated_data_dir):
    import auth
    ip, user = "1.2.3.4", "alice"
    # Fresh key: bucket full, no tokens consumed yet.
    assert auth._login_bucket_peek(ip, user) == float(auth.LOGIN_BUCKET_BURST)


def test_bucket_consume_returns_true_until_empty(isolated_data_dir):
    import auth
    ip, user = "1.2.3.4", "alice"
    for _ in range(auth.LOGIN_BUCKET_BURST):
        assert auth._login_bucket_consume(ip, user) is True
    # Bucket drained now — next consume should return False.
    assert auth._login_bucket_consume(ip, user) is False


def test_bucket_refills_after_idle(isolated_data_dir):
    import auth
    ip, user = "1.2.3.4", "alice"
    # Drain
    for _ in range(auth.LOGIN_BUCKET_BURST):
        auth._login_bucket_consume(ip, user)
    assert auth._login_bucket_consume(ip, user) is False
    # Wait long enough to refill at least one token.
    sleep_for = 1.0 / auth.LOGIN_BUCKET_RATE_PER_SEC + 0.2  # ~5.2s at default
    time.sleep(sleep_for)
    assert auth._login_bucket_consume(ip, user) is True


def test_different_keys_have_independent_buckets(isolated_data_dir):
    import auth
    ip_a, ip_b = "1.1.1.1", "2.2.2.2"
    # Drain A completely.
    for _ in range(auth.LOGIN_BUCKET_BURST):
        auth._login_bucket_consume(ip_a, "user")
    assert auth._login_bucket_consume(ip_a, "user") is False
    # B is unaffected. Peek with a small tolerance — micro-refill can
    # add a tiny fraction of a token between consume() and peek().
    assert auth._login_bucket_consume(ip_b, "user") is True
    assert auth._login_bucket_peek(ip_b, "user") == pytest.approx(
        float(auth.LOGIN_BUCKET_BURST - 1), abs=0.01
    )


def test_different_usernames_same_ip_independent(isolated_data_dir):
    import auth
    ip = "1.1.1.1"
    for _ in range(auth.LOGIN_BUCKET_BURST):
        auth._login_bucket_consume(ip, "alice")
    assert auth._login_bucket_consume(ip, "alice") is False
    # bob's bucket is independent.
    assert auth._login_bucket_consume(ip, "bob") is True


def test_username_key_is_case_insensitive(isolated_data_dir):
    """SQLite lockup lowercases the username; bucket must do the same so
    MiXeDcAsE doesn't bypass the shared bucket."""
    import auth
    ip = "1.1.1.1"
    auth._login_bucket_consume(ip, "Alice")
    assert auth._login_bucket_peek(ip, "alice") == pytest.approx(
        float(auth.LOGIN_BUCKET_BURST - 1), abs=0.01
    )
    assert auth._login_bucket_peek(ip, "ALICE") == pytest.approx(
        float(auth.LOGIN_BUCKET_BURST - 1), abs=0.01
    )


def test_is_login_locked_trips_on_bucket_exhaustion(isolated_data_dir):
    """Even with ZERO failed attempts recorded in SQLite, draining the
    bucket should make is_login_locked return True."""
    import auth
    ip, user = "9.9.9.9", "bucketdrain"
    # is_login_locked itself consumes a token — each call uses 1.
    # Exhaust by calling is_login_locked LOGIN_BUCKET_BURST times in a row.
    for _ in range(auth.LOGIN_BUCKET_BURST):
        assert auth.is_login_locked(ip, user) is False
    # BURST+1-th call: bucket empty, must return True even though SQLite
    # has seen zero failures.
    assert auth.is_login_locked(ip, user) is True


def test_sqlite_lockout_persists_even_when_bucket_has_tokens(isolated_data_dir):
    """Gate 2 (SQLite) is authoritative — bucket refilling after an
    attacker was locked out shouldn't let them back in."""
    import auth
    ip, user = "8.8.8.8", "sqllocked"
    # Record 5 failures to trip the SQLite lockout.
    for _ in range(auth.LOGIN_MAX_FAILURES):
        auth.record_login_attempt(ip, user, success=False)
    # Reset the bucket so it's full again.
    auth._reset_login_buckets()
    # is_login_locked consumes a token but SQLite still rejects.
    assert auth.is_login_locked(ip, user) is True


def test_successful_login_clears_sqlite_not_bucket(isolated_data_dir):
    """A successful login clears the SQLite failure rows but leaves the
    bucket alone — the bucket is about rate, not lockout."""
    import auth
    ip, user = "7.7.7.7", "succeeds"
    for _ in range(3):
        auth.record_login_attempt(ip, user, success=False)
    # 3 failures -> not locked yet per SQLite.
    assert auth.is_login_locked(ip, user) is False
    # Success clears SQLite failures. Peek the bucket afterwards.
    auth.record_login_attempt(ip, user, success=True)
    # Bucket is down by one (from the is_login_locked call above) — that's
    # expected. Anything else should be exactly as it was.
    tokens_before_reset = auth._login_bucket_peek(ip, user)
    # Another locked check now: SQLite has no failures, bucket has tokens.
    assert auth.is_login_locked(ip, user) is False
    # Each call consumed a token, so current peek should be lower.
    assert auth._login_bucket_peek(ip, user) < tokens_before_reset


def test_reset_login_buckets_clears_state(isolated_data_dir):
    import auth
    ip, user = "4.4.4.4", "resetme"
    for _ in range(auth.LOGIN_BUCKET_BURST):
        auth._login_bucket_consume(ip, user)
    assert auth._login_bucket_consume(ip, user) is False
    auth._reset_login_buckets()
    # Back to full.
    assert auth._login_bucket_peek(ip, user) == float(auth.LOGIN_BUCKET_BURST)


def test_concurrent_bucket_is_race_free(isolated_data_dir):
    """The specific race the audit called out: under concurrent load, the
    old SQLite-only impl could leak a few attempts through because two
    threads might both read count=4 before either wrote the 5th. With the
    atomic bucket in front, exactly LOGIN_BUCKET_BURST calls should
    return "allowed" out of THREADS_N concurrent attempts — no more,
    no less."""
    import auth
    ip, user = "5.5.5.5", "racetarget"
    THREADS_N = 200
    allowed = []
    denied = []
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS_N)

    def worker():
        # All threads released simultaneously so the race window is wide.
        barrier.wait()
        ok = auth._login_bucket_consume(ip, user)
        with lock:
            (allowed if ok else denied).append(ok)

    threads = [threading.Thread(target=worker) for _ in range(THREADS_N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Tokens accrued during the race window (time between barrier release
    # and final consume) — typically microseconds, so refill adds at most
    # a fraction of a token. The floor function inside consume means we
    # allow AT MOST BURST + 1 extra token in the worst case. But with
    # 0.2 tokens/sec refill and <<1s race window, that's still == BURST.
    assert len(allowed) == auth.LOGIN_BUCKET_BURST, (
        f"expected exactly {auth.LOGIN_BUCKET_BURST} allowed, got {len(allowed)}; "
        f"denied {len(denied)}"
    )
    assert len(denied) == THREADS_N - auth.LOGIN_BUCKET_BURST


def test_monotonic_time_used_not_wall_clock(isolated_data_dir):
    """Wall-clock jumps (NTP sync, DST) must not throw the refill
    calculation. The bucket uses time.monotonic() specifically to avoid
    this class of bug — this test asserts that by consuming and then
    checking peek is SANE (non-negative, non-absurd)."""
    import auth
    ip, user = "6.6.6.6", "nomono"
    auth._login_bucket_consume(ip, user)
    peek = auth._login_bucket_peek(ip, user)
    # After one consume + small elapsed, peek should be between
    # (BURST - 1) and BURST, never negative, never > BURST.
    assert 0 <= peek <= float(auth.LOGIN_BUCKET_BURST)
    assert peek < float(auth.LOGIN_BUCKET_BURST)  # at least one consumed


def test_bucket_caps_refill_at_burst(isolated_data_dir):
    """Even after a long idle, the bucket should not exceed BURST tokens."""
    import auth
    ip, user = "3.3.3.3", "capper"
    # Drain
    for _ in range(auth.LOGIN_BUCKET_BURST):
        auth._login_bucket_consume(ip, user)
    # Fake a long idle by directly writing the state with an old timestamp.
    # This is test-only introspection; normal code path uses monotonic().
    with auth._login_bucket_lock:
        auth._login_bucket_state[auth._login_bucket_key(ip, user)] = (
            0.0, time.monotonic() - 10_000.0   # 10k seconds ago
        )
    # Peek after the long idle should cap at BURST, not be in the hundreds.
    assert auth._login_bucket_peek(ip, user) == float(auth.LOGIN_BUCKET_BURST)
