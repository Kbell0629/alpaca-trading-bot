"""Round-61 pt.37 — backtest simulation engine (pure).

Mirrors the pt.7/34/36 pattern: pure functions live here, the I/O
boundary (fetching OHLCV bars, persisting results) lives in
``backtest_data.py``. This module is fully unit-testable without
network or filesystem.

Scope (deliberately narrow for pt.37):
  * Simulate the bot's STRATEGY ENTRY rules against historical
    daily bars for a given symbol universe.
  * Track each hypothetical position through stop / target / max-
    hold-days exits.
  * Report per-strategy aggregates: trades, win rate, total P&L.

Strategies supported (simplified vs production — production has news
sentiment + earnings filters + sector + factor gates that aren't
backtestable from bars alone):
  * **breakout** — close > 20-day high AND volume > 1.5x avg-20.
  * **mean_reversion** — close < 20-day low AND RSI(14) < 30.
  * **trailing_stop** — not a standalone entry; the exit policy is
    applied universally.
  * **short_sell** — close < 20-day low AND RSI(14) > 70 (rare
    config). Leveraged/inverse ETFs blocked per pt.35.

Inputs:
  * ``bars``: dict {symbol: [bar, bar, ...]} where each bar is
    {"date", "open", "high", "low", "close", "volume"}.
  * ``strategy``: one of ``backtest_core.BACKTESTABLE_STRATEGIES``.
  * ``params``: optional override for stop_pct / target_pct /
    max_hold_days. Defaults match production.

Outputs (per-strategy backtest result):
  * trades      — list of hypothetical {symbol, entry_date,
                  entry_price, exit_date, exit_price, exit_reason,
                  pnl, pnl_pct, hold_days}
  * count, wins, losses, win_rate, total_pnl, avg_pnl, avg_hold_days
  * best_pnl, worst_pnl

Design notes:
  * Daily bars only (no intraday) — enough for 30-day windows.
  * Simulator runs strictly forward through bars; no look-ahead.
  * Each strategy gets ONE hypothetical position per symbol per
    backtest window (avoid re-entering same name on consecutive
    bars).
  * Exits checked at next bar's high/low for stop-trigger detection.
"""
from __future__ import annotations


BACKTESTABLE_STRATEGIES = frozenset({
    "breakout", "mean_reversion", "short_sell",
})


# Default rules (mirror production scheduler)
DEFAULT_PARAMS = {
    "breakout": {
        "stop_pct": 0.10,
        "target_pct": 0.30,
        "max_hold_days": 30,
        "lookback_high": 20,
        "vol_lookback": 20,
        "vol_mult": 1.5,
        "side": "long",
    },
    "mean_reversion": {
        "stop_pct": 0.08,
        "target_pct": 0.10,
        "max_hold_days": 10,
        "lookback_low": 20,
        "rsi_period": 14,
        "rsi_threshold": 30,
        "side": "long",
    },
    "short_sell": {
        "stop_pct": 0.08,
        "target_pct": 0.15,
        "max_hold_days": 14,
        "lookback_low": 20,
        "rsi_period": 14,
        "rsi_threshold": 70,
        "side": "short",
    },
}


# ============================================================================
# Indicator helpers (pure, no numpy/pandas — stdlib only)
# ============================================================================

def _highest_close(bars, end_idx, n):
    """Highest close in the n bars BEFORE end_idx (exclusive). Used
    for breakout's 20-day-high comparison: today's close vs the
    highest close in the prior 20 days."""
    start = max(0, end_idx - n)
    if start >= end_idx:
        return None
    return max(b["close"] for b in bars[start:end_idx])


def _lowest_close(bars, end_idx, n):
    start = max(0, end_idx - n)
    if start >= end_idx:
        return None
    return min(b["close"] for b in bars[start:end_idx])


def _avg_volume(bars, end_idx, n):
    start = max(0, end_idx - n)
    if start >= end_idx:
        return None
    vols = [b.get("volume", 0) or 0 for b in bars[start:end_idx]]
    if not vols:
        return None
    return sum(vols) / len(vols)


def _rsi(bars, end_idx, period=14):
    """Wilder's RSI on closes for the period ending at end_idx
    (exclusive). Returns None if insufficient bars. Pure stdlib —
    no numpy."""
    if end_idx < period + 1:
        return None
    closes = [b["close"] for b in bars[end_idx - period - 1:end_idx]]
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-delta)
    if not gains:
        return None
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ============================================================================
# Entry rules (signal-only, no sizing — backtest assumes 1 share)
# ============================================================================

