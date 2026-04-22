"""
Admin-only HTTP handlers. Each handler checks is_admin before performing
any action. Mixed into DashboardHandler via MRO.
"""
import json
import urllib.parse

import auth
import os
# Lazy server-module proxy: resolves `server.X` references at first
# *call* time, not import time. Required because server.py is launched
# as `python3 server.py` which makes it __main__, so `import server`
# at mixin import time re-executes server.py and crashes on the
# circular import (server -> mixin -> server). By then server.py has
# finished loading so the attribute lookup succeeds.
import sys as _sys
class _ServerProxy:
    def __getattr__(self, name):
        s = _sys.modules.get("server") or _sys.modules.get("__main__")
        if s is None:
            import server as _s
            s = _s
        return getattr(s, name)
server = _ServerProxy()


class AdminHandlerMixin:
    def handle_admin_set_active(self, body):
        """Admin-only: set is_active on any user."""
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        target_id = body.get("user_id")
        is_active = body.get("is_active")
        if target_id is None or is_active is None:
            return self.send_json({"error": "user_id and is_active required"}, 400)
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return self.send_json({"error": "user_id must be integer"}, 400)

        # Block last-admin deactivation
        if not is_active:
            try:
                conn = auth._get_db()
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*) FROM users
                    WHERE is_admin = 1 AND is_active = 1 AND id != ?
                """, (target_id,))
                others = cur.fetchone()[0]
                cur.execute("SELECT is_admin FROM users WHERE id = ?", (target_id,))
                row = cur.fetchone()
                conn.close()
                if row and row[0] and others == 0:
                    return self.send_json({"error":
                        "Cannot deactivate the last active admin"}, 400)
            except Exception:
                pass

        try:
            conn = auth._get_db()
            cur = conn.cursor()
            cur.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if is_active else 0, target_id))
            if not is_active:
                cur.execute("DELETE FROM sessions WHERE user_id = ?", (target_id,))
            conn.commit()
            conn.close()
            auth.log_admin_action(
                "reactivate_user" if is_active else "deactivate_user",
                actor=self.current_user,
                target_user_id=target_id,
                ip_address=self.client_address[0] if self.client_address else None,
            )
            self.send_json({"success": True})
        except Exception as e:
            self._send_error_safe(e, 500, "admin-op")
    def handle_admin_reset_password(self, body):
        """Admin-only: force a password reset for any user.
        An admin CANNOT reset another admin's password (prevents insider
        takeover). Admins who want to change their own password use the
        standard /api/change-password flow. Only self-reset allowed here.
        """
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        target_id = body.get("user_id")
        new_password = body.get("new_password") or ""
        if target_id is None:
            return self.send_json({"error": "user_id required"}, 400)
        if len(new_password) < 8:
            return self.send_json({"error": "Password must be at least 8 characters"}, 400)
        try:
            target_id = int(target_id)
            # Block cross-admin reset unless target == actor (self-reset OK)
            if target_id != self.current_user.get("id"):
                target_user = auth.get_user_by_id(target_id)
                if target_user and target_user.get("is_admin"):
                    return self.send_json({
                        "error": "Cannot reset another admin's password. "
                                 "The target admin must use password-reset themselves."
                    }, 403)
            ok, err = auth.change_password(target_id, new_password)
            if not ok:
                return self.send_json({"error": err or "Password rejected"}, 400)
            # Invalidate all sessions for that user so they're forced to re-login
            conn = auth._get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM sessions WHERE user_id = ?", (target_id,))
            conn.commit()
            conn.close()
            auth.log_admin_action(
                "admin_reset_password",
                actor=self.current_user,
                target_user_id=target_id,
                ip_address=self.client_address[0] if self.client_address else None,
            )
            self.send_json({"success": True})
        except Exception as e:
            self._send_error_safe(e, 500, "admin-op")
    def handle_admin_create_backup(self):
        """Admin-only: create an on-demand backup of the Railway volume."""
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        try:
            import backup as _backup
            path, size, err = _backup.create_backup()
            if err:
                return self.send_json({"error": f"Backup failed: {err}"}, 500)
            auth.log_admin_action("backup_created_manual",
                                   actor=self.current_user,
                                   ip_address=self.client_address[0] if self.client_address else None,
                                   detail={"backup_path": os.path.basename(path),
                                           "size_mb": round(size / 1024 / 1024, 2)})
            self.send_json({
                "success": True,
                "name": os.path.basename(path),
                "size_mb": round(size / 1024 / 1024, 2),
            })
        except Exception as e:
            self._send_error_safe(e, 500, "create-backup")

    def handle_admin_create_invite(self, body):
        """Admin-only: generate a single-use signup invite. Returns the
        plaintext token ONCE in the response (never stored plaintext —
        only SHA-256 hash lands in the DB). Admin copies the URL and
        shares it with the invitee; on signup the token is consumed
        and can't be reused.

        Body: {"note": "friend1", "days": 7}  — both optional."""
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        note = (body.get("note") or "")[:80]
        days = body.get("days")
        try:
            days_i = int(days) if days else None
            if days_i is not None and (days_i < 1 or days_i > 30):
                return self.send_json(
                    {"error": "days must be between 1 and 30"}, 400)
        except (TypeError, ValueError):
            return self.send_json({"error": "days must be an integer"}, 400)
        try:
            token = auth.create_invite(self.current_user["id"],
                                        note=note,
                                        days_valid=days_i)
            auth.log_admin_action(
                "create_invite",
                actor=self.current_user,
                ip_address=self.client_address[0] if self.client_address else None,
                detail={"note": note, "days": days_i or auth.INVITE_DAYS})
            # Build the signup URL using the request host — works on
            # both Railway prod + localhost dev without hardcoding.
            host = self.headers.get("Host", "localhost")
            scheme = "https" if self.headers.get(
                "X-Forwarded-Proto", "").lower() == "https" else "http"
            signup_url = f"{scheme}://{host}/signup?invite={token}"
            return self.send_json({
                "success": True,
                "token": token,
                "signup_url": signup_url,
                "expires_in_days": days_i or auth.INVITE_DAYS,
            })
        except Exception as e:
            self._send_error_safe(e, 500, "create-invite")

    def handle_admin_list_invites(self):
        """Admin-only: list all invites the admin has created (hashes
        only — plaintext was shown once at creation time)."""
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        try:
            rows = auth.list_invites(self.current_user["id"])
            # Strip the hash from the response — it's useless client-side
            # and leaking the hash doesn't help but isn't needed either.
            # Enrich with computed status: active / used / expired.
            from et_time import now_et as _now_et
            now_iso = _now_et().isoformat()
            out = []
            for r in rows:
                status = "used" if r.get("used_at") else (
                    "expired" if r["expires_at"] < now_iso else "active")
                out.append({
                    "token_hash": r["token_hash"],
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "used_at": r.get("used_at"),
                    "used_by_user_id": r.get("used_by_user_id"),
                    "note": r.get("note") or "",
                    "status": status,
                })
            self.send_json({"invites": out})
        except Exception as e:
            self._send_error_safe(e, 500, "list-invites")

    def handle_admin_revoke_invite(self, body):
        """Round-36: admin-only revoke of an UNUSED invite.

        Body: {"token_hash": "<hash-from-list>"}

        Backed by auth.revoke_invite which sets expires_at in the past.
        Already-used invites are left alone (revoking a consumed invite
        has no effect and would obscure the audit trail). Writes an
        admin audit log entry."""
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        token_hash = (body.get("token_hash") or "").strip()
        if not token_hash or len(token_hash) > 128:
            return self.send_json({"error": "Missing or invalid token_hash"}, 400)
        try:
            ok = auth.revoke_invite(token_hash)
            if not ok:
                return self.send_json(
                    {"error": "No matching unused invite (already used or not found)"},
                    404)
            auth.log_admin_action(
                "revoke_invite",
                actor=self.current_user,
                ip_address=self.client_address[0] if self.client_address else None,
                detail={"token_hash_prefix": token_hash[:12] + "..."})
            return self.send_json({"success": True})
        except Exception as e:
            self._send_error_safe(e, 500, "revoke-invite")

    def handle_admin_set_admin(self, body):
        """Round-36: admin-only promote/demote of another user's
        `is_admin` flag. Guards against demoting the last active admin.

        Body: {"user_id": 12, "is_admin": true}"""
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        target_id = body.get("user_id")
        want_admin = bool(body.get("is_admin"))
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return self.send_json({"error": "Missing or invalid user_id"}, 400)
        if target_id <= 0:
            return self.send_json({"error": "Invalid user_id"}, 400)
        try:
            ok, err = auth.set_user_admin(target_id, want_admin)
            if not ok:
                return self.send_json(
                    {"error": err or "Update failed"}, 400)
            auth.log_admin_action(
                "set_admin" if want_admin else "revoke_admin",
                actor=self.current_user,
                target_user_id=target_id,
                ip_address=self.client_address[0] if self.client_address else None,
                detail={"is_admin": want_admin})
            return self.send_json({"success": True})
        except Exception as e:
            self._send_error_safe(e, 500, "set-admin")

    def handle_admin_update_user(self, body):
        """Round-37: admin-only edit of a user's email and/or username.

        Body: {"user_id": 12, "email": "new@x.com", "username": "newname"}
        Either field may be omitted; None / missing means "leave alone".
        Uniqueness is enforced at the DB level; the helper returns a
        friendly error if the new email/username collides with another
        active user.
        """
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        target_id = body.get("user_id")
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return self.send_json({"error": "Missing or invalid user_id"}, 400)
        if target_id <= 0:
            return self.send_json({"error": "Invalid user_id"}, 400)
        email = body.get("email")
        username = body.get("username")
        # Distinguish "field not provided" from "field provided as empty
        # string" — empty string should fail validation; None leaves alone.
        email = email if (email is None or isinstance(email, str)) else None
        username = username if (username is None or isinstance(username, str)) else None
        if email is None and username is None:
            return self.send_json({"error": "No fields to update"}, 400)
        try:
            ok, err = auth.update_user(
                target_id, email=email, username=username)
            if not ok:
                return self.send_json({"error": err or "Update failed"}, 400)
            auth.log_admin_action(
                "update_user",
                actor=self.current_user,
                target_user_id=target_id,
                ip_address=self.client_address[0] if self.client_address else None,
                detail={
                    "email_changed": email is not None,
                    "username_changed": username is not None,
                })
            return self.send_json({"success": True})
        except Exception as e:
            self._send_error_safe(e, 500, "update-user")

    def handle_admin_delete_user(self, body):
        """Round-40: admin-only permanent delete of a user account.

        Destructive. Cascades through per-user data dir, invites, password
        reset tokens. Preserves audit_log entries by default so the trail
        of "who deleted this user" stays intact.

        Body: {"user_id": 12}
        """
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        target_id = body.get("user_id")
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return self.send_json({"error": "Missing or invalid user_id"}, 400)
        if target_id <= 0:
            return self.send_json({"error": "Invalid user_id"}, 400)
        # Capture username BEFORE deletion so the audit log entry is
        # meaningful — can't SELECT username after DELETE cascaded.
        pre = auth.get_user_by_id(target_id)
        if not pre:
            return self.send_json({"error": "User not found"}, 404)
        try:
            ok, err = auth.delete_user(
                target_id,
                actor_user_id=self.current_user.get("id"))
            if not ok:
                return self.send_json({"error": err or "Delete failed"}, 400)
            auth.log_admin_action(
                "delete_user",
                actor=self.current_user,
                target_user_id=target_id,
                ip_address=self.client_address[0] if self.client_address else None,
                detail={
                    "deleted_username": pre.get("username"),
                    "deleted_email": pre.get("email"),
                })
            return self.send_json({"success": True})
        except Exception as e:
            self._send_error_safe(e, 500, "delete-user")

    def handle_admin_export_user_data(self):
        """Round-40: admin-only GDPR / CCPA-style data export for a user.

        Query: ?user_id=12
        Returns a ZIP attachment with JSON files covering the user's
        profile, sessions, audit log, invites, strategies, trade
        journal, scorecard, learned weights, and guardrails.
        Sensitive fields (password hash, encrypted Alpaca keys) are
        SANITIZED before export.
        """
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        import urllib.parse as _up
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = _up.parse_qs(qs)
        try:
            target_id = int((params.get("user_id") or ["0"])[0])
        except (TypeError, ValueError):
            return self.send_json({"error": "Missing or invalid user_id"}, 400)
        if target_id <= 0:
            return self.send_json({"error": "Invalid user_id"}, 400)
        pre = auth.get_user_by_id(target_id)
        if not pre:
            return self.send_json({"error": "User not found"}, 404)
        try:
            zip_bytes, err = auth.export_user_data(target_id)
            if err:
                return self.send_json({"error": err}, 400)
            auth.log_admin_action(
                "export_user_data",
                actor=self.current_user,
                target_user_id=target_id,
                ip_address=self.client_address[0] if self.client_address else None,
                detail={"size_bytes": len(zip_bytes)})
            # Return as downloadable ZIP
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="user_{target_id}_data_export.zip"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)
        except Exception as e:
            self._send_error_safe(e, 500, "export-user-data")

    def handle_admin_backfill_journal(self, body):
        """Round-40: one-shot backfill of the current user's trade journal
        from their Alpaca positions. Adds "open" entries for positions
        deployed before round-33's journal fix shipped. Idempotent.

        Body: {} — operates on the caller's own journal (not another
        user's). Kept admin-only for now since running it on someone
        else's journal would be a surprising privacy action; users
        should be able to run it on themselves if we ever expose a
        self-service version.
        """
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        try:
            import journal_backfill
            # Use the handler's own Alpaca fetcher for the caller's
            # creds. This intentionally only backfills the caller's
            # own journal — if you need to backfill another user's
            # journal, run it from their login.
            def _fetch(user):
                return self.user_api_get(f"{self.user_api_endpoint}/positions")
            result = journal_backfill.backfill_user_journal(
                self.current_user, _fetch)
            auth.log_admin_action(
                "journal_backfill",
                actor=self.current_user,
                target_user_id=self.current_user.get("id"),
                ip_address=self.client_address[0] if self.client_address else None,
                detail=result)
            return self.send_json({"success": True, "result": result})
        except Exception as e:
            self._send_error_safe(e, 500, "backfill-journal")

    def handle_admin_backfill_wheel_opens(self, body):
        """Round-43: retroactively recover entry prices for orphan_close
        wheel entries by walking wheel_*.json history[]. Turns [orphan]
        entries into proper closes with pnl_pct.

        Body: {} — operates on the caller's journal only. Idempotent.
        """
        if not self.current_user or not self.current_user.get("is_admin"):
            return self.send_json({"error": "Admin only"}, 403)
        try:
            import wheel_open_backfill
            result = wheel_open_backfill.backfill_wheel_opens(self.current_user)
            auth.log_admin_action(
                "wheel_open_backfill",
                actor=self.current_user,
                target_user_id=self.current_user.get("id"),
                ip_address=self.client_address[0] if self.client_address else None,
                detail=result)
            return self.send_json({"success": True, "result": result})
        except Exception as e:
            self._send_error_safe(e, 500, "backfill-wheel-opens")
