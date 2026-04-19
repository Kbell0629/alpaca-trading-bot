"""
Round-13 audit math + peripheral fixes.

Covers:
  * news_scanner score cap ±15
  * llm_sentiment malformed flag
  * economic_calendar FOMC 2027
  * capitol_trades hard-fail when disabled
  * iv_rank rate_limited flag
  * social_sentiment recency filter marks stale data
"""
from __future__ import annotations


# ---------- news_scanner per-article score cap ----------


def test_news_score_capped_positive():
    import news_scanner as ns
    # Force a headline that matches many bullish patterns
    art = {
        "headline": "acquired with record profit and upgraded to buy after beating earnings",
        "summary": "breakthrough deal expands partnership",
    }
    score, _ = ns.score_news_article(art)
    assert score <= 15, f"expected cap at 15, got {score}"


def test_news_score_capped_negative():
    import news_scanner as ns
    art = {
        "headline": "bankruptcy lawsuit probe fraud SEC charges investigation",
        "summary": "missed earnings downgraded layoffs downgrade missed",
    }
    score, _ = ns.score_news_article(art)
    assert score >= -15, f"expected floor at -15, got {score}"


def test_news_score_moderate_unaffected():
    import news_scanner as ns
    art = {"headline": "upgraded to buy", "summary": ""}
    score, _ = ns.score_news_article(art)
    assert -15 <= score <= 15


# ---------- llm_sentiment malformed flag ----------


def test_llm_parse_malformed_on_empty():
    import llm_sentiment as llm
    out = llm._parse_response("")
    assert out["malformed"] is True
    assert out["score"] == 0


def test_llm_parse_malformed_on_no_json():
    import llm_sentiment as llm
    out = llm._parse_response("I think this is bullish but who knows")
    assert out["malformed"] is True


def test_llm_parse_malformed_on_bad_json():
    import llm_sentiment as llm
    out = llm._parse_response("{score: not-a-number,}")
    assert out["malformed"] is True


def test_llm_parse_not_malformed_on_valid():
    import llm_sentiment as llm
    out = llm._parse_response('{"score": 7, "reason": "good news"}')
    assert out["malformed"] is False
    assert out["score"] == 7


def test_llm_parse_handles_markdown_fences():
    import llm_sentiment as llm
    out = llm._parse_response('```json\n{"score": -5, "reason": "bad"}\n```')
    assert out["malformed"] is False
    assert out["score"] == -5


# ---------- economic_calendar FOMC 2027 ----------


def test_fomc_2027_dates_in_all_set():
    import economic_calendar as ec
    assert "2027-03-17" in ec.FOMC_DATES_ALL
    assert "2027-12-15" in ec.FOMC_DATES_ALL


def test_fomc_2026_still_covered():
    import economic_calendar as ec
    assert "2026-01-28" in ec.FOMC_DATES_ALL


# ---------- capitol_trades hard-fail when disabled ----------


def test_capitol_hard_fails_when_disabled(monkeypatch):
    import capitol_trades as ct
    import update_dashboard as ud
    monkeypatch.setattr(ud, "COPY_TRADING_ENABLED", False)
    out = ct.refresh_cache()
    assert out.get("disabled") is True
    assert "error" in out
    assert out["count"] == 0


def test_capitol_hard_fails_when_no_api_key(monkeypatch):
    import capitol_trades as ct
    import update_dashboard as ud
    monkeypatch.setattr(ud, "COPY_TRADING_ENABLED", True)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("QUIVER_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    out = ct.refresh_cache()
    assert out.get("disabled") is True
    assert "no provider key" in out.get("error", "")


# ---------- iv_rank rate_limited flag ----------


def test_iv_rank_flags_rate_limit(monkeypatch, tmp_path):
    import iv_rank as iv
    # Force the cache miss path
    monkeypatch.setattr("yfinance_budget.yf_history",
                        lambda *a, **k: None)
    out = iv.get_hv_rank_for_symbol("AAPL", data_dir=str(tmp_path))
    assert out.get("rate_limited") is True
    # Neutral fallback still 50 so dashboard renders
    assert out["hv_rank"] == 50.0


# ---------- social_sentiment recency ----------


def test_social_marks_stale_when_no_fresh_messages(monkeypatch):
    """All messages >30min old → stale=True + neutral."""
    import social_sentiment as ss
    import json
    from datetime import datetime, timezone, timedelta

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"messages": [
        {"created_at": old_ts, "entities": {"sentiment": {"basic": "Bullish"}}}
        for _ in range(10)
    ]}

    class _FakeResp:
        def __init__(self, data): self._d = data
        def read(self): return json.dumps(self._d).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def _fake_urlopen(req, timeout=10):
        return _FakeResp(payload)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    out = ss.get_stocktwits_sentiment("AAPL")
    assert out.get("stale") is True
    assert out["sentiment"] == "neutral"
    assert out["score"] == 0


def test_social_keeps_fresh_messages(monkeypatch):
    """≥5 fresh messages → sentiment computed normally."""
    import social_sentiment as ss
    import json
    from datetime import datetime, timezone

    fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"messages": [
        {"created_at": fresh_ts, "entities": {"sentiment": {"basic": "Bullish"}}}
        for _ in range(10)
    ]}

    class _FakeResp:
        def __init__(self, data): self._d = data
        def read(self): return json.dumps(self._d).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=10: _FakeResp(payload))

    out = ss.get_stocktwits_sentiment("AAPL")
    assert not out.get("stale")
    assert out["sentiment"] == "bullish"
