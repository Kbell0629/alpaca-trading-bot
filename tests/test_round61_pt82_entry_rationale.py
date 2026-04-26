"""Round-61 pt.82 — per-trade entry-rationale audit.

Every closed trade has `exit_reason` but not `entry_rationale`.
Pt.82 records a structured rationale dict (score, RS, sector,
sector strength, news, VWAP offset, regime, sizing multipliers,
confluence count, filter chips) at deploy time, embedded directly
in the trade-journal entry.

Tests cover:
  * build_entry_rationale shape + content
  * format_rationale single-line summary
  * aggregate_winners_vs_losers analytics
  * Wiring source-pin: cloud_scheduler.run_auto_deployer embeds
    the rationale into the journal entry it appends
  * Pure-module discipline
"""
from __future__ import annotations


# ============================================================================
# build_entry_rationale
# ============================================================================

def test_build_rationale_minimal_pick():
    """A pick with just price + symbol → rationale still returns the
    expected keys (filled with safe defaults)."""
    import entry_rationale as er
    out = er.build_entry_rationale({"symbol": "AAPL", "price": 100})
    expected_keys = {
        "score", "rs_score", "sector", "sector_strength",
        "news_sentiment", "vwap_offset_pct", "atr_pct",
        "regime", "kelly_mult", "correlation_mult",
        "drawdown_mult", "adv_mult", "confluence_count",
        "filter_reasons", "headline",
    }
    assert set(out.keys()) == expected_keys


def test_build_rationale_captures_score_and_rs():
    import entry_rationale as er
    pick = {"symbol": "X", "price": 100, "best_score": 485,
            "rs_score": 8}
    r = er.build_entry_rationale(pick)
    assert r["score"] == 485.0
    assert r["rs_score"] == 8.0
    assert "score=485" in r["headline"]
    assert "RS=+8" in r["headline"]


def test_build_rationale_sector_strength_strong():
    import entry_rationale as er
    pick = {"symbol": "AAPL", "sector": "Technology",
              "best_score": 200}
    r = er.build_entry_rationale(
        pick, sector_returns={"Technology": 8.0})
    assert r["sector"] == "Technology"
    assert r["sector_strength"] == "strong"
    assert "Technology(strong)" in r["headline"]


def test_build_rationale_sector_strength_weak():
    import entry_rationale as er
    pick = {"sector": "Energy", "best_score": 200}
    r = er.build_entry_rationale(
        pick, sector_returns={"Energy": -7.0})
    assert r["sector_strength"] == "weak"


def test_build_rationale_sector_strength_unknown_when_no_data():
    import entry_rationale as er
    pick = {"sector": "Healthcare", "best_score": 200}
    r = er.build_entry_rationale(pick)
    assert r["sector_strength"] == "unknown"


def test_build_rationale_news_sentiment_passthrough():
    import entry_rationale as er
    r = er.build_entry_rationale(
        {"best_score": 200, "news_sentiment": "bullish"})
    assert r["news_sentiment"] == "bullish"
    assert "news=bullish" in r["headline"]


def test_build_rationale_picks_up_sizing_info():
    import entry_rationale as er
    sizing = {
        "kelly_multiplier": 1.5,
        "correlation_multiplier": 0.5,
        "drawdown_multiplier": 0.75,
        "adv_multiplier": 0.8,
    }
    r = er.build_entry_rationale(
        {"best_score": 300}, sizing_info=sizing)
    assert r["kelly_mult"] == 1.5
    assert r["correlation_mult"] == 0.5
    assert r["drawdown_mult"] == 0.75
    assert r["adv_mult"] == 0.8
    assert "kelly=1.50x" in r["headline"]


def test_build_rationale_kelly_one_x_omitted_from_headline():
    """Kelly 1.0× is the default; don't clutter the headline."""
    import entry_rationale as er
    r = er.build_entry_rationale(
        {"best_score": 300},
        sizing_info={"kelly_multiplier": 1.0})
    assert "kelly=" not in r["headline"]


def test_build_rationale_confluence_count():
    """confluence_count tallies positive signals (max 5)."""
    import entry_rationale as er
    pick = {
        "best_score": 200,           # +1
        "news_sentiment": "bullish",  # +1
        "llm_sentiment_score": 0.8,   # +1
        "insider_data": {"has_cluster_buy": True},   # +1
        "mtf_alignment": "bullish",  # +1
    }
    r = er.build_entry_rationale(pick)
    assert r["confluence_count"] == 5
    assert "conf=5/5" in r["headline"]


def test_build_rationale_partial_confluence():
    import entry_rationale as er
    pick = {
        "best_score": 50,            # below 100 — no signal
        "news_sentiment": "bullish",  # +1
        "llm_sentiment_score": 0.3,   # below 0.5 — no signal
    }
    r = er.build_entry_rationale(pick)
    assert r["confluence_count"] == 1


def test_build_rationale_includes_filter_chips():
    import entry_rationale as er
    pick = {"best_score": 200,
              "filter_reasons": ["already_held", "wide_spread"]}
    r = er.build_entry_rationale(pick)
    assert "already_held" in r["filter_reasons"]
    assert "wide_spread" in r["filter_reasons"]


