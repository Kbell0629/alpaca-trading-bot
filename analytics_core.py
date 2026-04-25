"""Round-61 pt.46 — analytics core: pure aggregate math for the
Analytics Hub dashboard tab.

Consolidates every "user wants to know how the bot is doing" metric
into one module so the dashboard renders from a single API call
instead of stitching together /api/data + /api/scorecard +
/api/trades + /api/perf-attribution.

Inputs (all parameters — no I/O, no globals):
  * `journal`  — parsed trade_journal.json
  * `scorecard` — parsed scorecard.json (daily_snapshots + readiness)
  * `account`   — parsed Alpaca /account dict (or None)
  * `picks`     — current top-50 screener picks (for filter summary)

Output: a dict with these top-level keys, all stable so the
dashboard renderer can rely on them:

    {
      "kpis":              {...top-line numbers},
      "equity_curve":      [{date, value}, ...],
      "drawdown_curve":    [{date, drawdown_pct}, ...],
      "strategy_breakdown": {strategy: {...stats}},
      "pnl_by_period":     {7d, 30d, 90d, all},
      "pnl_by_symbol":     [{symbol, pnl, count}, ...],
      "pnl_by_exit_reason": [{reason, pnl, count}, ...],
      "hold_time_distribution": [{bucket, count}, ...],
      "pnl_distribution":  [{bucket, count}, ...],
      "best_trades":       [trade, ...] (top 5 by pnl),
      "worst_trades":      [trade, ...] (bottom 5),
      "filter_summary":    {reason: count} (from screener picks),
      "session":           {paper_validation_days, etc},
    }
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ============================================================================
# ISO/datetime helpers
# ============================================================================

def _parse_iso(ts):
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


def _today():
    return datetime.now(timezone.utc)


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ============================================================================
# KPIs
# ============================================================================

def compute_headline_kpis(journal, scorecard=None, account=None,
                            now=None):
    """Top-line cards for the Analytics Hub. Single dict so the
    renderer can pull whatever it needs by key.

    Returns:
      total_trades, closed_trades, open_trades,
      wins, losses, win_rate,
      total_realized_pnl, avg_pnl, expectancy,
      best_trade_pnl, worst_trade_pnl,
      total_unrealized_pnl, portfolio_value,
      sharpe_ratio, max_drawdown_pct,
      paper_validation_days_elapsed,
      first_trade_date, last_trade_date,
      avg_hold_days
    """
    now = now or _today()
    trades = list((journal or {}).get("trades") or [])
    closed = [t for t in trades
               if isinstance(t, dict)
               and (t.get("status") or "open").lower() == "closed"]
    open_trades = [t for t in trades
                    if isinstance(t, dict)
                    and (t.get("status") or "open").lower() != "closed"]

    wins = sum(1 for t in closed if _safe_float(t.get("pnl")) > 0.005)
    losses = sum(1 for t in closed if _safe_float(t.get("pnl")) < -0.005)
    cnt = len(closed)
    win_rate = (wins / cnt) if cnt else 0.0

    pnls = [_safe_float(t.get("pnl")) for t in closed]
    total_pnl = sum(pnls)
    avg_pnl = (total_pnl / cnt) if cnt else 0.0
    win_pnls = [p for p in pnls if p > 0.005]
    loss_pnls = [p for p in pnls if p < -0.005]
    avg_win = (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0
    avg_loss = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0
    expectancy = (
        win_rate * avg_win
        + ((losses / cnt) if cnt else 0.0) * avg_loss
    )
    best = max(pnls) if pnls else 0.0
    worst = min(pnls) if pnls else 0.0

    # Hold-day stats from closed trades
    hold_days = []
    for t in closed:
        a = _parse_iso(t.get("timestamp"))
        b = _parse_iso(t.get("exit_timestamp"))
        if a and b:
            hold_days.append((b - a).total_seconds() / 86400.0)
    avg_hold = (sum(hold_days) / len(hold_days)) if hold_days else None

    # Account / unrealized from Alpaca
    account = account or {}
    portfolio_value = _safe_float(account.get("portfolio_value"))
    unrealized_pnl = 0.0
    # If positions array is on account dict (some helpers attach it)
    positions = account.get("positions") if isinstance(account, dict) else None
    if isinstance(positions, list):
        for p in positions:
            if isinstance(p, dict):
                unrealized_pnl += _safe_float(p.get("unrealized_pl"))

    # Sharpe + drawdown from scorecard if present
    sc = scorecard or {}
    sharpe = _safe_float(sc.get("sharpe_ratio"), default=None) \
        if sc.get("sharpe_ratio") is not None else None
    max_dd = _safe_float(sc.get("max_drawdown_pct"), default=None) \
        if sc.get("max_drawdown_pct") is not None else None

    # First / last trade dates
    closed_dates = [_parse_iso(t.get("timestamp")) for t in closed]
    closed_dates = [d for d in closed_dates if d]
    first_trade = min(closed_dates) if closed_dates else None
    last_trade = max(closed_dates) if closed_dates else None

    # Paper-validation window: bot started 2026-04-15 per CLAUDE.md
    # — fall back to first_trade if older.
    paper_start = datetime(2026, 4, 15, tzinfo=timezone.utc)
    if first_trade and first_trade < paper_start:
        paper_start = first_trade
    days_elapsed = (now - paper_start).days

    return {
        "total_trades": len(trades),
        "closed_trades": cnt,
        "open_trades": len(open_trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_realized_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 2),
        "avg_win_pnl": round(avg_win, 2),
        "avg_loss_pnl": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "best_trade_pnl": round(best, 2),
        "worst_trade_pnl": round(worst, 2),
        "total_unrealized_pnl": round(unrealized_pnl, 2),
        "portfolio_value": round(portfolio_value, 2),
        "sharpe_ratio": (round(sharpe, 2) if sharpe is not None else None),
        "max_drawdown_pct": (round(max_dd, 2)
                              if max_dd is not None else None),
        "first_trade_date": (first_trade.date().isoformat()
                              if first_trade else None),
        "last_trade_date": (last_trade.date().isoformat()
                             if last_trade else None),
        "avg_hold_days": (round(avg_hold, 2)
                           if avg_hold is not None else None),
        "paper_validation_days_elapsed": days_elapsed,
    }


# ============================================================================
# Equity & drawdown curves
# ============================================================================

def compute_equity_curve(scorecard):
    """Pull `daily_snapshots` from the scorecard and convert into a
    sorted list of ``[{date, value}]`` points.

    The scorecard's daily_snapshots schema (from update_scorecard.py)
    is ``[{"date": "YYYY-MM-DD", "portfolio_value": float, ...}]``.
    """
    sc = scorecard or {}
    snaps = sc.get("daily_snapshots") or []
    out = []
    for s in snaps:
        if not isinstance(s, dict):
            continue
        d = s.get("date")
        v = s.get("portfolio_value")
        if d is None or v is None:
            continue
        try:
            out.append({"date": str(d), "value": float(v)})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p["date"])
    return out


def compute_drawdown_curve(equity_curve):
    """Convert an equity curve to a running-drawdown curve.

    drawdown_pct[i] = (peak_so_far - value[i]) / peak_so_far * -100
    (negative numbers — drawdown is "below peak").

    Returns ``[{date, drawdown_pct, peak}]`` aligned with input.
    """
    if not equity_curve:
        return []
    out = []
    peak = 0.0
    for p in equity_curve:
        v = p.get("value")
        if v is None:
            continue
        if v > peak:
            peak = v
        dd = ((v - peak) / peak * 100.0) if peak > 0 else 0.0
        out.append({
            "date": p["date"],
            "value": v,
            "peak": peak,
            "drawdown_pct": round(dd, 2),
        })
    return out


# ============================================================================
# Per-period P&L
# ============================================================================

def compute_pnl_by_period(journal, now=None):
    """Sum P&L for the last 7 / 30 / 90 days plus all-time, using
    each closed trade's exit_timestamp."""
    now = now or _today()
    trades = list((journal or {}).get("trades") or [])
    buckets = {"7d": 0.0, "30d": 0.0, "90d": 0.0, "all": 0.0,
                "today": 0.0}
    counts = {"7d": 0, "30d": 0, "90d": 0, "all": 0, "today": 0}
    today_date = now.date()
    for t in trades:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        pnl = _safe_float(t.get("pnl"))
        exit_dt = _parse_iso(t.get("exit_timestamp"))
        if not exit_dt:
            continue
        age = (now - exit_dt).days
        buckets["all"] += pnl
        counts["all"] += 1
        if exit_dt.date() == today_date:
            buckets["today"] += pnl
            counts["today"] += 1
        if age <= 7:
            buckets["7d"] += pnl
            counts["7d"] += 1
        if age <= 30:
            buckets["30d"] += pnl
            counts["30d"] += 1
        if age <= 90:
            buckets["90d"] += pnl
            counts["90d"] += 1
    return {
        period: {"pnl": round(buckets[period], 2),
                  "count": counts[period]}
        for period in ("today", "7d", "30d", "90d", "all")
    }


