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

def _apply_slippage(price, side, is_entry, slippage_bps):
    """Round-61 pt.47: adjust fill price by slippage_bps (1 bps =
    0.0001 = 0.01%). Slippage always works against you:
      * long entry:  pay UP — entry_price * (1 + slip)
      * long exit:   receive LESS — exit_price * (1 - slip)
      * short entry: receive LESS — entry_price * (1 - slip)
      * short exit:  pay UP to cover — exit_price * (1 + slip)
    Returns the adjusted price.
    """
    if not slippage_bps:
        return price
    slip = float(slippage_bps) / 10000.0
    if side == "long":
        return price * (1 + slip) if is_entry else price * (1 - slip)
    else:  # short
        return price * (1 - slip) if is_entry else price * (1 + slip)


def _simulate_symbol(symbol, bars, strategy, params):
    """Walk the bars forward, opening at most ONE position per
    consecutive signal (debounced by an active-position flag), and
    track exits. Returns a list of hypothetical-trade dicts.

    Round-61 pt.47: optional ``slippage_bps`` (basis points applied
    to entry + exit prices, working against you on both sides) and
    ``commission_per_trade`` (dollar amount subtracted from pnl per
    round-trip). Both default to 0 for backwards compatibility.
    Production should pass realistic values (e.g. slippage_bps=10,
    commission_per_trade=1.0) so backtest expectancy doesn't
    over-promise.
    """
    side = params.get("side", "long")
    stop_pct = params["stop_pct"]
    target_pct = params["target_pct"]
    max_hold = params["max_hold_days"]
    slippage_bps = float(params.get("slippage_bps", 0) or 0)
    commission = float(params.get("commission_per_trade", 0) or 0)
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
                # Apply slippage to the realised fill price.
                fill_exit = _apply_slippage(
                    exit_price, side, is_entry=False,
                    slippage_bps=slippage_bps)
                pnl = (fill_exit - position["entry_price"]
                        if side == "long"
                        else position["entry_price"] - fill_exit)
                # Subtract commission (charged once per round-trip).
                pnl -= commission
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
                    "exit_price": fill_exit,
                    "exit_reason": exit_reason,
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 2),
                    "hold_days": idx - position["entry_idx"],
                    "slippage_bps": slippage_bps,
                    "commission": commission,
                })
                position = None

        # Entry check (only when flat)
        if position is None and signal_fn(bars, idx, params):
            raw_entry = bar["close"]
            entry_price = _apply_slippage(
                raw_entry, side, is_entry=True, slippage_bps=slippage_bps)
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
        fill_exit = _apply_slippage(
            last_bar["close"], side, is_entry=False,
            slippage_bps=slippage_bps)
        pnl = (fill_exit - position["entry_price"]
                if side == "long"
                else position["entry_price"] - fill_exit)
        pnl -= commission
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
            "exit_price": fill_exit,
            "exit_reason": "window_end",
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "hold_days": (len(bars) - 1) - position["entry_idx"],
            "slippage_bps": slippage_bps,
            "commission": commission,
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
# Round-61 pt.47: Walk-forward validation harness
# ============================================================================

def _slice_bars(bars, start_idx, end_idx):
    """Return bars[start_idx:end_idx] for one symbol; safe on bounds."""
    if not bars:
        return []
    if start_idx < 0:
        start_idx = 0
    if end_idx > len(bars):
        end_idx = len(bars)
    if start_idx >= end_idx:
        return []
    return bars[start_idx:end_idx]


def _slice_universe(bars_by_symbol, start_idx, end_idx):
    """Return a {symbol: sliced_bars} dict aligned by index across
    every symbol. Symbols with empty slices drop out."""
    out = {}
    for sym, bars in (bars_by_symbol or {}).items():
        sliced = _slice_bars(bars, start_idx, end_idx)
        if sliced:
            out[sym] = sliced
    return out


def _max_universe_len(bars_by_symbol):
    """Length of the longest bar series — defines the walk-forward
    timeline. Walk-forward assumes bars are aligned by date across
    the universe (all symbols share a common timeline)."""
    if not bars_by_symbol:
        return 0
    return max(len(b) for b in bars_by_symbol.values() if b)


