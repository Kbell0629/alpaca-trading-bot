"""Round-61 pt.66 — four-item accuracy refinement batch.

Item 3: sector-momentum filter
Item 4: VWAP retest pattern
Item 5: volume-confirmed gap penalty
Item 6: post-event momentum lean-in

Tests use the lazy-import pattern (pt.52+) so failures in one module
don't take the suite down at collection time.
"""
from __future__ import annotations

import pathlib
from datetime import date, datetime


# ============================================================================
# Item 3: sector_momentum
# ============================================================================

def test_sector_momentum_module_imports():
    import sector_momentum as sm
    assert hasattr(sm, "apply_sector_momentum_filter")
    assert hasattr(sm, "is_sector_in_downtrend")
    assert hasattr(sm, "compute_pct_return")


def test_compute_pct_return_basic():
    import sector_momentum as sm
    assert abs(sm.compute_pct_return(100.0, 110.0) - 10.0) < 1e-6
    assert abs(sm.compute_pct_return(100.0, 90.0) - (-10.0)) < 1e-6


def test_compute_pct_return_bad_inputs():
    import sector_momentum as sm
    assert sm.compute_pct_return(0, 100) is None
    assert sm.compute_pct_return(-10, 100) is None
    assert sm.compute_pct_return("bad", 100) is None
    assert sm.compute_pct_return(100, None) is None


def test_build_sector_returns_translates_etfs():
    import sector_momentum as sm
    out = sm.build_sector_returns({
        "XLE": {"start": 80.0, "end": 70.0},  # -12.5%
        "XLK": {"start": 200.0, "end": 220.0},  # +10%
    })
    assert out["Energy"] == -12.5
    assert out["Technology"] == 10.0


def test_build_sector_returns_skips_unknown_etfs():
    import sector_momentum as sm
    out = sm.build_sector_returns({
        "XLE": {"start": 100, "end": 90},
        "XYZ": {"start": 100, "end": 110},  # unknown ETF
    })
    assert "Energy" in out
    assert "XYZ" not in out
    assert len(out) == 1


def test_build_sector_returns_skips_bad_data():
    import sector_momentum as sm
    out = sm.build_sector_returns({
        "XLE": {"start": 0, "end": 90},  # zero start
        "XLK": "bad",
    })
    assert out == {}


def test_is_sector_in_downtrend_basic():
    import sector_momentum as sm
    rets = {"Energy": -12.0, "Technology": 5.0}
    assert sm.is_sector_in_downtrend("Energy", rets) is True
    assert sm.is_sector_in_downtrend("Technology", rets) is False


def test_is_sector_in_downtrend_unknown_sector_fails_open():
    import sector_momentum as sm
    rets = {"Energy": -15.0}
    assert sm.is_sector_in_downtrend("Mystery", rets) is False
    assert sm.is_sector_in_downtrend(None, rets) is False
    assert sm.is_sector_in_downtrend("Energy", None) is False


def test_is_sector_in_downtrend_custom_threshold():
    import sector_momentum as sm
    rets = {"Energy": -7.0}
    assert sm.is_sector_in_downtrend("Energy", rets,
                                       threshold_pct=-5.0) is True
    assert sm.is_sector_in_downtrend("Energy", rets,
                                       threshold_pct=-10.0) is False


def test_apply_sector_momentum_filter_blocks_long_in_downtrend():
    import sector_momentum as sm
    picks = [
        {"symbol": "XOM", "sector": "Energy",
         "best_strategy": "breakout", "will_deploy": True},
    ]
    sm.apply_sector_momentum_filter(picks, {"Energy": -15.0})
    assert picks[0]["_sector_downtrend"] is True
    assert picks[0]["will_deploy"] is False
    assert "sector_downtrend" in picks[0]["filter_reasons"]


def test_apply_sector_momentum_filter_leaves_strong_sectors():
    import sector_momentum as sm
    picks = [
        {"symbol": "AAPL", "sector": "Technology",
         "best_strategy": "breakout", "will_deploy": True},
    ]
    sm.apply_sector_momentum_filter(picks, {"Technology": 8.0})
    assert picks[0].get("_sector_downtrend") is not True
    assert picks[0]["will_deploy"] is True


def test_apply_sector_momentum_filter_skips_shorts_in_downtrend():
    """Shorting an already-falling sector is a GOOD setup."""
    import sector_momentum as sm
    picks = [
        {"symbol": "XOM", "sector": "Energy",
         "best_strategy": "short_sell", "will_deploy": True},
    ]
    sm.apply_sector_momentum_filter(picks, {"Energy": -20.0})
    assert picks[0].get("_sector_downtrend") is not True
    assert picks[0]["will_deploy"] is True


