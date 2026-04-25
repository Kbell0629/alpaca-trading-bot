"""
Round-61 pt.7 — pure scoring math extracted from update_dashboard.py.

update_dashboard.py is the 30-min screener. It mixes HTTP I/O (Alpaca,
yfinance, SEC) with pure scoring math (per-strategy scoring, filters,
position sizing). Because the whole file was in `pyproject.toml`'s
coverage omit list (it's run as a subprocess, not import-tested), the
pure math was invisible to `pytest-cov`.

This module extracts the pure functions. `update_dashboard.py` now
imports from here, keeping behavior identical. `screener_core.py`
is NOT in the omit list — its functions are testable in isolation.

Extracted:
  * pick_best_entry_strategy(scores, entry_strategies) — argmax
  * trading_day_fraction_elapsed(now) — trading-day time math
  * score_stocks(snapshots, *, entry_strategies, sector_map,
                  min_price, min_volume,
                  copy_trading_enabled, pead_enabled,
                  day_fraction=None) — the heart of the screener
  * apply_market_regime(picks, regime) — filter by bull/bear tilt
  * apply_sector_diversification(picks, max_per_sector, top_n)
  * calc_position_size(price, volatility, portfolio_value, max_risk_pct)
  * compute_portfolio_pnl(positions, portfolio_value)

All functions are PURE — no network, no disk, no globals. Every
external dependency is a parameter. Tests in
tests/test_round61_pt7_screener_core.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping, Optional, Sequence


# ============================================================================
# Defaults mirroring update_dashboard.py constants. Exposed so callers
# can pass custom values AND so the defaults document the screener's
# production settings.
# ============================================================================

DEFAULT_MIN_PRICE = 5.0
DEFAULT_MIN_VOLUME = 300_000
DEFAULT_ENTRY_STRATEGIES = ("Breakout", "Mean Reversion", "Wheel Strategy", "PEAD")


# ============================================================================
# Helpers
# ============================================================================

def pick_best_entry_strategy(scores: Mapping[str, float],
                              entry_strategies: Sequence[str] = DEFAULT_ENTRY_STRATEGIES
                              ) -> str:
    """Return the strategy name with the highest score, restricted to
    entry strategies. Trailing Stop is silently ignored (it's an exit
    policy, not an entry — see architecture note in update_dashboard.py).

    Empty `scores` → returns the first entry strategy (safe default).
    """
    if not scores:
        return entry_strategies[0] if entry_strategies else "Breakout"
    return max(entry_strategies,
               key=lambda s: float(scores.get(s, 0) or 0))


def trading_day_fraction_elapsed(now: Optional[datetime] = None) -> float:
    """What fraction of the US cash session has passed at `now`?

    Returns a value in (0, 1]:
      * 0.05 at 9:50 AM ET (first 20 min / 390 = ~5%)
      * 0.5  at 12:45 PM ET
      * 1.0  at/after 4 PM ET and on weekends / pre-market

    Used to rescale volume_surge so comparing 20 minutes of today's
    volume against a full day of yesterday's doesn't report -95% for
    every stock in the first hour of trading. Round-10 audit fix.

    Accepts `now` as a parameter so tests don't depend on wall-clock
    time. Callers pass `et_time.get_et_time()` in production.
    """
    if now is None:
        now = datetime.now()
    # Weekend → full day (no scaling)
    if now.weekday() >= 5:
        return 1.0
    open_mins = 9 * 60 + 30    # 9:30 AM
    close_mins = 16 * 60        # 4:00 PM
    now_mins = now.hour * 60 + now.minute
    # Pre-/post-market → full day (no scaling)
    if now_mins < open_mins or now_mins >= close_mins:
        return 1.0
    total = close_mins - open_mins  # 390 minutes
    elapsed = now_mins - open_mins
    return max(0.01, min(1.0, elapsed / total))


# ============================================================================
# Main scoring pipeline
# ============================================================================

def score_stocks(snapshots: Mapping[str, dict], *,
                  entry_strategies: Sequence[str] = DEFAULT_ENTRY_STRATEGIES,
                  sector_map: Optional[Mapping[str, str]] = None,
                  min_price: float = DEFAULT_MIN_PRICE,
                  min_volume: int = DEFAULT_MIN_VOLUME,
                  copy_trading_enabled: bool = False,
                  pead_enabled: bool = True,
                  day_fraction: Optional[float] = None,
                  pead_score_fn=None,
                  copy_score_fn=None,
                  ) -> list:
    """Score each stock across all entry strategies using Alpaca
    snapshot data. This is the initial fast-pass scorer that runs over
    ~12k tickers every 30 minutes.

    Args:
      snapshots: {symbol -> {"dailyBar": ..., "prevDailyBar": ...,
                              "latestTrade": ...}} from Alpaca's
                  /v2/stocks/snapshots endpoint.
      entry_strategies: which strategies compete for best_strategy.
                          Trailing Stop is excluded (exit policy).
      sector_map: optional {symbol -> sector_name}. Symbols without a
                    mapping get "Other". If None, all picks get "Other".
      min_price: reject symbols below this price (penny stocks).
      min_volume: reject symbols whose PREV-DAY volume is below this
                    (illiquid). Prev-day, not intraday — Round-10 fix
                    to avoid comparing partial intraday volume against
                    a fixed cutoff.
      copy_trading_enabled: if True, score Copy Trading via copy_score_fn.
      pead_enabled: if True, score PEAD via pead_score_fn.
      day_fraction: override the trading-day fraction. If None, called
                      via trading_day_fraction_elapsed() at score time.
                      Tests pass a fixed value for determinism.
      pead_score_fn: callable(symbol) -> (score, signal). Injected so
                       tests don't need pead_strategy.py wired up.
      copy_score_fn: callable(symbol) -> (score, signal_list).

    Returns a list of pick dicts, sorted by best_score descending.
    """
    results = []
    sector_map = sector_map or {}

    frac = day_fraction if day_fraction is not None else trading_day_fraction_elapsed()

    for symbol, snap in snapshots.items():
        try:
            daily_bar = snap.get("dailyBar") or {}
            prev_bar = snap.get("prevDailyBar") or {}
            latest_trade = snap.get("latestTrade") or {}

            price = latest_trade.get("p", 0)
            daily_close = daily_bar.get("c", 0)
            prev_close = prev_bar.get("c", 0)
            daily_high = daily_bar.get("h", 0)
            daily_low = daily_bar.get("l", 0)
            daily_volume = daily_bar.get("v", 0)
            prev_volume = prev_bar.get("v", 0)

            # Skip if missing critical data
            if not price or not prev_close or not daily_low:
                continue

            # Filter: penny stocks and illiquid
            if price < min_price:
                continue
            liquidity_volume = prev_volume or daily_volume
            if liquidity_volume < min_volume:
                continue

            # Calculate metrics — intraday volume_surge rescaled via
            # day_fraction so partial-day comparison vs full-prev-day is
            # apples-to-apples.
            daily_change = (daily_close / prev_close - 1) * 100 if prev_close else 0
            volatility = (daily_high - daily_low) / daily_low * 100 if daily_low else 0
            adjusted_prev_volume = prev_volume * max(frac, 0.05) if prev_volume else 0
            volume_surge = (daily_volume / adjusted_prev_volume - 1) * 100 if adjusted_prev_volume else 0

            # Data-quality filter: reject obvious stale/split-adjusted/bad-data snapshots.
            if abs(daily_change) > 100 or volatility > 100:
                continue

            # --- Strategy Scores ---
            trailing_score = daily_change * 0.5 + volatility * 0.3
            if volume_surge > 50:
                trailing_score += 5

            copy_score = 0
            copy_signals = []
            if copy_trading_enabled and copy_score_fn:
                try:
                    copy_score, copy_signals = copy_score_fn(symbol)
                except Exception:
                    pass

            pead_score = 0
            pead_signal = None
            if pead_enabled and pead_score_fn:
                try:
                    pead_score, pead_signal = pead_score_fn(symbol)
                except Exception:
                    pass

            # Wheel Strategy — bell curve around moderate volatility
            if volatility <= 5:
                wheel_score = volatility * 3
            elif volatility <= 10:
                wheel_score = 15 + (10 - volatility)
            else:
                wheel_score = max(0, 15 - (volatility - 10))
            if 20 <= price <= 500:
                wheel_score += 5
            if -5 <= daily_change <= 5:
                wheel_score += 5

            # Mean Reversion — oversold + high volume = bounce candidate
            mean_reversion_score = 0
            if daily_change < -5:
                mean_reversion_score = abs(daily_change) * 1.5 + volatility * 0.3
                if volume_surge > 200:
                    mean_reversion_score *= 0.5  # news-driven selloff, less bouncy
                elif volume_surge > 50:
                    mean_reversion_score += 5
            elif daily_change < -2:
                mean_reversion_score = abs(daily_change) * 0.8

            # Breakout — up on high volume; tiered by relative-volume strength
            breakout_score = 0
            breakout_note = None
            if daily_change > 3 and volume_surge > 50:
                breakout_score = daily_change * 1.5 + (volume_surge / 20)
                if volume_surge > 200:
                    breakout_score *= 1.5
                    breakout_note = "3x_volume_confirmed"
                elif volume_surge > 100:
                    breakout_score *= 1.2
                    breakout_note = "2x_volume_confirmed"
                else:
                    breakout_note = "standard_breakout"
            elif daily_change > 2 and volume_surge > 30:
                breakout_score = daily_change * 0.8
                breakout_note = "weak_breakout"

            # Volatility soft-cap — halve breakout score on big-range
            # names (news-driven / pump) so they can still rank but
            # don't dominate the top.
            if volatility > 25 and breakout_score > 0:
                breakout_score *= 0.5
                breakout_note = (breakout_note or "standard_breakout") + "_highvol_capped"

            # Best strategy — entry strategies only; Trailing Stop
            # deliberately excluded from the argmax.
            entry_scores = {
                "Copy Trading": copy_score,
                "Wheel Strategy": wheel_score,
                "Mean Reversion": mean_reversion_score,
                "Breakout": breakout_score,
                "PEAD": pead_score,
            }
            scores = {"Trailing Stop": 0, **entry_scores}
            best_strategy = pick_best_entry_strategy(scores, entry_strategies)
            best_score = entry_scores.get(best_strategy, 0)

            sector = sector_map.get(symbol, "Other")

            results.append({
                "symbol": symbol,
                "price": price,
                "daily_change": daily_change,
                "volatility": volatility,
                "daily_volume": daily_volume,
                "volume_surge": volume_surge,
                "best_strategy": best_strategy,
                "best_score": best_score,
                "scores": scores,
                "trailing_score": trailing_score,
                "copy_score": copy_score,
                "copy_signals": copy_signals,
                "wheel_score": wheel_score,
                "mean_reversion_score": mean_reversion_score,
                "breakout_score": breakout_score,
                "breakout_note": breakout_note,
                "pead_score": pead_score,
                "pead_signal": pead_signal,
                "sector": sector,
            })
        except Exception:
            continue

    results.sort(key=lambda x: x["best_score"], reverse=True)
    return results


# ============================================================================
# Post-scoring filters
# ============================================================================

def apply_market_regime(picks: list, regime: Optional[dict]) -> list:
    """Annotate each pick with regime context and re-sort. Doesn't
    remove picks — the scoring already filtered; regime just tags
    which ones the current market backdrop favors.

    regime dict shape (from fetch_market_regime): {"bias": "bull"|"bear"|
    "neutral", "vix_estimate": float, ...}. None/empty regime leaves
    picks unchanged.
    """
    if not picks or not isinstance(regime, dict):
        return picks
    bias = str(regime.get("bias") or "").lower()
    for p in picks:
        p["_regime_bias"] = bias or None
    return picks


# ============================================================================
# Round-61 pt.39: trend filter
# ============================================================================

# Strategies that take a LONG position. These should only fire when the
# stock trades ABOVE its 50-day moving average (with the underlying
# trend, not against it).
_LONG_STRATEGIES: frozenset = frozenset({
    "breakout", "trailing_stop", "mean_reversion", "pead",
    "copy_trading",
})
# Strategies that take a SHORT position. Should only fire BELOW the
# 50-day moving average (selling weakness, not chasing a stock that's
# trending up against you).
_SHORT_STRATEGIES: frozenset = frozenset({"short_sell"})


def _sma_from_closes(closes, period: int):
    """Simple moving average of the last `period` closes. Returns None
    if there aren't enough bars."""
    if not closes or len(closes) < period:
        return None
    window = list(closes[-period:])
    try:
        return sum(float(c) for c in window) / period
    except (TypeError, ValueError):
        return None


