"""Round-61 pt.72 — per-symbol cooldown after stop-out.

We have a bot-wide 60-min cooldown after any loss to prevent
revenge trading. But nothing prevents re-deploying the SAME symbol
the next morning if its 30-min screener score recovers.

Real-world example: SOXL stops out at -8% Tuesday afternoon. The
next morning the screener still has SOXL in the top 5 because
its momentum stats haven't rolled over yet. Bot deploys again.
SOXL drops another 5%. Lather, rinse, "death by a thousand
re-entries on a falling knife".

This module records every stop-out and provides a 24h block on
re-deploying that symbol. Pure module — caller persists the
cooldown state and reads it back.

Use:
    >>> from symbol_cooldown import record_stop_out, is_on_cooldown
    >>> record_stop_out(state, "SOXL", reason="stop_hit")
    >>> is_on_cooldown(state, "SOXL")  # → True for 24h
    True
"""
from __future__ import annotations

from typing import Mapping, Optional


# Default cooldown window. 24h covers a full overnight gap so the
# next morning's screener can't re-deploy. Caller can pass
# different values for different exit reasons.
DEFAULT_COOLDOWN_SECONDS: int = 24 * 60 * 60

# Exit reasons that should trigger a cooldown. Profit-target hits
# and target_hit are NOT cooldowns — those are good closes.
COOLDOWN_TRIGGERING_REASONS = frozenset({
    "stop_hit",
    "stop_loss",
    "trailing_stop",
    "bearish_news",       # pt.71 news exit
    "dead_money",         # pt.59 dead-money cutter
})


def record_stop_out(state: dict,
                      symbol: str,
                      reason: str,
                      *,
                      now_ts: Optional[float] = None,
                      cooldown_sec: int = DEFAULT_COOLDOWN_SECONDS,
                      ) -> bool:
    """Record a stop-out into ``state``. Returns True if a cooldown
    was set, False if the reason wasn't cooldown-triggering (e.g.
    target_hit).

    ``state`` is a dict ``{symbol_upper: {"until": ts, "reason": str,
    "cooldown_sec": int}}`` that the caller persists across runs
    (e.g. in `_last_runs` or a JSON file).
    """
    if not isinstance(state, dict):
        return False
    if not symbol or not reason:
        return False
    reason_l = (reason or "").lower()
    if reason_l not in COOLDOWN_TRIGGERING_REASONS:
        return False
    import time as _t
    now = now_ts if now_ts is not None else _t.time()
    state[(symbol or "").upper()] = {
        "until": now + cooldown_sec,
        "reason": reason_l,
        "set_at": now,
        "cooldown_sec": cooldown_sec,
    }
    return True


def is_on_cooldown(state: Optional[Mapping],
                     symbol: str,
                     *,
                     now_ts: Optional[float] = None,
                     ) -> bool:
    """Return True if `symbol` was stopped-out within the last
    cooldown window. False on missing state or expired entry."""
    if not isinstance(state, Mapping):
        return False
    if not symbol:
        return False
    entry = state.get((symbol or "").upper())
    if not isinstance(entry, Mapping):
        return False
    try:
        until = float(entry.get("until") or 0)
    except (TypeError, ValueError):
        return False
    import time as _t
    now = now_ts if now_ts is not None else _t.time()
    return now < until


def cooldown_remaining_sec(state: Optional[Mapping],
                              symbol: str,
                              *,
                              now_ts: Optional[float] = None,
                              ) -> float:
    """Return seconds remaining on the cooldown, or 0.0 if not on
    cooldown."""
    if not isinstance(state, Mapping):
        return 0.0
    entry = state.get((symbol or "").upper())
    if not isinstance(entry, Mapping):
        return 0.0
    try:
        until = float(entry.get("until") or 0)
    except (TypeError, ValueError):
        return 0.0
    import time as _t
    now = now_ts if now_ts is not None else _t.time()
    remaining = until - now
    return max(0.0, round(remaining, 1))


def explain_cooldown(state: Optional[Mapping],
                       symbol: str,
                       *,
                       now_ts: Optional[float] = None,
                       ) -> str:
    """Human-readable reason for the cooldown — surfaces in
    skip_reasons / filter_reasons."""
    remaining = cooldown_remaining_sec(state, symbol, now_ts=now_ts)
    if remaining <= 0:
        return ""
    if not isinstance(state, Mapping):
        return ""
    entry = state.get((symbol or "").upper()) or {}
    reason = entry.get("reason") or "stopped_out"
    hours = remaining / 3600
    return (f"cooldown_after_{reason}: "
              f"{hours:.1f}h remaining")


def prune_expired(state: dict, *, now_ts: Optional[float] = None) -> int:
    """Remove expired cooldown entries from `state` (mutates in
    place). Returns the count pruned. Safe to call on the caller's
    persistence dict every monitor tick."""
    if not isinstance(state, dict):
        return 0
    import time as _t
    now = now_ts if now_ts is not None else _t.time()
    pruned = 0
    for sym in list(state.keys()):
        entry = state.get(sym)
        if not isinstance(entry, Mapping):
            continue
        try:
            until = float(entry.get("until") or 0)
        except (TypeError, ValueError):
            until = 0
        if until <= now:
            del state[sym]
            pruned += 1
    return pruned
