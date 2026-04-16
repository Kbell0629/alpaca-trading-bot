#!/usr/bin/env python3
"""
Self-Learning Engine for the Alpaca Trading Bot.

Analyzes the trade journal to find patterns in wins vs losses,
then generates learned weights that the screener and auto-deployer
use to adjust strategy scores over time.

Run: python3 learn.py
Schedule: Fridays at 2 PM PT (after market close)
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from et_time import now_et


def load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())
load_dotenv()


def safe_save_json(path, data):
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp_path, path)
    except:
        try: os.unlink(tmp_path)
        except: pass
        raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent runtime data lives. On Railway, set to a volume mount
# path (e.g. /data). Locally defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
# Per-user paths — cloud_scheduler.run_weekly_learning passes
# TRADE_JOURNAL_PATH and LEARNED_WEIGHTS_PATH env vars pointing to the
# current user's dir. CRITICAL: without this override, every user's
# weekly learning overwrote the same shared learned_weights.json.
TRADE_JOURNAL_PATH = os.environ.get("TRADE_JOURNAL_PATH", os.path.join(DATA_DIR, "trade_journal.json"))
LEARNED_WEIGHTS_PATH = os.environ.get("LEARNED_WEIGHTS_PATH", os.path.join(DATA_DIR, "learned_weights.json"))

# Maximum weight change per update (20%)
MAX_CHANGE = 0.2

# Strategy name mapping (journal uses these keys)
STRATEGIES = ["trailing_stop", "mean_reversion", "breakout", "copy_trading", "wheel"]

# Default hold days per strategy (used when not enough data)
DEFAULT_HOLD_DAYS = {
    "trailing_stop": 14,
    "mean_reversion": 5,
    "breakout": 3,
    "copy_trading": 30,
    "wheel": 21,
}

# Default price range preferences (used when not enough data)
DEFAULT_PRICE_RANGES = {
    "trailing_stop": "$50-100",
    "mean_reversion": "$20-50",
    "breakout": "$20-50",
    "copy_trading": "$100-500",
    "wheel": "$10-50",
}

# Price range bins
PRICE_RANGES = [
    ("$5-20", 5, 20),
    ("$20-50", 20, 50),
    ("$50-100", 50, 100),
    ("$100-500", 100, 500),
    ("$500+", 500, float("inf")),
]


def load_json(path):
    """Load a JSON file, returning None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def get_closed_trades(journal):
    """Extract closed trades from the journal."""
    trades = journal.get("trades", [])
    return [t for t in trades if t.get("status") == "closed"]


def get_price_range(price):
    """Return the price range label for a given price."""
    for label, low, high in PRICE_RANGES:
        if low <= price < high:
            return label
    return "$500+"


def calc_holding_days(trade):
    """Calculate how many days a trade was held."""
    entry_date = trade.get("entry_date") or trade.get("opened_at")
    exit_date = trade.get("exit_date") or trade.get("closed_at")
    if not entry_date or not exit_date:
        return None
    try:
        # Handle various date formats
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
            try:
                dt_entry = datetime.strptime(entry_date[:26].replace("Z", ""), fmt.replace("%z", ""))
                break
            except ValueError:
                continue
        else:
            return None

        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
            try:
                dt_exit = datetime.strptime(exit_date[:26].replace("Z", ""), fmt.replace("%z", ""))
                break
            except ValueError:
                continue
        else:
            return None

        return max(1, (dt_exit - dt_entry).days)
    except Exception:
        return None


def is_win(trade):
    """Determine if a trade was a win (positive P&L)."""
    pnl = trade.get("realized_pnl", trade.get("pnl", 0))
    return pnl > 0


def get_pnl(trade):
    """Get the P&L of a trade."""
    return trade.get("realized_pnl", trade.get("pnl", 0))


def get_strategy(trade):
    """Get the normalized strategy name from a trade."""
    raw = trade.get("strategy", "").lower().replace(" ", "_")
    # Normalize common variants
    mapping = {
        "trailing_stop": "trailing_stop",
        "trailing": "trailing_stop",
        "mean_reversion": "mean_reversion",
        "meanreversion": "mean_reversion",
        "breakout": "breakout",
        "copy_trading": "copy_trading",
        "copytrading": "copy_trading",
        "copy": "copy_trading",
        "wheel": "wheel",
        "wheel_strategy": "wheel",
    }
    return mapping.get(raw, raw)


