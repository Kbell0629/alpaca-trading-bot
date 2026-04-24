"""Round-61 pt.14 — start.sh must use the venv's python, not the
system python3.

User-reported via pt.13's red banner: dashboard showed
`ModuleNotFoundError: No module named 'cryptography'` even though the
package was pinned in requirements.txt. Root cause: start.sh ran
`python3 -u server.py`, which on the Nixpacks runtime image resolves
to Nix's SYSTEM python — NOT the venv at /opt/venv where pip installed
all the requirements. Other deps (yfinance, sentry-sdk) only "worked"
because they import lazily and silently failed; cryptography failed
loudly because auth.py imports AESGCM at module load.

This file pins:
  1. start.sh prefers /opt/venv/bin/python over system python3.
  2. start.sh emits a verification line that imports
     cryptography.AESGCM at boot, so the build/deploy log shows the
     actual import error before the server even starts.
  3. start.sh exits via `exec "$PY" -u server.py` (not `exec python3`)
     so the running process is the venv interpreter.
"""
from __future__ import annotations


def _src(path):
    with open(path) as f:
        return f.read()


def test_start_sh_prefers_venv_python():
    src = _src("start.sh")
    assert "/opt/venv/bin/python" in src, (
        "start.sh must prefer /opt/venv/bin/python so the runtime "
        "uses the venv interpreter where pip installed the deps.")


def test_start_sh_does_not_unconditionally_run_system_python3():
    """The pre-pt.14 line `exec python3 -u server.py` was the bug.
    Allow `python3` to appear only as a fallback — never as the
    default exec target."""
    src = _src("start.sh")
    # The exec line must be the VARIABLE form, not bare python3.
    assert 'exec "$PY" -u server.py' in src, (
        "start.sh must exec via the $PY variable, not bare python3.")
    # Bare `exec python3` (with no `$PY`) must not be the active
    # command. (We do allow `PY=\"python3\"` as a fallback assignment.)
    bad_lines = [ln for ln in src.splitlines()
                 if ln.strip().startswith("exec python3 ")]
    assert not bad_lines, (
        "start.sh must not exec bare python3 directly: " + repr(bad_lines))


def test_start_sh_imports_cryptography_at_boot_for_visibility():
    """Boot-time AESGCM import smoke-test makes cryptography failures
    visible in the build log, not just at first encrypt attempt."""
    src = _src("start.sh")
    assert "from cryptography.hazmat.primitives.ciphers.aead import AESGCM" in src, (
        "start.sh must do a boot-time AESGCM import smoke check so "
        "the build log shows the real import error before the server "
        "starts handling requests.")


def test_start_sh_falls_back_to_system_python_with_warning():
    """If the venv was somehow lost (Nixpacks regression), start.sh
    must warn loudly so the operator sees it in the boot log."""
    src = _src("start.sh")
    assert "WARNING" in src and "/opt/venv" in src, (
        "start.sh must emit a WARNING when /opt/venv/bin/python is "
        "missing so the operator sees it in the boot log.")
