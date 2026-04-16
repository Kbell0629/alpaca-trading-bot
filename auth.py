#!/usr/bin/env python3
"""
Multi-user authentication module for the Alpaca trading bot.
SQLite-backed, stdlib only. Encrypts Alpaca credentials at rest.
"""
import os
import sqlite3
import hashlib
import secrets
import base64
import hmac
import json
from datetime import datetime, timezone, timedelta

# ET is the canonical timezone — market hours and user locale are both ET,
# so there is no reason for UTC to appear in any string this app emits.
# Stored ISO strings carry the ET offset (-04:00 EDT / -05:00 EST) and still
# compare correctly against any legacy UTC-stored rows because both are
# tz-aware.
try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone.utc  # safety fallback — should never trip on modern Python

def now_et():
    return datetime.now(ET_TZ)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent data lives. On Railway, set to a volume mount path.
# Locally, defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "users.db")
USERS_DIR = os.path.join(DATA_DIR, "users")

# Master encryption key from env — set on Railway, never in code.
# Production mode (REQUIRE_MASTER_KEY=1): fails closed if the key is missing.
# Dev mode (default when REQUIRE_MASTER_KEY unset): falls back to plaintext
# with a loud warning so local tests work without setup.
MASTER_KEY = os.environ.get("MASTER_ENCRYPTION_KEY", "")
_REQUIRE_MASTER_KEY = os.environ.get("REQUIRE_MASTER_KEY") == "1"
if not MASTER_KEY:
    if _REQUIRE_MASTER_KEY:
        raise RuntimeError(
            "MASTER_ENCRYPTION_KEY env var is required when REQUIRE_MASTER_KEY=1 "
            "but was not set. Refusing to start in plaintext-fallback mode. "
            "Either set MASTER_ENCRYPTION_KEY on the deployment or remove "
            "REQUIRE_MASTER_KEY=1 (not recommended for production)."
        )
    # Dev mode: print a loud warning at import time so this never slips
    import sys as _sys
    print(
        "[auth] WARNING: MASTER_ENCRYPTION_KEY is not set. Alpaca API secrets "
        "will be stored in plaintext prefixed 'PLAIN:'. Set MASTER_ENCRYPTION_KEY "
        "and REQUIRE_MASTER_KEY=1 in production.",
        file=_sys.stderr, flush=True,
    )

# Session duration
SESSION_DAYS = 30
RESET_TOKEN_HOURS = 1