def check_signal(trade, signal_name):
    """Check if a specific signal was present at trade entry."""
    entry_data = trade.get("entry_conditions", trade.get("entry_data", {}))
    if not entry_data:
        entry_data = trade  # Fall back to trade-level fields

    if signal_name == "high_momentum":
        return entry_data.get("momentum_20d", 0) > 10
    elif signal_name == "high_volume":
        return entry_data.get("relative_volume", 1) > 2
    elif signal_name == "positive_sentiment":
        return entry_data.get("news_sentiment") == "positive"
    elif signal_name == "low_volatility":
        return entry_data.get("volatility", 3) < 3
    elif signal_name == "high_volatility":
        return entry_data.get("volatility", 3) > 5
    elif signal_name == "bull_market":
        return entry_data.get("market_regime") == "bull"
    elif signal_name == "bear_market":
        return entry_data.get("market_regime") == "bear"
    return False


SIGNAL_NAMES = [
    "high_momentum",
    "high_volume",
    "positive_sentiment",
    "low_volatility",
    "high_volatility",
    "bull_market",
    "bear_market",
]


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def analyze_strategy_performance(closed_trades, existing):
    """Analyze win rate and P&L per strategy, return multipliers."""
    stats = {}
    for strat in STRATEGIES:
        stats[strat] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "count": 0}

    for trade in closed_trades:
        strat = get_strategy(trade)
        if strat not in stats:
            stats[strat] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "count": 0}
        stats[strat]["count"] += 1
        stats[strat]["total_pnl"] += get_pnl(trade)
        if is_win(trade):
            stats[strat]["wins"] += 1
        else:
            stats[strat]["losses"] += 1

    multipliers = {}
    details = {}
    for strat in STRATEGIES:
        s = stats.get(strat, {"wins": 0, "losses": 0, "total_pnl": 0.0, "count": 0})
        count = s["count"]
        win_rate = (s["wins"] / count * 100) if count > 0 else 0
        avg_pnl = (s["total_pnl"] / count) if count > 0 else 0

        # Use continuous multiplier function for strategies with 5+ trades
        if count >= 5:
            mult = 0.5 + (win_rate / 100)  # 50% win = 1.0x, 70% win = 1.2x, 30% win = 0.8x
            mult = max(0.5, min(1.5, mult))  # clamp
        else:
            mult = 1.0

        # Limit how much weights can change per update
        old = existing.get("strategy_multipliers", {}).get(strat, 1.0) if existing else 1.0
        if abs(mult - old) > MAX_CHANGE:
            mult = old + (MAX_CHANGE if mult > old else -MAX_CHANGE)
        mult = round(mult, 3)

        multipliers[strat] = mult

        details[strat] = {
            "trades": count,
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": round(win_rate, 1),
            "avg_pnl": round(avg_pnl, 2),
            "multiplier": multipliers[strat],
        }

    return multipliers, details


def analyze_signals(closed_trades):
    """Analyze which signals correlate with wins vs losses."""
    if not closed_trades:
        return [], []

    # Overall win rate
    total_wins = sum(1 for t in closed_trades if is_win(t))
    overall_win_rate = (total_wins / len(closed_trades) * 100) if closed_trades else 50

    boost_signals = []
    penalty_signals = []

    for signal in SIGNAL_NAMES:
        present_trades = [t for t in closed_trades if check_signal(t, signal)]
        absent_trades = [t for t in closed_trades if not check_signal(t, signal)]

        if len(present_trades) < 10:
            # Not enough data for this signal (increased from 3 to 10)
            continue

        present_wins = sum(1 for t in present_trades if is_win(t))
        present_wr = (present_wins / len(present_trades) * 100)

        absent_wins = sum(1 for t in absent_trades if is_win(t)) if absent_trades else 0
        absent_wr = (absent_wins / len(absent_trades) * 100) if absent_trades else overall_win_rate

        diff = present_wr - absent_wr

        if diff >= 10:
            boost_signals.append({
                "signal": signal,
                "win_rate_with": round(present_wr, 1),
                "win_rate_without": round(absent_wr, 1),
                "improvement": round(diff, 1),
                "sample_size": len(present_trades),
                "adjustment": 3,
            })
        elif diff <= -10:
            penalty_signals.append({
                "signal": signal,
                "win_rate_with": round(present_wr, 1),
                "win_rate_without": round(absent_wr, 1),
                "degradation": round(abs(diff), 1),
                "sample_size": len(present_trades),
                "adjustment": -3,
            })

    return boost_signals, penalty_signals


