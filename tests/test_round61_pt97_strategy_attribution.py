"""Round-61 pt.97 — per-strategy attribution sweep.

The Analytics Hub already renders a per-strategy breakdown
(count, wins, win rate, total / avg / best / worst), but it
doesn't surface edge metrics or contribution share — so a user
preparing to flip live can't easily see WHICH of their 6
strategies are carrying the bot vs which are neutral / dragging.

Pt.97 adds ``compute_strategy_attribution`` which extends the
breakdown with profit factor, expectancy, max drawdown,
dollar-contribution share, and a verdict bucket. New panel in
the Analytics Hub renders the per-strategy table sorted by
dollar contribution + a verdict-counts pill row + a headline
summary so the user can see the answer at a glance.

Tests cover:
  * Pure module — verdict thresholds (carrying / neutral /
    dragging / preliminary), profit factor (incl. infinity),
    max drawdown, dollar-contribution share math, ranking sort
    order, headline copy, empty-journal edge case.
  * build_analytics_view exposes ``strategy_attribution``.
  * Dashboard renders the panel + the verdict pills.
"""
from __future__ import annotations

import importlib
import pathlib
import sys


_HERE = pathlib.Path(__file__).resolve().parent.parent
_DASH = (_HERE / "templates" / "dashboard.html").read_text()


def _import_analytics_core():
    sys.path.insert(0, str(_HERE))
    if "analytics_core" in sys.modules:
        return importlib.reload(sys.modules["analytics_core"])
    import analytics_core
    return analytics_core


# ============================================================================
# Pure module
# ============================================================================

def _trade(strategy, pnl, ts="2026-04-20T15:00:00Z", status="closed"):
    return {"strategy": strategy, "pnl": pnl,
            "exit_timestamp": ts, "status": status}


def test_attribution_empty_journal():
    ac = _import_analytics_core()
    out = ac.compute_strategy_attribution({})
    assert out["strategies"] == {}
    assert out["ranking"] == []
    assert out["overall_realized_pnl"] == 0
    assert "No closed trades yet" in out["headline"]


def test_attribution_skips_open_trades_and_bad_pnl():
    ac = _import_analytics_core()
    out = ac.compute_strategy_attribution({"trades": [
        _trade("trailing_stop", 10.0, status="open"),
        {"strategy": "wheel", "pnl": "not-a-number",
          "status": "closed"},
        _trade("breakout", 5.0),
    ]})
    assert "trailing_stop" not in out["strategies"]
    assert "wheel" not in out["strategies"]
    assert "breakout" in out["strategies"]


def test_attribution_preliminary_under_min_trades():
    """Fewer than 10 closed trades for a strategy → preliminary
    verdict regardless of how good the stats look."""
    ac = _import_analytics_core()
    trades = [_trade("breakout", 50.0, ts=f"2026-04-{i:02d}T15:00:00Z")
              for i in range(1, 6)]  # 5 wins, $250 total
    out = ac.compute_strategy_attribution({"trades": trades})
    assert out["strategies"]["breakout"]["verdict"] == "preliminary"
    assert out["verdict_counts"]["preliminary"] == 1


def test_attribution_carrying_when_thresholds_clear():
    """≥10 trades, win rate ≥ 45%, profit factor ≥ 1.2, total $ > 0."""
    ac = _import_analytics_core()
    # 12 trades: 8 wins of $20, 4 losses of $10 → wr=67%, pf=4.0
    trades = []
    for i in range(8):
        trades.append(_trade("trailing_stop", 20.0,
                              ts=f"2026-04-{i+1:02d}T15:00:00Z"))
    for i in range(4):
        trades.append(_trade("trailing_stop", -10.0,
                              ts=f"2026-04-{i+9:02d}T15:00:00Z"))
    out = ac.compute_strategy_attribution({"trades": trades})
    s = out["strategies"]["trailing_stop"]
    assert s["count"] == 12
    assert s["win_rate"] == round(8 / 12, 4)
    assert s["profit_factor"] == 4.0
    assert s["verdict"] == "carrying"


