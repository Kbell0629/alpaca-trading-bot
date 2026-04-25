"""Round-61 pt.54 — full coverage for pt.50 closed-market routing
helpers. Pt.52 established that the lazy-import pattern (every
``from handlers import actions_mixin`` lives inside a test body) is
CI-stable; pt.54 adds the 20 tests that pt.51's monkeypatch-heavy
file couldn't ship.

Helpers under test:
  * ``_market_session(handler)`` — RTH / premarket / afterhours /
    overnight classification by combining Alpaca /clock with ET
    wall-clock for the non-RTH window split.
  * ``_position_qty(handler, symbol)`` — signed-qty lookup; long
    → positive, short → negative.
  * ``_latest_price(handler, symbol)`` — Alpaca /trades/latest probe
    used to price the extended-hours limit.
"""
from __future__ import annotations

from datetime import datetime


# ============================================================================
# Stub handler — minimal interface that the routing helpers need.
# Defined at module level since it has NO direct import of
# actions_mixin (just methods that the helpers call on it).
# ============================================================================

class _StubHandler:
    """Test double that captures every API call and serves
    pre-registered responses. No external imports needed."""

    def __init__(self, *, clock=None, position=None, trade=None,
                  raise_on=None):
        self.user_api_endpoint = "https://paper-api.alpaca.markets/v2"
        self.user_data_endpoint = "https://data.alpaca.markets/v2"
        self.user_api_key = "PKTEST"
        self.user_api_secret = "secret"
        self.session_mode = "paper"
        self._responses = {
            "/clock": clock if clock is not None else {"is_open": True},
            "/positions/": position,
            "/trades/latest": trade,
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
# _market_session — Alpaca /clock + ET-wall-clock fallback
# ============================================================================

def test_market_session_rth_when_clock_says_open():
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": True})
    assert am._market_session(h) == "rth"


def test_market_session_rth_when_clock_probe_fails():
    """Fail-open: probe error → assume RTH so we don't accidentally
    re-route during normal hours."""
    from handlers import actions_mixin as am
    h = _StubHandler(raise_on=("/clock",))
    assert am._market_session(h) == "rth"


def test_market_session_premarket_window(monkeypatch):
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 7, 0))   # Mon 7 AM
    assert am._market_session(h) == "premarket"


def test_market_session_afterhours_window(monkeypatch):
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 18, 30))  # Mon 6:30 PM
    assert am._market_session(h) == "afterhours"


def test_market_session_overnight_window(monkeypatch):
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 21, 0))
    assert am._market_session(h) == "overnight"


def test_market_session_overnight_for_weekend(monkeypatch):
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    # 2026-04-25 is Saturday.
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 25, 10, 0))
    assert am._market_session(h) == "overnight"


def test_market_session_pre_4am_is_overnight(monkeypatch):
    """Pre-market starts at 4 AM ET; before that it's overnight."""
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 3, 30))
    assert am._market_session(h) == "overnight"


def test_market_session_at_4am_is_premarket(monkeypatch):
    """At 4:00 AM ET sharp, classification flips to premarket."""
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 4, 0))
    assert am._market_session(h) == "premarket"


def test_market_session_at_930am_is_overnight_when_clock_lies(monkeypatch):
    """Boundary: 9:30 AM with clock saying closed → overnight (not
    premarket; window is 4:00..9:30 exclusive)."""
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 9, 30))
    assert am._market_session(h) == "overnight"


def test_market_session_at_4pm_is_afterhours(monkeypatch):
    """4:00 PM sharp → after-hours window starts."""
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 16, 0))
    assert am._market_session(h) == "afterhours"


# ============================================================================
# _market_is_closed compatibility shim
# ============================================================================

def test_market_is_closed_false_when_rth():
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": True})
    assert am._market_is_closed(h) is False


def test_market_is_closed_true_when_premarket(monkeypatch):
    from handlers import actions_mixin as am
    h = _StubHandler(clock={"is_open": False})
    monkeypatch.setattr(am, "now_et",
                          lambda: datetime(2026, 4, 27, 7, 0))
    assert am._market_is_closed(h) is True


# ============================================================================
# _position_qty
# ============================================================================

def test_position_qty_long():
    from handlers import actions_mixin as am
    h = _StubHandler(position={"qty": "100", "symbol": "AAPL"})
    assert am._position_qty(h, "AAPL") == 100


def test_position_qty_short_negative():
    from handlers import actions_mixin as am
    h = _StubHandler(position={"qty": "-50", "symbol": "SOXL"})
    assert am._position_qty(h, "SOXL") == -50


def test_position_qty_none_when_missing():
    from handlers import actions_mixin as am
    h = _StubHandler(position=None)
    assert am._position_qty(h, "AAPL") is None


def test_position_qty_none_when_no_qty_field():
    from handlers import actions_mixin as am
    h = _StubHandler(position={"symbol": "AAPL"})  # no qty key
    assert am._position_qty(h, "AAPL") is None


def test_position_qty_none_when_lookup_raises():
    from handlers import actions_mixin as am
    h = _StubHandler(raise_on=("/positions/",))
    assert am._position_qty(h, "AAPL") is None


# ============================================================================
# _latest_price
# ============================================================================

def test_latest_price_returns_float():
    from handlers import actions_mixin as am
    h = _StubHandler(trade={"trade": {"p": 19.85, "t": "..."}})
    assert am._latest_price(h, "SOXL") == 19.85


def test_latest_price_none_when_no_trade():
    from handlers import actions_mixin as am
    h = _StubHandler(trade={"trade": {}})
    assert am._latest_price(h, "X") is None


def test_latest_price_none_when_lookup_raises():
    from handlers import actions_mixin as am
    h = _StubHandler(raise_on=("/trades/latest",))
    assert am._latest_price(h, "X") is None


def test_latest_price_uses_data_endpoint():
    """The probe should hit data.alpaca.markets, not the trading
    endpoint. Pin so a future refactor doesn't accidentally fire
    market-data calls against /v2/orders."""
    from handlers import actions_mixin as am
    h = _StubHandler(trade={"trade": {"p": 100}})
    am._latest_price(h, "SOXL")
    found = any("data.alpaca.markets" in call[1]
                  for call in h.api_calls)
    assert found, f"expected data endpoint URL, got: {h.api_calls}"
