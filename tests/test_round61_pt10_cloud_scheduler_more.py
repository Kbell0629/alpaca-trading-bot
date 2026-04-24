"""
Round-61 pt.10 — more cloud_scheduler.py helper coverage.

Extends pt.9 to the file I/O + heartbeat + dual-mode dispatch helpers.
"""
from __future__ import annotations

import json as _json
import os
import sys
import time


def _import(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for name in ("cloud_scheduler",):
        sys.modules.pop(name, None)
    import cloud_scheduler
    return cloud_scheduler


# ============================================================================
# load_json / save_json round-trip
# ============================================================================

class TestLoadJson:
    def test_missing_file_returns_none(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        out = cs.load_json(str(tmp_path / "does-not-exist.json"))
        assert out is None

    def test_valid_json_returns_dict(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        p = tmp_path / "valid.json"
        p.write_text(_json.dumps({"foo": 1, "bar": [1, 2, 3]}))
        out = cs.load_json(str(p))
        assert out == {"foo": 1, "bar": [1, 2, 3]}

    def test_malformed_json_returns_none(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        p = tmp_path / "bad.json"
        p.write_text("{this is not valid json")
        out = cs.load_json(str(p))
        assert out is None

    def test_empty_file_returns_none(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        p = tmp_path / "empty.json"
        p.write_text("")
        out = cs.load_json(str(p))
        assert out is None


class TestSaveJson:
    def test_atomic_write_creates_file(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        p = tmp_path / "out.json"
        cs.save_json(str(p), {"a": 1})
        assert p.exists()
        assert _json.loads(p.read_text()) == {"a": 1}

    def test_creates_parent_directory(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        p = tmp_path / "nested" / "dir" / "file.json"
        cs.save_json(str(p), {"nested": True})
        assert p.exists()

    def test_round_trip_preserves_data(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        p = tmp_path / "rt.json"
        data = {
            "strings": ["a", "b"],
            "numbers": [1, 2.5, 3],
            "nested": {"k": [{"inner": True}]},
            "null_field": None,
        }
        cs.save_json(str(p), data)
        out = cs.load_json(str(p))
        assert out == data

    def test_default_str_handles_non_json_types(self, monkeypatch, tmp_path):
        """save_json uses default=str so non-JSON types (e.g. datetime) fall
        back to their string representation instead of crashing."""
        cs = _import(monkeypatch)
        import datetime as _dt
        p = tmp_path / "dt.json"
        now = _dt.datetime(2026, 4, 24, 12, 0, 0)
        cs.save_json(str(p), {"when": now})
        raw = p.read_text()
        assert "2026-04-24 12:00:00" in raw or "2026-04-24T12:00:00" in raw


# ============================================================================
# user_file / user_strategies_dir
# ============================================================================

class TestUserFile:
    def test_returns_per_user_path(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        user = {"id": 2, "_data_dir": str(tmp_path / "u2")}
        os.makedirs(user["_data_dir"])
        p = cs.user_file(user, "trade_journal.json")
        assert p == os.path.join(user["_data_dir"], "trade_journal.json")

    def test_non_admin_does_not_migrate_shared(self, monkeypatch, tmp_path):
        """CRITICAL: only user_id=1 inherits the shared DATA_DIR file.
        Other users must start with a clean data dir (cross-user-privacy)."""
        cs = _import(monkeypatch)
        user_dir = tmp_path / "u2"
        user_dir.mkdir()
        # Plant a shared file that user_id=2 should NOT inherit
        shared = tmp_path / "shared.json"
        shared.write_text('{"shared": true}')
        monkeypatch.setattr(cs, "DATA_DIR", str(tmp_path))
        user = {"id": 2, "_data_dir": str(user_dir)}
        # Request same-named file via user_file
        p = cs.user_file(user, "shared.json")
        # Path returned but file does NOT exist (no migration for user 2)
        assert p == os.path.join(str(user_dir), "shared.json")
        assert not os.path.exists(p)


class TestUserStrategiesDir:
    def test_creates_dir_when_missing(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        d = tmp_path / "u3" / "strategies"
        user = {"id": 3, "_strategies_dir": str(d)}
        out = cs.user_strategies_dir(user)
        assert out == str(d)
        assert os.path.isdir(out)

    def test_non_admin_does_not_seed_strategies(self, monkeypatch, tmp_path):
        """user_id != 1 gets an empty strategies dir (no cross-user seed)."""
        cs = _import(monkeypatch)
        d = tmp_path / "u4" / "strategies"
        user = {"id": 4, "_strategies_dir": str(d)}
        # Point STRATEGIES_DIR at a populated shared dir
        shared = tmp_path / "shared_strategies"
        shared.mkdir()
        (shared / "trailing_stop_AAPL.json").write_text('{"s":"x"}')
        monkeypatch.setattr(cs, "STRATEGIES_DIR", str(shared))
        cs.user_strategies_dir(user)
        # User 4's dir should be empty (not seeded from shared)
        contents = os.listdir(str(d))
        assert contents == []


# ============================================================================
# _save_last_runs + _load_last_runs round-trip
# ============================================================================

class TestLastRunsPersistence:
    def test_save_then_load_round_trips(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        last_runs_path = tmp_path / "scheduler_last_runs.json"
        monkeypatch.setattr(cs, "_LAST_RUNS_PATH", str(last_runs_path))
        with cs._last_runs_lock:
            cs._last_runs.clear()
            cs._last_runs["auto_deployer_1"] = "2026-04-24"
            cs._last_runs["monitor_1"] = time.time()
        cs._save_last_runs()
        # Corrupt the in-memory state, reload from disk
        with cs._last_runs_lock:
            cs._last_runs.clear()
        assert last_runs_path.exists()
        cs._load_last_runs()
        with cs._last_runs_lock:
            assert "auto_deployer_1" in cs._last_runs
            assert "monitor_1" in cs._last_runs

    def test_save_handles_missing_directory(self, monkeypatch, tmp_path):
        cs = _import(monkeypatch)
        # Point at a path whose parent doesn't exist yet
        new_path = tmp_path / "nested" / "runs.json"
        monkeypatch.setattr(cs, "_LAST_RUNS_PATH", str(new_path))
        with cs._last_runs_lock:
            cs._last_runs["x"] = 1
        cs._save_last_runs()
        assert new_path.exists()


# ============================================================================
# _heartbeat_tick
# ============================================================================

class TestHeartbeatTick:
    def test_first_call_records_ts(self, monkeypatch):
        cs = _import(monkeypatch)
        cs._last_heartbeat_ts = 0.0
        # Silence the save side-effect
        monkeypatch.setattr(cs, "_save_recent_logs", lambda: None)
        monkeypatch.setattr(cs, "log", lambda *a, **kw: None)
        cs._heartbeat_tick()
        assert cs._last_heartbeat_ts > 0

    def test_suppresses_rapid_calls_within_120s(self, monkeypatch):
        cs = _import(monkeypatch)
        calls = []
        monkeypatch.setattr(cs, "log", lambda msg, task="scheduler": calls.append(msg))
        monkeypatch.setattr(cs, "_save_recent_logs", lambda: None)
        cs._last_heartbeat_ts = time.time() - 30  # 30s ago
        cs._heartbeat_tick()
        assert calls == []  # within 2min window → no log

    def test_after_120s_fires_again(self, monkeypatch):
        cs = _import(monkeypatch)
        calls = []
        monkeypatch.setattr(cs, "log", lambda msg, task="scheduler": calls.append(msg))
        monkeypatch.setattr(cs, "_save_recent_logs", lambda: None)
        cs._last_heartbeat_ts = time.time() - 121  # 121s ago → past 120s window
        cs._heartbeat_tick()
        assert calls == ["heartbeat"]


# ============================================================================
# get_all_users_for_scheduling — dual-mode expansion
# ============================================================================

class TestGetAllUsersForScheduling:
    def test_env_fallback_when_auth_unavailable(self, monkeypatch):
        cs = _import(monkeypatch)
        monkeypatch.setattr(cs, "AUTH_AVAILABLE", False)
        # Must return something non-empty via env-var path.
        users = cs.get_all_users_for_scheduling()
        # Single-user env fallback: may be empty if env has no keys, but
        # the function should return a list.
        assert isinstance(users, list)

    def test_single_user_paper_only(self, monkeypatch):
        cs = _import(monkeypatch)
        monkeypatch.setattr(cs, "AUTH_AVAILABLE", True)
        monkeypatch.setattr(cs.auth, "list_active_users",
                             lambda: [{"id": 1, "username": "alice"}])
        monkeypatch.setattr(cs, "_build_user_dict_for_mode",
                             lambda u, mode: {"id": u["id"], "_mode": mode}
                             if mode == "paper" else None)
        out = cs.get_all_users_for_scheduling()
        assert len(out) == 1
        assert out[0]["_mode"] == "paper"

    def test_live_parallel_enables_second_entry(self, monkeypatch):
        cs = _import(monkeypatch)
        monkeypatch.setattr(cs, "AUTH_AVAILABLE", True)
        monkeypatch.setattr(cs.auth, "list_active_users",
                             lambda: [{"id": 2, "username": "bob",
                                       "live_parallel_enabled": 1}])
        monkeypatch.setattr(cs, "_build_user_dict_for_mode",
                             lambda u, mode: {"id": u["id"], "_mode": mode})
        out = cs.get_all_users_for_scheduling()
        modes = sorted(x["_mode"] for x in out)
        assert modes == ["live", "paper"]

    def test_live_keys_without_opt_in_stay_paper_only(self, monkeypatch):
        cs = _import(monkeypatch)
        monkeypatch.setattr(cs, "AUTH_AVAILABLE", True)
        # live_parallel_enabled is falsy → no live entry even if live
        # keys exist in the user record.
        monkeypatch.setattr(cs.auth, "list_active_users",
                             lambda: [{"id": 3, "username": "carol",
                                       "live_parallel_enabled": 0}])
        monkeypatch.setattr(cs, "_build_user_dict_for_mode",
                             lambda u, mode: {"id": u["id"], "_mode": mode})
        out = cs.get_all_users_for_scheduling()
        assert [x["_mode"] for x in out] == ["paper"]


# ============================================================================
# _track_child — Popen reaping
# ============================================================================

class TestTrackChild:
    def test_adds_child_to_tracking_list(self, monkeypatch):
        cs = _import(monkeypatch)
        # _child_procs is a module-level list
        if hasattr(cs, "_child_procs"):
            before = len(cs._child_procs)

            class FakePopen:
                pid = 99999
            cs._track_child(FakePopen())
            assert len(cs._child_procs) == before + 1
            # Clean up
            cs._child_procs.pop()
