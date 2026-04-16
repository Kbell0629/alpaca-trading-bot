#!/usr/bin/env python3
"""
Tax-loss harvesting module.
Identifies positions at a loss that can be sold for tax benefits,
then suggests replacement stocks to maintain market exposure.
Rules:
- Sell a losing position to realize the loss (tax deduction)
- Buy a similar (but not identical) stock to stay invested
- Must wait 30 days before buying back the SAME stock (wash sale rule)
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

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

# Similar stock replacements (not identical — avoids wash sale)
REPLACEMENT_MAP = {
    "AAPL": ["MSFT", "GOOG"],
    "MSFT": ["AAPL", "CRM"],
    "GOOG": ["META", "MSFT"],
    "META": ["GOOG", "SNAP"],
    "AMZN": ["SHOP", "WMT"],
    "TSLA": ["RIVN", "NIO"],
    "NVDA": ["AMD", "INTC"],
    "AMD": ["NVDA", "INTC"],
    "NFLX": ["DIS", "ROKU"],
    "JPM": ["BAC", "GS"],
    "BAC": ["JPM", "WFC"],
    "COIN": ["MARA", "RIOT"],
    "SOFI": ["HOOD", "ALLY"],
    "PLTR": ["SNOW", "NET"],
    "NIO": ["RIVN", "LCID"],
}

def api_get(url, timeout=10, max_retries=2):
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

def scan_for_harvest_opportunities(min_loss_pct=5.0, min_loss_dollars=50):
    """Scan current positions for tax-loss harvesting opportunities."""
    positions = api_get(f"{API_ENDPOINT}/positions")
    if not isinstance(positions, list):
        return {"opportunities": [], "total_harvestable": 0, "error": "Could not fetch positions"}

    opportunities = []
    total_harvestable = 0

    for pos in positions:
        symbol = pos.get("symbol", "")
        qty = int(float(pos.get("qty", 0)))
        avg_entry = float(pos.get("avg_entry_price", 0))
        current = float(pos.get("current_price", 0))
        unrealized_pl = float(pos.get("unrealized_pl", 0))
        unrealized_plpc = float(pos.get("unrealized_plpc", 0)) * 100
        market_value = float(pos.get("market_value", 0))

        # Only look at losing positions
        if unrealized_pl >= 0:
            continue

        # Must meet minimum thresholds
        if abs(unrealized_plpc) < min_loss_pct or abs(unrealized_pl) < min_loss_dollars:
            continue

        # Find replacement
        replacements = REPLACEMENT_MAP.get(symbol, [])

        opportunities.append({
            "symbol": symbol,
            "qty": qty,
            "avg_entry": avg_entry,
            "current_price": current,
            "unrealized_loss": round(unrealized_pl, 2),
            "loss_pct": round(unrealized_plpc, 1),
            "market_value": round(market_value, 2),
            "tax_benefit_estimate": round(abs(unrealized_pl) * 0.25, 2),  # ~25% tax bracket estimate
            "replacements": replacements,
            "recommended_replacement": replacements[0] if replacements else None,
            "wash_sale_end_date": None,  # Would be 30 days after sell
            "action": f"Sell {qty} {symbol} (loss ${abs(unrealized_pl):.2f}), buy {replacements[0] if replacements else 'ETF'} to maintain exposure"
        })

        total_harvestable += abs(unrealized_pl)

    opportunities.sort(key=lambda x: x["unrealized_loss"])  # Biggest losses first

    return {
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "opportunities": opportunities,
        "total_harvestable_loss": round(total_harvestable, 2),
        "estimated_tax_savings": round(total_harvestable * 0.25, 2),
        "note": "Tax-loss harvesting sells losing positions for tax deductions while maintaining market exposure via similar stocks. Consult a tax advisor."
    }

if __name__ == "__main__":
    result = scan_for_harvest_opportunities()
    print(f"Tax-loss harvesting scan:")
    print(f"  Opportunities: {len(result['opportunities'])}")
    print(f"  Total harvestable: ${result['total_harvestable_loss']:,.2f}")
    print(f"  Est. tax savings: ${result['estimated_tax_savings']:,.2f}")
    for opp in result["opportunities"]:
        print(f"  {opp['symbol']}: {opp['loss_pct']:.1f}% loss (${opp['unrealized_loss']:.2f})")
        print(f"    Action: {opp['action']}")
