"""Round-61 pt.55 — alpaca_mock harness tests using a lazy-import
pattern.

Pt.51 first attempt at this same goal failed CI with exit code 2
(collection error) because the fixture body did
``import scheduler_api`` and ``import cloud_scheduler`` AT FIXTURE
SETUP TIME — that broke module state when other tests in the same
run shared/popped those modules via the http_harness fixture.

Pt.55 v2: the fixture is a pure helper class instance with NO
module imports. Each test does its own lazy
``import cloud_scheduler as cs`` inside the test body, then uses
``monkeypatch.setattr(cs, "user_api_get", alpaca_mock._do_get)``.
Mirrors the pattern proven CI-stable in pt.52 + pt.54.
"""
from __future__ import annotations

import json
import os

import pytest


# ============================================================================
# Pure helper class — defined inside the test module so collection
# never imports cloud_scheduler. The actual patching happens inside
# test bodies, after lazy import.
# ============================================================================

class _AlpacaMock:
    """Records every Alpaca call + serves pre-registered responses
    by HTTP-method + URL substring."""

    def __init__(self):
        self._handlers = []
        self.calls = []

    def register(self, method, path_substring, response):
        self._handlers.append(
            (method.upper(), path_substring, response))

    def _match(self, method, url):
        m = method.upper()
        for hm, substr, resp in reversed(self._handlers):
            if hm == m and substr in url:
                return resp
        return {}

    def _do_get(self, user, path, **_kw):
        self.calls.append(("GET", path, None))
        return self._match("GET", path)

    def _do_post(self, user, path, body=None, **_kw):
        self.calls.append(("POST", path, body))
        return self._match("POST", path)

    def _do_delete(self, user, path, **_kw):
        self.calls.append(("DELETE", path, None))
        return self._match("DELETE", path)

    def _do_patch(self, user, path, body=None, **_kw):
        self.calls.append(("PATCH", path, body))
        return self._match("PATCH", path)

    def assert_called(self, method, path_substring):
        m = method.upper()
        for c_method, c_url, _body in self.calls:
            if c_method == m and path_substring in c_url:
                return True
        raise AssertionError(
            f"expected {m} containing {path_substring!r}; got "
            f"{[(c[0], c[1]) for c in self.calls]}")


@pytest.fixture
def alpaca_mock():
    """Returns a bare _AlpacaMock. Tests do their own lazy import +
    monkeypatch.setattr."""
    return _AlpacaMock()


def _patch_alpaca(monkeypatch, mock):
    """Helper: lazy-import cloud_scheduler and patch every Alpaca
    helper to use `mock`. Call from inside a test body, never at
    module load time."""
    import cloud_scheduler as cs
    monkeypatch.setattr(cs, "user_api_get", mock._do_get)
    monkeypatch.setattr(cs, "user_api_post", mock._do_post)
    monkeypatch.setattr(cs, "user_api_delete", mock._do_delete)
    monkeypatch.setattr(cs, "user_api_patch", mock._do_patch)
    return cs


def _make_user(tmp_path):
    udir = tmp_path / "user_dir"
    udir.mkdir(parents=True, exist_ok=True)
    sdir = tmp_path / "strategies"
    sdir.mkdir(parents=True, exist_ok=True)
    return {
        "id": 42, "username": "testuser",
        "_data_dir": str(udir),
        "_strategies_dir": str(sdir),
        "_api_key": "PKTEST",
        "_api_secret": "secret",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_data_endpoint": "https://data.alpaca.markets/v2",
        "_mode": "paper",
        "alpaca_paper_key_encrypted": "x",
        "alpaca_paper_secret_encrypted": "x",
    }


# ============================================================================
# AlpacaMock self-tests
# ============================================================================

def test_alpaca_mock_register_and_match(alpaca_mock, monkeypatch):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    alpaca_mock.register("GET", "/account",
                          {"portfolio_value": "1000"})
    out = cs.user_api_get({}, "https://api.alpaca/v2/account")
    assert out == {"portfolio_value": "1000"}


def test_alpaca_mock_records_calls(alpaca_mock, monkeypatch):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    cs.user_api_get({}, "/positions")
    cs.user_api_post({}, "/orders", {"symbol": "AAPL"})
    cs.user_api_delete({}, "/orders/abc123")
    methods = [c[0] for c in alpaca_mock.calls]
    assert "GET" in methods
    assert "POST" in methods
    assert "DELETE" in methods