def test_apply_sector_momentum_filter_handles_empty_inputs():
    import sector_momentum as sm
    assert sm.apply_sector_momentum_filter([], {}) == []
    assert sm.apply_sector_momentum_filter(None, {}) == []
    # Empty sector_returns should leave picks alone.
    picks = [{"symbol": "X", "sector": "Energy", "will_deploy": True}]
    sm.apply_sector_momentum_filter(picks, {})
    assert picks[0]["will_deploy"] is True


def test_apply_sector_momentum_filter_records_return_pct():
    import sector_momentum as sm
    picks = [{"symbol": "XOM", "sector": "Energy", "will_deploy": True}]
    sm.apply_sector_momentum_filter(picks, {"Energy": -12.5})
    assert picks[0]["_sector_return_pct"] == -12.5


def test_explain_sector_returns_human_readable():
    import sector_momentum as sm
    s = sm.explain_sector_returns({"Energy": -12.0, "Technology": 8.0,
                                     "Healthcare": 2.0})
    assert "Energy" in s
    assert "Technology" in s
    assert "%" in s


def test_explain_sector_returns_empty():
    import sector_momentum as sm
    assert sm.explain_sector_returns({}) == "(no sector data)"


def test_apply_sector_momentum_filter_appends_to_existing_reasons():
    import sector_momentum as sm
    picks = [{"symbol": "XOM", "sector": "Energy",
              "filter_reasons": ["chase_block"], "will_deploy": True}]
    sm.apply_sector_momentum_filter(picks, {"Energy": -15.0})
    assert "chase_block" in picks[0]["filter_reasons"]
    assert "sector_downtrend" in picks[0]["filter_reasons"]


# ============================================================================
# Item 4: VWAP retest pattern
# ============================================================================

def test_detect_vwap_retest_cross_up():
    import vwap_gate as vg
    # prev was below VWAP, now at-or-just-above → retest
    assert vg.detect_vwap_retest(
        price=100.05, vwap=100.0, prev_price=99.5) is True


def test_detect_vwap_retest_no_cross():
    import vwap_gate as vg
    # prev was already above → not a retest, just continuation
    assert vg.detect_vwap_retest(
        price=101.0, vwap=100.0, prev_price=100.5) is False


def test_detect_vwap_retest_via_session_low():
    import vwap_gate as vg
    # Session traded below VWAP earlier → counts as retest
    assert vg.detect_vwap_retest(
        price=100.1, vwap=100.0, session_low=98.5) is True


def test_detect_vwap_retest_chased_above_not_retest():
    """Far above VWAP is a chase even if it crossed up earlier."""
    import vwap_gate as vg
    assert vg.detect_vwap_retest(
        price=102.0, vwap=100.0, prev_price=99.0) is False


def test_detect_vwap_retest_far_below_not_retest():
    """Way below VWAP is not a retest, that's just weak price."""
    import vwap_gate as vg
    assert vg.detect_vwap_retest(
        price=99.0, vwap=100.0, prev_price=98.0) is False


def test_detect_vwap_retest_bad_inputs():
    import vwap_gate as vg
    assert vg.detect_vwap_retest(price="bad", vwap=100.0) is False
    assert vg.detect_vwap_retest(price=100, vwap=0) is False
    assert vg.detect_vwap_retest(price=100, vwap=None) is False


def test_evaluate_vwap_gate_retest_allowed():
    import vwap_gate as vg
    # 0.3% above VWAP — within tolerance — and a retest
    res = vg.evaluate_vwap_gate(
        strategy="breakout", price=100.3, vwap=100.0,
        prev_price=99.0)
    assert res["allowed"] is True
    assert res["is_retest"] is True
    assert "retest" in res["reason"]


def test_evaluate_vwap_gate_chase_blocked():
    """Non-retest, above tolerance → still blocked."""
    import vwap_gate as vg
    res = vg.evaluate_vwap_gate(
        strategy="breakout", price=102.0, vwap=100.0,
        prev_price=101.5)
    assert res["allowed"] is False
    assert res["is_retest"] is False
    assert "above_vwap" in res["reason"]


def test_evaluate_vwap_gate_includes_is_retest_key():
    import vwap_gate as vg
    res = vg.evaluate_vwap_gate(
        strategy="breakout", price=100.0, vwap=100.0)
    assert "is_retest" in res


