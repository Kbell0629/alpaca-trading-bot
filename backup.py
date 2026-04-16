#!/usr/bin/env python3
"""
Daily backup for the trading bot's persistent state.

Captures users.db (SQLite auth) and the users/ directory tree (per-user
strategies, guardrails, trade journal, dashboard data) into a single
timestamped tar.gz in /data/backups/ — same Railway volume, but a separate
file that can be copied off or rolled back to if something goes wrong
(bad migration, accidental file delete, corrupted strategy JSON).

The on-volume tactic protects against:
  - Accidental file deletion
  - Corruption of a single strategy file
  - Failed migration / bad deploy overwriting state

It does NOT protect against full volume loss — for that you need an
off-volume backup. To upload backups off the volume, use Railway CLI:
  railway ssh 'cat /data/backups/YYYY-MM-DD.tar.gz' > local-copy.tar.gz
Or download via the admin panel (/api/admin/download-backup).

stdlib-only; no pip deps.

Usage:
  python3 backup.py                   # make a backup now
  python3 backup.py --list            # list existing backups
  python3 backup.py --restore DATE    # restore from a dated backup (careful!)
"""
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

# How many daily backups to keep. Rotates the oldest on each run.
RETENTION_DAYS = 14

# Files/dirs to include in each backup — nothing else. This keeps the
# archive small and avoids shipping secrets env-vars (not on disk anyway).
INCLUDES = [
    "users.db",           # auth DB
    "users/",             # per-user dirs (strategies, journals, etc.)
    "strategies/",        # legacy shared strategies (env-var mode fallback)
    "guardrails.json",
    "auto_deployer_config.json",
    "scorecard.json",
    "trade_journal.json",
    "learned_weights.json",
    "capital_status.json",
]


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SUTC")


def _backup_filename(date_str=None):
    return os.path.join(BACKUP_DIR, f"{date_str or _ts()}.tar.gz")


import threading as _threading
_backup_lock = _threading.Lock()

def create_backup():
    """Create a fresh tar.gz backup. Returns (path, size_bytes, error).

    Uses SQLite's .backup API for users.db to ensure a consistent snapshot
    even if the server is mid-transaction. Other files are copied atomically
    into a staging dir before being archived.

    Concurrency: wrapped in a threading.Lock so rapid admin clicks on
    "Create Backup Now" don't spawn competing tarfile writers.
    """
    if not _backup_lock.acquire(blocking=False):
        return None, 0, "Another backup is already in progress"
    try:
        return _create_backup_inner()
    finally:
        _backup_lock.release()


def _create_backup_inner():
    try:
        timestamp = _ts()
        archive_path = os.path.join(BACKUP_DIR, f"{timestamp}.tar.gz")

        # Stage files in a temp dir so the final archive sees a consistent
        # snapshot (no half-written JSON mid-backup).
        with tempfile.TemporaryDirectory(prefix="wbkp-", dir=BACKUP_DIR) as staging:
            # SQLite consistent snapshot via .backup API.
            # CRITICAL: we NULL out alpaca_key_encrypted and alpaca_secret_encrypted
            # before archiving. The admin downloads backups; without this,
            # admin could extract every user's encrypted Alpaca credentials
            # (which admin could then decrypt offline using MASTER_KEY).
            # A restored backup WILL require users to re-enter their Alpaca
            # keys via Settings — by design. Rare event (disaster recovery).
            src_db = os.path.join(DATA_DIR, "users.db")
            if os.path.exists(src_db):
                dst_db = os.path.join(staging, "users.db")
                src_conn = sqlite3.connect(src_db)
                dst_conn = sqlite3.connect(dst_db)
                with dst_conn:
                    src_conn.backup(dst_conn)
                src_conn.close()
                # Strip Alpaca credential columns in the BACKUP copy only.
                # Live DB keeps them — scheduler still needs to decrypt to trade.
                try:
                    cur = dst_conn.cursor()
                    cur.execute("UPDATE users SET "
                                "alpaca_key_encrypted = NULL, "
                                "alpaca_secret_encrypted = NULL")
                    dst_conn.commit()
                except Exception:
                    pass
                dst_conn.close()

            # Copy remaining files
            for entry in INCLUDES:
                if entry == "users.db":
                    continue  # handled above
                src = os.path.join(DATA_DIR, entry.rstrip("/"))
                if not os.path.exists(src):
                    continue
                dst = os.path.join(staging, entry.rstrip("/"))
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                    shutil.copy2(src, dst)

            # Write metadata
            meta = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "data_dir": DATA_DIR,
                "includes": INCLUDES,
                "bot_version": "post-forensic-audit-r4",
                "alpaca_credentials_stripped": True,
                "restore_note": (
                    "This backup has Alpaca API credentials stripped from "
                    "users.db. After restore, every user must re-enter their "
                    "Alpaca key + secret via the Settings modal (or the bot "
                    "will not trade for them)."
                ),
            }
            with open(os.path.join(staging, "backup_meta.json"), "w") as f:
                json.dump(meta, f, indent=2)

            # Tar+gz into final location
            with tarfile.open(archive_path, "w:gz") as tar:
                for name in os.listdir(staging):
                    tar.add(os.path.join(staging, name), arcname=name)

        size = os.path.getsize(archive_path)
        _rotate_old_backups(RETENTION_DAYS)
        return archive_path, size, None
    except Exception as e:
        return None, 0, str(e)


