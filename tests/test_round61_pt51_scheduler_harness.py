"""Round-61 pt.51 — scheduler harness tests using the new
``alpaca_mock`` fixture from ``conftest.py``.

Goal: lift cloud_scheduler.py coverage by exercising the functions
that previously needed live Alpaca to test. Each test stays narrow
— stub Alpaca, call one scheduler function, assert side effects.
"""
from __future__ import annotations

import os


def _make_user(tmp_path):
    """Minimal user dict that cloud_scheduler functions accept."""
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
# AlpacaMock fixture self-tests
# ============================================================================

def test_alpaca_mock_registers_and_returns(alpaca_mock):
    alpaca_mock.register("GET", "/account",
                          {"portfolio_value": "1000"})
    import cloud_scheduler as cs
    out = cs.user_api_get({}, "https://api.alpaca/v2/account")
    assert out == {"portfolio_value": "1000"}


def test_alpaca_mock_records_all_calls(alpaca_mock):
    import cloud_scheduler as cs
    cs.user_api_get({}, "/positions")
    cs.user_api_post({}, "/orders", body={"symbol": "AAPL"})
    cs.user_api_delete({}, "/orders/abc123")
    methods = [c[0] for c in alpaca_mock.calls]
    assert "GET" in methods and "POST" in methods and "DELETE" in methods


def test_alpaca_mock_default_response_is_empty_dict(alpaca_mock):
    """Unregistered URL returns empty dict (not an error). Lets
    handlers run without crashing on incidental calls."""
    import cloud_scheduler as cs
    assert cs.user_api_get({}, "/something/unregistered") == {}


def test_alpaca_mock_assert_called_passes(alpaca_mock):
    import cloud_scheduler as cs
    cs.user_api_get({}, "/account")
    alpaca_mock.assert_called("GET", "/account")


def test_alpaca_mock_assert_called_raises_on_miss(alpaca_mock):
    try:
        alpaca_mock.assert_called("GET", "/never-called")
    except AssertionError:
        return
    raise AssertionError("expected AssertionError on missing call")


def test_alpaca_mock_last_registered_wins(alpaca_mock):
    """Registering twice on the same path; second registration
    should be the one returned (lets tests override defaults)."""
    alpaca_mock.register("GET", "/account", {"v": 1})
    alpaca_mock.register("GET", "/account", {"v": 2})
    import cloud_scheduler as cs
    assert cs.user_api_get({}, "/account") == {"v": 2}


def test_alpaca_mock_method_isolation(alpaca_mock):
    """A GET registration must not match POST and vice-versa."""
    alpaca_mock.register("GET", "/orders", [{"id": "g"}])
    alpaca_mock.register("POST", "/orders", {"id": "p"})
    import cloud_scheduler as cs
    assert cs.user_api_get({}, "/orders") == [{"id": "g"}]
    assert cs.user_api_post({}, "/orders", body={}) == {"id": "p"}


# ============================================================================
# Direct scheduler-function tests using the harness
# ============================================================================

def test_record_trade_open_writes_journal(alpaca_mock, tmp_path,
                                            monkeypatch):
    """record_trade_open writes a trades entry to per-user journal."""
    import cloud_scheduler as cs
    user = _make_user(tmp_path)
    # Override user_file so the function writes inside tmp_path.
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    cs.record_trade_open(
        user, "AAPL", "breakout", 100.0, 5,
        "test deploy", side="buy", deployer="harness")
    journal_path = os.path.join(user["_data_dir"], "trade_journal.json")
    assert os.path.exists(journal_path)
    import json
    with open(journal_path) as f:
        data = json.load(f)
    assert len(data["trades"]) == 1
    t = data["trades"][0]
    assert t["symbol"] == "AAPL"
    assert t["strategy"] == "breakout"
    assert t["status"] == "open"
    assert t["qty"] == 5


def test_record_trade_close_marks_open_trade_closed(alpaca_mock,
                                                       tmp_path,
                                                       monkeypatch):
    import cloud_scheduler as cs
    user = _make_user(tmp_path)
    monkeypatch.setattr(
        cs, "user_file",
        lambda u, name: os.path.join(u["_data_dir"], name))
    cs.record_trade_open(user, "AAPL", "breakout", 100.0, 5,
                          "test", side="buy", deployer="h")
    cs.record_trade_close(user, "AAPL", "breakout", 110.0,
                            pnl=50.0, exit_reason="target_hit",
                            qty=5, side="sell")
    import json
    with open(os.path.join(user["_data_dir"],
                            "trade_journal.json")) as f:
        data = json.load(f)
    t = data["trades"][0]
    assert t["status"] == "closed"
    assert t["exit_price"] == 110.0
    assert t["exit_reason"] == "target_hit"
    assert t["pnl"] == 50.0


def test_record_trade_close_idempotent_when_already_closed(
        alpaca_mock, tmp_path, monkeypatch):
    """Calling record_trade_close twice must not double-modify the
    closed-out entry."""
    import cloud_scheduler as cs
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
    import json
    with open(os.path.join(user["_data_dir"],
                            "trade_journal.json")) as f:
        data = json.load(f)
    # Second close call is a no-op for the already-closed entry.
    t = data["trades"][0]
    assert t["exit_price"] == 110.0
    assert t["pnl"] == 50.0


def test_check_correlation_allowed_blocks_third_in_sector(alpaca_mock):
    """check_correlation_allowed: 2 same-sector positions already +
    a third candidate in the same sector → blocked."""
    import cloud_scheduler as cs
    # Two existing tech positions; AAPL would be the third.
    existing = [
        {"symbol": "MSFT", "market_value": "10000"},
        {"symbol": "GOOGL", "market_value": "10000"},
    ]
    allowed, reason = cs.check_correlation_allowed("AAPL", existing)
    assert allowed is False
    assert "Tech" in reason or "sector" in reason.lower()


def test_check_correlation_allowed_passes_first_in_sector(alpaca_mock):
    import cloud_scheduler as cs
    existing = [{"symbol": "JPM", "market_value": "10000"}]
    allowed, reason = cs.check_correlation_allowed("AAPL", existing)
    assert allowed is True
