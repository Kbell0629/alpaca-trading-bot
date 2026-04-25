"""Round-61 pt.37 — /api/backtest/run endpoint + dashboard route wiring.

Pure simulation logic is covered by
test_round61_pt37_backtest_core.py and the data-cache layer by
test_round61_pt37_backtest_data.py. This file pins the HTTP surface
and the dashboard panel.
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# Source-pin: route + handler exist
# ============================================================================

def test_backtest_endpoint_route_registered():
    src = _src("server.py")
    assert '"/api/backtest/run"' in src
    assert "handle_backtest_run" in src


def test_backtest_endpoint_handler_exists():
    src = _src("handlers/actions_mixin.py")
    assert "def handle_backtest_run" in src
    assert "run_multi_strategy_backtest" in src
    assert "fetch_bars_for_symbols" in src


def test_backtest_handler_is_read_only():
    """No order placement, no journal writes — backtest is purely
    analytical."""
    src = _src("handlers/actions_mixin.py")
    s = src.find("def handle_backtest_run")
    e = src.find("def handle_force_orphan_adoption", s)
    body = src[s:e]
    assert "user_api_post" not in body
    assert "user_api_delete" not in body
    assert "record_trade" not in body


def test_backtest_handler_requires_auth():
    src = _src("handlers/actions_mixin.py")
    s = src.find("def handle_backtest_run")
    e = src.find("def handle_force_orphan_adoption", s)
    body = src[s:e]
    assert "if not self.current_user" in body
    assert "401" in body


def test_backtest_handler_falls_back_through_universe_sources():
    """Universe selection priority: explicit body.symbols → journal
    universe → dashboard picks. Pin all three sources are referenced."""
    src = _src("handlers/actions_mixin.py")
    s = src.find("def handle_backtest_run")
    e = src.find("def handle_force_orphan_adoption", s)
    body = src[s:e]
    assert "universe_from_journal" in body
    assert "universe_from_dashboard_data" in body
    assert "body.get(\"symbols\")" in body


# ============================================================================
# Dashboard panel surface
# ============================================================================

def test_dashboard_has_30day_backtest_panel():
    src = _src("templates/dashboard.html")
    assert "30-Day Strategy Backtest" in src
    assert "multiBacktestPanel" in src
    assert "runMultiStrategyBacktest" in src


def test_dashboard_has_renderMultiStrategyBacktest_handler():
    src = _src("templates/dashboard.html")
    assert "function renderMultiStrategyBacktest" in src
    assert "function runMultiStrategyBacktest" in src


def test_dashboard_calls_backtest_endpoint():
    src = _src("templates/dashboard.html")
    s = src.find("function runMultiStrategyBacktest")
    e = src.find("function renderMultiStrategyBacktest", s)
    body = src[s:e]
    assert "'/api/backtest/run'" in body or '"/api/backtest/run"' in body


def test_dashboard_panel_has_window_selector():
    """Days dropdown for window selection (14/30/60/90)."""
    src = _src("templates/dashboard.html")
    assert "multiBacktestDaysSelect" in src
    # Common windows present
    for opt in ('value="14"', 'value="30"', 'value="60"', 'value="90"'):
        assert opt in src


def test_dashboard_panel_has_explanation_block():
    """User-facing explainer of how to read the backtest output —
    pin so a refactor doesn't drop it silently."""
    src = _src("templates/dashboard.html")
    assert "How to read this" in src
    # Either an explicit "structurally losing" framing OR guidance
    # toward the disable action — at least one must be present.
    assert ("structurally losing" in src
             or "Auto-Deployer Strategies" in src
             or "Disabling" in src)
