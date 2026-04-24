"""
Round-61 pt.7 — pure scorecard math extracted from update_scorecard.py.

update_scorecard.py is the daily-close subprocess: it reads trade
journal + Alpaca account/positions, then computes ~20 performance
metrics (win rate, profit factor, sharpe, sortino, max drawdown,
strategy breakdown, A/B testing, correlation warning, readiness score)
and writes scorecard.json.

Because the whole file runs as a subprocess (spawned by
`cloud_scheduler.run_daily_close`), it's in `pyproject.toml`'s
coverage omit list — the pure math was invisible to pytest-cov.

This module extracts that math. `update_scorecard.py` now imports
from here, behavior identical. `scorecard_core.py` is NOT in the
omit list.

Functions are PURE — no network, no disk, no globals. External
dependencies (`now_et`, `SECTOR_MAP`, `annotate_sector`) are
injectable parameters so tests run deterministically and without
imports of production modules.

Tests in tests/test_round61_pt7_scorecard_core.py.
"""
from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Callable, Mapping, Optional, Sequence


# ============================================================================
# Decimal <-> float helpers — identical to update_scorecard.py's.
# ============================================================================

_CENT = Decimal("0.01")

# Risk-free rate per trading day (~4% annual / 252). Sharpe + Sortino use it.
DEFAULT_RF_DAILY = 0.00016

# Retention cap for daily_snapshots list on trade_journal.json.
DEFAULT_MAX_SNAPSHOTS = 800


def _dec(v, default: Decimal = Decimal("0")) -> Decimal:
    """Coerce to Decimal WITHOUT crossing float. Always via str."""
    if v is None or v == "":
        return default
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return default


def _to_cents_float(v) -> float:
    """Quantize to cents (banker's rounding) and emit a JSON-safe float."""
    if not isinstance(v, Decimal):
        v = _dec(v)
    return float(v.quantize(_CENT, rounding=ROUND_HALF_EVEN))


# ============================================================================
# Strategy-name normalisation — Round-7 audit fix (see update_scorecard.py).
# ============================================================================

def normalize_strategy_name(s) -> str:
    """Canonicalise strategy names to lowercase_underscore form.

    Without this, a journal entry with strategy=\"Copy Trading\" or
    \"trailing-stop\" falls through the strategy_breakdown bucket and
    silently undercounts performance.
    """
    if not s:
        return ""
    return str(s).strip().lower().replace(" ", "_").replace("-", "_")


# ============================================================================
# Trade-status bucketing
# ============================================================================

def count_trade_statuses(trades: Sequence[dict]) -> dict:
    """Return {total, open, closed} counts."""
    total = len(trades)
    open_ = sum(1 for t in trades if t.get("status") == "open")
    closed = sum(1 for t in trades if t.get("status") == "closed")
    return {"total": total, "open": open_, "closed": closed}


