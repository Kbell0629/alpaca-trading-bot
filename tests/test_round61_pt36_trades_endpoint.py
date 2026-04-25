"""Round-61 pt.36 — /api/trades endpoint + dashboard route wiring.

The pure analysis logic is covered by
test_round61_pt36_trades_analysis_core.py. This file pins the HTTP
surface: route registered, auth required, payload shape returned,
filter/sort plumbed through to trades_analysis_core.build_trades_view.
"""
from __future__ import annotations

import json
import os
import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# Source-pin: route + handler exist
# ============================================================================

def test_trades_endpoint_route_registered():
    """server.py must dispatch POST /api/trades to handle_trades_view."""
    src = _src("server.py")
    assert '"/api/trades"' in src
    assert "handle_trades_view" in src


def test_trades_endpoint_handler_exists():
    """The handler lives in handlers/actions_mixin.py and is read-only
    (no order placement, no file writes)."""
    src = _src("handlers/actions_mixin.py")
    assert "def handle_trades_view" in src
    assert "build_trades_view" in src
    assert "trades_analysis_core" in src
    # Read-only — must NOT call user_api_post / save_json / record_trade_*
    body_start = src.find("def handle_trades_view")
    body_end = src.find("def handle_force_orphan_adoption", body_start)
    body = src[body_start:body_end]
    assert "user_api_post" not in body
    assert "user_api_delete" not in body
    assert "record_trade" not in body
    assert "save_json" not in body


def test_trades_endpoint_requires_auth_on_unauthenticated_request():
    """Pin: handler returns 401 when no current_user."""
    src = _src("handlers/actions_mixin.py")
    handler_start = src.find("def handle_trades_view")
    handler_end = src.find("def handle_force_orphan_adoption", handler_start)
    body = src[handler_start:handler_end]
    assert "if not self.current_user" in body
    assert '401' in body


# ============================================================================
# End-to-end via http_harness (mirrors the audit-endpoint tests)
# ============================================================================

def test_trades_endpoint_requires_auth(http_harness):
    """No session → 401."""
    http_harness.create_user()
    http_harness.logout()
    resp = http_harness.post("/api/trades", body={})
    assert resp["status"] in (401, 403)


def test_trades_endpoint_returns_full_payload_shape(http_harness):
    """Authed request returns the build_trades_view payload."""
    http_harness.create_user()
    resp = http_harness.post("/api/trades", body={})
    assert resp["status"] == 200
    body = resp["body"]
    assert body.get("success") is True
    assert "trades" in body
    assert "strategy_summary" in body
    assert "overall_summary" in body
    assert "filters_applied" in body
    assert "sort_by" in body
    assert "descending" in body
    assert "total_count" in body
    assert "filtered_count" in body


def test_trades_endpoint_default_sort_is_exit_timestamp_desc(http_harness):
    """Most recent close at top — pin the user-facing default."""
    http_harness.create_user()
    resp = http_harness.post("/api/trades", body={})
    assert resp["body"]["sort_by"] == "exit_timestamp"
    assert resp["body"]["descending"] is True


def test_trades_endpoint_passes_filters_through(http_harness):
    """Filters in the request body land in `filters_applied` of the
    response — proves they're plumbed through to build_trades_view."""
    http_harness.create_user()
    body_in = {
        "filters": {"status": "closed", "win_loss": "win",
                     "strategy": ["breakout"]},
        "sort_by": "pnl",
        "descending": False,
    }
    resp = http_harness.post("/api/trades", body=body_in)
    assert resp["status"] == 200
    out = resp["body"]
    assert out["filters_applied"]["status"] == "closed"
    assert out["filters_applied"]["win_loss"] == "win"
    assert out["filters_applied"]["strategy"] == ["breakout"]
    assert out["sort_by"] == "pnl"
    assert out["descending"] is False


def test_trades_endpoint_with_seeded_journal(http_harness, tmp_path):
    """Seed a known trade_journal.json and verify the endpoint
    returns the trades + computes summaries."""
    http_harness.create_user()
    user_dir = http_harness.session_user.get("_data_dir")
    if not user_dir:
        # The harness should always set _data_dir; skip if not.
        return
    journal_path = os.path.join(user_dir, "trade_journal.json")
    journal = {
        "trades": [
            {
                "timestamp": "2026-04-20T09:30:00-04:00",
                "symbol": "AAPL", "strategy": "breakout",
                "side": "buy", "qty": 10, "price": 100.0,
                "reason": "breakout score 85",
                "deployer": "cloud_scheduler",
                "status": "closed",
                "exit_timestamp": "2026-04-22T15:55:00-04:00",
                "exit_price": 110.0,
                "exit_reason": "target_hit",
                "pnl": 100.0, "pnl_pct": 10.0,
                "exit_side": "sell",
            },
            {
                "timestamp": "2026-04-21T09:30:00-04:00",
                "symbol": "MSFT", "strategy": "breakout",
                "side": "buy", "qty": 5, "price": 400.0,
                "reason": "breakout score 80",
                "deployer": "cloud_scheduler",
                "status": "closed",
                "exit_timestamp": "2026-04-23T10:15:00-04:00",
                "exit_price": 380.0,
                "exit_reason": "stop_triggered",
                "pnl": -100.0, "pnl_pct": -5.0,
                "exit_side": "sell",
            },
        ],
        "daily_snapshots": [],
    }
    with open(journal_path, "w") as f:
        json.dump(journal, f)

    resp = http_harness.post("/api/trades", body={})
    assert resp["status"] == 200
    out = resp["body"]
    assert out["total_count"] == 2
    assert out["filtered_count"] == 2
    # Strategy summary should bucket both under 'breakout' = 1 win, 1 loss
    bk = out["strategy_summary"].get("breakout") or {}
    assert bk.get("count") == 2
    assert bk.get("wins") == 1
    assert bk.get("losses") == 1