def test_alpaca_mock_default_is_empty_dict(alpaca_mock, monkeypatch):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    assert cs.user_api_get({}, "/unregistered") == {}


def test_alpaca_mock_assert_called_passes(alpaca_mock, monkeypatch):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    cs.user_api_get({}, "/account")
    alpaca_mock.assert_called("GET", "/account")


def test_alpaca_mock_assert_called_raises_on_miss(alpaca_mock):
    try:
        alpaca_mock.assert_called("GET", "/never-called")
    except AssertionError:
        return
    raise AssertionError("expected AssertionError on missing call")


def test_alpaca_mock_last_registered_wins(alpaca_mock, monkeypatch):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    alpaca_mock.register("GET", "/account", {"v": 1})
    alpaca_mock.register("GET", "/account", {"v": 2})
    assert cs.user_api_get({}, "/account") == {"v": 2}


def test_alpaca_mock_method_isolation(alpaca_mock, monkeypatch):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    alpaca_mock.register("GET", "/orders", [{"id": "g"}])
    alpaca_mock.register("POST", "/orders", {"id": "p"})
    assert cs.user_api_get({}, "/orders") == [{"id": "g"}]
    assert cs.user_api_post({}, "/orders", {}) == {"id": "p"}


# ============================================================================
# Direct scheduler-function tests using the fixture
# ============================================================================

def test_record_trade_open_writes_journal(alpaca_mock, monkeypatch,
                                            tmp_path):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    user = _make_user(tmp_path)
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    cs.record_trade_open(
        user, "AAPL", "breakout", 100.0, 5,
        "test deploy", side="buy", deployer="harness")
    journal_path = os.path.join(user["_data_dir"], "trade_journal.json")
    assert os.path.exists(journal_path)
    with open(journal_path) as f:
        data = json.load(f)
    assert len(data["trades"]) == 1
    t = data["trades"][0]
    assert t["symbol"] == "AAPL"
    assert t["strategy"] == "breakout"
    assert t["status"] == "open"
    assert t["qty"] == 5


def test_record_trade_close_marks_open_trade_closed(alpaca_mock,
                                                       monkeypatch,
                                                       tmp_path):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    user = _make_user(tmp_path)
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    cs.record_trade_open(user, "AAPL", "breakout", 100.0, 5,
                          "test", side="buy", deployer="h")
    cs.record_trade_close(user, "AAPL", "breakout", 110.0,
                            pnl=50.0, exit_reason="target_hit",
                            qty=5, side="sell")
    with open(os.path.join(user["_data_dir"],
                            "trade_journal.json")) as f:
        data = json.load(f)
    t = data["trades"][0]
    assert t["status"] == "closed"
    assert t["exit_price"] == 110.0
    assert t["exit_reason"] == "target_hit"
    assert t["pnl"] == 50.0


def test_record_trade_close_idempotent_when_already_closed(
        alpaca_mock, monkeypatch, tmp_path):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    user = _make_user(tmp_path)
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    cs.record_trade_open(user, "AAPL", "breakout", 100.0, 5,
                          "t", side="buy", deployer="h")
    cs.record_trade_close(user, "AAPL", "breakout", 110.0,
                            pnl=50, exit_reason="target_hit",
                            qty=5, side="sell")
    cs.record_trade_close(user, "AAPL", "breakout", 99.0,
                            pnl=-5, exit_reason="overwrite",
                            qty=5, side="sell")
    with open(os.path.join(user["_data_dir"],
                            "trade_journal.json")) as f:
        data = json.load(f)
    t = data["trades"][0]
    assert t["exit_price"] == 110.0   # original close untouched
    assert t["pnl"] == 50.0


def test_check_correlation_allowed_blocks_third_in_sector(
        alpaca_mock, monkeypatch):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    existing = [
        {"symbol": "MSFT", "market_value": "10000"},
        {"symbol": "GOOGL", "market_value": "10000"},
    ]
    allowed, reason = cs.check_correlation_allowed("AAPL", existing)
    assert allowed is False
    assert "Tech" in reason or "sector" in reason.lower()


def test_check_correlation_allowed_passes_first_in_sector(
        alpaca_mock, monkeypatch):
    cs = _patch_alpaca(monkeypatch, alpaca_mock)
    existing = [{"symbol": "JPM", "market_value": "10000"}]
    allowed, reason = cs.check_correlation_allowed("AAPL", existing)
    assert allowed is True