def _get_db():
    """Get a connection with foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    """Create tables if they don't exist. Safe to call multiple times."""
    conn = _get_db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        alpaca_key_encrypted TEXT,
        alpaca_secret_encrypted TEXT,
        alpaca_endpoint TEXT DEFAULT 'https://paper-api.alpaca.markets/v2',
        alpaca_data_endpoint TEXT DEFAULT 'https://data.alpaca.markets/v2',
        ntfy_topic TEXT,
        notification_email TEXT,
        is_admin INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login TEXT,
        login_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        ip_address TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS password_resets (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        actor_user_id INTEGER,            -- NULL for system/automated actions
        actor_username TEXT,
        action TEXT NOT NULL,             -- e.g. 'deactivate_user', 'reset_password'
        target_user_id INTEGER,
        target_username TEXT,
        ip_address TEXT,
        detail TEXT                        -- JSON blob with additional context
    );
    CREATE TABLE IF NOT EXISTS login_attempts (
        -- Persistent login rate-limit storage so restart doesn't clear
        -- the brute-force lockout window. Pruned by _gc_login_attempts.
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        username TEXT NOT NULL,
        ts REAL NOT NULL,                 -- UNIX timestamp
        success INTEGER NOT NULL          -- 0 or 1
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
    CREATE INDEX IF NOT EXISTS idx_reset_token ON password_resets(token);
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON admin_audit_log(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_actor ON admin_audit_log(actor_user_id);
    CREATE INDEX IF NOT EXISTS idx_audit_target ON admin_audit_log(target_user_id);
    CREATE INDEX IF NOT EXISTS idx_login_attempts_key ON login_attempts(ip, username, ts);
    """)
    conn.commit()
    conn.close()
    os.makedirs(USERS_DIR, exist_ok=True)

# ===== Password Hashing =====
#
# OWASP 2023 guidance: PBKDF2-HMAC-SHA256 with ≥600k iterations.
# We bumped from 200k to 600k but need to remain backward-compat with
# existing users. Hashes now carry an iteration count prefix:
#   Format: "v1$ITERATIONS$BASE64HASH"   (e.g. "v1$600000$XXXX")
#   Legacy: "BASE64HASH"                 (implicit 200_000 iterations)
# verify_password handles both. On successful login with a legacy hash,
# the caller (auth.authenticate) re-hashes at the new cost and upgrades
# the stored record transparently.
PBKDF2_CURRENT_ITERATIONS = 600_000
PBKDF2_LEGACY_ITERATIONS = 200_000
PBKDF2_HASH_PREFIX = "v1$"


def _raw_pbkdf2(password, salt_bytes, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, iterations)


def hash_password(password, salt=None, iterations=None):
    """Return (hash_blob, salt_b64). The hash_blob is self-describing:
    "v1$<iterations>$<base64>". If salt not provided, generates one.
    """
    if salt is None:
        salt_bytes = secrets.token_bytes(16)
        salt = base64.b64encode(salt_bytes).decode()
    else:
        salt_bytes = base64.b64decode(salt)
    iters = iterations or PBKDF2_CURRENT_ITERATIONS
    hashed = _raw_pbkdf2(password, salt_bytes, iters)
    blob = f"{PBKDF2_HASH_PREFIX}{iters}${base64.b64encode(hashed).decode()}"
    return blob, salt


def _parse_hash_blob(blob):
    """Return (iterations, base64_hash). Legacy blobs are treated as 200k."""
    if blob and blob.startswith(PBKDF2_HASH_PREFIX):
        try:
            _, iter_str, b64 = blob.split("$", 2)
            return int(iter_str), b64
        except (ValueError, AttributeError):
            return PBKDF2_LEGACY_ITERATIONS, blob
    return PBKDF2_LEGACY_ITERATIONS, blob


def verify_password(password, stored_hash, stored_salt):
    """Timing-safe password verification. Works for both legacy (200k, no
    prefix) and current (v1$600000$...) hashes.
    """
    try:
        iters, b64 = _parse_hash_blob(stored_hash)
        salt_bytes = base64.b64decode(stored_salt)
        expected = _raw_pbkdf2(password, salt_bytes, iters)
        actual = base64.b64decode(b64)
        return hmac.compare_digest(expected, actual)
    except Exception:
        return False


def needs_rehash(stored_hash):
    """Return True if the stored hash uses an older iteration count and
    should be upgraded on next successful verification.
    """
    iters, _ = _parse_hash_blob(stored_hash)
    return iters < PBKDF2_CURRENT_ITERATIONS

# ===== Encryption (Fernet-like using stdlib) =====
# Uses HMAC-SHA256 for auth + simple XOR stream cipher derived from master key.
# Not as strong as AES-GCM, but stdlib-only and adequate when master key is secret.

def _derive_key(master, purpose):
    """Derive a sub-key for a specific purpose (encryption vs MAC)."""
    return hashlib.sha256(f"{master}:{purpose}".encode()).digest()

# Try to load cryptography (AES-GCM). Fall back to the legacy stdlib
# HMAC-stream cipher if unavailable — backward compat preserves decrypt
# for any existing "ENC:" ciphertexts and prevents deployments where
# cryptography fails to install from being bricked.
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AESGCM = True
except Exception:
    _HAS_AESGCM = False


def _derive_aesgcm_key(master):
    """Derive a 32-byte AES-256 key from the master secret."""
    return hashlib.sha256(f"{master}:aesgcm-v2".encode()).digest()


def encrypt_secret(plaintext):
    """Encrypt a secret (Alpaca API key).

    Format tiers (encoded as prefix):
      "PLAIN:..."  -> dev-mode cleartext fallback (no MASTER_KEY set)
      "ENC:..."    -> legacy HMAC-stream cipher (encrypt-then-MAC)
      "ENCv2:..."  -> AES-256-GCM (authenticated, IETF standard)
    New writes use ENCv2 when cryptography is available; decrypt supports
    all three so existing DB rows keep working without migration.
    """
    if not plaintext:
        return ""
    if not MASTER_KEY:
        return "PLAIN:" + plaintext
    if _HAS_AESGCM:
        key = _derive_aesgcm_key(MASTER_KEY)
        aes = AESGCM(key)
        nonce = secrets.token_bytes(12)  # 96-bit nonce is standard for GCM
        ct = aes.encrypt(nonce, plaintext.encode(), associated_data=b"alpaca-cred-v2")
        # Store nonce || ciphertext+tag (the cryptography lib appends the 16-byte tag)
        return "ENCv2:" + base64.b64encode(nonce + ct).decode()
    # Fallback: legacy HMAC stream cipher
    enc_key = _derive_key(MASTER_KEY, "encrypt")
    mac_key = _derive_key(MASTER_KEY, "mac")
    nonce = secrets.token_bytes(16)
    plaintext_bytes = plaintext.encode()
    blocks_needed = (len(plaintext_bytes) + 31) // 32
    keystream = b""
    for i in range(blocks_needed):
        counter = nonce + i.to_bytes(16, "big")
        keystream += hmac.new(enc_key, counter, hashlib.sha256).digest()
    ciphertext = bytes(a ^ b for a, b in zip(plaintext_bytes, keystream[:len(plaintext_bytes)]))
    mac = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    combined = nonce + mac + ciphertext
    return "ENC:" + base64.b64encode(combined).decode()


def decrypt_secret(encrypted):
    """Decrypt a secret back to plaintext. Supports PLAIN / ENC / ENCv2."""
    if not encrypted:
        return ""
    if encrypted.startswith("PLAIN:"):
        return encrypted[6:]

    # ENCv2: AES-GCM (current format)
    if encrypted.startswith("ENCv2:"):
        if not MASTER_KEY or not _HAS_AESGCM:
            return ""
        try:
            blob = base64.b64decode(encrypted[6:])
            nonce, ct = blob[:12], blob[12:]
            key = _derive_aesgcm_key(MASTER_KEY)
            aes = AESGCM(key)
            pt = aes.decrypt(nonce, ct, associated_data=b"alpaca-cred-v2")
            return pt.decode()
        except Exception:
            return ""

    # ENC: legacy HMAC stream cipher
    if encrypted.startswith("ENC:"):
        if not MASTER_KEY:
            return ""
        try:
            combined = base64.b64decode(encrypted[4:])
            nonce = combined[:16]
            mac_stored = combined[16:48]
            ciphertext = combined[48:]
            mac_key = _derive_key(MASTER_KEY, "mac")
            mac_check = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
            if not hmac.compare_digest(mac_stored, mac_check):
                return ""
            enc_key = _derive_key(MASTER_KEY, "encrypt")
            blocks_needed = (len(ciphertext) + 31) // 32
            keystream = b""
            for i in range(blocks_needed):
                counter = nonce + i.to_bytes(16, "big")
                keystream += hmac.new(enc_key, counter, hashlib.sha256).digest()
            plaintext = bytes(a ^ b for a, b in zip(ciphertext, keystream[:len(ciphertext)]))
            return plaintext.decode()
        except Exception:
            return ""

    # Legacy plaintext (pre-encryption deploys)
    return encrypted


def needs_cipher_upgrade(encrypted):
    """Return True if the ciphertext is in an older format that should be
    re-encrypted under the current (ENCv2) scheme on next opportunity.
    """
    if not encrypted:
        return False
    if encrypted.startswith("PLAIN:"):
        return bool(MASTER_KEY) and _HAS_AESGCM
    if encrypted.startswith("ENC:"):
        return _HAS_AESGCM
    return False

# ===== User CRUD =====
def create_user(email, username, password, alpaca_key, alpaca_secret,
                alpaca_endpoint="https://paper-api.alpaca.markets/v2",
                alpaca_data_endpoint="https://data.alpaca.markets/v2",
                ntfy_topic=None, notification_email=None, is_admin=False):
    """Create a new user. Returns (user_id, error).

    Enforces the same username regex as the signup form — previously only the
    HTTP handler validated, so callers like bootstrap_from_env() could inject
    arbitrary chars. With usernames rendered in JS template strings, an
    unescaped quote would break out of the string literal.
    """
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_-]{3,30}$", username or ""):
        return None, "Username must be 3-30 chars (letters, digits, _, -)"
    if not email or "@" not in email:
        return None, "Invalid email"
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters"
    try:
        conn = _get_db()
        cur = conn.cursor()
        # Check if this will be the first user (auto-admin)
        cur.execute("SELECT COUNT(*) FROM users")
        is_first = cur.fetchone()[0] == 0
        if is_first:
            is_admin = True

        pwhash, salt = hash_password(password)
        now = now_et().isoformat()

        cur.execute("""
            INSERT INTO users (email, username, password_hash, password_salt,
                              alpaca_key_encrypted, alpaca_secret_encrypted,
                              alpaca_endpoint, alpaca_data_endpoint,
                              ntfy_topic, notification_email,
                              is_admin, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (email.lower(), username, pwhash, salt,
              encrypt_secret(alpaca_key), encrypt_secret(alpaca_secret),
              alpaca_endpoint, alpaca_data_endpoint,
              ntfy_topic or f"alpaca-bot-{username.lower()}",
              notification_email or email,
              1 if is_admin else 0, now))
        user_id = cur.lastrowid
        conn.commit()

        # Create user data directory
        user_dir = os.path.join(USERS_DIR, str(user_id))
        os.makedirs(os.path.join(user_dir, "strategies"), exist_ok=True)

        return user_id, None
    except sqlite3.IntegrityError as e:
        msg = str(e)
        if "users.email" in msg:
            return None, "Email already registered"
        if "users.username" in msg:
            return None, "Username already taken"
        return None, "User already exists"
    except Exception as e:
        return None, str(e)
    finally:
        try: conn.close()
        except: pass

def get_user_by_id(user_id):
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_by_username(username):
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?) AND is_active = 1", (username,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_by_email(email):
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?) AND is_active = 1", (email,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def list_active_users():
    """Return all active users (for cloud scheduler iteration)."""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE is_active = 1")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def update_user_credentials(user_id, alpaca_key=None, alpaca_secret=None,
                            alpaca_endpoint=None, alpaca_data_endpoint=None,
                            ntfy_topic=None, notification_email=None):
    """Update user's Alpaca/notification settings."""
    conn = _get_db()
    cur = conn.cursor()
    updates = []
    params = []
    if alpaca_key is not None:
        updates.append("alpaca_key_encrypted = ?"); params.append(encrypt_secret(alpaca_key))
    if alpaca_secret is not None:
        updates.append("alpaca_secret_encrypted = ?"); params.append(encrypt_secret(alpaca_secret))
    if alpaca_endpoint is not None:
        updates.append("alpaca_endpoint = ?"); params.append(alpaca_endpoint)
    if alpaca_data_endpoint is not None:
        updates.append("alpaca_data_endpoint = ?"); params.append(alpaca_data_endpoint)
    if ntfy_topic is not None:
        updates.append("ntfy_topic = ?"); params.append(ntfy_topic)
    if notification_email is not None:
        updates.append("notification_email = ?"); params.append(notification_email)
    if not updates:
        conn.close()
        return False
    params.append(user_id)
    cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    return True

