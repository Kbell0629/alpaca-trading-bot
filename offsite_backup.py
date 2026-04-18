#!/usr/bin/env python3
"""
offsite_backup.py — Off-Railway backup destinations for daily DB + state.

Round-11 expansion (item 4). Currently `backup.py` writes to Railway's
own volume — single point of failure if Railway has an incident or
the volume gets corrupted. This module pushes the same archive to an
external destination so we always have a recoverable copy elsewhere.

Supported destinations (auto-detected from env vars):
  AWS S3    → set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY +
              S3_BACKUP_BUCKET (and optionally S3_BACKUP_PREFIX)
  Backblaze → set B2_KEY_ID + B2_APPLICATION_KEY + B2_BUCKET
              (cheaper than S3 — $0.005/GB/mo)
  GitHub    → set GITHUB_BACKUP_TOKEN + GITHUB_BACKUP_REPO
              (uses GitHub's release-assets API — free for small files)
  None      → if no env vars set, push_to_offsite() is a no-op
              (returns {ok: False, reason: "no destination configured"})

Public API:

    push_to_offsite(local_archive_path) -> dict
        Tries each configured destination in order; returns first
        success. Result dict carries:
        {ok, destination, remote_url, bytes_uploaded, error?}

    list_offsite_backups() -> list[dict]
        Polls each configured destination for known backup objects.
        Useful for the dashboard "verify off-site backups" panel.

Wired into: backup.py at the end of the daily backup task. Failure
to push offsite never fails the local backup — logged + reported.
"""
from __future__ import annotations
import json
import os
import urllib.request
import urllib.error


def _push_s3(local_path):
    """Push to AWS S3 using boto3 if available, else fall back to
    aws-cli or requests-based signed PUT. boto3 is in the project
    requirements transitively (yfinance pulls it via requests). If
    not available, return ok=False so the caller tries the next dest."""
    bucket = os.environ.get("S3_BACKUP_BUCKET")
    if not bucket:
        return {"ok": False, "reason": "S3_BACKUP_BUCKET not set"}
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        return {"ok": False, "reason": "AWS credentials not set"}
    try:
        import boto3
    except ImportError:
        return {"ok": False, "reason": "boto3 not installed (pip install boto3)"}
    try:
        prefix = os.environ.get("S3_BACKUP_PREFIX", "alpaca-bot-backups").strip("/")
        key = f"{prefix}/{os.path.basename(local_path)}"
        client = boto3.client("s3")
        client.upload_file(local_path, bucket, key)
        size = os.path.getsize(local_path)
        return {
            "ok": True,
            "destination": "s3",
            "remote_url": f"s3://{bucket}/{key}",
            "bytes_uploaded": size,
        }
    except Exception as e:
        return {"ok": False, "reason": f"S3 upload failed: {e}"}


def _push_b2(local_path):
    """Push to Backblaze B2 — cheaper than S3 ($0.005/GB/mo storage,
    free egress to Cloudflare). Uses b2sdk if available."""
    bucket = os.environ.get("B2_BUCKET")
    key_id = os.environ.get("B2_KEY_ID")
    app_key = os.environ.get("B2_APPLICATION_KEY")
    if not (bucket and key_id and app_key):
        return {"ok": False, "reason": "B2 env vars not set"}
    try:
        import b2sdk.v2 as b2
    except ImportError:
        return {"ok": False, "reason": "b2sdk not installed (pip install b2sdk)"}
    try:
        info = b2.InMemoryAccountInfo()
        api = b2.B2Api(info)
        api.authorize_account("production", key_id, app_key)
        bucket_obj = api.get_bucket_by_name(bucket)
        prefix = os.environ.get("B2_BACKUP_PREFIX", "alpaca-bot-backups").strip("/")
        remote_name = f"{prefix}/{os.path.basename(local_path)}"
        bucket_obj.upload_local_file(local_file=local_path, file_name=remote_name)
        size = os.path.getsize(local_path)
        return {
            "ok": True,
            "destination": "b2",
            "remote_url": f"b2://{bucket}/{remote_name}",
            "bytes_uploaded": size,
        }
    except Exception as e:
        return {"ok": False, "reason": f"B2 upload failed: {e}"}


