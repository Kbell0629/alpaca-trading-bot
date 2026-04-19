#!/usr/bin/env python3
"""
Multi-user authentication module for the Alpaca trading bot.
SQLite-backed, stdlib only. Encrypts Alpaca credentials at rest.
"""
import logging
import os
import sqlite3
import hashlib
import secrets
import base64
import hmac
import json
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

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
# The plaintext-fallback mode (PLAIN: prefix for writes when MASTER_KEY was
# unset) has been retired. Startup logs confirm all existing rows are on
# ENCv3, so there is no longer a reason to tolerate a missing key: a silent
# downgrade to plaintext is strictly worse than failing loud and letting the
# operator fix their environment. Decryption of legacy PLAIN: rows (if any
# ever existed) still works — only the WRITE path requires a key.
MASTER_KEY = os.environ.get("MASTER_ENCRYPTION_KEY", "")
if not MASTER_KEY:
    raise RuntimeError(
        "MASTER_ENCRYPTION_KEY env var is required. Plaintext-fallback mode "
        "(PLAIN: prefix for stored credentials) has been retired. Set "
        "MASTER_ENCRYPTION_KEY on the deployment or in your local .env file. "
        "For a fresh dev env: generate one with "
        "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`."
    )

# Session duration
SESSION_DAYS = 30
RESET_TOKEN_HOURS = 1

def _get_db():
    """Get a connection with foreign keys + WAL + busy_timeout.

    Round-11 audit: prior impl relied on server.py setting WAL at
    startup on a one-shot connection, which doesn't stick —
    journal_mode is per-DB but busy_timeout is per-connection, and
    each call here created a fresh default-timeout connection. Under
    concurrent writers (scheduler thread + HTTP handler + backup
    subprocess) any writer blocked >0 seconds threw SQLITE_BUSY,
    silently losing the login/session/audit write."""
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
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
    # Round-11 live-trading: add paper/live dual-credential columns + live toggle.
    # Idempotent — PRAGMA table_info check before ALTER TABLE.
    _existing = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    for col, ddl in [
        ("alpaca_live_key_encrypted", "ALTER TABLE users ADD COLUMN alpaca_live_key_encrypted TEXT"),
        ("alpaca_live_secret_encrypted", "ALTER TABLE users ADD COLUMN alpaca_live_secret_encrypted TEXT"),
        ("live_mode", "ALTER TABLE users ADD COLUMN live_mode INTEGER DEFAULT 0"),
        ("live_enabled_at", "ALTER TABLE users ADD COLUMN live_enabled_at TEXT"),
        ("live_max_position_dollars", "ALTER TABLE users ADD COLUMN live_max_position_dollars REAL DEFAULT 500"),
        ("track_record_public", "ALTER TABLE users ADD COLUMN track_record_public INTEGER DEFAULT 0"),
        ("scorecard_email_enabled", "ALTER TABLE users ADD COLUMN scorecard_email_enabled INTEGER DEFAULT 0"),
    ]:
        if col not in _existing:
            try:
                cur.execute(ddl)
            except Exception as _e:
                log.warning("auth: DDL migration failed",
                            extra={"column": col, "error": str(_e)})
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

# ===== Encryption (AES-256-GCM via cryptography pip dep) =====
# Round-7 cleanup: the pre-AES-GCM HMAC-stream cipher (_derive_key helper
# and its ENC: decrypt/encrypt paths) was removed after Railway confirmed
# 0 credentials remained on that format. Current encryption path is
# ENCv3 (AES-GCM with HKDF key derivation); ENCv2 kept decrypt-only for
# one more deploy cycle as a safety net.

# Try to load cryptography (AES-GCM). Required for the encrypt path —
# PLAIN-fallback was retired along with MASTER_KEY-fallback; the module
# now raises at import if the key is missing, and encrypt_secret raises
# if AES-GCM is unavailable.
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AESGCM = True
except Exception:
    _HAS_AESGCM = False

