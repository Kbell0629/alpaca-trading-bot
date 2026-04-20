#!/usr/bin/env python3
"""
llm_sentiment.py — LLM-powered news sentiment scoring (multi-provider).

Round-11 expansion (item 8). The current bullish_news_bonus uses
keyword matching which catches obvious cases ("upgraded", "beats")
but misses nuance like "Apple chips lawsuit dismissed" (positive
despite "lawsuit"). LLM-as-judge gives us context-aware scoring at
~$0.01-0.05/day for our volume.

Provider auto-detection (set whichever env var):
  GEMINI_API_KEY  → Google Gemini 1.5 Flash (cheapest: $0.075/1M in)  [DEFAULT]
  OPENAI_API_KEY  → GPT-4o-mini ($0.15/1M in, $0.60/1M out)
  GROQ_API_KEY    → Llama 3.1 70B (FREE TIER, fast)
  ANTHROPIC_API_KEY → Claude Haiku ($0.80/1M in)

Public API:

    score_news(headline, summary=None, symbol=None) -> dict
        Returns {score: int -10..+10, reasoning: str, provider: str,
                 cached: bool, error?: str}
        Higher = more bullish for the stock. Cached 1h per
        (provider, headline-hash).

    score_batch(items) -> list[dict]
        Each item: {headline, summary?, symbol?}. Returns list of
        score dicts in the same order. Sequential calls (LLM
        rate limits make parallel risky).

    estimate_daily_cost() -> dict
        Returns {provider, cost_per_1k_calls, today_call_count} for
        the dashboard's Factor Health panel.

Falls back gracefully:
  - No API key → returns {score: 0, error: "no LLM provider"}
  - Rate-limited → cached result if any, else 0
  - Provider error → 0 score with error reason
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


# Provider preference: Gemini Flash (cheapest paid), then Groq (free),
# then GPT-4o-mini, then Claude Haiku.
def _detect_provider():
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _cache_dir():
    base = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "llm_sentiment_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_key(provider, headline, summary):
    h = hashlib.sha1((provider + "|" + (headline or "") + "|" + (summary or "")).encode()).hexdigest()
    return os.path.join(_cache_dir(), f"{h}.json")


def _read_cache(path, max_age_seconds=3600):
    try:
        if not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > max_age_seconds:
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path, data):
    # Narrow catch so code bugs (TypeError on non-serialisable payload)
    # still surface. Disk / permission errors route through observability
    # so we notice systematic cache breakage instead of silently re-running
    # the LLM every call.
    tmp = None
    try:
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.rename(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass
        try:
            from observability import capture_exception
            capture_exception(e, component="llm_sentiment_cache_write")
        except ImportError:
            pass


def _bump_call_counter():
    """Track today's call count for the cost-estimate display.

    Uses ET to match the rest of the bot's timeline — otherwise the
    "today" boundary jumps when the system clock crosses midnight UTC,
    making the dashboard cost estimate look noisy across midnight ET."""
    try:
        path = os.path.join(_cache_dir(), "_counter.json")
        try:
            from et_time import now_et
            today = now_et().strftime("%Y-%m-%d")
        except Exception:
            today = datetime.now().strftime("%Y-%m-%d")
        data = {}
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f) or {}
        if data.get("date") != today:
            data = {"date": today, "count": 0}
        data["count"] = int(data.get("count", 0)) + 1
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _build_prompt(headline, summary, symbol):
    sym_str = f" for {symbol}" if symbol else ""
    body = headline or ""
    if summary:
        body += "\n\nSummary: " + summary
    return f"""Score the following news headline{sym_str} for stock-price impact.

Output ONLY a JSON object: {{"score": <int from -10 to +10>, "reason": "<one short sentence>"}}

Scale:
  +10 = strongly bullish (FDA approval, contract win, big buyout)
  +5  = mildly bullish (analyst upgrade, beat estimates)
  0   = neutral (general news, mixed signals)
  -5  = mildly bearish (downgrade, missed earnings)
  -10 = strongly bearish (fraud, bankruptcy, halt)

Consider context — "lawsuit dismissed" is bullish despite "lawsuit".

