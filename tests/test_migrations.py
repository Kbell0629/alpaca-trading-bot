"""
Round-20 guardrails migration tests.

The repo-level preset in dashboard.html moved from 10% to 7% positions,
but existing per-user guardrails.json files on Railway aren't rewritten
by a git push. migrations.migrate_guardrails_round20 bridges that gap:
on every scheduler boot it bumps users still on the 0.10 default to
0.07, stamps `_migrations_applied` so it's idempotent, and respects
operators who deliberately customised their cap to a non-0.10 value.
"""
from __future__ import annotations

import json
import os


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _read(path):
    with open(path) as f:
        return json.load(f)


def test_migration_bumps_default_0_10_to_0_07(tmp_path):
    from migrations import migrate_guardrails_round20, MIGRATION_ROUND20_POSITION_CAP
    p = str(tmp_path / "guardrails.json")
    _write(p, {"max_position_pct": 0.10, "mode": "paper"})
    result = migrate_guardrails_round20(p)
    assert result == "migrated"
    g = _read(p)
    assert g["max_position_pct"] == 0.07
    assert MIGRATION_ROUND20_POSITION_CAP in g["_migrations_applied"]


def test_migration_respects_user_customised_cap(tmp_path):
    """Operator explicitly set 0.15 → leave it alone, just mark stamped
    so we never try again."""
    from migrations import migrate_guardrails_round20, MIGRATION_ROUND20_POSITION_CAP
    p = str(tmp_path / "guardrails.json")
    _write(p, {"max_position_pct": 0.15, "mode": "paper"})
    result = migrate_guardrails_round20(p)
    assert result == "user_customised"
    g = _read(p)
    assert g["max_position_pct"] == 0.15  # NOT changed
    assert MIGRATION_ROUND20_POSITION_CAP in g["_migrations_applied"]


def test_migration_is_idempotent(tmp_path):
    """Running twice is a no-op the second time."""
    from migrations import migrate_guardrails_round20
    p = str(tmp_path / "guardrails.json")
    _write(p, {"max_position_pct": 0.10})
    assert migrate_guardrails_round20(p) == "migrated"
    # Re-run: should detect stamp and skip
    assert migrate_guardrails_round20(p) == "already_applied"
    g = _read(p)
    assert g["max_position_pct"] == 0.07  # unchanged from first run


def test_migration_handles_missing_cap(tmp_path):
    """max_position_pct unset / None → treated same as 0.10 default and
    bumped to 0.07."""
    from migrations import migrate_guardrails_round20
    p = str(tmp_path / "guardrails.json")
    _write(p, {"mode": "paper"})  # no max_position_pct key
    assert migrate_guardrails_round20(p) == "migrated"
    g = _read(p)
    assert g["max_position_pct"] == 0.07


def test_migration_handles_missing_file(tmp_path):
    from migrations import migrate_guardrails_round20
    p = str(tmp_path / "nonexistent.json")
    assert migrate_guardrails_round20(p) == "no_file"


def test_migration_handles_malformed_json(tmp_path):
    from migrations import migrate_guardrails_round20
    p = str(tmp_path / "guardrails.json")
    with open(p, "w") as f:
        f.write("{ this is not valid json")
    assert migrate_guardrails_round20(p) == "no_file"


def test_run_all_migrations_multi_user(tmp_path):
    """Sweep across multiple users — idempotent across the fleet."""
    from migrations import run_all_migrations, MIGRATION_ROUND20_POSITION_CAP
    # Three users with different states
    user_dirs = {}
    for uid, cap in [(1, 0.10), (2, 0.15), (3, None)]:
        udir = tmp_path / f"users/{uid}"
        udir.mkdir(parents=True)
        gpath = udir / "guardrails.json"
        data = {"mode": "paper"}
        if cap is not None:
            data["max_position_pct"] = cap
        with open(gpath, "w") as f:
            json.dump(data, f)
        user_dirs[uid] = str(gpath)

    users = [{"id": uid, "username": f"u{uid}"} for uid in (1, 2, 3)]

    def _user_file_fn(user, filename):
        return user_dirs[user["id"]]

    summary = run_all_migrations(users, _user_file_fn)
    assert summary[1]["round20_position_cap"] == "migrated"
    assert summary[2]["round20_position_cap"] == "user_customised"
    assert summary[3]["round20_position_cap"] == "migrated"

    # Verify each file on disk
    assert _read(user_dirs[1])["max_position_pct"] == 0.07
    assert _read(user_dirs[2])["max_position_pct"] == 0.15
    assert _read(user_dirs[3])["max_position_pct"] == 0.07

    # Re-run — all should be "already_applied"
    summary2 = run_all_migrations(users, _user_file_fn)
    for uid in (1, 2, 3):
        assert summary2[uid]["round20_position_cap"] == "already_applied"


def test_run_all_migrations_tolerates_errors(tmp_path):
    """One user's failure must not affect others."""
    from migrations import run_all_migrations
    good_gpath = str(tmp_path / "users/1/guardrails.json")
    _write(good_gpath, {"max_position_pct": 0.10})
    users = [
        {"id": 1, "username": "good"},
        {"id": 2, "username": "broken"},
    ]

    def _user_file_fn(user, filename):
        if user["id"] == 2:
            raise RuntimeError("simulated failure")
        return good_gpath

    summary = run_all_migrations(users, _user_file_fn)
    assert summary[1]["round20_position_cap"] == "migrated"
    assert summary[2]["round20_position_cap"].startswith("error")
