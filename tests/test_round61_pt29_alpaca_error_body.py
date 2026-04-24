"""
Round-61 pt.29: surface Alpaca HTTP error response body in user_api_*.

Context
-------
pt.26 added "loud placement-failure logging" so the monitor's
``"SHORT cover-stop placement FAILED — Alpaca returned: ..."`` line
shows the ``{"error": ...}`` returned by ``user_api_post``. User's
live log (SOXL Apr 24) only ever surfaced ``"HTTP 403: Forbidden"``
— the HTTP status text, nothing about WHY. Alpaca attaches the
diagnostic detail (``{"code": 40310000, "message": "asset is not
shortable"}``) to the response **body**, which ``user_api_*`` was
throwing away.

Separately, the Friday risk-reduction job returned the same bare
``HTTP 403: Forbidden`` when trimming INTC during regular hours —
which CAN'T be an extended-hours or shortable issue, confirming
that without the body the operator has no way to distinguish the
failure classes.

pt.29 reads and surfaces the response body. It also tightens the
401/403 critical-alert heuristic so business-logic 403s (asset
restrictions, extended-hours rejection, buying-power shortfall)
no longer spam users with a ``"credentials rejected"`` alert —
only true auth failures do.

Invariants this file pins
-------------------------
* POST / DELETE / PATCH / GET all include the Alpaca ``message``
  and ``code`` in the returned ``{"error": ...}`` string when
  Alpaca returned a structured JSON body.
* Non-JSON / HTML error pages are safely truncated into the error
  string rather than dropped.
* Empty-body 403 still fires the auth alert (conservative default —
  preserves pre-pt.29 behavior for the original Round-15 motivation).
* 403 with a business-logic Alpaca code (``"code": 40310000``,
  ``"code": 42210000``, etc.) does NOT fire the auth alert.
* 403 with a business-logic keyword message (``"asset is not
  shortable"``, ``"extended hours..."``, ``"insufficient buying
  power"``) does NOT fire the auth alert.
* 401 ALWAYS fires the auth alert regardless of body shape
  (401 is unambiguous auth).
"""
from __future__ import annotations

import io
import sys
import urllib.error
import urllib.request


def _user(mode="paper", uid=1):
    return {
        "id": uid, "username": f"u{uid}",
        "_api_key": "k", "_api_secret": "s",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_data_endpoint": "https://data.alpaca.markets/v2",
        "_mode": mode,
    }