News:
{body}"""


def _call_gemini(prompt):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None, "GEMINI_API_KEY not set"
    # Round-21: gemini-1.5-flash was deprecated → Google's API returns
    # 404 on that model name. Switched to gemini-2.0-flash (stable,
    # same pricing tier). Model is configurable via env var so a future
    # deprecation doesn't require a code change — operator can set
    # GEMINI_MODEL=gemini-2.5-flash when they want to upgrade.
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 100},
    }).encode()
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            text = (data.get("candidates", [{}])[0]
                    .get("content", {}).get("parts", [{}])[0].get("text", ""))
            return text, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, str(e)


def _call_openai(prompt):
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None, "OPENAI_API_KEY not set"
    body = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 80,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return text, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, str(e)


def _call_groq(prompt):
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return None, "GROQ_API_KEY not set"
    body = json.dumps({
        "model": "llama-3.1-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 80,
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return text, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, str(e)


def _call_anthropic(prompt):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, "ANTHROPIC_API_KEY not set"
    body = json.dumps({
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            text = data.get("content", [{}])[0].get("text", "")
            return text, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, str(e)


_PROVIDERS = {
    "gemini": _call_gemini,
    "openai": _call_openai,
    "groq": _call_groq,
    "anthropic": _call_anthropic,
}


def _parse_response(text):
    """Extract {score, reason, malformed} from the LLM response. Tolerates
    markdown code fences and extra prose around the JSON object.

    When the response can't be parsed as expected JSON, we set
    malformed=True so callers can distinguish "LLM said score=0 because
    news was neutral" from "LLM replied gibberish and we coerced to 0".
    Malformed responses are also logged for telemetry."""
    if not text:
        log.warning("llm_sentiment empty response")
        return {"score": 0, "reason": "empty response", "malformed": True}
    # Strip markdown code fences if present
    s = text.strip().lstrip("`").rstrip("`")
    s = s.replace("json\n", "").replace("JSON\n", "")
    # Find the first {...} block
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < 0:
        log.warning("llm_sentiment no JSON block",
                    extra={"text_head": text[:120]})
        return {"score": 0, "reason": f"unparseable: {text[:80]}",
                "malformed": True}
    try:
        obj = json.loads(s[start:end + 1])
        score = int(obj.get("score", 0))
        score = max(-10, min(10, score))
        return {"score": score, "reason": str(obj.get("reason", ""))[:200],
                "malformed": False}
    except (ValueError, json.JSONDecodeError, TypeError) as e:
        log.warning("llm_sentiment JSON parse failed",
                    extra={"error": str(e), "text_head": text[:120]})
        return {"score": 0, "reason": f"bad JSON: {text[:80]}",
                "malformed": True}


def score_news(headline, summary=None, symbol=None):
    """Return {score, reasoning, provider, cached} — fully fail-soft."""
    provider = _detect_provider()
    if not provider:
        return {"score": 0, "reasoning": "no LLM provider configured",
                "provider": "none", "cached": False, "error": "no LLM"}

    cache_path = _cache_key(provider, headline, summary or "")
    cached = _read_cache(cache_path, max_age_seconds=3600)
    if cached is not None:
        return {**cached, "cached": True}

    prompt = _build_prompt(headline, summary, symbol)
    fn = _PROVIDERS.get(provider)
    text, err = fn(prompt)
    if err:
        return {"score": 0, "reasoning": f"{provider} error: {err}",
                "provider": provider, "cached": False, "error": err}
    parsed = _parse_response(text)
    result = {
        "score": parsed["score"],
        "reasoning": parsed["reason"],
        "provider": provider,
        "cached": False,
        "malformed": parsed.get("malformed", False),
    }
    _write_cache(cache_path, result)
    _bump_call_counter()
    return result


def score_batch(items):
    """Scores items sequentially. Returns list in same order.
    Caches mean repeats are ~free."""
    return [score_news(i.get("headline"),
                        i.get("summary"),
                        i.get("symbol")) for i in items]


def estimate_daily_cost():
    """Returns {provider, cost_per_1k_calls_usd, today_call_count} for
    surfacing in the Factor Health panel."""
    provider = _detect_provider()
    cost_map = {
        "gemini": 0.0075,    # ~$0.075/1M tokens × 100 tok/call
        "openai": 0.015,     # ~$0.15/1M in × 100 + $0.60/1M out × 30
        "groq": 0.0,         # free tier
        "anthropic": 0.085,  # ~$0.80/1M in × 100 + $4/1M out × 30
    }
    counter_path = os.path.join(_cache_dir(), "_counter.json")
    today_count = 0
    try:
        if os.path.exists(counter_path):
            with open(counter_path) as f:
                d = json.load(f) or {}
            if d.get("date") == datetime.now().strftime("%Y-%m-%d"):
                today_count = int(d.get("count", 0))
    except Exception:
        pass
    return {
        "provider": provider or "none",
        "cost_per_1k_calls_usd": cost_map.get(provider, 0),
        "today_call_count": today_count,
        "estimated_today_cost_usd": round(
            (today_count / 1000.0) * cost_map.get(provider, 0), 4
        ),
    }


if __name__ == "__main__":
    import sys
    print(f"Detected provider: {_detect_provider() or 'NONE'}")
    if len(sys.argv) > 1:
        result = score_news(" ".join(sys.argv[1:]))
        print(json.dumps(result, indent=2))
    else:
        print("Usage: python3 llm_sentiment.py <headline>")
        print(json.dumps(estimate_daily_cost(), indent=2))