# zxcvbn password strength estimator — optional pip dep. Silently falls
# back to the 8-char minimum if not installed so dev envs aren't blocked.
try:
    from zxcvbn import zxcvbn as _zxcvbn
    _HAS_ZXCVBN = True
except Exception:
    _HAS_ZXCVBN = False


# Minimum zxcvbn strength score (0-4). 3 = "safely unguessable without
# throttling" per zxcvbn's scoring guide — blocks "password", keyboard
# walks, common leaked passwords, but permits reasonable passphrases
# (e.g. "correct horse battery" scores 4).
MIN_PASSWORD_SCORE = 3


def check_password_strength(password, user_inputs=None):
    """Return (ok, message). `user_inputs` is a list of username/email
    fragments that should not appear in the password (zxcvbn weights these
    heavily).

    Policy:
      - at least 10 characters
      - zxcvbn score >= MIN_PASSWORD_SCORE when zxcvbn is installed
      - if zxcvbn not installed (dev env without deps), falls back to 8 chars
    """
    if not password:
        return False, "Password required"
    min_len = 10 if _HAS_ZXCVBN else 8
    if len(password) < min_len:
        return False, f"Password must be at least {min_len} characters"
    if not _HAS_ZXCVBN:
        return True, None
    try:
        result = _zxcvbn(password, user_inputs=user_inputs or [])
    except Exception:
        return True, None  # don't fail-closed on zxcvbn internal error
    score = int(result.get("score", 0))
    if score < MIN_PASSWORD_SCORE:
        fb = result.get("feedback") or {}
        warn = fb.get("warning") or "Password is too guessable."
        suggestions = fb.get("suggestions") or []
        hint = (suggestions[0] if suggestions else
                "Try a passphrase like four random words.")
        return False, f"{warn} {hint}"
    return True, None


def _derive_aesgcm_key(master):
    """Derive a 32-byte AES-256 key from the master secret (ENCv2 legacy).

    Used only for decrypting rows written before the ENCv3 (HKDF) rollout.
    New encryptions go through _derive_aesgcm_key_v3.
    """
    return hashlib.sha256(f"{master}:aesgcm-v2".encode()).digest()


# HKDF-SHA256 for ENCv3. Single-SHA256 derivation (v2) was fine as long as
# MASTER_KEY wasn't compromised, but HKDF gives strict extract-then-expand
# guarantees and allows per-purpose keying without re-using the derivation
# (e.g. if we later need separate keys for different credential types).
# Info string must be stable — changing it invalidates every ENCv3 row.
_ENCv3_SALT = b"alpaca-trading-bot/credential-kdf/v3"
_ENCv3_INFO = b"aesgcm-256-alpaca-creds"


def _derive_aesgcm_key_v3(master):
    """HKDF-SHA256(salt, MASTER_KEY, info) → 32-byte AES-256 key."""
    # Try to use the `cryptography` HKDF primitive when available; fall
    # back to a stdlib HKDF implementation (RFC 5869) so the code works
    # even in the unlikely case cryptography install is incomplete.
    try:
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes as _hashes
        hkdf = HKDF(
            algorithm=_hashes.SHA256(),
            length=32,
            salt=_ENCv3_SALT,
            info=_ENCv3_INFO,
        )
        return hkdf.derive(master.encode() if isinstance(master, str) else master)
    except Exception:
        pass
    # stdlib fallback — RFC 5869 HKDF-SHA256
    m = master.encode() if isinstance(master, str) else master
    prk = hmac.new(_ENCv3_SALT, m, hashlib.sha256).digest()
    # Expand to 32 bytes (single block since L=32 ≤ HashLen=32)
    t = hmac.new(prk, _ENCv3_INFO + b"\x01", hashlib.sha256).digest()
    return t[:32]


