"""Round-61 pt.37 — backtest data layer (OHLCV cache + fetcher) coverage.

Pure-helper-style tests for the cache I/O + cache-freshness logic +
public fetch_bars/fetch_bars_for_symbols/universe_from_journal/
universe_from_dashboard_data. The real yfinance path is bypassed via
the ``fetcher`` parameter — tests inject fakes.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from backtest_data import (
    CACHE_DIRNAME,
    CACHE_TTL_HOURS,
    fetch_bars,
    fetch_bars_for_symbols,
    is_cache_fresh,
    load_cached_bars,
    save_cached_bars,
    universe_from_dashboard_data,
    universe_from_journal,
)


def _bar(date, c=100):
    return {"date": date, "open": c, "high": c + 1, "low": c - 1,
             "close": c, "volume": 1_000_000}


def _bars(n=30):
    return [_bar(f"2026-04-{i+1:02d}", 100 + i) for i in range(n)]


# ============================================================================
# Cache I/O round-trip
# ============================================================================

def test_save_then_load_round_trip(tmp_path):
    bars = _bars(5)
    save_cached_bars(str(tmp_path), "AAPL", bars)
    loaded = load_cached_bars(str(tmp_path), "AAPL")
    assert loaded is not None
    assert loaded["symbol"] == "AAPL"
    assert loaded["bars"] == bars
    assert "fetched_at" in loaded


def test_load_returns_none_for_missing_symbol(tmp_path):
    assert load_cached_bars(str(tmp_path), "NEVERSEEN") is None


def test_save_uppercases_symbol_in_payload(tmp_path):
    save_cached_bars(str(tmp_path), "aapl", _bars(3))
    loaded = load_cached_bars(str(tmp_path), "aapl")
    assert loaded["symbol"] == "AAPL"


def test_save_creates_cache_dir_if_missing(tmp_path):
    """First save should create the per-instance cache subdir."""
    cache_dir = os.path.join(str(tmp_path), CACHE_DIRNAME)
    assert not os.path.exists(cache_dir)
    save_cached_bars(str(tmp_path), "X", _bars(2))
    assert os.path.exists(cache_dir)


def test_load_returns_none_on_corrupt_cache_file(tmp_path):
    """Garbage JSON in the cache file shouldn't crash callers."""
    cache_dir = os.path.join(str(tmp_path), CACHE_DIRNAME)
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "X.json"), "w") as f:
        f.write("{not valid json")
    assert load_cached_bars(str(tmp_path), "X") is None


def test_save_is_atomic(tmp_path, monkeypatch):
    """A failure mid-write must NOT leave a corrupt .json on disk —
    atomic write via tempfile + os.rename. Crashes during the write
    leave the OLD file (or no file) intact, never a partial."""
    save_cached_bars(str(tmp_path), "AAPL", _bars(3))
    original = load_cached_bars(str(tmp_path), "AAPL")
    # Force os.rename to fail; the new content should not land
    import backtest_data as bd
    real_rename = bd.os.rename

    def _fail(*a, **kw):
        raise OSError("simulated rename failure")
    bd.os.rename = _fail
    try:
        try:
            save_cached_bars(str(tmp_path), "AAPL", _bars(7))
        except OSError:
            pass
    finally:
        bd.os.rename = real_rename
    after = load_cached_bars(str(tmp_path), "AAPL")
    # The OLD content survives intact
    assert after["bars"] == original["bars"]


# ============================================================================
# is_cache_fresh
# ============================================================================

def test_is_cache_fresh_returns_false_for_none():
    assert is_cache_fresh(None) is False


def test_is_cache_fresh_returns_false_for_missing_fetched_at():
    assert is_cache_fresh({"bars": []}) is False


def test_is_cache_fresh_returns_true_for_recent_fetch():
    fetched = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    entry = {"fetched_at": fetched, "bars": []}
    assert is_cache_fresh(entry, ttl_hours=12) is True


def test_is_cache_fresh_returns_false_when_past_ttl():
    fetched = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    entry = {"fetched_at": fetched, "bars": []}
    assert is_cache_fresh(entry, ttl_hours=12) is False


def test_is_cache_fresh_returns_false_for_unparseable_timestamp():
    assert is_cache_fresh({"fetched_at": "garbage", "bars": []}) is False


