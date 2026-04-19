"""
Round-15 audit fixes — every fix backed by a pinning test.

Covers:
  * capital_check._compute_reserved_by_orders fallback ladder
    (live quote → position avg cost → $1000 floor)
  * _load_with_shared_fallback per-user isolation boundary
  * smart_orders partial-fill blended cost basis
  * aria-sort helper on sortable headers
  * Alpaca 401/403 auth failure alert dedup
  * notify ntfy-topic scrub in error logs
  * capitol_trades Stock Watcher dead-code removal
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock


# ---------- capital_check._compute_reserved_by_orders ----------


def _fake_fetch(_sym):
    return 0.0  # always "live quote unavailable" for deterministic tests


def test_capital_reserved_explicit_limit_price():
    """Explicit limit_price → direct multiplication, no fallback."""
    import capital_check as cc
    orders = [{"side": "buy", "symbol": "AAPL",
               "limit_price": "150", "qty": "10"}]
    got = cc._compute_reserved_by_orders(orders, {}, _fake_fetch)
    assert got == 1500.0


def test_capital_reserved_explicit_notional():
    """Explicit notional → used directly."""
    import capital_check as cc
    orders = [{"side": "buy", "symbol": "SPY",
               "notional": "500", "qty": "0"}]
    got = cc._compute_reserved_by_orders(orders, {}, _fake_fetch)
    assert got == 500.0


def test_capital_reserved_uses_live_quote_when_available():
    """No price → live quote is the first fallback."""
    import capital_check as cc
    orders = [{"side": "buy", "symbol": "AAPL", "qty": "5"}]
    got = cc._compute_reserved_by_orders(
        orders, {}, lambda sym: 200.0  # live quote
    )
    assert got == 1000.0


def test_capital_reserved_uses_position_avg_when_live_missing():
    """No price + no live quote → position avg cost for the same symbol."""
    import capital_check as cc
    orders = [{"side": "buy", "symbol": "AAPL", "qty": "5"}]
    got = cc._compute_reserved_by_orders(
        orders, {"AAPL": 120.0}, lambda _sym: 0.0  # live unavailable
    )
    assert got == 600.0


def test_capital_reserved_uses_1000_floor_when_all_missing():
    """No price + no live + no position → $1000/share conservative floor.
    This is the critical security guardrail — we'd rather refuse an
    over-leveraged deploy than under-reserve capital."""
    import capital_check as cc
    orders = [{"side": "buy", "symbol": "UNKNOWN", "qty": "5"}]
    got = cc._compute_reserved_by_orders(
        orders, {}, lambda _sym: 0.0
    )
    assert got == 5000.0, (
        "fallback should reserve $1000/share when no other price available — "
        "under-reservation here is a real-money over-leverage bug"
    )


def test_capital_reserved_ignores_sell_orders():
    import capital_check as cc
    orders = [{"side": "sell", "limit_price": "200", "qty": "10"}]
    assert cc._compute_reserved_by_orders(orders, {}, _fake_fetch) == 0.0


def test_capital_reserved_empty_list():
    import capital_check as cc
    assert cc._compute_reserved_by_orders([], {}, _fake_fetch) == 0.0
    assert cc._compute_reserved_by_orders(None, {}, _fake_fetch) == 0.0


def test_capital_reserved_tolerates_malformed_price():
    """A bad price field shouldn't crash — skip that order."""
    import capital_check as cc
    orders = [{"side": "buy", "limit_price": "not-a-number", "qty": "5"}]
    # Should not raise; the order is skipped (continue)
    got = cc._compute_reserved_by_orders(orders, {}, _fake_fetch)
    assert got == 0.0


def test_capital_reserved_tolerates_fetch_last_raising():
    """If fetch_last raises, fall through to position avg / floor."""
    import capital_check as cc
    def _raiser(_sym):
        raise RuntimeError("yfinance down")
    orders = [{"side": "buy", "symbol": "AAPL", "qty": "5"}]
    # No position, no live → $1000 floor
    got = cc._compute_reserved_by_orders(orders, {}, _raiser)
    assert got == 5000.0