def encrypt_secret(plaintext):
    """Encrypt a secret (Alpaca API key).

    Format tiers (encoded as prefix):
      "PLAIN:..."  -> retired write path. decrypt_secret still recognises it
                      for any legacy row, but this function never emits PLAIN
                      any more — MASTER_KEY is required at import time.
      "ENCv2:..."  -> AES-256-GCM with SHA256-derived key (decrypt-only
                      safety net; new writes use ENCv3)
      "ENCv3:..."  -> AES-256-GCM with HKDF-SHA256-derived key (current)

    Round-7 cleanup: the pre-AES-GCM "ENC:" HMAC-stream cipher was
    removed after Railway confirmed 0 rows on that format (startup log:
    "[auth] credential cipher distribution: {'ENC': 0, ...}"). The
    decrypt side follows in decrypt_secret().

    `needs_cipher_upgrade()` still returns True for ENCv2 so the login
    re-encrypt migration keeps running.
    """
    if not plaintext:
        return ""
    # MASTER_KEY presence is enforced at import time; if we got here without
    # one the module-level guard would already have raised. Defensive check
    # in case someone later loosens that.
    if not MASTER_KEY:
        raise RuntimeError(
            "encrypt_secret called without MASTER_ENCRYPTION_KEY set."
        )
    if not _HAS_AESGCM:
        raise RuntimeError(
            "cryptography package required for encrypt_secret(). Install "
            "it (pip install 'cryptography>=42.0.0')."
        )
    key = _derive_aesgcm_key_v3(MASTER_KEY)
    aes = AESGCM(key)
    nonce = secrets.token_bytes(12)  # 96-bit nonce is standard for GCM
    ct = aes.encrypt(nonce, plaintext.encode(), associated_data=b"alpaca-cred-v3")
    # Store nonce || ciphertext+tag (the cryptography lib appends the 16-byte tag)
    return "ENCv3:" + base64.b64encode(nonce + ct).decode()


def decrypt_secret(encrypted):
    """Decrypt a secret back to plaintext. Supports PLAIN / ENCv2 / ENCv3.

    Round-7: the pre-AES-GCM ENC: HMAC-stream path was removed once
    Railway confirmed 0 rows remained on that format. ENCv2 is kept as
    a decrypt-only safety net (one more deploy cycle) in case a pre-
    ENCv3 row somehow survived migration."""
    if not encrypted:
        return ""
    if encrypted.startswith("PLAIN:"):
        return encrypted[6:]

    # ENCv3: AES-GCM with HKDF-derived key (current format)
    if encrypted.startswith("ENCv3:"):
        if not MASTER_KEY or not _HAS_AESGCM:
            return ""
        try:
            blob = base64.b64decode(encrypted[6:])
            nonce, ct = blob[:12], blob[12:]
            key = _derive_aesgcm_key_v3(MASTER_KEY)
            aes = AESGCM(key)
            pt = aes.decrypt(nonce, ct, associated_data=b"alpaca-cred-v3")
            return pt.decode()
        except Exception:
            return ""

    # ENCv2: AES-GCM with SHA256-derived key (legacy, decrypt-only safety net)
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

    # Anything else — including the removed legacy "ENC:" prefix — is
    # undecodable. Returning the input string verbatim keeps pre-
    # encryption dev data from dying; production won't hit this path.
    return encrypted


def needs_cipher_upgrade(encrypted):
    """Return True if the ciphertext is in an older format that should be
    re-encrypted under the current (ENCv3) scheme on next opportunity.

    Round-7: "ENC:" removed from upgrade targets (legacy HMAC stream
    cipher is no longer decryptable anyway). PLAIN and ENCv2 still
    considered upgradable so the login migration keeps working.
    """
    if not encrypted:
        return False
    if not (_HAS_AESGCM and MASTER_KEY):
        return False
    if encrypted.startswith("PLAIN:"):
        return True
    if encrypted.startswith("ENCv2:"):
        return True
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
    # Use the shared strength check — enforces length + zxcvbn score with
    # username/email as user_inputs so "kevinbell2026" is flagged.
    ok, pw_err = check_password_strength(
        password,
        user_inputs=[username, email, email.split("@")[0] if email else ""],
    )
    if not ok:
        return None, pw_err
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

        # ntfy.sh topics are world-readable — anyone who knows the topic
        # string can subscribe and see every trade notification. Guessable
        # topics ("alpaca-bot-kevin") are therefore a trade-signal leak.
        # Default to an unguessable random token for new signups; existing
        # users are unaffected (this is only the default for new rows).
        # Users can still override via the Settings modal.
        default_topic = ntfy_topic or f"alpaca-bot-{secrets.token_urlsafe(12)}"

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
              default_topic,
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

