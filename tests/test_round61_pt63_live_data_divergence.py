"""Round-61 pt.63 — live-data divergence monitor.

Pure-module tests + scheduler-hook source pin.
"""
from __future__ import annotations


# ============================================================================
# compute_divergence_pct
# ============================================================================

def test_divergence_pct_positive_drift():
    import live_data_monitor as ldm
    # bot saw 100, live is 102 → +2%
    assert abs(ldm.compute_divergence_pct(100.0, 102.0) - 2.0) < 1e-9


def test_divergence_pct_negative_drift():
    import live_data_monitor as ldm
    assert abs(ldm.compute_divergence_pct(100.0, 98.0) - (-2.0)) < 1e-9


def test_divergence_pct_zero_drift():
    import live_data_monitor as ldm
    assert ldm.compute_divergence_pct(100.0, 100.0) == 0.0


def test_divergence_pct_invalid_inputs():
    import live_data_monitor as ldm
    assert ldm.compute_divergence_pct("bad", 100.0) is None
    assert ldm.compute_divergence_pct(100.0, "bad") is None
    assert ldm.compute_divergence_pct(0, 100.0) is None
    assert ldm.compute_divergence_pct(-1.0, 100.0) is None
    assert ldm.compute_divergence_pct(None, None) is None


# ============================================================================
# classify_divergence — severity tiers
# ============================================================================

def test_classify_ok_within_threshold():
    """Δ < 2% → ok, not diverged."""
    import live_data_monitor as ldm
    out = ldm.classify_divergence(100.0, 101.5, threshold_pct=2.0)
    assert out["severity"] == "ok"
    assert out["diverged"] is False


def test_classify_warn_at_threshold():
    """Δ between threshold and 2× threshold → warn."""
    import live_data_monitor as ldm
    out = ldm.classify_divergence(100.0, 103.0, threshold_pct=2.0)
    assert out["severity"] == "warn"
    assert out["diverged"] is True


def test_classify_alert_above_2x_threshold():
    """Δ ≥ 2× threshold → alert."""
    import live_data_monitor as ldm
    out = ldm.classify_divergence(100.0, 105.0, threshold_pct=2.0)
    assert out["severity"] == "alert"
    assert out["diverged"] is True


def test_classify_negative_drift_alert():
    """Big negative drift also fires alert."""
    import live_data_monitor as ldm
    out = ldm.classify_divergence(100.0, 90.0, threshold_pct=2.0)
    assert out["severity"] == "alert"
    assert out["delta_pct"] < 0


def test_classify_returns_prices_in_dict():
    import live_data_monitor as ldm
    out = ldm.classify_divergence(100.0, 95.0)
    assert out["bot_price"] == 100.0
    assert out["live_price"] == 95.0


def test_classify_invalid_inputs_returns_ok():
    """Bad inputs → severity stays 'ok' so callers don't false-alert."""
    import live_data_monitor as ldm
    out = ldm.classify_divergence("bad", 100.0)
    assert out["severity"] == "ok"
    assert out["diverged"] is False
    assert out["delta_pct"] is None


def test_classify_configurable_threshold():
    """Tighter threshold catches smaller drift."""
    import live_data_monitor as ldm
    out = ldm.classify_divergence(100.0, 100.6, threshold_pct=0.5)
    assert out["severity"] == "warn"
    out2 = ldm.classify_divergence(100.0, 100.6, threshold_pct=2.0)
    assert out2["severity"] == "ok"


# ============================================================================
# check_position_divergence — full sweep
# ============================================================================

def test_check_position_divergence_no_positions():
    import live_data_monitor as ldm
    out = ldm.check_position_divergence([], lambda s: None)
    assert out["checked"] == 0
    assert out["divergent"] == 0
    assert out["alerts"] == []
    assert out["warnings"] == []


def test_check_position_divergence_clean_positions():
    """All positions within tolerance → no alerts."""
    import live_data_monitor as ldm
    positions = [
        {"symbol": "AAPL", "current_price": 100.0},
        {"symbol": "MSFT", "current_price": 200.0},
    ]

    def latest(sym):
        return {"AAPL": 100.5, "MSFT": 199.0}.get(sym)

    out = ldm.check_position_divergence(
        positions, latest, threshold_pct=2.0)
    assert out["checked"] == 2
    assert out["divergent"] == 0


