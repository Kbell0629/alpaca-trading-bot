"""Round-61 pt.51 — regression tests for pt.50 closed-market order
routing helpers in handlers/actions_mixin.py.

Pt.50 shipped the routing without tests. Pt.51 adds them so the
next pre-market regression doesn't slip past CI silently.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from handlers import actions_mixin as am


# ============================================================================
# Stub handler — minimal interface that the routing helpers need.
# ============================================================================

class _StubHandler:
    """Minimal handler test double. Captures every API call so tests can
    assert what was sent."""

    def __init__(self, *, clock_response=None, position_response=None,
                  trade_response=None, raise_on=None):
        self.user_api_endpoint = "https://paper-api.alpaca.markets/v2"
        self.user_data_endpoint = "https://data.alpaca.markets/v2"
        self.user_api_key = "PKTEST"
        self.user_api_secret = "secret"
        self.session_mode = "paper"
        # Pre-canned responses keyed by URL substring.
        self._responses = {
            "/clock": clock_response if clock_response is not None
                       else {"is_open": True},
            "/positions/": position_response,
            "/trades/latest": trade_response,
        }
        self._raise_on = set(raise_on or ())
        self.api_calls = []

    def user_api_get(self, url):
        self.api_calls.append(("GET", url))
        for substr, resp in self._responses.items():
            if substr in url:
                if substr in self._raise_on:
                    raise RuntimeError(f"forced fail on {substr}")
                return resp
        return None


# ============================================================================
# _market_session
# ============================================================================

def test_market_session_rth_when_clock_says_open():
    h = _StubHandler(clock_response={"is_open": True})
    assert am._market_session(h) == "rth"


def test_market_session_rth_when_clock_probe_fails():
    """Fail-open: probe error → assume RTH so we don't accidentally
    re-route during normal hours."""
    h = _StubHandler(raise_on=("/clock",))
    assert am._market_session(h) == "rth"


def test_market_session_premarket_window(monkeypatch):
    """is_open=False AND ET clock between 4:00 and 9:30 → premarket."""
    h = _StubHandler(clock_response={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 7, 0))  # Mon 7 AM
    assert am._market_session(h) == "premarket"


def test_market_session_afterhours_window(monkeypatch):
    """is_open=False AND ET clock between 16:00 and 20:00 → afterhours."""
    h = _StubHandler(clock_response={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 18, 30))  # Mon 6:30 PM
    assert am._market_session(h) == "afterhours"


def test_market_session_overnight_window(monkeypatch):
    """is_open=False AND ET clock 21:00 → overnight."""
    h = _StubHandler(clock_response={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 21, 0))
    assert am._market_session(h) == "overnight"


def test_market_session_overnight_for_weekend(monkeypatch):
    """is_open=False AND day of week is Sat/Sun → overnight (queue MOO)."""
    h = _StubHandler(clock_response={"is_open": False})
    # 2026-04-25 is Saturday.
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 25, 10, 0))
    assert am._market_session(h) == "overnight"


def test_market_session_pre_4am_is_overnight(monkeypatch):
    """is_open=False AND ET clock 03:30 → overnight (pre-market starts 4 AM)."""
    h = _StubHandler(clock_response={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 3, 30))
    assert am._market_session(h) == "overnight"


def test_market_session_at_4am_is_premarket(monkeypatch):
    """At 4:00 AM ET sharp, classification flips to premarket."""
    h = _StubHandler(clock_response={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 4, 0))
    assert am._market_session(h) == "premarket"


def test_market_session_at_930am_is_overnight_when_clock_lies(monkeypatch):
    """If Alpaca's /clock says is_open=False at 9:30 AM, our window
    classifier should still produce a sensible answer (overnight,
    because the boundary is exclusive — premarket ends BEFORE 9:30)."""
    h = _StubHandler(clock_response={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 9, 30))
    assert am._market_session(h) == "overnight"


def test_market_session_at_4pm_is_afterhours(monkeypatch):
    """4:00 PM sharp → after-hours window starts."""
    h = _StubHandler(clock_response={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 16, 0))
    assert am._market_session(h) == "afterhours"


# ============================================================================
# _market_is_closed compatibility shim
# ============================================================================

def test_market_is_closed_false_when_rth():
    h = _StubHandler(clock_response={"is_open": True})
    assert am._market_is_closed(h) is False


def test_market_is_closed_true_when_premarket(monkeypatch):
    h = _StubHandler(clock_response={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 7, 0))
    assert am._market_is_closed(h) is True


# ============================================================================
# _position_qty
# ============================================================================

def test_position_qty_long():
    h = _StubHandler(position_response={"qty": "100", "symbol": "AAPL"})
    assert am._position_qty(h, "AAPL") == 100


def test_position_qty_short_negative():
    h = _StubHandler(position_response={"qty": "-50", "symbol": "SOXL"})
    assert am._position_qty(h, "SOXL") == -50


def test_position_qty_none_when_missing():
    h = _StubHandler(position_response=None)
    assert am._position_qty(h, "AAPL") is None


def test_position_qty_none_when_no_qty_field():
    h = _StubHandler(position_response={"symbol": "AAPL"})  # no qty key
    assert am._position_qty(h, "AAPL") is None


def test_position_qty_none_when_lookup_raises():
    h = _StubHandler(raise_on=("/positions/",))
    assert am._position_qty(h, "AAPL") is None


# ============================================================================
# _latest_price
# ============================================================================

def test_latest_price_returns_float():
    h = _StubHandler(trade_response={"trade": {"p": 19.85, "t": "..."}})
    assert am._latest_price(h, "SOXL") == 19.85


def test_latest_price_none_when_no_trade():
    h = _StubHandler(trade_response={"trade": {}})
    assert am._latest_price(h, "X") is None


def test_latest_price_none_when_lookup_raises():
    h = _StubHandler(raise_on=("/trades/latest",))
    assert am._latest_price(h, "X") is None


def test_latest_price_uses_data_endpoint():
    """_latest_price should hit the data endpoint, not the trading
    endpoint. Pin so a future refactor doesn't accidentally fire
    market-data calls against /v2/orders."""
    h = _StubHandler(trade_response={"trade": {"p": 100}})
    am._latest_price(h, "SOXL")
    # Verify the URL prefix included data.alpaca.markets
    found = any("data.alpaca.markets" in call[1]
                  for call in h.api_calls)
    assert found, f"expected data endpoint URL, got: {h.api_calls}"


# ============================================================================
# _build_xh_close_order
# ============================================================================

def test_build_xh_close_long_uses_minus_1pct():
    body = am._build_xh_close_order("SOXL", 18, "sell", 20.00)
    assert body["limit_price"] == "19.8"   # 20.00 * 0.99 → round(2) = 19.8
    assert body["side"] == "sell"
    assert body["type"] == "limit"
    assert body["extended_hours"] is True
    assert body["time_in_force"] == "day"
    assert body["qty"] == "18"


def test_build_xh_close_short_uses_plus_1pct():
    body = am._build_xh_close_order("SOXL", -50, "buy", 10.00)
    assert body["limit_price"] == "10.1"   # 10.00 * 1.01
    assert body["side"] == "buy"
    assert body["qty"] == "50"   # abs() applied


def test_build_xh_close_returns_none_for_zero_price():
    assert am._build_xh_close_order("X", 10, "sell", 0.0) is None


def test_build_xh_close_returns_none_for_negative_price():
    assert am._build_xh_close_order("X", 10, "sell", -1.0) is None


def test_build_xh_close_returns_none_for_invalid_price():
    assert am._build_xh_close_order("X", 10, "sell", None) is None
    assert am._build_xh_close_order("X", 10, "sell", "bad") is None


def test_build_xh_close_rounds_to_two_decimals():
    body = am._build_xh_close_order("X", 10, "sell", 19.876543)
    # 19.876543 * 0.99 = 19.6777... → 19.68
    assert body["limit_price"] == "19.68"