# ============================================================================
# Per-symbol + per-exit-reason aggregates
# ============================================================================

def compute_pnl_by_symbol(journal, top_n=10):
    """Aggregate P&L by symbol (OCC option contracts resolve to
    underlying). Returns sorted list of top-N best + bottom-N worst
    in one pass: list ordered by absolute |pnl| desc, top_n entries."""
    trades = list((journal or {}).get("trades") or [])
    by_sym = {}
    for t in trades:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        sym = (t.get("symbol") or "").upper()
        if not sym:
            continue
        # OCC underlying resolution
        if (len(sym) >= 15 and sym[-15:-9].isdigit()
                and sym[-9] in ("P", "C")
                and sym[-8:].isdigit()):
            sym = sym[:-15]
        slot = by_sym.setdefault(sym, {"pnl": 0.0, "count": 0,
                                          "wins": 0, "losses": 0})
        pnl = _safe_float(t.get("pnl"))
        slot["pnl"] += pnl
        slot["count"] += 1
        if pnl > 0.005:
            slot["wins"] += 1
        elif pnl < -0.005:
            slot["losses"] += 1
    out = [{"symbol": s, "pnl": round(v["pnl"], 2),
             "count": v["count"], "wins": v["wins"],
             "losses": v["losses"],
             "win_rate": (v["wins"] / v["count"]) if v["count"] else 0.0}
            for s, v in by_sym.items()]
    out.sort(key=lambda r: abs(r["pnl"]), reverse=True)
    return out[:top_n]


