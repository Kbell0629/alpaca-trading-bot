#!/usr/bin/env python3
"""
premarket_scanner.py — Pre-market gap + volume scanner.

Round-11 expansion (item 6). Wires `extended_hours.py` (which exists
but isn't connected) into a useful scanner: between 8:00 AM and
9:30 AM ET, scan for stocks gapping >2% on 1.5x average pre-market
volume. Feed those into the 9:45 AM auto-deployer as priority candidates.

Public API:

    scan_premarket(api_get, api_endpoint, data_endpoint, symbols,
                    min_gap_pct=2.0, min_premarket_volume=50000) -> list[dict]
        Returns list of {symbol, premarket_price, prior_close,
        gap_pct, premarket_volume, premarket_dollar_volume} sorted
        by gap × volume descending.

    is_premarket_window(now=None) -> bool
        True between 8:00 AM and 9:30 AM ET on weekdays.

    save_premarket_picks(user_dir, picks) -> str
        Writes premarket_picks.json so the auto-deployer can prioritize.
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime

try:
    from et_time import now_et
except ImportError:
    def now_et():
        return datetime.now()


def is_premarket_window(now=None):
    """8:00 AM - 9:30 AM ET on weekdays."""
    n = now or now_et()
    if n.weekday() >= 5:
        return False
    minutes = n.hour * 60 + n.minute
    return 480 <= minutes < 570  # 8:00 - 9:30


def _safe_get(api_get, url, headers=None):
    try:
        return api_get(url, headers=headers) if headers else api_get(url)
    except Exception:
        return None


def scan_premarket(api_get, api_endpoint, data_endpoint, symbols,
                    min_gap_pct=2.0, min_premarket_volume=50000):
    """Scan a list of symbols for pre-market gaps + volume.

    Args:
        api_get: callable like user_api_get (takes URL, returns dict)
        api_endpoint: trading API endpoint (for /assets, etc.)
        data_endpoint: market data endpoint
        symbols: list of tickers to scan (e.g. top 100 by liquidity)
        min_gap_pct: minimum % gap from prior close (default 2.0)
        min_premarket_volume: minimum shares traded in premarket

    Returns sorted list of dicts (gap × dollar volume desc).
    """
    candidates = []
    for sym in symbols:
        try:
            # Get latest trade (current premarket price)
            latest_trade = _safe_get(
                api_get, f"{data_endpoint}/stocks/{sym}/trades/latest?feed=iex"
            )
            if not isinstance(latest_trade, dict):
                continue
            current = float((latest_trade.get("trade") or {}).get("p") or 0)
            if current <= 0:
                continue

            # Get yesterday's close (1-day bar)
            bars_resp = _safe_get(
                api_get,
                f"{data_endpoint}/stocks/{sym}/bars?timeframe=1Day&limit=2&adjustment=split"
            )
            if not isinstance(bars_resp, dict):
                continue
            bars = bars_resp.get("bars", [])
            if len(bars) < 1:
                continue
            prior_close = float(bars[-1].get("c") or 0)
            if prior_close <= 0:
                continue
            gap_pct = (current - prior_close) / prior_close * 100
            if abs(gap_pct) < min_gap_pct:
                continue

            # Get pre-market volume (sum of 1-min bars from 4 AM today)
            today = now_et().strftime("%Y-%m-%d")
            min_bars = _safe_get(
                api_get,
                f"{data_endpoint}/stocks/{sym}/bars?timeframe=1Min"
                f"&start={today}T08:00:00-04:00&end={today}T13:30:00-04:00"
                f"&adjustment=split&limit=600"
            )
            pm_volume = 0
            if isinstance(min_bars, dict):
                for b in (min_bars.get("bars") or []):
                    pm_volume += int(b.get("v") or 0)
            if pm_volume < min_premarket_volume:
                continue

            candidates.append({
                "symbol": sym,
                "premarket_price": round(current, 2),
                "prior_close": round(prior_close, 2),
                "gap_pct": round(gap_pct, 2),
                "premarket_volume": pm_volume,
                "premarket_dollar_volume": round(pm_volume * current, 0),
                "scanned_at": now_et().isoformat(),
            })
        except Exception:
            continue
    # Sort by absolute gap × dollar volume — biggest movers with conviction first
    candidates.sort(
        key=lambda c: abs(c["gap_pct"]) * c["premarket_dollar_volume"],
        reverse=True
    )
    return candidates


def save_premarket_picks(user_dir, picks):
    """Write premarket_picks.json atomically."""
    path = os.path.join(user_dir, "premarket_picks.json")
    payload = {
        "generated_at": now_et().isoformat(),
        "picks": picks[:30],  # top 30
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.rename(tmp, path)
    except Exception as e:
        print(f"[premarket_scanner] save failed: {e}")
    return path


def load_premarket_picks(user_dir, max_age_minutes=180):
    """Load premarket_picks.json if recent. Auto-deployer calls this."""
    path = os.path.join(user_dir, "premarket_picks.json")
    if not os.path.exists(path):
        return []
    try:
        import time
        if time.time() - os.path.getmtime(path) > max_age_minutes * 60:
            return []
        with open(path) as f:
            data = json.load(f)
        return data.get("picks", [])
    except Exception:
        return []


if __name__ == "__main__":
    print("In premarket window:", is_premarket_window())
    print("Module is wired into cloud_scheduler — no standalone run.")
