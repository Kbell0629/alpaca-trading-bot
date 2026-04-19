"""
Round-19: options_flow + options_analysis pure-logic tests.

Both modules are mostly network-bound but have testable scoring logic:

  options_flow.analyze_options_flow — signal / confidence categorisation
    from call:put open-interest ratio.

  options_flow.scan_options_flow — filtering + sorting by deviation.

  options_analysis.analyze_wheel_candidates — DTE scoring, OTM filter,
    strike-distance scoring, sort-by-score.

Network calls (api_get, get_options_chain) are monkeypatched to return
canned fixtures so we test the logic, not Alpaca.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta


# ====================== options_flow ======================


def _mock_options_api(monkeypatch, call_oi, put_oi, call_count=5, put_count=5):
    """Return canned call/put contracts with the given aggregate OI."""
    import options_flow
    call_contracts = [{"open_interest": str(call_oi // call_count)}
                      for _ in range(call_count)] if call_count else []
    put_contracts = [{"open_interest": str(put_oi // put_count)}
                     for _ in range(put_count)] if put_count else []

    def _fake_api_get(url, timeout=15):
        if "type=call" in url:
            return {"option_contracts": call_contracts}
        if "type=put" in url:
            return {"option_contracts": put_contracts}
        return {}
    monkeypatch.setattr(options_flow, "api_get", _fake_api_get)


def test_options_flow_bullish_signal_on_high_call_put_ratio(monkeypatch):
    _mock_options_api(monkeypatch, call_oi=10000, put_oi=2500)
    from options_flow import analyze_options_flow
    result = analyze_options_flow("AAPL")
    assert result["signal"] == "bullish"
    assert result["call_put_ratio"] >= 2.0


def test_options_flow_bearish_signal_on_low_ratio(monkeypatch):
    _mock_options_api(monkeypatch, call_oi=1000, put_oi=5000)
    from options_flow import analyze_options_flow
    result = analyze_options_flow("TSLA")
    assert result["signal"] == "bearish"
    assert result["call_put_ratio"] < 0.5


def test_options_flow_neutral_signal(monkeypatch):
    _mock_options_api(monkeypatch, call_oi=3000, put_oi=3000)
    from options_flow import analyze_options_flow
    result = analyze_options_flow("SPY")
    assert result["signal"] == "neutral"
    assert result["confidence"] == "low"


def test_options_flow_high_confidence_at_extreme_ratios(monkeypatch):
    """call:put > 3.0 → high-confidence bullish."""
    _mock_options_api(monkeypatch, call_oi=20000, put_oi=5000)
    from options_flow import analyze_options_flow
    result = analyze_options_flow("NVDA")
    assert result["signal"] == "bullish"
    assert result["confidence"] == "high"


def test_options_flow_no_data_on_zero_oi(monkeypatch):
    _mock_options_api(monkeypatch, call_oi=0, put_oi=0)
    from options_flow import analyze_options_flow
    result = analyze_options_flow("NODATA")
    assert result["signal"] == "no_data"
    assert "error" in result


def test_options_flow_divide_by_zero_safety(monkeypatch):
    """Zero puts + some calls → call_put_ratio handled safely."""
    _mock_options_api(monkeypatch, call_oi=5000, put_oi=0, put_count=0)
    from options_flow import analyze_options_flow
    result = analyze_options_flow("ALLCALLS")
    # Should not raise division error; returns a signal dict
    assert "signal" in result
    assert "call_put_ratio" in result


def test_scan_options_flow_filters_low_confidence(monkeypatch):
    """scan_options_flow should skip neutral / low-confidence results."""
    import options_flow
    def _canned(sym):
        return {
            "AAPL": {"symbol": "AAPL", "signal": "bullish",
                     "confidence": "high", "call_put_ratio": 3.5},
            "TSLA": {"symbol": "TSLA", "signal": "neutral",
                     "confidence": "low", "call_put_ratio": 1.0},
            "NVDA": {"symbol": "NVDA", "signal": "bearish",
                     "confidence": "medium", "call_put_ratio": 0.4},
        }.get(sym, {"signal": "no_data"})
    monkeypatch.setattr(options_flow, "analyze_options_flow", _canned)
    out = options_flow.scan_options_flow(["AAPL", "TSLA", "NVDA", "MISSING"])
    assert len(out) == 2  # AAPL + NVDA, TSLA filtered out
    syms = {r["symbol"] for r in out}
    assert syms == {"AAPL", "NVDA"}


def test_scan_options_flow_sorts_by_deviation(monkeypatch):
    """Results ordered by |ratio - 1.0| descending."""
    import options_flow
    def _canned(sym):
        return {
            "A": {"symbol": "A", "signal": "bullish", "confidence": "high",
                  "call_put_ratio": 3.5},   # deviation 2.5
            "B": {"symbol": "B", "signal": "bullish", "confidence": "medium",
                  "call_put_ratio": 2.2},   # deviation 1.2
            "C": {"symbol": "C", "signal": "bearish", "confidence": "high",
                  "call_put_ratio": 0.2},   # deviation 0.8
        }.get(sym, {"signal": "no_data"})
    monkeypatch.setattr(options_flow, "analyze_options_flow", _canned)
    out = options_flow.scan_options_flow(["A", "B", "C"])
    # Top result should be A (biggest deviation)
    assert out[0]["symbol"] == "A"


# ====================== options_analysis ======================


def test_analyze_wheel_candidates_put_scores_near_target(monkeypatch):
    """Put strike exactly at target (10% OTM) should score highest."""
    import options_analysis
    # Current price 100 → put target 90 (10% below)
    today = date.today()
    contracts = [
        {"strike_price": "90", "expiration_date":
         (today + timedelta(days=21)).isoformat(), "symbol": "TEST_AT_TARGET"},
        {"strike_price": "80", "expiration_date":
         (today + timedelta(days=21)).isoformat(), "symbol": "TEST_FAR"},
        {"strike_price": "95", "expiration_date":
         (today + timedelta(days=21)).isoformat(), "symbol": "TEST_NEAR"},
    ]
    monkeypatch.setattr(options_analysis, "get_options_chain",
                        lambda *a, **kw: contracts)
    result = options_analysis.analyze_wheel_candidates("TEST", 100.0, "put")
    assert result["available"] is True
    assert result["target_strike"] == 90.0
    # Best candidate should be the one AT target
    assert result["best"]["contract_symbol"] == "TEST_AT_TARGET"
    # All are OTM relative to 100
    assert all(c["otm"] for c in result["candidates"])


def test_analyze_wheel_candidates_call_uses_above_current(monkeypatch):
    """Call strategy → target = current*1.10, OTM = strike > current."""
    import options_analysis
    today = date.today()
    contracts = [
        {"strike_price": "110", "expiration_date":
         (today + timedelta(days=21)).isoformat(), "symbol": "ATM_CALL"},
    ]
    monkeypatch.setattr(options_analysis, "get_options_chain",
                        lambda *a, **kw: contracts)
    result = options_analysis.analyze_wheel_candidates("TEST", 100.0, "call")
    assert result["target_strike"] == 110.0
    assert result["best"]["otm"] is True


def test_analyze_wheel_candidates_no_options_available(monkeypatch):
    """Empty chain → available=False, no crash."""
    import options_analysis
    monkeypatch.setattr(options_analysis, "get_options_chain",
                        lambda *a, **kw: [])
    result = options_analysis.analyze_wheel_candidates("NOPE", 50.0, "put")
    assert result["available"] is False
    assert result["best"] is None
    assert result["candidates"] == []


def test_analyze_wheel_candidates_dte_scoring_peaks_at_21(monkeypatch):
    """DTE score should peak at 21 days — a 21-DTE contract should beat
    a 45-DTE one at the same strike."""
    import options_analysis
    today = date.today()
    contracts = [
        {"strike_price": "90", "expiration_date":
         (today + timedelta(days=21)).isoformat(), "symbol": "TWENTYONE"},
        {"strike_price": "90", "expiration_date":
         (today + timedelta(days=45)).isoformat(), "symbol": "FORTYFIVE"},
    ]
    monkeypatch.setattr(options_analysis, "get_options_chain",
                        lambda *a, **kw: contracts)
    result = options_analysis.analyze_wheel_candidates("X", 100.0, "put")
    # 21-DTE should score higher than 45-DTE
    score_21 = next(c["score"] for c in result["candidates"]
                    if c["contract_symbol"] == "TWENTYONE")
    score_45 = next(c["score"] for c in result["candidates"]
                    if c["contract_symbol"] == "FORTYFIVE")
    assert score_21 > score_45


def test_analyze_wheel_candidates_handles_malformed_contracts(monkeypatch):
    """Contracts with missing strike / expiration should be skipped."""
    import options_analysis
    today = date.today()
    contracts = [
        {"strike_price": "", "expiration_date": "", "symbol": "BAD"},
        {"strike_price": "90", "expiration_date":
         (today + timedelta(days=21)).isoformat(), "symbol": "GOOD"},
    ]
    monkeypatch.setattr(options_analysis, "get_options_chain",
                        lambda *a, **kw: contracts)
    result = options_analysis.analyze_wheel_candidates("X", 100.0, "put")
    assert result["best"]["contract_symbol"] == "GOOD"
    # BAD was skipped
    assert all(c["contract_symbol"] != "BAD" for c in result["candidates"])
