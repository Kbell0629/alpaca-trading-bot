"""Round-61 pt.49 — fractional-Kelly + correlation-aware position sizing.

Pure module — no I/O, stdlib only. Layered ON TOP of the existing
screener `recommended_shares` value as a multiplier. Two independent
adjustments:

  1. **Kelly multiplier** — based on the per-strategy realised edge
     (win_rate, avg_win, avg_loss) computed from the trade journal.
     Strong strategies get bigger size; weak strategies get smaller.
     Half-Kelly with a 25% absolute cap, mapped onto a [0.5, 2.0]
     multiplier so adjustments are bounded.
  2. **Correlation discount** — count existing positions that are
     plausibly correlated with the new candidate (same sector, or
     pt.35-style high-correlation list) and apply a 1/(1 + N)-style
     discount so the bot doesn't double-down on the same factor.

Combine: ``new_qty = base_qty * kelly_mult * correlation_mult``,
clamped to ``[1, base_qty * MAX_SCALE_UP]``.

Why "fractional" Kelly? Full Kelly is mathematically optimal for
geometric growth but assumes the win-rate and edge are KNOWN
exactly. With <100 trades the edge estimate has wide error bars;
half-Kelly is the standard practitioner choice — gives up some
upside in exchange for surviving a string of estimate-busting
losses. We also cap absolute Kelly at 25% per position so a hot
streak can't sneak above a quarter of the portfolio on one ticker.

This is layered ADDITIVELY — every legacy size cap (max_position_pct,
LIVE_MAX_DOLLARS, drawdown multiplier, settled-funds gate) still
applies after Kelly. Kelly only nudges within those bounds.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional


# ============================================================================
# Tunables
# ============================================================================

# Half-Kelly is the standard practitioner cut to compensate for
# uncertainty in the edge estimate. 1.0 = full Kelly, 0.5 = half.
DEFAULT_KELLY_FRACTION: float = 0.5

# Hard absolute cap on the fraction of bankroll any one position may
# claim per Kelly's formula. Guards against the "edge is huge!"
# error mode where a small sample inflates the apparent edge.
MAX_KELLY_FRACTION: float = 0.25

# Minimum closed trades for a strategy before the Kelly multiplier
# departs from 1.0. Below this, we don't have enough signal to trust
# the edge estimate — return the base size unchanged.
MIN_TRADES_FOR_KELLY: int = 10

# Multiplier bounds: the Kelly adjustment can scale a position by at
# most 2× up or 0.5× down. Outside of this range we're either
# over-confident in a noisy estimate or being too conservative — pin
# to the bounds and move on.
KELLY_MULT_FLOOR: float = 0.5
KELLY_MULT_CEILING: float = 2.0

# Baseline Kelly fraction that maps to a 1.0× multiplier. This is the
# Kelly fraction a "neutral" strategy would produce — strategies
# above this get scaled up; below get scaled down. 5% Kelly is a
# reasonable midpoint for a well-calibrated trading strategy.
BASELINE_KELLY: float = 0.05

# Correlation multiplier: each correlated already-held position
# multiplies the size by this factor. With the default 0.5, holding
# 0 correlated positions = 1.0× (no discount); 1 correlated = 0.5×;
# 2 correlated = 0.25×. Capped at CORRELATION_MULT_FLOOR.
CORRELATION_PER_POSITION_DISCOUNT: float = 0.5
CORRELATION_MULT_FLOOR: float = 0.25


# ============================================================================
# Kelly fraction
# ============================================================================

def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float,
                    *,
                    fraction_of: float = DEFAULT_KELLY_FRACTION,
                    max_fraction: float = MAX_KELLY_FRACTION) -> float:
    """Compute the (fractional) Kelly betting fraction for a strategy
    with the given empirical statistics.

    Kelly's formula for a binary win/lose bet:
        f* = (p * b - q) / b
    where p = win_rate, q = 1 - p, b = avg_win / |avg_loss|.

    Returns 0 if:
      * Inputs are invalid (NaN, negatives where they shouldn't be).
      * The strategy has negative edge (Kelly says don't bet).
      * avg_loss is non-negative (loss must be a NEGATIVE number).

    Otherwise returns ``fraction_of × kelly``, clamped to
    ``[0, max_fraction]``. ``fraction_of=0.5`` is the standard
    half-Kelly used in practice.
    """
    try:
        p = float(win_rate)
        aw = float(avg_win)
        al = float(avg_loss)
    except (TypeError, ValueError):
        return 0.0
    # Sanity guards.
    if not (0.0 <= p <= 1.0):
        return 0.0
    if aw <= 0:
        return 0.0  # need at least one win
    if al >= 0:
        return 0.0  # losses must be negative
    abs_al = abs(al)
    # Win/loss ratio.
    b = aw / abs_al
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f_star = (p * b - q) / b
    if f_star <= 0:
        return 0.0  # negative edge — Kelly says don't bet
    f = max(0.0, min(max_fraction, f_star * fraction_of))
    return f


# ============================================================================
# Per-strategy edge from the trade journal
# ============================================================================

def compute_strategy_edge(journal: Optional[Mapping],
                            strategy: str,
                            *,
                            min_trades: int = MIN_TRADES_FOR_KELLY) -> dict:
    """Aggregate closed-trade stats for `strategy` from the user's
    trade journal. Returns:

        {
          "trade_count": int,
          "win_rate": float,            # 0..1
          "avg_win": float,             # > 0 (or 0 if no wins)
          "avg_loss": float,            # < 0 (or 0 if no losses)
          "kelly_eligible": bool,       # trade_count >= min_trades
          "kelly_fraction": float,      # the fractional kelly value
        }

    `kelly_eligible` is False when the strategy doesn't have enough
    closed trades to compute a reliable edge — callers should treat
    that as "no adjustment, use base size".
    """
    out = {
        "trade_count": 0, "win_rate": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0,
        "kelly_eligible": False, "kelly_fraction": 0.0,
    }
    if not isinstance(journal, Mapping):
        return out
    trades = journal.get("trades") or []
    if not isinstance(trades, list):
        return out
    wins = []
    losses = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        if (t.get("strategy") or "").lower() != strategy.lower():
            continue
        try:
            pnl = float(t.get("pnl"))
        except (TypeError, ValueError):
            continue
        if pnl > 0.005:
            wins.append(pnl)
        elif pnl < -0.005:
            losses.append(pnl)
        # Flat trades excluded — neither win nor loss for edge calc.
    cnt = len(wins) + len(losses)
    out["trade_count"] = cnt
    if cnt == 0:
        return out
    out["win_rate"] = round(len(wins) / cnt, 4)
    out["avg_win"] = round(sum(wins) / len(wins), 4) if wins else 0.0
    out["avg_loss"] = round(sum(losses) / len(losses), 4) if losses else 0.0
    if cnt >= min_trades:
        out["kelly_eligible"] = True
        out["kelly_fraction"] = round(
            kelly_fraction(out["win_rate"], out["avg_win"],
                            out["avg_loss"]), 4)
    return out


# ============================================================================
# Kelly multiplier — maps fractional Kelly to a size adjustment in
# [floor, ceiling].
# ============================================================================

def kelly_size_multiplier(edge: Mapping,
                            *,
                            baseline_kelly: float = BASELINE_KELLY,
                            floor: float = KELLY_MULT_FLOOR,
                            ceiling: float = KELLY_MULT_CEILING) -> float:
    """Convert the per-strategy edge dict into a position-size
    multiplier. Returns 1.0 (no adjustment) if the strategy is
    not Kelly-eligible (insufficient sample) — the legacy size
    flow remains in charge.

    Mapping:
      * Kelly = 0       → multiplier = floor (0.5×)
      * Kelly = baseline → multiplier = 1.0× (no adjustment)
      * Kelly = max_kelly → multiplier = ceiling (2.0×)
      * Linear interpolation between these anchor points.
    """
    if not isinstance(edge, Mapping):
        return 1.0
    if not edge.get("kelly_eligible"):
        return 1.0
    try:
        k = float(edge.get("kelly_fraction") or 0)
    except (TypeError, ValueError):
        return 1.0
    if k <= 0:
        return floor
    base = float(baseline_kelly)
    if k <= base:
        # Linear from (0, floor) to (base, 1.0).
        if base <= 0:
            return 1.0
        slope = (1.0 - floor) / base
        return round(floor + slope * k, 4)
    # Linear from (base, 1.0) to (max, ceiling).
    span = MAX_KELLY_FRACTION - base
    if span <= 0:
        return ceiling
    over = min(k - base, span)
    slope = (ceiling - 1.0) / span
    return round(1.0 + slope * over, 4)


# ============================================================================
# Correlation multiplier
# ============================================================================

def count_correlated_positions(symbol: str,
                                 existing_positions: Optional[Iterable],
                                 *,
                                 sector_map: Optional[Mapping] = None) -> int:
    """Count how many existing positions are plausibly correlated
    with `symbol`. Uses the sector map (same-sector ⇒ correlated).
    Falls back to the canonical ``constants.SECTOR_MAP`` when no
    explicit map is passed in.

    Returns 0 if `existing_positions` is empty / None / `symbol` is
    falsy. Symbols not in the sector map are treated as "Other" and
    don't contribute to the correlated count (avoids over-discounting
    on unrelated tickers like MARA + HIMS + TAL).
    """
    if not symbol or not existing_positions:
        return 0
    smap = sector_map
    if smap is None:
        try:
            from constants import SECTOR_MAP
            smap = SECTOR_MAP
        except ImportError:
            smap = {}
    sym_upper = symbol.upper()
    sym_sector = smap.get(sym_upper)
    if not sym_sector or sym_sector == "Other":
        # No reliable sector for the candidate — can't measure
        # correlation. Don't discount.
        return 0
    correlated = 0
    for pos in existing_positions:
        if not isinstance(pos, Mapping):
            continue
        other_sym = (pos.get("symbol") or "").upper()
        if not other_sym or other_sym == sym_upper:
            continue
        other_sector = smap.get(other_sym)
        if other_sector and other_sector == sym_sector:
            correlated += 1
    return correlated


def correlation_size_multiplier(correlated_count: int,
                                  *,
                                  per_position_discount: float = CORRELATION_PER_POSITION_DISCOUNT,
                                  floor: float = CORRELATION_MULT_FLOOR) -> float:
    """Discount factor: each correlated position cuts the size by
    `per_position_discount` (default 0.5×). Floored at `floor`
    (default 0.25× — never go below a quarter of the base size).
    """
    try:
        n = int(correlated_count)
    except (TypeError, ValueError):
        return 1.0
    if n <= 0:
        return 1.0
    mult = per_position_discount ** n
    return max(floor, round(mult, 4))


# ============================================================================
# Round-61 pt.58: confluence multiplier
# ============================================================================
# More-confirming-signals → bigger size; weak/single-factor picks
# → smaller size. Counts up to 5 binary "this signal is on" flags
# from the pick dict and maps the count to a multiplier in
# [0.7, 1.5]. Designed to ride pt.49's Kelly + correlation —
# applied as another step in compute_full_size.
#
# Signals (each adds 1 to confluence_count):
#   * `best_score >= 80`  — high-conviction screener pick
#   * `news_sentiment == "positive"` — keyword-based news scan
#   * `llm_sentiment_score > 3` — Gemini/LLM read says bullish
#   * `(insider_data or {}).get("has_cluster_buy")` — SEC Form 4 cluster
#   * `mtf_alignment == "aligned"` — daily + weekly trend agree
# Two missing mappings:
#   * 0–1 signals  → 0.7×  (down-size)
#   * 2 signals    → 1.0×  (no change)
#   * 3 signals    → 1.2×
#   * 4–5 signals  → 1.5×

CONFLUENCE_MULT_FLOOR: float = 0.7
CONFLUENCE_MULT_CEILING: float = 1.5

CONFLUENCE_HIGH_SCORE_THRESHOLD: float = 80.0
CONFLUENCE_POSITIVE_LLM_THRESHOLD: float = 3.0


def count_confluence_signals(pick: Optional[Mapping]) -> int:
    """Count binary positive signals on the pick dict. Returns 0..5."""
    if not isinstance(pick, Mapping):
        return 0
    count = 0
    try:
        if float(pick.get("best_score") or 0) >= CONFLUENCE_HIGH_SCORE_THRESHOLD:
            count += 1
    except (TypeError, ValueError):
        pass
    if (pick.get("news_sentiment") or "").lower() == "positive":
        count += 1
    try:
        if float(pick.get("llm_sentiment_score") or 0) > CONFLUENCE_POSITIVE_LLM_THRESHOLD:
            count += 1
    except (TypeError, ValueError):
        pass
    insider = pick.get("insider_data") or {}
    if isinstance(insider, Mapping) and insider.get("has_cluster_buy"):
        count += 1
    if (pick.get("mtf_alignment") or "").lower() == "aligned":
        count += 1
    return count


def confluence_size_multiplier(count: int,
                                 *,
                                 floor: float = CONFLUENCE_MULT_FLOOR,
                                 ceiling: float = CONFLUENCE_MULT_CEILING) -> float:
    """Map confluence-signal count to a size multiplier:
        0–1 signals → floor (default 0.7×)
        2 signals   → 1.0×
        3 signals   → 1.2×
        4–5 signals → ceiling (default 1.5×)
    """
    try:
        n = int(count)
    except (TypeError, ValueError):
        return 1.0
    if n <= 1:
        return floor
    if n == 2:
        return 1.0
    if n == 3:
        return 1.2
    return ceiling


# ============================================================================
# End-to-end wrapper
# ============================================================================

def compute_full_size(*,
                       base_qty: int,
                       strategy: str,
                       symbol: str,
                       journal: Optional[Mapping] = None,
                       existing_positions: Optional[Iterable] = None,
                       sector_map: Optional[Mapping] = None,
                       pick: Optional[Mapping] = None,
                       enable_kelly: bool = True,
                       enable_correlation: bool = True,
                       enable_confluence: bool = True,
                       ) -> dict:
    """Apply Kelly + correlation + confluence multipliers on top of
    `base_qty`. Round-61 pt.58 added the confluence step which reads
    the pick dict (`best_score`, `news_sentiment`,
    `llm_sentiment_score`, `insider_data.has_cluster_buy`,
    `mtf_alignment`) and counts up to 5 positive signals. More
    signals → bigger size, fewer signals → smaller. Independent of
    Kelly (compounds multiplicatively).

    Returns:
      {
        "qty": int,                        # final qty (>= 1 if base_qty >= 1)
        "base_qty": int,                   # what the caller passed in
        "kelly_multiplier": float,         # 1.0 if disabled or ineligible
        "correlation_multiplier": float,   # 1.0 if disabled or no correlation
        "confluence_multiplier": float,    # 1.0 if pick=None or disabled
        "confluence_count": int,           # 0..5 signals lit
        "edge": {...},                     # the per-strategy edge dict
        "correlated_count": int,           # how many correlated positions
        "rationale": str,                  # human-readable explanation
      }
    """
    try:
        bq = int(base_qty)
    except (TypeError, ValueError):
        bq = 0
    if bq <= 0:
        return {
            "qty": 0, "base_qty": 0,
            "kelly_multiplier": 1.0, "correlation_multiplier": 1.0,
            "confluence_multiplier": 1.0, "confluence_count": 0,
            "edge": {}, "correlated_count": 0,
            "rationale": "base_qty <= 0 — no sizing applied",
        }
    edge = {}
    k_mult = 1.0
    if enable_kelly:
        edge = compute_strategy_edge(journal, strategy)
        k_mult = kelly_size_multiplier(edge)
    correlated = 0
    c_mult = 1.0
    if enable_correlation:
        correlated = count_correlated_positions(
            symbol, existing_positions, sector_map=sector_map)
        c_mult = correlation_size_multiplier(correlated)
    confluence_count = 0
    f_mult = 1.0
    if enable_confluence and pick is not None:
        confluence_count = count_confluence_signals(pick)
        f_mult = confluence_size_multiplier(confluence_count)
    final_float = bq * k_mult * c_mult * f_mult
    final_qty = max(1, int(final_float))
    rationale_bits = []
    if k_mult != 1.0:
        rationale_bits.append(
            f"Kelly {k_mult:.2f}× ({strategy} edge: "
            f"WR={edge.get('win_rate', 0):.0%}, "
            f"K={edge.get('kelly_fraction', 0):.3f})")
    if c_mult != 1.0:
        rationale_bits.append(
            f"correlation {c_mult:.2f}× ({correlated} correlated)")
    if f_mult != 1.0:
        rationale_bits.append(
            f"confluence {f_mult:.2f}× ({confluence_count}/5 signals)")
    if not rationale_bits:
        rationale_bits.append("base size (no adjustment)")
    return {
        "qty": final_qty, "base_qty": bq,
        "kelly_multiplier": round(k_mult, 4),
        "correlation_multiplier": round(c_mult, 4),
        "confluence_multiplier": round(f_mult, 4),
        "confluence_count": confluence_count,
        "edge": edge,
        "correlated_count": correlated,
        "rationale": "; ".join(rationale_bits),
    }