def change_password(user_id, new_password, invalidate_sessions=True, enforce_strength=True):
    """Update a user's password. By default invalidates all existing sessions
    so a compromised cookie can't be used after the user changes their
    password. `consume_reset_token` already does this separately.

    Enforces password strength when `enforce_strength=True` (the default).
    Callers that have already run the check (e.g. reset flow) can pass
    False to skip it. Returns (True, None) on success or (False, msg).
    """
    if enforce_strength:
        user = get_user_by_id(user_id) or {}
        email = user.get("email") or ""
        ok, pw_err = check_password_strength(
            new_password,
            user_inputs=[user.get("username") or "", email,
                         email.split("@")[0] if email else ""],
        )
        if not ok:
            return False, pw_err
    conn = _get_db()
    cur = conn.cursor()
    pwhash, salt = hash_password(new_password)
    cur.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pwhash, salt, user_id))
    if invalidate_sessions:
        cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True, None

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
        log.warning("PBKDF2 rehash failed",
                    extra={"user_id": user.get('id'), "error": str(e)})

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
        log.warning("cipher upgrade failed",
                    extra={"user_id": user.get('id'), "error": str(e)})
    # Update last login
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_login = ?, login_count = login_count + 1 WHERE id = ?",
                (now_et().isoformat(), user["id"]))
    conn.commit()
    conn.close()
    return user

def get_user_alpaca_creds(user_id):
    """Return decrypted Alpaca credentials for a user.

    Round-11 live-trading: returns LIVE creds when user.live_mode == 1,
    else PAPER creds. Endpoint automatically switches between paper and
    live Alpaca URLs. Callers don't need to know which mode is active —
    they just use whatever this function returns.
    """
    user = get_user_by_id(user_id)
    if not user:
        return None
    live = bool(user.get("live_mode"))
    if live:
        key_col = "alpaca_live_key_encrypted"
        sec_col = "alpaca_live_secret_encrypted"
        endpoint = "https://api.alpaca.markets/v2"  # LIVE endpoint
    else:
        key_col = "alpaca_key_encrypted"
        sec_col = "alpaca_secret_encrypted"
        endpoint = user.get("alpaca_endpoint") or "https://paper-api.alpaca.markets/v2"
    return {
        "key": decrypt_secret(user.get(key_col, "")),
        "secret": decrypt_secret(user.get(sec_col, "")),
        "endpoint": endpoint,
        "data_endpoint": user.get("alpaca_data_endpoint") or "https://data.alpaca.markets/v2",
        "ntfy_topic": user.get("ntfy_topic"),
        "notification_email": user.get("notification_email"),
        "live_mode": live,
        "live_max_position_dollars": float(user.get("live_max_position_dollars") or 500),
    }


def save_user_alpaca_creds(user_id, *, paper_key=None, paper_secret=None,
                             live_key=None, live_secret=None):
    """Atomically update one or both credential pairs. Pass None to leave
    a pair untouched. Returns True on success."""
    conn = _get_db()
    cur = conn.cursor()
    updates = []
    params = []
    if paper_key is not None:
        updates.append("alpaca_key_encrypted = ?")
        params.append(encrypt_secret(paper_key))
    if paper_secret is not None:
        updates.append("alpaca_secret_encrypted = ?")
        params.append(encrypt_secret(paper_secret))
    if live_key is not None:
        updates.append("alpaca_live_key_encrypted = ?")
        params.append(encrypt_secret(live_key))
    if live_secret is not None:
        updates.append("alpaca_live_secret_encrypted = ?")
        params.append(encrypt_secret(live_secret))
    if not updates:
        conn.close()
        return False
    params.append(user_id)
    cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    return True