# ============================================================================
# fetch_bars (cache-first with injected fetcher)
# ============================================================================

def test_fetch_bars_uses_cache_when_fresh(tmp_path):
    """Cached entry within TTL → no fetcher call."""
    save_cached_bars(str(tmp_path), "AAPL", _bars(40))
    fetcher_calls = []
    def _fetcher(sym, days):
        fetcher_calls.append((sym, days))
        return _bars(40)
    bars = fetch_bars(str(tmp_path), "AAPL", days=30, fetcher=_fetcher)
    assert bars is not None
    assert len(bars) == 30
    assert fetcher_calls == []  # cache satisfied the request


def test_fetch_bars_calls_fetcher_when_cache_stale(tmp_path):
    """Stale cache → refetch."""
    cache_dir = os.path.join(str(tmp_path), CACHE_DIRNAME)
    os.makedirs(cache_dir, exist_ok=True)
    stale = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with open(os.path.join(cache_dir, "AAPL.json"), "w") as f:
        json.dump({
            "symbol": "AAPL", "fetched_at": stale,
            "bars": _bars(40),
        }, f)
    fetcher_calls = []
    def _fetcher(sym, days):
        fetcher_calls.append((sym, days))
        return _bars(40)
    fetch_bars(str(tmp_path), "AAPL", days=30,
                ttl_hours=12, fetcher=_fetcher)
    assert fetcher_calls == [("AAPL", 30)]


def test_fetch_bars_calls_fetcher_when_no_cache(tmp_path):
    fetcher_calls = []
    def _fetcher(sym, days):
        fetcher_calls.append((sym, days))
        return _bars(30)
    fetch_bars(str(tmp_path), "MSFT", days=30, fetcher=_fetcher)
    assert fetcher_calls == [("MSFT", 30)]


def test_fetch_bars_force_refresh_bypasses_cache(tmp_path):
    save_cached_bars(str(tmp_path), "AAPL", _bars(40))
    fetcher_calls = []
    def _fetcher(sym, days):
        fetcher_calls.append((sym, days))
        return _bars(40)
    fetch_bars(str(tmp_path), "AAPL", days=30,
                force_refresh=True, fetcher=_fetcher)
    assert fetcher_calls == [("AAPL", 30)]


def test_fetch_bars_returns_stale_cache_on_network_failure(tmp_path):
    """If the fetcher returns None (network down), we fall back to
    the cached bars even if past TTL — better stale than empty."""
    cache_dir = os.path.join(str(tmp_path), CACHE_DIRNAME)
    os.makedirs(cache_dir, exist_ok=True)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    with open(os.path.join(cache_dir, "X.json"), "w") as f:
        json.dump({"symbol": "X", "fetched_at": stale,
                    "bars": _bars(20)}, f)
    bars = fetch_bars(str(tmp_path), "X", days=20, ttl_hours=12,
                       fetcher=lambda s, d: None)
    assert bars is not None
    assert len(bars) == 20


def test_fetch_bars_returns_none_when_no_cache_and_fetcher_fails(tmp_path):
    bars = fetch_bars(str(tmp_path), "NEW", days=30,
                       fetcher=lambda s, d: None)
    assert bars is None


def test_fetch_bars_refetches_when_cached_has_too_few_bars(tmp_path):
    """Cache with 5 bars but request is for 30 → must refetch."""
    save_cached_bars(str(tmp_path), "AAPL", _bars(5))
    fetcher_calls = []
    def _fetcher(sym, days):
        fetcher_calls.append((sym, days))
        return _bars(40)
    bars = fetch_bars(str(tmp_path), "AAPL", days=30, fetcher=_fetcher)
    assert fetcher_calls == [("AAPL", 30)]
    assert len(bars) == 30


def test_fetch_bars_returns_only_last_n_bars(tmp_path):
    """Cache may have MORE bars than requested; trim to last `days`."""
    save_cached_bars(str(tmp_path), "AAPL", _bars(50))
    bars = fetch_bars(str(tmp_path), "AAPL", days=10,
                       fetcher=lambda s, d: _bars(50))
    assert len(bars) == 10
    # Should be the LAST 10 bars (most recent)
    assert bars[-1]["date"] == "2026-04-50"


# ============================================================================
# fetch_bars_for_symbols
# ============================================================================

