"""
Round-36: pin the per-user LEARNED_WEIGHTS_PATH wiring so the
screener actually APPLIES the weekly-learning output.

Before round-36:
  * learn.py wrote weights to LEARNED_WEIGHTS_PATH (per-user, e.g.
    /data/users/1/learned_weights.json) — correct.
  * update_dashboard.py read from DATA_DIR/learned_weights.json (the
    SHARED path) — so the per-user weights were never picked up.
  * Weekly learning was effectively a no-op for the screener.

Fix (verified in this test):
  * update_dashboard.py now reads from LEARNED_WEIGHTS_PATH env var
    with the same default as learn.py.
  * cloud_scheduler.run_screener_for_user sets that env var to the
    per-user path when spawning the screener subprocess.
"""
from __future__ import annotations


def test_update_dashboard_uses_env_var_for_learned_weights():
    """Grep-level verification — the read path in update_dashboard.py
    should use os.environ.get('LEARNED_WEIGHTS_PATH', ...)."""
    with open("update_dashboard.py") as f:
        content = f.read()
    # The fix introduces a '_learned_path' variable that reads from env.
    assert 'os.environ.get(' in content and 'LEARNED_WEIGHTS_PATH' in content, \
        "update_dashboard.py must honor LEARNED_WEIGHTS_PATH env var"
    # And the old hardcoded shared-path read is gone.
    assert 'load_json(os.path.join(DATA_DIR, "learned_weights.json"))' not in content, \
        "update_dashboard.py should NOT read directly from DATA_DIR/learned_weights.json"


def test_cloud_scheduler_passes_learned_weights_path_to_screener():
    """cloud_scheduler.run_screener_for_user must set
    env['LEARNED_WEIGHTS_PATH'] so the subprocess reads the per-user
    file instead of falling back to the shared default."""
    with open("cloud_scheduler.py") as f:
        content = f.read()
    assert 'env["LEARNED_WEIGHTS_PATH"]' in content, \
        "cloud_scheduler must set LEARNED_WEIGHTS_PATH for the screener"
    # And it should point at the per-user file, not the shared.
    # We look for the substring 'user_file(user, "learned_weights.json")'
    # appearing in the env block (both run_screener_for_user and
    # run_weekly_learning should use this).
    assert 'user_file(user, "learned_weights.json")' in content, \
        "cloud_scheduler must use user_file(...) for the per-user path"
