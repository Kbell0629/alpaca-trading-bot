"""Round-61 pt.71 — position-level news exit triggers.

We score news at ENTRY (the deployer's news_signals path) but
ignore it on HOLD. A position can take a 10% hit on FDA-rejection /
SEC-probe / M&A-fall-through / class-action news before the bot
reacts on the next monitor tick. This is the single biggest
blast-radius reduction available — a market-order on a -10%
overnight gap is much better than waiting for a -25% stop.

Pure module. Caller supplies:
  * a list of held positions
  * a fetcher fn ``(symbol, limit) -> list[news_item]``
  * a scorer fn ``(article) -> (score, signals)`` (default uses
    ``news_scanner.score_news_article`` — the same scorer the
    pre-market scan already uses)

Returns ``{"closes": [...], "warnings": [...]}`` so the caller
(monitor_strategies) can act + notify.

Use:
    >>> from news_exit_monitor import check_position_news
    >>> result = check_position_news(positions, fetch_news_fn)
    >>> for sym, reason in result["closes"]:
    ...     close_position(sym, reason)
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional


# Threshold tuning. Pre-market scan flags `actionable` at score ≥ 8;
# hold-time exits should be MORE conservative (cost of a wrong close
# is selling at a low) so we use a stricter cutoff.
DEFAULT_BEARISH_CLOSE_THRESHOLD: int = -10
DEFAULT_BEARISH_WARN_THRESHOLD: int = -6

# How recent the news must be to trigger an exit. Anything older
# than this is stale and the market has already priced it in.
DEFAULT_MAX_NEWS_AGE_HOURS: int = 12

# Per-position rate limit: don't re-evaluate the same position more
# than once every N seconds. Cheap protection against API-rate-limit
# burn if the monitor cycle is fast.
DEFAULT_PER_SYMBOL_COOLDOWN_SEC: int = 600


def _score_default(article):
    """Default scorer — delegates to news_scanner.score_news_article
    so we share the same bullish/bearish vocabulary as the pre-market
    scan."""
    try:
        from news_scanner import score_news_article
        return score_news_article(article)
    except Exception:
        return 0, []


def _article_age_hours(article: Mapping, now_iso: Optional[str] = None) -> Optional[float]:
    """Return age of `article` in hours from now (or `now_iso` for tests).
    None on bad input."""
    if not isinstance(article, Mapping):
        return None
    ts = article.get("created_at") or article.get("updated_at") or ""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        from datetime import datetime, timezone
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if now_iso:
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        else:
            now = datetime.now(timezone.utc)
        delta = (now - dt).total_seconds() / 3600
        return max(0.0, delta)
    except (ValueError, TypeError, AttributeError):
        return None


def aggregate_symbol_news_score(
        articles,
        *,
        max_age_hours: float = DEFAULT_MAX_NEWS_AGE_HOURS,
        score_fn: Callable = _score_default,
        now_iso: Optional[str] = None,
        ) -> dict:
    """Sum scores across recent articles for a single symbol.
    Returns ``{"score": int, "articles_used": int, "signals": [...],
    "headlines": [str], "max_bearish_signal": Optional[str]}``.

    Articles older than ``max_age_hours`` are ignored. ``score_fn``
    defaults to ``news_scanner.score_news_article`` — caller can
    inject a stub for testing.
    """
    out = {
        "score": 0, "articles_used": 0, "signals": [],
        "headlines": [], "max_bearish_signal": None,
    }
    if not articles or not isinstance(articles, (list, tuple)):
        return out
    most_bearish_score = 0
    most_bearish_label = None
    for article in articles:
        if not isinstance(article, Mapping):
            continue
        age_hours = _article_age_hours(article, now_iso=now_iso)
        if age_hours is None:
            continue
        if age_hours > max_age_hours:
            continue
        try:
            s, signals = score_fn(article)
        except Exception:
            continue
        if not isinstance(s, (int, float)):
            continue
        out["score"] += int(s)
        out["articles_used"] += 1
        if signals:
            out["signals"].extend(signals)
            for sig in signals:
                if not isinstance(sig, Mapping):
                    continue
                w = sig.get("weight")
                if isinstance(w, (int, float)) and w < most_bearish_score:
                    most_bearish_score = w
                    most_bearish_label = sig.get("signal")
        h = article.get("headline")
        if h and len(out["headlines"]) < 5:
            out["headlines"].append(h)
    out["max_bearish_signal"] = most_bearish_label
    return out


def check_position_news(
        positions,
        fetch_news_fn: Callable,
        *,
        bearish_close_threshold: int = DEFAULT_BEARISH_CLOSE_THRESHOLD,
        bearish_warn_threshold: int = DEFAULT_BEARISH_WARN_THRESHOLD,
        max_age_hours: float = DEFAULT_MAX_NEWS_AGE_HOURS,
        score_fn: Callable = _score_default,
        cooldown_state: Optional[dict] = None,
        cooldown_sec: int = DEFAULT_PER_SYMBOL_COOLDOWN_SEC,
        now_iso: Optional[str] = None,
        ) -> dict:
    """Sweep all held positions for fresh bearish news. Returns
    ``{"closes": [{"symbol", "score", "headlines", "signal"}],
       "warnings": [{"symbol", "score", "headlines", "signal"}],
       "checked": int, "skipped_cooldown": int}``.

    For each position:
      1. Skip if symbol is on cooldown (recent check < cooldown_sec)
      2. Fetch up to 10 recent news items via ``fetch_news_fn(symbol, 10)``
      3. Aggregate score over articles within ``max_age_hours``
      4. score ≤ ``bearish_close_threshold`` → CLOSE recommendation
      5. ``bearish_close_threshold`` < score ≤ ``bearish_warn_threshold`` → WARN

    Long positions only — shorts BENEFIT from bearish news, so the
    sweep skips them. ``cooldown_state`` is a caller-managed dict
    ``{symbol: timestamp}``; the function reads + updates it. Caller
    is responsible for persistence (e.g. in ``_last_runs``).
    """
    out = {"closes": [], "warnings": [], "checked": 0, "skipped_cooldown": 0}
    if not positions or not isinstance(positions, (list, tuple)):
        return out
    if cooldown_state is None:
        cooldown_state = {}
    import time as _t
    now_ts = _t.time()
    for pos in positions:
        if not isinstance(pos, Mapping):
            continue
        sym = (pos.get("symbol") or "").upper()
        if not sym:
            continue
        # Long positions only — shorts benefit from bearish news.
        try:
            qty = float(pos.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        # Cooldown
        last_check = cooldown_state.get(sym, 0)
        if last_check and (now_ts - last_check) < cooldown_sec:
            out["skipped_cooldown"] += 1
            continue
        cooldown_state[sym] = now_ts
        out["checked"] += 1
        # Fetch + aggregate
        try:
            articles = fetch_news_fn(sym, 10) or []
        except Exception:
            continue
        agg = aggregate_symbol_news_score(
            articles, max_age_hours=max_age_hours,
            score_fn=score_fn, now_iso=now_iso)
        score = agg["score"]
        if score <= bearish_close_threshold:
            out["closes"].append({
                "symbol": sym, "score": score,
                "headlines": agg["headlines"],
                "signal": agg["max_bearish_signal"],
                "articles_used": agg["articles_used"],
            })
        elif score <= bearish_warn_threshold:
            out["warnings"].append({
                "symbol": sym, "score": score,
                "headlines": agg["headlines"],
                "signal": agg["max_bearish_signal"],
                "articles_used": agg["articles_used"],
            })
    return out


def explain_close(close_entry: Mapping) -> str:
    """Human-readable one-liner for the notification body."""
    if not isinstance(close_entry, Mapping):
        return ""
    sym = close_entry.get("symbol", "?")
    score = close_entry.get("score", 0)
    signal = close_entry.get("signal") or "bearish news"
    used = close_entry.get("articles_used", 0)
    parts = [
        f"{sym} closed on bearish news (aggregate score {score})",
        f"trigger: {signal}",
        f"based on {used} recent article(s)",
    ]
    headlines = close_entry.get("headlines") or []
    if headlines:
        parts.append("headlines:\n  - " + "\n  - ".join(
            str(h) for h in headlines[:3]))
    return ". ".join(parts[:3]) + (
        ". " + parts[3] if len(parts) > 3 else "")
