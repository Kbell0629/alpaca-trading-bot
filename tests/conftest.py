"""
pytest fixtures shared across the test suite.

Every test gets a fresh, isolated DATA_DIR so SQLite state (users.db,
per-user files) can't bleed between tests or pollute the real Railway
volume / local working directory.
"""
import io
import json
import os
import sys
import tempfile
import shutil
import pytest

# Make repo root importable regardless of where pytest is invoked from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ============================================================================
# Round-61 pt.51: alpaca_mock — patch every Alpaca HTTP helper used by
# cloud_scheduler so tests can run scheduler functions against
# deterministic responses without touching the network.
#
# Usage:
#     def test_deploy_skips_existing(alpaca_mock):
#         alpaca_mock.register("GET", "/positions", [
#             {"symbol": "AAPL", "qty": "10", "market_value": "1500"}
#         ])
#         alpaca_mock.register("GET", "/account",
#                              {"portfolio_value": "100000",
#                               "equity": "100000", "multiplier": "1"})
#         # ... call cloud_scheduler.run_auto_deployer(user) ...
#         alpaca_mock.assert_called("GET", "/positions")
# ============================================================================


class AlpacaMock:
    """Records all Alpaca calls + serves pre-registered responses.

    Match by HTTP method + URL substring. Last-registered wins.
    Default response when no match is found is an empty dict (so
    cloud_scheduler treats it as a no-op rather than crashing).
    """

    def __init__(self):
        self._handlers = []   # list of (method, substr, response)
        self.calls = []        # list of (method, url, body)

    def register(self, method, path_substring, response):
        self._handlers.append(
            (method.upper(), path_substring, response))

    def _match(self, method, url):
        m = method.upper()
        # Iterate in reverse so the last registered wins.
        for hm, substr, resp in reversed(self._handlers):
            if hm == m and substr in url:
                return resp
        return {}

    def _do_get(self, user, path, **_kw):
        url = path if path.startswith("http") else path
        self.calls.append(("GET", url, None))
        return self._match("GET", url)

    def _do_post(self, user, path, body=None, **_kw):
        self.calls.append(("POST", path, body))
        return self._match("POST", path)

    def _do_delete(self, user, path, **_kw):
        self.calls.append(("DELETE", path, None))
        return self._match("DELETE", path)

    def _do_patch(self, user, path, body=None, **_kw):
        self.calls.append(("PATCH", path, body))
        return self._match("PATCH", path)

    def assert_called(self, method, path_substring):
        m = method.upper()
        for c_method, c_url, _body in self.calls:
            if c_method == m and path_substring in c_url:
                return True
        raise AssertionError(
            f"expected {m} containing {path_substring!r}; got "
            f"{[(c[0], c[1]) for c in self.calls]}")


@pytest.fixture
def alpaca_mock(monkeypatch):
    """Patch every cloud_scheduler / scheduler_api Alpaca helper to use
    AlpacaMock for deterministic testing without network. Returns the
    mock so the test can register responses + assert calls.

    Pt.51: does NOT pop sys.modules — that would force-reimport
    scheduler_api/cloud_scheduler and corrupt coverage tracking
    + break other tests that share module state. monkeypatch.setattr
    handles cleanup automatically when the test finishes.
    """
    mock = AlpacaMock()
    import scheduler_api as _sa
    monkeypatch.setattr(_sa, "user_api_get", mock._do_get)
    monkeypatch.setattr(_sa, "user_api_post", mock._do_post)
    monkeypatch.setattr(_sa, "user_api_delete", mock._do_delete)
    monkeypatch.setattr(_sa, "user_api_patch", mock._do_patch)
    # cloud_scheduler imports those names at module load — patch
    # them on the module's namespace too so existing call sites pick
    # up the mock.
    import cloud_scheduler as _cs
    monkeypatch.setattr(_cs, "user_api_get", mock._do_get)
    monkeypatch.setattr(_cs, "user_api_post", mock._do_post)
    monkeypatch.setattr(_cs, "user_api_delete", mock._do_delete)
    monkeypatch.setattr(_cs, "user_api_patch", mock._do_patch)
    yield mock


