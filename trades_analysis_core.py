"""Round-61 pt.36 — pure analysis helpers for the trades dashboard.

Extracted following the pt.7/pt.34 pattern: pure functions that take
a journal dict + filter args and return view-ready data structures.
The HTTP handler (`handlers/actions_mixin.handle_trades_view`) wraps
this with read access; this module is fully unit-testable.

Surface:
  * `filter_trades(trades, filters)` — apply user-selected filters
    (strategy, win/loss, date range, symbol substring, exit reason).
  * `sort_trades(trades, sort_by, descending)` — stable sort by any
    schema field, with sensible numeric coercion for pnl/pnl_pct/qty.
  * `compute_strategy_summary(trades)` — per-strategy aggregates:
    win rate, total pnl, avg pnl, avg hold days, expectancy, count.
  * `compute_overall_summary(trades)` — top-line totals across all
    closed trades (count, win count, total pnl, avg pnl, win rate).
  * `enrich_trade(trade)` — derived view fields the dashboard needs:
    hold_days, pnl_class ('win' / 'loss' / 'flat'), display label,
    is_winner, exit_reason_human.
  * `build_trades_view(journal, filters, sort_by, descending)` —
    end-to-end glue: enrich → filter → sort → return list + summary.

Schema reminders (from cloud_scheduler.record_trade_close +
record_trade_open):
  * timestamp           — ISO entry time
  * symbol              — ticker (or OCC option symbol)
  * strategy            — one of constants.STRATEGY_NAMES
  * side                — 'buy' (long) or 'sell' (short open)
  * qty                 — share count (positive)
  * price               — entry fill price
  * reason              — entry reason / signal summary
  * deployer            — 'cloud_scheduler' / 'wheel_strategy' / etc.
  * status              — 'open' or 'closed'
  * exit_timestamp      — ISO exit time (only on closed)
  * exit_price          — exit fill price (only on closed)
  * exit_reason         — string code (target_hit, short_stop_covered,
                          pead_window_complete, etc.)
  * pnl                 — realized P&L $ (only on closed)
  * pnl_pct             — realized P&L % (only on closed)
  * exit_side           — 'sell' (long close) or 'buy' (short cover)
  * orphan_close        — True for synthetic closes (round-34)
"""
from __future__ import annotations

from datetime import datetime, timezone


# ============================================================================
# Constants
# ============================================================================

# Human-readable rendering of the machine exit_reason codes the
# scheduler / monitor write into the journal. Anything not in this
# map falls back to a Title-Case version of the code.
EXIT_REASON_LABELS = {
    "target_hit": "Profit target hit",
    "short_target_hit": "Short profit target hit",
    "short_stop_covered": "Short stopped out (cover)",
    "stop_triggered": "Stop-loss triggered",
    "pead_window_complete": "PEAD hold window complete",
    "pre_earnings_exit": "Pre-earnings exit",
    "max_hold_exceeded": "Max hold days exceeded",
    "closed_externally": "Closed externally (broker)",
    "manual_close": "Manually closed",
    "orphan_close": "Orphan close (no entry record)",
    "wheel_assigned": "Wheel: shares assigned",
    "wheel_called_away": "Wheel: shares called away",
    "wheel_btc_50pct": "Wheel: bought-to-close at 50% profit",
    "wheel_expired": "Wheel: option expired worthless",
    "kill_switch_flatten": "Kill switch flatten",
    "monthly_rebalance": "Monthly rebalance close",
    "friday_risk_reduction": "Friday risk reduction trim",
    "ladder_10pct": "Profit ladder rung 10%",
    "ladder_20pct": "Profit ladder rung 20%",
    "ladder_30pct": "Profit ladder rung 30%",
    "ladder_50pct": "Profit ladder rung 50%",
}


# ============================================================================
# Per-trade enrichment
# ============================================================================