# ---------- _load_with_shared_fallback per-user isolation ----------


def test_load_with_shared_fallback_user_1_inherits(tmp_path):
    """user_id==1 (bootstrap admin) inherits shared DATA_DIR files."""
    from per_user_isolation import load_with_shared_fallback
    user_path = str(tmp_path / "users/1/wheel.json")
    shared_path = str(tmp_path / "wheel.json")
    os.makedirs(os.path.dirname(user_path), exist_ok=True)
    with open(shared_path, "w") as f:
        f.write('{"shared": true}')
    val = load_with_shared_fallback(user_path, shared_path, user_id=1)
    assert val == {"shared": True}


def test_load_with_shared_fallback_other_users_isolated(tmp_path):
    """CRITICAL: user_id != 1 must NEVER fall back to shared. A
    regression here would cross-contaminate strategies and auto-trade
    on other users' accounts. This test pins the invariant."""
    from per_user_isolation import load_with_shared_fallback
    user_path = str(tmp_path / "users/2/wheel.json")
    shared_path = str(tmp_path / "wheel.json")
    os.makedirs(os.path.dirname(user_path), exist_ok=True)
    with open(shared_path, "w") as f:
        f.write('{"shared": true}')
    val = load_with_shared_fallback(user_path, shared_path, user_id=2)
    assert val is None, (
        "SECURITY REGRESSION: non-admin user_id=2 inherited shared config. "
        "This would auto-trade on users' Alpaca accounts with other people's "
        "strategies. See per_user_isolation.load_with_shared_fallback docstring."
    )


def test_load_with_shared_fallback_user_1_no_shared_returns_none(tmp_path):
    """user_id==1 with no shared file + no user file → None."""
    from per_user_isolation import load_with_shared_fallback
    user_path = str(tmp_path / "users/1/wheel.json")
    shared_path = str(tmp_path / "wheel.json")  # doesn't exist
    val = load_with_shared_fallback(user_path, shared_path, user_id=1)
    assert val is None


def test_load_with_shared_fallback_user_file_takes_precedence(tmp_path):
    """Even for user_id==1, a user-specific file beats shared."""
    from per_user_isolation import load_with_shared_fallback
    user_path = str(tmp_path / "users/1/wheel.json")
    shared_path = str(tmp_path / "wheel.json")
    os.makedirs(os.path.dirname(user_path), exist_ok=True)
    with open(user_path, "w") as f:
        f.write('{"mine": true}')
    with open(shared_path, "w") as f:
        f.write('{"shared": true}')
    val = load_with_shared_fallback(user_path, shared_path, user_id=1)
    assert val == {"mine": True}


def test_load_with_shared_fallback_all_user_ids_above_1_blocked(tmp_path):
    """Sweep user_ids 2..10 to lock the invariant: only id==1 inherits."""
    from per_user_isolation import load_with_shared_fallback
    shared_path = str(tmp_path / "wheel.json")
    with open(shared_path, "w") as f:
        f.write('{"shared": true}')
    for uid in range(2, 11):
        user_path = str(tmp_path / f"users/{uid}/wheel.json")
        os.makedirs(os.path.dirname(user_path), exist_ok=True)
        val = load_with_shared_fallback(user_path, shared_path, user_id=uid)
        assert val is None, f"user_id={uid} leaked shared"


# ---------- capitol_trades Stock Watcher removal ----------


def test_stock_watcher_provider_removed():
    """The deprecated Stock Watcher S3 provider was removed in round-15
    (hosts stopped resolving). Lock in the removal so a future revert
    triggers a test failure."""
    import capitol_trades
    assert "stock_watcher" not in capitol_trades._providers
    assert not hasattr(capitol_trades, "_fetch_stock_watcher")
    assert not hasattr(capitol_trades, "_STOCK_WATCHER_HOUSE")
    assert not hasattr(capitol_trades, "_STOCK_WATCHER_SENATE")


