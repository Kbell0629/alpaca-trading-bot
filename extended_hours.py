#!/usr/bin/env python3
"""
Extended hours trading support.
Alpaca supports pre-market (4 AM - 9:30 AM ET) and after-hours (4 PM - 8 PM ET).
This module determines if extended hours trading is appropriate and configures orders.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from et_time import now_et as _now_et

def get_trading_session():
    """Determine the current trading session.
    Returns: 'pre_market', 'market', 'after_hours', or 'closed'

    BUG FIX (audit round 5): previously used a hardcoded -4h offset to
    convert UTC to ET. That silently broke for half of every year during
    EST (winter), shifting the reported session by one full hour — so
    users saw "CLOSED" at 9:30 AM ET from early November through early
    March, and "MARKET OPEN" for the hour after the real close. Now uses
    zoneinfo via the shared ET helper, which handles EDT/EST correctly.
    """
    now = _now_et()

    weekday = now.weekday()  # 0=Monday, 6=Sunday
    hour = now.hour
    minute = now.minute
    time_minutes = hour * 60 + minute

    if weekday >= 5:  # Weekend
        return "closed"

    if time_minutes < 240:  # Before 4 AM
        return "closed"
    elif time_minutes < 570:  # 4:00 AM - 9:30 AM
        return "pre_market"
    elif time_minutes < 960:  # 9:30 AM - 4:00 PM
        return "market"
    elif time_minutes < 1200:  # 4:00 PM - 8:00 PM
        return "after_hours"
    else:
        return "closed"

def should_use_extended_hours(strategy_type, urgency="normal"):
    """Determine if an order should use extended hours.

    Args:
        strategy_type: trailing_stop, mean_reversion, breakout, wheel, copy_trading
        urgency: normal, high (e.g., stop-loss triggered by news)

    Returns: dict with extended_hours flag and reasoning
    """
    session = get_trading_session()

    # During regular market hours, no need for extended hours flag
    if session == "market":
        return {"extended_hours": False, "session": session, "reason": "Regular market hours"}

    # Kill switch and emergency sells should always use extended hours
    if urgency == "high":
        return {"extended_hours": True, "session": session, "reason": "High urgency -- using extended hours"}

    # Strategy-specific rules
    rules = {
        "trailing_stop": {
            "pre_market": False,  # Don't enter new trailing stops pre-market (thin liquidity)
            "after_hours": False,  # Same
            "reason_no": "Trailing stops need good liquidity -- wait for market open"
        },
        "mean_reversion": {
            "pre_market": True,  # Big gaps often happen pre-market, good for mean reversion
            "after_hours": True,
            "reason_yes": "Mean reversion can capture post-earnings gaps"
        },
        "breakout": {
            "pre_market": True,  # Breakouts often start pre-market on news
            "after_hours": False,
            "reason_yes": "Pre-market breakouts on news can be profitable",
            "reason_no": "After-hours breakouts are unreliable"
        },
        "wheel": {
            "pre_market": False,
            "after_hours": False,
            "reason_no": "Options don't trade in extended hours"
        },
        "copy_trading": {
            "pre_market": False,
            "after_hours": False,
            "reason_no": "Copy trades should execute during regular hours for best fills"
        },
    }

    strat_rules = rules.get(strategy_type, {})
    use_extended = strat_rules.get(session, False)
    reason = strat_rules.get(f"reason_{'yes' if use_extended else 'no'}", "Default rules applied")

    return {
        "extended_hours": use_extended,
        "session": session,
        "reason": reason,
        "warning": "Extended hours have wider spreads and lower liquidity" if use_extended else None
    }

def get_order_params(strategy_type, urgency="normal"):
    """Get order parameters including extended_hours flag."""
    eh = should_use_extended_hours(strategy_type, urgency)
    params = {}
    if eh["extended_hours"]:
        params["extended_hours"] = True
        # Extended hours orders must be limit orders (not market)
        params["force_limit_order"] = True
        params["note"] = eh["reason"]
    return {**eh, "order_params": params}

if __name__ == "__main__":
    session = get_trading_session()
    print(f"Current session: {session}")
    for strat in ["trailing_stop", "mean_reversion", "breakout", "wheel", "copy_trading"]:
        result = should_use_extended_hours(strat)
        print(f"  {strat}: extended_hours={result['extended_hours']} -- {result['reason']}")