def change_password(user_id, new_password, invalidate_sessions=True):
    """Update a user's password. By default invalidates all existing sessions
    so a compromised cookie can't be used after the user changes their
    password. `consume_reset_token` already does this separately.
    """
    conn = _get_db()
    cur = conn.cursor()
    pwhash, salt = hash_password(new_password)
    cur.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pwhash, salt, user_id))
    if invalidate_sessions:
        cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True

def authenticate(username_or_email, password):
    """Return user dict if credentials valid, None otherwise.

    Transparently upgrades legacy PBKDF2 hashes (200k) to the current cost
    (600k) on successful verification, so password strengths improve across
    the user base without requiring manual password resets.

    Timing-attack mitigation: when the user doesn't exist, we still run a
    dummy PBKDF2 with the same iteration count. Without this, an attacker
    timing /api/login responses could enumerate registered usernames/emails
    (~ms for non-existent, ~200-400ms for existing).
    """
    user = get_user_by_username(username_or_email) or get_user_by_email(username_or_email)
    if not user:
        # Dummy PBKDF2 so timing matches the "user exists, wrong password"
        # path. Uses a fixed dummy salt — doesn't matter, we discard result.
        _DUMMY_SALT = "AAAAAAAAAAAAAAAAAAAAAAAA"  # 16 bytes base64
        try:
            _raw_pbkdf2(password, base64.b64decode(_DUMMY_SALT), PBKDF2_CURRENT_ITERATIONS)
        except Exception:
            pass
        return None
    if not verify_password(password, user["password_hash"], user["password_salt"]):
        return None
    # Upgrade hash if it's still the legacy iteration count
    try:
        if needs_rehash(user["password_hash"]):
            new_hash, new_salt = hash_password(password)
            conn = _get_db()
            cur = conn.cursor()
            cur.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
                        (new_hash, new_salt, user["id"]))
            conn.commit()
            conn.close()
    except Exception as e:
        # Rehash is best-effort, never fail login over it — but DO surface
        # the error so a persistent rehash failure doesn't hide silently.
        print(f"[auth] PBKDF2 rehash failed for user {user.get('id')}: {e}", flush=True)

    # Upgrade Alpaca credentials to AES-GCM (ENCv2) if they're still on
    # the legacy cipher. Transparent — user doesn't notice, next scheduler
    # tick decrypts under the new format.
    try:
        updates = []
        params = []
        for col in ("alpaca_key_encrypted", "alpaca_secret_encrypted"):
            current = user.get(col)
            if current and needs_cipher_upgrade(current):
                plaintext = decrypt_secret(current)
                if plaintext:
                    updates.append(f"{col} = ?")
                    params.append(encrypt_secret(plaintext))
        if updates:
            conn = _get_db()
            cur = conn.cursor()
            params.append(user["id"])
            cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[auth] cipher upgrade failed for user {user.get('id')}: {e}", flush=True)
    # Update last login
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_login = ?, login_count = login_count + 1 WHERE id = ?",
                (now_et().isoformat(), user["id"]))
    conn.commit()
    conn.close()
    return user

