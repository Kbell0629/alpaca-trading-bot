"""
Exception-handling hardening — round follow-up to PR #21.

Previous rounds fixed yfinance_budget._call_with_retry and
wheel_strategy._detect_split_since. These tests pin the next set of
fixes — sites where a broad `except Exception: pass` or bare `except:`
was swallowing shape-drift / disk / permission errors without any
observability hook.

Covered:
  * llm_sentiment._write_cache — narrow catch + Sentry route
  * insider_signals._write_cache — narrow catch + Sentry route
  * insider_signals._read_cache — narrow catch (was catching all Exception)
  * smart_orders._dec — narrow catch preserves InvalidOperation handling
  * smart_orders._get_quote — shape drift routes to observability
  * social_sentiment recency filter — shape drift routes to observability
  * capital_check.safe_save_json — narrow bare except (no longer swallows
    KeyboardInterrupt / SystemExit)
  * notify.safe_save_json — same
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from decimal import Decimal

import pytest


# ---------- llm_sentiment._write_cache ----------


def test_llm_sentiment_write_cache_routes_os_error_to_observability(monkeypatch, tmp_path):
    """Disk-full / permission errors used to be `except Exception: pass`.
    Now they route through observability so Sentry sees systematic cache
    breakage."""
    import llm_sentiment

    captured = []
    def _fake_capture(exc, **ctx):
        captured.append((type(exc).__name__, ctx))
    fake_obs = type(sys)("observability")
    fake_obs.capture_exception = _fake_capture
    monkeypatch.setitem(sys.modules, "observability", fake_obs)

    # Force the rename to fail with OSError
    def _raise_os(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(llm_sentiment.os, "rename", _raise_os)

    path = os.path.join(str(tmp_path), "cache.json")
    # Must not raise — cache write failures are non-fatal
    llm_sentiment._write_cache(path, {"ok": 1})

    assert captured, "OSError was not routed through capture_exception"
    assert captured[0][0] == "OSError"
    assert captured[0][1].get("component") == "llm_sentiment_cache_write"


def test_llm_sentiment_write_cache_routes_type_error(monkeypatch, tmp_path):
    """Non-JSON-serialisable payload used to be silently dropped. Now
    routes through observability so we notice the upstream shape drift."""
    import llm_sentiment

    captured = []
    def _fake_capture(exc, **ctx):
        captured.append((type(exc).__name__, ctx))
    fake_obs = type(sys)("observability")
    fake_obs.capture_exception = _fake_capture
    monkeypatch.setitem(sys.modules, "observability", fake_obs)

    path = os.path.join(str(tmp_path), "cache.json")
    # A set is not JSON-serialisable; json.dump raises TypeError
    llm_sentiment._write_cache(path, {"bad": {1, 2, 3}})
    assert captured, "TypeError was not routed through capture_exception"
    assert captured[0][0] == "TypeError"


def test_llm_sentiment_write_cache_happy_path(tmp_path):
    """Sanity check — the narrow catch doesn't interfere with the happy path."""
    import llm_sentiment
    path = os.path.join(str(tmp_path), "cache.json")
    llm_sentiment._write_cache(path, {"ok": 1})
    assert os.path.exists(path)
    with open(path) as f:
        assert json.load(f) == {"ok": 1}


# ---------- insider_signals._write_cache / _read_cache ----------


def test_insider_signals_write_cache_routes_to_observability(monkeypatch, tmp_path):
    import insider_signals

    monkeypatch.setattr(insider_signals, "_cache_dir", lambda: str(tmp_path))

    captured = []
    fake_obs = type(sys)("observability")
    fake_obs.capture_exception = lambda exc, **ctx: captured.append(
        (type(exc).__name__, ctx))
    monkeypatch.setitem(sys.modules, "observability", fake_obs)

    def _raise_os(*a, **kw):
        raise OSError("permission denied")
    monkeypatch.setattr(insider_signals.os, "rename", _raise_os)

    insider_signals._write_cache("TSLA", {"score": 5})
    assert captured
    assert captured[0][0] == "OSError"
    assert captured[0][1].get("component") == "insider_signals_cache_write"


def test_insider_signals_read_cache_returns_none_on_corrupt_json(tmp_path, monkeypatch):
    """Previously `except Exception: return None` would mask any bug.
    Now only the specific (OSError, json.JSONDecodeError) are caught —
    a real code bug inside the try block would propagate."""
    import insider_signals

    monkeypatch.setattr(insider_signals, "_cache_dir", lambda: str(tmp_path))
    # Write corrupt JSON
    path = insider_signals._cache_path("TSLA")
    with open(path, "w") as f:
        f.write("{this is not valid json")
    # Should return None via the narrow catch
    assert insider_signals._read_cache("TSLA") is None


