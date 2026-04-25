"""Round-61 pt.56 — picks history snapshot + pipeline-backtest
endpoint + dashboard panel.

Three layers:
  1. ``picks_history`` pure module (snapshot/load/trim).
  2. ``/api/pipeline-backtest`` endpoint in actions_mixin.
  3. Dashboard panel render hook in templates/dashboard.html.

Tests use lazy imports inside test bodies (CI-stable pattern from
pt.52).
"""
from __future__ import annotations

import json
import os
import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# picks_history pure module
# ============================================================================

def test_snapshot_picks_creates_file(tmp_path):
    import picks_history as ph
    p = tmp_path / "h.json"
    ph.snapshot_picks("2026-04-25", [{"symbol": "AAPL"}], str(p))
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["version"] == 1
    assert "2026-04-25" in data["snapshots"]
    assert data["snapshots"]["2026-04-25"]["picks"][0]["symbol"] == "AAPL"


def test_snapshot_picks_appends_subsequent_dates(tmp_path):
    import picks_history as ph
    p = tmp_path / "h.json"
    ph.snapshot_picks("2026-04-24", [{"symbol": "A"}], str(p))
    ph.snapshot_picks("2026-04-25", [{"symbol": "B"}], str(p))
    data = json.loads(p.read_text())
    assert "2026-04-24" in data["snapshots"]
    assert "2026-04-25" in data["snapshots"]


def test_snapshot_picks_idempotent_on_same_date(tmp_path):
    """Re-snapshotting same date overwrites (not duplicate)."""
    import picks_history as ph
    p = tmp_path / "h.json"
    ph.snapshot_picks("2026-04-25", [{"symbol": "A"}], str(p))
    ph.snapshot_picks("2026-04-25", [{"symbol": "B"}], str(p))
    data = json.loads(p.read_text())
    picks = data["snapshots"]["2026-04-25"]["picks"]
    assert len(picks) == 1
    assert picks[0]["symbol"] == "B"


def test_snapshot_picks_caps_at_max_days(tmp_path):
    import picks_history as ph
    p = tmp_path / "h.json"
    # Push 5 days, cap at 3.
    for d in ("2026-04-20", "2026-04-21", "2026-04-22",
                "2026-04-23", "2026-04-24"):
        ph.snapshot_picks(d, [{"date": d}], str(p), max_days=3)
    data = json.loads(p.read_text())
    snaps = data["snapshots"]
    assert len(snaps) == 3
    # Oldest two pruned; only the most-recent 3 retained.
    assert "2026-04-22" in snaps
    assert "2026-04-23" in snaps
    assert "2026-04-24" in snaps
    assert "2026-04-20" not in snaps


def test_snapshot_picks_invalid_input_no_op(tmp_path):
    import picks_history as ph
    p = tmp_path / "h.json"
    ph.snapshot_picks("", [], str(p))
    assert not p.exists()
    ph.snapshot_picks("2026-04-25", "not a list", str(p))
    assert not p.exists()


def test_load_picks_history_returns_sorted_list(tmp_path):
    import picks_history as ph
    p = tmp_path / "h.json"
    # Snapshot dates out-of-order — load should sort ascending.
    ph.snapshot_picks("2026-04-25", [{"v": "third"}], str(p))
    ph.snapshot_picks("2026-04-23", [{"v": "first"}], str(p))
    ph.snapshot_picks("2026-04-24", [{"v": "second"}], str(p))
    history = ph.load_picks_history(str(p))
    dates = [h["date"] for h in history]
    assert dates == ["2026-04-23", "2026-04-24", "2026-04-25"]


def test_load_picks_history_empty_when_missing(tmp_path):
    import picks_history as ph
    assert ph.load_picks_history(str(tmp_path / "missing.json")) == []


def test_load_picks_history_handles_corrupt_file(tmp_path):
    import picks_history as ph
    p = tmp_path / "h.json"
    p.write_text("{not valid json")
    assert ph.load_picks_history(str(p)) == []


def test_trim_to_max_days_keeps_last_n():
    import picks_history as ph
    history = [{"date": f"2026-04-{i:02d}", "picks": []}
                for i in range(20, 26)]
    out = ph.trim_to_max_days(history, 3)
    dates = [h["date"] for h in out]
    assert dates == ["2026-04-23", "2026-04-24", "2026-04-25"]