def compute_pnl_by_exit_reason(journal):
    """Aggregate by `exit_reason` so the user sees which exit codes
    are eating P&L. Useful for tuning stops/targets."""
    trades = list((journal or {}).get("trades") or [])
    by_reason = {}
    for t in trades:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        reason = t.get("exit_reason") or "unknown"
        slot = by_reason.setdefault(reason,
                                       {"pnl": 0.0, "count": 0})
        slot["pnl"] += _safe_float(t.get("pnl"))
        slot["count"] += 1
    out = [{"exit_reason": r, "pnl": round(v["pnl"], 2),
             "count": v["count"]}
            for r, v in by_reason.items()]
    out.sort(key=lambda r: r["pnl"])  # worst first → user sees the
                                        # leakers
    return out


# ============================================================================
# Distributions
# ============================================================================

def compute_hold_time_distribution(journal):
    """Buckets: <1d, 1-3d, 3-7d, 7-14d, 14-30d, 30d+."""
    edges = [(0, 1, "<1d"), (1, 3, "1-3d"), (3, 7, "3-7d"),
              (7, 14, "7-14d"), (14, 30, "14-30d"),
              (30, 99999, "30d+")]
    counts = {label: 0 for _, _, label in edges}
    trades = list((journal or {}).get("trades") or [])
    for t in trades:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        a = _parse_iso(t.get("timestamp"))
        b = _parse_iso(t.get("exit_timestamp"))
        if not a or not b:
            continue
        days = (b - a).total_seconds() / 86400.0
        for lo, hi, label in edges:
            if lo <= days < hi:
                counts[label] += 1
                break
    return [{"bucket": label, "count": counts[label]}
            for _, _, label in edges]


def compute_pnl_distribution(journal):
    """P&L buckets: big-loss (<-$200), -200..-50, -50..0, 0..50,
    50..200, big-win (>$200)."""
    edges = [
        (-1e9, -200, "big_loss"),
        (-200, -50, "loss"),
        (-50, 0, "small_loss"),
        (0, 50, "small_win"),
        (50, 200, "win"),
        (200, 1e9, "big_win"),
    ]
    labels_friendly = {
        "big_loss": "<-$200",
        "loss": "-$200 to -$50",
        "small_loss": "-$50 to $0",
        "small_win": "$0 to $50",
        "win": "$50 to $200",
        "big_win": ">$200",
    }
    counts = {label: 0 for _, _, label in edges}
    trades = list((journal or {}).get("trades") or [])
    for t in trades:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        pnl = _safe_float(t.get("pnl"))
        for lo, hi, label in edges:
            if lo <= pnl < hi:
                counts[label] += 1
                break
    return [{"bucket": labels_friendly[label],
              "key": label,
              "count": counts[label]}
            for _, _, label in edges]