def get_user_alpaca_creds(user_id):
    """Return decrypted Alpaca credentials for a user."""
    user = get_user_by_id(user_id)
    if not user:
        return None
    return {
        "key": decrypt_secret(user.get("alpaca_key_encrypted", "")),
        "secret": decrypt_secret(user.get("alpaca_secret_encrypted", "")),
        "endpoint": user.get("alpaca_endpoint"),
        "data_endpoint": user.get("alpaca_data_endpoint"),
        "ntfy_topic": user.get("ntfy_topic"),
        "notification_email": user.get("notification_email"),
    }

# ===== Sessions =====
def create_session(user_id, ip_address=None):
    """Create a new session. Returns session token."""
    token = secrets.token_urlsafe(48)
    now = now_et()
    expires = now + timedelta(days=SESSION_DAYS)
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (token, user_id, created_at, expires_at, ip_address)
        VALUES (?, ?, ?, ?, ?)
    """, (token, user_id, now.isoformat(), expires.isoformat(), ip_address))
    conn.commit()
    conn.close()
    return token

def validate_session(token):
    """Return user dict if session valid, None otherwise."""
    if not token:
        return None
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.* FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token = ? AND s.expires_at > ? AND u.is_active = 1
    """, (token, now_et().isoformat()))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_session(token):
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()

