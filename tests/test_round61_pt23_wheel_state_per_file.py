"""Round-61 pt.23 — wheel state writes back to the file it loaded from.

Pt.22 added multi-contract wheel support by creating an indexed
sibling file `wheel_<UNDERLYING>__<CONTRACT>.json` when the default
`wheel_<UNDERLYING>.json` is already tracking a different contract.
But `save_wheel_state` derived the path from `state["symbol"]` (the
underlying), not from the file the state was loaded from. So on
every monitor tick:
  - Default file's state gets overwritten with indexed file's state
    (last-write-wins)
  - Indexed file stays frozen (monitor never writes back to it)
  - Two contracts compete for the default file — state corrupted

Fix: `list_wheel_files` stamps `_state_file` on the loaded state
dict, `save_wheel_state` uses it to write back to the same file.
"""
from __future__ import annotations

import json
import os


def _make_wheel(symbol, contract_symbol, strike):
    return {
        "symbol": symbol,
        "strategy": "wheel",
        "status": "active",
        "stage": "stage_1_put_active",
        "shares_owned": 0,
        "active_contract": {
            "contract_symbol": contract_symbol,
            "type": "put",
            "strike": strike,
            "quantity": 1,
        },
    }


def _fake_user(data_dir):
    return {
        "id": 999,
        "username": "testuser",
        "_data_dir": str(data_dir),
        "_strategies_dir": str(os.path.join(str(data_dir), "strategies")),
    }


def test_list_wheel_files_stamps_state_file_marker(tmp_path):
    import wheel_strategy as ws
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "wheel_HIMS.json").write_text(json.dumps(
        _make_wheel("HIMS", "HIMS260508P00027000", 27.0)))
    (sdir / "wheel_HIMS__260515P00026000.json").write_text(json.dumps(
        _make_wheel("HIMS", "HIMS260515P00026000", 26.0)))

    user = _fake_user(tmp_path)
    import wheel_strategy
    # Patch _user_strategies_dir since the real one reads from env/auth.
    original = wheel_strategy._user_strategies_dir
    wheel_strategy._user_strategies_dir = lambda u: str(sdir)
    try:
        files = ws.list_wheel_files(user)
    finally:
        wheel_strategy._user_strategies_dir = original

    assert len(files) == 2
    by_name = {f[0]: f[1] for f in files}
    assert "wheel_HIMS.json" in by_name
    assert "wheel_HIMS__260515P00026000.json" in by_name
    # Each state dict carries the filename it came from.
    assert by_name["wheel_HIMS.json"]["_state_file"] == "wheel_HIMS.json"
    assert by_name["wheel_HIMS__260515P00026000.json"]["_state_file"] \
        == "wheel_HIMS__260515P00026000.json"


def test_save_wheel_state_writes_back_to_indexed_file(tmp_path):
    """Loading an indexed wheel, modifying state, saving — must
    write to the indexed filename, NOT the default."""
    import wheel_strategy as ws
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "wheel_HIMS.json").write_text(json.dumps(
        _make_wheel("HIMS", "HIMS260508P00027000", 27.0)))
    indexed_fname = "wheel_HIMS__260515P00026000.json"
    (sdir / indexed_fname).write_text(json.dumps(
        _make_wheel("HIMS", "HIMS260515P00026000", 26.0)))

    user = _fake_user(tmp_path)
    original = ws._user_strategies_dir
    ws._user_strategies_dir = lambda u: str(sdir)
    try:
        files = ws.list_wheel_files(user)
        indexed_state = [s for f, s in files if f == indexed_fname][0]
        # Mutate something so we can verify the write lands in the
        # right file.
        indexed_state["cycles_completed"] = 99
        ws.save_wheel_state(user, indexed_state)
    finally:
        ws._user_strategies_dir = original

    # Default file unchanged.
    default = json.loads((sdir / "wheel_HIMS.json").read_text())
    assert default.get("cycles_completed", 0) == 0, (
        "Default wheel_HIMS.json should NOT be touched when saving "
        "indexed-file state — pt.23 preserves per-file state.")
    # Indexed file has the new value.
    indexed = json.loads((sdir / indexed_fname).read_text())
    assert indexed["cycles_completed"] == 99
    # And the `_state_file` marker is stripped from persisted JSON.
    assert "_state_file" not in indexed


def test_save_wheel_state_falls_back_to_default_when_no_marker(tmp_path):
    """A freshly-built state dict (e.g. from run_wheel_auto_deploy)
    has no `_state_file` marker — must write to the default path."""
    import wheel_strategy as ws
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    user = _fake_user(tmp_path)
    original = ws._user_strategies_dir
    ws._user_strategies_dir = lambda u: str(sdir)
    try:
        state = _make_wheel("AAPL", "AAPL260619P00180000", 180.0)
        # No _state_file marker — fresh state.
        ws.save_wheel_state(user, state)
    finally:
        ws._user_strategies_dir = original

    # Default file created.
    assert (sdir / "wheel_AAPL.json").exists()


def test_save_wheel_state_strips_state_file_marker_before_persist(tmp_path):
    """The load-time marker must not leak into the saved JSON."""
    import wheel_strategy as ws
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    user = _fake_user(tmp_path)
    original = ws._user_strategies_dir
    ws._user_strategies_dir = lambda u: str(sdir)
    try:
        state = _make_wheel("AAPL", "AAPL260619P00180000", 180.0)
        state["_state_file"] = "wheel_AAPL.json"
        ws.save_wheel_state(user, state)
    finally:
        ws._user_strategies_dir = original

    saved = json.loads((sdir / "wheel_AAPL.json").read_text())
    assert "_state_file" not in saved, (
        "The load-time _state_file marker must NOT be persisted to "
        "disk — it's a hint for save-path routing, not part of the "
        "wheel schema.")


def test_round_trip_preserves_multi_contract_isolation(tmp_path):
    """End-to-end: two wheel files for HIMS, each mutated + saved
    independently, must remain independent."""
    import wheel_strategy as ws
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "wheel_HIMS.json").write_text(json.dumps(
        _make_wheel("HIMS", "HIMS260508P00027000", 27.0)))
    (sdir / "wheel_HIMS__260515P00026000.json").write_text(json.dumps(
        _make_wheel("HIMS", "HIMS260515P00026000", 26.0)))
    user = _fake_user(tmp_path)
    original = ws._user_strategies_dir
    ws._user_strategies_dir = lambda u: str(sdir)
    try:
        # First pass: mutate + save default.
        files = ws.list_wheel_files(user)
        default_state = [s for f, s in files if f == "wheel_HIMS.json"][0]
        default_state["total_premium_collected"] = 111.0
        ws.save_wheel_state(user, default_state)

        # Second pass: mutate + save indexed.
        files = ws.list_wheel_files(user)
        indexed_state = [s for f, s in files
                          if f == "wheel_HIMS__260515P00026000.json"][0]
        indexed_state["total_premium_collected"] = 222.0
        ws.save_wheel_state(user, indexed_state)
    finally:
        ws._user_strategies_dir = original

    # Each file holds its own value.
    default = json.loads((sdir / "wheel_HIMS.json").read_text())
    indexed = json.loads(
        (sdir / "wheel_HIMS__260515P00026000.json").read_text())
    assert default["total_premium_collected"] == 111.0
    assert indexed["total_premium_collected"] == 222.0
    # Active contracts are different.
    assert default["active_contract"]["contract_symbol"] == "HIMS260508P00027000"
    assert indexed["active_contract"]["contract_symbol"] == "HIMS260515P00026000"