def analyze_price_ranges(closed_trades):
    """Find best price range per strategy."""
    # strategy -> price_range -> {wins, total}
    range_stats = {}
    for strat in STRATEGIES:
        range_stats[strat] = {}
        for label, _, _ in PRICE_RANGES:
            range_stats[strat][label] = {"wins": 0, "total": 0}

    for trade in closed_trades:
        strat = get_strategy(trade)
        if strat not in range_stats:
            continue
        price = trade.get("entry_price", trade.get("price", 0))
        if not price:
            continue
        pr = get_price_range(price)
        range_stats[strat][pr]["total"] += 1
        if is_win(trade):
            range_stats[strat][pr]["wins"] += 1

    preferences = {}
    for strat in STRATEGIES:
        best_range = DEFAULT_PRICE_RANGES[strat]
        best_wr = -1
        for label in range_stats[strat]:
            s = range_stats[strat][label]
            if s["total"] >= 2:  # Need at least 2 trades
                wr = s["wins"] / s["total"]
                if wr > best_wr:
                    best_wr = wr
                    best_range = label
        preferences[strat] = best_range

    return preferences


def analyze_holding_periods(closed_trades):
    """Find optimal holding period per strategy."""
    # strategy -> list of (hold_days, pnl)
    hold_data = {strat: [] for strat in STRATEGIES}

    for trade in closed_trades:
        strat = get_strategy(trade)
        if strat not in hold_data:
            continue
        days = calc_holding_days(trade)
        if days is None:
            continue
        pnl = get_pnl(trade)
        hold_data[strat].append((days, pnl))

    recommendations = {}
    for strat in STRATEGIES:
        data = hold_data[strat]
        if len(data) < 3:
            recommendations[strat] = DEFAULT_HOLD_DAYS[strat]
            continue

        # Group by duration buckets and find which has best avg return
        buckets = {}
        for days, pnl in data:
            # Round to nearest bucket
            if days <= 3:
                bucket = 3
            elif days <= 7:
                bucket = 5
            elif days <= 14:
                bucket = 10
            elif days <= 21:
                bucket = 14
            elif days <= 30:
                bucket = 21
            else:
                bucket = 30

            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append(pnl)

        # Find bucket with best average P&L
        best_bucket = DEFAULT_HOLD_DAYS[strat]
        best_avg = float("-inf")
        for bucket, pnls in buckets.items():
            if len(pnls) >= 2:
                avg = sum(pnls) / len(pnls)
                if avg > best_avg:
                    best_avg = avg
                    best_bucket = bucket

        recommendations[strat] = best_bucket

    return recommendations