# ---------- smart_orders._dec ----------


def test_smart_orders_dec_handles_invalid_operation():
    """Gibberish strings are normal user data — handled by the narrow catch."""
    import smart_orders
    assert smart_orders._dec("not-a-number") == Decimal("0")
    assert smart_orders._dec("not-a-number", default=Decimal("5")) == Decimal("5")


def test_smart_orders_dec_handles_none_and_empty():
    import smart_orders
    assert smart_orders._dec(None) == Decimal("0")
    assert smart_orders._dec("") == Decimal("0")


def test_smart_orders_dec_handles_numeric():
    import smart_orders
    assert smart_orders._dec(1.5) == Decimal("1.5")
    assert smart_orders._dec("2.25") == Decimal("2.25")
    assert smart_orders._dec(Decimal("3.0")) == Decimal("3.0")


def test_smart_orders_dec_handles_type_error():
    """Lists / dicts don't convert to Decimal — caught by the narrow
    (TypeError) case in the catch."""
    import smart_orders
    # str([1,2]) is "[1, 2]" which InvalidOperation-rejects → default.
    assert smart_orders._dec([1, 2]) == Decimal("0")
    assert smart_orders._dec({"x": 1}) == Decimal("0")


# ---------- smart_orders._get_quote ----------


def test_smart_orders_get_quote_routes_shape_drift_to_observability(monkeypatch):
    """If api_get returns an unexpected shape, the function should
    still return None (callers fall back to market order) AND route
    the exception through observability so we see the shape drift."""
    import smart_orders

    captured = []
    fake_obs = type(sys)("observability")
    fake_obs.capture_exception = lambda exc, **ctx: captured.append(
        (type(exc).__name__, ctx))
    monkeypatch.setitem(sys.modules, "observability", fake_obs)

    # Simulate a bad shape where q.get("bp") returns a dict instead of scalar
    def bad_api_get(url):
        return {"quote": {"bp": {"nested": "shape drift"}, "ap": 10.0}}

    result = smart_orders._get_quote(bad_api_get, "http://fake", "TSLA")
    assert result is None
    assert captured, "shape drift was not routed through capture_exception"
    assert captured[0][1].get("component") == "smart_orders_get_quote"
    assert captured[0][1].get("symbol") == "TSLA"


def test_smart_orders_get_quote_happy_path(monkeypatch):
    """Normal response still works."""
    import smart_orders
    def good_api_get(url):
        return {"quote": {"bp": 100.0, "ap": 100.05}}
    q = smart_orders._get_quote(good_api_get, "http://fake", "TSLA")
    assert q is not None
    assert q["bid"] == 100.0
    assert q["ask"] == 100.05


# ---------- social_sentiment recency filter ----------


