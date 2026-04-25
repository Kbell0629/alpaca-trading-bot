"""Round-61 pt.49 — pipeline-aware backtest harness.

The pt.37 backtest tests the STRATEGY in isolation: "if breakout
fired on this symbol, would the trade have been profitable?". The
production deploy pipeline is much harder than that — chase-block
filters, sector caps, correlation guards, trend filter, event-day
gate, score thresholds. A pick can pass the strategy signal but
still not deploy because one of the deploy-side gates blocks it.

Pt.49 adds a second backtest layer: take a historical picks
sequence and replay it through the FULL deploy pipeline. Reports:

  * `total_picks` — picks the screener produced
  * `would_deploy` — picks that pass EVERY gate
  * `blocked_by_reason` — `{chase_block: 12, sector_cap: 4, ...}`
  * `deploys` — the actual list of picks that passed
  * (optional) per-pick counterfactual outcome simulated by
    `backtest_core._simulate_symbol`

This answers "are we being too conservative? are the gates blocking
profitable picks?" — a question pt.45's filter chips surface
visually but never quantified.

Pure module — caller injects the gate logic where production
imports are heavy. Default gate set mirrors the pt.10-48
production gates so a quick `run_pipeline_backtest(picks_history)`
call produces a realistic answer.
"""
from __future__ import annotations

from datetime import date as _date_cls
from typing import Iterable, Mapping, Optional


# ============================================================================
# Default gate thresholds (mirror production)
# ============================================================================

DEFAULT_CHASE_BLOCK_PCT: float = 8.0     # daily_change > 8% blocked
DEFAULT_VOLATILITY_BLOCK_PCT: float = 25.0  # volatility > 25% blocked
DEFAULT_MAX_PER_SECTOR: int = 2           # max 2 same-sector positions
DEFAULT_MIN_SCORE: float = 50.0           # implicit good-pick threshold
DEFAULT_EVENT_SCORE_GATE: float = 50.0    # × event multiplier on event days


# ============================================================================
# Per-pick gate evaluation
# ============================================================================

