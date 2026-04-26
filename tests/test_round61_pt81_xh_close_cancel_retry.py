"""Round-61 pt.81 — extend cancel+retry recovery to xh_close /
overnight POST path.

User-reported (with screenshot at 8:34 PM ET): the SOXL "insufficient
qty available" close kept failing despite pt.50/53/69/75. Root
cause: pt.50 routes after-hours/overnight closes through a POST
to /orders (xh_close limit or MOO BUY-to-cover for shorts), NOT
through DELETE /positions. The pt.69 cancel-and-retry recovery
only covered the DELETE branch — the POST branch surfaced the
bare error.

Pt.81 adds the same recovery to the POST branch:
  1. POST /orders → "insufficient qty"
  2. _cancel_pending_sell_orders (drops the SOXL BUY-stop)
  3. Retry POST up to 4 times with backoff (0.3, 0.6, 1.0, 1.5s)
  4. Success → fall through to settled-funds + success response
  5. All retries exhausted → clear "try again" message
  6. Different error → surface immediately
"""
from __future__ import annotations


class _AlpacaStub:
    def __init__(self):
        self.user_api_endpoint = "https://paper-api.alpaca.markets/v2"
        self.user_data_endpoint = "https://data.alpaca.markets/v2"
        self.user_api_key = "PKTEST"
        self.user_api_secret = "secret"
        self.session_mode = "paper"
        self.user_id = 1
        self.client_address = ("127.0.0.1", 0)
        self.current_user = {"id": 1, "username": "tester"}
        # Simulate after-hours: clock returns is_open=False.
        self.clock = {"is_open": False}
        self.position = None
        self.trade = None
        self.open_orders = []
        self.post_order_responses = []
        self.cancel_order_responses = []
        self.api_calls = []
        self._sent_json = None
        self._sent_status = 200

    def user_api_get(self, url):
        self.api_calls.append(("GET", url))
        if "/clock" in url:
            return self.clock
        if "/positions/" in url:
            return self.position
        if "/orders?" in url or url.endswith("/orders"):
            return self.open_orders
        if "/trades/latest" in url:
            return self.trade
        return {}

    def user_api_post(self, url, body):
        self.api_calls.append(("POST", url, body))
        if self.post_order_responses:
            return self.post_order_responses.pop(0)
        return {"id": "ord_x", "status": "new"}

    def user_api_delete(self, url):
        self.api_calls.append(("DELETE", url))
        if "/orders/" in url:
            if self.cancel_order_responses:
                return self.cancel_order_responses.pop(0)
            return {}
        return {}

    def send_json(self, data, status=200):
        self._sent_json = data
        self._sent_status = status

    def _user_file(self, name):
        return f"/tmp/{name}"


def _patch_sleep(monkeypatch):
    from handlers import actions_mixin as am
    monkeypatch.setattr(am.time, "sleep", lambda s: None)


def _patch_now_overnight(monkeypatch):
    """Force _market_session() to return 'overnight' (not RTH)."""
    from handlers import actions_mixin as am
    import datetime as _dt
    monkeypatch.setattr(am, "now_et",
                          lambda: _dt.datetime(2026, 4, 25, 20, 34))


# ============================================================================
# The user's exact scenario: SOXL short close at 8:34 PM ET on a Saturday
# ============================================================================

def test_overnight_soxl_close_recovers_via_cancel_retry(monkeypatch):
    """Reproduces the screenshot: SOXL short, after-hours, POST
    returns insufficient qty because of pending BUY-stop. Pt.81
    cancels + retries the POST."""
    _patch_sleep(monkeypatch)
    _patch_now_overnight(monkeypatch)
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _AlpacaStub):
        pass

    h = H()
    h.position = {"qty": "-29", "symbol": "SOXL"}
    h.open_orders = [{
        "id": "stop_xyz", "symbol": "SOXL", "side": "buy",
        "type": "stop", "status": "accepted", "qty": "29",
    }]
    err = ("insufficient qty available for order "
           "(requested: 29, available: 0)")
    h.post_order_responses = [
        {"error": err},   # initial POST
        {"error": err},   # retry 1 (still propagating)
        {"id": "ord_close", "status": "accepted"},  # success
    ]
    h.cancel_order_responses = [{}]

    h.handle_close_position({"symbol": "SOXL"})

    assert h._sent_status == 200, (
        f"Expected success, got status={h._sent_status} "
        f"json={h._sent_json}")
    assert h._sent_json.get("success") is True
    assert h._sent_json.get("queued") is True

    # Cancel scan ran with the pt.75 URL (no &symbols=).
    cancel_scans = [c for c in h.api_calls
                      if c[0] == "GET" and "/orders?" in c[1]]
    assert len(cancel_scans) == 1
    assert "&symbols=" not in cancel_scans[0][1]
    # Pending order was cancelled.
    assert any(c[0] == "DELETE" and "/orders/stop_xyz" in c[1]
                 for c in h.api_calls)


