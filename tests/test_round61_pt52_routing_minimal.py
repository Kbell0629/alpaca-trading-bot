"""Round-61 pt.52 — minimal smoke test to find what about
``test_round61_pt51_closed_market_routing.py`` made CI exit with
code 2. This file deliberately starts MINIMAL — if CI passes,
we'll add more tests one-at-a-time until we find the offending
pattern.

Pattern: lazy import inside each test function (no module-level
import of the system-under-test). Mirrors the pt.22 scanner test
which is known to coexist cleanly with the http_harness fixture.
"""
from __future__ import annotations


def test_actions_mixin_module_imports():
    """Smoke: actions_mixin can be imported."""
    from handlers import actions_mixin
    assert hasattr(actions_mixin, "_market_session")
    assert hasattr(actions_mixin, "_position_qty")
    assert hasattr(actions_mixin, "_latest_price")
    assert hasattr(actions_mixin, "_build_xh_close_order")


def test_build_xh_close_order_long_pure_function():
    """Pure-function test — no monkeypatching, no fixtures."""
    from handlers import actions_mixin as am
    body = am._build_xh_close_order("SOXL", 18, "sell", 20.00)
    assert body is not None
    assert body["limit_price"] == "19.8"
    assert body["side"] == "sell"
    assert body["type"] == "limit"
    assert body["extended_hours"] is True


def test_build_xh_close_order_short_pure_function():
    from handlers import actions_mixin as am
    body = am._build_xh_close_order("SOXL", -50, "buy", 10.00)
    assert body is not None
    assert body["limit_price"] == "10.1"
    assert body["side"] == "buy"
    assert body["qty"] == "50"


def test_build_xh_close_order_zero_price():
    from handlers import actions_mixin as am
    assert am._build_xh_close_order("X", 10, "sell", 0.0) is None


def test_build_xh_close_order_negative_price():
    from handlers import actions_mixin as am
    assert am._build_xh_close_order("X", 10, "sell", -1.0) is None


def test_build_xh_close_order_invalid_price():
    from handlers import actions_mixin as am
    assert am._build_xh_close_order("X", 10, "sell", None) is None
    assert am._build_xh_close_order("X", 10, "sell", "bad") is None


def test_build_xh_close_order_rounds_two_decimals():
    from handlers import actions_mixin as am
    body = am._build_xh_close_order("X", 10, "sell", 19.876543)
    assert body["limit_price"] == "19.68"