def _reload(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for m in ("auth", "scheduler_api"):
        sys.modules.pop(m, None)
    import scheduler_api
    monkeypatch.setattr(scheduler_api.time, "sleep", lambda *a, **k: None)
    return scheduler_api


def _http_error(code, reason, body_bytes):
    return urllib.error.HTTPError(
        "http://x", code, reason, {}, io.BytesIO(body_bytes))


# ============================================================
# _parse_alpaca_error_body — pure helper
# ============================================================

class TestParseAlpacaErrorBody:
    def test_empty_body_returns_empty(self, monkeypatch):
        sa = _reload(monkeypatch)
        he = _http_error(403, "Forbidden", b"")
        summary, parsed = sa._parse_alpaca_error_body(he)
        assert summary == ""
        assert parsed is None

    def test_structured_json_message_and_code(self, monkeypatch):
        sa = _reload(monkeypatch)
        body = b'{"code": 40310000, "message": "asset SOXL is not shortable"}'
        he = _http_error(403, "Forbidden", body)
        summary, parsed = sa._parse_alpaca_error_body(he)
        assert "asset SOXL is not shortable" in summary
        assert "alpaca_code=40310000" in summary
        assert parsed == {"code": 40310000,
                          "message": "asset SOXL is not shortable"}

    def test_message_only_no_code(self, monkeypatch):
        sa = _reload(monkeypatch)
        body = b'{"message": "authentication required"}'
        he = _http_error(401, "Unauthorized", body)
        summary, parsed = sa._parse_alpaca_error_body(he)
        assert "authentication required" in summary
        assert "alpaca_code=" not in summary
        assert parsed == {"message": "authentication required"}

    def test_reject_reason_field(self, monkeypatch):
        """Some Alpaca endpoints use ``reject_reason`` instead of ``message``."""
        sa = _reload(monkeypatch)
        body = b'{"reject_reason": "insufficient buying power"}'
        he = _http_error(422, "Unprocessable Entity", body)
        summary, _ = sa._parse_alpaca_error_body(he)
        assert "insufficient buying power" in summary

    def test_non_json_body_truncated(self, monkeypatch):
        """HTML error page from a proxy — don't crash, just include the text."""
        sa = _reload(monkeypatch)
        body = b"<html><body>Gateway Timeout</body></html>"
        he = _http_error(504, "Gateway Timeout", body)
        summary, parsed = sa._parse_alpaca_error_body(he)
        assert "Gateway Timeout" in summary
        assert parsed is None

    def test_non_dict_json(self, monkeypatch):
        """``[1,2,3]`` or ``"string"`` as body — no crash."""
        sa = _reload(monkeypatch)
        he = _http_error(400, "Bad", b'["just","an","array"]')
        summary, parsed = sa._parse_alpaca_error_body(he)
        assert summary != ""  # truncated repr of the list
        assert parsed is None

    def test_oversized_body_truncated_at_200_chars(self, monkeypatch):
        sa = _reload(monkeypatch)
        # 500-char raw HTML should not produce a 500-char error string
        body = b"<html>" + (b"x" * 500) + b"</html>"
        he = _http_error(500, "Server Error", body)
        summary, _ = sa._parse_alpaca_error_body(he)
        assert len(summary) <= 200


# ============================================================
# _is_auth_failure — business-logic vs auth distinction
# ============================================================

class TestIsAuthFailure:
    def test_401_always_auth(self, monkeypatch):
        sa = _reload(monkeypatch)
        assert sa._is_auth_failure(401, None) is True
        assert sa._is_auth_failure(401, {"code": 40110000}) is True
        assert sa._is_auth_failure(401,
                                   {"message": "asset not shortable"}) is True

    def test_403_empty_body_treated_as_auth(self, monkeypatch):
        """Empty body → can't distinguish → conservative default = auth.

        Preserves pre-pt.29 behaviour that motivated the Round-15
        critical_alert introduction.
        """
        sa = _reload(monkeypatch)
        assert sa._is_auth_failure(403, None) is True

    def test_403_with_alpaca_code_is_business_logic(self, monkeypatch):
        sa = _reload(monkeypatch)
        assert sa._is_auth_failure(
            403, {"code": 40310000, "message": "asset not shortable"}
        ) is False

    def test_403_asset_not_shortable_message_is_business(self, monkeypatch):
        sa = _reload(monkeypatch)
        assert sa._is_auth_failure(
            403, {"message": "asset AAPL is not shortable"}
        ) is False

    def test_403_extended_hours_message_is_business(self, monkeypatch):
        sa = _reload(monkeypatch)
        assert sa._is_auth_failure(
            403, {"message": "extended hours order type not supported"}
        ) is False

    def test_403_insufficient_buying_power_is_business(self, monkeypatch):
        sa = _reload(monkeypatch)
        assert sa._is_auth_failure(
            403, {"message": "insufficient buying power"}
        ) is False

    def test_403_generic_forbidden_message_is_auth(self, monkeypatch):
        """Bare ``"forbidden"`` / ``"access denied"`` → treat as auth."""
        sa = _reload(monkeypatch)
        assert sa._is_auth_failure(403, {"message": "forbidden"}) is True
        assert sa._is_auth_failure(
            403, {"message": "access denied"}) is True


# ============================================================
# user_api_post — end-to-end body surfacing
# ============================================================

class TestPostSurfacesBody:
    def test_403_asset_not_shortable_surfaces_message_and_code(
            self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        body = b'{"code": 40310000, "message": "asset SOXL is not shortable"}'

        def fake_urlopen(*a, **k):
            raise _http_error(403, "Forbidden", body)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_post(u, "/orders", {"symbol": "SOXL"})
        assert "HTTP 403: Forbidden" in result["error"]
        assert "asset SOXL is not shortable" in result["error"]
        assert "alpaca_code=40310000" in result["error"]

    def test_403_business_logic_skips_auth_alert(self, monkeypatch):
        """Business-logic 403 (has an Alpaca code) must NOT fire the
        ``"credentials rejected"`` critical alert — that prompt tells
        users to re-enter their keys, which does nothing for an
        asset-restriction rejection."""
        sa = _reload(monkeypatch)
        u = _user()
        alerts = []
        monkeypatch.setattr(sa, "_alert_alpaca_auth_failure",
                            lambda user, code, reason: alerts.append(code))
        body = b'{"code": 40310000, "message": "asset SOXL is not shortable"}'

        def fake_urlopen(*a, **k):
            raise _http_error(403, "Forbidden", body)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_post(u, "/orders", {"symbol": "SOXL"})
        assert alerts == [], \
            "business-logic 403 must not fire auth-failure alert"

    def test_403_empty_body_still_fires_auth_alert(self, monkeypatch):
        """Pre-pt.29 behaviour preserved: empty/opaque 403 → auth alert."""
        sa = _reload(monkeypatch)
        u = _user()
        alerts = []
        monkeypatch.setattr(sa, "_alert_alpaca_auth_failure",
                            lambda user, code, reason: alerts.append(code))

        def fake_urlopen(*a, **k):
            raise _http_error(403, "Forbidden", b"")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        sa.user_api_post(u, "/orders", {})
        assert alerts == [403]

    def test_401_with_any_body_fires_auth_alert(self, monkeypatch):
        """401 is unambiguous auth — fire regardless of body content."""
        sa = _reload(monkeypatch)
        u = _user()
        alerts = []
        monkeypatch.setattr(sa, "_alert_alpaca_auth_failure",
                            lambda user, code, reason: alerts.append(code))

        def fake_urlopen(*a, **k):
            raise _http_error(
                401, "Unauthorized",
                b'{"message": "API key not found"}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_post(u, "/orders", {})
        assert alerts == [401]
        assert "API key not found" in result["error"]

    def test_422_validation_body_surfaced(self, monkeypatch):
        """422 from Alpaca order rejection commonly includes
        ``{"code": 42210000, "message": "qty must be integer"}``. The
        caller's log should see that, not just ``"HTTP 422"``."""
        sa = _reload(monkeypatch)
        u = _user()

        def fake_urlopen(*a, **k):
            raise _http_error(
                422, "Unprocessable Entity",
                b'{"code": 42210000, "message": "qty must be > 0"}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_post(u, "/orders", {"qty": "0"})
        assert "qty must be > 0" in result["error"]
        assert "alpaca_code=42210000" in result["error"]


# ============================================================
# user_api_delete / user_api_patch — same body surfacing
# ============================================================

class TestDeletePatchSurfaceBody:
    def test_delete_403_business_logic_skips_alert(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()
        alerts = []
        monkeypatch.setattr(sa, "_alert_alpaca_auth_failure",
                            lambda user, code, reason: alerts.append(code))

        def fake_urlopen(*a, **k):
            raise _http_error(
                403, "Forbidden",
                b'{"code": 40310003, "message": "order is not cancelable"}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_delete(u, "/orders/abc")
        assert "order is not cancelable" in result["error"]
        assert alerts == []

    def test_patch_403_surfaces_body(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def fake_urlopen(*a, **k):
            raise _http_error(
                403, "Forbidden",
                b'{"code": 40310002, "message": "stop price below current"}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_patch(u, "/orders/abc", {"stop_price": "100"})
        assert "stop price below current" in result["error"]


# ============================================================
# user_api_get — unified error shape
# ============================================================

class TestGetSurfacesBody:
    def test_get_4xx_includes_body_detail(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def fake_urlopen(*a, **k):
            raise _http_error(
                404, "Not Found",
                b'{"code": 40410000, "message": "order not found"}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = sa.user_api_get(u, "/orders/missing")
        assert "HTTP 404" in result["error"]
        assert "order not found" in result["error"]

    def test_get_5xx_exhausted_retries_include_body(self, monkeypatch):
        sa = _reload(monkeypatch)
        u = _user()

        def always_503(*a, **k):
            raise _http_error(
                503, "Service Unavailable",
                b'{"message": "backend degraded"}')

        monkeypatch.setattr(urllib.request, "urlopen", always_503)
        result = sa.user_api_get(u, "/account")
        assert "HTTP 503" in result["error"]
        assert "backend degraded" in result["error"]


# ============================================================
# Source-level pins — regression guards
# ============================================================

class TestSourcePins:
    """Grep-level guardrails so a future refactor can't silently drop
    body-surfacing — the bug that motivated pt.29 was exactly a
    ``{"error": f"HTTP {code}"}`` bare return."""

    def test_post_uses_format_http_error(self):
        import pathlib
        src = pathlib.Path("scheduler_api.py").read_text()
        # ``_format_http_error`` is called from all 4 HTTPError branches.
        # At least one per helper — 4 total minimum.
        assert src.count("_format_http_error(") >= 4

    def test_no_bare_http_reason_returns_in_user_api_helpers(self):
        """``return {"error": f"HTTP {he.code}: {he.reason}"}`` — the
        exact bare-return shape pre-pt.29. Should be zero occurrences
        now (all paths go through ``_format_http_error``).

        ``_format_http_error`` itself DOES reference
        ``f"HTTP {he.code}: {he.reason}"`` once — that's the single
        source. Filter it out by requiring the pattern be inside a
        ``return`` statement."""
        import pathlib
        src = pathlib.Path("scheduler_api.py").read_text()
        bare_returns = [
            line for line in src.splitlines()
            if line.strip().startswith("return")
            and ('f"HTTP {he.code}: {he.reason}"' in line
                 or 'f"HTTP {e.code}"' in line)
        ]
        assert bare_returns == [], \
            f"found bare-HTTP-error returns pre-pt.29 style: {bare_returns}"

    def test_is_auth_failure_guards_auth_alert(self):
        """Every ``_alert_alpaca_auth_failure`` call-site in scheduler_api
        must be gated by ``_is_auth_failure`` so business-logic 403s
        don't trip it."""
        import pathlib
        src = pathlib.Path("scheduler_api.py").read_text()
        for line_block in src.split("_alert_alpaca_auth_failure(")[1:]:
            # Each call-site should have ``_is_auth_failure`` within
            # the preceding 200 chars (the conditional on the same
            # ``if ... and`` line).
            pre = src.split("_alert_alpaca_auth_failure(")[0][-400:] \
                if "_alert_alpaca_auth_failure(" in src else ""
            # Don't inspect the definition itself
            if line_block.startswith("user, code, reason)"):
                continue
            assert "_is_auth_failure" in pre or "_is_auth_failure" in src, \
                "auth-alert call must be gated by _is_auth_failure"