def test_evaluate_vwap_gate_non_breakout_unchanged():
    import vwap_gate as vg
    res = vg.evaluate_vwap_gate(
        strategy="mean_reversion", price=200.0, vwap=100.0,
        prev_price=99.0)
    assert res["allowed"] is True
    assert res["reason"] == "not_breakout"


# ============================================================================
# Item 5: volume-confirmed gap penalty
# ============================================================================

def test_apply_gap_penalty_applies_when_volume_low():
    import screener_core as sc
    picks = [{
        "symbol": "X", "best_score": 100, "daily_change": 5.0,
        "relative_volume": 1.2,
    }]
    sc.apply_gap_penalty(picks)
    assert picks[0].get("_gap_penalty_applied") is True
    assert picks[0]["best_score"] == 85.0  # 100 * 0.85


def test_apply_gap_penalty_skipped_when_volume_confirms():
    """Volume >= 2.0× → gap is institutional, not a chase."""
    import screener_core as sc
    picks = [{
        "symbol": "X", "best_score": 100, "daily_change": 5.0,
        "relative_volume": 2.5,
    }]
    sc.apply_gap_penalty(picks)
    assert picks[0].get("_gap_penalty_applied") is not True
    assert picks[0].get("_gap_volume_confirmed") is True
    assert picks[0]["best_score"] == 100  # unchanged


def test_apply_gap_penalty_volume_confirm_threshold_at_2x():
    """At exactly 2.0× → confirmed. Just below → penalized."""
    import screener_core as sc
    picks = [
        {"symbol": "A", "best_score": 100, "daily_change": 5.0,
         "relative_volume": 2.0},
        {"symbol": "B", "best_score": 100, "daily_change": 5.0,
         "relative_volume": 1.99},
    ]
    sc.apply_gap_penalty(picks)
    a = next(p for p in picks if p["symbol"] == "A")
    b = next(p for p in picks if p["symbol"] == "B")
    assert a.get("_gap_volume_confirmed") is True
    assert b.get("_gap_penalty_applied") is True


def test_apply_gap_penalty_no_volume_data_falls_through():
    """Missing relative_volume → behaves like pre-pt.66 (penalize)."""
    import screener_core as sc
    picks = [{"symbol": "X", "best_score": 100, "daily_change": 5.0}]
    sc.apply_gap_penalty(picks)
    assert picks[0].get("_gap_penalty_applied") is True


def test_apply_gap_penalty_records_relative_volume():
    import screener_core as sc
    picks = [{
        "symbol": "X", "best_score": 100, "daily_change": 5.0,
        "relative_volume": 3.5,
    }]
    sc.apply_gap_penalty(picks)
    assert picks[0]["_gap_relative_volume"] == 3.5


def test_apply_gap_penalty_outside_band_unaffected_by_volume_confirm():
    """daily_change < threshold → no penalty regardless of volume."""
    import screener_core as sc
    picks = [{
        "symbol": "X", "best_score": 100, "daily_change": 1.0,
        "relative_volume": 5.0,
    }]
    sc.apply_gap_penalty(picks)
    assert picks[0].get("_gap_penalty_applied") is not True
    assert picks[0].get("_gap_volume_confirmed") is not True
    assert picks[0]["best_score"] == 100


# ============================================================================
# Item 6: post-event momentum lean-in
# ============================================================================

def test_post_event_module_imports():
    import post_event_momentum as pem
    assert hasattr(pem, "post_event_boost")
    assert hasattr(pem, "find_recent_event")
    assert hasattr(pem, "adjust_score_threshold")


def test_post_event_boost_no_recent_event():
    import post_event_momentum as pem
    # 2026-04-23 was Thursday with no FOMC/CPI/NFP/PCE in range.
    mult, label = pem.post_event_boost(date(2026, 4, 23),
                                          lookback_days=2)
    assert mult == 1.0
    assert label is None


def test_post_event_boost_day_after_fomc():
    import post_event_momentum as pem
    # 2026-04-29 is FOMC; day after is 2026-04-30 (Thursday).
    mult, label = pem.post_event_boost(date(2026, 4, 30))
    assert mult > 1.0
    assert label is not None
    assert "FOMC" in label


