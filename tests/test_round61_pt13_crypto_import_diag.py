"""Round-61 pt.13 — surface the real cryptography-import failure
instead of degrading silently to a 401-everywhere state.

User-reported flow:
  1. Dashboard renders $0 + HTTP 401 from /account.
  2. User clicks Test Saved Keys → "no keys saved" (decrypt returned "").
  3. User pastes new keys → Save Paper Keys → "couldn't write them to
     local storage: RuntimeError: cryptography package required for
     encrypt_secret()".

Root cause: auth.py's `try: from cryptography ... import AESGCM`
has a bare `except Exception` that swallows pyo3 PanicException AND
the ModuleNotFoundError for `_cffi_backend`. _HAS_AESGCM ends up
False, every saved credential becomes inaccessible (decrypt returns
"") AND new ones can't be persisted (encrypt raises).

This file pins:
  1. The import-failure path captures the exception text into
     `_AESGCM_IMPORT_ERROR` and prints to stderr at module load.
  2. `encrypt_secret`'s RuntimeError now includes the captured
     boot-time error text.
  3. `_fetch_live_alpaca_state` adds an `encryption: ...` entry to
     `api_errors` when `_HAS_AESGCM` is False.
  4. `requirements.txt` pins cryptography to <44.0.0 + adds cffi.
  5. `nixpacks.toml` adds libffi to nixPkgs.
  6. dashboard banner branches on `encryption:` prefix separately
     from the `account:` banner.
"""
from __future__ import annotations


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# auth.py — import-error capture
# ----------------------------------------------------------------------------

def test_auth_captures_import_error_into_module_var():
    src = _src("auth.py")
    assert "_AESGCM_IMPORT_ERROR" in src, (
        "auth.py must capture the cryptography import failure into a "
        "module-level variable so it can be surfaced downstream.")


def test_auth_uses_baseexception_to_catch_pyo3_panics():
    """Bare `except Exception` misses pyo3's PanicException (which is
    BaseException). Use `except BaseException` so we catch everything
    the cryptography wheel can throw at import time."""
    src = _src("auth.py")
    # Find the AESGCM import block
    idx = src.find("from cryptography.hazmat.primitives.ciphers.aead import AESGCM")
    assert idx > 0, "AESGCM import line missing"
    # Look at the surrounding ~400 chars for the except clause
    block = src[max(0, idx - 200):idx + 600]
    assert "except BaseException" in block, (
        "AESGCM import must use `except BaseException` so pyo3 "
        "PanicException doesn't crash the boot path.")


def test_auth_prints_critical_to_stderr_on_import_failure():
    src = _src("auth.py")
    assert "[auth] CRITICAL: cryptography.AESGCM import failed" in src, (
        "auth.py must print a CRITICAL message to stderr at boot when "
        "the cryptography import fails — Railway logs need to show "
        "the real cause.")


def test_encrypt_secret_includes_captured_import_error():
    src = _src("auth.py")
    # The new RuntimeError message must reference the captured detail.
    assert "_AESGCM_IMPORT_ERROR" in src
    # The new error message phrasing.
    assert "did not load" in src or "Import failed at boot with" in src, (
        "encrypt_secret RuntimeError must reference the actual import "
        "failure so the user can see WHY cryptography didn't load.")


# ----------------------------------------------------------------------------
# server.py — surface crypto status in api_errors
# ----------------------------------------------------------------------------

def test_fetch_live_alpaca_state_surfaces_crypto_status():
    src = _src("server.py")
    assert "encryption: cryptography package failed to load" in src, (
        "_fetch_live_alpaca_state must add an explicit 'encryption: ...' "
        "entry to api_errors when _HAS_AESGCM is False so the dashboard "
        "banner can render the crypto-broken state.")
    assert "_HAS_AESGCM" in src, (
        "server.py must check auth._HAS_AESGCM to decide whether to "
        "surface the crypto-broken state.")


# ----------------------------------------------------------------------------
# Dashboard banner
# ----------------------------------------------------------------------------

def test_dashboard_has_dedicated_crypto_broken_banner():
    src = _src("templates/dashboard.html")
    assert "Encryption broken on this deployment" in src, (
        "Dashboard must render a dedicated banner when api_errors "
        "contains an 'encryption:' entry — distinct fix path from "
        "the account-fetch banner.")
    assert "indexOf('encryption:')" in src, (
        "Banner JS must filter api_errors for the 'encryption:' prefix.")


# ----------------------------------------------------------------------------
# Build / requirements pins
# ----------------------------------------------------------------------------

def test_requirements_pins_cryptography_below_44():
    src = _src("requirements.txt")
    assert "cryptography>=42.0.0,<44.0.0" in src, (
        "Round-61 pt.13: cryptography must be pinned <44.0.0 until "
        "44.x+ is re-validated against the live Nixpacks Python 3.12 "
        "image.")


def test_requirements_pins_cffi_explicitly():
    src = _src("requirements.txt")
    assert "cffi>=1.16.0" in src, (
        "cffi must be pinned explicitly so pip resolves the manylinux "
        "wheel even when the cryptography wheel doesn't pull it in.")


def test_nixpacks_includes_libffi():
    src = _src("nixpacks.toml")
    assert "libffi" in src, (
        "nixpacks.toml must include libffi in nixPkgs so cffi can "
        "dlopen() libffi.so.8 at runtime — without this, cryptography "
        "import fails on the slim Nixpacks Python image.")


# ----------------------------------------------------------------------------
# Behavioral: api_errors via http_harness
# ----------------------------------------------------------------------------

def test_api_data_surfaces_crypto_error_when_aesgcm_unavailable(http_harness, monkeypatch):
    """Force _HAS_AESGCM False → /api/data api_errors must contain the
    'encryption: ...' entry."""
    http_harness.create_user()
    import auth as _auth
    monkeypatch.setattr(_auth, "_HAS_AESGCM", False)
    monkeypatch.setattr(_auth, "_AESGCM_IMPORT_ERROR",
                        "ModuleNotFoundError: No module named '_cffi_backend'",
                        raising=False)
    resp = http_harness.get("/api/data")
    assert resp["status"] == 200
    api_errors = resp["body"].get("api_errors") or []
    crypto_errs = [e for e in api_errors
                   if isinstance(e, str) and e.lower().startswith("encryption:")]
    assert crypto_errs, (
        "api_errors must include an 'encryption:' entry when "
        "_HAS_AESGCM is False. Got: " + repr(api_errors))
    assert "_cffi_backend" in crypto_errs[0], (
        "Surfaced error must include the captured import-error text.")