def test_build_rationale_handles_none_pick():
    """Pure module — gracefully handles None / non-dict input."""
    import entry_rationale as er
    r = er.build_entry_rationale(None)
    assert r["score"] is None
    assert r["sector"] == "Unknown"
    assert r["confluence_count"] == 0
    r2 = er.build_entry_rationale("not a dict")
    assert r2["score"] is None


def test_build_rationale_regime_passthrough():
    import entry_rationale as er
    r = er.build_entry_rationale(
        {"best_score": 200}, regime="bull-strong")
    assert r["regime"] == "bull-strong"


def test_build_rationale_unknown_regime_default():
    import entry_rationale as er
    r = er.build_entry_rationale({"best_score": 200})
    assert r["regime"] == "unknown"


# ============================================================================
# format_rationale
# ============================================================================

def test_format_rationale_returns_headline():
    import entry_rationale as er
    rat = er.build_entry_rationale(
        {"best_score": 300, "rs_score": 5, "news_sentiment": "bullish"})
    s = er.format_rationale(rat)
    assert s == rat["headline"]


def test_format_rationale_handles_missing():
    import entry_rationale as er
    assert er.format_rationale(None) == "(no rationale)"
    assert er.format_rationale({}) == "(no rationale)"


# ============================================================================
# aggregate_winners_vs_losers
# ============================================================================

def _trade(pnl, score=200, rs=5, conf=3, kelly=1.0):
    return {
        "status": "closed", "pnl": pnl,
        "entry_rationale": {
            "score": score, "rs_score": rs,
            "confluence_count": conf,
            "kelly_mult": kelly,
        },
    }


def test_aggregate_empty_journal():
    import entry_rationale as er
    out = er.aggregate_winners_vs_losers(None)
    assert out["winners"]["count"] == 0
    assert out["losers"]["count"] == 0


def test_aggregate_separates_winners_and_losers():
    import entry_rationale as er
    journal = {"trades": [
        _trade(pnl=100, score=300, rs=10),
        _trade(pnl=80, score=250, rs=8),
        _trade(pnl=-50, score=150, rs=2),
    ]}
    out = er.aggregate_winners_vs_losers(journal)
    assert out["winners"]["count"] == 2
    assert out["losers"]["count"] == 1
    # Winners' mean score (275) > losers' mean score (150)
    assert out["winners"]["mean_score"] > out["losers"]["mean_score"]
    assert out["delta"]["score"] > 0


def test_aggregate_skips_open_trades():
    import entry_rationale as er
    closed = _trade(pnl=50)
    open_t = dict(closed)
    open_t["status"] = "open"
    journal = {"trades": [closed, open_t]}
    out = er.aggregate_winners_vs_losers(journal)
    assert out["winners"]["count"] == 1


def test_aggregate_skips_legacy_trades_without_rationale():
    """Pre-pt.82 trades without entry_rationale are silently skipped."""
    import entry_rationale as er
    legacy = {"status": "closed", "pnl": 100, "strategy": "breakout"}
    new = _trade(pnl=100)
    journal = {"trades": [legacy, new]}
    out = er.aggregate_winners_vs_losers(journal)
    assert out["winners"]["count"] == 1


def test_aggregate_delta_signed():
    """delta = winners − losers, so positive deltas mean winners
    had MORE of that signal at entry."""
    import entry_rationale as er
    journal = {"trades": [
        _trade(pnl=100, conf=5, kelly=1.5),
        _trade(pnl=-50, conf=2, kelly=0.5),
    ]}
    out = er.aggregate_winners_vs_losers(journal)
    assert out["delta"]["confluence"] == 3.0   # 5 - 2
    assert out["delta"]["kelly"] == 1.0        # 1.5 - 0.5


# ============================================================================
# Wiring source-pin: auto-deployer embeds the rationale in the journal
# ============================================================================

def test_cloud_scheduler_imports_entry_rationale():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    assert "entry_rationale" in src
    assert "import entry_rationale as _er" in src
    assert "build_entry_rationale(" in src


def test_cloud_scheduler_embeds_rationale_in_journal_append():
    """The auto-deployer's journal append now includes the
    entry_rationale field."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    # Find the auto-deployer's journal-append block.
    idx = src.find('"_screener_score"')
    assert idx > 0
    block = src[idx:idx + 1000]
    assert '"entry_rationale"' in block


def test_cloud_scheduler_logs_rationale_at_deploy():
    """Source-pin: the deployer logs the human-readable rationale
    line so it's visible in the scheduler log without opening the
    journal file."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    assert "format_rationale(_entry_rationale)" in src or \
           "format_rationale(" in src


# ============================================================================
# Pure-module discipline
# ============================================================================

def test_entry_rationale_pure_module():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "entry_rationale.py").read_text()
    forbidden = ("import cloud_scheduler", "from cloud_scheduler",
                  "import auth\n", "from auth import",
                  "import server", "from server import")
    for f in forbidden:
        assert f not in src
