"""
Round-61 pt.6: first behavioral tests for server.py via the mock WSGI
harness. Smoke tests + a handful of endpoints to validate the harness
works end-to-end before the big coverage push.

server.py was 7% covered (1708 statements). This file opens the door;
follow-up commits on this branch add more per-endpoint coverage.
"""
from __future__ import annotations

import json


# ============================================================================
# Harness smoke tests — confirm the fixture works at all.
# ============================================================================

def test_harness_basic_fixture_loads(http_harness):
    """Creating a user + getting a session cookie should succeed."""
    uid = http_harness.create_user()
    assert uid > 0
    assert http_harness.session_token, "create_user must leave a session cookie"


def test_harness_returns_structured_response(http_harness):
    """Every request should return a dict with status, headers, body."""
    resp = http_harness.get("/api/version")
    assert "status" in resp
    assert "headers" in resp
    assert "body" in resp
    assert resp["status"] in (200, 401), f"unexpected status: {resp['status']}"


# ============================================================================
# Public endpoints — no auth required.
# ============================================================================

class TestApiVersion:
    def test_returns_200(self, http_harness):
        resp = http_harness.get("/api/version")
        assert resp["status"] == 200

    def test_body_has_version_fields(self, http_harness):
        resp = http_harness.get("/api/version")
        body = resp["body"]
        assert isinstance(body, dict)
        # Must carry at least one of: bot_version, commit, git_describe
        # (the exact set depends on env but at least bot_version is
        # always populated from __version__).
        assert "bot_version" in body or "commit" in body or "git_describe" in body, (
            f"version response missing version fields: {body}")

    def test_no_auth_required(self, http_harness):
        """Version endpoint is public — no cookie, still 200."""
        resp = http_harness.get("/api/version", auth_session=False)
        assert resp["status"] == 200

    def test_content_type_is_json(self, http_harness):
        resp = http_harness.get("/api/version")
        assert "application/json" in resp["headers"].get("Content-Type", "")