def split_wins_losses(trades: Sequence[dict]) -> tuple:
    """Return (closed_with_pnl, wins, losses).

    `wins` = closed with pnl > 0; `losses` = closed with pnl <= 0.
    (Zero P&L trades bucket into losses — matches scorecard behavior.)
    """
    closed = [t for t in trades
              if t.get("status") == "closed" and t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    return closed, wins, losses


def win_rate_pct(wins: Sequence[dict], closed: Sequence[dict]) -> float:
    """Win rate as a percentage (0-100). Zero closed trades → 0."""
    if not closed:
        return 0.0
    return len(wins) / len(closed) * 100


def avg_pnl_pct(trades: Sequence[dict]) -> float:
    """Average pnl_pct across trades. Empty → 0."""
    if not trades:
        return 0.0
    return sum(t.get("pnl_pct", 0) for t in trades) / len(trades)


def profit_factor(wins: Sequence[dict], losses: Sequence[dict]) -> float:
    """Total-wins / |total-losses|. No losses but some wins → float(total_wins)."""
    total_wins_d = sum((_dec(t["pnl"]) for t in wins), Decimal("0"))
    total_losses_d = abs(sum((_dec(t["pnl"]) for t in losses), Decimal("0")))
    if total_losses_d > 0:
        return float(total_wins_d / total_losses_d)
    if total_wins_d > 0:
        return float(total_wins_d)
    return 0.0


def largest_win_loss(wins: Sequence[dict], losses: Sequence[dict]) -> tuple:
    """Return (largest_win_decimal, largest_loss_decimal)."""
    largest_win = max((_dec(t["pnl"]) for t in wins), default=Decimal("0"))
    largest_loss = min((_dec(t["pnl"]) for t in losses), default=Decimal("0"))
    return largest_win, largest_loss


def avg_holding_days(closed: Sequence[dict]) -> float:
    """Mean days between entry `timestamp` and `exit_timestamp`."""
    days = []
    for t in closed:
        ts = t.get("timestamp")
        xts = t.get("exit_timestamp")
        if not ts or not xts:
            continue
        try:
            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            exit_dt = datetime.fromisoformat(xts.replace("Z", "+00:00"))
            days.append((exit_dt - entry_dt).total_seconds() / 86400)
        except (ValueError, TypeError):
            pass
    return sum(days) / len(days) if days else 0.0


# ============================================================================
# Drawdown, returns, ratios
# ============================================================================

def max_drawdown(snapshots: Sequence[dict],
                 starting_capital: Decimal,
                 portfolio_value: Decimal,
                 scorecard_peak: Decimal) -> tuple:
    """Compute max_dd (float %) and the peak Decimal.

    Walks the snapshot equity curve, then compares the running peak
    against `portfolio_value` + `scorecard_peak` as a final safety
    net (so a peak we saw mid-day but never snapshotted still counts).
    """
    peak = starting_capital
    max_dd = 0.0
    for snap in snapshots:
        val = _dec(snap.get("portfolio_value", 0))
        if val > peak:
            peak = val
        if peak > 0:
            dd = float((peak - val) / peak) * 100
            if dd > max_dd:
                max_dd = dd
    peak = max(peak, portfolio_value, scorecard_peak)
    if peak > 0:
        current_dd = float((peak - portfolio_value) / peak) * 100
        max_dd = max(max_dd, current_dd)
    return max_dd, peak


def daily_returns_from_snapshots(snapshots: Sequence[dict]) -> list:
    """Return list of successive daily returns from snapshot equity curve."""
    returns = []
    if len(snapshots) < 2:
        return returns
    for i in range(1, len(snapshots)):
        prev_val = snapshots[i - 1].get("portfolio_value", 0)
        curr_val = snapshots[i].get("portfolio_value", 0)
        if prev_val > 0:
            returns.append(curr_val / prev_val - 1)
    return returns


def sharpe_sortino(daily_returns: Sequence[float],
                   rf_daily: float = DEFAULT_RF_DAILY) -> tuple:
    """Annualised Sharpe + Sortino from a sequence of daily returns.

    Needs at least 2 points. Sortino divides downside-variance by the
    TOTAL n (not len(neg_returns)) — matches the existing update_scorecard
    math (common convention for downside-deviation Sortino).
    """
    n = len(daily_returns)
    if n < 2:
        return 0.0, 0.0
    mean_ret = sum(daily_returns) / n
    variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (n - 1)
    std_ret = math.sqrt(variance) if variance > 0 else 0
    sharpe = 0.0
    if std_ret > 0:
        sharpe = ((mean_ret - rf_daily) / std_ret) * math.sqrt(252)
    sortino = 0.0
    neg_returns = [r for r in daily_returns if r < 0]
    if neg_returns:
        neg_variance = sum(r ** 2 for r in neg_returns) / n
        neg_std = math.sqrt(neg_variance)
        if neg_std > 0:
            sortino = ((mean_ret - rf_daily) / neg_std) * math.sqrt(252)
    return sharpe, sortino


def total_return_pct(portfolio_value: Decimal,
                     starting_capital: Decimal) -> float:
    """Return pct (float). Zero starting capital → 0."""
    if starting_capital <= 0:
        return 0.0
    return float(
        (portfolio_value - starting_capital) / starting_capital
    ) * 100


# ============================================================================
# Strategy breakdown + A/B testing
# ============================================================================

STRATEGY_BUCKETS: tuple = (
    "trailing_stop", "copy_trading", "wheel",
    "mean_reversion", "breakout", "pead",
)


def build_strategy_breakdown(trades: Sequence[dict]) -> dict:
    """Per-strategy {trades, wins, pnl-as-float-cents} breakdown.

    pnl accumulates as Decimal internally then gets cents-rounded to
    float on output — Round-10 + Phase-2 decimal migration contract.
    """
    buckets: dict = {name: {"trades": 0, "wins": 0, "pnl": Decimal("0")}
                     for name in STRATEGY_BUCKETS}
    for t in trades:
        strat = normalize_strategy_name(t.get("strategy", ""))
        if strat not in buckets:
            continue
        buckets[strat]["trades"] += 1
        if t.get("status") == "closed" and t.get("pnl") is not None:
            buckets[strat]["pnl"] += _dec(t["pnl"])
            if t["pnl"] > 0:
                buckets[strat]["wins"] += 1
    for row in buckets.values():
        row["pnl"] = _to_cents_float(row["pnl"])
    return buckets


def build_ab_testing(strategy_breakdown: Mapping[str, dict],
                     min_trades: int = 5) -> dict:
    """Pairwise A/B report for strategies that each have >= min_trades."""
    ab: dict = {}
    names = list(strategy_breakdown.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a_name, b_name = names[i], names[j]
            a, b = strategy_breakdown[a_name], strategy_breakdown[b_name]
            if a["trades"] < min_trades or b["trades"] < min_trades:
                continue
            a_wr = (a["wins"] / a["trades"] * 100) if a["trades"] > 0 else 0
            b_wr = (b["wins"] / b["trades"] * 100) if b["trades"] > 0 else 0
            a_avg = a["pnl"] / a["trades"] if a["trades"] > 0 else 0
            b_avg = b["pnl"] / b["trades"] if b["trades"] > 0 else 0
            if a_avg > b_avg:
                winner = a_name
            elif b_avg > a_avg:
                winner = b_name
            else:
                winner = "tie"
            ab[f"{a_name}_vs_{b_name}"] = {
                a_name: {"trades": a["trades"],
                          "win_rate": round(a_wr, 1),
                          "avg_pnl": round(a_avg, 2)},
                b_name: {"trades": b["trades"],
                          "win_rate": round(b_wr, 1),
                          "avg_pnl": round(b_avg, 2)},
                "better_avg_pnl": winner,
            }
    return ab


# ============================================================================
# Correlation / sector concentration guard
# ============================================================================

def build_correlation_warning(positions: Optional[Sequence[dict]],
                              sector_map: Optional[Mapping[str, str]] = None,
                              annotate_fn: Optional[Callable] = None,
                              threshold: int = 3) -> Optional[dict]:
    """Detect sector concentration (3+ positions in the same sector).

    `annotate_fn` defaults to `position_sector.annotate_sector` — it
    resolves OCC option symbols to their underlying for sector lookup.
    Round-58 fix: without this, a HIMS put routes through the
    literal OCC contract symbol and falls to "Other", producing false
    concentration warnings.

    Tests inject a stub; production passes the real annotator.
    """
    if not positions or not isinstance(positions, list):
        return None
    sector_map = sector_map or {}
    annotated = None
    if annotate_fn is not None:
        try:
            annotated = annotate_fn([dict(p) for p in positions])
        except Exception:
            annotated = None
    sector_positions: dict = {}
    for idx, p in enumerate(positions):
        sym = p.get("symbol", "")
        if annotated and idx < len(annotated):
            ap = annotated[idx]
            sector = ap.get("_sector") or "Other"
            display_sym = ap.get("_underlying") or sym
        else:
            sector = sector_map.get(sym, "Other")
            display_sym = sym
        sector_positions.setdefault(sector, []).append(display_sym)
    concentrated = {s: syms for s, syms in sector_positions.items()
                    if len(syms) >= threshold}
    if not concentrated:
        return None
    details = [f"{sector}: {', '.join(syms)} ({len(syms)} positions)"
               for sector, syms in concentrated.items()]
    return {
        "warning": "Sector concentration detected (3+ positions in same sector)",
        "sectors": concentrated,
        "details": details,
    }


# ============================================================================
# Readiness score
# ============================================================================

def compute_readiness(*, days_tracked: int, win_rate: float, max_dd: float,
                      profit_factor_val: float, sharpe: float,
                      criteria: Mapping) -> tuple:
    """Return (score_0_to_100, ready_for_live). Ready at >=80."""
    score = 0
    if days_tracked >= criteria.get("min_days", 30):
        score += 20
    if win_rate >= criteria.get("min_win_rate", 50):
        score += 20
    if max_dd < criteria.get("max_drawdown", 10):
        score += 20
    if profit_factor_val >= criteria.get("min_profit_factor", 1.5):
        score += 20
    if sharpe >= criteria.get("min_sharpe", 0.5):
        score += 20
    return score, (score >= 80)


# ============================================================================
# Daily snapshot retention
# ============================================================================

def apply_snapshot_retention(snapshots: list,
                             max_count: int = DEFAULT_MAX_SNAPSHOTS) -> list:
    """Cap snapshots at `max_count`, keeping the most-recent entries.

    Prevents unbounded growth of trade_journal.json on long-running
    deployments — ~2 years of dailies at default cap.
    """
    if len(snapshots) > max_count:
        return snapshots[-max_count:]
    return snapshots


# ============================================================================
# Orchestrators — mirror update_scorecard.calculate_metrics /
# take_daily_snapshot, with injectable now/sector-resolver deps.
# ============================================================================

def calculate_metrics(journal: Mapping, scorecard: Mapping,
                     account: Mapping, positions: Optional[Sequence],
                     *,
                     now_fn: Optional[Callable[[], datetime]] = None,
                     sector_map: Optional[Mapping[str, str]] = None,
                     annotate_fn: Optional[Callable] = None) -> dict:
    """Compute the full scorecard dict from journal/account/positions.

    Mirrors `update_scorecard.calculate_metrics` exactly. Dependencies
    injected:
      * now_fn — returns a timezone-aware `datetime` used for
        last_updated. Defaults to `datetime.now()` so the orchestrator
        is usable standalone; production passes `et_time.now_et`.
      * sector_map / annotate_fn — see build_correlation_warning.
    """
    trades = journal.get("trades", [])
    snapshots = journal.get("daily_snapshots", [])

    pv_raw = _dec(account.get("portfolio_value", 0))
    starting_capital_d = _dec(scorecard.get("starting_capital", 100000))
    portfolio_value_d = pv_raw if pv_raw != Decimal("0") else starting_capital_d
    starting_capital = _to_cents_float(starting_capital_d)

    statuses = count_trade_statuses(trades)
    closed, wins, losses = split_wins_losses(trades)

    win_rate = win_rate_pct(wins, closed)
    avg_win_pct_val = avg_pnl_pct(wins)
    avg_loss_pct_val = avg_pnl_pct(losses)
    pf_val = profit_factor(wins, losses)
    largest_win_d, largest_loss_d = largest_win_loss(wins, losses)
    hold_days = avg_holding_days(closed)

    scorecard_peak_d = _dec(scorecard.get("peak_value", starting_capital))
    max_dd_val, peak_value_d = max_drawdown(
        snapshots, starting_capital_d, portfolio_value_d, scorecard_peak_d)

    daily_returns = daily_returns_from_snapshots(snapshots)
    sharpe, sortino = sharpe_sortino(daily_returns)

    total_ret = total_return_pct(portfolio_value_d, starting_capital_d)

    strategy_breakdown = build_strategy_breakdown(trades)
    ab_testing = build_ab_testing(strategy_breakdown)

    correlation_warning = build_correlation_warning(
        positions, sector_map=sector_map, annotate_fn=annotate_fn)

    criteria = scorecard.get("readiness_criteria") or {}
    readiness_score, ready_for_live = compute_readiness(
        days_tracked=len(snapshots),
        win_rate=win_rate,
        max_dd=max_dd_val,
        profit_factor_val=pf_val,
        sharpe=sharpe,
        criteria=criteria,
    )

    if now_fn is None:
        now_fn = datetime.now
    now_str = now_fn().isoformat()

    updated = {
        "start_date": scorecard.get("start_date", "2026-04-15"),
        "starting_capital": starting_capital,
        "current_value": _to_cents_float(portfolio_value_d),
        "total_return_pct": round(total_ret, 2),
        "total_trades": statuses["total"],
        "open_trades": statuses["open"],
        "closed_trades": statuses["closed"],
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win_pct_val, 2),
        "avg_loss_pct": round(avg_loss_pct_val, 2),
        "profit_factor": round(pf_val, 2),
        "largest_win": _to_cents_float(largest_win_d),
        "largest_loss": _to_cents_float(largest_loss_d),
        "max_drawdown_pct": round(max_dd_val, 2),
        "peak_value": _to_cents_float(peak_value_d),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "avg_holding_days": round(hold_days, 1),
        "strategy_breakdown": strategy_breakdown,
        "ab_testing": ab_testing,
        "ready_for_live": ready_for_live,
        "readiness_score": readiness_score,
        "readiness_criteria": criteria if criteria
                               else scorecard.get("readiness_criteria", {}),
        "last_updated": now_str,
    }
    if correlation_warning:
        updated["correlation_warning"] = correlation_warning
    return updated


def take_daily_snapshot(journal: dict, account: Mapping,
                        positions, scorecard: Mapping,
                        *,
                        now_fn: Optional[Callable[[], datetime]] = None,
                        max_snapshots: int = DEFAULT_MAX_SNAPSHOTS) -> dict:
    """Append today's snapshot to journal['daily_snapshots'] in place.

    Returns the new snapshot dict. Deduplicates today's date (replaces
    the existing same-day row). Applies retention cap.
    """
    if now_fn is None:
        now_fn = datetime.now
    today = now_fn().strftime("%Y-%m-%d")
    snapshots = list(journal.get("daily_snapshots", []))

    if snapshots and snapshots[-1].get("date") == today:
        snapshots.pop()

    portfolio_value = float(account.get("portfolio_value", 0))
    cash = float(account.get("cash", 0))
    positions_count = len(positions) if isinstance(positions, list) else 0

    starting_capital = scorecard.get("starting_capital", 100000)
    prev_value = starting_capital
    if snapshots:
        prev_value = snapshots[-1].get("portfolio_value", starting_capital)

    daily_pnl = portfolio_value - prev_value
    daily_pnl_pct = (daily_pnl / prev_value * 100) if prev_value > 0 else 0
    total_pnl = portfolio_value - starting_capital
    total_pnl_pct = (total_pnl / starting_capital * 100) if starting_capital > 0 else 0

    peak = max(starting_capital, scorecard.get("peak_value", starting_capital))
    for s in snapshots:
        v = s.get("portfolio_value", 0)
        if v > peak:
            peak = v
    if portfolio_value > peak:
        peak = portfolio_value
    max_dd = (peak - portfolio_value) / peak * 100 if peak > 0 else 0

    trades = journal.get("trades", [])
    closed_today = sum(
        1 for t in trades
        if t.get("status") == "closed"
        and t.get("exit_timestamp", "").startswith(today)
    )
    wins_today = sum(
        1 for t in trades
        if t.get("status") == "closed"
        and t.get("exit_timestamp", "").startswith(today)
        and t.get("pnl", 0) > 0
    )
    losses_today = sum(
        1 for t in trades
        if t.get("status") == "closed"
        and t.get("exit_timestamp", "").startswith(today)
        and t.get("pnl", 0) <= 0
    )
    open_trade_count = sum(1 for t in trades if t.get("status") == "open")

    snapshot = {
        "date": today,
        "portfolio_value": round(portfolio_value, 2),
        "cash": round(cash, 2),
        "positions_count": positions_count,
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(daily_pnl_pct, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "open_trades": open_trade_count,
        "closed_today": closed_today,
        "wins_today": wins_today,
        "losses_today": losses_today,
    }
    snapshots.append(snapshot)
    journal["daily_snapshots"] = apply_snapshot_retention(
        snapshots, max_count=max_snapshots)
    return snapshot


__all__ = [
    "_dec", "_to_cents_float",
    "normalize_strategy_name",
    "count_trade_statuses", "split_wins_losses",
    "win_rate_pct", "avg_pnl_pct",
    "profit_factor", "largest_win_loss", "avg_holding_days",
    "max_drawdown", "daily_returns_from_snapshots", "sharpe_sortino",
    "total_return_pct",
    "build_strategy_breakdown", "build_ab_testing",
    "build_correlation_warning", "compute_readiness",
    "apply_snapshot_retention",
    "calculate_metrics", "take_daily_snapshot",
    "STRATEGY_BUCKETS", "DEFAULT_MAX_SNAPSHOTS", "DEFAULT_RF_DAILY",
]