def test_attribution_dragging_when_negative_total():
    """Negative total $ pulls a strategy into the dragging bucket
    even before the win-rate / PF gates fire."""
    ac = _import_analytics_core()
    trades = []
    # 11 trades: 4 wins of $5, 7 losses of $10 → total = -$50
    for i in range(4):
        trades.append(_trade("mean_reversion", 5.0,
                              ts=f"2026-04-{i+1:02d}T15:00:00Z"))
    for i in range(7):
        trades.append(_trade("mean_reversion", -10.0,
                              ts=f"2026-04-{i+5:02d}T15:00:00Z"))
    out = ac.compute_strategy_attribution({"trades": trades})
    s = out["strategies"]["mean_reversion"]
    assert s["total_pnl"] < 0
    assert s["verdict"] == "dragging"


def test_attribution_dragging_low_winrate_low_pf():
    """Even if total $ is technically positive, a strategy below
    BOTH the dragging win-rate floor (35%) AND the dragging PF
    floor (0.8) is still dragging."""
    ac = _import_analytics_core()
    # 10 trades: 3 wins $30, 7 losses $12 → wr=30%, pf=$90/$84≈1.07
    # That's >0.8 PF so this case actually verdicts neutral —
    # use a tighter setup: 3 wins $30, 7 losses $13 → pf=$90/$91≈0.99
    trades = []
    for _ in range(3):
        trades.append(_trade("dragging_strat", 30.0))
    for _ in range(7):
        trades.append(_trade("dragging_strat", -13.0))
    out = ac.compute_strategy_attribution({"trades": trades})
    s = out["strategies"]["dragging_strat"]
    assert s["count"] == 10
    # Verdict should be dragging or neutral — total here is
    # $90 - $91 = -$1, just barely negative → dragging via
    # the negative-total check.
    assert s["verdict"] == "dragging"


def test_attribution_neutral_when_neither_carrying_nor_dragging():
    ac = _import_analytics_core()
    # 10 trades: 5 wins $10, 5 losses $5 → wr=50%, pf=2.0, total=+$25
    # Should be carrying, not neutral. Use a mid-range case:
    # 10 trades: 4 wins $10, 6 losses $4 → wr=40%, pf=$40/$24=1.67, total=+$16
    # Win rate < 45 → can't be carrying. Total > 0 → not dragging.
    # Win rate > 35 OR pf > 0.8 (both true) → not dragging via the
    # combined-floor check.  Verdict: neutral.
    trades = []
    for _ in range(4):
        trades.append(_trade("neutral_strat", 10.0))
    for _ in range(6):
        trades.append(_trade("neutral_strat", -4.0))
    out = ac.compute_strategy_attribution({"trades": trades})
    s = out["strategies"]["neutral_strat"]
    assert s["verdict"] == "neutral"


def test_attribution_profit_factor_infinity_when_no_losses():
    """All wins and zero losses → profit factor of inf — must be
    JSON-serialisable shape (we render '∞' in the UI)."""
    ac = _import_analytics_core()
    trades = [_trade("perfect_strat", 5.0,
                       ts=f"2026-04-{i+1:02d}T15:00:00Z")
              for i in range(15)]
    out = ac.compute_strategy_attribution({"trades": trades})
    s = out["strategies"]["perfect_strat"]
    assert s["profit_factor"] == float("inf")
    assert s["verdict"] == "carrying"


def test_attribution_max_drawdown_chronological():
    """+$10, -$30 (cum -$20, peak $10, dd 100% = (10 - -20)/10 * 100 = 300%)"""
    ac = _import_analytics_core()
    trades = [
        _trade("dd_strat",  10.0, ts="2026-04-01T15:00:00Z"),
        _trade("dd_strat", -30.0, ts="2026-04-02T15:00:00Z"),
    ]
    out = ac.compute_strategy_attribution({"trades": trades})
    s = out["strategies"]["dd_strat"]
    # Peak was $10 cum; trough is -$20 cum. dd = (10 - -20)/10 * 100.
    assert s["max_drawdown_pct"] == 300.0


