"""Auth-layer tests: hashing, encryption, sessions, reset tokens, rate limit."""
import pytest


def test_hash_password_roundtrip(isolated_data_dir):
    import auth
    h, salt = auth.hash_password("correct horse battery staple!!")
    assert h.startswith("v1$600000$"), "current format should use 600k iterations"
    assert auth.verify_password("correct horse battery staple!!", h, salt)
    assert not auth.verify_password("wrong password", h, salt)


def test_hash_legacy_200k_still_verifies(isolated_data_dir):
    import auth
    # Simulate a pre-v1 stored hash (bare base64, no prefix, 200k iters)
    h, salt = auth.hash_password("hunter22 passphrase", iterations=auth.PBKDF2_LEGACY_ITERATIONS)
    # Strip the prefix to imitate the legacy-format-in-DB scenario
    legacy = h.split("$", 2)[-1]
    assert auth.verify_password("hunter22 passphrase", legacy, salt)
    assert auth.needs_rehash(legacy), "legacy hash should flag for upgrade"


def test_encrypt_decrypt_roundtrip_encv3(isolated_data_dir):
    import auth
    ct = auth.encrypt_secret("FAKE-TEST-KEY-NOT-A-REAL-SECRET")
    assert ct.startswith("ENCv3:"), f"new writes must be ENCv3, got: {ct[:8]}"
    assert auth.decrypt_secret(ct) == "FAKE-TEST-KEY-NOT-A-REAL-SECRET"


def test_encv2_still_decrypts_backward_compat(isolated_data_dir):
    import auth
    # Force an ENCv2 write by using the old derivation manually, then
    # confirm the current decrypt_secret picks the right path.
    if not auth._HAS_AESGCM:
        pytest.skip("cryptography not installed")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64, secrets
    key = auth._derive_aesgcm_key(auth.MASTER_KEY)
    nonce = secrets.token_bytes(12)
    aes = AESGCM(key)
    ct = aes.encrypt(nonce, b"legacy-secret", associated_data=b"alpaca-cred-v2")
    blob = "ENCv2:" + base64.b64encode(nonce + ct).decode()
    assert auth.decrypt_secret(blob) == "legacy-secret"
    assert auth.needs_cipher_upgrade(blob), "ENCv2 should be flagged for upgrade to ENCv3"


def test_password_strength_rejects_weak(isolated_data_dir):
    import auth
    ok, msg = auth.check_password_strength("password")
    assert not ok, "'password' must be rejected"
    ok, msg = auth.check_password_strength("Aa1!")
    assert not ok, "short password must be rejected"
    ok, msg = auth.check_password_strength("correct horse battery staple!!")
    assert ok, f"passphrase should pass: got {msg!r}"


def test_create_user_and_authenticate(isolated_data_dir):
    import auth
    uid, err = auth.create_user(
        email="test@example.com",
        username="testuser",
        password="correct horse battery staple!!",
        alpaca_key="test-key",
        alpaca_secret="test-secret",
    )
    assert uid and not err, f"create_user failed: {err}"

    # First user is auto-admin
    user = auth.get_user_by_id(uid)
    assert user["is_admin"] == 1

    # New signups get a random ntfy topic, not "alpaca-bot-testuser"
    assert user["ntfy_topic"] != "alpaca-bot-testuser"
    assert user["ntfy_topic"].startswith("alpaca-bot-")
    assert len(user["ntfy_topic"]) > len("alpaca-bot-testuser")

    # Credentials encrypted
    assert user["alpaca_key_encrypted"].startswith("ENCv3:")
    creds = auth.get_user_alpaca_creds(uid)
    assert creds["key"] == "test-key" and creds["secret"] == "test-secret"

    # Authenticate round-trip
    u = auth.authenticate("testuser", "correct horse battery staple!!")
    assert u and u["id"] == uid
    assert auth.authenticate("testuser", "wrong") is None
    assert auth.authenticate("nope@example.com", "wrong") is None


def test_session_lifecycle(isolated_data_dir):
    import auth
    uid, _ = auth.create_user(
        email="s@example.com", username="sessuser",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    tok = auth.create_session(uid, ip_address="1.2.3.4")
    assert auth.validate_session(tok)["id"] == uid
    auth.delete_session(tok)
    assert auth.validate_session(tok) is None


def test_reset_token_hashed_at_rest(isolated_data_dir):
    import auth, sqlite3
    auth.create_user(email="r@example.com", username="resetuser",
                     password="correct horse battery staple!!",
                     alpaca_key="k", alpaca_secret="s")
    tok, user = auth.create_password_reset("r@example.com")
    assert tok and user

    # The RAW token must NOT be present in the DB row
    conn = sqlite3.connect(auth.DB_PATH)
    row = conn.execute("SELECT token FROM password_resets").fetchone()
    conn.close()
    assert row[0] != tok, "token stored plaintext — should be SHA-256 hash"
    assert row[0] == auth._hash_reset_token(tok), "stored value != SHA-256 of token"

    # But validate_reset_token accepts the plaintext token (hashes internally)
    assert auth.validate_reset_token(tok) == user["id"]


def test_rate_limit_locks_after_5_failures(isolated_data_dir):
    import auth
    auth.create_user(email="rl@example.com", username="rluser",
                     password="correct horse battery staple!!",
                     alpaca_key="k", alpaca_secret="s")
    ip = "9.9.9.9"
    for _ in range(4):
        auth.record_login_attempt(ip, "rluser", success=False)
    assert not auth.is_login_locked(ip, "rluser")
    auth.record_login_attempt(ip, "rluser", success=False)
    assert auth.is_login_locked(ip, "rluser"), "5 failures should trip lockout"

    # Success clears the failures
    auth.record_login_attempt(ip, "rluser", success=True)
    assert not auth.is_login_locked(ip, "rluser")


def test_gc_functions_do_not_raise(isolated_data_dir):
    import auth
    # All four GC helpers should return without error on an empty DB.
    for fn in ("gc_login_attempts", "cleanup_expired_sessions",
               "gc_audit_log", "gc_password_resets"):
        result = getattr(auth, fn)()
        assert isinstance(result, int), f"{fn} should return rowcount int"


def test_count_legacy_encrypted_rows(isolated_data_dir):
    import auth
    auth.create_user(email="l@example.com", username="leguser",
                     password="correct horse battery staple!!",
                     alpaca_key="k", alpaca_secret="s")
    counts = auth.count_legacy_encrypted_rows()
    assert isinstance(counts, dict)
    assert counts.get("ENCv3", 0) >= 1, "new user should be on ENCv3"
    assert counts.get("ENC", 0) == 0 and counts.get("ENCv2", 0) == 0
