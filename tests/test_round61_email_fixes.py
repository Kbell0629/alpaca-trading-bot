"""
Round-61 notification-email fixes.

User reported (2026-04-24): "can you also check the notification emails
and make sure working correctly like they are all firing I feel like
some are not coming though"

Audit found two things:
  (a) short_sell force-cover after 14 days notified with type "info"
      which notify.py does NOT email. Important position-closing event
      — should email. Changed to "exit".
  (b) No dashboard visibility into the email pipeline. If Gmail
      credentials are missing on Railway OR a user hasn't set their
      notification_email, the entire email path silently drops events.
      Added /api/email-status endpoint + a dashboard chip that shows
      SMTP enabled / queued / sent-today so the user can diagnose
      problems at a glance.

These pin both fixes so they can't regress silently.
"""
from __future__ import annotations

import json
import os


def test_short_force_cover_uses_exit_notify_type():
    """14-day force-cover of a short is a position-closing event —
    must email (notify_type='exit'), not push-only ('info'). Without
    this, the user had no record of the bot forcing a short cover."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    # Find the force-covered notify call
    idx = src.find("force-covered after")
    assert idx > 0, "force-cover notify call moved or removed"
    # Grab the whole line
    start = src.rfind("\n", 0, idx) + 1
    end = src.find("\n", idx)
    line = src[start:end]
    assert '"exit"' in line, (
        "short force-cover notify must use type 'exit' so it emails — "
        f"got: {line.strip()}")
    assert '"info"' not in line, (
        "short force-cover should NOT use 'info' (push-only); "
        f"got: {line.strip()}")


def test_api_email_status_endpoint_exists():
    """/api/email-status is the diagnostic endpoint the dashboard
    chip polls. Must stay present and require auth."""
    with open("server.py") as f:
        src = f.read()
    assert '"/api/email-status"' in src, (
        "/api/email-status endpoint missing — dashboard chip will "
        "break without it")
    # The endpoint must check authentication
    idx = src.find('"/api/email-status"')
    block = src[idx:idx + 3000]
    assert "Not authenticated" in block, (
        "/api/email-status must 401 unauthenticated callers — "
        "email-queue internals are user-private")


def test_api_email_status_returns_expected_fields():
    """The endpoint returns a JSON payload with specific fields the
    dashboard chip relies on. Missing any one of them breaks the
    chip's render logic."""
    with open("server.py") as f:
        src = f.read()
    idx = src.find('"/api/email-status"')
    block = src[idx:idx + 3000]
    for field in ('"enabled"', '"queued"', '"sent_today"',
                  '"last_sent_at"', '"recipient"', '"dead_letter_count"'):
        assert field in block, (
            f"/api/email-status missing field {field} — dashboard chip "
            "will show wrong label without it")


def test_email_status_chip_present_in_dashboard():
    """The header chip with id=emailStatusChip is what calls
    refreshEmailStatus on a 60s tick."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert 'id="emailStatusChip"' in src, (
        "emailStatusChip element missing from dashboard header — "
        "refreshEmailStatus has nothing to populate")
    # Click handler opens a detail modal
    assert "showEmailStatusDetail" in src, (
        "showEmailStatusDetail handler missing — chip should be "
        "clickable for details")


def test_email_status_poll_interval_60s():
    """The chip's own poll interval (separate from renderDashboard
    because we don't want email-status to tick every 10s — it's a
    health indicator, not live data)."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "setInterval(refreshEmailStatus, 60000)" in src, (
        "email status chip must poll on its own 60s interval")


def test_email_status_chip_preserved_across_app_swap():
    """The pt.6-prep #4 panel-preservation logic must include
    emailStatusChip so the chip doesn't flash '…' every time
    renderDashboard runs a full rewrite."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    idx = src.find("_asyncPanelIds")
    assert idx > 0
    window = src[idx:idx + 500]
    assert "emailStatusChip" in window, (
        "emailStatusChip must be in _asyncPanelIds — otherwise the "
        "chip shows '📧 …' for a moment on every full app rewrite "
        "(feels like jitter)")


def test_email_status_chip_renders_off_when_disabled():
    """When enabled=false (missing GMAIL_USER), chip must show OFF
    in red so the user sees the pipeline is broken at a glance."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "📧 OFF" in src, (
        "email chip must render '📧 OFF' state when SMTP is disabled — "
        "user needs visibility when Gmail creds are missing")


def test_email_status_chip_renders_no_addr_when_no_recipient():
    """When the user has no notification_email set, chip shows NO ADDR
    in orange. Actionable — points to Settings."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "📧 NO ADDR" in src, (
        "email chip must render '📧 NO ADDR' when recipient is empty — "
        "user should know the email target isn't set")


def test_email_status_chip_warns_on_stuck_queue():
    """If >10 emails are queued and not sending, the chip shows STUCK
    in orange — suggests SMTP auth or Gmail app-password lock."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "STUCK" in src, (
        "email chip must render STUCK state when queue > 10 unsent — "
        "signals a real send failure, not just backlog")