def _breakout_signal(bars, idx, params):
    if idx < params["lookback_high"]:
        return False
    today = bars[idx]
    prior_high = _highest_close(bars, idx, params["lookback_high"])
    if prior_high is None or today["close"] <= prior_high:
        return False
    avg_vol = _avg_volume(bars, idx, params["vol_lookback"])
    if avg_vol is None or avg_vol == 0:
        return False
    vol_today = today.get("volume", 0) or 0
    if vol_today < params["vol_mult"] * avg_vol:
        return False
    return True


def _mean_reversion_signal(bars, idx, params):
    if idx < max(params["lookback_low"], params["rsi_period"] + 1):
        return False
    today = bars[idx]
    prior_low = _lowest_close(bars, idx, params["lookback_low"])
    if prior_low is None or today["close"] > prior_low:
        return False
    rsi = _rsi(bars, idx, params["rsi_period"])
    if rsi is None or rsi >= params["rsi_threshold"]:
        return False
    return True


def _short_sell_signal(bars, idx, params):
    if idx < max(params["lookback_low"], params["rsi_period"] + 1):
        return False
    today = bars[idx]
    prior_low = _lowest_close(bars, idx, params["lookback_low"])
    if prior_low is None or today["close"] > prior_low:
        return False
    rsi = _rsi(bars, idx, params["rsi_period"])
    if rsi is None or rsi <= params["rsi_threshold"]:
        return False
    return True


_SIGNAL_FNS = {
    "breakout": _breakout_signal,
    "mean_reversion": _mean_reversion_signal,
    "short_sell": _short_sell_signal,
}


# ============================================================================
# Per-symbol simulator
# ============================================================================

def _simulate_symbol(symbol, bars, strategy, params):
    """Walk the bars forward, opening at most ONE position per
    consecutive signal (debounced by an active-position flag), and
    track exits. Returns a list of hypothetical-trade dicts."""
    side = params.get("side", "long")
    stop_pct = params["stop_pct"]
    target_pct = params["target_pct"]
    max_hold = params["max_hold_days"]
    signal_fn = _SIGNAL_FNS[strategy]

    trades = []
    position = None  # {entry_idx, entry_price, stop, target}

    for idx in range(len(bars)):
        bar = bars[idx]

        # Exit check FIRST (active position takes priority over new entries)
        if position is not None:
            exit_reason = None
            exit_price = None
            if side == "long":
                # Stop: today's low crosses stop → exit at stop price
                if bar["low"] <= position["stop"]:
                    exit_reason = "stop_triggered"
                    exit_price = position["stop"]
                # Target: today's high reaches target → exit at target
                elif bar["high"] >= position["target"]:
                    exit_reason = "target_hit"
                    exit_price = position["target"]
            else:  # short
                if bar["high"] >= position["stop"]:
                    exit_reason = "short_stop_covered"
                    exit_price = position["stop"]
                elif bar["low"] <= position["target"]:
                    exit_reason = "short_target_hit"
                    exit_price = position["target"]
            # Max hold
            if (exit_reason is None
                    and (idx - position["entry_idx"]) >= max_hold):
                exit_reason = "max_hold_exceeded"
                exit_price = bar["close"]

            if exit_reason is not None:
                pnl = (exit_price - position["entry_price"]
                        if side == "long"
                        else position["entry_price"] - exit_price)
                pnl_pct = (
                    (pnl / position["entry_price"]) * 100
                    if position["entry_price"] else 0.0
                )
                trades.append({
                    "symbol": symbol,
                    "strategy": strategy,
                    "side": side,
                    "entry_date": bars[position["entry_idx"]]["date"],
                    "entry_price": position["entry_price"],
                    "exit_date": bar["date"],
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 2),
                    "hold_days": idx - position["entry_idx"],
                })
                position = None

        # Entry check (only when flat)
        if position is None and signal_fn(bars, idx, params):
            entry_price = bar["close"]
            if side == "long":
                stop = entry_price * (1 - stop_pct)
                target = entry_price * (1 + target_pct)
            else:
                stop = entry_price * (1 + stop_pct)
                target = entry_price * (1 - target_pct)
            position = {
                "entry_idx": idx,
                "entry_price": entry_price,
                "stop": stop,
                "target": target,
            }

    # Open position at end of window — close at last bar
    if position is not None:
        last_bar = bars[-1]
        exit_price = last_bar["close"]
        pnl = (exit_price - position["entry_price"]
                if side == "long"
                else position["entry_price"] - exit_price)
        pnl_pct = (
            (pnl / position["entry_price"]) * 100
            if position["entry_price"] else 0.0
        )
        trades.append({
            "symbol": symbol,
            "strategy": strategy,
            "side": side,
            "entry_date": bars[position["entry_idx"]]["date"],
            "entry_price": position["entry_price"],
            "exit_date": last_bar["date"],
            "exit_price": exit_price,
            "exit_reason": "window_end",
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "hold_days": (len(bars) - 1) - position["entry_idx"],
        })

    return trades


# ============================================================================
# Aggregation
# ============================================================================

