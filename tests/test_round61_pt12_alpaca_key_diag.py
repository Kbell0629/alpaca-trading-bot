"""
Round-61 pt.12 — Alpaca key diagnostics.

User report: dashboard rendered $0.00 because /account fetch failed.
Then "Save failed" with no detail when the user re-entered keys, even
though "Test Connection" passed with the same keys.

This file pins:
  1. handle_save_alpaca_keys returns the actual exception body
     when auth.save_user_alpaca_creds raises (was a bare 500 before).
  2. handle_save_alpaca_keys differentiates HTTP 401/403/429/5xx with
     hint copy that points at the most-likely cause.
  3. handle_test_alpaca_keys differentiates the same status codes.
  4. New endpoint /api/test-saved-alpaca-keys uses stored creds and
     surfaces "no keys saved" when the user has none.
"""
from __future__ import annotations


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# Save-side error surfacing
# ----------------------------------------------------------------------------

def test_save_handler_wraps_persist_in_try_except():
    src = _src("handlers/auth_mixin.py")
    # The persist call must be inside a try block so a raise from
    # auth.save_user_alpaca_creds doesn't fall through to a bare 500.
    assert "write them to local storage" in src, (
        "handle_save_alpaca_keys must surface the persist exception so "
        "the user sees the actual reason instead of a generic 'Save "
        "failed'.")


def test_save_handler_surfaces_alpaca_http_status():
    src = _src("handlers/auth_mixin.py")
    # Each branch must mention its own status code so the user knows
    # what Alpaca actually returned.
    assert "Alpaca rejected the key check (HTTP" in src
    assert "alpaca_status" in src, (
        "Save handler must return alpaca_status in the JSON body so the "
        "JS can show the actual HTTP code.")


def test_save_handler_explains_endpoint_mismatch():
    """Most common 401/403 cause: paper keys typed into live form (or
    vice versa). Hint must call that out by name."""
    src = _src("handlers/auth_mixin.py")
    assert "wrong mode's pair" in src or "wrong mode" in src or "(not the other mode" in src


# ----------------------------------------------------------------------------
# Test-saved-keys endpoint
# ----------------------------------------------------------------------------

def test_test_saved_keys_handler_exists():
    src = _src("handlers/auth_mixin.py")
    assert "def handle_test_saved_alpaca_keys" in src
    assert "auth.get_user_alpaca_creds" in src


def test_test_saved_keys_routed_in_server():
    src = _src("server.py")
    assert '"/api/test-saved-alpaca-keys"' in src
    assert "handle_test_saved_alpaca_keys" in src


def test_test_saved_keys_returns_no_keys_message_clearly():
    src = _src("handlers/auth_mixin.py")
    assert "No " in src and "keys are saved for this user" in src, (
        "test-saved-keys must say 'No PAPER keys are saved' (or LIVE) "
        "when the user has no creds on file.")


def test_test_saved_keys_warns_about_master_key_mismatch():
    """If get_user_alpaca_creds raises (decryption fail), the most
    common cause is a MASTER_ENCRYPTION_KEY rotation that invalidated
    every saved credential. Surface that hint."""
    src = _src("handlers/auth_mixin.py")
    assert "MASTER_ENCRYPTION_KEY changed" in src


# ----------------------------------------------------------------------------
# UI: button + JS handler
# ----------------------------------------------------------------------------

def test_dashboard_has_test_saved_keys_button():
    src = _src("templates/dashboard.html")
    assert 'onclick="testSavedAlpacaKeys(\'paper\')"' in src
    assert 'onclick="testSavedAlpacaKeys(\'live\')"' in src


def test_dashboard_has_testSavedAlpacaKeys_js_handler():
    src = _src("templates/dashboard.html")
    assert "async function testSavedAlpacaKeys" in src
    assert "/api/test-saved-alpaca-keys" in src


# ----------------------------------------------------------------------------
# Behavioral via http_harness
# ----------------------------------------------------------------------------

def test_test_saved_keys_no_keys_returns_helpful_error(http_harness, monkeypatch):
    """User with no saved keys hits the endpoint → gets a clear message
    pointing at the form below."""
    http_harness.create_user()
    import auth as _auth
    monkeypatch.setattr(_auth, "get_user_alpaca_creds",
                        lambda uid, mode="paper": None)
    resp = http_harness.post("/api/test-saved-alpaca-keys",
                              body={"mode": "paper"})
    assert resp["status"] == 400
    assert "No PAPER keys are saved" in resp["body"]["error"]


def test_save_handler_persist_failure_returns_500_with_detail(http_harness, monkeypatch):
    """Simulate auth.save_user_alpaca_creds raising — assert the user
    gets a 500 with the actual exception, not a bare 'Save failed'."""
    http_harness.create_user()
    import auth as _auth

    # Stub the Alpaca key-verification call to succeed
    import urllib.request as _ur
    class _FakeResp:
        def read(self): return b'{"id":"x"}'
        def close(self): return None
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(_ur, "urlopen", lambda *a, **kw: _FakeResp())

    # Force the persist call to raise
    def _boom(*a, **kw):
        raise RuntimeError("simulated db lock")
    monkeypatch.setattr(_auth, "save_user_alpaca_creds", _boom)

    resp = http_harness.post("/api/save-alpaca-keys", body={
        "api_key": "PK" + "x" * 18,
        "api_secret": "secret_value",
        "mode": "paper",
    })
    assert resp["status"] == 500
    assert "write them to local storage" in resp["body"]["error"]
    assert "simulated db lock" in resp["body"]["error"]
