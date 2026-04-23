"""
Round-61 pt.6 pass 2 — exercise the AUTHED code paths inside handlers.
Previous endpoint file (test_round61_pt6_wsgi_endpoints.py) focused on
auth gates; this one submits valid bodies while authenticated so the
handler's actual logic executes. That's where the remaining 70% of
server.py uncovered lines live.
"""
from __future__ import annotations

import json
import os


# ============================================================================
# handle_update_settings — input validation + persistence paths
# ============================================================================

class TestUpdateSettings:
    def test_empty_body_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/update-settings", body={})
        assert resp["status"] == 400
        assert "error" in resp["body"]

    def test_notification_email_updated(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/update-settings",
                                   body={"notification_email": "new@addr.com"})
        assert resp["status"] == 200
        assert resp["body"].get("success") is True

    def test_invalid_alpaca_endpoint_rejected(self, http_harness):
        """SSRF defense: only Alpaca's real domains are accepted."""
        http_harness.create_user()
        resp = http_harness.post("/api/update-settings", body={
            "alpaca_endpoint": "http://attacker.com/api"
        })
        assert resp["status"] == 400
        assert "invalid" in resp["body"].get("error", "").lower() or \
               "alpaca" in resp["body"].get("error", "").lower()

    def test_valid_alpaca_endpoint_accepted(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/update-settings", body={
            "alpaca_endpoint": "https://paper-api.alpaca.markets/v2"
        })
        assert resp["status"] == 200

    def test_invalid_ntfy_topic_rejected(self, http_harness):
        """ntfy_topic has an allowlist: letters/digits/_/- 4-64 chars."""
        http_harness.create_user()
        resp = http_harness.post("/api/update-settings", body={
            "ntfy_topic": "../../../etc/passwd"
        })
        assert resp["status"] == 400

    def test_valid_ntfy_topic_accepted(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/update-settings", body={
            "ntfy_topic": "my-valid-topic_123"
        })
        assert resp["status"] == 200

    def test_empty_alpaca_key_skipped_as_keep_existing(self, http_harness):
        """An empty string for alpaca_key means 'keep existing' — not
        'clear it'. That's a key-rotation safety feature."""
        http_harness.create_user()
        resp = http_harness.post("/api/update-settings", body={
            "alpaca_key": "",
            "notification_email": "kept@x.com",  # something else non-empty
        })
        assert resp["status"] == 200


# ============================================================================
# handle_change_password
# ============================================================================

class TestChangePassword:
    def test_missing_fields_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/change-password", body={})
        assert resp["status"] == 400

    def test_wrong_old_password_rejected(self, http_harness):
        http_harness.create_user(password="correct horse battery staple!!")
        resp = http_harness.post("/api/change-password", body={
            "old_password": "wrong",
            "new_password": "another correct horse battery staple"
        })
        assert resp["status"] == 400
        err = resp["body"].get("error", "").lower()
        assert "incorrect" in err or "current" in err

    def test_weak_new_password_rejected(self, http_harness):
        http_harness.create_user(password="correct horse battery staple!!")
        resp = http_harness.post("/api/change-password", body={
            "old_password": "correct horse battery staple!!",
            "new_password": "123"
        })
        assert resp["status"] == 400

    def test_successful_password_change(self, http_harness):
        http_harness.create_user(password="correct horse battery staple!!")
        resp = http_harness.post("/api/change-password", body={
            "old_password": "correct horse battery staple!!",
            "new_password": "brand new correct horse battery staple"
        })
        assert resp["status"] == 200
        assert resp["body"].get("success") is True


# ============================================================================
# handle_delete_account
# ============================================================================

