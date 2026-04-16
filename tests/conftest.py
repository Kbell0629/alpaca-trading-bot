"""
pytest fixtures shared across the test suite.

Every test gets a fresh, isolated DATA_DIR so SQLite state (users.db,
per-user files) can't bleed between tests or pollute the real Railway
volume / local working directory.
"""
import os
import sys
import tempfile
import shutil
import pytest

# Make repo root importable regardless of where pytest is invoked from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


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
