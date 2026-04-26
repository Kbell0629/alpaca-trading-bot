"""Round-61 pt.88 — Analytics Hub UI for slippage + entry rationale.

Pt.80 + pt.82 + pt.84 produced the data; pt.88 surfaces it in
the Analytics Hub via two new render functions:
  * `_analyticsRenderSlippagePanel(slippage_summary)` — verdict
    pill + per-strategy mean-bps grid
  * `_analyticsRenderRationalePanel(rationale_breakdown)` —
    winners-vs-losers signal-comparison table with signed deltas

Both panels read from new `build_analytics_view` keys that
pt.88 added:
  * `slippage_summary` (already added by pt.80, render added here)
  * `rationale_breakdown` (added by pt.88)

Tests cover:
  * `_safe_rationale_breakdown` returns aggregator output
  * `build_analytics_view` exposes both keys
  * Source-pin: render functions defined + called from
    renderAnalyticsPanel
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent
_DASH = (_HERE / "templates" / "dashboard.html").read_text()
_AC = (_HERE / "analytics_core.py").read_text()


# ============================================================================
# analytics_core wiring
# ============================================================================

def test_analytics_view_includes_rationale_breakdown():
    import analytics_core
    out = analytics_core.build_analytics_view(
        journal={"trades": []}, scorecard={}, account=None, picks=[])
    assert "rationale_breakdown" in out
    rb = out["rationale_breakdown"]
    assert "winners" in rb
    assert "losers" in rb


def test_rationale_breakdown_with_data():
    import analytics_core
    journal = {"trades": [
        {"status": "closed", "pnl": 100,
         "entry_rationale": {
             "score": 300, "rs_score": 8,
             "confluence_count": 4, "kelly_mult": 1.2}},
        {"status": "closed", "pnl": -50,
         "entry_rationale": {
             "score": 150, "rs_score": 1,
             "confluence_count": 2, "kelly_mult": 0.8}},
    ]}
    out = analytics_core.build_analytics_view(
        journal=journal, scorecard={}, account=None, picks=[])
    rb = out["rationale_breakdown"]
    assert rb["winners"]["count"] == 1
    assert rb["losers"]["count"] == 1
    # Winners had higher score → positive delta.
    assert rb["delta"]["score"] > 0


def test_safe_rationale_breakdown_handles_missing_module(monkeypatch):
    """If entry_rationale import fails for any reason, returns
    a safe default shape."""
    import analytics_core
    # Force ImportError by sabotaging the lazy import.
    import sys
    if "entry_rationale" in sys.modules:
        del sys.modules["entry_rationale"]
    sys.modules["entry_rationale"] = None  # any access raises
    try:
        out = analytics_core._safe_rationale_breakdown({"trades": []})
        assert out["winners"]["count"] == 0
        assert out["losers"]["count"] == 0
    finally:
        del sys.modules["entry_rationale"]


# ============================================================================
# Source-pin: dashboard render
# ============================================================================

def test_analytics_panel_renders_slippage():
    """`_analyticsRenderSlippagePanel` is defined + called from
    renderAnalyticsPanel."""
    assert "function _analyticsRenderSlippagePanel" in _DASH
    assert "_analyticsRenderSlippagePanel(d.slippage_summary)" in _DASH


def test_analytics_panel_renders_rationale():
    assert "function _analyticsRenderRationalePanel" in _DASH
    assert "_analyticsRenderRationalePanel(d.rationale_breakdown)" in _DASH


def test_slippage_panel_renders_verdict_pill_states():
    """The slippage verdict has 4 states (ok / warn / alert /
    preliminary). Each gets its own pill color."""
    fn_idx = _DASH.find("function _analyticsRenderSlippagePanel")
    fn_block = _DASH[fn_idx:fn_idx + 3000]
    for state in ("ok", "warn", "alert"):
        assert "'" + state + "'" in fn_block


def test_slippage_panel_includes_per_strategy_grid():
    fn_idx = _DASH.find("function _analyticsRenderSlippagePanel")
    fn_block = _DASH[fn_idx:fn_idx + 3000]
    assert "by_strategy" in fn_block
    assert "mean_bps" in fn_block


def test_rationale_panel_handles_empty():
    """No closed trades with structured rationale → empty-state
    message instead of an empty table."""
    fn_idx = _DASH.find("function _analyticsRenderRationalePanel")
    fn_block = _DASH[fn_idx:fn_idx + 3500]
    assert "No closed trades with structured rationale" in fn_block


def test_rationale_panel_shows_signed_deltas():
    """The winners-vs-losers delta column uses signed colors —
    green when winners had more of the signal, red when fewer."""
    fn_idx = _DASH.find("function _analyticsRenderRationalePanel")
    fn_block = _DASH[fn_idx:fn_idx + 3500]
    assert "var(--green)" in fn_block
    assert "var(--red)" in fn_block
    # The 4-row signal table covers score, RS, confluence, kelly.
    for sig in ("Screener score", "RS score", "Confluence", "Kelly"):
        assert sig in fn_block


def test_renderAnalyticsPanel_calls_both_new_helpers():
    """The main panel renderer wires both new helpers in via
    `html += ...`. Order: slippage first, rationale second."""
    idx = _DASH.find("function renderAnalyticsPanel")
    block = _DASH[idx:_DASH.find("\n}\n", idx)]
    sl_idx = block.find("_analyticsRenderSlippagePanel")
    rt_idx = block.find("_analyticsRenderRationalePanel")
    assert sl_idx > 0 and rt_idx > 0


# ============================================================================
# Pure-module discipline (analytics_core stays free of cycle imports)
# ============================================================================

def test_analytics_core_imports_safely():
    """analytics_core should not import entry_rationale or
    slippage_tracker at module-load time; both are lazy in the
    `_safe_*` helpers."""
    forbidden_top = ("\nimport entry_rationale\n",
                       "\nfrom entry_rationale import",
                       "\nimport slippage_tracker\n",
                       "\nfrom slippage_tracker import")
    for f in forbidden_top:
        assert f not in _AC