def evaluate_gates(pick: Mapping,
                    *,
                    held_symbols: Optional[set] = None,
                    sector_counts: Optional[Mapping] = None,
                    sector_map: Optional[Mapping] = None,
                    chase_block_pct: float = DEFAULT_CHASE_BLOCK_PCT,
                    volatility_block_pct: float = DEFAULT_VOLATILITY_BLOCK_PCT,
                    max_per_sector: int = DEFAULT_MAX_PER_SECTOR,
                    min_score: float = DEFAULT_MIN_SCORE,
                    event_label: Optional[str] = None,
                    event_multiplier: float = 1.0,
                    ) -> dict:
    """Run a single pick through every production gate. Returns:

        {
          "deploy": bool,
          "block_reasons": [reason, ...],   # empty when deploy=True
        }

    Reasons (pulled from pt.10-48 production):
      * `already_held` — symbol already in `held_symbols`
      * `below_50ma` / `above_50ma` — trend filter (pt.39 tags
                                        bridged from `_filtered_by_trend`)
      * `breakout_unconfirmed` — pt.40 multi-day breakout failed
      * `chase_block` — daily_change > chase_block_pct
      * `volatility_block` — volatility > volatility_block_pct
      * `sector_cap` — already 2+ positions in same sector
      * `event_day_score` — pt.48 event-day multiplier kicks score
                              gate higher than the pick's score
      * `min_score` — pick.best_score < min_score
    """
    if not isinstance(pick, Mapping):
        return {"deploy": False, "block_reasons": ["invalid_pick"]}
    held_symbols = held_symbols or set()
    sector_counts = dict(sector_counts or {})
    sector_map = sector_map or {}

    reasons = []
    symbol = (pick.get("symbol") or "").upper()
    if not symbol:
        return {"deploy": False, "block_reasons": ["missing_symbol"]}

    # 1. Already held
    if symbol in held_symbols:
        reasons.append("already_held")

    # 2. Trend filter (pt.39): the screener tags
    #    `_filtered_by_trend = True` and `_filtered_strategy = "..."`
    #    when the pick is below SMA(50) for a long strategy or above
    #    SMA(50) for a short strategy. Bridge into the canonical reason.
    if pick.get("_filtered_by_trend"):
        strat = (pick.get("best_strategy") or "").lower().replace(" ", "_")
        if strat == "short_sell":
            reasons.append("above_50ma")
        else:
            reasons.append("below_50ma")

    # 3. Breakout unconfirmed (pt.40)
    if pick.get("_breakout_unconfirmed"):
        reasons.append("breakout_unconfirmed")

    # 4. Chase block — daily_change too steep
    try:
        dc = float(pick.get("daily_change") or 0)
    except (TypeError, ValueError):
        dc = 0.0
    if dc > chase_block_pct:
        reasons.append("chase_block")

    # 5. Volatility block — too noisy
    try:
        vol = float(pick.get("volatility") or 0)
    except (TypeError, ValueError):
        vol = 0.0
    if vol > volatility_block_pct:
        reasons.append("volatility_block")

    # 6. Sector cap — running count of same-sector deploys this run
    sector = sector_map.get(symbol) or pick.get("sector") or "Other"
    if sector and sector != "Other":
        if sector_counts.get(sector, 0) >= max_per_sector:
            reasons.append("sector_cap")

    # 7. Event-day score gate (pt.48)
    try:
        score = float(pick.get("best_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    if event_label and event_multiplier > 1.0:
        gate = DEFAULT_EVENT_SCORE_GATE * event_multiplier
        if score < gate:
            reasons.append(f"event_day_{event_label.lower()}")

    # 8. Below the implicit min-score baseline.
    if score < min_score:
        # Don't double-count — only fire if no event-day reason yet.
        if not any(r.startswith("event_day_") for r in reasons):
            reasons.append("min_score")

    return {
        "deploy": len(reasons) == 0,
        "block_reasons": reasons,
        "sector": sector,
    }


# ============================================================================
# End-to-end harness
# ============================================================================

def run_pipeline_backtest(picks_history: Iterable[Mapping],
                            *,
                            sector_map: Optional[Mapping] = None,
                            initial_held: Optional[set] = None,
                            chase_block_pct: float = DEFAULT_CHASE_BLOCK_PCT,
                            volatility_block_pct: float = DEFAULT_VOLATILITY_BLOCK_PCT,
                            max_per_sector: int = DEFAULT_MAX_PER_SECTOR,
                            min_score: float = DEFAULT_MIN_SCORE,
                            event_label_fn=None,
                            simulate_outcomes: bool = False,
                            bars_by_symbol: Optional[Mapping] = None,
                            ) -> dict:
    """Replay a sequence of historical picks through the full deploy
    pipeline.

    Args:
      picks_history: iterable of ``{date, picks: [pick_dict, ...]}``.
        Each `pick_dict` should match the screener's pick schema —
        symbol, best_strategy, best_score, daily_change, volatility,
        sector, _filtered_by_trend, _breakout_unconfirmed, etc.
      sector_map: optional override; falls back to ``constants.SECTOR_MAP``.
      initial_held: positions already open at the start of the
        replay window. Defaults to empty set.
      chase_block_pct / volatility_block_pct / max_per_sector /
        min_score: gate thresholds. Production defaults shown in
        module-level constants.
      event_label_fn: optional ``callable(date) -> (label, multiplier)``
        for the pt.48 event gate. If None, falls back to
        `event_calendar.is_high_impact_event_day`.
      simulate_outcomes: if True, fold each deployed pick's
        counterfactual P&L using `backtest_core._simulate_symbol`.
        Requires `bars_by_symbol`.
      bars_by_symbol: per-symbol OHLCV bars when
        `simulate_outcomes=True`.

    Returns the summary dict described in the module docstring.
    """
    if event_label_fn is None:
        try:
            from event_calendar import (
                is_high_impact_event_day, event_score_multiplier,
            )

            def _fn(d):
                hit, label = is_high_impact_event_day(d)
                if hit and label:
                    return label, event_score_multiplier(label)
                return None, 1.0

            event_label_fn = _fn
        except ImportError:
            event_label_fn = lambda d: (None, 1.0)  # noqa: E731

    if sector_map is None:
        try:
            from constants import SECTOR_MAP
            sector_map = SECTOR_MAP
        except ImportError:
            sector_map = {}

    held = set(initial_held or set())
    blocked_by_reason: dict = {}
    deploys: list = []
    blocks_by_day: list = []
    total_picks = 0

    for day in (picks_history or []):
        if not isinstance(day, Mapping):
            continue
        d = day.get("date")
        picks = day.get("picks") or []
        if not isinstance(picks, list):
            continue
        try:
            evt_label, evt_mult = event_label_fn(d)
        except Exception:
            evt_label, evt_mult = None, 1.0
        sector_counts: dict = {}
        for pos_sym in held:
            sec = sector_map.get(pos_sym) or "Other"
            if sec and sec != "Other":
                sector_counts[sec] = sector_counts.get(sec, 0) + 1
        day_block_summary: dict = {}
        day_deploys: list = []
        for pick in picks:
            total_picks += 1
            res = evaluate_gates(
                pick,
                held_symbols=held,
                sector_counts=sector_counts,
                sector_map=sector_map,
                chase_block_pct=chase_block_pct,
                volatility_block_pct=volatility_block_pct,
                max_per_sector=max_per_sector,
                min_score=min_score,
                event_label=evt_label,
                event_multiplier=evt_mult,
            )
            if res["deploy"]:
                sym = (pick.get("symbol") or "").upper()
                day_deploys.append({
                    "date": d,
                    "symbol": sym,
                    "strategy": pick.get("best_strategy"),
                    "score": pick.get("best_score"),
                    "sector": res.get("sector"),
                })
                # Fold this deploy into running held + sector counts so
                # subsequent picks on the same day see the cap.
                held.add(sym)
                sec = res.get("sector") or "Other"
                if sec and sec != "Other":
                    sector_counts[sec] = sector_counts.get(sec, 0) + 1
            else:
                for reason in res["block_reasons"]:
                    blocked_by_reason[reason] = (
                        blocked_by_reason.get(reason, 0) + 1)
                    day_block_summary[reason] = (
                        day_block_summary.get(reason, 0) + 1)
        if day_deploys or day_block_summary:
            blocks_by_day.append({
                "date": d, "deploys": len(day_deploys),
                "blocks": day_block_summary,
                "event_label": evt_label,
            })
        deploys.extend(day_deploys)

    out = {
        "total_picks": total_picks,
        "would_deploy": len(deploys),
        "blocked_by_reason": blocked_by_reason,
        "deploys": deploys,
        "blocks_by_day": blocks_by_day,
        "block_rate": (
            round(1.0 - (len(deploys) / total_picks), 4)
            if total_picks else 0.0),
    }

    # Counterfactual P&L: simulate each deployed pick's outcome
    # against `bars_by_symbol`.
    if simulate_outcomes and bars_by_symbol:
        out["counterfactual"] = _simulate_deploys(
            deploys, bars_by_symbol)
    return out


def _simulate_deploys(deploys, bars_by_symbol) -> dict:
    """Simulate each deploy via backtest_core._simulate_symbol with
    DEFAULT_PARAMS for the strategy. Returns aggregate trade stats."""
    try:
        from backtest_core import (
            _simulate_symbol, _summarize, DEFAULT_PARAMS,
            BACKTESTABLE_STRATEGIES,
        )
    except ImportError:
        return {"error": "backtest_core unavailable"}
    pooled = []
    for dep in deploys:
        strat = (dep.get("strategy") or "").lower().replace(" ", "_")
        if strat not in BACKTESTABLE_STRATEGIES:
            continue
        sym = dep.get("symbol")
        bars = (bars_by_symbol or {}).get(sym)
        if not bars or len(bars) < 5:
            continue
        params = dict(DEFAULT_PARAMS[strat])
        # Slice bars to start from the deploy date.
        deploy_date = dep.get("date")
        if deploy_date and isinstance(deploy_date, (str, _date_cls)):
            target = (str(deploy_date) if isinstance(deploy_date, str)
                       else deploy_date.isoformat())
            sliced = []
            seen_start = False
            for b in bars:
                if not seen_start and b.get("date") and b["date"] >= target:
                    seen_start = True
                if seen_start:
                    sliced.append(b)
            bars_to_use = sliced or bars
        else:
            bars_to_use = bars
        trades = _simulate_symbol(sym, bars_to_use, strat, params)
        pooled.extend(trades)
    return {
        "trades": pooled,
        "summary": _summarize(pooled),
    }