def cleanup_expired_sessions():
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE expires_at < ?", (now_et().isoformat(),))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted

# ===== Password Reset =====
# Reset tokens are returned to the user in plaintext (via email / UI) but
# stored as SHA-256 hashes in the DB. Defense-in-depth: if the users.db
# file is leaked (backup mis-handled, Railway volume snapshot shared, etc.),
# an attacker can't immediately reset any user's password from the dump.
# They'd need to reverse the hash of an active 1-hour window token, which
# for a 32-byte urlsafe token is infeasible.
def _hash_reset_token(tok):
    return hashlib.sha256((tok or "").encode()).hexdigest()


def create_password_reset(email):
    """Create a reset token. Returns (token, user) or (None, None).
    The DB stores only the hash; the plaintext is returned once to the caller
    so it can be emailed/displayed, then discarded."""
    user = get_user_by_email(email)
    if not user:
        return None, None
    token = secrets.token_urlsafe(32)
    token_hash = _hash_reset_token(token)
    now = now_et()
    expires = now + timedelta(hours=RESET_TOKEN_HOURS)
    conn = _get_db()
    cur = conn.cursor()
    # Invalidate previous tokens
    cur.execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (user["id"],))
    cur.execute("""
        INSERT INTO password_resets (token, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
    """, (token_hash, user["id"], now.isoformat(), expires.isoformat()))
    conn.commit()
    conn.close()
    return token, user

