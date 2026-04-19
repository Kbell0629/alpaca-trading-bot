#!/usr/bin/env python3
"""
yfinance_budget.py — Cross-module rate-limit + retry helper for yfinance.

Round-11 hardening. Multiple factor modules (market_breadth, factor_
enrichment, quality_filter, iv_rank) each hit yfinance in their own
refresh path. If they all trigger fresh on the same screener run
(e.g. caches expired during a Railway redeploy), we can blast 150+
requests in a few seconds and get silently throttled by Yahoo.

This module provides:
  - A global semaphore-style budget shared across the process
  - Exponential-backoff retry on transient errors
  - A circuit breaker that trips after N consecutive failures
    so we don't waste time hammering an unreachable endpoint

Callers opt in by wrapping their yfinance call:

    from yfinance_budget import yf_download, yf_ticker_info

    data = yf_download(tickers="AAPL MSFT", period="3mo", ...)
    info = yf_ticker_info("AAPL")

Rate-limit targets ~30 requests per 60s window, which empirically
stays under Yahoo's throttling threshold for anonymous access.
"""
from __future__ import annotations
import threading
import time
from collections import deque


# --- Configuration ----------------------------------------------------------
# Rate limit: max N requests per WINDOW_SECONDS
MAX_REQUESTS_PER_WINDOW = 30
WINDOW_SECONDS = 60
# Retry policy
MAX_RETRIES = 3
INITIAL_BACKOFF = 2.0  # seconds
# Circuit breaker
CB_FAILURE_THRESHOLD = 6     # consecutive fails to trip
CB_COOL_OFF_SECONDS = 300    # 5 min lockout after trip

# --- Internal state --------------------------------------------------------
_lock = threading.Lock()
_request_times = deque()         # monotonic timestamps of recent requests
_cb_failures = 0                 # consecutive failure counter
_cb_tripped_until = 0.0          # 0 if healthy; monotonic-time else


def _now():
    return time.monotonic()


def _wait_for_slot():
    """Block until we have a rate-limit slot available. Prunes expired
    timestamps from the sliding window first."""
    while True:
        with _lock:
            now = _now()
            # Drop timestamps outside the window
            while _request_times and now - _request_times[0] >= WINDOW_SECONDS:
                _request_times.popleft()
            if len(_request_times) < MAX_REQUESTS_PER_WINDOW:
                _request_times.append(now)
                return
            # Else sleep until oldest slot opens
            sleep_for = max(0.1, WINDOW_SECONDS - (now - _request_times[0]))
        time.sleep(min(sleep_for, 5.0))


def _circuit_open():
    """True if the circuit breaker is currently tripped."""
    with _lock:
        return _cb_tripped_until > _now()


def _record_success():
    global _cb_failures
    with _lock:
        _cb_failures = 0


def _record_failure():
    global _cb_failures, _cb_tripped_until
    with _lock:
        _cb_failures += 1
        if _cb_failures >= CB_FAILURE_THRESHOLD:
            _cb_tripped_until = _now() + CB_COOL_OFF_SECONDS
            _cb_failures = 0  # reset for next cycle


# Permanent errors — programming bugs or bad inputs. Retrying won't help
# and can mask the underlying issue. Everything else (network, HTTPError,
# JSONDecodeError, pandas parse errors) is treated as transient.
_PERMANENT_ERRORS = (ValueError, TypeError, AttributeError, KeyError)


def _report_failure(exc, fn_name):
    """Best-effort Sentry capture so systematic failures surface beyond
    stdout. Never raises — observability import can be missing in tests."""
    try:
        from observability import capture_exception
        capture_exception(exc, component="yfinance_budget", fn=fn_name)
    except Exception:
        pass


def _call_with_retry(fn, *args, **kwargs):
    """Execute fn with retry + backoff. Returns (result, None) on success
    or (None, error_msg) on exhausted retries / open circuit.

    Permanent errors (ValueError/TypeError/AttributeError/KeyError) bypass
    the retry loop — they indicate bad input or shape drift, not a
    transient upstream hiccup."""
    if _circuit_open():
        return None, "yfinance circuit breaker open (too many recent failures)"
    last_err = None
    fn_name = getattr(fn, "__name__", "unknown")
    for attempt in range(MAX_RETRIES + 1):
        _wait_for_slot()
        try:
            result = fn(*args, **kwargs)
            _record_success()
            return result, None
        except _PERMANENT_ERRORS as e:
            _record_failure()
            _report_failure(e, fn_name)
            return None, f"{type(e).__name__}: {e}"
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                backoff = INITIAL_BACKOFF * (2 ** attempt)
                time.sleep(backoff)
                continue
            _record_failure()
            _report_failure(e, fn_name)
            return None, last_err
    _record_failure()
    return None, last_err or "unknown yfinance error"


# --- Public wrappers -------------------------------------------------------

def yf_download(**kwargs):
    """Rate-limited yfinance.download(). Returns the DataFrame on success
    or None on failure. Caller checks for None and falls back gracefully."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    result, err = _call_with_retry(yf.download, **kwargs)
    if err:
        print(f"[yfinance_budget] download failed: {err}")
    return result


def yf_ticker_info(symbol):
    """Rate-limited yfinance.Ticker(symbol).info dict. Returns {} on
    failure. Many yfinance versions throw on .info for delisted tickers
    — we swallow + return {}."""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    def _fetch():
        t = yf.Ticker(symbol)
        return t.info or {}
    result, err = _call_with_retry(_fetch)
    if err:
        print(f"[yfinance_budget] {symbol} info failed: {err}")
        return {}
    return result or {}


def yf_history(symbol, **kwargs):
    """Rate-limited yfinance Ticker().history() DataFrame. Returns None
    on failure."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    def _fetch():
        t = yf.Ticker(symbol)
        return t.history(**kwargs)
    result, err = _call_with_retry(_fetch)
    if err:
        print(f"[yfinance_budget] {symbol} history failed: {err}")
        return None
    return result


def yf_splits(symbol):
    """Rate-limited yfinance split history. Returns a list of
    (datetime, ratio) tuples in chronological order, or [] on failure.

    Used by wheel_strategy to detect and auto-resolve stock splits that
    would otherwise be mis-attributed as assignment anomalies. Each
    ratio is the multiplier applied at that split (2.0 for a 2:1 split
    where shareholders end up with 2x the shares; 0.5 for a 1:2 reverse
    split)."""
    try:
        import yfinance as yf
    except ImportError:
        return []
    def _fetch():
        t = yf.Ticker(symbol)
        s = t.splits   # pandas Series indexed by Timestamp
        if s is None or (hasattr(s, "empty") and s.empty):
            return []
        return [(ts.to_pydatetime(), float(r)) for ts, r in s.items()]
    result, err = _call_with_retry(_fetch)
    if err:
        print(f"[yfinance_budget] {symbol} splits failed: {err}")
        return []
    return result or []


def stats():
    """Current budget state — useful for observability / health checks."""
    with _lock:
        return {
            "requests_in_window": len(_request_times),
            "window_limit": MAX_REQUESTS_PER_WINDOW,
            "window_seconds": WINDOW_SECONDS,
            "circuit_open": _cb_tripped_until > _now(),
            "circuit_trip_until_monotonic": _cb_tripped_until,
            "consecutive_failures": _cb_failures,
        }


if __name__ == "__main__":
    import json
    print("Stats:", json.dumps(stats(), indent=2))
    df = yf_download(tickers="SPY", period="5d", interval="1d", progress=False)
    print(f"SPY bars: {len(df) if df is not None else 'None'}")
    print("Stats after:", json.dumps(stats(), indent=2))
