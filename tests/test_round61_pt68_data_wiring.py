"""Round-61 pt.68 — three-item accuracy follow-up.

Item 1a: sector ETF fetcher builds sector_returns from injected fetcher
Item 1b: VWAP gate now receives prev_price + session_low for retest detection
Item 2:  bid-ask spread filter (new spread_filter.py)
Item 3:  4h MTF breakout confirmation (new fns in multi_timeframe.py)

Source-pin tests use lazy imports so a sibling-module collection
failure can't take down the whole suite (pt.52+ pattern).
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent


# ============================================================================
# Item 1a — sector_momentum.fetch_sector_returns
# ============================================================================

def test_fetch_sector_returns_translates_etf_bars_into_returns():
    import sector_momentum as sm
    def _fake_fetch(symbols, days):
        return {
            "XLE": [{"c": 80.0}, {"c": 70.0}],   # -12.5%
            "XLK": [{"c": 200.0}, {"c": 220.0}],  # +10%
        }
    out = sm.fetch_sector_returns(_fake_fetch, lookback_days=20)
    assert out["Energy"] == -12.5
    assert out["Technology"] == 10.0


def test_fetch_sector_returns_skips_etfs_with_too_few_bars():
    import sector_momentum as sm
    def _fake_fetch(symbols, days):
        return {
            "XLE": [{"c": 80.0}],          # only 1 bar — skipped
            "XLK": [{"c": 200.0}, {"c": 220.0}],
        }
    out = sm.fetch_sector_returns(_fake_fetch)
    assert "Energy" not in out
    assert out["Technology"] == 10.0


def test_fetch_sector_returns_handles_fetch_failure():
    import sector_momentum as sm
    def _boom(symbols, days):
        raise RuntimeError("bars endpoint down")
    assert sm.fetch_sector_returns(_boom) == {}


def test_fetch_sector_returns_handles_non_mapping_response():
    import sector_momentum as sm
    def _bad(symbols, days):
        return "not a dict"
    assert sm.fetch_sector_returns(_bad) == {}


def test_fetch_sector_returns_skips_bad_bar_data():
    import sector_momentum as sm
    def _fetch(symbols, days):
        return {
            "XLE": "bad",
            "XLF": [],
            "XLK": [{"c": 100}, {"c": 110}],
        }
    out = sm.fetch_sector_returns(_fetch)
    assert "Energy" not in out
    assert "Financial Services" not in out
    assert out["Technology"] == 10.0


def test_fetch_sector_returns_uses_subset_of_etfs():
    import sector_momentum as sm
    captured = {}
    def _fetch(symbols, days):
        captured["symbols"] = list(symbols)
        return {s: [{"c": 100}, {"c": 105}] for s in symbols}
    sm.fetch_sector_returns(_fetch, etfs=("XLE", "XLK"))
    assert set(captured["symbols"]) == {"XLE", "XLK"}


# ============================================================================
# Item 1b — VWAP retest data feed (wiring source-pin)
# ============================================================================

def test_cloud_scheduler_passes_session_low_to_vwap_gate():
    src = (_HERE / "cloud_scheduler.py").read_text()
    # Find the VWAP gate block.
    idx = src.find("VWAP-relative entry gate")
    assert idx > 0
    block = src[idx:idx + 3500]
    assert "session_low=" in block
    assert "prev_price=" in block


def test_cloud_scheduler_logs_retest_detection():
    src = (_HERE / "cloud_scheduler.py").read_text()
    idx = src.find("VWAP-relative entry gate")
    block = src[idx:idx + 3500]
    assert "is_retest" in block
    assert "retest" in block.lower()


def test_cloud_scheduler_extracts_session_low_from_bars():
    """Source-pin: the block must compute session_low from the
    bars it already fetched (no extra API calls)."""
    src = (_HERE / "cloud_scheduler.py").read_text()
    idx = src.find("VWAP-relative entry gate")
    block = src[idx:idx + 5000]
    assert 'b.get("l")' in block
    assert "_prev = _bars_today[-2]" in block


# ============================================================================
# Item 2 — spread_filter
# ============================================================================

def test_spread_filter_module_imports():
    import spread_filter as spf
    assert hasattr(spf, "compute_spread_pct")
    assert hasattr(spf, "is_spread_tight")
    assert hasattr(spf, "apply_spread_filter")


def test_compute_spread_pct_basic():
    import spread_filter as spf
    # bid=100, ask=101 → mid=100.5, spread = 1/100.5 = ~0.995%
    pct = spf.compute_spread_pct(100, 101)
    assert abs(pct - 0.995) < 0.01


def test_compute_spread_pct_zero():
    import spread_filter as spf
    assert spf.compute_spread_pct(100, 100) == 0.0


def test_compute_spread_pct_bad_inputs():
    import spread_filter as spf
    assert spf.compute_spread_pct(0, 100) is None
    assert spf.compute_spread_pct(100, 0) is None
    assert spf.compute_spread_pct(101, 100) is None  # crossed
    assert spf.compute_spread_pct("bad", 100) is None
    assert spf.compute_spread_pct(100, None) is None


def test_is_spread_tight_basic():
    import spread_filter as spf
    # 0.05% spread — well under 0.5% default
    assert spf.is_spread_tight(200.00, 200.10) is True


def test_is_spread_tight_rejects_wide():
    import spread_filter as spf
    # ~6% spread on a $5 stock
    assert spf.is_spread_tight(5.00, 5.30) is False


def test_is_spread_tight_fails_open_on_bad_data():
    import spread_filter as spf
    # Missing data → fail-open (allow)
    assert spf.is_spread_tight(0, 100) is True
    assert spf.is_spread_tight(None, None) is True


def test_apply_spread_filter_blocks_wide():
    import spread_filter as spf
    picks = [
        {"symbol": "TIGHT", "will_deploy": True},
        {"symbol": "WIDE", "will_deploy": True},
    ]
    quotes = {
        "TIGHT": {"bid": 200.00, "ask": 200.05},
        "WIDE":  {"bid": 5.00, "ask": 5.30},
    }
    spf.apply_spread_filter(picks, quotes)
    tight = next(p for p in picks if p["symbol"] == "TIGHT")
    wide = next(p for p in picks if p["symbol"] == "WIDE")
    assert tight.get("_wide_spread") is not True
    assert wide["_wide_spread"] is True
    assert wide["will_deploy"] is False
    assert "wide_spread" in wide["filter_reasons"]


def test_apply_spread_filter_records_spread_pct():
    import spread_filter as spf
    picks = [{"symbol": "X", "will_deploy": True}]
    quotes = {"X": {"bid": 100, "ask": 102}}
    spf.apply_spread_filter(picks, quotes)
    assert picks[0]["_spread_pct"] is not None
    assert picks[0]["_spread_pct"] > 0


def test_apply_spread_filter_handles_missing_quote():
    import spread_filter as spf
    picks = [{"symbol": "X", "will_deploy": True}]
    spf.apply_spread_filter(picks, {})  # no quote data → fail-open
    assert picks[0].get("_wide_spread") is not True
    assert picks[0]["will_deploy"] is True


def test_apply_spread_filter_custom_threshold():
    """A 0.3% spread passes default 0.5% but fails 0.2%."""
    import spread_filter as spf
    picks = [{"symbol": "X", "will_deploy": True}]
    quotes = {"X": {"bid": 100.0, "ask": 100.3}}
    spf.apply_spread_filter(picks, quotes, max_spread_pct=0.2)
    assert picks[0]["_wide_spread"] is True


def test_apply_spread_filter_handles_empty_picks():
    import spread_filter as spf
    assert spf.apply_spread_filter([], {}) == []
    assert spf.apply_spread_filter(None, {}) == []


# ============================================================================
# Item 3 — multi_timeframe 4h breakout confirmation
# ============================================================================

def test_compute_4h_high_basic():
    import multi_timeframe as mtf
    bars = [{"h": 100}, {"h": 105}, {"h": 102}]
    assert mtf.compute_4h_high(bars) == 105


def test_compute_4h_high_uses_lookback_window():
    import multi_timeframe as mtf
    # 25 bars; lookback=20 → ignores the first 5
    bars = [{"h": 200}] * 5 + [{"h": 100}] * 20
    assert mtf.compute_4h_high(bars, lookback_bars=20) == 100


def test_compute_4h_high_handles_empty():
    import multi_timeframe as mtf
    assert mtf.compute_4h_high([]) is None
    assert mtf.compute_4h_high(None) is None


def test_compute_4h_high_skips_malformed_bars():
    import multi_timeframe as mtf
    bars = [{"h": 100}, "bad", {"h": None}, {"h": "x"}, {"h": 110}]
    assert mtf.compute_4h_high(bars) == 110


def test_is_breakout_confirmed_above_high():
    import multi_timeframe as mtf
    bars = [{"h": 100}, {"h": 102}]
    assert mtf.is_breakout_confirmed(105, bars) is True


def test_is_breakout_confirmed_at_high_is_not_breakout():
    """Strictly above — at-or-below the 20-bar high doesn't qualify."""
    import multi_timeframe as mtf
    bars = [{"h": 100}, {"h": 102}]
    assert mtf.is_breakout_confirmed(102, bars) is False