def set_live_mode(user_id, enabled, max_position_dollars=None):
    """Toggle live trading. Returns the new user dict."""
    conn = _get_db()
    cur = conn.cursor()
    if enabled:
        cur.execute("""UPDATE users SET live_mode = 1, live_enabled_at = ?,
                       live_max_position_dollars = COALESCE(?, live_max_position_dollars)
                       WHERE id = ?""",
                    (now_et().isoformat(), max_position_dollars, user_id))
    else:
        cur.execute("UPDATE users SET live_mode = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return get_user_by_id(user_id)

# ===== Sessions =====
def create_session(user_id, ip_address=None):
    """Create a new session. Returns session token.

    Normalises ip_address to 'unknown' if the caller didn't supply one —
    the sessions table column is nominally nullable for legacy reasons
    but every audit/logging path downstream assumes a string. Passing
    NULL would break log correlation for that session."""
    token = secrets.token_urlsafe(48)
    now = now_et()
    expires = now + timedelta(days=SESSION_DAYS)
    ip_str = (ip_address or "unknown").strip() or "unknown"
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (token, user_id, created_at, expires_at, ip_address)
        VALUES (?, ?, ?, ?, ?)
    """, (token, user_id, now.isoformat(), expires.isoformat(), ip_str))
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
    """Use a reset token to set a new password.
    Returns (True, None) on success, (False, err_msg) otherwise.

    Atomic mark-used: we UPDATE ... SET used=1 WHERE used=0 FIRST, and
    only proceed with the password change if rowcount=1. This closes a
    TOCTOU window where two concurrent reset requests for the same token
    could both validate (read used=0) before either marked it used —
    letting an attacker with the plaintext token re-use it in a burst.
    Under the new ordering, exactly one request wins the UPDATE race;
    the others see rowcount=0 and bail without changing the password.
    """
    if not new_password:
        return False, "Password required"
    token_hash = _hash_reset_token(token)
    now_iso = now_et().isoformat()

    conn = _get_db()
    cur = conn.cursor()
    # Atomically mark the token used IF it's still valid + unused. This
    # is the race-free gate; returns rowcount=1 only for the winning
    # caller, rowcount=0 for every subsequent re-use attempt.
    cur.execute(
        "UPDATE password_resets SET used = 1 "
        "WHERE token IN (?, ?) AND used = 0 AND expires_at > ?",
        (token_hash, token, now_iso),
    )
    if cur.rowcount == 0:
        conn.close()
        return False, "Invalid or expired reset link"
    # Fetch the user_id now that we've claimed the token.
    cur.execute(
        "SELECT user_id FROM password_resets WHERE token IN (?, ?)",
        (token_hash, token),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "Invalid or expired reset link"
    user_id = row[0]
    # Invalidate all existing sessions FIRST — ensures a reset-in-progress
    # attacker with a stolen session cookie gets booted before the new
    # password takes effect.
    cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    # Change the password in a separate connection. If this fails (e.g.,
    # password-strength rejection) the token is already consumed — the
    # user has to request a fresh reset, which is the correct UX (token
    # claimed = token spent, regardless of outcome).
    ok, err = change_password(user_id, new_password)
    if not ok:
        return False, err
    return True, None

# ===== Legacy cipher diagnostics =====
def count_legacy_encrypted_rows():
    """Return dict of {format: count} for users whose credentials are stored
    under an older cipher format. Transparent upgrade happens on login, so
    counts trend to zero. When all three (PLAIN, ENC, ENCv2) stay at zero
    across deploys, the corresponding decrypt paths can be safely removed.

    Called opportunistically from server startup to log the state.
    """
    try:
        conn = _get_db()
        cur = conn.cursor()
        out = {}
        for label, pattern in (
            ("PLAIN",  "PLAIN:%"),
            ("ENC",    "ENC:%"),  # legacy HMAC stream, NOT ENCv2/ENCv3
            ("ENCv2",  "ENCv2:%"),
            ("ENCv3",  "ENCv3:%"),
        ):
            # For ENC: we must exclude rows that are actually ENCv2/ENCv3
            # (they all share the "ENC" prefix).
            if label == "ENC":
                cur.execute(
                    "SELECT COUNT(*) FROM users WHERE "
                    "(alpaca_key_encrypted LIKE 'ENC:%' "
                    " AND alpaca_key_encrypted NOT LIKE 'ENCv2:%' "
                    " AND alpaca_key_encrypted NOT LIKE 'ENCv3:%') "
                    "OR (alpaca_secret_encrypted LIKE 'ENC:%' "
                    " AND alpaca_secret_encrypted NOT LIKE 'ENCv2:%' "
                    " AND alpaca_secret_encrypted NOT LIKE 'ENCv3:%')"
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM users WHERE "
                    "alpaca_key_encrypted LIKE ? OR alpaca_secret_encrypted LIKE ?",
                    (pattern, pattern),
                )
            out[label] = cur.fetchone()[0]
        conn.close()
        return out
    except Exception:
        return {"error": -1}  # sentinel: check failed


# ===== Per-user paths =====
def user_data_dir(user_id):
    """Return the data directory for a user.

    mode=0o700 means owner-only read/write/execute. On Railway (single
    container, single process user) this is belt-and-suspenders; on a
    shared local dev box it prevents OS-level users from reading another
    trader's strategies or journal. If the directory already exists with
    looser perms (from a pre-round-6 deploy), we tighten it on next call.
    """
    d = os.path.join(USERS_DIR, str(user_id))
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(d, "strategies"), mode=0o700, exist_ok=True)
    # `exist_ok=True` skips mode on pre-existing dirs — enforce it
    # explicitly so upgraded deploys get the tighter permissions.
    try:
        os.chmod(d, 0o700)
        os.chmod(os.path.join(d, "strategies"), 0o700)
    except OSError:
        pass
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

# ===== Login Attempt Rate Limiting =====
#
# Two-layer defence:
#
#   1. In-memory token bucket (fast path, brute-force-proof under load)
#        Per-key (ip + username) bucket with configurable burst + refill.
#        Empties atomically under a lock — race-free even at 100+
#        concurrent auth threads, which is the gap the previous SQLite-
#        only implementation left open (two threads could both read
#        count=4 before either recorded the new failure, letting both
#        through on attempt 5 and 6).
#
#   2. SQLite login_attempts table (authoritative, survives restart)
#        5 failures in 15 min locks the (ip, username) pair. Persists
#        across deploys so an attacker can't clear the counter by
#        forcing a Railway restart.
#
# Both gates must pass for a login attempt to proceed. The bucket is a
# fast short-term defence; the SQLite row is the long-term authoritative
# lockout. The bucket is NOT persistent — on server restart it re-fills
# to full, which is intentional: restart doesn't happen maliciously, and
# the SQLite gate still holds the authoritative 15-min window.
LOGIN_WINDOW_SEC = 15 * 60
LOGIN_MAX_FAILURES = 5

# Token-bucket tuning. At 0.2 tokens/s refill with a 10-token burst, a
# key's bucket recovers fully after ~50s idle. 10 back-to-back attempts
# are allowed (covers fat-finger retries on the login form); the 11th
# within a few seconds is rejected by the bucket before we even touch
# the database. A legitimate human user hitting the wrong password is
# unaffected; a scripted brute-forcer is effectively throttled.
LOGIN_BUCKET_RATE_PER_SEC = 0.2
LOGIN_BUCKET_BURST = 10

# Thread-safety for the bucket state dict.
import threading as _threading
_login_bucket_state: "dict[tuple[str, str], tuple[float, float]]" = {}
_login_bucket_lock = _threading.Lock()


def _login_bucket_key(ip, username):
    """Canonical key for the bucket dict. Match the SQLite lookup key."""
    return ((ip or "unknown"), (username or "").lower())


def _login_bucket_consume(ip, username, cost: float = 1.0) -> bool:
    """Atomically try to consume `cost` tokens from the (ip, username) bucket.

    Returns True if the request is allowed (tokens available), False if
    the bucket is currently empty and the request should be rejected as
    rate-limited.

    Thread-safe. Uses time.monotonic() — immune to wall-clock adjustments.
    """
    import time as _time
    now = _time.monotonic()
    key = _login_bucket_key(ip, username)
    with _login_bucket_lock:
        tokens, last = _login_bucket_state.get(key, (float(LOGIN_BUCKET_BURST), now))
        # Refill tokens based on elapsed time since the last check, capped
        # at the configured burst.
        elapsed = max(0.0, now - last)
        tokens = min(float(LOGIN_BUCKET_BURST),
                     tokens + elapsed * LOGIN_BUCKET_RATE_PER_SEC)
        if tokens >= cost:
            tokens -= cost
            _login_bucket_state[key] = (tokens, now)
            return True
        # Not enough tokens — update last-seen so future calls resume
        # refilling from now, but don't grant the request.
        _login_bucket_state[key] = (tokens, now)
        return False


def _login_bucket_peek(ip, username) -> float:
    """Return the current token count for (ip, username) without mutating.

    Exposed primarily for tests and operational introspection."""
    import time as _time
    now = _time.monotonic()
    key = _login_bucket_key(ip, username)
    with _login_bucket_lock:
        tokens, last = _login_bucket_state.get(key, (float(LOGIN_BUCKET_BURST), now))
        elapsed = max(0.0, now - last)
        return min(float(LOGIN_BUCKET_BURST),
                   tokens + elapsed * LOGIN_BUCKET_RATE_PER_SEC)


def _reset_login_buckets() -> None:
    """Clear in-memory bucket state. Test hook — normal code should not
    call this, the bucket is self-managing."""
    with _login_bucket_lock:
        _login_bucket_state.clear()


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
        log.warning("record_login_attempt failed", extra={"error": str(e)})


def is_login_locked(ip, username):
    """Return True if the (ip, username) pair is currently rate-limited.

    Two gates, either of which trips the lockout:
      1. In-memory token bucket empty — short-burst brute-force defence.
         Race-free under concurrent load.
      2. SQLite failure count ≥ LOGIN_MAX_FAILURES in the last
         LOGIN_WINDOW_SEC seconds — long-term, restart-persistent lockout.

    The bucket is consumed BEFORE the SQLite check so a rapid-fire
    attacker is rejected on the fast path without even opening the DB
    connection.
    """
    # Gate 1: token bucket. Consuming one token here is intentional even
    # for "just checking" — otherwise a malicious client could poll the
    # endpoint endlessly without being throttled. Legitimate users retry
    # at human speed and stay well within the burst allowance.
    if not _login_bucket_consume(ip, username):
        return True
    # Gate 2: SQLite authoritative lockout window.
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
        log.warning("is_login_locked check failed", extra={"error": str(e)})
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
    Called opportunistically from the same hook as gc_login_attempts.

    Batched: processes 500 rows per statement to avoid holding a long
    write lock if the table has accumulated many expired rows (e.g., after
    a deploy that lost the GC hook). SQLite blocks other writers during
    a DELETE, so unbounded deletes can pause logins for seconds.
    """
    try:
        cutoff = (now_et() - timedelta(days=AUDIT_RETENTION_DAYS)).isoformat()
        conn = _get_db()
        cur = conn.cursor()
        total = 0
        for _ in range(20):  # cap at 20 * 500 = 10k rows per GC call
            cur.execute(
                "DELETE FROM admin_audit_log "
                "WHERE id IN (SELECT id FROM admin_audit_log WHERE ts < ? LIMIT 500)",
                (cutoff,),
            )
            deleted = cur.rowcount
            conn.commit()
            total += deleted
            if deleted < 500:
                break
        conn.close()
        return total
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
        log.warning("audit log write failed",
                    extra={"action": action, "error": str(e)})


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
