#!/usr/bin/env python3
"""
Options flow tracker.
Detects unusual options volume — often leads stock price moves.
Uses Alpaca's options data (free).
"""
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta
from et_time import now_et

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_dotenv():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())
load_dotenv()

API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

def api_get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def analyze_options_flow(symbol):
    """Analyze options activity for a symbol. Look for unusual call/put ratio."""
    # Get all active option contracts expiring within 45 days
    end = (now_et() + timedelta(days=45)).strftime("%Y-%m-%d")
    start = now_et().strftime("%Y-%m-%d")

    url_calls = f"{API_ENDPOINT}/options/contracts?underlying_symbols={symbol}&type=call&expiration_date_gte={start}&expiration_date_lte={end}&status=active&limit=100"
    url_puts = f"{API_ENDPOINT}/options/contracts?underlying_symbols={symbol}&type=put&expiration_date_gte={start}&expiration_date_lte={end}&status=active&limit=100"

    calls_data = api_get(url_calls)
    puts_data = api_get(url_puts)

    calls = calls_data.get("option_contracts", []) if isinstance(calls_data, dict) else []
    puts = puts_data.get("option_contracts", []) if isinstance(puts_data, dict) else []

    # Sum open interest
    call_oi = sum(int(c.get("open_interest", 0) or 0) for c in calls)
    put_oi = sum(int(p.get("open_interest", 0) or 0) for p in puts)

    if call_oi + put_oi == 0:
        return {"symbol": symbol, "signal": "no_data", "error": "No options data available"}

    call_put_ratio = call_oi / max(put_oi, 1)

    # Signal interpretation
    signal = "neutral"
    confidence = "low"
    if call_put_ratio > 2.0:
        signal = "bullish"
        confidence = "high" if call_put_ratio > 3.0 else "medium"
    elif call_put_ratio < 0.5:
        signal = "bearish"
        confidence = "high" if call_put_ratio < 0.33 else "medium"

    return {
        "symbol": symbol,
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
        "call_put_ratio": round(call_put_ratio, 2),
        "signal": signal,
        "confidence": confidence,
        "contracts_available": len(calls) + len(puts),
        "interpretation": f"C/P ratio {round(call_put_ratio,2)} = {signal} bias ({confidence} confidence)"
    }

def scan_options_flow(symbols, min_ratio=2.0):
    """Scan multiple symbols for unusual options activity."""
    results = []
    for sym in symbols[:20]:  # Limit API calls
        analysis = analyze_options_flow(sym)
        if analysis.get("signal") in ("bullish", "bearish") and analysis.get("confidence") != "low":
            results.append(analysis)
    # Sort by deviation from 1.0 (neutral)
    results.sort(key=lambda x: abs(x.get("call_put_ratio", 1) - 1), reverse=True)
    return results

if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    result = analyze_options_flow(symbol)
    print(f"Options flow for {symbol}:")
    for k, v in result.items():
        print(f"  {k}: {v}")
