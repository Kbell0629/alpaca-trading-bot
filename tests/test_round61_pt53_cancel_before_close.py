"""Round-61 pt.53 — test the auto-cancel-pending-orders path on
``handle_close_position``.

Regression: a pre-market MOO sell from pt.50 stays queued at 9:30,
reserving the shares. User clicks Close at 9:43 → DELETE /positions
returns 422 "insufficient qty available for order (available: 0)".
Pt.53 catches that error, cancels the pending sell, and retries.

Tests use lazy imports inside each function (mirrors the
test_round61_pt22_no_silent_except pattern that's known to be
CI-stable).
"""
from __future__ import annotations


def test_cancel_pending_helper_exists():
    """Smoke: the helper is defined at module level."""
    from handlers import actions_mixin
    assert hasattr(actions_mixin, "_cancel_pending_sell_orders")
    assert callable(actions_mixin._cancel_pending_sell_orders)


def test_cancel_pending_helper_returns_count_and_no_error_on_success():
    """Stub handler with one pending sell order — helper cancels it,
    returns (1, None)."""
    from handlers import actions_mixin as am

    class StubHandler:
        user_api_endpoint = "https://paper-api.alpaca.markets/v2"

        def __init__(self):
            self.delete_calls = []

        def user_api_get(self, url):
            return [{
                "id": "abc-123-def-456-7890-1234-5678-9abc",
                "symbol": "SOXL",
                "side": "sell",
                "qty": "29",
                "status": "new",
            }]

        def user_api_delete(self, url):
            self.delete_calls.append(url)
            return {}

    h = StubHandler()
    cancelled, err = am._cancel_pending_sell_orders(h, "SOXL")
    assert cancelled == 1
    assert err is None
    assert any("/orders/" in u for u in h.delete_calls)


def test_cancel_pending_helper_handles_no_open_orders():
    from handlers import actions_mixin as am

    class StubHandler:
        user_api_endpoint = "https://paper-api.alpaca.markets/v2"

        def user_api_get(self, url):
            return []

        def user_api_delete(self, url):
            raise AssertionError("should not delete when nothing open")

    cancelled, err = am._cancel_pending_sell_orders(
        StubHandler(), "SOXL")
    assert cancelled == 0
    assert err is None


def test_cancel_pending_helper_handles_garbage_response():
    from handlers import actions_mixin as am

    class StubHandler:
        user_api_endpoint = "https://paper-api.alpaca.markets/v2"

        def user_api_get(self, url):
            return {"error": "auth"}        # not a list

    cnt, err = am._cancel_pending_sell_orders(StubHandler(), "SOXL")
    assert cnt == 0
    assert err and "couldn't list" in err.lower()


def test_cancel_pending_helper_handles_get_exception():
    from handlers import actions_mixin as am

    class StubHandler:
        user_api_endpoint = "https://paper-api.alpaca.markets/v2"

        def user_api_get(self, url):
            raise RuntimeError("network down")

    cnt, err = am._cancel_pending_sell_orders(
        StubHandler(), "SOXL")
    assert cnt == 0
    assert err and "failed" in err.lower()


def test_cancel_pending_helper_filters_by_symbol():
    """Open orders may contain rows for OTHER symbols; we should
    only cancel SOXL's."""
    from handlers import actions_mixin as am

    class StubHandler:
        user_api_endpoint = "https://paper-api.alpaca.markets/v2"

        def __init__(self):
            self.delete_calls = []

        def user_api_get(self, url):
            return [
                {"id": "soxl-1", "symbol": "SOXL", "side": "sell"},
                {"id": "aapl-1", "symbol": "AAPL", "side": "sell"},
                {"id": "soxl-2", "symbol": "SOXL", "side": "buy"},
            ]

        def user_api_delete(self, url):
            self.delete_calls.append(url)
            return {}

    h = StubHandler()
    cnt, err = am._cancel_pending_sell_orders(h, "SOXL")
    assert cnt == 2
    assert all("aapl" not in u.lower() for u in h.delete_calls)


def test_cancel_pending_helper_skips_orders_without_id():
    """Defensive: an order dict missing the `id` field can't be
    cancelled by URL — should skip without raising."""
    from handlers import actions_mixin as am

    class StubHandler:
        user_api_endpoint = "https://paper-api.alpaca.markets/v2"

        def user_api_get(self, url):
            return [
                {"symbol": "SOXL", "side": "sell"},        # no id
                {"id": "abc", "symbol": "SOXL", "side": "sell"},
            ]

        def __init__(self):
            self.delete_calls = []

        def user_api_delete(self, url):
            self.delete_calls.append(url)
            return {}

    h = StubHandler()
    cnt, err = am._cancel_pending_sell_orders(h, "SOXL")
    assert cnt == 1
    assert len(h.delete_calls) == 1


def test_cancel_pending_helper_continues_on_individual_cancel_failure():
    """If one DELETE raises, others should still attempt and the
    counter should reflect successful cancels only."""
    from handlers import actions_mixin as am

    class StubHandler:
        user_api_endpoint = "https://paper-api.alpaca.markets/v2"

        def user_api_get(self, url):
            return [
                {"id": "good-1", "symbol": "SOXL", "side": "sell"},
                {"id": "bad-2",  "symbol": "SOXL", "side": "sell"},
                {"id": "good-3", "symbol": "SOXL", "side": "sell"},
            ]

        def user_api_delete(self, url):
            if "bad-2" in url:
                raise RuntimeError("rate limited")
            return {}

    cnt, err = am._cancel_pending_sell_orders(
        StubHandler(), "SOXL")
    assert cnt == 2


# ============================================================================
# Source-pin: handle_close_position uses the helper on insufficient-qty
# ============================================================================

def test_handle_close_position_imports_cancel_helper():
    """The handler should reference _cancel_pending_sell_orders so
    the auto-recovery path stays wired."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "handlers" / "actions_mixin.py").read_text()
    assert "_cancel_pending_sell_orders" in src


def test_handle_close_position_retries_after_cancel():
    """Source pin: the close handler must retry the DELETE after
    cancelling pending orders."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "handlers" / "actions_mixin.py").read_text()
    # Look for both the cancel call AND a second DELETE attempt.
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    assert "_cancel_pending_sell_orders" in body
    # Two DELETE /positions calls in the body (initial + retry).
    assert body.count("/positions/{symbol}") >= 2


def test_handle_close_position_enriches_insufficient_qty_message():
    """When no pending orders are found, the error message should
    point the user to Open Orders so they know what to look for."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    assert "Open Orders" in body
    assert "reserving the shares" in body