def test_trades_endpoint_filter_strategy_narrows_results(
    http_harness, tmp_path,
):
    """Strategy filter must shrink the result to only matching trades."""
    http_harness.create_user()
    user_dir = http_harness.session_user.get("_data_dir")
    if not user_dir:
        return
    journal_path = os.path.join(user_dir, "trade_journal.json")
    journal = {
        "trades": [
            {"timestamp": "2026-04-20T09:30:00-04:00",
             "symbol": "AAPL", "strategy": "breakout",
             "side": "buy", "qty": 10, "price": 100.0,
             "status": "closed",
             "exit_timestamp": "2026-04-22T15:00:00-04:00",
             "exit_price": 110.0, "pnl": 100.0, "pnl_pct": 10.0,
             "exit_reason": "target_hit"},
            {"timestamp": "2026-04-21T09:30:00-04:00",
             "symbol": "MSFT", "strategy": "wheel",
             "side": "buy", "qty": 5, "price": 400.0,
             "status": "closed",
             "exit_timestamp": "2026-04-23T10:15:00-04:00",
             "exit_price": 420.0, "pnl": 100.0, "pnl_pct": 5.0,
             "exit_reason": "wheel_btc_50pct"},
        ],
        "daily_snapshots": [],
    }
    with open(journal_path, "w") as f:
        json.dump(journal, f)

    resp = http_harness.post("/api/trades",
                              body={"filters": {"strategy": ["wheel"]}})
    assert resp["status"] == 200
    out = resp["body"]
    assert out["filtered_count"] == 1
    assert out["trades"][0]["symbol"] == "MSFT"


def test_trades_endpoint_handles_missing_journal_file(http_harness):
    """A user with no closed trades yet (no trade_journal.json on
    disk) must NOT 500 — return an empty payload."""
    http_harness.create_user()
    resp = http_harness.post("/api/trades", body={})
    assert resp["status"] == 200
    assert resp["body"]["total_count"] == 0
    assert resp["body"]["trades"] == []


# ============================================================================
# Dashboard JS surface — Trades tab + panel + handlers exist
# ============================================================================

def test_dashboard_has_trades_nav_tab():
    """The Trades tab must be in the nav so users can find it."""
    src = _src("templates/dashboard.html")
    assert "section-trades" in src
    assert "_navBtn('section-trades', 'Trades')" in src


def test_dashboard_has_trades_section_anchor():
    src = _src("templates/dashboard.html")
    assert 'id="section-trades"' in src


def test_dashboard_has_refreshTradesPanel_handler():
    src = _src("templates/dashboard.html")
    assert "function refreshTradesPanel" in src
    assert "function renderTradesPanel" in src


def test_dashboard_calls_trades_endpoint():
    """The fetch must hit /api/trades."""
    src = _src("templates/dashboard.html")
    # Check the fetch call exists in the refreshTradesPanel function
    start = src.find("function refreshTradesPanel")
    end = src.find("function renderTradesPanel", start)
    body = src[start:end]
    assert "'/api/trades'" in body or '"/api/trades"' in body


def test_dashboard_trades_panel_has_filter_controls():
    """Pin the major filter controls (status, win/loss, symbol) so a
    refactor that drops them surfaces in tests."""
    src = _src("templates/dashboard.html")
    start = src.find("function renderTradesPanel")
    end = src.find("function _tradeSortHeader", start)
    body = src[start:end]
    # Calls live inside JS string literals with single-quote
    # backslash-escapes (`\'status\'`) — match either form.
    def _has(pat):
        return pat in body or pat.replace("'", "\\'") in body
    assert _has("_setTradesFilter('status'")
    assert _has("_setTradesFilter('win_loss'")
    assert _has("_setTradesFilter('symbol'")
    assert "_toggleTradesStrategy" in body


def test_dashboard_trades_panel_has_post_mortem_render():
    """Per-trade post-mortem render function exists; the click-to-
    expand handler toggles it."""
    src = _src("templates/dashboard.html")
    assert "_renderTradePostMortem" in src
    assert "_toggleTradeDetail" in src


def test_dashboard_trades_panel_has_strategy_summary_section():
    """Per-strategy summary cards above the table — proves the
    strategy_summary payload is rendered."""
    src = _src("templates/dashboard.html")
    start = src.find("function renderTradesPanel")
    end = start + 8000  # stays within renderTradesPanel
    body = src[start:end]
    assert "PER-STRATEGY PERFORMANCE" in body
    assert "strategy_summary" in body


def test_dashboard_trades_panel_has_top_summary_cards():
    """Top-line cards: total trades, win rate, total P&L,
    expectancy, best, worst."""
    src = _src("templates/dashboard.html")
    start = src.find("function renderTradesPanel")
    body = src[start:start + 6000]
    for label in ("Total Trades", "Win Rate", "Total P&amp;L",
                   "Expectancy", "Best", "Worst"):
        assert label in body, f"Missing top-line summary card: {label}"


def test_dashboard_trades_table_supports_sort_by_columns():
    """Sortable column headers — pin that the major fields are
    clickable for sort."""
    src = _src("templates/dashboard.html")
    for field in ("exit_timestamp", "symbol", "strategy",
                   "qty", "price", "exit_price", "pnl", "pnl_pct",
                   "hold_days"):
        assert f"_tradeSortHeader('{field}'" in src, (
            f"Sort header for {field} missing")
