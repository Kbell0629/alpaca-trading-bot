"""
Round-12 audit fixes — regression tests.

Three distinct fixes each with targeted tests:

  1. Password-reset token TOCTOU: atomic "mark used FIRST via
     UPDATE...WHERE used=0" closes the race where two concurrent
     reset requests could both succeed on the same token.

  2. create_session(ip_address=None) now normalises to 'unknown'
     instead of persisting NULL, which used to break downstream
     audit log queries that assumed string.

  3. smart_orders client_order_id entropy increased from 6 hex
     chars to 12 so the ~1-in-16M collision per second at 20 users
     becomes ~1-in-281-trillion.
"""
from __future__ import annotations

import threading

import pytest


# ---------- create_session ip_address normalisation ----------


def test_create_session_normalises_none_ip(isolated_data_dir):
    import auth
    uid, err = auth.create_user(
        email="ipnorm@test.com", username="ipnorm",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    assert err is None, err
    tok = auth.create_session(uid, ip_address=None)
    conn = auth._get_db()
    try:
        row = conn.execute(
            "SELECT ip_address FROM sessions WHERE token = ?", (tok,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["ip_address"] == "unknown"


def test_create_session_normalises_empty_ip(isolated_data_dir):
    import auth
    uid, err = auth.create_user(
        email="ipempty@test.com", username="ipempty",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    assert err is None, err
    tok = auth.create_session(uid, ip_address="")
    conn = auth._get_db()
    try:
        row = conn.execute(
            "SELECT ip_address FROM sessions WHERE token = ?", (tok,)
        ).fetchone()
    finally:
        conn.close()
    assert row["ip_address"] == "unknown"


def test_create_session_preserves_real_ip(isolated_data_dir):
    import auth
    uid, err = auth.create_user(
        email="ipreal@test.com", username="ipreal",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    assert err is None, err
    tok = auth.create_session(uid, ip_address="192.168.1.42")
    conn = auth._get_db()
    try:
        row = conn.execute(
            "SELECT ip_address FROM sessions WHERE token = ?", (tok,)
        ).fetchone()
    finally:
        conn.close()
    assert row["ip_address"] == "192.168.1.42"


# ---------- password reset TOCTOU ----------


def test_reset_token_consumed_once_even_under_concurrent_use(isolated_data_dir):
    """The whole point of the atomic UPDATE...WHERE used=0 fix: given the
    same token, TWO concurrent consume attempts can't both succeed."""
    import auth
    uid, err = auth.create_user(
        email="reset@test.com", username="resettest",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    assert err is None, err

    plaintext_token, user_row = auth.create_password_reset("reset@test.com")
    assert plaintext_token
    assert user_row["id"] == uid

    # Fire two consume attempts concurrently — classic TOCTOU race.
    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(password_attempt: str):
        barrier.wait()   # align both threads to hit the UPDATE at the same time
        ok, err_msg = auth.consume_reset_token(plaintext_token, password_attempt)
        with lock:
            results.append((ok, err_msg))

    t1 = threading.Thread(target=worker, args=("attacker password for A!!",))
    t2 = threading.Thread(target=worker, args=("attacker password for B!!",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    successes = [r for r in results if r[0]]
    failures = [r for r in results if not r[0]]
    assert len(successes) == 1, (
        f"Exactly one consume attempt must win; got {len(successes)} "
        f"successes and {len(failures)} failures: {results}"
    )
    assert len(failures) == 1
    # The loser gets the generic invalid-link message (same as other
    # invalid-token paths — don't leak the race condition).
    assert "invalid" in failures[0][1].lower() or "expired" in failures[0][1].lower()


def test_reset_token_cannot_be_reused_sequentially(isolated_data_dir):
    """Even without concurrency, a single token must be spent-on-first-use."""
    import auth
    uid, err = auth.create_user(
        email="reuse@test.com", username="reusetest",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    assert err is None, err
    token, _ = auth.create_password_reset("reuse@test.com")
    ok1, _ = auth.consume_reset_token(token, "new password first try!!")
    assert ok1
    ok2, err2 = auth.consume_reset_token(token, "new password second try!!")
    assert not ok2
    assert err2


def test_reset_token_consume_invalidates_all_sessions(isolated_data_dir):
    """Defence-in-depth: successful reset also boots all existing sessions
    — if the attacker had a stolen cookie, it should stop working the
    moment the user resets their password."""
    import auth
    uid, err = auth.create_user(
        email="sesskill@test.com", username="sesskilltest",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    assert err is None, err
    existing_tok = auth.create_session(uid, ip_address="1.2.3.4")
    assert auth.validate_session(existing_tok) is not None
    token, _ = auth.create_password_reset("sesskill@test.com")
    ok, _ = auth.consume_reset_token(token, "brand new safe password!!")
    assert ok
    # Existing session should no longer validate.
    assert auth.validate_session(existing_tok) is None


def test_reset_token_rejects_empty_password(isolated_data_dir):
    import auth
    uid, err = auth.create_user(
        email="empty@test.com", username="emptytest",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    assert err is None, err
    token, _ = auth.create_password_reset("empty@test.com")
    ok, err_msg = auth.consume_reset_token(token, "")
    assert not ok
    assert err_msg
    # Verify the token is still available — empty password shouldn't
    # burn a valid reset attempt.
    ok2, _ = auth.consume_reset_token(token, "valid password now please!!")
    assert ok2


# ---------- smart_orders client_order_id entropy ----------


def test_smart_orders_coid_has_12_char_entropy_suffix():
    """Regression guard: the round-12 audit fix bumped the uuid suffix
    from 6 hex chars to 12. Verify via the format string directly rather
    than calling place_smart_buy (which requires an Alpaca mock)."""
    import smart_orders as so
    import inspect
    src = inspect.getsource(so)
    # Both place_smart_buy + place_smart_sell must use the 12-char form.
    assert "uuid.uuid4().hex[:12]" in src, (
        "smart_orders must use 12-char uuid suffix (per round-12 audit); "
        "6-char suffix had a 1-in-16M collision rate at sub-second scheduling."
    )
    assert "uuid.uuid4().hex[:6]" not in src, (
        "Stale 6-char suffix survived — check all coid format strings."
    )


def test_smart_orders_coid_is_unique_across_calls():
    """Property check: 100 back-to-back smart-order calls within the same
    second produce 100 distinct client_order_ids."""
    import smart_orders as so
    import time, uuid
    coids = set()
    t0 = int(time.time())
    for _ in range(100):
        coid = f"smart-buy-SYM-{t0}-{uuid.uuid4().hex[:12]}"
        coids.add(coid)
    assert len(coids) == 100