def _summarize(trades):
    """Aggregate stats matching the trades-dashboard schema (so the
    backtest output can be rendered with the same panels)."""
    if not trades:
        return {
            "count": 0, "wins": 0, "losses": 0, "flat": 0,
            "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
            "best_pnl": None, "worst_pnl": None,
            "avg_hold_days": None, "expectancy": 0.0,
        }
    wins = sum(1 for t in trades if t["pnl"] > 0.005)
    losses = sum(1 for t in trades if t["pnl"] < -0.005)
    flat = len(trades) - wins - losses
    total = sum(t["pnl"] for t in trades)
    win_rate = wins / len(trades)
    loss_rate = losses / len(trades)
    win_pnls = [t["pnl"] for t in trades if t["pnl"] > 0.005]
    loss_pnls = [t["pnl"] for t in trades if t["pnl"] < -0.005]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    return {
        "count": len(trades),
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "total_pnl": round(total, 2),
        "avg_pnl": round(total / len(trades), 2),
        "avg_win_pnl": round(avg_win, 2),
        "avg_loss_pnl": round(avg_loss, 2),
        "best_pnl": round(max(t["pnl"] for t in trades), 2),
        "worst_pnl": round(min(t["pnl"] for t in trades), 2),
        "avg_hold_days": round(
            sum(t["hold_days"] for t in trades) / len(trades), 2),
        "expectancy": round(
            win_rate * avg_win + loss_rate * avg_loss, 2),
    }


def run_backtest(bars_by_symbol, strategy, params=None):
    """Run the strategy backtest across every symbol's bar series.

    ``bars_by_symbol``: {SYMBOL: [bar, bar, ...]} where each bar is
        {"date": "YYYY-MM-DD", "open": float, "high": float,
         "low": float, "close": float, "volume": float}.
        Bars MUST be in chronological order.
    ``strategy``: one of ``BACKTESTABLE_STRATEGIES``.
    ``params``: optional dict to override defaults from
        ``DEFAULT_PARAMS[strategy]``.

    Returns ``{"strategy": ..., "params": ..., "trades": [...],
    "summary": {...}}``. The strategy + params are echoed back so
    the caller can render the input alongside the output.
    """
    if strategy not in BACKTESTABLE_STRATEGIES:
        return {
            "error": f"Unsupported strategy '{strategy}'. "
                      f"Supported: {sorted(BACKTESTABLE_STRATEGIES)}",
        }
    merged_params = dict(DEFAULT_PARAMS[strategy])
    if params:
        merged_params.update(params)

    all_trades = []
    for symbol, bars in (bars_by_symbol or {}).items():
        if not bars or len(bars) < 5:
            continue
        # Pt.35 invariant: leveraged/inverse ETFs are unconditionally
        # blocked from short-sell sims. Long sims may legitimately
        # include them (some users hold leveraged longs even though
        # the bot doesn't auto-deploy them).
        if (merged_params.get("side") == "short"
                and _is_blocked_short_symbol(symbol)):
            continue
        all_trades.extend(_simulate_symbol(
            symbol, bars, strategy, merged_params))

    return {
        "strategy": strategy,
        "params": merged_params,
        "trades": all_trades,
        "summary": _summarize(all_trades),
    }


def run_multi_strategy_backtest(bars_by_symbol, strategies=None,
                                  params_by_strategy=None):
    """Run every supported strategy back-to-back over the same bar
    universe and return a per-strategy result dict + cross-strategy
    overall summary. Useful for the "compare all strategies on
    last 30 days" dashboard view.
    """
    strategies = strategies or sorted(BACKTESTABLE_STRATEGIES)
    params_by_strategy = params_by_strategy or {}
    results = {}
    for s in strategies:
        if s not in BACKTESTABLE_STRATEGIES:
            results[s] = {"error": f"unsupported: {s}"}
            continue
        results[s] = run_backtest(
            bars_by_symbol, s, params_by_strategy.get(s))
    # Cross-strategy overall (sums all strategies' trades together)
    pooled = []
    for r in results.values():
        if isinstance(r, dict) and "trades" in r:
            pooled.extend(r["trades"])
    return {
        "by_strategy": results,
        "overall_summary": _summarize(pooled),
        "strategies_run": list(results.keys()),
        "symbols_evaluated": list(bars_by_symbol.keys()
                                    if bars_by_symbol else []),
    }


# ============================================================================
# Pt.35 invariant: don't short leveraged/inverse ETFs
# ============================================================================

def _is_blocked_short_symbol(symbol):
    """Defer to the canonical pt.35 list. Lazy import so this module
    stays importable even if `constants` evolves."""
    try:
        from constants import is_leveraged_or_inverse_etf
        return is_leveraged_or_inverse_etf(symbol)
    except Exception:
        return False