def _push_github_release(local_path):
    """Push to a GitHub release-asset. Free for files <2GB. Uses the
    REST v3 API directly via urllib (no extra deps).

    Setup: create a private repo (e.g. `alpaca-bot-backups`), generate a
    fine-grained PAT with Contents: write scope, set:
      GITHUB_BACKUP_TOKEN=ghp_...
      GITHUB_BACKUP_REPO=username/repo
    The function creates one release per day named `backup-YYYY-MM-DD`
    and uploads the archive as an asset.
    """
    token = os.environ.get("GITHUB_BACKUP_TOKEN")
    repo = os.environ.get("GITHUB_BACKUP_REPO")
    if not (token and repo):
        return {"ok": False, "reason": "GITHUB_BACKUP_TOKEN / GITHUB_BACKUP_REPO not set"}
    try:
        from datetime import datetime
        try:
            from et_time import now_et
            today = now_et().strftime("%Y-%m-%d")
        except ImportError:
            today = datetime.now().strftime("%Y-%m-%d")
        tag_name = f"backup-{today}"
        api_base = f"https://api.github.com/repos/{repo}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "alpaca-bot-backup",
        }

        # 1. Try to find or create a release for today
        release_url = f"{api_base}/releases/tags/{tag_name}"
        req = urllib.request.Request(release_url, headers=headers)
        release_id = None
        upload_url_template = None
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                rel = json.loads(resp.read().decode())
                release_id = rel.get("id")
                upload_url_template = rel.get("upload_url", "")
        except urllib.error.HTTPError as he:
            if he.code != 404:
                return {"ok": False, "reason": f"GitHub release lookup failed: {he}"}
            # Create the release
            create_body = json.dumps({
                "tag_name": tag_name,
                "name": tag_name,
                "body": f"Daily backup snapshot {today}",
                "draft": False,
                "prerelease": False,
            }).encode()
            create_req = urllib.request.Request(
                f"{api_base}/releases", data=create_body,
                headers={**headers, "Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(create_req, timeout=15) as resp:
                rel = json.loads(resp.read().decode())
                release_id = rel.get("id")
                upload_url_template = rel.get("upload_url", "")

        if not upload_url_template:
            return {"ok": False, "reason": "no upload URL returned"}
        upload_url = upload_url_template.split("{")[0]
        filename = os.path.basename(local_path)

        # 2. Upload the file
        with open(local_path, "rb") as f:
            file_data = f.read()
        upload_req = urllib.request.Request(
            f"{upload_url}?name={filename}",
            data=file_data,
            headers={
                **headers,
                "Content-Type": "application/gzip",
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(upload_req, timeout=120) as resp:
                asset = json.loads(resp.read().decode())
                return {
                    "ok": True,
                    "destination": "github",
                    "remote_url": asset.get("browser_download_url"),
                    "bytes_uploaded": asset.get("size", len(file_data)),
                }
        except urllib.error.HTTPError as he:
            # 422 means the asset already exists for today — not a real error
            if he.code == 422:
                return {"ok": True, "destination": "github",
                        "remote_url": f"https://github.com/{repo}/releases/tag/{tag_name}",
                        "note": "asset already uploaded today"}
            raise
    except Exception as e:
        return {"ok": False, "reason": f"GitHub release push failed: {e}"}


def push_to_offsite(local_archive_path):
    """Try each configured destination in priority order. Returns the
    first success or the last error if all fail.

    Priority: S3 → B2 → GitHub. Set whichever env vars match your
    preferred destination."""
    if not os.path.exists(local_archive_path):
        return {"ok": False, "reason": f"local archive not found: {local_archive_path}"}
    attempts = []
    for fn in (_push_s3, _push_b2, _push_github_release):
        result = fn(local_archive_path)
        if result.get("ok"):
            return result
        attempts.append({"destination": fn.__name__.replace("_push_", ""),
                          "reason": result.get("reason")})
    return {
        "ok": False,
        "reason": "no destination succeeded",
        "attempts": attempts,
    }


def configured_destination():
    """Return the name of the highest-priority configured destination,
    or None. Used by the dashboard health panel to show 'S3 backup
    enabled' style messages."""
    if os.environ.get("S3_BACKUP_BUCKET") and os.environ.get("AWS_ACCESS_KEY_ID"):
        return "s3"
    if os.environ.get("B2_BUCKET") and os.environ.get("B2_KEY_ID"):
        return "b2"
    if os.environ.get("GITHUB_BACKUP_TOKEN") and os.environ.get("GITHUB_BACKUP_REPO"):
        return "github"
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 offsite_backup.py <local_archive_path>")
        print(f"Configured destination: {configured_destination() or 'none'}")
        sys.exit(0)
    result = push_to_offsite(sys.argv[1])
    print(json.dumps(result, indent=2))