def test_check_position_divergence_one_alert():
    """One position diverges 5% → alerts contains 1 entry."""
    import live_data_monitor as ldm
    positions = [
        {"symbol": "AAPL", "current_price": 100.0},
        {"symbol": "SOXL", "current_price": 20.0},
    ]

    def latest(sym):
        return {"AAPL": 100.5, "SOXL": 21.0}.get(sym)   # SOXL +5%

    out = ldm.check_position_divergence(
        positions, latest, threshold_pct=2.0)
    assert out["checked"] == 2
    assert out["divergent"] == 1
    assert len(out["alerts"]) == 1
    assert out["alerts"][0]["symbol"] == "SOXL"


def test_check_position_divergence_separates_warn_and_alert():
    """3% drift → warn; 5% drift → alert."""
    import live_data_monitor as ldm
    positions = [
        {"symbol": "AAPL", "current_price": 100.0},   # +3% → warn
        {"symbol": "MSFT", "current_price": 100.0},   # +5% → alert
    ]

    def latest(sym):
        return {"AAPL": 103.0, "MSFT": 105.0}.get(sym)

    out = ldm.check_position_divergence(
        positions, latest, threshold_pct=2.0)
    assert len(out["warnings"]) == 1
    assert len(out["alerts"]) == 1


def test_check_position_divergence_handles_lookup_failure():
    """latest_trade_fn raises → counted as error, not divergence."""
    import live_data_monitor as ldm
    positions = [{"symbol": "AAPL", "current_price": 100.0}]

    def latest(sym):
        raise RuntimeError("network down")

    out = ldm.check_position_divergence(positions, latest)
    assert out["errors"] == 1
    assert out["checked"] == 0


def test_check_position_divergence_handles_none_lookup():
    """latest_trade_fn returns None → counted as error."""
    import live_data_monitor as ldm
    positions = [{"symbol": "AAPL", "current_price": 100.0}]
    out = ldm.check_position_divergence(positions, lambda s: None)
    assert out["errors"] == 1


def test_check_position_divergence_falls_back_to_avg_entry():
    """If position has no current_price, use avg_entry_price."""
    import live_data_monitor as ldm
    positions = [
        {"symbol": "AAPL", "avg_entry_price": 100.0},
    ]
    out = ldm.check_position_divergence(
        positions, lambda s: 105.0, threshold_pct=2.0)
    assert out["checked"] == 1
    assert out["divergent"] == 1


def test_check_position_divergence_skips_positions_without_symbol():
    import live_data_monitor as ldm
    positions = [
        {"current_price": 100.0},                # no symbol
        {"symbol": "", "current_price": 100.0},  # empty
    ]
    out = ldm.check_position_divergence(
        positions, lambda s: 100.0)
    assert out["checked"] == 0


def test_check_position_divergence_skips_non_dict_entries():
    import live_data_monitor as ldm
    positions = [
        None, "not a dict", 42,
        {"symbol": "AAPL", "current_price": 100.0},
    ]
    out = ldm.check_position_divergence(
        positions, lambda s: 100.0)
    assert out["checked"] == 1


def test_check_position_divergence_uppercases_symbol():
    """Symbol returned in alerts should be uppercase consistently."""
    import live_data_monitor as ldm
    positions = [{"symbol": "aapl", "current_price": 100.0}]

    def latest(sym):
        # Return divergent price so we get an entry in alerts.
        return 110.0 if sym in ("aapl", "AAPL") else None

    out = ldm.check_position_divergence(
        positions, latest, threshold_pct=2.0)
    if out["alerts"]:
        assert out["alerts"][0]["symbol"] == "AAPL"


def test_check_position_divergence_tight_threshold():
    """Tighter threshold catches smaller drift."""
    import live_data_monitor as ldm
    positions = [{"symbol": "AAPL", "current_price": 100.0}]
    # 1% drift, threshold 0.5% → triggers alert (≥1.0% = 2× threshold).
    out = ldm.check_position_divergence(
        positions, lambda s: 101.0, threshold_pct=0.5)
    assert out["divergent"] == 1
