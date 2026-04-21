"""
Round-40 tests: pure-logic coverage for options_flow + options_analysis.

These tests do NOT mock the Alpaca HTTP layer — that was the blocker
that kept these modules under-covered. Instead we monkeypatch the
`api_get` helper to return pre-built contract payloads in the exact
shape Alpaca returns, and verify the scoring / signal / ratio logic.

Covered:
  * options_flow.analyze_options_flow
    - bullish signal when C/P ratio > 2 (high conf > 3)
    - bearish signal when C/P ratio < 0.5 (high conf < 0.33)
    - neutral in between
    - no_data when both sides are empty
    - handles empty response / error dicts
  * options_analysis.analyze_wheel_candidates
    - filters to target strike range
    - skips contracts with empty / malformed strike_price (round-19 bug)
    - ranks by the composite score (strike distance + DTE sweet spot)
    - handles empty chain
"""
from __future__ import annotations

import importlib
import sys as _sys

import pytest


@pytest.fixture
def _of(monkeypatch):
    if "options_flow" in _sys.modules:
        del _sys.modules["options_flow"]
    import options_flow
    importlib.reload(options_flow)
    return options_flow


@pytest.fixture
def _oa(monkeypatch):
    if "options_analysis" in _sys.modules:
        del _sys.modules["options_analysis"]
    import options_analysis
    importlib.reload(options_analysis)
    return options_analysis


# ---------- options_flow.analyze_options_flow ----------


def _stub_flow(of, monkeypatch, calls=None, puts=None):
    """Stub api_get to return call/put contract lists by URL shape."""
    def _fake_get(url, **kwargs):
        if "type=call" in url:
            return {"option_contracts": calls or []}
        if "type=put" in url:
            return {"option_contracts": puts or []}
        return {}
    monkeypatch.setattr(of, "api_get", _fake_get)


def test_analyze_options_flow_bullish_high_confidence(_of, monkeypatch):
    calls = [{"open_interest": "5000"}, {"open_interest": "3000"}]  # 8000
    puts = [{"open_interest": "2000"}]                               # 2000
    _stub_flow(_of, monkeypatch, calls=calls, puts=puts)
    result = _of.analyze_options_flow("TSLA")
    assert result["signal"] == "bullish"
    assert result["confidence"] == "high"
    assert result["call_put_ratio"] == 4.0
    assert result["call_open_interest"] == 8000
    assert result["put_open_interest"] == 2000


def test_analyze_options_flow_bullish_medium_confidence(_of, monkeypatch):
    # C/P ratio 2.5 — bullish but below 3.0 threshold
    calls = [{"open_interest": "2500"}]
    puts = [{"open_interest": "1000"}]
    _stub_flow(_of, monkeypatch, calls=calls, puts=puts)
    result = _of.analyze_options_flow("TSLA")
    assert result["signal"] == "bullish"
    assert result["confidence"] == "medium"


def test_analyze_options_flow_bearish_high_confidence(_of, monkeypatch):
    # C/P ratio 0.2 — bearish with high conf
    calls = [{"open_interest": "200"}]
    puts = [{"open_interest": "1000"}]
    _stub_flow(_of, monkeypatch, calls=calls, puts=puts)
    result = _of.analyze_options_flow("TSLA")
    assert result["signal"] == "bearish"
    assert result["confidence"] == "high"


def test_analyze_options_flow_neutral_ratio(_of, monkeypatch):
    # C/P ratio 1.5 — neutral
    calls = [{"open_interest": "1500"}]
    puts = [{"open_interest": "1000"}]
    _stub_flow(_of, monkeypatch, calls=calls, puts=puts)
    result = _of.analyze_options_flow("TSLA")
    assert result["signal"] == "neutral"


def test_analyze_options_flow_no_data_returns_sentinel(_of, monkeypatch):
    _stub_flow(_of, monkeypatch, calls=[], puts=[])
    result = _of.analyze_options_flow("TSLA")
    assert result["signal"] == "no_data"
    assert "error" in result


def test_analyze_options_flow_handles_missing_open_interest(_of, monkeypatch):
    """Contracts with missing / null open_interest shouldn't crash."""
    calls = [{"open_interest": None}, {"open_interest": "100"}]
    puts = [{}]  # missing key entirely
    _stub_flow(_of, monkeypatch, calls=calls, puts=puts)
    result = _of.analyze_options_flow("TSLA")
    assert result["call_open_interest"] == 100  # Nones treated as 0
    assert result["put_open_interest"] == 0