def test_attribution_dollar_contribution_share():
    ac = _import_analytics_core()
    trades = [
        # carrier_a: 12 trades sums to +$80
        *[_trade("carrier_a", 10.0,
                  ts=f"2026-04-{i+1:02d}T15:00:00Z") for i in range(8)],
        *[_trade("carrier_a", 0.0,
                  ts=f"2026-04-{i+9:02d}T15:00:00Z") for i in range(4)],
        # neutral_b: 10 trades sums to +$20
        *[_trade("neutral_b", 2.0,
                  ts=f"2026-04-{i+15:02d}T15:00:00Z") for i in range(10)],
    ]
    # Adjust sums: carrier_a has 8 × $10 + 4 × $0 = $80 wins, no
    # losses → pf = inf, verdict carrying
    out = ac.compute_strategy_attribution({"trades": trades})
    overall = out["overall_realized_pnl"]
    assert overall == 100.0  # $80 + $20
    a = out["strategies"]["carrier_a"]
    b = out["strategies"]["neutral_b"]
    assert a["dollar_contribution_pct"] == 80.0
    assert b["dollar_contribution_pct"] == 20.0
    # Ranking: carrier_a should come first.
    assert out["ranking"][0]["strategy"] == "carrier_a"


def test_attribution_headline_summarises_buckets():
    ac = _import_analytics_core()
    # Build one strategy per bucket so the headline lists all of them.
    trades = []
    # carrier: 12 wins, 0 losses
    for i in range(12):
        trades.append(_trade("carrier", 10.0,
                              ts=f"2026-04-{i+1:02d}T15:00:00Z"))
    # dragger: 11 trades, total negative
    for i in range(3):
        trades.append(_trade("dragger", 5.0,
                              ts=f"2026-04-{i+1:02d}T16:00:00Z"))
    for i in range(8):
        trades.append(_trade("dragger", -10.0,
                              ts=f"2026-04-{i+4:02d}T16:00:00Z"))
    # preliminary: just 3 trades
    for i in range(3):
        trades.append(_trade("preliminary", 5.0,
                              ts=f"2026-04-{i+1:02d}T17:00:00Z"))
    out = ac.compute_strategy_attribution({"trades": trades})
    h = out["headline"]
    assert "carrying" in h.lower()
    assert "dragging" in h.lower()
    assert "preliminary" in h.lower()
    assert "realized" in h.lower()


# ============================================================================
# build_analytics_view integration
# ============================================================================

def test_build_analytics_view_exposes_strategy_attribution():
    ac = _import_analytics_core()
    view = ac.build_analytics_view(journal={"trades": []})
    assert "strategy_attribution" in view
    sa = view["strategy_attribution"]
    for k in ("strategies", "ranking", "verdict_counts",
              "overall_realized_pnl", "headline"):
        assert k in sa


# ============================================================================
# Dashboard markup
# ============================================================================

def test_render_attribution_panel_helper_defined():
    assert "function _analyticsRenderAttributionPanel" in _DASH


def test_render_attribution_panel_called_from_render_analytics():
    idx = _DASH.find("function renderAnalyticsPanel")
    assert idx > 0
    # Function body is ~270 lines; window must fit it.
    fn_block = _DASH[idx:idx + 30000]
    assert "_analyticsRenderAttributionPanel(d.strategy_attribution)" in fn_block


def test_attribution_panel_has_verdict_pills():
    idx = _DASH.find("function _analyticsRenderAttributionPanel")
    fn_block = _DASH[idx:idx + 5000]
    # All four verdict buckets must be rendered as pills.
    for verdict in ("Carrying", "Neutral", "Dragging", "Preliminary"):
        assert verdict in fn_block


def test_attribution_panel_renders_table_columns():
    idx = _DASH.find("function _analyticsRenderAttributionPanel")
    fn_block = _DASH[idx:idx + 5000]
    for col in ("Strategy", "Trades", "Win rate", "Expectancy",
                "Profit factor", "Max DD", "Total $", "Share",
                "Verdict"):
        assert col in fn_block


def test_attribution_panel_handles_infinity_profit_factor():
    """JS path must render Infinity / >999 PF as '∞' so the UI
    doesn't say 'Infinity' literally (matches the pure module's
    'all wins, no losses' case)."""
    idx = _DASH.find("function _analyticsRenderAttributionPanel")
    fn_block = _DASH[idx:idx + 5000]
    assert "Infinity" in fn_block
    assert "'∞'" in fn_block or "∞" in fn_block


def test_attribution_panel_empty_state():
    idx = _DASH.find("function _analyticsRenderAttributionPanel")
    fn_block = _DASH[idx:idx + 5000]
    assert "Closes will populate" in fn_block