def apply_trend_filter(picks: list, bars_map: Optional[Mapping[str, list]],
                        period: int = 50,
                        long_strategies: Optional[frozenset] = None,
                        short_strategies: Optional[frozenset] = None) -> list:
    """Round-61 pt.39: gate strategy selection by the longer-term trend.

    Most "fake breakouts" happen on stocks below their longer-term
    trend — they look exciting on the daily but are dead-cat-bouncing
    inside a downtrend. Same problem in reverse for shorts (chasing
    weakness in a stock that's actually in an uptrend).

    Behaviour per pick:
      * Compute SMA(``period``) from ``bars_map[symbol]`` closes. If
        we don't have enough bars, the pick passes through unchanged
        (fail OPEN — never block a deploy on missing data).
      * Tag the pick with ``sma_<period>`` and ``above_sma_<period>``
        booleans for the dashboard / audit.
      * If ``best_strategy`` is a long strategy AND price <= SMA:
        zero out the score, set ``_filtered_by_trend = "below_sma"``,
        clear ``best_strategy`` and ``will_deploy``. Pick stays in
        the list (with an explanatory tag) so the dashboard's
        "filtered out" panel can show it.
      * Mirror for shorts (price >= SMA → filter).
      * Picks with no `best_strategy` / unknown strategy pass through.

    Caller (update_dashboard) is responsible for sourcing the
    ``bars_map`` (typically the same factor_bars used for RS ranking).
    """
    long_strategies = long_strategies or _LONG_STRATEGIES
    short_strategies = short_strategies or _SHORT_STRATEGIES
    bars_map = bars_map or {}
    if not picks:
        return picks

    sma_key = f"sma_{period}"
    above_key = f"above_sma_{period}"

    for p in picks:
        symbol = p.get("symbol")
        bars = bars_map.get(symbol) or []
        # Extract closes from bars (Alpaca format: each bar has 'c').
        closes = [b.get("c") for b in bars if isinstance(b, dict)]
        sma = _sma_from_closes(closes, period)
        if sma is None:
            # Fail open — no trend data, no filter.
            continue
        p[sma_key] = round(sma, 4)
        try:
            price = float(p.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        if price <= 0:
            continue
        p[above_key] = bool(price > sma)

        strategy = p.get("best_strategy") or ""
        if strategy in long_strategies and price <= sma:
            p["_filtered_by_trend"] = "below_sma"
            p["best_score"] = 0
            p["will_deploy"] = False
            # Keep the strategy name for the dashboard's "why filtered"
            # tooltip but mark as filtered.
            p["_filtered_strategy"] = strategy
            p["best_strategy"] = None
        elif strategy in short_strategies and price >= sma:
            p["_filtered_by_trend"] = "above_sma"
            p["best_score"] = 0
            p["will_deploy"] = False
            p["_filtered_strategy"] = strategy
            p["best_strategy"] = None

    # Re-sort so filtered picks fall to the bottom.
    picks.sort(key=lambda p: float(p.get("best_score") or 0), reverse=True)
    return picks


# ============================================================================
# Round-61 pt.58: graduated pre-market gap penalty
# ============================================================================

GAP_PENALTY_THRESHOLD_PCT: float = 3.0
GAP_PENALTY_BLOCK_THRESHOLD_PCT: float = 8.0
GAP_PENALTY_MULTIPLIER: float = 0.85


def apply_gap_penalty(picks: list,
                        *,
                        threshold_pct: float = GAP_PENALTY_THRESHOLD_PCT,
                        block_threshold_pct: float = GAP_PENALTY_BLOCK_THRESHOLD_PCT,
                        multiplier: float = GAP_PENALTY_MULTIPLIER) -> list:
    """Round-61 pt.58: penalize picks with a 3-8% intraday gap.

    The pt.45 chase_block already hard-blocks picks with daily_change
    > 8%. The gap between 3-8% is a grey zone — sometimes a real
    breakout, often "the screener was too late and we'd be buying
    the local high". This applies a graduated penalty:

    * daily_change <  threshold_pct  → no change
    * threshold_pct <= daily_change < block_threshold_pct → multiply
        best_score by `multiplier` (default 0.85, ~15% demotion)
    * daily_change >= block_threshold_pct → leave alone (chase_block
        will hard-block at deploy time)

    Tags affected picks with `_gap_penalty_applied: True` so the
    dashboard can show the "deploy-time chip" pattern.

    Mutates each pick's `best_score`. Returns the (re-sorted) list.
    """
    if not picks:
        return []
    for p in picks:
        if not isinstance(p, dict):
            continue
        try:
            dc = float(p.get("daily_change") or 0)
        except (TypeError, ValueError):
            continue
        if threshold_pct <= dc < block_threshold_pct:
            try:
                bs = float(p.get("best_score") or 0)
            except (TypeError, ValueError):
                continue
            p["best_score"] = round(bs * multiplier, 2)
            p["_gap_penalty_applied"] = True
            p["_gap_penalty_pct"] = round(dc, 2)
    # Sort, defending against non-dict entries / non-numeric scores
    # the caller may have mixed in (we tolerate them silently above).
    def _key(p):
        if not isinstance(p, dict):
            return 0.0
        try:
            return float(p.get("best_score") or 0)
        except (TypeError, ValueError):
            return 0.0
    picks.sort(key=_key, reverse=True)
    return picks


def apply_sector_diversification(picks: list, max_per_sector: int = 2,
                                    top_n: int = 5) -> list:
    """Return the top `top_n` picks with at most `max_per_sector` per
    sector. Picks beyond the per-sector cap get their place taken by
    the next-highest-scoring pick from an under-represented sector.
    """
    if not picks:
        return []
    if max_per_sector < 1:
        max_per_sector = 1
    if top_n < 1:
        return []
    sector_counts: dict = {}
    chosen: list = []
    for p in picks:
        if len(chosen) >= top_n:
            break
        sec = p.get("sector") or "Other"
        if sector_counts.get(sec, 0) < max_per_sector:
            chosen.append(p)
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
    return chosen


# ============================================================================
# Position sizing
# ============================================================================

def calc_position_size(price: float, volatility: float,
                       portfolio_value: float,
                       max_risk_pct: float = 0.02) -> int:
    """Compute share count for a given entry given price, volatility
    (percent), and portfolio value. Caps risk at max_risk_pct of
    portfolio (default 2%).

    Formula: risk_dollars = portfolio * max_risk_pct;
             stop_pct = min(volatility, 10); stop_dollars = price * stop_pct/100;
             shares = risk_dollars / stop_dollars, floored + capped at 10%
             of portfolio by notional so a tiny stop doesn't blow the position
             size to the moon.
    Returns int >= 0. If inputs are invalid, returns 0.
    """
    try:
        price = float(price)
        volatility = float(volatility)
        portfolio_value = float(portfolio_value)
        max_risk_pct = float(max_risk_pct)
    except (TypeError, ValueError):
        return 0
    if price <= 0 or portfolio_value <= 0 or max_risk_pct <= 0:
        return 0
    risk_dollars = portfolio_value * max_risk_pct
    stop_pct = max(min(volatility, 10.0), 1.0) / 100.0
    stop_dollars = price * stop_pct
    if stop_dollars <= 0:
        return 0
    by_risk = int(risk_dollars // stop_dollars)
    # Don't let a tiny stop balloon the position beyond 10% of portfolio
    max_notional = portfolio_value * 0.10
    by_notional = int(max_notional // price)
    return max(0, min(by_risk, by_notional))


# ============================================================================
# Portfolio math
# ============================================================================

def compute_portfolio_pnl(positions: Iterable[dict],
                           portfolio_value: float) -> dict:
    """Summarize per-strategy P&L across a position list.

    positions — iterable of Alpaca-position-like dicts
    (`symbol`, `qty`, `avg_entry_price`, `unrealized_pl`, ...).
    portfolio_value — for computing percent-of-portfolio.

    Returns {"total_unrealized_pl": float, "exposure_pct": float,
              "position_count": int, "long_count": int, "short_count": int}.
    """
    total_pl = 0.0
    total_value = 0.0
    long_count = 0
    short_count = 0
    count = 0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        try:
            qty = float(p.get("qty") or 0)
            upl = float(p.get("unrealized_pl") or 0)
            mv = float(p.get("market_value") or 0)
        except (TypeError, ValueError):
            continue
        total_pl += upl
        total_value += abs(mv)
        count += 1
        if qty > 0:
            long_count += 1
        elif qty < 0:
            short_count += 1
    try:
        pv = float(portfolio_value)
    except (TypeError, ValueError):
        pv = 0.0
    exposure = (total_value / pv * 100) if pv > 0 else 0.0
    return {
        "total_unrealized_pl": round(total_pl, 2),
        "exposure_pct": round(exposure, 2),
        "position_count": count,
        "long_count": long_count,
        "short_count": short_count,
    }


# ============================================================================
# Round-61 pt.40: multi-day breakout confirmation
# ============================================================================

# How many bars to use for the breakout-level reference. 20 trading
# days = ~one calendar month, the standard "20-day high" definition
# used by Donchian Channel + Turtle traders. The bot's existing
# breakout heuristic in score_stocks relies on intraday volume + day
# change; pt.40 adds a STRUCTURAL confirmation on top.
_BREAKOUT_LOOKBACK_DAYS: int = 20


def _max_high_window(bars, end_idx_exclusive: int, window: int):
    """Return the max(high) of the `window` bars ending just before
    ``end_idx_exclusive``. Returns None if not enough bars.

    e.g. ``_max_high_window(bars, len(bars)-1, 20)`` returns the
    20-bar high BEFORE today's bar — the level today's close would
    have to clear to qualify as a true breakout.
    """
    if not bars or end_idx_exclusive <= 0 or window < 1:
        return None
    start = end_idx_exclusive - window
    if start < 0:
        return None
    highs = []
    for b in bars[start:end_idx_exclusive]:
        if not isinstance(b, dict):
            continue
        h = b.get("h")
        try:
            if h is not None:
                highs.append(float(h))
        except (TypeError, ValueError):
            continue
    return max(highs) if highs else None


def apply_breakout_confirmation(picks: list,
                                  bars_map: Optional[Mapping[str, list]],
                                  lookback: int = _BREAKOUT_LOOKBACK_DAYS,
                                  ) -> list:
    """Round-61 pt.40: require a SECOND day of close above the 20-day
    high before treating a breakout as real.

    The single-day breakout has the well-known "Tuesday-fake-breakout-
    Wednesday-collapses" failure mode: a stock pops above its 20-day
    high once, draws in late buyers, then fades. Academic momentum
    research (Asness et al., Moskowitz et al.) consistently shows
    multi-bar confirmation lifts the win rate ~10-15 points at the
    cost of ~20% fewer entries — net positive.

    Per pick where ``best_strategy == "Breakout"``:
      * Compute today_breakout_level = max(high) of bars[-(lookback+1):-1]
        (the lookback-day high BEFORE today).
      * Compute prior_breakout_level = max(high) of bars[-(lookback+2):-2]
        (the lookback-day high BEFORE yesterday).
      * REQUIRES: today's close > today_breakout_level AND
                  yesterday's close > prior_breakout_level.
      * If only today qualifies → tag ``_breakout_unconfirmed`` and
        demote score by 50% (still rankable but won't be deploy-top).
        Filtered picks stay in the list.
      * Picks with insufficient bars or non-breakout strategy: pass
        through unchanged (fail open).
    """
    bars_map = bars_map or {}
    if not picks:
        return picks

    for p in picks:
        # Only confirm Breakout. Other strategies don't share the
        # 20-day-high signal pattern.
        strat = p.get("best_strategy") or ""
        if strat.lower() != "breakout":
            continue

        symbol = p.get("symbol")
        bars = bars_map.get(symbol) or []
        # Need at least lookback+2 bars: today + yesterday + lookback prior.
        if len(bars) < lookback + 2:
            continue

        today_bar = bars[-1] if isinstance(bars[-1], dict) else None
        yesterday_bar = bars[-2] if isinstance(bars[-2], dict) else None
        if not today_bar or not yesterday_bar:
            continue

        try:
            today_close = float(today_bar.get("c", 0))
            yesterday_close = float(yesterday_bar.get("c", 0))
        except (TypeError, ValueError):
            continue
        if today_close <= 0 or yesterday_close <= 0:
            continue

        today_level = _max_high_window(bars, len(bars) - 1, lookback)
        prior_level = _max_high_window(bars, len(bars) - 2, lookback)
        if today_level is None or prior_level is None:
            continue

        p["breakout_level_today"] = round(today_level, 4)
        p["breakout_level_prior"] = round(prior_level, 4)
        p["breakout_today_above"] = bool(today_close > today_level)
        p["breakout_yesterday_above"] = bool(yesterday_close > prior_level)
        p["breakout_confirmed"] = bool(
            p["breakout_today_above"] and p["breakout_yesterday_above"])

        if not p["breakout_confirmed"]:
            # Single-day breakout — demote (not eliminate). Tags +
            # halved score so the dashboard can show why and the
            # auto-deployer's threshold gates this pick out unless
            # nothing else qualifies.
            p["_breakout_unconfirmed"] = True
            try:
                p["best_score"] = float(p.get("best_score") or 0) * 0.5
            except (TypeError, ValueError):
                p["best_score"] = 0
            try:
                if "breakout_score" in p:
                    p["breakout_score"] = float(p["breakout_score"]) * 0.5
            except (TypeError, ValueError):
                pass

    # Re-sort by best_score so demoted picks fall down.
    picks.sort(key=lambda p: float(p.get("best_score") or 0), reverse=True)
    return picks


# ============================================================================
# Round-61 pt.41: per-strategy adaptive thresholds (rolling win rate)
# ============================================================================

# Default lookback: most recent N closed trades per strategy. Smaller
# = more responsive but noisier; larger = slower to adapt but more
# reliable. 30 trades is the academic baseline for "stable enough to
# infer expectancy" without baking in stale market regime.
ADAPTIVE_LOOKBACK_DEFAULT: int = 30

# Below this trade count we don't have enough signal to adjust the
# threshold — return multiplier=1.0 (no-op).
ADAPTIVE_MIN_SAMPLE: int = 5

# Boundaries for the multiplier curve. Win rate below LOW means the
# strategy is "cold" — raise the threshold (multiplier < 1 demotes
# scores so picks need to be stronger to deploy). Above HIGH means
# "hot" — lower the threshold (multiplier > 1 boosts scores so the
# bot deploys more aggressively).
ADAPTIVE_COLD_WIN_RATE: float = 0.40
ADAPTIVE_HOT_WIN_RATE: float = 0.60
ADAPTIVE_MAX_DEMOTE: float = 0.70   # cold → score × 0.70
ADAPTIVE_MAX_BOOST: float = 1.30    # hot  → score × 1.30


def compute_strategy_win_rates(journal: Optional[dict],
                                 lookback: int = ADAPTIVE_LOOKBACK_DEFAULT,
                                 ) -> dict:
    """Return ``{strategy_name: {"wins": N, "losses": N, "count": N,
    "win_rate": float}}`` for the most recent ``lookback`` CLOSED
    trades per strategy.

    Pure: takes a parsed journal dict, returns a stats dict. Caller
    handles loading ``trade_journal.json``.

    Strategy names are taken verbatim from the journal entries (lower-
    case is canonical: "breakout", "mean_reversion", "wheel",
    "short_sell", "pead", "copy_trading", "trailing_stop"). Open
    trades are skipped — they have no W/L yet.
    """
    if not journal or not isinstance(journal, dict):
        return {}
    trades = journal.get("trades") or []
    by_strategy: dict = {}
    # Walk newest → oldest so we collect the most recent N per strategy.
    for t in reversed(trades):
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
            "wins": 0, "losses": 0, "count": 0,
        })
        if slot["count"] >= lookback:
            continue  # cap reached for this strategy
        slot["count"] += 1
        if pnl > 0.005:
            slot["wins"] += 1
        elif pnl < -0.005:
            slot["losses"] += 1
        # near-zero pnl is neither win nor loss
    # Compute win_rate per strategy
    for slot in by_strategy.values():
        cnt = slot["count"]
        slot["win_rate"] = (slot["wins"] / cnt) if cnt else 0.0
    return by_strategy


def get_threshold_multiplier(win_rate: float, sample_size: int,
                              min_sample: int = ADAPTIVE_MIN_SAMPLE,
                              cold_threshold: float = ADAPTIVE_COLD_WIN_RATE,
                              hot_threshold: float = ADAPTIVE_HOT_WIN_RATE,
                              max_demote: float = ADAPTIVE_MAX_DEMOTE,
                              max_boost: float = ADAPTIVE_MAX_BOOST,
                              ) -> float:
    """Return the multiplier to apply to a strategy's pick scores.

    Curve (win_rate → multiplier):
      * sample_size < min_sample          → 1.0 (no-op, insufficient data)
      * win_rate < cold_threshold (40%)   → max_demote (0.70)
      * win_rate > hot_threshold (60%)    → max_boost (1.30)
      * cold ≤ win_rate ≤ hot             → linear interpolation between
                                              max_demote and max_boost
    """
    if sample_size < min_sample:
        return 1.0
    if win_rate <= cold_threshold:
        return max_demote
    if win_rate >= hot_threshold:
        return max_boost
    # Linear interpolation between (cold, max_demote) and (hot, max_boost)
    span = hot_threshold - cold_threshold
    if span <= 0:
        return 1.0
    fraction = (win_rate - cold_threshold) / span
    return max_demote + fraction * (max_boost - max_demote)


def apply_adaptive_thresholds(picks: list,
                                win_rates: Optional[Mapping[str, dict]],
                                ) -> list:
    """Round-61 pt.41: scale each pick's ``best_score`` by its
    strategy's adaptive multiplier so cold strategies are deployed
    less aggressively and hot strategies more.

    Each pick is annotated with:
      * ``strategy_win_rate`` — rolling win rate for its strategy
      * ``strategy_sample_size`` — how many closed trades were used
      * ``adaptive_multiplier`` — what we multiplied the score by

    Picks whose strategy has no journal entries OR fewer than the
    minimum sample are passed through unchanged.

    Strategy name normalisation: picks use the screener's mixed-case
    names ("Breakout", "Mean Reversion") while the journal stores
    lowercase ("breakout", "mean_reversion"). Match both via
    snake-cased lower comparison.
    """
    if not picks:
        return picks
    win_rates = win_rates or {}
    if not win_rates:
        return picks

    # Build a lookup keyed by canonical (lowercase + snake_case) name.
    canonical_rates = {}
    for name, stats in win_rates.items():
        key = (name or "").lower().replace(" ", "_")
        canonical_rates[key] = stats

    for p in picks:
        strat = (p.get("best_strategy") or "").lower().replace(" ", "_")
        stats = canonical_rates.get(strat)
        if not stats:
            continue
        win_rate = float(stats.get("win_rate") or 0)
        sample = int(stats.get("count") or 0)
        mult = get_threshold_multiplier(win_rate, sample)
        p["strategy_win_rate"] = round(win_rate, 4)
        p["strategy_sample_size"] = sample
        p["adaptive_multiplier"] = round(mult, 4)
        if mult == 1.0:
            continue  # nothing to do
        try:
            p["best_score"] = float(p.get("best_score") or 0) * mult
        except (TypeError, ValueError):
            pass

    # Re-sort so demoted/boosted picks land in their new positions.
    picks.sort(key=lambda p: float(p.get("best_score") or 0), reverse=True)
    return picks


# ============================================================================
# Round-61 pt.42: composite-regime weighting
# ============================================================================
#
# Existing `apply_strategy_rotation` (in update_dashboard.py) uses a
# simple 3-bucket regime (bull / neutral / bear). Pt.42 layers on a
# richer composite regime built from THREE signals:
#
#   1. SPY trend — close vs 200-day MA. Positive = secular up-trend.
#   2. Breadth  — % of S&P stocks above their 50-day MA. >60% =
#                 broad participation; <40% = narrow leadership.
#   3. VIX      — volatility regime. <15 = complacent; >25 = stressed.
#
# Five composite regimes with hand-calibrated per-strategy weights.

REGIME_WEIGHTS: dict = {
    "strong_bull": {
        "Breakout": 1.40, "Mean Reversion": 0.50,
        "Wheel Strategy": 0.80, "PEAD": 1.30,
        "Copy Trading": 1.10, "short_sell": 0.20,
    },
    "weak_bull": {
        "Breakout": 1.15, "Mean Reversion": 0.85,
        "Wheel Strategy": 1.00, "PEAD": 1.10,
        "Copy Trading": 1.00, "short_sell": 0.50,
    },
    "choppy": {
        "Breakout": 0.70, "Mean Reversion": 1.30,
        "Wheel Strategy": 1.40, "PEAD": 1.00,
        "Copy Trading": 1.00, "short_sell": 0.80,
    },
    "weak_bear": {
        "Breakout": 0.55, "Mean Reversion": 1.10,
        "Wheel Strategy": 1.30, "PEAD": 0.85,
        "Copy Trading": 0.90, "short_sell": 1.30,
    },
    "strong_bear": {
        "Breakout": 0.30, "Mean Reversion": 0.85,
        "Wheel Strategy": 1.20, "PEAD": 0.60,
        "Copy Trading": 0.70, "short_sell": 1.50,
    },
}


def compute_composite_regime(spy_above_200ma: Optional[bool],
                              breadth_pct: Optional[float],
                              vix_estimate: Optional[float]) -> str:
    """Return one of five composite regime tags. Inputs are
    independently-derived market signals; missing inputs degrade
    gracefully toward "choppy" (neutral default)."""
    if spy_above_200ma is None:
        spy_above_200ma = True  # benign default
    breadth = float(breadth_pct) if breadth_pct is not None else 50.0
    vix = float(vix_estimate) if vix_estimate is not None else 18.0

    # Strong bear: SPY below 200MA + narrow + high VIX
    if not spy_above_200ma and breadth < 30 and vix > 25:
        return "strong_bear"
    if not spy_above_200ma:
        return "weak_bear"
    # Strong bull: above 200MA + broad + low VIX
    if breadth > 60 and vix < 15:
        return "strong_bull"
    # Weak bull: above 200MA + (good breadth OR low VIX)
    if breadth > 50 or vix < 18:
        return "weak_bull"
    return "choppy"


def apply_regime_weighting(picks: list, regime: str,
                             weights_table: Optional[Mapping[str, dict]] = None,
                             ) -> list:
    """Round-61 pt.42: multiply each pick's per-strategy score by its
    regime-fit weight. Tags every pick with `composite_regime` and
    `regime_weight_applied` for dashboard visibility.

    Adjusts breakout/mean_reversion/wheel/pead/copy/short scores.
    Recomputes `best_score` from the adjusted score for the pick's
    currently-chosen strategy. Layered ON TOP of
    `update_dashboard.apply_strategy_rotation`.
    """
    weights_table = weights_table or REGIME_WEIGHTS
    if not picks:
        return picks
    weights = weights_table.get(regime) or weights_table.get("choppy", {})

    score_fields = {
        "Breakout": "breakout_score",
        "Mean Reversion": "mean_reversion_score",
        "Wheel Strategy": "wheel_score",
        "PEAD": "pead_score",
        "Copy Trading": "copy_score",
        "short_sell": "short_score",
    }

    for p in picks:
        p["composite_regime"] = regime
        p["regime_weight_applied"] = dict(weights)
        for strat_name, field in score_fields.items():
            try:
                cur = float(p.get(field) or 0)
            except (TypeError, ValueError):
                cur = 0.0
            mult = weights.get(strat_name, 1.0)
            p[field] = cur * mult
        chosen = (p.get("best_strategy") or "").strip()
        chosen_field = score_fields.get(chosen)
        if chosen_field is None:
            for name, fld in score_fields.items():
                if (name.lower().replace(" ", "_") ==
                        chosen.lower().replace(" ", "_")):
                    chosen_field = fld
                    break
        if chosen_field is not None:
            try:
                p["best_score"] = float(p.get(chosen_field) or 0)
            except (TypeError, ValueError):
                pass

    picks.sort(key=lambda p: float(p.get("best_score") or 0), reverse=True)
    return picks
