"""Round-61 pt.59 — dead-money detector.

Closes positions that haven't moved meaningfully in 10+ days.
The bot's `max_hold_days` already catches slow-bleed positions but
typically at 30-60 days; by then a stagnant position has tied up
capital that could have funded better setups for weeks.

The dead-money cutter runs daily during the regular monitor cycle
and triggers on:
  * `days_held >= MIN_DAYS_HELD` (default 10), AND
  * `|pnl_pct| < MAX_DRIFT_PCT` (default 2%)

Pure module — no I/O, no globals, dependencies injected so the
function is unit-testable. Exits the position with reason
`dead_money` so the analytics hub's exit-reason breakdown can
report on how much capital this rule recovered.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


MIN_DAYS_HELD: int = 10
MAX_DRIFT_PCT: float = 2.0


def _parse_created_date(created_str) -> Optional[date]:
    """Strategy files store `created` as ``YYYY-MM-DD`` (no tz).
    Returns a `date` or None on parse failure."""
    if not created_str:
        return None
    if isinstance(created_str, date) and not isinstance(created_str, datetime):
        return created_str
    if isinstance(created_str, datetime):
        return created_str.date()
    if isinstance(created_str, str):
        try:
            return datetime.strptime(created_str.strip()[:10],
                                       "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    return None


def is_dead_money(*,
                    created_str,
                    today,
                    entry_price,
                    current_price,
                    min_days_held: int = MIN_DAYS_HELD,
                    max_drift_pct: float = MAX_DRIFT_PCT) -> dict:
    """Decide whether a position has gone dead.

    Args:
      created_str: strategy file's `created` field. Either a string
        ``YYYY-MM-DD``, a ``date``, or a ``datetime``.
      today: today's date (in ET — caller passes get_et_time().date()).
      entry_price: average fill price.
      current_price: latest mark.
      min_days_held: dead-money threshold.
      max_drift_pct: |pnl_pct| below which the position counts as
        not-moving.

    Returns:
      {
        "is_dead": bool,
        "days_held": int | None,
        "pnl_pct": float | None,
        "reason": str,        # short label or "" on no decision
      }

    Conservative on bad inputs — anything we can't parse → returns
    `is_dead=False` so the caller continues with normal exit logic.
    """
    out = {"is_dead": False, "days_held": None,
            "pnl_pct": None, "reason": ""}
    created = _parse_created_date(created_str)
    if created is None:
        out["reason"] = "missing_created_date"
        return out
    if not isinstance(today, date):
        try:
            today = today.date()
        except AttributeError:
            out["reason"] = "bad_today_type"
            return out
    days_held = (today - created).days
    out["days_held"] = days_held
    try:
        ep = float(entry_price)
        cp = float(current_price)
    except (TypeError, ValueError):
        out["reason"] = "bad_price"
        return out
    if ep <= 0:
        out["reason"] = "zero_entry_price"
        return out
    pnl_pct = (cp / ep - 1) * 100.0
    out["pnl_pct"] = round(pnl_pct, 2)
    if days_held < min_days_held:
        out["reason"] = "too_recent"
        return out
    if abs(pnl_pct) >= max_drift_pct:
        out["reason"] = "moved_enough"
        return out
    out["is_dead"] = True
    out["reason"] = (f"dead_money: held {days_held}d, |pnl_pct| "
                       f"{abs(pnl_pct):.1f}% < {max_drift_pct}%")
    return out