def test_post_event_boost_fomc_decays_by_day_3():
    """Day 3 boost should be smaller than day 1."""
    import post_event_momentum as pem
    fomc = date(2026, 4, 29)
    # day 1 = 2026-04-30, day 2 = 2026-05-01, day 3 = 2026-05-04
    # (skipping weekend)
    m1, _ = pem.post_event_boost(fomc + __import__("datetime").timedelta(days=1))
    m3, _ = pem.post_event_boost(date(2026, 5, 4))
    assert m1 > m3


def test_post_event_boost_accepts_string_date():
    import post_event_momentum as pem
    mult, label = pem.post_event_boost("2026-04-30")
    assert mult > 1.0


def test_post_event_boost_accepts_datetime():
    import post_event_momentum as pem
    mult, label = pem.post_event_boost(datetime(2026, 4, 30, 10, 0))
    assert mult > 1.0


def test_post_event_boost_invalid_date_returns_neutral():
    import post_event_momentum as pem
    mult, label = pem.post_event_boost("not-a-date")
    assert mult == 1.0
    assert label is None


def test_post_event_boost_far_past_event_no_boost():
    import post_event_momentum as pem
    # 30 days after FOMC — way out of lookback window
    mult, label = pem.post_event_boost(date(2026, 5, 29))
    assert mult == 1.0


def test_post_event_boost_label_format():
    import post_event_momentum as pem
    _mult, label = pem.post_event_boost(date(2026, 4, 30))
    # Format is "post-FOMC day N"
    assert label.startswith("post-")
    assert "day" in label


def test_find_recent_event_within_window():
    import post_event_momentum as pem
    found = pem.find_recent_event(date(2026, 4, 30))
    assert found is not None
    label, event_date, day_after = found
    assert label == "FOMC"
    assert event_date == date(2026, 4, 29)
    assert day_after == 1


def test_find_recent_event_none():
    import post_event_momentum as pem
    # Mid-April week with no events in range
    found = pem.find_recent_event(date(2026, 4, 23), lookback_days=2)
    assert found is None


def test_adjust_score_threshold_lowers_bar():
    import post_event_momentum as pem
    adjusted, label = pem.adjust_score_threshold(100.0, date(2026, 4, 30))
    assert adjusted < 100.0
    assert label is not None


def test_adjust_score_threshold_unchanged_when_no_event():
    import post_event_momentum as pem
    adjusted, label = pem.adjust_score_threshold(
        100.0, date(2026, 4, 23), lookback_days=2)
    assert adjusted == 100.0
    assert label is None


def test_post_event_boost_custom_schedule():
    """Caller can override the schedule for testing."""
    import post_event_momentum as pem
    custom = {"FOMC": {1: 1.50}}
    mult, _label = pem.post_event_boost(date(2026, 4, 30),
                                          schedule=custom)
    assert mult == 1.50


# ============================================================================
# Source-pin: confirm pt.66 changes are visible in the actual source.
# ============================================================================

_HERE = pathlib.Path(__file__).resolve().parent.parent


def test_screener_core_has_volume_confirm_constant():
    src = (_HERE / "screener_core.py").read_text()
    assert "GAP_PENALTY_VOLUME_CONFIRM_X" in src


def test_screener_core_has_volume_confirm_logic():
    src = (_HERE / "screener_core.py").read_text()
    assert "_gap_volume_confirmed" in src


def test_vwap_gate_has_retest_detector():
    src = (_HERE / "vwap_gate.py").read_text()
    assert "detect_vwap_retest" in src


def test_vwap_gate_returns_is_retest_field():
    src = (_HERE / "vwap_gate.py").read_text()
    assert '"is_retest"' in src


def test_sector_momentum_module_exists():
    p = _HERE / "sector_momentum.py"
    assert p.exists()
    assert "apply_sector_momentum_filter" in p.read_text()


def test_post_event_momentum_module_exists():
    p = _HERE / "post_event_momentum.py"
    assert p.exists()
    assert "post_event_boost" in p.read_text()


def test_cloud_scheduler_wires_post_event_momentum():
    src = (_HERE / "cloud_scheduler.py").read_text()
    assert "post_event_momentum" in src
    assert "POST_EVENT_BOOST" in src


def test_update_dashboard_wires_sector_momentum():
    src = (_HERE / "update_dashboard.py").read_text()
    assert "sector_momentum" in src
    assert "apply_sector_momentum_filter" in src


def test_update_dashboard_bridges_sector_downtrend_to_reasons():
    src = (_HERE / "update_dashboard.py").read_text()
    assert "_sector_downtrend" in src
    assert "sector_downtrend" in src