@pytest.fixture
def isolated_data_dir(monkeypatch):
    """A clean DATA_DIR for each test. Reimports auth + companions so they
    bind the new DB path and initialize a fresh schema. Yields the temp
    directory; auto-cleans afterward."""
    d = tempfile.mkdtemp(prefix="alpaca-test-")
    monkeypatch.setenv("DATA_DIR", d)
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "a" * 64)
    # Clear any cached modules so they rebind DATA_DIR/DB_PATH on next import
    for mod in ("auth", "et_time", "constants", "cloud_scheduler",
                "wheel_strategy", "update_dashboard", "update_scorecard",
                "extended_hours"):
        sys.modules.pop(mod, None)
    # Initialize the auth schema in the fresh DB so tests can use sessions,
    # login_attempts, admin_audit_log, etc. without boilerplate.
    import auth
    auth.init_db()
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ============================================================================
# Round-61 pt.6: Mock WSGI harness for DashboardHandler.
#
# server.py was 7% covered (1708 statements, 1555 uncovered) because its
# HTTP handlers require a real socket to exercise. This fixture lets us
# invoke do_GET / do_POST / do_DELETE directly, bypassing sockets, so
# we can:
#   * Inject a session cookie (auth-as-user)
#   * Inject a JSON/form body
#   * Capture the response status + headers + body as plain dicts
#   * Assert on side effects (DB state, guardrails.json, strategy files)
#
# Pattern: instantiate DashboardHandler WITHOUT running its parent's
# __init__ (which expects a socket), populate the attributes it reads
# during dispatch, call do_GET etc., parse the wfile BytesIO.
# ============================================================================


class _FakeHandler:
    """Placeholder — the real class is built inside the fixture so it can
    subclass the live DashboardHandler (which isn't importable at module
    load time because its parent BaseHTTPRequestHandler probes a socket
    if instantiated naively). See http_harness below."""