def test_normalize_amount_label_still_exists():
    """Used by Quiver + FMP; must still be importable."""
    import capitol_trades
    assert callable(capitol_trades._normalize_amount_label)
    assert capitol_trades._normalize_amount_label("$1,001 - $15,000") == "$1,001 - $15,000"


# ---------- 401/403 auth failure alert dedup ----------


def test_alpaca_auth_failure_alert_fires_once_per_day(monkeypatch):
    """Round-15: when Alpaca returns 401/403, we fire critical_alert
    but dedupe by (user_id, date) so a bad-key-run doesn't spam the
    operator with one push per order attempt."""
    import cloud_scheduler as cs
    calls = []
    fake_obs = MagicMock()
    fake_obs.critical_alert = lambda *a, **k: calls.append((a, k))
    monkeypatch.setitem(sys.modules, "observability", fake_obs)

    # Reset the dedup dict for a clean test
    with cs._auth_alert_lock:
        cs._auth_alert_dates.clear()

    user = {"id": 42, "username": "tester"}
    cs._alert_alpaca_auth_failure(user, 401, "Unauthorized")
    cs._alert_alpaca_auth_failure(user, 403, "Forbidden")
    cs._alert_alpaca_auth_failure(user, 401, "Unauthorized")
    assert len(calls) == 1, f"expected 1 alert, got {len(calls)}"


def test_alpaca_auth_failure_alert_per_user(monkeypatch):
    """Two different users each get their own alert on the same day."""
    import cloud_scheduler as cs
    calls = []
    fake_obs = MagicMock()
    fake_obs.critical_alert = lambda *a, **k: calls.append(a)
    monkeypatch.setitem(sys.modules, "observability", fake_obs)

    with cs._auth_alert_lock:
        cs._auth_alert_dates.clear()

    cs._alert_alpaca_auth_failure({"id": 1, "username": "a"}, 401, "x")
    cs._alert_alpaca_auth_failure({"id": 2, "username": "b"}, 401, "x")
    assert len(calls) == 2


# ---------- notify ntfy-topic scrub ----------


def test_notify_error_log_does_not_leak_topic(capsys, monkeypatch, tmp_path):
    """If ntfy push fails, the logged exception MUST NOT include the
    topic (which would live in the URL and grant log-readers subscribe
    access)."""
    if "notify" in sys.modules:
        del sys.modules["notify"]
    import notify
    monkeypatch.setattr(notify, "NTFY_TOPIC", "secret-topic-abc123")
    monkeypatch.setattr(notify, "NTFY_URL", "https://ntfy.sh/secret-topic-abc123")

    import urllib.request
    def _fail(*a, **k):
        # Simulate an exception whose str() includes the URL
        raise Exception("HTTP 500 on https://ntfy.sh/secret-topic-abc123")
    monkeypatch.setattr(urllib.request, "urlopen", _fail)

    notify.send_notification("hello", "info")
    out = capsys.readouterr().out
    assert "secret-topic-abc123" not in out, (
        "REGRESSION: ntfy topic leaked in error log — log readers can "
        "subscribe to the user's alerts."
    )


# ---------- aria-sort (cosmetic dashboard fix) ----------


def test_dashboard_has_aria_sort_helper():
    """The ariaSort helper must exist so screen-reader users can tell
    which column is currently sorted and in which direction."""
    dashboard = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates/dashboard.html"
    )).read()
    assert "function ariaSort(col)" in dashboard
    # Every sortable <th> references it
    import re
    sortable_lines = [line for line in dashboard.split("\n")
                      if 'class="sortable"' in line
                      and "onclick=\"sortScreener" in line]
    for line in sortable_lines:
        assert 'aria-sort="' in line, f"sortable header missing aria-sort: {line[:100]}"


# ---------- smart_orders blended cost basis ----------


def test_smart_orders_module_has_blended_helper_logic():
    """Verify the blended-cost-basis code path is present in smart_orders."""
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "smart_orders.py"
    )).read()
    # The blended formula must exist in both buy + sell paths
    assert src.count("_smart_limit_fill_price") >= 2
    assert src.count("_smart_market_fill_price") >= 2
    assert src.count("_round_cent_float(blended_d)") >= 2
