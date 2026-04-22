"""
Round-50: precise settled-funds tracking for cash accounts.

Cash accounts are subject to T+1 (since 2024-05-28) settlement:
proceeds from a stock sale can't be used to buy again until the
sale settles one business day later. Using unsettled funds causes
a "Good Faith Violation" — 3 GFVs in 12 months and Alpaca freezes
the account for 90 days.

Simple approach (what some bots do): use Alpaca's
`cash_withdrawable` field as the settled-cash proxy. Works for most
cases but misses edge scenarios (large pending deposits, cash
already committed to unfilled buy orders, etc.).

Precise approach (this module): maintain a per-user ledger of
SALE lots. Each entry is `{"symbol", "amount", "settles_on"}`.
When the bot asks "can I spend $X?":
  1. Start from today's FREE settled cash = cash - sum(lots not yet settled)
  2. Subtract any reserved/pending amount
  3. Return True if >= desired_spend

Ledger is per-user + per-mode (paper vs live) so live and paper
don't confuse each other.

The ledger is best-effort — if Alpaca's authoritative
cash_withdrawable diverges from our calculation (happens during
short sells, dividend pays, etc.), we trust Alpaca and re-sync.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

LEDGER_FILENAME = "settled_funds_ledger.json"
# T+1 settlement (changed from T+2 in 2024). Keep as constant so we can
# flip back if SEC changes the rule again.
SETTLEMENT_DAYS = 1
# Retain ledger entries for 30 days after settlement for audit.
LEDGER_RETENTION_DAYS = 30
# Buffer on settled-cash usage: deploy at most 95% to leave room for
# order-fee drift, slippage, and rounding Good Faith Violation risk.
SETTLED_CASH_BUFFER = 0.95


def _ledger_path(user: dict) -> str:
    data_dir = user.get("_data_dir")
    if not data_dir:
        import tempfile
        data_dir = tempfile.gettempdir()
    return os.path.join(data_dir, LEDGER_FILENAME)


def _next_business_day(d: date, n: int = 1) -> date:
    """Add n US business days (Mon-Fri). Doesn't account for federal
    market holidays — for exact settlement dates use Alpaca's clock
    endpoint. This is close enough for local budget math."""
    result = d
    while n > 0:
        result += timedelta(days=1)
        if result.weekday() < 5:  # Mon=0, Fri=4
            n -= 1
    return result


def _load_ledger(user: dict) -> list:
    path = _ledger_path(user)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    # Prune old entries
    cutoff = (date.today() - timedelta(days=LEDGER_RETENTION_DAYS)).isoformat()
    return [e for e in data
            if isinstance(e, dict) and (e.get("settles_on") or "") >= cutoff]


def _save_ledger(user: dict, ledger: list) -> None:
    path = _ledger_path(user)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(ledger, f, indent=2)
        os.rename(tmp, path)
    except OSError:
        try: os.unlink(tmp)
        except OSError: pass


def record_sale(user: dict, symbol: str, proceeds: float,
                 sold_on: Optional[date] = None) -> None:
    """Record a sale — proceeds are unsettled until T+1. Best-effort;
    never raises. If proceeds <= 0 or the inputs are malformed, skip."""
    try:
        if proceeds is None or float(proceeds) <= 0:
            return
    except (TypeError, ValueError):
        return
    sold_on = sold_on or date.today()
    settles_on = _next_business_day(sold_on, SETTLEMENT_DAYS)
    try:
        ledger = _load_ledger(user)
        ledger.append({
            "sold_on": sold_on.isoformat(),
            "settles_on": settles_on.isoformat(),
            "symbol": (symbol or "").upper(),
            "amount": round(float(proceeds), 2),
        })
        _save_ledger(user, ledger)
    except Exception:
        pass


def unsettled_cash(user: dict, as_of: Optional[date] = None) -> float:
    """Sum of proceeds from sales that haven't settled as of `as_of`
    (defaults to today)."""
    as_of = as_of or date.today()
    try:
        ledger = _load_ledger(user)
    except Exception:
        return 0.0
    total = 0.0
    cutoff = as_of.isoformat()
    for e in ledger:
        if not isinstance(e, dict):
            continue
        settles = e.get("settles_on") or ""
        if settles > cutoff:
            try:
                total += float(e.get("amount") or 0)
            except (TypeError, ValueError):
                pass
    return round(total, 2)


def settled_cash_available(user: dict, total_cash: float,
                             as_of: Optional[date] = None) -> float:
    """Compute settled cash = total_cash - unsettled_cash, minus buffer.
    Never returns negative (a settled balance of $-5 means 0 usable).
    """
    try:
        total = float(total_cash)
    except (TypeError, ValueError):
        return 0.0
    unsettled = unsettled_cash(user, as_of)
    net = total - unsettled
    usable = net * SETTLED_CASH_BUFFER
    return max(0.0, round(usable, 2))


def can_deploy(user: dict, desired_spend: float,
                 total_cash: float,
                 tier_cfg: Optional[dict] = None,
                 as_of: Optional[date] = None) -> tuple:
    """Check whether the desired spend is safe under settled-funds
    rules. Returns (ok: bool, settled_usable: float, reason: str).

    For MARGIN accounts (tier_cfg.settled_funds_required == False),
    always returns True without consulting the ledger — margin buys
    don't have the T+1 constraint.

    For CASH accounts, respects the ledger. If the desired spend
    exceeds settled-usable cash, returns False with a reason the
    dashboard can surface to the user.
    """
    if tier_cfg and not tier_cfg.get("settled_funds_required", True):
        return True, float("inf"), ""
    try:
        spend = float(desired_spend)
    except (TypeError, ValueError):
        return False, 0.0, "invalid desired_spend"
    if spend <= 0:
        return True, 0.0, ""
    usable = settled_cash_available(user, total_cash, as_of)
    if spend <= usable:
        return True, usable, ""
    unsettled = unsettled_cash(user, as_of)
    # Find earliest settlement date so we can tell the user when
    # funds will be available.
    try:
        ledger = _load_ledger(user)
        future = [e.get("settles_on") for e in ledger
                  if isinstance(e, dict)
                  and (e.get("settles_on") or "") > (as_of or date.today()).isoformat()]
        earliest = min(future) if future else None
    except Exception:
        earliest = None
    reason = (f"Insufficient settled cash: need ${spend:.2f}, "
              f"have ${usable:.2f} settled "
              f"(${unsettled:.2f} unsettled from recent sales).")
    if earliest:
        reason += f" Next settlement: {earliest}."
    reason += (" Good Faith Violation prevention — wait for funds to settle "
               "before deploying.")
    return False, usable, reason