@pytest.fixture
def http_harness(isolated_data_dir, monkeypatch):
    """Return an HTTPHarness whose get/post/delete methods invoke
    DashboardHandler's dispatch directly.

    Usage:
        def test_api_version(http_harness):
            uid = http_harness.create_user()
            resp = http_harness.get("/api/version")
            assert resp["status"] == 200
            assert "commit" in resp["body"]
    """
    # Make sure every server module reloads against the isolated_data_dir
    # env. server.py imports cloud_scheduler + auth + lots else at module
    # load, so we pop them before re-importing server.
    for mod in ("auth", "scheduler_api", "cloud_scheduler",
                "wheel_strategy", "update_dashboard", "update_scorecard",
                "extended_hours", "et_time", "constants",
                "handlers", "handlers.auth_mixin", "handlers.admin_mixin",
                "handlers.strategy_mixin", "handlers.actions_mixin",
                "server"):
        sys.modules.pop(mod, None)
    import server as _server  # noqa: F401 — trigger clean import

    class FakeDashboardHandler(_server.DashboardHandler):
        """DashboardHandler that skips the socket setup. All attributes
        the dispatch logic reads are populated by HTTPHarness below
        before do_GET/do_POST/do_DELETE is called."""

        # Skip BaseHTTPRequestHandler's __init__ — it expects a real
        # request/client_address/server tuple. We'll set every attribute
        # it reads by hand.
        def __init__(self):  # noqa: B010 — intentional: skip super
            self.rfile = io.BytesIO()
            self.wfile = io.BytesIO()
            self.client_address = ("127.0.0.1", 0)
            self.request_version = "HTTP/1.1"
            self.server_version = "HarnessFake/0.1"
            self.sys_version = ""
            self.headers = {}
            self.path = "/"
            self.command = "GET"
            self._response_status = None
            self._response_message = None
            self._response_headers = []
            self._ended_headers = False

        # ---- Override socket-dependent methods ----

        def send_response(self, code, message=None):
            """BaseHTTPRequestHandler's default writes to wfile. We just
            record the status + message so tests can assert on them."""
            self._response_status = code
            self._response_message = message
            # Emulate the default headers that BaseHTTPRequestHandler
            # adds so callers can verify them (Date, Server).
            self._response_headers.append(("Server", self.server_version))

        def send_response_only(self, code, message=None):
            # Some callers use send_response_only (no automatic headers).
            self._response_status = code
            self._response_message = message

        def send_header(self, key, value):
            self._response_headers.append((key, value))

        def end_headers(self):
            self._ended_headers = True

        def log_message(self, format, *args):
            # Silence access log noise in tests.
            pass

        def log_request(self, code="-", size="-"):
            pass

    class HTTPHarness:
        """Test client for DashboardHandler. One instance per test."""

        def __init__(self, data_dir, server_module):
            self.data_dir = data_dir
            self.server = server_module
            self.session_token = None
            self.csrf_token = None
            self.user_id = None
            self.username = None

        # ---- Auth helpers ----

        def create_user(self, username="alice", email="alice@x.com",
                         password="correct horse battery staple!!",
                         alpaca_key="k", alpaca_secret="s"):
            """Create an auth user + start a session. Stores the cookie
            so subsequent get/post calls auto-authenticate. Also
            generates a CSRF token so POST requests pass the double-
            submit CSRF check (server.py's _csrf_ok requires the
            `csrf=` cookie to match the X-CSRF-Token header)."""
            import auth
            import secrets as _secrets
            uid, err = auth.create_user(
                email=email, username=username, password=password,
                alpaca_key=alpaca_key, alpaca_secret=alpaca_secret,
            )
            assert uid is not None, f"create_user failed: {err}"
            self.user_id = uid
            self.username = username
            # Start a session the same way auth.authenticate →
            # create_session produces the session token.
            authed = auth.authenticate(username, password)
            assert authed is not None, "authenticate returned None after create_user"
            self.session_token = auth.create_session(uid)
            # Generate a CSRF token — the server's double-submit check
            # only verifies the cookie == header. It doesn't bind the
            # token to the session, so any matched value works.
            self.csrf_token = _secrets.token_urlsafe(32)
            return uid

        def logout(self):
            """Drop the cookie so subsequent requests are anonymous."""
            self.session_token = None
            self.csrf_token = None

        # ---- Request methods ----

        def _build_handler(self, method, path, body=None, cookies=None,
                            headers=None, auth_session=True):
            h = FakeDashboardHandler()
            h.path = path
            h.command = method
            h.headers = dict(headers or {})
            # Cookie header: explicit override wins; otherwise the
            # harness's stored session_token + csrf_token if
            # auth_session=True.
            cookie_str = None
            if cookies is not None:
                cookie_str = cookies
            elif auth_session and self.session_token:
                cookie_parts = [f"session={self.session_token}"]
                if self.csrf_token:
                    cookie_parts.append(f"csrf={self.csrf_token}")
                cookie_str = "; ".join(cookie_parts)
            if cookie_str:
                h.headers["Cookie"] = cookie_str
            # For state-changing requests (POST/DELETE/PATCH) the server
            # enforces a double-submit CSRF check: the csrf cookie MUST
            # equal the X-CSRF-Token header. Inject it automatically for
            # authed POSTs unless the caller already set the header.
            if method in ("POST", "DELETE", "PATCH") and auth_session \
                    and self.csrf_token and "X-CSRF-Token" not in h.headers:
                h.headers["X-CSRF-Token"] = self.csrf_token
            # Default content-type for JSON bodies.
            if isinstance(body, dict):
                body = json.dumps(body).encode("utf-8")
                h.headers.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            if body:
                h.headers["Content-Length"] = str(len(body))
                h.rfile = io.BytesIO(body)
            return h

        def _response(self, handler):
            raw = handler.wfile.getvalue()
            body = None
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    body = raw.decode("utf-8", errors="replace")
            headers_dict = {}
            # Headers may have duplicates (Set-Cookie); join with newline
            # if so. Simpler: keep the list around too.
            for k, v in handler._response_headers:
                if k in headers_dict:
                    headers_dict[k] = headers_dict[k] + "\n" + str(v)
                else:
                    headers_dict[k] = v
            return {
                "status": handler._response_status,
                "headers": headers_dict,
                "headers_list": list(handler._response_headers),
                "body": body,
                "raw": raw,
            }

        def get(self, path, cookies=None, headers=None, auth_session=True):
            h = self._build_handler("GET", path, cookies=cookies,
                                     headers=headers, auth_session=auth_session)
            h.do_GET()
            return self._response(h)

        def post(self, path, body=None, cookies=None, headers=None,
                   auth_session=True):
            h = self._build_handler("POST", path, body=body, cookies=cookies,
                                     headers=headers, auth_session=auth_session)
            h.do_POST()
            return self._response(h)

        def delete(self, path, cookies=None, headers=None, auth_session=True):
            h = self._build_handler("DELETE", path, cookies=cookies,
                                     headers=headers, auth_session=auth_session)
            # DashboardHandler routes all DELETE through do_POST's
            # "method override" style OR has a do_DELETE depending on
            # server.py shape. Try do_DELETE if it exists, else do_POST.
            if hasattr(h, "do_DELETE"):
                h.do_DELETE()
            else:
                h.do_POST()
            return self._response(h)

    yield HTTPHarness(isolated_data_dir, _server)