def test_is_breakout_confirmed_fails_open_on_no_bars():
    import multi_timeframe as mtf
    assert mtf.is_breakout_confirmed(100, []) is True


def test_is_breakout_confirmed_with_buffer():
    import multi_timeframe as mtf
    bars = [{"h": 100}]
    # 0.5% buffer means must clear 100.5
    assert mtf.is_breakout_confirmed(100.4, bars, buffer_pct=0.5) is False
    assert mtf.is_breakout_confirmed(100.6, bars, buffer_pct=0.5) is True


def test_apply_mtf_breakout_confirmation_rejects_below_high():
    import multi_timeframe as mtf
    picks = [{
        "symbol": "AAPL", "best_strategy": "Breakout",
        "price": 95.0, "will_deploy": True,
    }]
    bars_4h = {"AAPL": [{"h": 100}, {"h": 102}]}
    mtf.apply_mtf_breakout_confirmation(picks, bars_4h)
    assert picks[0]["_mtf_rejected"] is True
    assert picks[0]["will_deploy"] is False
    assert "mtf_breakout_unconfirmed" in picks[0]["filter_reasons"]


def test_apply_mtf_breakout_confirmation_passes_above_high():
    import multi_timeframe as mtf
    picks = [{
        "symbol": "AAPL", "best_strategy": "Breakout",
        "price": 105.0, "will_deploy": True,
    }]
    bars_4h = {"AAPL": [{"h": 100}, {"h": 102}]}
    mtf.apply_mtf_breakout_confirmation(picks, bars_4h)
    assert picks[0].get("_mtf_rejected") is not True
    assert picks[0]["will_deploy"] is True


