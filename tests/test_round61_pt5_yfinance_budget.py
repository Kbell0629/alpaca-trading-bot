"""
Round-61 pt.5: fill coverage gaps in yfinance_budget.py.

Existing tests in test_yfinance_budget_errors.py cover the retry/permanent
error branches of `_call_with_retry`. This file fills the remaining
gaps: the rate-limit slot window, circuit breaker lifecycle, public
wrappers (yf_download / yf_ticker_info / yf_history / yf_splits),
and the stats() helper.
"""
from __future__ import annotations

import sys
import time
import types

import pytest


def _reload(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    sys.modules.pop("yfinance_budget", None)
    import yfinance_budget as yb
    # Reset module state so each test starts clean
    yb._request_times.clear()
    yb._cb_failures = 0
    yb._cb_tripped_until = 0.0
    return yb


# ========= Rate-limit slot window =========

class TestWaitForSlot:
    def test_first_call_records_timestamp(self, monkeypatch):
        yb = _reload(monkeypatch)
        yb._wait_for_slot()
        assert len(yb._request_times) == 1

    def test_multiple_calls_fill_window(self, monkeypatch):
        yb = _reload(monkeypatch)
        for _ in range(5):
            yb._wait_for_slot()
        assert len(yb._request_times) == 5

    def test_expired_timestamps_pruned(self, monkeypatch):
        yb = _reload(monkeypatch)
        # Pre-populate with old timestamps
        old = time.monotonic() - yb.WINDOW_SECONDS - 5
        for _ in range(10):
            yb._request_times.append(old)
        # Next call should prune them all
        yb._wait_for_slot()
        assert len(yb._request_times) == 1, (
            "expired timestamps must be dropped from the sliding window")


# ========= Circuit breaker =========

class TestCircuitBreakerYb:
    def test_initial_closed(self, monkeypatch):
        yb = _reload(monkeypatch)
        assert yb._circuit_open() is False

    def test_threshold_failures_trip(self, monkeypatch):
        yb = _reload(monkeypatch)
        for _ in range(yb.CB_FAILURE_THRESHOLD):
            yb._record_failure()
        assert yb._circuit_open() is True

    def test_below_threshold_does_not_trip(self, monkeypatch):
        yb = _reload(monkeypatch)
        for _ in range(yb.CB_FAILURE_THRESHOLD - 1):
            yb._record_failure()
        assert yb._circuit_open() is False

    def test_success_resets_counter(self, monkeypatch):
        yb = _reload(monkeypatch)
        for _ in range(yb.CB_FAILURE_THRESHOLD - 1):
            yb._record_failure()
        yb._record_success()
        # One more failure must not trip (count was reset)
        yb._record_failure()
        assert yb._circuit_open() is False

    def test_cooldown_expires_closes_breaker(self, monkeypatch):
        yb = _reload(monkeypatch)
        for _ in range(yb.CB_FAILURE_THRESHOLD):
            yb._record_failure()
        # Force the trip-until into the past
        yb._cb_tripped_until = time.monotonic() - 1
        assert yb._circuit_open() is False

    def test_call_with_retry_short_circuits_on_open(self, monkeypatch):
        yb = _reload(monkeypatch)
        yb._cb_tripped_until = time.monotonic() + 100
        calls = []

        def should_not_run():
            calls.append(1)
            return "X"

        result, err = yb._call_with_retry(should_not_run)
        assert result is None
        assert "circuit breaker open" in err
        assert calls == []


# ========= stats() =========

class TestStats:
    def test_stats_snapshot_shape(self, monkeypatch):
        yb = _reload(monkeypatch)
        s = yb.stats()
        assert "requests_in_window" in s
        assert "window_limit" in s
        assert "window_seconds" in s
        assert "circuit_open" in s
        assert "consecutive_failures" in s

    def test_stats_tracks_request_count(self, monkeypatch):
        yb = _reload(monkeypatch)
        for _ in range(3):
            yb._wait_for_slot()
        assert yb.stats()["requests_in_window"] == 3

    def test_stats_reflects_circuit_state(self, monkeypatch):
        yb = _reload(monkeypatch)
        for _ in range(yb.CB_FAILURE_THRESHOLD):
            yb._record_failure()
        assert yb.stats()["circuit_open"] is True


# ========= Public wrappers with yfinance stubbed =========

def _install_fake_yfinance(monkeypatch):
    """Install a fake yfinance module so the wrappers can exercise
    their public-API plumbing without requiring a real network."""
    yf = types.ModuleType("yfinance")

    def fake_download(**kwargs):
        return {"mock": "df", "kwargs": kwargs}

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.info = {"regularMarketPrice": 100.0, "symbol": symbol}

        def history(self, **kwargs):
            return {"mock": "history", "symbol": self.symbol}

        @property
        def splits(self):
            import types as _t
            s = _t.SimpleNamespace(empty=True)
            return s

    yf.download = fake_download
    yf.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    return yf


class TestYfDownload:
    def test_happy_path_returns_result(self, monkeypatch):
        yb = _reload(monkeypatch)
        _install_fake_yfinance(monkeypatch)
        result = yb.yf_download(tickers="AAPL", period="3mo")
        assert result is not None
        assert result["kwargs"]["tickers"] == "AAPL"

    def test_missing_yfinance_returns_none(self, monkeypatch):
        yb = _reload(monkeypatch)
        monkeypatch.setitem(sys.modules, "yfinance", None)
        # With yfinance=None, the ImportError path runs
        # Actually setitem with None doesn't cause ImportError; we need
        # to simulate the raise. Easier: ensure yfinance isn't importable.
        monkeypatch.delitem(sys.modules, "yfinance", raising=False)
        # Force the import to fail
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "yfinance":
                raise ImportError("not installed")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert yb.yf_download(tickers="AAPL") is None

    def test_download_retry_exhaustion_returns_none(self, monkeypatch):
        yb = _reload(monkeypatch)
        yf = _install_fake_yfinance(monkeypatch)

        def always_fails(**kwargs):
            raise RuntimeError("yahoo down")

        yf.download = always_fails
        result = yb.yf_download(tickers="AAPL")
        assert result is None


class TestYfTickerInfo:
    def test_happy_path(self, monkeypatch):
        yb = _reload(monkeypatch)
        _install_fake_yfinance(monkeypatch)
        info = yb.yf_ticker_info("AAPL")
        assert info["symbol"] == "AAPL"

    def test_missing_yfinance_returns_empty_dict(self, monkeypatch):
        yb = _reload(monkeypatch)
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "yfinance":
                raise ImportError("not installed")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert yb.yf_ticker_info("AAPL") == {}

    def test_empty_info_returns_empty_dict(self, monkeypatch):
        yb = _reload(monkeypatch)
        yf = _install_fake_yfinance(monkeypatch)

        class EmptyTicker:
            def __init__(self, sym):
                self.info = {}

        yf.Ticker = EmptyTicker
        assert yb.yf_ticker_info("AAPL") == {}

    def test_fetch_failure_returns_empty(self, monkeypatch):
        yb = _reload(monkeypatch)
        yf = _install_fake_yfinance(monkeypatch)

        class BrokenTicker:
            def __init__(self, sym):
                raise RuntimeError("delisted")

        yf.Ticker = BrokenTicker
        assert yb.yf_ticker_info("AAPL") == {}


class TestYfHistory:
    def test_happy_path(self, monkeypatch):
        yb = _reload(monkeypatch)
        _install_fake_yfinance(monkeypatch)
        h = yb.yf_history("AAPL", period="1mo")
        assert h is not None
        assert h["symbol"] == "AAPL"

    def test_missing_yfinance_returns_none(self, monkeypatch):
        yb = _reload(monkeypatch)
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "yfinance":
                raise ImportError("not installed")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert yb.yf_history("AAPL") is None


class TestYfSplits:
    def test_empty_splits_returns_empty_list(self, monkeypatch):
        yb = _reload(monkeypatch)
        _install_fake_yfinance(monkeypatch)
        splits = yb.yf_splits("AAPL")
        assert splits == []

    def test_missing_yfinance_returns_empty_list(self, monkeypatch):
        yb = _reload(monkeypatch)
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "yfinance":
                raise ImportError("not installed")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert yb.yf_splits("AAPL") == []

    def test_splits_with_data(self, monkeypatch):
        """If yfinance returns actual split data, yf_splits must
        convert to a list of (datetime, ratio) tuples."""
        yb = _reload(monkeypatch)
        yf = _install_fake_yfinance(monkeypatch)
        from datetime import datetime

        class DtLike:
            def __init__(self, dt):
                self._dt = dt
            def to_pydatetime(self):
                return self._dt

        class FakeSplitsSeries:
            empty = False
            def __init__(self, items):
                self._items = items
            def items(self):
                return iter(self._items)

        class TickerWithSplits:
            def __init__(self, sym):
                self.splits = FakeSplitsSeries([
                    (DtLike(datetime(2020, 8, 31)), 4.0),  # AAPL 4:1
                ])

        yf.Ticker = TickerWithSplits
        result = yb.yf_splits("AAPL")
        assert len(result) == 1
        assert result[0][1] == 4.0