# ============================================================================
# Best / worst single trades
# ============================================================================

def compute_best_worst_trades(journal, top_n=5):
    """Return ``{"best": [...], "worst": [...]}``: top-N highest-pnl
    and lowest-pnl closed trades."""
    trades = [t for t in (journal or {}).get("trades") or []
               if isinstance(t, dict)
               and (t.get("status") or "open").lower() == "closed"
               and t.get("pnl") is not None]
    sorted_trades = sorted(trades, key=lambda t: _safe_float(t.get("pnl")))
    best = list(reversed(sorted_trades[-top_n:]))
    worst = sorted_trades[:top_n]
    keys = ("symbol", "strategy", "pnl", "pnl_pct", "exit_reason",
             "timestamp", "exit_timestamp", "qty", "price",
             "exit_price")
    return {
        "best": [{k: t.get(k) for k in keys} for t in best],
        "worst": [{k: t.get(k) for k in keys} for t in worst],
    }


# ============================================================================
# Filter summary (from current screener picks)
# ============================================================================

def compute_filter_summary(picks):
    """Count screener picks by filter_reason. Identifies the gates
    blocking the most candidates so the user sees what's being
    filtered most often."""
    counts = {}
    deployable = 0
    blocked = 0
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        reasons = p.get("filter_reasons") or []
        if not reasons:
            deployable += 1
            continue
        blocked += 1
        for r in reasons:
            # Normalize chase_block / volatility_block which include
            # values in the string
            key = str(r).split(" ")[0] if r else "unknown"
            counts[key] = counts.get(key, 0) + 1
    sorted_counts = sorted(
        [{"reason": k, "count": v} for k, v in counts.items()],
        key=lambda r: r["count"], reverse=True,
    )
    return {
        "total_picks": len(picks or []),
        "deployable": deployable,
        "blocked": blocked,
        "by_reason": sorted_counts,
    }


# ============================================================================
# Per-strategy breakdown (delegated to trades_analysis_core)
# ============================================================================

def compute_strategy_breakdown(journal):
    """Per-strategy aggregate stats. Mirrors trades_analysis_core's
    `compute_strategy_summary` but inlined here so the analytics
    module is self-contained for testing."""
    trades = list((journal or {}).get("trades") or [])
    by_strategy = {}
    for t in trades:
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
            "count": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best": None, "worst": None,
        })
        slot["count"] += 1
        slot["total_pnl"] += pnl
        if pnl > 0.005:
            slot["wins"] += 1
        elif pnl < -0.005:
            slot["losses"] += 1
        if slot["best"] is None or pnl > slot["best"]:
            slot["best"] = pnl
        if slot["worst"] is None or pnl < slot["worst"]:
            slot["worst"] = pnl
    for slot in by_strategy.values():
        cnt = slot["count"]
        slot["win_rate"] = (slot["wins"] / cnt) if cnt else 0.0
        slot["avg_pnl"] = (slot["total_pnl"] / cnt) if cnt else 0.0
        slot["total_pnl"] = round(slot["total_pnl"], 2)
        slot["avg_pnl"] = round(slot["avg_pnl"], 2)
        if slot["best"] is not None:
            slot["best"] = round(slot["best"], 2)
        if slot["worst"] is not None:
            slot["worst"] = round(slot["worst"], 2)
    return by_strategy


# ============================================================================
# Round-61 pt.47: score-to-outcome correlation
# ============================================================================