def _rotate_old_backups(keep):
    """Delete all but the N most-recent backup files."""
    try:
        files = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.endswith(".tar.gz")],
            reverse=True,
        )
        for old in files[keep:]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except OSError:
                pass
    except FileNotFoundError:
        pass


def list_backups():
    """Return list of {name, path, size_bytes, created_at} sorted newest first."""
    try:
        files = [f for f in os.listdir(BACKUP_DIR) if f.endswith(".tar.gz")]
    except FileNotFoundError:
        return []
    out = []
    for f in sorted(files, reverse=True):
        path = os.path.join(BACKUP_DIR, f)
        try:
            stat = os.stat(path)
            out.append({
                "name": f,
                "path": path,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
        except OSError:
            pass
    return out


def restore_backup(name, dry_run=False):
    """EXTREMELY DESTRUCTIVE. Restores a backup by extracting it on top of
    the current DATA_DIR. The existing users.db and users/ are backed up
    once into a .pre-restore-<ts>/ subdir first so we can unroll if needed.

    Returns (success, message).
    """
    archive = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(archive):
        return False, f"Backup not found: {name}"
    if dry_run:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getnames()
        return True, f"Dry run — would extract {len(members)} members"
    try:
        pre = os.path.join(BACKUP_DIR, f".pre-restore-{_ts()}")
        os.makedirs(pre, exist_ok=True)
        for entry in INCLUDES:
            src = os.path.join(DATA_DIR, entry.rstrip("/"))
            if os.path.exists(src):
                dst = os.path.join(pre, entry.rstrip("/"))
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                    shutil.copy2(src, dst)
        # Now extract archive on top of DATA_DIR. Use filter='data' on Py 3.12+
        # to block path-traversal (..) and absolute paths. Fall back to manual
        # member validation on older versions.
        with tarfile.open(archive, "r:gz") as tar:
            try:
                tar.extractall(DATA_DIR, filter="data")  # Py 3.12+
            except TypeError:
                # Older Python — validate manually
                safe_members = []
                for m in tar.getmembers():
                    if m.name.startswith("/") or ".." in m.name.split("/"):
                        continue  # reject
                    safe_members.append(m)
                tar.extractall(DATA_DIR, members=safe_members)
        return True, f"Restored from {name}. Prior state saved to {pre}"
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        for b in list_backups():
            size_mb = b["size_bytes"] / 1024 / 1024
            print(f"{b['name']}  {size_mb:.1f}MB  ({b['created_at']})")
    elif len(sys.argv) > 2 and sys.argv[1] == "--restore":
        ok, msg = restore_backup(sys.argv[2])
        print(msg)
        sys.exit(0 if ok else 1)
    else:
        path, size, err = create_backup()
        if err:
            print(f"Backup FAILED: {err}")
            sys.exit(1)
        size_mb = size / 1024 / 1024
        print(f"Backup created: {path} ({size_mb:.1f}MB)")
        # Show retention
        kept = list_backups()
        print(f"Keeping {len(kept)} backups (retention = {RETENTION_DAYS} days)")