def validate_reset_token(token):
    """Return user_id if token valid and unused, None otherwise.
    Accepts the plaintext token (hashes it to look up). Backward-compatible
    with any legacy plaintext tokens still in-flight at deploy time."""
    if not token:
        return None
    token_hash = _hash_reset_token(token)
    conn = _get_db()
    cur = conn.cursor()
    now_iso = now_et().isoformat()
    # Try hashed-token row first; fall back to legacy plaintext row for any
    # tokens issued before this change (1hr TTL so the window is brief).
    cur.execute("""
        SELECT user_id FROM password_resets
        WHERE token = ? AND used = 0 AND expires_at > ?
    """, (token_hash, now_iso))
    row = cur.fetchone()
    if not row:
        cur.execute("""
            SELECT user_id FROM password_resets
            WHERE token = ? AND used = 0 AND expires_at > ?
        """, (token, now_iso))
        row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def consume_reset_token(token, new_password):
    """Use a reset token to set a new password. Returns True on success."""
    user_id = validate_reset_token(token)
    if not user_id:
        return False
    change_password(user_id, new_password)
    token_hash = _hash_reset_token(token)
    conn = _get_db()
    cur = conn.cursor()
    # Mark both the hashed row and any legacy plaintext row as used.
    cur.execute("UPDATE password_resets SET used = 1 WHERE token IN (?, ?)",
                (token_hash, token))
    # Also invalidate all existing sessions for security
    cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True

# ===== Per-user paths =====
def user_data_dir(user_id):
    """Return the data directory for a user."""
    d = os.path.join(USERS_DIR, str(user_id))
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "strategies"), exist_ok=True)
    return d

def user_file(user_id, filename):
    """Return path to a user-scoped file."""
    return os.path.join(user_data_dir(user_id), filename)

# ===== Bootstrap =====
def bootstrap_from_env():
    """If no users exist, create one from environment variables (backward compat)."""
    init_db()
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    count = cur.fetchone()[0]
    conn.close()
    if count > 0:
        return None  # Already have users
    # Check for env var credentials
    legacy_user = os.environ.get("DASHBOARD_USER", "")
    legacy_pass = os.environ.get("DASHBOARD_PASS", "")
    legacy_key = os.environ.get("ALPACA_API_KEY", "")
    legacy_secret = os.environ.get("ALPACA_API_SECRET", "")
    legacy_email = os.environ.get("NOTIFICATION_EMAIL", "")
    legacy_ntfy = os.environ.get("NTFY_TOPIC", "")
    if legacy_user and legacy_pass and legacy_key and legacy_secret:
        user_id, err = create_user(
            email=legacy_email or f"{legacy_user}@localhost",
            username=legacy_user,
            password=legacy_pass,
            alpaca_key=legacy_key,
            alpaca_secret=legacy_secret,
            ntfy_topic=legacy_ntfy,
            notification_email=legacy_email,
            is_admin=True
        )
        if user_id:
            return user_id
    return None

# ===== Login Attempt Rate Limiting (persistent) =====
LOGIN_WINDOW_SEC = 15 * 60
LOGIN_MAX_FAILURES = 5