def compute_score_outcome(journal, *, bucket_count: int = 5):
    """Bin closed trades by their `_screener_score` (set at open time
    by the auto-deployer) into ``bucket_count`` quantile buckets, then
    compute win-rate / total P&L / expectancy per bucket.

    This is the meta-validation that pt.46 was missing: did higher-
    scored picks actually win more often, or is the screener's score
    uncorrelated with realised outcome?

    Returns:
      {
        "tracked_trades": int,        # closed trades with _screener_score
        "untracked_trades": int,      # closed trades missing the field
        "total_closed": int,
        "buckets": [                  # ordered low-score → high-score
          {
            "label": "Q1 (lowest)",
            "score_range": [low, high],
            "count": int,
            "wins": int,
            "win_rate": float,
            "total_pnl": float,
            "avg_pnl": float,
            "expectancy": float,
          },
          ...
        ],
        "monotonic_winrate": bool,    # True if win_rate strictly
                                       # non-decreasing across buckets
                                       # (the healthy pattern)
        "monotonic_expectancy": bool,
      }

    If there aren't enough scored trades to populate at least 2
    buckets, returns the same shape with ``buckets=[]`` and
    ``tracked_trades`` reflecting what was found.
    """
    trades = list((journal or {}).get("trades") or [])
    closed = [t for t in trades
                if isinstance(t, dict)
                and (t.get("status") or "open").lower() == "closed"]

    scored = []
    untracked = 0
    for t in closed:
        raw = t.get("_screener_score")
        try:
            s = float(raw)
        except (TypeError, ValueError):
            untracked += 1
            continue
        try:
            pnl = float(t.get("pnl"))
        except (TypeError, ValueError):
            untracked += 1
            continue
        scored.append((s, pnl))

    base = {
        "tracked_trades": len(scored),
        "untracked_trades": untracked,
        "total_closed": len(closed),
        "buckets": [],
        "monotonic_winrate": False,
        "monotonic_expectancy": False,
    }
    # Need at least 2 trades per bucket to make a meaningful bin.
    min_per_bucket = 2
    needed = bucket_count * min_per_bucket
    if len(scored) < needed:
        return base

    scored.sort(key=lambda x: x[0])
    n = len(scored)
    buckets = []
    # Equal-count slicing — last bucket gets the remainder.
    per = n // bucket_count
    for i in range(bucket_count):
        start = i * per
        end = (i + 1) * per if i < bucket_count - 1 else n
        slc = scored[start:end]
        if not slc:
            continue
        scores = [s for s, _p in slc]
        pnls = [p for _s, p in slc]
        wins = sum(1 for p in pnls if p > 0.005)
        losses = sum(1 for p in pnls if p < -0.005)
        cnt = len(slc)
        win_rate = wins / cnt if cnt else 0.0
        loss_rate = losses / cnt if cnt else 0.0
        win_pnls = [p for p in pnls if p > 0.005]
        loss_pnls = [p for p in pnls if p < -0.005]
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / cnt if cnt else 0.0
        expectancy = win_rate * avg_win + loss_rate * avg_loss
        buckets.append({
            "label": _bucket_label(i, bucket_count),
            "score_range": [round(min(scores), 2), round(max(scores), 2)],
            "count": cnt,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "expectancy": round(expectancy, 2),
        })

    # Healthy strategy → win_rate / expectancy strictly non-decreasing
    # from low-score bucket to high-score bucket.
    if len(buckets) >= 2:
        wr = [b["win_rate"] for b in buckets]
        ex = [b["expectancy"] for b in buckets]
        base["monotonic_winrate"] = all(wr[i] <= wr[i + 1]
                                          for i in range(len(wr) - 1))
        base["monotonic_expectancy"] = all(ex[i] <= ex[i + 1]
                                             for i in range(len(ex) - 1))
    base["buckets"] = buckets
    return base


def _bucket_label(idx, total):
    """Q1 .. Q5 with low/high markers."""
    label = f"Q{idx + 1}"
    if idx == 0:
        label += " (lowest)"
    elif idx == total - 1:
        label += " (highest)"
    return label


# ============================================================================
# Round-61 pt.49: score-degradation alerting
# ============================================================================

