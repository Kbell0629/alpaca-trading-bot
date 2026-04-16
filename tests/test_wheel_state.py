"""Wheel strategy state primitives — file lock, JSON atomic write, state template."""
import os
import json


def test_wheel_state_template_shape(isolated_data_dir):
    import wheel_strategy as ws
    t = ws.WHEEL_STATE_TEMPLATE
    assert t["stage"] == "stage_1_searching"
    assert t["cycles_completed"] == 0
    assert t["shares_owned"] == 0
    assert t["active_contract"] is None


def test_history_capped(isolated_data_dir):
    import wheel_strategy as ws
    state = dict(ws.WHEEL_STATE_TEMPLATE)
    state["symbol"] = "TESTSYM"
    state["history"] = []
    # Spam events past HISTORY_MAX
    for i in range(ws.HISTORY_MAX + 50):
        ws.log_history(state, event="ping", detail={"i": i})
    assert len(state["history"]) == ws.HISTORY_MAX, \
        f"history must cap at {ws.HISTORY_MAX}, got {len(state['history'])}"


def test_json_atomic_write(isolated_data_dir):
    import wheel_strategy as ws
    path = os.path.join(isolated_data_dir, "atomic-test.json")
    ws._save_json(path, {"a": 1, "b": [1, 2, 3]})
    # Confirm no stray .tmp file
    dir_entries = os.listdir(isolated_data_dir)
    assert not any(e.endswith(".tmp") for e in dir_entries)
    assert json.load(open(path)) == {"a": 1, "b": [1, 2, 3]}


def test_wheel_lock_acquire_release(isolated_data_dir):
    import wheel_strategy as ws
    path = os.path.join(isolated_data_dir, "lockfile-test")
    with ws._WheelLock(path):
        pass  # If flock fails, the __enter__ sets self.fh=None and this
              # exits cleanly. Should never raise.
    # fh cleared on exit