def test_overnight_close_retries_exhausted_returns_clear_message(monkeypatch):
    """All 4 retries exhausted with same insufficient-qty error →
    surface 'try again in a moment' hint."""
    _patch_sleep(monkeypatch)
    _patch_now_overnight(monkeypatch)
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _AlpacaStub):
        pass

    h = H()
    h.position = {"qty": "-10", "symbol": "AAPL"}
    h.open_orders = [{
        "id": "ord1", "symbol": "AAPL", "side": "buy",
        "type": "stop", "status": "accepted", "qty": "10",
    }]
    err = "insufficient qty available for order (requested: 10, available: 0)"
    h.post_order_responses = [{"error": err}] * 5
    h.cancel_order_responses = [{}]

    h.handle_close_position({"symbol": "AAPL"})

    assert h._sent_status == 400
    assert "try again in a moment" in h._sent_json["error"]
    assert "cancelled 1" in h._sent_json["error"].lower()


def test_overnight_close_different_error_surfaces_immediately(monkeypatch):
    """A different error mid-retry surfaces on the spot."""
    _patch_sleep(monkeypatch)
    _patch_now_overnight(monkeypatch)
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _AlpacaStub):
        pass

    h = H()
    h.position = {"qty": "-10", "symbol": "X"}
    h.open_orders = [{
        "id": "ord1", "symbol": "X", "side": "buy",
        "qty": "10", "status": "accepted",
    }]
    h.post_order_responses = [
        {"error": "insufficient qty available (requested: 10, available: 0)"},
        {"error": "account is locked for trading"},
    ]
    h.cancel_order_responses = [{}]

    h.handle_close_position({"symbol": "X"})

    assert h._sent_status == 400
    assert "account is locked" in h._sent_json["error"]
    # Should have stopped retrying after the different error.
    posts = [c for c in h.api_calls if c[0] == "POST"]
    assert len(posts) <= 3


def test_overnight_close_no_pending_orders_enriched_error(monkeypatch):
    """If the cancel scan returns 0 orders, error gets the
    'check Open Orders' enrichment."""
    _patch_sleep(monkeypatch)
    _patch_now_overnight(monkeypatch)
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _AlpacaStub):
        pass

    h = H()
    h.position = {"qty": "-10", "symbol": "X"}
    h.open_orders = []
    err = "insufficient qty available for order (requested: 10, available: 0)"
    h.post_order_responses = [{"error": err}]

    h.handle_close_position({"symbol": "X"})

    assert h._sent_status == 400
    assert "check Open Orders" in h._sent_json["error"]


def test_overnight_close_clean_path_unchanged(monkeypatch):
    """When the initial POST succeeds, no cancel scan or retry —
    same response shape as before pt.81."""
    _patch_sleep(monkeypatch)
    _patch_now_overnight(monkeypatch)
    from handlers.actions_mixin import ActionsHandlerMixin

    class H(ActionsHandlerMixin, _AlpacaStub):
        pass

    h = H()
    h.position = {"qty": "10", "symbol": "AAPL"}
    h.trade = {"trade": {"p": 100.0}}
    h.post_order_responses = [{"id": "ord_x", "status": "new"}]

    h.handle_close_position({"symbol": "AAPL"})

    assert h._sent_status == 200
    assert h._sent_json.get("success") is True
    assert h._sent_json.get("queued") is True
    # No cancel scan triggered.
    assert not any(c[0] == "GET" and "/orders?" in c[1]
                     for c in h.api_calls)


# ============================================================================
# Source-pin: the recovery code is wired into the xh_close branch
# ============================================================================

def test_close_handler_has_xh_cancel_retry_block():
    """The handler must contain the pt.81 retry block in the
    xh_close / overnight POST branch."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "handlers" / "actions_mixin.py").read_text()
    handler_start = src.find("def handle_close_position")
    handler_end = src.find("def handle_sell", handler_start)
    body = src[handler_start:handler_end]
    # The xh_close POST branch is the part BEFORE the
    # `_cancel_pending_sell_orders` call we inherited from pt.53.
    # Pt.81 added a SECOND `_cancel_pending_sell_orders` call in
    # the xh_close branch — there should be at least 2 references.
    assert body.count("_cancel_pending_sell_orders") >= 2
    # And the retry-with-backoff schedule appears in both branches.
    assert body.count("(0.3, 0.6, 1.0, 1.5)") >= 2


def test_close_handler_pt81_xh_recovery_documented():
    """The new block is tagged with `pt.81` so a future reader
    knows why it's there."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "handlers" / "actions_mixin.py").read_text()
    assert "pt.81" in src
