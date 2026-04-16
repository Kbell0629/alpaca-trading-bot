"""
Admin-only HTTP handlers. Each handler checks is_admin before performing
any action. Mixed into DashboardHandler via MRO.
"""
import json
import urllib.parse

import auth


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
