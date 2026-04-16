#!/usr/bin/env python3
"""
Portfolio correlation analysis.
Calculates statistical correlation between holdings to prevent over-concentration.
Uses historical daily returns from Alpaca bar data.
"""
import json
import os
import urllib.request
import urllib.parse
import time
from datetime import datetime, timedelta, timezone
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

DATA_ENDPOINT = os.environ.get("ALPACA_DATA_ENDPOINT", "https://data.alpaca.markets/v2")
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

def fetch_daily_returns(symbol, days=60):
    """Fetch daily closing prices and compute returns."""
    end = now_et().strftime("%Y-%m-%d")
    start = (now_et() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    url = (f"{DATA_ENDPOINT}/stocks/{urllib.parse.quote(symbol)}/bars"
           f"?timeframe=1Day&start={start}&end={end}&limit={days}&feed=iex")
    data = api_get(url)
    bars = data.get("bars", []) if isinstance(data, dict) else []
    if len(bars) < 2:
        return []
    closes = [b.get("c", 0) for b in bars]
    returns = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes))]
    return returns

def pearson_correlation(x, y):
    """Calculate Pearson correlation coefficient between two lists."""
    n = min(len(x), len(y))
    if n < 5:
        return 0.0
    x, y = x[:n], y[:n]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n)) / n
    std_x = (sum((xi - mean_x) ** 2 for xi in x) / n) ** 0.5
    std_y = (sum((yi - mean_y) ** 2 for yi in y) / n) ** 0.5
    if std_x == 0 or std_y == 0:
        return 0.0
    return cov / (std_x * std_y)

def calculate_correlation_matrix(symbols, days=60):
    """Calculate pairwise correlation for a list of symbols."""
    print(f"Calculating correlation matrix for {len(symbols)} symbols...")
    returns = {}
    for sym in symbols:
        r = fetch_daily_returns(sym, days)
        if r:
            returns[sym] = r
        time.sleep(0.2)

    matrix = {}
    warnings = []
    high_corr_pairs = []

    syms = list(returns.keys())
    for i in range(len(syms)):
        matrix[syms[i]] = {}
        for j in range(len(syms)):
            if i == j:
                matrix[syms[i]][syms[j]] = 1.0
            elif j < i:
                matrix[syms[i]][syms[j]] = matrix[syms[j]][syms[i]]
            else:
                corr = pearson_correlation(returns[syms[i]], returns[syms[j]])
                matrix[syms[i]][syms[j]] = round(corr, 3)

                if abs(corr) > 0.7:
                    high_corr_pairs.append({
                        "pair": [syms[i], syms[j]],
                        "correlation": round(corr, 3),
                        "risk": "HIGH" if abs(corr) > 0.85 else "MODERATE",
                    })

    if high_corr_pairs:
        warnings.append(f"{len(high_corr_pairs)} highly correlated pairs detected — consider diversifying")

    # Portfolio diversification score (0-100)
    if len(syms) < 2:
        div_score = 50
    else:
        avg_abs_corr = sum(abs(matrix[s1][s2]) for s1 in syms for s2 in syms if s1 != s2) / (len(syms) * (len(syms) - 1)) if len(syms) > 1 else 0
        div_score = max(0, min(100, int((1 - avg_abs_corr) * 100)))

    return {
        "symbols": syms,
        "matrix": matrix,
        "high_correlation_pairs": high_corr_pairs,
        "diversification_score": div_score,
        "warnings": warnings,
    }

if __name__ == "__main__":
    result = calculate_correlation_matrix(["TSLA", "NVDA", "AAPL"], 30)
    print(f"Diversification score: {result['diversification_score']}/100")
    for pair in result["high_correlation_pairs"]:
        print(f"  {pair['pair'][0]}-{pair['pair'][1]}: {pair['correlation']} ({pair['risk']})")
    print("Matrix:")
    for s1 in result["symbols"]:
        row = " ".join(f"{result['matrix'][s1][s2]:+.2f}" for s2 in result["symbols"])
        print(f"  {s1}: {row}")
