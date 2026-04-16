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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent data lives. On Railway, set to a volume mount path.
# Locally, defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "users.db")
USERS_DIR = os.path.join(DATA_DIR, "users")

# Master encryption key from env — set on Railway, never in code
MASTER_KEY = os.environ.get("MASTER_ENCRYPTION_KEY", "")
if not MASTER_KEY:
    # Fallback for first-run / local dev — generate and warn
    # In production, Railway env var MUST be set
    pass

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
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
    CREATE INDEX IF NOT EXISTS idx_reset_token ON password_resets(token);
    """)
    conn.commit()
    conn.close()
    os.makedirs(USERS_DIR, exist_ok=True)

# ===== Password Hashing =====
def hash_password(password, salt=None):
    """Return (hash_b64, salt_b64). If salt not provided, generates one."""
    if salt is None:
        salt_bytes = secrets.token_bytes(16)
        salt = base64.b64encode(salt_bytes).decode()
    else:
        salt_bytes = base64.b64decode(salt)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, 200_000)
    return base64.b64encode(hashed).decode(), salt

def verify_password(password, stored_hash, stored_salt):
    """Timing-safe password verification."""
    try:
        check_hash, _ = hash_password(password, stored_salt)
        return hmac.compare_digest(check_hash, stored_hash)
    except Exception:
        return False

# ===== Encryption (Fernet-like using stdlib) =====
# Uses HMAC-SHA256 for auth + simple XOR stream cipher derived from master key.
# Not as strong as AES-GCM, but stdlib-only and adequate when master key is secret.

def _derive_key(master, purpose):
    """Derive a sub-key for a specific purpose (encryption vs MAC)."""
    return hashlib.sha256(f"{master}:{purpose}".encode()).digest()

def encrypt_secret(plaintext):
    """Encrypt a secret (Alpaca API key). Returns base64 string."""
    if not plaintext:
        return ""
    if not MASTER_KEY:
        # Without master key, store plaintext (DB is gitignored, single-user fallback)
        return "PLAIN:" + plaintext
    enc_key = _derive_key(MASTER_KEY, "encrypt")
    mac_key = _derive_key(MASTER_KEY, "mac")
    nonce = secrets.token_bytes(16)
    # Generate keystream via HMAC counter mode
    plaintext_bytes = plaintext.encode()
    blocks_needed = (len(plaintext_bytes) + 31) // 32
    keystream = b""
    for i in range(blocks_needed):
        counter = nonce + i.to_bytes(16, "big")
        keystream += hmac.new(enc_key, counter, hashlib.sha256).digest()
    ciphertext = bytes(a ^ b for a, b in zip(plaintext_bytes, keystream[:len(plaintext_bytes)]))
    # MAC the (nonce + ciphertext)
    mac = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    combined = nonce + mac + ciphertext
    return "ENC:" + base64.b64encode(combined).decode()

def decrypt_secret(encrypted):
    """Decrypt a secret back to plaintext."""
    if not encrypted:
        return ""
    if encrypted.startswith("PLAIN:"):
        return encrypted[6:]
    if not encrypted.startswith("ENC:"):
        return encrypted  # Legacy plaintext
    if not MASTER_KEY:
        return ""  # Can't decrypt without key
    try:
        combined = base64.b64decode(encrypted[4:])
        nonce = combined[:16]
        mac_stored = combined[16:48]
        ciphertext = combined[48:]
        mac_key = _derive_key(MASTER_KEY, "mac")
        mac_check = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac_stored, mac_check):
            return ""  # Tampered or wrong key
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

# ===== User CRUD =====
def create_user(email, username, password, alpaca_key, alpaca_secret,
                alpaca_endpoint="https://paper-api.alpaca.markets/v2",
                alpaca_data_endpoint="https://data.alpaca.markets/v2",
                ntfy_topic=None, notification_email=None, is_admin=False):
    """Create a new user. Returns (user_id, error)."""
    try:
        conn = _get_db()
        cur = conn.cursor()
        # Check if this will be the first user (auto-admin)
        cur.execute("SELECT COUNT(*) FROM users")
        is_first = cur.fetchone()[0] == 0
        if is_first:
            is_admin = True

        pwhash, salt = hash_password(password)
        now = datetime.now(timezone.utc).isoformat()

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

def change_password(user_id, new_password):
    conn = _get_db()
    cur = conn.cursor()
    pwhash, salt = hash_password(new_password)
    cur.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pwhash, salt, user_id))
    conn.commit()
    conn.close()
    return True

def authenticate(username_or_email, password):
    """Return user dict if credentials valid, None otherwise."""
    user = get_user_by_username(username_or_email) or get_user_by_email(username_or_email)
    if not user:
        return None
    if not verify_password(password, user["password_hash"], user["password_salt"]):
        return None
    # Update last login
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_login = ?, login_count = login_count + 1 WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), user["id"]))
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
    now = datetime.now(timezone.utc)
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
    """, (token, datetime.now(timezone.utc).isoformat()))
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
    cur.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted

# ===== Password Reset =====
def create_password_reset(email):
    """Create a reset token. Returns (token, user) or (None, None)."""
    user = get_user_by_email(email)
    if not user:
        return None, None
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=RESET_TOKEN_HOURS)
    conn = _get_db()
    cur = conn.cursor()
    # Invalidate previous tokens
    cur.execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (user["id"],))
    cur.execute("""
        INSERT INTO password_resets (token, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
    """, (token, user["id"], now.isoformat(), expires.isoformat()))
    conn.commit()
    conn.close()
    return token, user

def validate_reset_token(token):
    """Return user_id if token valid and unused, None otherwise."""
    if not token:
        return None
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id FROM password_resets
        WHERE token = ? AND used = 0 AND expires_at > ?
    """, (token, datetime.now(timezone.utc).isoformat()))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def consume_reset_token(token, new_password):
    """Use a reset token to set a new password. Returns True on success."""
    user_id = validate_reset_token(token)
    if not user_id:
        return False
    change_password(user_id, new_password)
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("UPDATE password_resets SET used = 1 WHERE token = ?", (token,))
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

if __name__ == "__main__":
    init_db()
    bootstrap_from_env()
    print(f"Users: {len(list_active_users())}")
    for u in list_active_users():
        print(f"  {u['id']}: {u['username']} ({u['email']}) admin={u['is_admin']}")