def _parse_iso(ts):
    """Parse an ISO-8601 string with or without timezone, return aware
    datetime in UTC. Returns None on failure (callers fail open)."""
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _hold_days(open_ts, close_ts):
    """Return hold duration in days as a float. None if either ts
    missing/unparseable."""
    a = _parse_iso(open_ts)
    b = _parse_iso(close_ts)
    if not a or not b:
        return None
    delta = b - a
    return round(delta.total_seconds() / 86400.0, 2)


def enrich_trade(trade):
    """Add view-ready derived fields. Pure: returns a NEW dict, leaves
    input untouched. Safe on already-enriched trades (idempotent).

    Adds / overwrites:
      * hold_days        — float or None
      * pnl_class        — 'win' | 'loss' | 'flat' | 'open'
      * is_winner        — bool (True only for closed trades with pnl>0)
      * is_open          — bool
      * exit_reason_human — pretty label or original code
      * occ_underlying    — for OCC option symbols, the underlying
                            ticker; for normal stocks, same as symbol.
                            Lets the dashboard group HIMS260508P00027000
                            with HIMS.
    """
    if not isinstance(trade, dict):
        return {}
    out = dict(trade)
    status = (out.get("status") or "open").lower()
    out["is_open"] = (status != "closed")

    # P&L class
    if out["is_open"]:
        out["pnl_class"] = "open"
        out["is_winner"] = False
    else:
        try:
            pnl = float(out.get("pnl") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        if pnl > 0.005:  # floor to handle float dust
            out["pnl_class"] = "win"
            out["is_winner"] = True
        elif pnl < -0.005:
            out["pnl_class"] = "loss"
            out["is_winner"] = False
        else:
            out["pnl_class"] = "flat"
            out["is_winner"] = False

    # Hold duration (closed) — None for open trades
    if not out["is_open"]:
        out["hold_days"] = _hold_days(
            out.get("timestamp"), out.get("exit_timestamp"))
    else:
        out["hold_days"] = None

    # Exit reason — pretty version
    raw_reason = out.get("exit_reason")
    if raw_reason:
        out["exit_reason_human"] = EXIT_REASON_LABELS.get(
            raw_reason, str(raw_reason).replace("_", " ").title())
    else:
        out["exit_reason_human"] = None

    # OCC option underlying — the dashboard groups option contracts
    # under their underlying ticker. Match the same regex shape used
    # elsewhere (round-22 multi-contract wheel): UNDERLYING + 6-digit
    # YYMMDD + P|C + 8-digit strike.
    sym = str(out.get("symbol") or "")
    out["occ_underlying"] = _occ_underlying(sym) or sym

    return out


def _occ_underlying(symbol):
    """If `symbol` is an OCC option symbol (e.g. HIMS260508P00027000),
    return the underlying ticker ('HIMS'). Otherwise None."""
    if not symbol or len(symbol) < 15:
        return None
    s = str(symbol)
    # Last 15 chars: 6 digits date + 1 letter P/C + 8 digit strike
    tail = s[-15:]
    if (tail[:6].isdigit() and tail[6] in ("P", "C", "p", "c")
            and tail[7:].isdigit()):
        return s[:-15]
    return None


# ============================================================================
# Filtering
# ============================================================================

def filter_trades(trades, filters=None):
    """Return a new list containing only trades that match every
    active filter. Pure / no mutation.

    Recognised filter keys (all optional — missing/None means "no
    constraint"):
      * status       — 'open' / 'closed' / 'all' (default 'all')
      * strategy     — list of strategy names to include, e.g.
                       ['breakout', 'wheel']
      * win_loss     — 'win' / 'loss' / 'flat' / 'all' (default 'all')
                       — only meaningful for closed trades
      * symbol       — case-insensitive substring; matches
                       OCC underlying too via enrich_trade output
      * exit_reason  — list of reason codes (or 'human' labels)
      * date_from    — ISO string; trades with entry_timestamp >= this
      * date_to      — ISO string; trades with entry_timestamp <= this
      * side         — 'long' / 'short' / 'all' (default 'all').
                       'long'  → trade.side == 'buy'
                       'short' → trade.side == 'sell'
      * min_pnl      — numeric; only trades with pnl >= this
      * max_pnl      — numeric; only trades with pnl <= this
    """
    f = filters or {}
    out = []
    status_filter = (f.get("status") or "all").lower()
    strategies = set(f.get("strategy") or [])
    win_loss = (f.get("win_loss") or "all").lower()
    symbol_q = (f.get("symbol") or "").strip().upper()
    exit_reasons = set(
        r.lower() for r in (f.get("exit_reason") or []) if r)
    side_filter = (f.get("side") or "all").lower()
    date_from = _parse_iso(f.get("date_from"))
    date_to = _parse_iso(f.get("date_to"))
    min_pnl = f.get("min_pnl")
    max_pnl = f.get("max_pnl")

    for t in trades or []:
        if not isinstance(t, dict):
            continue
        is_open = (str(t.get("status") or "open").lower() != "closed")

        # Status
        if status_filter == "open" and not is_open:
            continue
        if status_filter == "closed" and is_open:
            continue

        # Strategy
        if strategies and (t.get("strategy") not in strategies):
            continue

        # Win/loss (only for closed)
        if win_loss != "all":
            if is_open:
                # open trades have no W/L yet — exclude when filtering
                continue
            tc = t.get("pnl_class")
            if tc is None:
                tc = enrich_trade(t).get("pnl_class")
            if tc != win_loss:
                continue

        # Symbol substring (matches base symbol AND OCC underlying)
        if symbol_q:
            sym = str(t.get("symbol") or "").upper()
            underlying = (_occ_underlying(sym) or "").upper()
            if symbol_q not in sym and symbol_q not in underlying:
                continue

        # Exit reason
        if exit_reasons:
            er = str(t.get("exit_reason") or "").lower()
            if er not in exit_reasons:
                continue

        # Side
        if side_filter == "long":
            if str(t.get("side") or "buy").lower() != "buy":
                continue
        elif side_filter == "short":
            if str(t.get("side") or "buy").lower() != "sell":
                continue

        # Date range
        ts = _parse_iso(t.get("timestamp"))
        if date_from and ts and ts < date_from:
            continue
        if date_to and ts and ts > date_to:
            continue

        # P&L bounds (only closed trades)
        if (min_pnl is not None) or (max_pnl is not None):
            try:
                pnl = float(t.get("pnl") or 0)
            except (TypeError, ValueError):
                continue
            if min_pnl is not None and pnl < float(min_pnl):
                continue
            if max_pnl is not None and pnl > float(max_pnl):
                continue

        out.append(t)
    return out


# ============================================================================
# Sorting
# ============================================================================

_NUMERIC_FIELDS = {"pnl", "pnl_pct", "price", "exit_price", "qty",
                    "hold_days"}


def sort_trades(trades, sort_by="exit_timestamp", descending=True):
    """Return a new list sorted by ``sort_by``. Stable; missing values
    ALWAYS sort last regardless of ``descending``.

    Numeric fields (pnl/pnl_pct/price/exit_price/qty/hold_days) coerce
    via float; date fields parse via _parse_iso; everything else sorts
    as a string. Implementation: split the list into present + missing,
    sort the present-only list in the requested direction, then append
    the missing entries in their original (stable) order.
    """
    if not trades:
        return []
    is_numeric = sort_by in _NUMERIC_FIELDS
    is_date = sort_by in ("timestamp", "exit_timestamp")

    def _extract(t):
        """Return (has_value, sort_key). has_value=False marks the
        entry as 'missing'; the sort_key is irrelevant in that case."""
        if not isinstance(t, dict):
            return False, None
        v = t.get(sort_by)
        if v is None or v == "":
            return False, None
        if is_numeric:
            try:
                return True, float(v)
            except (TypeError, ValueError):
                return False, None
        if is_date:
            dt = _parse_iso(v)
            if dt is None:
                return False, None
            return True, dt.timestamp()
        return True, str(v)

    present, missing = [], []
    for t in trades:
        has_value, key = _extract(t)
        if has_value:
            present.append((key, t))
        else:
            missing.append(t)
    # Sort just the present-value entries; stable sort preserves
    # ordering when keys tie.
    present.sort(key=lambda pair: pair[0], reverse=descending)
    return [t for _, t in present] + missing


# ============================================================================
# Aggregation / summary
# ============================================================================

def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def compute_strategy_summary(trades):
    """Per-strategy aggregates over the CLOSED trades in `trades`.
    Returns a dict keyed by strategy name; each value contains:

        count         — number of closed trades
        wins          — count where pnl > 0
        losses        — count where pnl < 0
        flat          — count where pnl == 0
        win_rate      — wins / count (0 if count==0)
        total_pnl     — sum of pnl
        avg_pnl       — total_pnl / count
        avg_hold_days — mean hold days (None if no parsable dates)
        expectancy    — avg_win * win_rate - avg_loss * loss_rate
        best_pnl      — max single-trade pnl
        worst_pnl     — min single-trade pnl

    Open trades and entries with non-numeric pnl are skipped. The
    schema is stable so the dashboard renderer can rely on every key
    being present.
    """
    by_strategy = {}
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        try:
            pnl = float(t.get("pnl"))
        except (TypeError, ValueError):
            continue
        strat = t.get("strategy") or "unknown"
        slot = by_strategy.setdefault(strat, {
            "count": 0, "wins": 0, "losses": 0, "flat": 0,
            "total_pnl": 0.0, "win_pnl_sum": 0.0, "loss_pnl_sum": 0.0,
            "best_pnl": None, "worst_pnl": None,
            "hold_days_sum": 0.0, "hold_days_count": 0,
        })
        slot["count"] += 1
        slot["total_pnl"] += pnl
        if pnl > 0.005:
            slot["wins"] += 1
            slot["win_pnl_sum"] += pnl
        elif pnl < -0.005:
            slot["losses"] += 1
            slot["loss_pnl_sum"] += pnl
        else:
            slot["flat"] += 1
        if slot["best_pnl"] is None or pnl > slot["best_pnl"]:
            slot["best_pnl"] = pnl
        if slot["worst_pnl"] is None or pnl < slot["worst_pnl"]:
            slot["worst_pnl"] = pnl
        hd = _hold_days(t.get("timestamp"), t.get("exit_timestamp"))
        if hd is not None:
            slot["hold_days_sum"] += hd
            slot["hold_days_count"] += 1

    # Finalise — compute derived rates from the running sums
    for strat, slot in by_strategy.items():
        cnt = slot["count"]
        slot["win_rate"] = (slot["wins"] / cnt) if cnt else 0.0
        slot["loss_rate"] = (slot["losses"] / cnt) if cnt else 0.0
        slot["avg_pnl"] = (slot["total_pnl"] / cnt) if cnt else 0.0
        slot["avg_win_pnl"] = (
            slot["win_pnl_sum"] / slot["wins"]) if slot["wins"] else 0.0
        slot["avg_loss_pnl"] = (
            slot["loss_pnl_sum"] / slot["losses"]) if slot["losses"] else 0.0
        # Expectancy: win_rate * avg_win + loss_rate * avg_loss
        # avg_loss is already negative, so this comes out signed.
        slot["expectancy"] = (
            slot["win_rate"] * slot["avg_win_pnl"]
            + slot["loss_rate"] * slot["avg_loss_pnl"]
        )
        slot["avg_hold_days"] = (
            slot["hold_days_sum"] / slot["hold_days_count"]
            if slot["hold_days_count"] else None
        )
        # Drop the running-sum internals; they're not part of the
        # public schema.
        for k in ("win_pnl_sum", "loss_pnl_sum", "hold_days_sum",
                   "hold_days_count"):
            slot.pop(k, None)
    return by_strategy


def compute_overall_summary(trades):
    """Top-line aggregate across ALL closed trades (every strategy).
    Same shape as a single-strategy entry from
    `compute_strategy_summary`, useful for the dashboard's top card."""
    rolled = compute_strategy_summary(trades)
    if not rolled:
        return {
            "count": 0, "wins": 0, "losses": 0, "flat": 0,
            "win_rate": 0.0, "loss_rate": 0.0,
            "total_pnl": 0.0, "avg_pnl": 0.0,
            "avg_win_pnl": 0.0, "avg_loss_pnl": 0.0,
            "expectancy": 0.0,
            "best_pnl": None, "worst_pnl": None,
            "avg_hold_days": None,
        }
    cnt = sum(s["count"] for s in rolled.values())
    wins = sum(s["wins"] for s in rolled.values())
    losses = sum(s["losses"] for s in rolled.values())
    flat = sum(s["flat"] for s in rolled.values())
    total = sum(s["total_pnl"] for s in rolled.values())
    win_pnl = sum(s["avg_win_pnl"] * s["wins"] for s in rolled.values())
    loss_pnl = sum(s["avg_loss_pnl"] * s["losses"] for s in rolled.values())
    best_vals = [s["best_pnl"] for s in rolled.values()
                  if s["best_pnl"] is not None]
    worst_vals = [s["worst_pnl"] for s in rolled.values()
                   if s["worst_pnl"] is not None]
    hd_total = 0.0
    hd_count = 0
    for s in rolled.values():
        if s.get("avg_hold_days") is not None:
            hd_total += s["avg_hold_days"] * s["count"]
            hd_count += s["count"]
    return {
        "count": cnt,
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate": (wins / cnt) if cnt else 0.0,
        "loss_rate": (losses / cnt) if cnt else 0.0,
        "total_pnl": total,
        "avg_pnl": (total / cnt) if cnt else 0.0,
        "avg_win_pnl": (win_pnl / wins) if wins else 0.0,
        "avg_loss_pnl": (loss_pnl / losses) if losses else 0.0,
        "expectancy": (
            ((wins / cnt) * (win_pnl / wins) if wins else 0.0)
            + ((losses / cnt) * (loss_pnl / losses) if losses else 0.0)
        ) if cnt else 0.0,
        "best_pnl": max(best_vals) if best_vals else None,
        "worst_pnl": min(worst_vals) if worst_vals else None,
        "avg_hold_days": (hd_total / hd_count) if hd_count else None,
    }


# ============================================================================
# End-to-end builder
# ============================================================================

def build_trades_view(journal, filters=None, sort_by="exit_timestamp",
                      descending=True, enrich=True):
    """Produce the complete trades-tab payload from a raw journal dict.

    Steps:
      1. Pull `trades` from the journal (default to []).
      2. Optionally enrich each trade (add hold_days, pnl_class, etc.).
      3. Apply filters.
      4. Sort.
      5. Compute strategy + overall summary OVER THE FILTERED LIST so
         the summary cards reflect the user's current view.

    Returns:
        {
            "trades":            [enriched + filtered + sorted],
            "strategy_summary":  {strategy: {...stats}},
            "overall_summary":   {...stats across all filtered closed},
            "filters_applied":   <copy of input>,
            "sort_by":           <field>,
            "descending":        <bool>,
            "total_count":       <int — pre-filter count of all trades>,
            "filtered_count":    <int — post-filter count>,
        }
    """
    trades = list((journal or {}).get("trades") or [])
    total_count = len(trades)
    if enrich:
        trades = [enrich_trade(t) for t in trades]
    trades = filter_trades(trades, filters)
    trades = sort_trades(trades, sort_by=sort_by, descending=descending)
    return {
        "trades": trades,
        "strategy_summary": compute_strategy_summary(trades),
        "overall_summary": compute_overall_summary(trades),
        "filters_applied": filters or {},
        "sort_by": sort_by,
        "descending": bool(descending),
        "total_count": total_count,
        "filtered_count": len(trades),
    }
