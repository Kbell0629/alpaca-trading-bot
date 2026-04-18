#!/usr/bin/env python3
"""
news_websocket.py — Alpaca real-time news streaming.

Round-11 expansion (item 10). Currently we POLL for news in the
screener (every 30 min). Alpaca offers a real-time news websocket
that pushes headlines as they're published. Catches breaking news
mid-day for stocks we already hold OR for new candidates worth
deploying immediately.

Architecture:
  - Background thread connects to wss://stream.data.alpaca.markets/v1beta1/news
  - Filters for symbols in current positions + watch list
  - Each incoming headline is scored by llm_sentiment (if configured)
    OR keyword scanner. Strong sentiment (|score| ≥ 6) triggers an
    action: ntfy push notification + write to news_alerts.json
  - The auto-deployer's news_signals reader picks up alerts on next tick

Public API:

    start_news_stream(user, symbols, on_event=None) -> Thread
        Spawns a daemon thread that maintains the websocket connection.
        on_event(news_dict) called for each filtered headline.

    stop_news_stream() -> None
        Sets a flag that the loop checks; thread exits cleanly.

    get_recent_alerts(user_dir, max_age_minutes=60) -> list
        Reads news_alerts.json — alerts the websocket scored as
        actionable in the last hour.

Note: requires `websocket-client` pip package. If not installed,
start_news_stream returns None — module fails soft.
"""
from __future__ import annotations
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta

try:
    from et_time import now_et
except ImportError:
    def now_et():
        return datetime.now()


_stream_thread = None
_stop_flag = threading.Event()
_alerts_lock = threading.Lock()


def _save_alert(user_dir, alert):
    """Append alert to news_alerts.json (atomic, capped to 100 entries)."""
    path = os.path.join(user_dir, "news_alerts.json")
    with _alerts_lock:
        try:
            existing = []
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f) or {}
                existing = data.get("alerts", [])
            existing.append(alert)
            existing = existing[-100:]  # cap
            payload = {
                "updated_at": now_et().isoformat(),
                "alerts": existing,
            }
            d = os.path.dirname(path)
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            os.rename(tmp, path)
        except Exception as e:
            print(f"[news_websocket] alert save failed: {e}")


def get_recent_alerts(user_dir, max_age_minutes=60):
    path = os.path.join(user_dir, "news_alerts.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f) or {}
        cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
        out = []
        for a in (data.get("alerts") or []):
            try:
                ts = datetime.fromisoformat(a.get("received_at", "").replace("Z", ""))
                if ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                if ts >= cutoff:
                    out.append(a)
            except Exception:
                continue
        return out
    except Exception:
        return []


def _score_headline(headline, summary, symbol):
    """Try LLM sentiment first; fall back to keyword scanner."""
    try:
        from llm_sentiment import score_news, _detect_provider
        if _detect_provider():
            r = score_news(headline, summary, symbol)
            return r.get("score", 0), r.get("reasoning", "")
    except Exception:
        pass
    try:
        from quality_filter import bullish_news_bonus
        r = bullish_news_bonus([{"headline": headline, "summary": summary}])
        # Convert 0..15 keyword bonus → -10..+10 scale
        return min(10, r["bonus"] // 1.5), ", ".join(r["matched_keywords"])
    except Exception:
        return 0, ""


def _run_stream(user, symbols, on_event):
    """Maintains the websocket; reconnects on disconnect."""
    try:
        import websocket
    except ImportError:
        print("[news_websocket] websocket-client not installed — skipping stream")
        return

    api_key = user.get("api_key") or os.environ.get("ALPACA_API_KEY", "")
    api_secret = user.get("api_secret") or os.environ.get("ALPACA_API_SECRET", "")
    if not api_key or not api_secret:
        print("[news_websocket] no Alpaca credentials — stream disabled")
        return

    udir = user.get("_data_dir") or os.environ.get("DATA_DIR") or "."

    def on_message(ws, msg):
        try:
            events = json.loads(msg)
            if not isinstance(events, list):
                events = [events]
            for ev in events:
                if ev.get("T") != "n":  # 'n' = news event
                    continue
                headline = ev.get("headline", "")
                summary = ev.get("summary", "")
                tickers = ev.get("symbols", []) or []
                # Filter to our watched set
                relevant = [t for t in tickers if t in symbols]
                if not relevant:
                    continue
                sym = relevant[0]
                score, reason = _score_headline(headline, summary, sym)
                if abs(score) < 6:
                    continue  # only strong signals get saved + pushed
                alert = {
                    "received_at": now_et().isoformat(),
                    "symbol": sym,
                    "headline": headline,
                    "summary": summary[:300],
                    "score": score,
                    "reason": reason,
                    "tickers": tickers,
                }
                _save_alert(udir, alert)
                # Push to ntfy if configured
                try:
                    if user.get("ntfy_topic"):
                        import urllib.request
                        topic = user["ntfy_topic"]
                        direction = "BULLISH" if score > 0 else "BEARISH"
                        body = f"{direction} news on {sym} ({score:+d}): {headline[:120]}"
                        urllib.request.urlopen(
                            f"https://ntfy.sh/{topic}",
                            data=body.encode(), timeout=5
                        )
                except Exception:
                    pass
                if callable(on_event):
                    try:
                        on_event(alert)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[news_websocket] message error: {e}")

    def on_open(ws):
        print(f"[news_websocket] connected — subscribing to {len(symbols)} symbols")
        ws.send(json.dumps({
            "action": "auth",
            "key": api_key,
            "secret": api_secret,
        }))
        # Subscribe to news for our watched symbols
        ws.send(json.dumps({
            "action": "subscribe",
            "news": list(symbols),
        }))

    def on_error(ws, error):
        print(f"[news_websocket] error: {error}")

    while not _stop_flag.is_set():
        try:
            ws = websocket.WebSocketApp(
                "wss://stream.data.alpaca.markets/v1beta1/news",
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print(f"[news_websocket] connection error: {e}")
        if _stop_flag.is_set():
            break
        # Reconnect with backoff
        print("[news_websocket] reconnecting in 30s...")
        time.sleep(30)


def start_news_stream(user, symbols, on_event=None):
    """Spawn the background thread. Returns the Thread object or None
    if websocket-client isn't installed."""
    global _stream_thread
    try:
        import websocket  # noqa
    except ImportError:
        print("[news_websocket] pip install websocket-client to enable")
        return None
    if _stream_thread and _stream_thread.is_alive():
        print("[news_websocket] stream already running")
        return _stream_thread
    _stop_flag.clear()
    _stream_thread = threading.Thread(
        target=_run_stream,
        args=(user, set(symbols), on_event),
        name="NewsWebsocket",
        daemon=True,
    )
    _stream_thread.start()
    return _stream_thread


def stop_news_stream():
    """Signal the stream thread to exit. Returns immediately; the
    thread cleans up on its next iteration."""
    _stop_flag.set()


if __name__ == "__main__":
    print("Module loaded. Use start_news_stream(user, symbols).")