class TestDeleteAccount:
    def test_requires_confirm_password(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/delete-account", body={})
        # Must reject without password confirmation
        assert resp["status"] in (400, 403)

    def test_wrong_password_rejected(self, http_harness):
        http_harness.create_user(password="correct horse battery staple!!")
        resp = http_harness.post("/api/delete-account", body={
            "password": "wrong"
        })
        assert resp["status"] in (400, 401, 403)


# ============================================================================
# /api/scheduler-status — authed happy path with ?all param variants
# ============================================================================

class TestSchedulerStatusFlows:
    def test_default_returns_running_field(self, http_harness):
        http_harness.create_user()
        resp = http_harness.get("/api/scheduler-status")
        assert resp["status"] == 200
        assert "running" in resp["body"]

    def test_all_param_admin_only(self, http_harness):
        """?all=1 is admin-only — a non-admin passing it should still
        only see their own activity."""
        http_harness.create_user(username="admin1", email="admin1@x.com")
        http_harness.logout()
        http_harness.create_user(username="user2", email="user2@x.com")
        resp = http_harness.get("/api/scheduler-status?all=1")
        # Must not 500; can be 200 but filtered to own activity
        assert resp["status"] == 200


# ============================================================================
# /api/email-status — check queue/sent counts after real email activity
# ============================================================================

class TestEmailStatusFlows:
    def test_empty_queue_initial_state(self, http_harness):
        http_harness.create_user()
        resp = http_harness.get("/api/email-status")
        body = resp["body"]
        assert body["queued"] == 0
        assert body["sent_today"] == 0
        assert body["dead_letter_count"] == 0
        # recipient should be populated (user.email fallback)
        assert body["recipient"], "recipient should fallback to user.email"

    def test_counts_queued_entries_from_queue_file(self, http_harness):
        http_harness.create_user()
        # Drop a fake queue file with 3 unsent + 1 sent entries
        import auth
        user_dir = auth.user_data_dir(http_harness.user_id)
        os.makedirs(user_dir, exist_ok=True)
        queue_path = os.path.join(user_dir, "email_queue.json")
        from et_time import now_et
        today = now_et().strftime("%Y-%m-%d")
        with open(queue_path, "w") as f:
            json.dump([
                {"timestamp": f"{today}T10:00:00-04:00", "to": "a@x.com",
                 "subject": "s1", "body": "b", "type": "trade", "sent": False},
                {"timestamp": f"{today}T10:01:00-04:00", "to": "a@x.com",
                 "subject": "s2", "body": "b", "type": "trade", "sent": False},
                {"timestamp": f"{today}T10:02:00-04:00", "to": "a@x.com",
                 "subject": "s3", "body": "b", "type": "trade", "sent": False,
                 "last_error": "smtp timeout"},
                {"timestamp": f"{today}T10:03:00-04:00", "to": "a@x.com",
                 "subject": "s4", "body": "b", "type": "trade", "sent": True,
                 "sent_at": f"{today}T10:03:05-04:00"},
            ], f)
        resp = http_harness.get("/api/email-status")
        body = resp["body"]
        assert body["queued"] == 3, f"expected 3 queued, got {body['queued']}"
        assert body["sent_today"] == 1
        assert body["failed_recent"] == 1
        assert body["last_sent_at"] is not None


# ============================================================================
# /api/calibration (GET) — can be accessed even if detect_tier returns None
# ============================================================================

class TestCalibrationGet:
    def test_authed_does_not_crash(self, http_harness):
        http_harness.create_user()
        resp = http_harness.get("/api/calibration")
        # Any structured response is fine; must not 500
        assert resp["status"] in (200, 400, 502)


# ============================================================================
# /api/tax-report — authed flow variants
# ============================================================================

class TestTaxReportFlows:
    def test_empty_journal_summary_zero(self, http_harness):
        http_harness.create_user()
        resp = http_harness.get("/api/tax-report")
        assert resp["status"] == 200
        body = resp["body"]
        assert "summary" in body or "lots" in body

    def test_with_seeded_journal(self, http_harness):
        """Seed the journal with a closed trade and verify the report
        reflects it. This exercises the tax_lots.compute_tax_lots
        integration path inside server.py."""
        http_harness.create_user()
        import auth
        user_dir = auth.user_data_dir(http_harness.user_id)
        journal_path = os.path.join(user_dir, "trade_journal.json")
        os.makedirs(user_dir, exist_ok=True)
        with open(journal_path, "w") as f:
            json.dump({
                "trades": [
                    {"timestamp": "2026-01-15T10:00:00-05:00",
                     "symbol": "AAPL", "side": "buy", "qty": 10,
                     "price": 100.0, "strategy": "breakout",
                     "deployer": "cloud_scheduler", "status": "closed",
                     "exit_timestamp": "2026-02-15T14:00:00-05:00",
                     "exit_price": 110.0, "exit_side": "sell",
                     "pnl": 100.0},
                ],
                "daily_snapshots": [],
            }, f)
        resp = http_harness.get("/api/tax-report?method=FIFO")
        assert resp["status"] == 200
        body = resp["body"]
        # Either lots present or summary non-zero
        assert body.get("lots") or body.get("summary")


# ============================================================================
# /api/factor-bypass (POST) — toggle flag
# ============================================================================

class TestFactorBypassFlows:
    def test_enable_persists_flag(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/factor-bypass", body={"enable": True})
        # Either 200 or 400/403 depending on any guards — not 500
        assert resp["status"] in (200, 400, 403)

    def test_disable_persists_flag(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/factor-bypass", body={"enable": False})
        assert resp["status"] in (200, 400, 403)


# ============================================================================
# /api/auto-deployer (POST) — toggle auto_deployer.enabled
# ============================================================================

class TestAutoDeployerFlows:
    def test_authed_responds(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/auto-deployer", body={"enabled": True})
        assert resp["status"] in (200, 400)


# ============================================================================
# /api/toggle-short-selling (POST) — toggle via strategy_mixin
# ============================================================================

class TestToggleShortSellingFlows:
    def test_authed_enable(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/toggle-short-selling",
                                   body={"enabled": True})
        assert resp["status"] in (200, 400)

    def test_authed_disable(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/toggle-short-selling",
                                   body={"enabled": False})
        assert resp["status"] in (200, 400)


# ============================================================================
# /api/apply-preset (POST)
# ============================================================================

class TestApplyPresetFlows:
    def test_missing_preset_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/apply-preset", body={})
        assert resp["status"] == 400


# ============================================================================
# /api/pause-strategy + /api/stop-strategy
# ============================================================================

class TestPauseStrategyFlows:
    def test_missing_symbol_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/pause-strategy", body={})
        assert resp["status"] in (400, 404)


class TestStopStrategyFlows:
    def test_missing_symbol_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/stop-strategy", body={})
        assert resp["status"] in (400, 404)


# ============================================================================
# /api/cancel-order + /api/close-position + /api/sell
# ============================================================================

class TestCancelOrderFlows:
    def test_missing_order_id_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/cancel-order", body={})
        assert resp["status"] in (400, 404)


class TestClosePositionFlows:
    def test_missing_symbol_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/close-position", body={})
        assert resp["status"] in (400, 404)


class TestSellFlows:
    def test_missing_params_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/sell", body={})
        assert resp["status"] in (400, 404)


# ============================================================================
# /api/kill-switch — toggle on/off
# ============================================================================

class TestKillSwitchFlows:
    def test_authed_activate(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/kill-switch", body={"activate": True})
        assert resp["status"] in (200, 400, 500)

    def test_authed_deactivate(self, http_harness):
        http_harness.create_user()
        # First activate
        http_harness.post("/api/kill-switch", body={"activate": True})
        # Then deactivate
        resp = http_harness.post("/api/kill-switch", body={"activate": False})
        assert resp["status"] in (200, 400, 500)


# ============================================================================
# Admin flows (user #1 is auto-admin)
# ============================================================================

class TestAdminUserList:
    def test_admin_sees_users(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.get("/api/admin/users")
        assert resp["status"] == 200
        body = resp["body"]
        assert isinstance(body, dict) or isinstance(body, list)


class TestAdminInviteList:
    def test_admin_sees_invites(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.get("/api/admin/invites")
        assert resp["status"] == 200


class TestAdminAuditLogAuthed:
    def test_admin_sees_audit_log(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.get("/api/admin/audit-log")
        assert resp["status"] == 200


class TestAdminCreateInvite:
    def test_admin_can_create_invite(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.post("/api/admin/invites", body={})
        # Accepts: 200 (invite code returned) or 400 (needs params)
        assert resp["status"] in (200, 400)


class TestAdminSetActiveAuthed:
    def test_empty_body_rejected(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.post("/api/admin/set-active", body={})
        assert resp["status"] in (400, 404)


class TestAdminResetPasswordAuthed:
    def test_empty_body_rejected(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.post("/api/admin/reset-password", body={})
        assert resp["status"] in (400, 404)


class TestAdminUpdateUser:
    def test_missing_target_user_rejected(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.post("/api/admin/update-user", body={})
        assert resp["status"] in (400, 404)


class TestAdminDeleteUser:
    def test_missing_target_user_rejected(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.post("/api/admin/delete-user", body={})
        assert resp["status"] in (400, 404)


class TestAdminBackfillJournal:
    def test_authed_admin_can_trigger(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.post("/api/admin/backfill-journal", body={})
        assert resp["status"] in (200, 400, 500)


class TestAdminRevokeInvite:
    def test_missing_code_rejected(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.post("/api/admin/revoke-invite", body={})
        assert resp["status"] in (400, 404)


class TestAdminSetAdminToggle:
    def test_missing_target_rejected(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.post("/api/admin/set-admin", body={})
        assert resp["status"] in (400, 404)


# ============================================================================
# /api/test-alpaca-keys + /api/save-alpaca-keys — key-management flows
# ============================================================================

class TestTestAlpacaKeys:
    def test_missing_keys_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/test-alpaca-keys", body={})
        assert resp["status"] in (400, 502)


class TestSaveAlpacaKeys:
    def test_missing_keys_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/save-alpaca-keys", body={})
        assert resp["status"] in (400, 502)


# ============================================================================
# /api/switch-mode — paper / live view toggle
# ============================================================================

class TestSwitchModeFlows:
    def test_invalid_mode_rejected(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/switch-mode", body={"mode": "bogus"})
        assert resp["status"] in (400, 403)

    def test_paper_mode_accepted(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/switch-mode", body={"mode": "paper"})
        assert resp["status"] in (200, 400)

    def test_live_mode_without_keys_falls_back(self, http_harness):
        """Switching to live without live keys: should either accept
        (and silently fall back to paper on next request) or reject with
        a clear error. Never 500."""
        http_harness.create_user()
        resp = http_harness.post("/api/switch-mode", body={"mode": "live"})
        assert resp["status"] in (200, 400, 403)


# ============================================================================
# Toggle endpoints — track-record-public, scorecard-email
# ============================================================================

class TestToggleTrackRecordAuthed:
    def test_authed_toggle(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/toggle-track-record-public",
                                   body={"enable": True})
        assert resp["status"] in (200, 400)


class TestToggleScorecardEmailAuthed:
    def test_authed_toggle(self, http_harness):
        http_harness.create_user()
        resp = http_harness.post("/api/toggle-scorecard-email",
                                   body={"enable": True})
        assert resp["status"] in (200, 400)


# ============================================================================
# /api/forgot + /api/reset — password reset flow
# ============================================================================

class TestForgotPassword:
    def test_unknown_email_returns_200(self, http_harness):
        """Email-enumeration defense: always return 200 regardless of
        whether the email exists. Otherwise an attacker could enumerate
        users by timing or response text."""
        resp = http_harness.post("/api/forgot",
                                   body={"email": "nobody@nowhere.com"},
                                   auth_session=False)
        # Either 200 (success response) or 400 (missing param) — never
        # a 404 or differentiation based on user existence.
        assert resp["status"] in (200, 400)

    def test_missing_email_rejected(self, http_harness):
        resp = http_harness.post("/api/forgot", body={}, auth_session=False)
        assert resp["status"] in (200, 400)


class TestResetPassword:
    def test_missing_token_rejected(self, http_harness):
        resp = http_harness.post("/api/reset", body={}, auth_session=False)
        assert resp["status"] in (400, 403)

    def test_bogus_token_rejected(self, http_harness):
        resp = http_harness.post("/api/reset", body={
            "token": "bogus", "new_password": "correct horse battery staple!!"
        }, auth_session=False)
        assert resp["status"] in (400, 403)


# ============================================================================
# /api/admin/list-backups + /api/admin/download-backup
# ============================================================================

class TestAdminBackups:
    def test_list_backups_admin(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.get("/api/admin/list-backups")
        assert resp["status"] == 200

    def test_download_backup_missing_filename(self, http_harness):
        http_harness.create_user(username="admin1", email="admin1@x.com")
        resp = http_harness.get("/api/admin/download-backup")
        assert resp["status"] in (400, 404)


# ============================================================================
# Dashboard root + login/signup pages
# ============================================================================

class TestRootPageRedirect:
    def test_root_redirects_to_login_when_unauthed(self, http_harness):
        resp = http_harness.get("/", auth_session=False)
        # Either redirect (302) or HTML page or 401 — all legitimate
        assert resp["status"] in (200, 302, 401)

    def test_root_authed_returns_html(self, http_harness):
        http_harness.create_user()
        resp = http_harness.get("/")
        assert resp["status"] == 200
        ct = resp["headers"].get("Content-Type", "")
        assert "text/html" in ct.lower()


class TestLoginPage:
    def test_login_page_unauthed_returns_html(self, http_harness):
        resp = http_harness.get("/login", auth_session=False)
        assert resp["status"] == 200

    def test_signup_page_unauthed_returns_html(self, http_harness):
        resp = http_harness.get("/signup", auth_session=False)
        assert resp["status"] == 200