def test_analyze_options_flow_handles_api_error_response(_of, monkeypatch):
    """If api_get returns a non-dict error, the function must not
    crash and should return no_data rather than propagating."""
    monkeypatch.setattr(_of, "api_get",
                        lambda url, **kw: {"error": "401 Unauthorized"})
    result = _of.analyze_options_flow("TSLA")
    assert result["signal"] == "no_data"


# ---------- options_analysis.analyze_wheel_candidates ----------


def _stub_analysis(oa, monkeypatch, contracts=None, quote=None):
    """Stub api_get to return a contracts list + quote payload."""
    def _fake_get(url, **kw):
        if "/contracts" in url:
            return {"option_contracts": contracts or []}
        if "/snapshots/" in url or "/quotes/" in url:
            return quote or {}
        return {}
    monkeypatch.setattr(oa, "api_get", _fake_get)


def test_analyze_wheel_candidates_skips_empty_strike(_oa, monkeypatch):
    """Round-19 bug pin: Alpaca occasionally returns strike_price=""
    on newly-listed contracts. `float("")` would crash the loop."""
    contracts = [
        {"symbol": "TSLA260517P00170000", "strike_price": "",
         "expiration_date": "2026-05-17", "type": "put"},
        {"symbol": "TSLA260517P00180000", "strike_price": "180",
         "expiration_date": "2026-05-17", "type": "put"},
    ]
    _stub_analysis(_oa, monkeypatch, contracts=contracts)
    # Should NOT raise
    result = _oa.analyze_wheel_candidates("TSLA", current_price=200.0,
                                            strategy="put")
    # Only the valid contract makes it through
    assert result["available"] is True
    # The empty-strike contract was skipped
    syms = [c.get("contract_symbol") for c in result.get("candidates", [])]
    assert "TSLA260517P00170000" not in syms


def test_analyze_wheel_candidates_returns_unavailable_on_empty_chain(_oa, monkeypatch):
    _stub_analysis(_oa, monkeypatch, contracts=[])
    result = _oa.analyze_wheel_candidates("TSLA", current_price=200.0,
                                            strategy="put")
    assert result["available"] is False


def test_analyze_wheel_candidates_filters_to_target_strike_range(_oa, monkeypatch):
    """Puts 10% below current (target strike ~180 for current=200)
    should be preferred over deep OTM (strike 100)."""
    contracts = [
        {"symbol": "TSLA260517P00100000", "strike_price": "100",
         "expiration_date": "2026-05-17", "type": "put"},
        {"symbol": "TSLA260517P00180000", "strike_price": "180",
         "expiration_date": "2026-05-17", "type": "put"},
        {"symbol": "TSLA260517P00185000", "strike_price": "185",
         "expiration_date": "2026-05-17", "type": "put"},
    ]
    _stub_analysis(_oa, monkeypatch, contracts=contracts)
    result = _oa.analyze_wheel_candidates("TSLA", current_price=200.0,
                                            strategy="put")
    assert result["available"] is True
    # Best candidate should be the closest-to-target strike, 180 or 185
    best = result["best"]
    assert best is not None
    # Strike within 10% of current
    assert 170 <= best["strike"] <= 200


def test_analyze_wheel_candidates_ignores_malformed_strike_types(_oa, monkeypatch):
    """None / dict / weird shapes in strike_price shouldn't crash."""
    contracts = [
        {"symbol": "X1", "strike_price": None,
         "expiration_date": "2026-05-17", "type": "put"},
        {"symbol": "X2", "strike_price": {"whoops": "object"},
         "expiration_date": "2026-05-17", "type": "put"},
        {"symbol": "X3", "strike_price": "200",
         "expiration_date": "2026-05-17", "type": "put"},
    ]
    _stub_analysis(_oa, monkeypatch, contracts=contracts)
    # Should not raise
    result = _oa.analyze_wheel_candidates("TSLA", current_price=200.0,
                                            strategy="put")
    # Only the valid strike made it through
    syms = [c.get("contract_symbol") for c in result.get("candidates", [])]
    assert "X1" not in syms
    assert "X2" not in syms


def test_analyze_wheel_candidates_returns_shape_contract(_oa, monkeypatch):
    """Response must contain the keys the dashboard expects."""
    contracts = [
        {"symbol": "TSLA260517P00180000", "strike_price": "180",
         "expiration_date": "2026-05-17", "type": "put"},
    ]
    _stub_analysis(_oa, monkeypatch, contracts=contracts)
    result = _oa.analyze_wheel_candidates("TSLA", current_price=200.0,
                                            strategy="put")
    # Contract surface the dashboard / screener relies on
    for key in ("symbol", "current_price", "type", "available",
                "total_contracts", "scored_candidates", "candidates",
                "best", "target_strike"):
        assert key in result, f"missing key {key}"