def generate_insights(closed_trades, multipliers, details, boost_signals, penalty_signals,
                      confidence):
    """Generate human-readable insights."""
    insights = []
    total = len(closed_trades)

    if total == 0:
        insights.append("No closed trades yet. The learning engine needs trade data to generate insights.")
        insights.append("Strategy weights will auto-adjust after sufficient data.")
        return insights

    if total < 20:
        insights.append(
            f"Only {total} closed trade(s) analyzed. Need 20+ closed trades to generate meaningful insights."
        )
        insights.append("Strategy weights will auto-adjust after sufficient data.")

    # Strategy insights
    for strat in STRATEGIES:
        d = details.get(strat, {})
        count = d.get("trades", 0)
        if count == 0:
            continue
        wr = d.get("win_rate", 0)
        mult = d.get("multiplier", 1.0)

        if mult > 1.0:
            insights.append(
                f"{strat}: Strong performer -- {wr}% win rate over {count} trades. "
                f"Score boosted to {mult}x."
            )
        elif mult < 1.0:
            insights.append(
                f"{strat}: Underperforming -- {wr}% win rate over {count} trades. "
                f"Score reduced to {mult}x."
            )
        elif count >= 3:
            insights.append(
                f"{strat}: {wr}% win rate over {count} trades (avg P&L: ${d.get('avg_pnl', 0):.2f}). "
                f"Need 5+ trades to adjust weight."
            )

    # Signal insights
    for sig in boost_signals:
        insights.append(
            f"BOOST signal '{sig['signal']}': Win rate {sig['win_rate_with']}% when present "
            f"vs {sig['win_rate_without']}% when absent (+{sig['improvement']}%). "
            f"Adding +3 score bonus."
        )

    for sig in penalty_signals:
        insights.append(
            f"PENALTY signal '{sig['signal']}': Win rate drops to {sig['win_rate_with']}% when present "
            f"vs {sig['win_rate_without']}% when absent (-{sig['degradation']}%). "
            f"Applying -3 score penalty."
        )

    if not insights:
        insights.append("Analysis complete. No significant patterns detected yet.")

    return insights


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_learning_engine():
    """Run the full learning analysis and save results."""
    print("=" * 60)
    print("SELF-LEARNING ENGINE")
    print("=" * 60)
    print()

    # Load trade journal
    journal = load_json(TRADE_JOURNAL_PATH)
    if not journal:
        print("No trade journal found. Initializing with defaults.")
        journal = {"trades": [], "daily_snapshots": []}

    # Check for manual override
    existing = load_json(LEARNED_WEIGHTS_PATH)
    if existing and existing.get("manual_override"):
        print("Manual override flag set in learned_weights.json -- skipping auto-adjustment")
        return existing

    closed_trades = get_closed_trades(journal)
    total_closed = len(closed_trades)
    print(f"Trade journal loaded: {len(journal.get('trades', []))} total trades, {total_closed} closed")

    # Determine confidence level
    if total_closed >= 50:
        confidence = "high"
    elif total_closed >= 20:
        confidence = "medium"
    else:
        confidence = "low"
    print(f"Confidence level: {confidence} ({total_closed} closed trades)")
    print()

    # 1. Strategy Performance
    print("--- Strategy Performance Analysis ---")
    multipliers, details = analyze_strategy_performance(closed_trades, existing)
    for strat in STRATEGIES:
        d = details.get(strat, {})
        print(
            f"  {strat}: {d.get('trades', 0)} trades, "
            f"{d.get('win_rate', 0)}% win rate, "
            f"avg P&L ${d.get('avg_pnl', 0):.2f}, "
            f"multiplier={d.get('multiplier', 1.0)}"
        )
    print()

    # 2. Signal Analysis
    print("--- Signal Analysis ---")
    boost_signals, penalty_signals = analyze_signals(closed_trades)
    if boost_signals:
        for sig in boost_signals:
            print(f"  BOOST: {sig['signal']} (+{sig['improvement']}% win rate improvement)")
    if penalty_signals:
        for sig in penalty_signals:
            print(f"  PENALTY: {sig['signal']} (-{sig['degradation']}% win rate degradation)")
    if not boost_signals and not penalty_signals:
        print("  No significant signal patterns detected (need more data).")
    print()

    # 3. Price Range Analysis
    print("--- Price Range Analysis ---")
    price_preferences = analyze_price_ranges(closed_trades)
    for strat in STRATEGIES:
        print(f"  {strat}: preferred range = {price_preferences[strat]}")
    print()

    # 4. Holding Period Analysis
    print("--- Holding Period Analysis ---")
    hold_recommendations = analyze_holding_periods(closed_trades)
    for strat in STRATEGIES:
        print(f"  {strat}: recommended hold = {hold_recommendations[strat]} days")
    print()

    # 5. Generate insights
    insights = generate_insights(
        closed_trades, multipliers, details, boost_signals, penalty_signals, confidence
    )

    # Build output
    learned_weights = {
        "last_updated": now_et().isoformat(),
        "total_trades_analyzed": total_closed,
        "strategy_multipliers": multipliers,
        "strategy_details": details,
        "boost_signals": [s["signal"] for s in boost_signals],
        "penalty_signals": [s["signal"] for s in penalty_signals],
        "boost_signal_details": boost_signals,
        "penalty_signal_details": penalty_signals,
        "price_range_preferences": price_preferences,
        "recommended_hold_days": hold_recommendations,
        "insights": insights,
        "confidence": confidence,
    }

    # Save
    safe_save_json(LEARNED_WEIGHTS_PATH, learned_weights)

    # Print summary
    print()
    print("=" * 60)
    print("INSIGHTS")
    print("=" * 60)
    for insight in insights:
        print(f"  - {insight}")
    print()
    print(f"Confidence: {confidence}")
    print(f"Saved to: {LEARNED_WEIGHTS_PATH}")
    print()

    # Print weight changes if any
    changed = [s for s in STRATEGIES if multipliers.get(s, 1.0) != 1.0]
    if changed:
        print("WEIGHT CHANGES:")
        for s in changed:
            print(f"  {s}: {multipliers[s]}x")
    else:
        print("No weight changes (all strategies at 1.0x default).")

    return learned_weights


if __name__ == "__main__":
    run_learning_engine()