def check_score_degradation(journal, *,
                              min_trades: int = 30,
                              bucket_count: int = 5) -> dict:
    """Active health check on the screener's score-to-outcome
    correlation. Pt.46 surfaced the panel; pt.49 turns it into an
    alert so silent regression doesn't go unnoticed.

    Returns:
      {
        "degraded": bool,             # True if both monotonic flags
                                       # are False AND tracked_trades
                                       # >= min_trades
        "warning": bool,              # True if EITHER monotonic flag
                                       # is False AND tracked >= min
        "tracked_trades": int,
        "total_closed": int,
        "min_trades": int,
        "monotonic_winrate": bool,
        "monotonic_expectancy": bool,
        "headline": str,              # short notification-ready
                                       # one-liner
        "detail": str,                # longer explanation
      }

    Two thresholds:
      * `degraded`: BOTH flags False — strongest signal that scoring
        is broken. Trip a notification.
      * `warning`: ONE flag False — soft signal; surface in dashboard,
        no notification.

    Below `min_trades` of tracked closed trades, both flags are
    False but `degraded` and `warning` stay False because the sample
    is too small to draw conclusions.
    """
    so = compute_score_outcome(journal, bucket_count=bucket_count)
    tracked = so.get("tracked_trades", 0)
    mwr = bool(so.get("monotonic_winrate"))
    mex = bool(so.get("monotonic_expectancy"))
    out = {
        "degraded": False,
        "warning": False,
        "tracked_trades": tracked,
        "total_closed": so.get("total_closed", 0),
        "min_trades": min_trades,
        "monotonic_winrate": mwr,
        "monotonic_expectancy": mex,
        "headline": "",
        "detail": "",
    }
    if tracked < min_trades:
        out["headline"] = "Score health: insufficient sample"
        out["detail"] = (
            f"Need ≥{min_trades} closed trades with embedded "
            f"screener scores to evaluate score-to-outcome "
            f"correlation. Currently tracking {tracked}.")
        return out

    if not mwr and not mex:
        out["degraded"] = True
        out["warning"] = True
        out["headline"] = "⚠ Screener scoring appears uncorrelated to outcome"
        out["detail"] = (
            f"Across {tracked} closed trades, neither win rate nor "
            f"expectancy increases monotonically with screener score. "
            f"Higher-scored picks aren't winning more often than "
            f"lower-scored picks. The score-ranking system likely "
            f"needs investigation.")
    elif not mwr or not mex:
        out["warning"] = True
        broken = []
        if not mwr:
            broken.append("win rate")
        if not mex:
            broken.append("expectancy")
        out["headline"] = (
            f"Score health: {' + '.join(broken)} not monotonic")
        out["detail"] = (
            f"Across {tracked} closed trades, "
            f"{' and '.join(broken)} does not increase monotonically "
            f"with score. Soft signal — keep monitoring.")
    else:
        out["headline"] = "Score health: OK"
        out["detail"] = (
            f"Across {tracked} closed trades, both win rate and "
            f"expectancy increase monotonically with screener score. "
            f"Scoring is correlated with outcome.")
    return out


# ============================================================================
# End-to-end builder
# ============================================================================

def _safe_risk_parity_weights(journal):
    """Round-61 pt.65: surface risk_parity weights in the analytics
    view. Best-effort — falls back to {} on any error so missing
    journal data never breaks the analytics fetch."""
    try:
        import risk_parity as _rp
        return _rp.compute_risk_parity_weights(journal)
    except Exception:
        return {}


def build_analytics_view(journal=None, scorecard=None, account=None,
                          picks=None, now=None):
    """Single end-to-end call. Returns the full analytics payload
    described at the module docstring."""
    equity = compute_equity_curve(scorecard)
    drawdown = compute_drawdown_curve(equity)
    return {
        "kpis": compute_headline_kpis(journal, scorecard, account, now),
        "equity_curve": equity,
        "drawdown_curve": drawdown,
        "strategy_breakdown": compute_strategy_breakdown(journal),
        "pnl_by_period": compute_pnl_by_period(journal, now),
        "pnl_by_symbol": compute_pnl_by_symbol(journal),
        "pnl_by_exit_reason": compute_pnl_by_exit_reason(journal),
        "hold_time_distribution": compute_hold_time_distribution(journal),
        "pnl_distribution": compute_pnl_distribution(journal),
        "best_worst_trades": compute_best_worst_trades(journal),
        "filter_summary": compute_filter_summary(picks),
        "score_outcome": compute_score_outcome(journal),
        "score_health": check_score_degradation(journal),
        # Round-61 pt.65: risk-parity strategy weights — read-only
        # for the dashboard. Inverse-σ weighting; sums to 1.0.
        "risk_parity_weights": _safe_risk_parity_weights(journal),
    }
