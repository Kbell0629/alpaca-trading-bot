"""Round-61 pt.71 — three accuracy items.

Item A: position-level news exit triggers (new news_exit_monitor.py)
Item B: liquidity-aware ADV cap (extend position_sizing.py)
Item C: recent-drawdown taper for sizing (extend position_sizing.py)

Tests use lazy imports per the pt.52+ pattern.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone


_HERE = pathlib.Path(__file__).resolve().parent.parent


# ============================================================================
# Item A: news_exit_monitor
# ============================================================================

def _now_iso(offset_hours=0):
    return (datetime.now(timezone.utc)
             + timedelta(hours=offset_hours)).isoformat()


def test_news_exit_module_imports():
    import news_exit_monitor as nem
    assert hasattr(nem, "check_position_news")
    assert hasattr(nem, "aggregate_symbol_news_score")
    assert hasattr(nem, "explain_close")


def test_aggregate_score_sums_recent_articles():
    import news_exit_monitor as nem
    def _scorer(article):
        return -10, [{"type": "bearish", "signal": "fda rejection",
                       "weight": -10}]
    articles = [
        {"headline": "FDA rejects drug",
         "created_at": _now_iso(-1)},
        {"headline": "FDA rejects drug",
         "created_at": _now_iso(-2)},
    ]
    out = nem.aggregate_symbol_news_score(articles, score_fn=_scorer)
    assert out["score"] == -20
    assert out["articles_used"] == 2
    assert out["max_bearish_signal"] == "fda rejection"
    assert len(out["headlines"]) == 2


def test_aggregate_score_skips_old_articles():
    import news_exit_monitor as nem
    def _scorer(article):
        return -10, []
    articles = [
        {"headline": "old", "created_at": _now_iso(-100)},
        {"headline": "new", "created_at": _now_iso(-1)},
    ]
    out = nem.aggregate_symbol_news_score(
        articles, max_age_hours=12, score_fn=_scorer)
    assert out["articles_used"] == 1
    assert out["score"] == -10


def test_aggregate_score_handles_empty_input():
    import news_exit_monitor as nem
    out = nem.aggregate_symbol_news_score([])
    assert out["score"] == 0
    assert out["articles_used"] == 0
    assert nem.aggregate_symbol_news_score(None)["score"] == 0


def test_aggregate_score_skips_articles_with_bad_timestamps():
    import news_exit_monitor as nem
    articles = [
        {"headline": "no timestamp"},
        {"headline": "bad timestamp", "created_at": "not-a-date"},
        {"headline": "good", "created_at": _now_iso(-1)},
    ]
    def _scorer(a):
        return -5, []
    out = nem.aggregate_symbol_news_score(articles, score_fn=_scorer)
    assert out["articles_used"] == 1


def test_check_position_news_triggers_close_at_threshold():
    import news_exit_monitor as nem
    def _fetch(sym, limit):
        return [{"headline": "FDA rejects",
                 "created_at": _now_iso(-1)}]
    def _scorer(a):
        return -12, [{"type": "bearish", "signal": "fda rejection",
                       "weight": -12}]
    positions = [{"symbol": "AAA", "qty": 10}]
    out = nem.check_position_news(
        positions, _fetch,
        bearish_close_threshold=-10,
        score_fn=_scorer, cooldown_state={})
    assert len(out["closes"]) == 1
    assert out["closes"][0]["symbol"] == "AAA"
    assert out["closes"][0]["score"] == -12


def test_check_position_news_emits_warning_at_lower_threshold():
    import news_exit_monitor as nem
    def _fetch(sym, limit):
        return [{"headline": "downgrade",
                 "created_at": _now_iso(-1)}]
    def _scorer(a):
        return -7, []
    positions = [{"symbol": "AAA", "qty": 10}]
    out = nem.check_position_news(
        positions, _fetch,
        bearish_close_threshold=-10,
        bearish_warn_threshold=-6,
        score_fn=_scorer, cooldown_state={})
    assert len(out["closes"]) == 0
    assert len(out["warnings"]) == 1


def test_check_position_news_skips_short_positions():
    """Shorts BENEFIT from bearish news — the sweep skips them."""
    import news_exit_monitor as nem
    def _fetch(sym, limit):
        return [{"headline": "FDA rejects",
                 "created_at": _now_iso(-1)}]
    def _scorer(a):
        return -15, []
    positions = [
        {"symbol": "LONG", "qty": 10},
        {"symbol": "SHORT", "qty": -10},
    ]
    out = nem.check_position_news(
        positions, _fetch, score_fn=_scorer, cooldown_state={})
    assert len(out["closes"]) == 1
    assert out["closes"][0]["symbol"] == "LONG"


def test_check_position_news_respects_cooldown():
    import news_exit_monitor as nem
    import time
    def _fetch(sym, limit):
        return [{"headline": "FDA rejects",
                 "created_at": _now_iso(-1)}]
    def _scorer(a):
        return -15, []
    positions = [{"symbol": "AAA", "qty": 10}]
    state = {"AAA": time.time()}  # just checked
    out = nem.check_position_news(
        positions, _fetch, score_fn=_scorer,
        cooldown_state=state, cooldown_sec=600)
    assert out["skipped_cooldown"] == 1
    assert out["checked"] == 0


def test_check_position_news_handles_fetch_failure():
    import news_exit_monitor as nem
    def _boom(sym, limit):
        raise RuntimeError("API down")
    positions = [{"symbol": "AAA", "qty": 10}]
    out = nem.check_position_news(
        positions, _boom, cooldown_state={})
    assert out["closes"] == []


def test_check_position_news_handles_empty_positions():
    import news_exit_monitor as nem
    out = nem.check_position_news([], lambda s, l: [])
    assert out["checked"] == 0
    assert out["closes"] == []


def test_check_position_news_below_thresholds_passes():
    import news_exit_monitor as nem
    def _fetch(sym, limit):
        return [{"headline": "neutral",
                 "created_at": _now_iso(-1)}]
    def _scorer(a):
        return -3, []
    positions = [{"symbol": "AAA", "qty": 10}]
    out = nem.check_position_news(
        positions, _fetch,
        bearish_close_threshold=-10,
        bearish_warn_threshold=-6,
        score_fn=_scorer, cooldown_state={})
    assert out["closes"] == []
    assert out["warnings"] == []


def test_explain_close_human_readable():
    import news_exit_monitor as nem
    s = nem.explain_close({
        "symbol": "AAPL", "score": -15,
        "signal": "sec probe",
        "headlines": ["AAPL faces SEC probe"],
        "articles_used": 2,
    })
    assert "AAPL" in s
    assert "-15" in s
    assert "sec probe" in s


def test_explain_close_handles_bad_input():
    import news_exit_monitor as nem
    assert nem.explain_close(None) == ""
    assert nem.explain_close({}) != ""  # graceful


# ============================================================================
# Item B: ADV cap
# ============================================================================

def test_adv_size_multiplier_within_cap():
    import position_sizing as ps
    # $50 * 100 shares = $5000; 5% of $1M ADV = $50k → cap not hit
    assert ps.adv_size_multiplier(50, 100, 1_000_000) == 1.0


def test_adv_size_multiplier_above_cap():
    import position_sizing as ps
    # $100 * 100 shares = $10k; 5% of $100k ADV = $5k → cap hit at 0.5×
    mult = ps.adv_size_multiplier(100, 100, 100_000)
    assert abs(mult - 0.5) < 0.01


def test_adv_size_multiplier_custom_cap():
    import position_sizing as ps
    # $100 * 100 = $10k; 10% of $100k = $10k → exactly at cap (1.0×)
    mult = ps.adv_size_multiplier(100, 100, 100_000, cap_pct=10.0)
    assert mult == 1.0


def test_adv_size_multiplier_fails_open_on_bad_input():
    import position_sizing as ps
    assert ps.adv_size_multiplier(0, 100, 100_000) == 1.0
    assert ps.adv_size_multiplier(50, 0, 100_000) == 1.0
    assert ps.adv_size_multiplier(50, 100, 0) == 1.0
    assert ps.adv_size_multiplier("bad", 100, 100_000) == 1.0


def test_adv_size_multiplier_floor_at_5pct():
    """A massive over-cap should still yield at least a 5% multiplier
    rather than 0 (avoid silently dropping every deploy)."""
    import position_sizing as ps
    # $1M proposed against a $1k 5% cap → mult would be 0.001;
    # floor at 0.05.
    mult = ps.adv_size_multiplier(100_000, 10, 20_000)
    assert mult == 0.05  # floored


# ============================================================================
# Item C: drawdown taper
# ============================================================================

def test_drawdown_size_multiplier_no_drawdown():
    import position_sizing as ps
    assert ps.drawdown_size_multiplier(0) == 1.0


def test_drawdown_size_multiplier_at_threshold():
    """At halving_threshold (default 5%), multiplier is the midpoint
    between 1.0 and floor (0.5) → 0.75."""
    import position_sizing as ps
    mult = ps.drawdown_size_multiplier(5)
    assert abs(mult - 0.75) < 0.01


def test_drawdown_size_multiplier_at_double_threshold():
    """At 2× threshold, multiplier hits the floor."""
    import position_sizing as ps
    mult = ps.drawdown_size_multiplier(10)
    assert mult == 0.5


def test_drawdown_size_multiplier_floors():
    """Very large drawdown still floors at 0.5."""
    import position_sizing as ps
    mult = ps.drawdown_size_multiplier(50)
    assert mult == 0.5


def test_drawdown_size_multiplier_custom_floor():
    import position_sizing as ps
    mult = ps.drawdown_size_multiplier(30, floor=0.25)
    assert mult == 0.25


def test_drawdown_size_multiplier_bad_inputs():
    import position_sizing as ps
    assert ps.drawdown_size_multiplier(-5) == 1.0
    assert ps.drawdown_size_multiplier("bad") == 1.0
    assert ps.drawdown_size_multiplier(0, halving_threshold_pct=0) == 1.0


def test_compute_strategy_recent_drawdown_basic():
    import position_sizing as ps
    now = datetime.now(timezone.utc)
    journal = {"trades": [
        {"status": "closed", "strategy": "breakout", "pnl": 100,
         "exit_timestamp": (now - timedelta(days=10)).isoformat()},
        {"status": "closed", "strategy": "breakout", "pnl": -50,
         "exit_timestamp": (now - timedelta(days=5)).isoformat()},
    ]}
    out = ps.compute_strategy_recent_drawdown(journal, "breakout")
    # Cumulative: 100 (peak), 50 (trough). DD = (100-50)/100 = 50%.
    assert out["drawdown_pct"] == 50.0
    assert out["trade_count"] == 2


def test_compute_strategy_recent_drawdown_no_drawdown():
    import position_sizing as ps
    now = datetime.now(timezone.utc)
    journal = {"trades": [
        {"status": "closed", "strategy": "breakout", "pnl": 50,
         "exit_timestamp": (now - timedelta(days=5)).isoformat()},
        {"status": "closed", "strategy": "breakout", "pnl": 100,
         "exit_timestamp": (now - timedelta(days=2)).isoformat()},
    ]}
    out = ps.compute_strategy_recent_drawdown(journal, "breakout")
    assert out["drawdown_pct"] == 0.0


def test_compute_strategy_recent_drawdown_excludes_old_trades():
    import position_sizing as ps
    now = datetime.now(timezone.utc)
    journal = {"trades": [
        {"status": "closed", "strategy": "breakout", "pnl": 100,
         "exit_timestamp": (now - timedelta(days=60)).isoformat()},
        {"status": "closed", "strategy": "breakout", "pnl": -50,
         "exit_timestamp": (now - timedelta(days=2)).isoformat()},
    ]}
    out = ps.compute_strategy_recent_drawdown(
        journal, "breakout", lookback_days=30)
    # Old +100 excluded; only -50 in window. Peak = 0 → 0% DD.
    assert out["trade_count"] == 1


def test_compute_strategy_recent_drawdown_excludes_other_strategies():
    import position_sizing as ps
    now = datetime.now(timezone.utc)
    journal = {"trades": [
        {"status": "closed", "strategy": "breakout", "pnl": 100,
         "exit_timestamp": (now - timedelta(days=5)).isoformat()},
        {"status": "closed", "strategy": "wheel", "pnl": -200,
         "exit_timestamp": (now - timedelta(days=2)).isoformat()},
    ]}
    out = ps.compute_strategy_recent_drawdown(journal, "breakout")
    assert out["trade_count"] == 1
    assert out["drawdown_pct"] == 0.0


def test_compute_strategy_recent_drawdown_empty_journal():
    import position_sizing as ps
    out = ps.compute_strategy_recent_drawdown(None, "breakout")
    assert out["drawdown_pct"] == 0.0
    out2 = ps.compute_strategy_recent_drawdown({}, "breakout")
    assert out2["drawdown_pct"] == 0.0


# ============================================================================
# compute_full_size integration
# ============================================================================

def test_compute_full_size_applies_adv_cap():
    import position_sizing as ps
    # Base 100 shares × $100 = $10k. ADV $100k → 5% cap = $5k.
    # Expected mult ≈ 0.5 → 50 shares.
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="X",
        price=100, adv_dollar=100_000,
        enable_kelly=False, enable_correlation=False,
        enable_confluence=False, enable_drawdown_taper=False,
    )
    assert out["adv_multiplier"] < 1.0
    assert out["qty"] < 100


def test_compute_full_size_applies_drawdown_taper():
    import position_sizing as ps
    now = datetime.now(timezone.utc)
    journal = {"trades": [
        {"status": "closed", "strategy": "breakout", "pnl": 100,
         "exit_timestamp": (now - timedelta(days=10)).isoformat()},
        {"status": "closed", "strategy": "breakout", "pnl": -60,
         "exit_timestamp": (now - timedelta(days=5)).isoformat()},
    ]}
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="X",
        journal=journal,
        enable_kelly=False, enable_correlation=False,
        enable_confluence=False,
    )
    # 60% drawdown — well above 5% halving threshold → should be < 1.0
    assert out["drawdown_multiplier"] < 1.0


def test_compute_full_size_no_taper_with_no_journal():
    import position_sizing as ps
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="X",
        enable_kelly=False, enable_correlation=False,
        enable_confluence=False,
    )
    assert out["drawdown_multiplier"] == 1.0
    assert out["adv_multiplier"] == 1.0


def test_compute_full_size_includes_new_keys():
    import position_sizing as ps
    out = ps.compute_full_size(
        base_qty=10, strategy="breakout", symbol="X",
    )
    for k in ("adv_multiplier", "drawdown_multiplier", "drawdown_info"):
        assert k in out


def test_compute_full_size_can_disable_new_features():
    import position_sizing as ps
    out = ps.compute_full_size(
        base_qty=100, strategy="breakout", symbol="X",
        price=100, adv_dollar=100_000,
        enable_adv_cap=False, enable_drawdown_taper=False,
        enable_kelly=False, enable_correlation=False,
        enable_confluence=False,
    )
    assert out["adv_multiplier"] == 1.0
    assert out["drawdown_multiplier"] == 1.0
    assert out["qty"] == 100


# ============================================================================
# Wiring source-pin tests
# ============================================================================

def test_cloud_scheduler_wires_news_exit_monitor():
    src = (_HERE / "cloud_scheduler.py").read_text()
    assert "news_exit_monitor" in src
    assert "check_position_news" in src
    assert "news_exit_close_" in src


def test_cloud_scheduler_close_path_handles_news_flag():
    src = (_HERE / "cloud_scheduler.py").read_text()
    # The per-strategy close path must consume the news_exit_close flag.
    assert "bearish_news" in src
    assert "news-exit close" in src


def test_cloud_scheduler_passes_adv_to_compute_full_size():
    src = (_HERE / "cloud_scheduler.py").read_text()
    # Find a compute_full_size call and verify adv_dollar / price
    # are passed.
    idx = src.find("import position_sizing as _ps")
    assert idx > 0
    block = src[idx:idx + 2500]
    assert "adv_dollar" in block
    assert "price=" in block


def test_news_exit_monitor_pure_module():
    """Pure module — no top-level imports of cloud_scheduler / auth /
    settings; dependencies are injected."""
    src = (_HERE / "news_exit_monitor.py").read_text()
    # The score_fn fallback uses news_scanner.score_news_article via
    # late import — that's fine. Caller-injected fetch_news_fn is the
    # only other dependency.
    forbidden = ("import cloud_scheduler", "from cloud_scheduler",
                  "import auth\n", "from auth import")
    for f in forbidden:
        assert f not in src


def test_position_sizing_drawdown_helpers_exposed():
    """The new public functions are at module level so callers can
    import them directly."""
    import position_sizing as ps
    assert callable(ps.adv_size_multiplier)
    assert callable(ps.compute_strategy_recent_drawdown)
    assert callable(ps.drawdown_size_multiplier)