def record_login_attempt(ip, username, success):
    """Record a login attempt persistently. Survives server restart."""
    try:
        import time as _time
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO login_attempts (ip, username, ts, success) VALUES (?, ?, ?, ?)",
            (ip or "unknown", (username or "").lower(), _time.time(), 1 if success else 0),
        )
        # On success, purge that pair's prior failures so lockout clears
        if success:
            cur.execute(
                "DELETE FROM login_attempts WHERE ip = ? AND username = ? AND success = 0",
                (ip or "unknown", (username or "").lower()),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        # Audit-style helpers must never break the calling flow
        print(f"[auth] record_login_attempt failed: {e}", flush=True)


def is_login_locked(ip, username):
    """Return True if the (ip, username) pair is currently locked out due
    to too many failures in the last LOGIN_WINDOW_SEC seconds.
    """
    try:
        import time as _time
        cutoff = _time.time() - LOGIN_WINDOW_SEC
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE ip = ? AND username = ? AND success = 0 AND ts >= ?",
            (ip or "unknown", (username or "").lower(), cutoff),
        )
        count = cur.fetchone()[0]
        conn.close()
        return count >= LOGIN_MAX_FAILURES
    except Exception as e:
        print(f"[auth] is_login_locked check failed: {e}", flush=True)
        return False  # fail open on DB error so legitimate users aren't locked out


def gc_login_attempts():
    """Delete login_attempts rows older than 24 hours. Called periodically
    from server side to keep table small.
    """
    try:
        import time as _time
        cutoff = _time.time() - 86400
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM login_attempts WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted
    except Exception:
        return 0


# Retain 90 days of admin audit log; older rows are pruned. 90d covers a full
# quarter so forensic questions ("who reset whose password last quarter")
# remain answerable, but the table can't grow unbounded.
AUDIT_RETENTION_DAYS = 90

def gc_audit_log():
    """Delete admin_audit_log rows older than AUDIT_RETENTION_DAYS.
    Called opportunistically from the same hook as gc_login_attempts."""
    try:
        cutoff = (now_et() - timedelta(days=AUDIT_RETENTION_DAYS)).isoformat()
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM admin_audit_log WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted
    except Exception:
        return 0


def gc_password_resets():
    """Delete expired or used password reset tokens.
    Prevents unbounded table growth and reduces leak surface."""
    try:
        cutoff = now_et().isoformat()
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM password_resets WHERE expires_at < ? OR used = 1",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted
    except Exception:
        return 0


# ===== Admin Audit Log =====
def log_admin_action(action, actor=None, target_user_id=None, ip_address=None, detail=None):
    """Record an admin/auth action to the audit log.

    `action` is a short machine-readable string:
      login_success, login_failed, signup, password_changed, password_reset,
      deactivate_user, reactivate_user, admin_reset_password, account_deleted.
    `actor` is a user dict (for authenticated actions) or None (for anonymous
    events like login_failed).
    `detail` is a JSON-serializable dict with action-specific context.
    """
    try:
        conn = _get_db()
        cur = conn.cursor()
        # Resolve target username if we only have the id
        target_username = None
        if target_user_id and actor and actor.get("id") == target_user_id:
            target_username = actor.get("username")
        elif target_user_id:
            cur.execute("SELECT username FROM users WHERE id = ?", (target_user_id,))
            row = cur.fetchone()
            if row:
                target_username = row[0]
        cur.execute("""
            INSERT INTO admin_audit_log
            (ts, actor_user_id, actor_username, action,
             target_user_id, target_username, ip_address, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_et().isoformat(),
            actor.get("id") if actor else None,
            actor.get("username") if actor else None,
            action,
            target_user_id,
            target_username,
            ip_address,
            json.dumps(detail) if detail is not None else None,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        # Audit log failure must NEVER break the action itself.
        print(f"[audit] WARN failed to record '{action}': {e}", flush=True)


def list_audit_log(limit=200, action_filter=None, user_id_filter=None):
    """Return recent audit log entries, newest first."""
    conn = _get_db()
    cur = conn.cursor()
    sql = "SELECT * FROM admin_audit_log WHERE 1=1"
    params = []
    if action_filter:
        sql += " AND action = ?"
        params.append(action_filter)
    if user_id_filter:
        sql += " AND (actor_user_id = ? OR target_user_id = ?)"
        params.extend([user_id_filter, user_id_filter])
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    # Parse detail JSON for convenience
    for r in rows:
        if r.get("detail"):
            try:
                r["detail"] = json.loads(r["detail"])
            except Exception:
                pass
    return rows


if __name__ == "__main__":
    init_db()
    bootstrap_from_env()
    print(f"Users: {len(list_active_users())}")
    for u in list_active_users():
        print(f"  {u['id']}: {u['username']} ({u['email']}) admin={u['is_admin']}")