def test_apply_mtf_breakout_confirmation_skips_non_breakouts():
    import multi_timeframe as mtf
    picks = [{
        "symbol": "AAPL", "best_strategy": "Mean Reversion",
        "price": 95.0, "will_deploy": True,
    }]
    bars_4h = {"AAPL": [{"h": 100}, {"h": 102}]}
    mtf.apply_mtf_breakout_confirmation(picks, bars_4h)
    assert picks[0].get("_mtf_rejected") is not True
    assert picks[0]["will_deploy"] is True


def test_apply_mtf_breakout_confirmation_fails_open_on_missing_bars():
    import multi_timeframe as mtf
    picks = [{
        "symbol": "AAPL", "best_strategy": "Breakout",
        "price": 95.0, "will_deploy": True,
    }]
    mtf.apply_mtf_breakout_confirmation(picks, {})  # no bars
    assert picks[0].get("_mtf_rejected") is not True
    assert picks[0]["will_deploy"] is True


def test_apply_mtf_breakout_confirmation_records_4h_high():
    import multi_timeframe as mtf
    picks = [{
        "symbol": "AAPL", "best_strategy": "Breakout",
        "price": 95.0, "will_deploy": True,
    }]
    bars_4h = {"AAPL": [{"h": 100}, {"h": 105}, {"h": 102}]}
    mtf.apply_mtf_breakout_confirmation(picks, bars_4h)
    assert picks[0]["_mtf_4h_high"] == 105.0


def test_apply_mtf_breakout_confirmation_handles_empty():
    import multi_timeframe as mtf
    assert mtf.apply_mtf_breakout_confirmation([], {}) == []
    assert mtf.apply_mtf_breakout_confirmation(None, {}) == []


def test_fetch_4h_bars_for_breakouts_only_breakouts():
    import multi_timeframe as mtf
    captured = {}
    def _fetch(syms, tf, lim):
        captured["syms"] = list(syms)
        captured["tf"] = tf
        return {s: [{"h": 100}] for s in syms}
    picks = [
        {"symbol": "AAA", "best_strategy": "Breakout"},
        {"symbol": "BBB", "best_strategy": "Mean Reversion"},
        {"symbol": "CCC", "best_strategy": "Breakout"},
    ]
    out = mtf.fetch_4h_bars_for_breakouts(picks, _fetch)
    assert set(captured["syms"]) == {"AAA", "CCC"}
    assert captured["tf"] == "4Hour"
    assert "AAA" in out
    assert "CCC" in out


def test_fetch_4h_bars_for_breakouts_handles_empty():
    import multi_timeframe as mtf
    out = mtf.fetch_4h_bars_for_breakouts([], lambda s, t, l: {})
    assert out == {}


def test_fetch_4h_bars_for_breakouts_handles_fetch_failure():
    import multi_timeframe as mtf
    def _boom(syms, tf, lim):
        raise RuntimeError("network down")
    picks = [{"symbol": "AAA", "best_strategy": "Breakout"}]
    assert mtf.fetch_4h_bars_for_breakouts(picks, _boom) == {}


# ============================================================================
# Wiring source-pin tests
# ============================================================================

def test_update_dashboard_wires_sector_etf_fetch():
    src = (_HERE / "update_dashboard.py").read_text()
    assert "fetch_sector_returns" in src
    assert "fetch_bars_for_symbols" in src


def test_update_dashboard_wires_spread_filter():
    src = (_HERE / "update_dashboard.py").read_text()
    assert "spread_filter" in src
    assert "apply_spread_filter" in src
    assert "latestQuote" in src


def test_update_dashboard_wires_mtf_4h_gate():
    src = (_HERE / "update_dashboard.py").read_text()
    assert "fetch_4h_bars_for_breakouts" in src
    assert "apply_mtf_breakout_confirmation" in src
    assert "fetch_intraday_bars" in src


def test_update_dashboard_bridges_pt68_tags_to_filter_reasons():
    src = (_HERE / "update_dashboard.py").read_text()
    assert "_wide_spread" in src
    assert '"wide_spread"' in src
    assert "_mtf_rejected" in src
    assert '"mtf_breakout_unconfirmed"' in src