def test_social_sentiment_recency_filter_reports_shape_drift(monkeypatch):
    """If StockTwits renames `created_at`, the recency filter would
    previously silently fall back to the raw list — masking the shape
    change. Now it routes through observability."""
    import social_sentiment

    captured = []
    fake_obs = type(sys)("observability")
    fake_obs.capture_exception = lambda exc, **ctx: captured.append(
        (type(exc).__name__, ctx))
    monkeypatch.setitem(sys.modules, "observability", fake_obs)

    # Simulate the StockTwits HTTP response where created_at is present
    # but datetime.now subtraction explodes (shape drift: the filter
    # block imports datetime, so we force an error by patching .now).
    class _FakeHTTPResponse:
        def __init__(self, payload):
            self._p = payload
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._p

    def _fake_urlopen(req, timeout=10):
        payload = json.dumps({
            "messages": [
                {"created_at": "2026-04-19T12:34:56Z", "entities": {}},
            ] * 10
        }).encode()
        return _FakeHTTPResponse(payload)
    monkeypatch.setattr(social_sentiment.urllib.request, "urlopen", _fake_urlopen)

    # Force the recency filter to raise by making datetime.now blow up
    import datetime as real_dt
    class _BoomDatetime(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            raise RuntimeError("time broke")
    monkeypatch.setattr(social_sentiment, "datetime", _BoomDatetime)

    result = social_sentiment.get_stocktwits_sentiment("TSLA")
    # Function should still return a normal result (falls back to raw list)
    assert result["source"] == "stocktwits"
    # But observability should have seen the shape drift
    assert captured, "recency-filter shape drift was not routed to Sentry"
    assert captured[0][1].get("component") == "stocktwits_recency_filter"


# ---------- safe_save_json bare-except → narrow ----------


def test_capital_check_safe_save_json_propagates_keyboard_interrupt(tmp_path, monkeypatch):
    """Previously `except: ... raise` caught KeyboardInterrupt + SystemExit
    and re-raised them. Narrowing to `except Exception` means signals no
    longer enter the cleanup branch — they propagate immediately, which
    is what we want for Ctrl-C / process shutdown."""
    import capital_check

    def _raise_ki(*a, **kw):
        raise KeyboardInterrupt("user ctrl-c")
    monkeypatch.setattr(capital_check.os, "rename", _raise_ki)

    path = os.path.join(str(tmp_path), "capital.json")
    with pytest.raises(KeyboardInterrupt):
        capital_check.safe_save_json(path, {"x": 1})


def test_notify_safe_save_json_propagates_system_exit(tmp_path, monkeypatch):
    import notify

    def _raise_se(*a, **kw):
        raise SystemExit(1)
    monkeypatch.setattr(notify.os, "rename", _raise_se)

    path = os.path.join(str(tmp_path), "queue.json")
    with pytest.raises(SystemExit):
        notify.safe_save_json(path, {"x": 1})


def test_notify_safe_save_json_still_cleans_up_tmp_on_error(tmp_path, monkeypatch):
    """The narrow catch must still perform the tmp-file unlink cleanup
    on the normal-exception path (disk full, JSON-encode error, etc.)."""
    import notify

    # Count mkstemp tmp files created
    originals = {}
    originals["rename"] = notify.os.rename
    unlinked = []
    real_unlink = notify.os.unlink
    def _tracking_unlink(p):
        unlinked.append(p)
        return real_unlink(p)
    monkeypatch.setattr(notify.os, "unlink", _tracking_unlink)

    def _raise_os(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(notify.os, "rename", _raise_os)

    path = os.path.join(str(tmp_path), "queue.json")
    with pytest.raises(OSError):
        notify.safe_save_json(path, {"x": 1})
    # Exactly one .tmp file was created + cleaned up
    assert len(unlinked) == 1
    assert unlinked[0].endswith(".tmp")
    assert not os.path.exists(unlinked[0])


# ---------- llm_sentiment cache self-heals on malformed entries ----------


def test_llm_sentiment_cache_skips_malformed_entries(monkeypatch, tmp_path):
    """Malformed cache entries (e.g. from a previous outage where Gemini
    returned just "```" because maxOutputTokens was too low) must NOT
    be served. They should be treated as a cache miss so the next call
    hits the fixed code path and overwrites them with a good response."""
    import llm_sentiment

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(llm_sentiment, "_cache_dir", lambda: str(tmp_path))

    # Pre-seed a malformed cache entry (simulating the old outage)
    import hashlib
    key = hashlib.sha1(b"gemini|Test headline|").hexdigest()
    cache_path = os.path.join(str(tmp_path), f"{key}.json")
    with open(cache_path, "w") as f:
        json.dump({
            "score": 0,
            "reasoning": "unparseable: ```",
            "provider": "gemini",
            "cached": False,
            "malformed": True,
        }, f)

    # Replace _call_gemini with a stub that returns a good response
    calls = []
    def _fake_gemini(prompt):
        calls.append(prompt)
        return '{"score": 7, "reason": "Positive earnings beat"}', None
    monkeypatch.setitem(llm_sentiment._PROVIDERS, "gemini", _fake_gemini)

    result = llm_sentiment.score_news("Test headline", "")
    # Should have called Gemini (cache was skipped) and returned the fresh result
    assert len(calls) == 1, "malformed cache entry was served instead of re-fetched"
    assert result["score"] == 7
    assert result["reasoning"] == "Positive earnings beat"
    assert result["malformed"] is False


def test_llm_sentiment_cache_serves_good_entries(monkeypatch, tmp_path):
    """Sanity check: well-formed cache entries are still served as before.
    We shouldn't invalidate every entry — only malformed ones."""
    import llm_sentiment

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(llm_sentiment, "_cache_dir", lambda: str(tmp_path))

    import hashlib
    key = hashlib.sha1(b"gemini|Good headline|").hexdigest()
    cache_path = os.path.join(str(tmp_path), f"{key}.json")
    with open(cache_path, "w") as f:
        json.dump({
            "score": 5, "reasoning": "Cached analysis",
            "provider": "gemini", "cached": False, "malformed": False,
        }, f)

    calls = []
    def _fake_gemini(prompt):
        calls.append(prompt)
        return '{"score": 1, "reason": "fresh"}', None
    monkeypatch.setitem(llm_sentiment._PROVIDERS, "gemini", _fake_gemini)

    result = llm_sentiment.score_news("Good headline", "")
    # Should NOT have re-called Gemini
    assert len(calls) == 0
    assert result["score"] == 5
    assert result["cached"] is True