def test_fetch_bars_for_symbols_returns_dict(tmp_path):
    fake = lambda sym, d: _bars(d)
    out = fetch_bars_for_symbols(
        str(tmp_path), ["AAPL", "MSFT"], days=15, fetcher=fake)
    assert set(out.keys()) == {"AAPL", "MSFT"}
    assert len(out["AAPL"]) == 15
    assert len(out["MSFT"]) == 15


def test_fetch_bars_for_symbols_handles_empty_input(tmp_path):
    assert fetch_bars_for_symbols(str(tmp_path), [], fetcher=lambda *a: None) == {}
    assert fetch_bars_for_symbols(str(tmp_path), None, fetcher=lambda *a: None) == {}


def test_fetch_bars_for_symbols_skips_falsy_entries(tmp_path):
    out = fetch_bars_for_symbols(
        str(tmp_path), ["AAPL", "", None, "MSFT"], days=5,
        fetcher=lambda s, d: _bars(5))
    assert set(out.keys()) == {"AAPL", "MSFT"}


def test_fetch_bars_for_symbols_returns_none_for_failed_symbol(tmp_path):
    """Per-symbol fetch failures don't break the bulk call — that
    symbol just maps to None."""
    def _fetcher(sym, d):
        if sym == "BAD":
            return None
        return _bars(5)
    out = fetch_bars_for_symbols(
        str(tmp_path), ["AAPL", "BAD"], days=5, fetcher=_fetcher)
    assert out["AAPL"] is not None
    assert out["BAD"] is None


# ============================================================================
# universe_from_journal
# ============================================================================

def test_universe_from_journal_dedups_and_sorts():
    journal = {"trades": [
        {"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "AAPL"},
    ]}
    assert universe_from_journal(journal) == ["AAPL", "MSFT"]


def test_universe_from_journal_resolves_occ_options():
    """OCC option contracts should resolve to the underlying ticker
    (round-22 multi-contract pattern)."""
    journal = {"trades": [
        {"symbol": "HIMS260508P00027000"},
        {"symbol": "DKNG260515C00021000"},
        {"symbol": "AAPL"},
    ]}
    assert universe_from_journal(journal) == ["AAPL", "DKNG", "HIMS"]


def test_universe_from_journal_handles_empty_or_missing():
    assert universe_from_journal(None) == []
    assert universe_from_journal({}) == []
    assert universe_from_journal({"trades": []}) == []


def test_universe_from_journal_skips_blank_symbols():
    journal = {"trades": [
        {"symbol": ""}, {"symbol": None}, {"symbol": "AAPL"},
    ]}
    assert universe_from_journal(journal) == ["AAPL"]


def test_universe_from_journal_uppercases():
    journal = {"trades": [{"symbol": "aapl"}, {"symbol": "Msft"}]}
    assert universe_from_journal(journal) == ["AAPL", "MSFT"]


# ============================================================================
# universe_from_dashboard_data
# ============================================================================

def test_universe_from_dashboard_picks_extracts_symbols():
    data = {"picks": [
        {"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "GOOG"},
    ]}
    universe = universe_from_dashboard_data(data)
    assert "AAPL" in universe
    assert "GOOG" in universe


def test_universe_from_dashboard_dedups():
    data = {"picks": [{"symbol": "AAPL"}, {"symbol": "AAPL"}]}
    assert universe_from_dashboard_data(data) == ["AAPL"]


def test_universe_from_dashboard_empty():
    assert universe_from_dashboard_data(None) == []
    assert universe_from_dashboard_data({}) == []
    assert universe_from_dashboard_data({"picks": []}) == []


def test_universe_from_dashboard_preserves_order():
    """Dashboard picks are pre-sorted by score; preserve that ordering
    so the backtest evaluates the highest-scored symbols first."""
    data = {"picks": [
        {"symbol": "TSLA"}, {"symbol": "AAPL"}, {"symbol": "NVDA"},
    ]}
    assert universe_from_dashboard_data(data) == ["TSLA", "AAPL", "NVDA"]


# ============================================================================
# Constants pinned
# ============================================================================

def test_cache_ttl_default_is_12_hours():
    assert CACHE_TTL_HOURS == 12


def test_cache_dirname_constant():
    assert CACHE_DIRNAME == "backtest_cache"
