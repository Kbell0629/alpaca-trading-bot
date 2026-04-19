"""
Exception-handling hardening for yfinance_budget._call_with_retry.

The retry loop previously caught every `Exception` and retried, burning
the entire 3-attempt budget on permanent bugs (TypeError, KeyError from
upstream API shape changes). That wasted rate-limit slots and delayed
the error by ~14 seconds of backoff. These tests pin the new behaviour:

  * permanent errors bypass retry and return immediately
  * transient errors still retry with backoff
  * final failure routes through observability.capture_exception
"""
from __future__ import annotations

import yfinance_budget as yb


def _reset_state():
    yb._request_times.clear()
    yb._cb_failures = 0
    yb._cb_tripped_until = 0.0


def test_value_error_does_not_retry(monkeypatch):
    """ValueError is permanent — stop after the first attempt."""
    _reset_state()
    calls = []
    def _flaky():
        calls.append(1)
        raise ValueError("bad shape")
    result, err = yb._call_with_retry(_flaky)
    assert result is None
    assert "ValueError" in err
    assert len(calls) == 1, f"permanent error retried: {len(calls)} calls"


def test_type_error_does_not_retry(monkeypatch):
    _reset_state()
    calls = []
    def _broken():
        calls.append(1)
        raise TypeError("NoneType has no .keys()")
    result, err = yb._call_with_retry(_broken)
    assert result is None
    assert "TypeError" in err
    assert len(calls) == 1


def test_key_error_does_not_retry():
    _reset_state()
    calls = []
    def _missing():
        calls.append(1)
        raise KeyError("regularMarketPrice")
    result, err = yb._call_with_retry(_missing)
    assert result is None
    assert len(calls) == 1


def test_transient_error_retries(monkeypatch):
    """Network-ish error (ConnectionError) should go through the full
    retry loop."""
    _reset_state()
    # Make backoff near-zero so the test runs fast
    monkeypatch.setattr(yb, "INITIAL_BACKOFF", 0.001)
    calls = []
    def _net_err():
        calls.append(1)
        raise ConnectionError("name resolution failed")
    result, err = yb._call_with_retry(_net_err)
    assert result is None
    # MAX_RETRIES=3 => 4 total attempts (range(MAX_RETRIES + 1))
    assert len(calls) == yb.MAX_RETRIES + 1


def test_transient_recovers_on_second_attempt(monkeypatch):
    _reset_state()
    monkeypatch.setattr(yb, "INITIAL_BACKOFF", 0.001)
    calls = []
    def _flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("transient")
        return "ok"
    result, err = yb._call_with_retry(_flaky)
    assert result == "ok"
    assert err is None
    assert len(calls) == 2


def test_permanent_error_reports_to_observability(monkeypatch):
    """On permanent error, capture_exception is invoked."""
    _reset_state()
    captured = []
    def _fake_capture(exc, **ctx):
        captured.append((exc, ctx))
    # Stub the observability module that _report_failure imports lazily
    import sys, types
    fake = types.ModuleType("observability")
    fake.capture_exception = _fake_capture
    monkeypatch.setitem(sys.modules, "observability", fake)

    def _bad():
        raise ValueError("x")
    result, err = yb._call_with_retry(_bad)
    assert result is None
    assert len(captured) == 1
    exc, ctx = captured[0]
    assert isinstance(exc, ValueError)
    assert ctx.get("component") == "yfinance_budget"


def test_transient_final_failure_reports_to_observability(monkeypatch):
    _reset_state()
    monkeypatch.setattr(yb, "INITIAL_BACKOFF", 0.001)
    captured = []
    import sys, types
    fake = types.ModuleType("observability")
    fake.capture_exception = lambda exc, **ctx: captured.append((exc, ctx))
    monkeypatch.setitem(sys.modules, "observability", fake)

    def _down():
        raise ConnectionError("nope")
    yb._call_with_retry(_down)
    # Only reported once — on final failure, not each retry
    assert len(captured) == 1
    assert isinstance(captured[0][0], ConnectionError)


def test_observability_import_failure_does_not_break_caller(monkeypatch):
    """If observability itself blows up, the retry loop must still
    return a clean (None, err) tuple to the caller."""
    _reset_state()
    import sys, types
    fake = types.ModuleType("observability")
    def _broken_capture(exc, **ctx):
        raise RuntimeError("sentry sdk corrupted")
    fake.capture_exception = _broken_capture
    monkeypatch.setitem(sys.modules, "observability", fake)

    def _bad():
        raise ValueError("x")
    # Should return gracefully even though capture_exception throws
    result, err = yb._call_with_retry(_bad)
    assert result is None
    assert "ValueError" in err


def test_circuit_breaker_short_circuits_before_calling_fn():
    _reset_state()
    yb._cb_tripped_until = yb._now() + 60  # open for 60s
    calls = []
    def _should_not_run():
        calls.append(1)
    result, err = yb._call_with_retry(_should_not_run)
    assert result is None
    assert "circuit breaker open" in err
    assert calls == []
    _reset_state()
