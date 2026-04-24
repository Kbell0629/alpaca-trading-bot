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