def run_walk_forward_backtest(bars_by_symbol, strategy, *,
                                train_days: int = 30,
                                test_days: int = 30,
                                step_days: int = 7,
                                param_grid=None,
                                base_params=None,
                                metric: str = "expectancy"):
    """Walk-forward validation of a strategy. Slides a (train_days,
    test_days) window forward by step_days through the bar timeline.
    For each fold, picks the best param variant on the train window
    and evaluates that exact variant on the immediately-following
    test window. Reports both per-fold + aggregate test-window
    metrics — a healthy strategy should show test ~= train; if test
    is much worse, the param tuning is overfitting.

    Args:
      bars_by_symbol: same shape as run_backtest.
      strategy: one of BACKTESTABLE_STRATEGIES.
      train_days: bars in the train window (used for param selection).
      test_days: bars in the test window (out-of-sample eval).
      step_days: how far forward to slide each fold.
      param_grid: list of param-override dicts to compete on the
        train window. If None, defaults to a small ±20% sweep around
        DEFAULT_PARAMS[strategy] for stop_pct + target_pct.
      base_params: dict merged INTO DEFAULT_PARAMS before each
        variant override (e.g. inject slippage_bps + commission).
      metric: which summary key to maximize on the train window.
        Default "expectancy"; "total_pnl" or "win_rate" also work.

    Returns:
      {
        "strategy": str,
        "folds": [
          {
            "fold_idx": int,
            "train_window": [start_date, end_date],
            "test_window":  [start_date, end_date],
            "best_params":  {...},
            "train_summary": {...},
            "test_summary":  {...},
          },
          ...
        ],
        "aggregate_test_summary": {...},   # pooled over all folds
        "aggregate_train_summary": {...},  # pooled over all folds
        "overfit_ratio": float,            # train_expectancy / test_expectancy
                                              # (>1.5 ⇒ overfitting risk)
        "fold_count": int,
      }
    """
    if strategy not in BACKTESTABLE_STRATEGIES:
        return {"error": f"unsupported strategy: {strategy}"}
    if train_days < 5 or test_days < 5 or step_days < 1:
        return {"error": "train_days/test_days must be >=5, step_days >=1"}
    timeline = _max_universe_len(bars_by_symbol)
    if timeline < (train_days + test_days):
        return {"error": (
            f"need >= {train_days + test_days} bars; have {timeline}")}

    if not param_grid:
        param_grid = _default_param_grid(strategy)
    if base_params:
        param_grid = [{**base_params, **p} for p in param_grid]

    folds = []
    fold_idx = 0
    start = 0
    while start + train_days + test_days <= timeline:
        train_end = start + train_days
        test_end = train_end + test_days
        train_universe = _slice_universe(bars_by_symbol, start, train_end)
        test_universe = _slice_universe(bars_by_symbol, train_end, test_end)
        if not train_universe or not test_universe:
            start += step_days
            continue

        # Score every param variant on train.
        best = None
        best_metric_val = None
        best_train_summary = None
        for variant in param_grid:
            r = run_backtest(train_universe, strategy, variant)
            sm = r.get("summary") or {}
            mval = sm.get(metric, 0)
            if mval is None:
                mval = 0
            if best is None or mval > best_metric_val:
                best = variant
                best_metric_val = mval
                best_train_summary = sm

        # Test the winning variant on the immediately-following window.
        test_r = run_backtest(test_universe, strategy, best)

        train_dates = _window_dates(train_universe)
        test_dates = _window_dates(test_universe)
        folds.append({
            "fold_idx": fold_idx,
            "train_window": train_dates,
            "test_window": test_dates,
            "best_params": best,
            "train_summary": best_train_summary,
            "test_summary": test_r.get("summary"),
            "test_trades": test_r.get("trades", []),
        })
        fold_idx += 1
        start += step_days

    # Aggregate test trades across all folds → out-of-sample summary.
    pooled_test = []
    pooled_train_pnls = []
    for f in folds:
        pooled_test.extend(f.get("test_trades") or [])
        ts = f.get("train_summary") or {}
        if ts.get("count"):
            pooled_train_pnls.append(ts.get("expectancy") or 0)
    aggregate_test_summary = _summarize(pooled_test)

    # Train aggregate (best-variant-per-fold expectancy mean) for
    # the overfit ratio.
    train_avg_exp = (sum(pooled_train_pnls) / len(pooled_train_pnls)
                      if pooled_train_pnls else 0.0)
    test_avg_exp = aggregate_test_summary.get("expectancy") or 0.0
    overfit_ratio = None
    if test_avg_exp != 0:
        overfit_ratio = round(train_avg_exp / test_avg_exp, 3)

    return {
        "strategy": strategy,
        "folds": folds,
        "fold_count": len(folds),
        "aggregate_test_summary": aggregate_test_summary,
        "aggregate_train_expectancy": round(train_avg_exp, 4),
        "aggregate_test_expectancy": round(test_avg_exp, 4),
        "overfit_ratio": overfit_ratio,
    }


def _window_dates(universe):
    """Earliest-start, latest-end date across an aligned universe."""
    starts = []
    ends = []
    for bars in universe.values():
        if bars:
            starts.append(bars[0].get("date"))
            ends.append(bars[-1].get("date"))
    if not starts:
        return [None, None]
    return [min(starts), max(ends)]


def _default_param_grid(strategy):
    """Small ±20% sweep around DEFAULT_PARAMS[strategy] for the
    walk-forward harness when no caller-supplied grid is given."""
    base = dict(DEFAULT_PARAMS[strategy])
    grid = [dict(base)]  # baseline
    for stop_mult in (0.8, 1.0, 1.2):
        for tgt_mult in (0.8, 1.0, 1.2):
            if stop_mult == 1.0 and tgt_mult == 1.0:
                continue  # already in baseline
            v = dict(base)
            v["stop_pct"] = round(base["stop_pct"] * stop_mult, 4)
            v["target_pct"] = round(base["target_pct"] * tgt_mult, 4)
            grid.append(v)
    return grid


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
