"""Round-61 pt.46 — /api/analytics endpoint + dashboard wiring.

Pure analytics math is covered by test_round61_pt46_analytics_core.py.
This file pins the HTTP surface, server route, dashboard tab, and
panel render hooks so a future refactor can't silently break the
end-to-end flow.
"""
from __future__ import annotations

import json
import os
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# Server route + handler exist
# ============================================================================

def test_analytics_route_registered_in_server():
    src = _src("server.py")
    assert '"/api/analytics"' in src
    assert "handle_analytics_view" in src


def test_analytics_handler_lives_in_actions_mixin():
    src = _src("handlers/actions_mixin.py")
    assert "def handle_analytics_view" in src
    assert "build_analytics_view" in src
    assert "analytics_core" in src


def test_analytics_handler_is_read_only():
    """The endpoint must not place orders, modify files, or write
    journal entries. Read-only invariant."""
    src = _src("handlers/actions_mixin.py")
    handler_start = src.find("def handle_analytics_view")
    handler_end = src.find("def handle_trades_view", handler_start)
    body = src[handler_start:handler_end]
    assert "user_api_post" not in body
    assert "user_api_delete" not in body
    assert "record_trade" not in body
    assert "save_json" not in body


def test_analytics_handler_requires_auth():
    src = _src("handlers/actions_mixin.py")
    handler_start = src.find("def handle_analytics_view")
    body = src[handler_start:handler_start + 1500]
    assert "if not self.current_user" in body
    assert "401" in body


# ============================================================================
# Dashboard wiring
# ============================================================================

def test_dashboard_has_analytics_nav_tab():
    src = _src("templates/dashboard.html")
    assert "section-analytics" in src
    assert "_navBtn('section-analytics'" in src


def test_dashboard_has_analytics_section_anchor():
    src = _src("templates/dashboard.html")
    assert 'id="section-analytics"' in src


def test_dashboard_has_refresh_analytics_handler():
    src = _src("templates/dashboard.html")
    assert "function refreshAnalyticsPanel" in src
    assert "function renderAnalyticsPanel" in src


def test_dashboard_calls_analytics_endpoint():
    src = _src("templates/dashboard.html")
    start = src.find("function refreshAnalyticsPanel")
    end = src.find("function renderAnalyticsPanel", start)
    body = src[start:end]
    assert "'/api/analytics'" in body or '"/api/analytics"' in body
    # POST with empty body
    assert "'POST'" in body or '"POST"' in body


def test_dashboard_init_prefetches_analytics():
    """The init function should prefetch analytics so the tab is ready."""
    src = _src("templates/dashboard.html")
    assert "refreshAnalyticsPanel()" in src


def test_dashboard_renders_kpi_cards():
    """Every headline KPI card must be present in the render path."""
    src = _src("templates/dashboard.html")
    start = src.find("function renderAnalyticsPanel")
    end = start + 12000
    body = src[start:end]
    # Labels use literal `&` in JS strings (no HTML-escaped form).
    for label in ("Total P&L", "Win Rate", "Expectancy", "Avg Win",
                   "Max DD", "Sharpe", "Avg Hold", "Validation"):
        assert label in body, f"KPI card missing: {label}"


def test_dashboard_renders_equity_chart():
    src = _src("templates/dashboard.html")
    assert "_analyticsRenderEquityChart" in src
    # SVG-based, no Chart.js dependency
    start = src.find("function _analyticsRenderEquityChart")
    body = src[start:start + 2000]
    assert "polyline" in body
    assert "polygon" in body


def test_dashboard_renders_per_strategy_breakdown():
    src = _src("templates/dashboard.html")
    start = src.find("function renderAnalyticsPanel")
    body = src[start:start + 12000]
    assert "PER-STRATEGY PERFORMANCE" in body
    assert "strategy_breakdown" in body


def test_dashboard_renders_distributions():
    src = _src("templates/dashboard.html")
    start = src.find("function renderAnalyticsPanel")
    body = src[start:start + 12000]
    assert "P&L Distribution" in body
    assert "Hold Time Distribution" in body


def test_dashboard_renders_top_symbols_and_exit_reasons():
    src = _src("templates/dashboard.html")
    start = src.find("function renderAnalyticsPanel")
    body = src[start:start + 12000]
    assert "Top Symbols by Impact" in body
    assert "P&L by Exit Reason" in body


def test_dashboard_renders_best_worst_trades():
    src = _src("templates/dashboard.html")
    start = src.find("function renderAnalyticsPanel")
    body = src[start:start + 12000]
    assert "Best Trades" in body
    assert "Worst Trades" in body


def test_dashboard_renders_filter_summary():
    src = _src("templates/dashboard.html")
    start = src.find("function renderAnalyticsPanel")
    body = src[start:start + 12000]
    assert "Screener Filter Summary" in body


# ============================================================================
# Endpoint via http_harness — runs only on CI (cryptography needed)
# ============================================================================

def test_analytics_endpoint_requires_auth(http_harness):
    http_harness.create_user()
    http_harness.logout()
    resp = http_harness.post("/api/analytics", body={})
    assert resp["status"] in (401, 403)


def test_analytics_endpoint_returns_full_payload_shape(http_harness):
    """Authed request returns the build_analytics_view payload."""
    http_harness.create_user()
    resp = http_harness.post("/api/analytics", body={})
    assert resp["status"] == 200
    body = resp["body"]
    assert body.get("success") is True
    expected_keys = {
        "kpis", "equity_curve", "drawdown_curve",
        "strategy_breakdown", "pnl_by_period",
        "pnl_by_symbol", "pnl_by_exit_reason",
        "hold_time_distribution", "pnl_distribution",
        "best_worst_trades", "filter_summary",
    }
    for k in expected_keys:
        assert k in body, f"Missing analytics key: {k}"


def test_analytics_endpoint_with_seeded_journal(http_harness, tmp_path):
    """Seed a known journal + scorecard and verify the endpoint
    returns the right aggregates."""
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
             "exit_timestamp": "2026-04-22T15:55:00-04:00",
             "exit_price": 110.0, "pnl": 100.0, "pnl_pct": 10.0,
             "exit_reason": "target_hit"},
            {"timestamp": "2026-04-21T09:30:00-04:00",
             "symbol": "MSFT", "strategy": "wheel",
             "side": "buy", "qty": 5, "price": 400.0,
             "status": "closed",
             "exit_timestamp": "2026-04-23T10:15:00-04:00",
             "exit_price": 380.0, "pnl": -100.0, "pnl_pct": -5.0,
             "exit_reason": "stop_triggered"},
        ],
        "daily_snapshots": [],
    }
    with open(journal_path, "w") as f:
        json.dump(journal, f)
    resp = http_harness.post("/api/analytics", body={})
    assert resp["status"] == 200
    body = resp["body"]
    kpis = body["kpis"]
    assert kpis["closed_trades"] == 2
    assert kpis["wins"] == 1
    assert kpis["losses"] == 1
    assert kpis["total_realized_pnl"] == 0.0  # 100 + -100
    breakdown = body["strategy_breakdown"]
    assert breakdown["breakout"]["total_pnl"] == 100.0
    assert breakdown["wheel"]["total_pnl"] == -100.0


def test_analytics_endpoint_handles_missing_files(http_harness):
    """A user with no journal/scorecard yet should get an empty
    analytics payload, not a 500."""
    http_harness.create_user()
    resp = http_harness.post("/api/analytics", body={})
    assert resp["status"] == 200
    body = resp["body"]
    assert body["kpis"]["closed_trades"] == 0
    assert body["equity_curve"] == []
    assert body["strategy_breakdown"] == {}
