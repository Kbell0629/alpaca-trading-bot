"""
Subprocess-based boot smoke test.

Round 7 shipped a mixin-import regression that passed every existing
test (including the AST-based undefined-names check) but crashed at
server startup on Railway because `python3 server.py` makes server.py
__main__, not 'server', and `import server` inside the mixins
triggered a circular re-execution that the `import server` call
path tests never exercised.

This test catches that class of bug by doing exactly what Railway does:
launch `python3 server.py` as a subprocess, wait for it to come up,
hit /healthz, assert the response is healthy. If the process crashes
during boot (for any reason — import errors, missing env vars, bad
init code), the test fails.

It's slower than the unit tests (~3s for server boot + one request),
so it lives in its own file and can be skipped with `-k 'not boot'` if
someone wants a fast inner-loop run. Still under 5s total.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_BOOT_MAX_SECONDS = 15  # Railway healthcheckTimeout is 10, give ourselves slack


def _find_free_port():
    """Bind to port 0 and return what the kernel picked. There's a tiny race
    between close() and the subprocess rebinding, but in practice it's fine
    for a single-threaded smoke test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_healthz(port, timeout):
    """Poll /healthz until it returns 200 or timeout elapses. Returns the
    parsed JSON body on success; raises TimeoutError with diagnostic
    detail on failure."""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=2
            ) as resp:
                body = resp.read().decode()
                if resp.status == 200:
                    return json.loads(body)
                last_err = f"HTTP {resp.status}: {body}"
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_err = repr(e)
        time.sleep(0.25)
    raise TimeoutError(
        f"Server did not answer /healthz with 200 within {timeout}s. "
        f"Last error: {last_err}"
    )


@pytest.fixture
def server_subprocess():
    """Spawn `python3 server.py` in a subprocess with an isolated
    DATA_DIR and a free port. Teardown kills the process and captures
    stdout/stderr for diagnostics if the test fails."""
    data_dir = tempfile.mkdtemp(prefix="alpaca-boot-test-")
    port = _find_free_port()
    env = os.environ.copy()
    env["DATA_DIR"] = data_dir
    env["MASTER_ENCRYPTION_KEY"] = "a" * 64
    env["PORT"] = str(port)
    # Don't let Railway-specific env vars from the dev's shell poison the test.
    # (REQUIRE_MASTER_KEY was removed from auth.py in the cryptography-mandatory
    # PR; nothing reads it any more.)
    env.pop("ENABLE_CLOUD_SCHEDULER", None)

    # Suppress the email queue from actually firing
    env["NTFY_TOPIC"] = "test-boot-suppress"

    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield proc, port
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        shutil.rmtree(data_dir, ignore_errors=True)


def test_server_boots_and_healthz_returns_200(server_subprocess):
    """THE regression test for round-7. If any future refactor breaks
    the import chain, this fires before the commit lands on Railway."""
    proc, port = server_subprocess
    try:
        body = _wait_for_healthz(port, SERVER_BOOT_MAX_SECONDS)
    except TimeoutError:
        # If the server died, dump its output so the failure message
        # contains the actual traceback instead of just "timeout".
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            pytest.fail(
                f"Server process exited with code {proc.returncode} "
                f"before answering /healthz. Output:\n{output[-4000:]}"
            )
        raise

    assert body.get("status") == "ok", f"unexpected healthz body: {body}"
    assert body.get("scheduler_alive") is True, \
        f"scheduler not alive: {body}"


def test_server_boots_and_public_pages_load(server_subprocess):
    """/login, /signup, /forgot should all return 200 without auth.
    This exercises template loading + DashboardHandler.do_GET routing
    through the full mixin chain (auth_mixin owns the POST handlers
    for these pages but the GET is in server.py itself)."""
    proc, port = server_subprocess
    _wait_for_healthz(port, SERVER_BOOT_MAX_SECONDS)  # block until ready

    for path in ("/login", "/signup", "/forgot", "/api/version"):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=3
            ) as resp:
                assert resp.status == 200, f"{path} returned {resp.status}"
                body = resp.read().decode()
                assert body, f"{path} returned empty body"
        except urllib.error.HTTPError as e:
            pytest.fail(f"{path} raised HTTPError: {e}")


def test_api_version_returns_commit_info(server_subprocess):
    """Sanity check the /api/version endpoint we added in round 6 still
    returns the fields the UI and ops folks look for."""
    proc, port = server_subprocess
    _wait_for_healthz(port, SERVER_BOOT_MAX_SECONDS)

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/version", timeout=3
    ) as resp:
        info = json.loads(resp.read().decode())

    assert "bot_version" in info
    assert "python" in info
    assert info["python"].startswith("3."), f"unexpected python: {info}"
    # commit may be missing if .git isn't accessible in the subprocess's
    # cwd context, so don't fail on that — just print it for logs.
    if "commit" in info:
        assert len(info["commit"]) >= 7