def test_trim_to_max_days_handles_invalid_input():
    import picks_history as ph
    assert ph.trim_to_max_days([], 3) == []
    assert ph.trim_to_max_days(None, 3) == []
    assert ph.trim_to_max_days([{"date": "x"}], 0) == []


def test_round_trip_snapshot_then_load(tmp_path):
    """Snapshot multiple days, load back, format matches what
    pipeline_backtest.run_pipeline_backtest expects."""
    import picks_history as ph
    p = tmp_path / "h.json"
    pick_a = {"symbol": "AAPL", "best_score": 80, "daily_change": 2}
    pick_b = {"symbol": "MSFT", "best_score": 60, "daily_change": 3}
    ph.snapshot_picks("2026-04-24", [pick_a], str(p))
    ph.snapshot_picks("2026-04-25", [pick_b], str(p))
    history = ph.load_picks_history(str(p))
    assert len(history) == 2
    assert history[0]["picks"] == [pick_a]
    assert history[1]["picks"] == [pick_b]


def test_atomic_write_doesnt_leave_temp_on_success(tmp_path):
    """No .picks_hist_*.tmp files after a successful write."""
    import picks_history as ph
    p = tmp_path / "h.json"
    ph.snapshot_picks("2026-04-25", [], str(p))
    leftovers = [f for f in os.listdir(str(tmp_path))
                 if f.startswith(".picks_hist_")]
    assert leftovers == []


# ============================================================================
# update_dashboard hooks the snapshot
# ============================================================================

def test_update_dashboard_imports_picks_history():
    src = _src("update_dashboard.py")
    assert "import picks_history" in src
    assert "snapshot_picks" in src


def test_update_dashboard_picks_history_path_env_overridable():
    """Test env var name is wired so per-user paths can be set."""
    src = _src("update_dashboard.py")
    assert "PICKS_HISTORY_PATH" in src


# ============================================================================
# /api/pipeline-backtest endpoint source pins
# ============================================================================

def test_server_route_registered():
    src = _src("server.py")
    assert '"/api/pipeline-backtest"' in src
    assert "handle_pipeline_backtest" in src


def test_handler_lives_in_actions_mixin():
    src = _src("handlers/actions_mixin.py")
    assert "def handle_pipeline_backtest" in src


def test_handler_is_read_only():
    src = _src("handlers/actions_mixin.py")
    start = src.find("def handle_pipeline_backtest")
    end = src.find("def handle_trades_view", start)
    body = src[start:end]
    assert "user_api_post" not in body
    assert "user_api_delete" not in body
    assert "save_json" not in body


def test_handler_requires_auth():
    src = _src("handlers/actions_mixin.py")
    start = src.find("def handle_pipeline_backtest")
    body = src[start:start + 1500]
    assert "if not self.current_user" in body
    assert "401" in body


def test_handler_handles_empty_history():
    """When picks_history.json doesn't exist or is empty, return a
    helpful message instead of zeroing-out the response."""
    src = _src("handlers/actions_mixin.py")
    start = src.find("def handle_pipeline_backtest")
    end = src.find("def handle_trades_view", start)
    body = src[start:end]
    assert "No picks history yet" in body


# ============================================================================
# Dashboard panel source pins
# ============================================================================

def test_dashboard_has_pipeline_backtest_panel():
    src = _src("templates/dashboard.html")
    assert "Pipeline Backtest" in src
    assert "pipelineBacktestPanel" in src


def test_dashboard_has_run_pipeline_backtest_function():
    src = _src("templates/dashboard.html")
    assert "function runPipelineBacktest" in src
    assert "function renderPipelineBacktest" in src


def test_dashboard_calls_pipeline_backtest_endpoint():
    src = _src("templates/dashboard.html")
    start = src.find("function runPipelineBacktest")
    end = src.find("function renderPipelineBacktest", start)
    body = src[start:end]
    assert "/api/pipeline-backtest" in body
    assert "POST" in body


def test_dashboard_renders_block_rate_and_reasons():
    src = _src("templates/dashboard.html")
    start = src.find("function renderPipelineBacktest")
    end = start + 4000
    body = src[start:end]
    assert "Block rate" in body or "block_rate" in body
    assert "BLOCKED BY REASON" in body
    assert "Would deploy" in body
