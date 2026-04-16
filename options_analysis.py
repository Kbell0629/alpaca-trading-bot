#!/usr/bin/env python3
"""
Options chain analysis for the wheel strategy.
Finds optimal put/call strikes and expirations based on premium, IV, and risk.
Uses Alpaca's free options API.
"""
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta, timezone

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

def api_get(url, timeout=15, max_retries=2):
    import time as _time
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < max_retries - 1:
                _time.sleep(1)
                continue
            return {"error": str(e)}

def get_options_chain(symbol, option_type="put", min_days=14, max_days=45):
    """Fetch available options contracts for a symbol."""
    today = date.today()
    min_exp = (today + timedelta(days=min_days)).isoformat()
    max_exp = (today + timedelta(days=max_days)).isoformat()

    url = (f"{API_ENDPOINT}/options/contracts"
           f"?underlying_symbols={symbol}"
           f"&type={option_type}"
           f"&expiration_date_gte={min_exp}"
           f"&expiration_date_lte={max_exp}"
           f"&status=active"
           f"&limit=100")

    result = api_get(url)
    if isinstance(result, dict) and "option_contracts" in result:
        return result["option_contracts"]
    elif isinstance(result, dict) and "error" in result:
        return []
    return result if isinstance(result, list) else []

def get_option_quote(contract_symbol):
    """Get latest quote for an options contract."""
    url = f"https://data.alpaca.markets/v1beta1/options/quotes/latest?symbols={contract_symbol}&feed=indicative"
    result = api_get(url)
    if isinstance(result, dict) and "quotes" in result:
        return result["quotes"].get(contract_symbol, {})
    return {}

def analyze_wheel_candidates(symbol, current_price, strategy="put"):
    """Find the best option contract for the wheel strategy.

    For puts: look for strike ~10% below current price, 2-4 weeks out, highest premium/risk ratio.
    For calls: look for strike ~10% above cost basis, 2-4 weeks out.
    """
    contracts = get_options_chain(symbol, option_type=strategy, min_days=14, max_days=45)

    if not contracts:
        return {
            "symbol": symbol,
            "type": strategy,
            "available": False,
            "message": "No options contracts found (options may not be available for this symbol or on paper trading)",
            "candidates": [],
            "best": None,
        }

    candidates = []
    target_strike_pct = 0.90 if strategy == "put" else 1.10  # 10% OTM
    target_strike = current_price * target_strike_pct

    for contract in contracts:
        strike = float(contract.get("strike_price", 0))
        expiry = contract.get("expiration_date", "")
        contract_sym = contract.get("symbol", "")

        if not strike or not expiry:
            continue

        # Calculate days to expiration
        try:
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            dte = (exp_date - date.today()).days
        except (ValueError, TypeError):
            dte = 0

        # Distance from ideal strike
        strike_distance_pct = abs(strike - target_strike) / current_price * 100

        # Score: prefer strikes close to target, moderate DTE
        # Lower distance = better, 21 DTE is ideal
        dte_score = max(0, 10 - abs(dte - 21) / 3)  # peaks at 21 days
        distance_score = max(0, 10 - strike_distance_pct * 2)
        total_score = dte_score + distance_score

        # For puts: OTM is below current price
        otm = strike < current_price if strategy == "put" else strike > current_price

        candidates.append({
            "contract_symbol": contract_sym,
            "strike": strike,
            "expiration": expiry,
            "dte": dte,
            "otm": otm,
            "strike_distance_pct": round(strike_distance_pct, 1),
            "score": round(total_score, 1),
        })

    # Sort by score
    candidates.sort(key=lambda x: x["score"], reverse=True)

    best = candidates[0] if candidates else None

    return {
        "symbol": symbol,
        "type": strategy,
        "current_price": current_price,
        "target_strike": round(target_strike, 2),
        "available": True,
        "total_contracts": len(contracts),
        "scored_candidates": len(candidates),
        "candidates": candidates[:5],  # Top 5
        "best": best,
    }

def get_wheel_recommendation(symbol, current_price, cost_basis=None):
    """Get full wheel strategy recommendation with put and call analysis."""
    put_analysis = analyze_wheel_candidates(symbol, current_price, "put")

    call_analysis = None
    if cost_basis:
        call_analysis = analyze_wheel_candidates(symbol, cost_basis, "call")

    # Estimated annual return from wheel (rough calculation)
    # Assume ~2% premium per 3-week cycle, ~17 cycles/year
    estimated_premium_pct = 2.0
    estimated_annual_return = estimated_premium_pct * 17

    return {
        "symbol": symbol,
        "current_price": current_price,
        "cost_basis": cost_basis,
        "put_analysis": put_analysis,
        "call_analysis": call_analysis,
        "estimated_premium_per_cycle_pct": estimated_premium_pct,
        "estimated_annual_return_pct": round(estimated_annual_return, 1),
        "capital_required": round(current_price * 100, 2),  # 100 shares
    }

if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "SOFI"
    price = float(sys.argv[2]) if len(sys.argv) > 2 else 18.0
    print(f"Options analysis for {symbol} at ${price:.2f}:")
    result = get_wheel_recommendation(symbol, price)
    put = result["put_analysis"]
    print(f"  Put contracts found: {put.get('total_contracts', 0)}")
    if put.get("best"):
        b = put["best"]
        print(f"  Best put: Strike ${b['strike']} exp {b['expiration']} ({b['dte']} DTE, score {b['score']})")
    else:
        print(f"  No suitable puts found: {put.get('message', 'unknown')}")
    print(f"  Capital required: ${result['capital_required']:,.2f}")
    print(f"  Est. annual return: {result['estimated_annual_return_pct']}%")
