#!/usr/bin/env python3
"""
factor_enrichment.py — Relative Strength + Sector Rotation factors.

Round-11 Tier 1 addition. Two classic equity factors layered on top
of the existing screener scores:

1) Relative Strength (Jegadeesh & Titman 1993): rank stocks by how
   much they've outperformed SPY over 3m and 6m. Best-RS stocks in
   a trending market carry 2-3x the forward return of worst-RS.

2) Sector Rotation: compute each sector ETF's 1-month return, rank,
   and apply a multiplier to picks in the strongest sectors (x1.2)
   and weakest (x0.8). Sector momentum is one of the most persistent
   factors in equity research.

Public API:

    compute_relative_strength(pick_bars, spy_bars) -> dict
        {rs_3m, rs_6m, rs_composite} — all decimals (0.15 = +15%)

    rank_sectors_by_momentum(data_dir=None, max_age_hours=24) -> dict
        {XLK: {return_1m, rank, multiplier}, ...}
        Caches to disk. rank=1 is strongest, multiplier ≥1 means boost.

    sector_multiplier_for_symbol(sym, sector_rankings, sector_map) -> float
        0.8 .. 1.2, centered at 1.0 for middle-ranked sectors.

    apply_factor_scores(picks, spy_bars, data_dir=None) -> picks
        Mutates each pick dict: adds rs_3m, rs_6m, rs_score,
        sector_etf, sector_rank, sector_multiplier. Also applies
        the factor boost/penalty to existing {breakout,mean_reversion,
        pead}_score fields so downstream ranking picks up the new
        signal without code changes elsewhere.
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime

try:
    from et_time import now_et
except ImportError:
    def now_et():
        return datetime.now()

try:
    from constants import SECTOR_MAP
except ImportError:
    SECTOR_MAP = {}


# SPDR sector ETFs (11 standard GICS sectors + the catch-all "Other"
# which we don't assign an ETF to). Map from the SECTOR_MAP string
# to its tracking ETF symbol.
SECTOR_ETFS = {
    "Tech": "XLK",
    "Finance": "XLF",
    "Healthcare": "XLV",
    "Consumer": "XLY",      # Consumer discretionary (closer to AMZN/TSLA/NKE basket)
    "ConsumerStaples": "XLP",
    "Industrial": "XLI",
    "Energy": "XLE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "REIT": "XLRE",
    "Media": "XLC",          # Communication services
    # Crypto / Education / Other -> no clean sector ETF, skip rotation
}


def _cache_path(data_dir, name):
    base = data_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def _load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(path, data):
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except Exception as e:
        print(f"[factor_enrichment] cache save failed: {e}")


# ---------------------------------------------------------------------------
# Relative Strength
# ---------------------------------------------------------------------------

def _pct_change(closes, lookback):
    """(closes[-1] / closes[-lookback-1] - 1) with guardrails."""
    if not closes or len(closes) < lookback + 1:
        return 0.0
    try:
        base = float(closes[-lookback - 1])
        last = float(closes[-1])
        if base <= 0:
            return 0.0
        return (last / base) - 1.0
    except (ValueError, TypeError, IndexError):
        return 0.0


def compute_relative_strength(pick_bars, spy_bars):
    """Compute stock's return - SPY's return over 3m (~63 trading days)
    and 6m (~126 days). Composite weights 3m heavier (more recent trend
    matters more). Returns dict with three decimals.

    Designed to work with whatever bars are available — if only 63 days
    are present, 6m falls back to available range. Returns 0s on any
    error so callers can proceed with a neutral factor."""
    def _closes(bars):
        return [b.get("c", 0) or 0 for b in (bars or [])]
    p = _closes(pick_bars)
    s = _closes(spy_bars)
    if not p or not s:
        return {"rs_3m": 0.0, "rs_6m": 0.0, "rs_composite": 0.0}
    # Use the shorter of (63, len(bars)-1) so we handle shorter series
    n3 = min(63, len(p) - 1, len(s) - 1)
    n6 = min(126, len(p) - 1, len(s) - 1)
    if n3 < 5:
        return {"rs_3m": 0.0, "rs_6m": 0.0, "rs_composite": 0.0}
    rs_3m = _pct_change(p, n3) - _pct_change(s, n3)
    rs_6m = _pct_change(p, n6) - _pct_change(s, n6) if n6 >= n3 else rs_3m
    rs_composite = 0.6 * rs_3m + 0.4 * rs_6m  # 3m weighted heavier
    return {
        "rs_3m": round(rs_3m, 4),
        "rs_6m": round(rs_6m, 4),
        "rs_composite": round(rs_composite, 4),
    }


# ---------------------------------------------------------------------------
# Sector Rotation
# ---------------------------------------------------------------------------

def rank_sectors_by_momentum(data_dir=None, max_age_hours=24):
    """Fetches 1-month returns for each sector ETF via yfinance, ranks
    them, and returns {ETF: {sector, return_1m, rank, multiplier}}.

    Multiplier mapping (1 = strongest of 11):
       rank 1-2  -> 1.20 (boost)
       rank 3-5  -> 1.10
       rank 6-7  -> 1.00 (neutral)
       rank 8-9  -> 0.95
       rank 10-11-> 0.85 (penalty)

    Cached for 24h (sector ranks are slow-moving).
    """
    path = _cache_path(data_dir, "sector_rankings.json")
    cached = _load_cache(path)
    if cached and cached.get("computed_at"):
        try:
            computed = datetime.fromisoformat(cached["computed_at"])
            if computed.tzinfo is not None:
                computed = computed.replace(tzinfo=None)
            age_hours = (now_et().replace(tzinfo=None) - computed).total_seconds() / 3600.0
            if age_hours < max_age_hours:
                return cached.get("rankings", {})
        except (ValueError, TypeError):
            pass

    # Round-11: shared rate-limit wrapper (yfinance_budget) so we don't
    # contend with market_breadth + quality_filter + iv_rank for the
    # same Yahoo quota on every screener run.
    try:
        from yfinance_budget import yf_download
    except ImportError:
        try:
            import yfinance as yf
            yf_download = lambda **kw: yf.download(**kw)
        except ImportError:
            return {}

    etfs = list(SECTOR_ETFS.values())
    data = yf_download(
        tickers=" ".join(etfs),
        period="2mo",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    if data is None:
        print("[factor_enrichment] yfinance download returned None (rate-limited?)")
        return {}

    returns = {}
    for etf in etfs:
        try:
            if hasattr(data, "columns") and hasattr(data.columns, "levels"):
                closes = data[etf]["Close"].dropna()
            else:
                closes = data["Close"].dropna()
            # ~21 trading days in 1 month
            if len(closes) < 22:
                continue
            ret = float(closes.iloc[-1]) / float(closes.iloc[-22]) - 1.0
            returns[etf] = ret
        except Exception:
            continue

    if not returns:
        return {}

    # Rank 1 (highest return) to N (lowest)
    sorted_etfs = sorted(returns.items(), key=lambda x: x[1], reverse=True)
    rankings = {}
    n = len(sorted_etfs)
    for rank, (etf, ret) in enumerate(sorted_etfs, start=1):
        # Map rank -> multiplier
        if rank <= 2:
            mult = 1.20
        elif rank <= 5:
            mult = 1.10
        elif rank <= 7:
            mult = 1.00
        elif rank <= 9:
            mult = 0.95
        else:
            mult = 0.85
        # Find the sector name for this ETF
        sector_name = next((s for s, e in SECTOR_ETFS.items() if e == etf), "")
        rankings[etf] = {
            "sector": sector_name,
            "return_1m": round(ret, 4),
            "rank": rank,
            "of": n,
            "multiplier": mult,
        }

    _save_cache(path, {
        "rankings": rankings,
        "computed_at": now_et().isoformat(),
    })
    return rankings


def sector_multiplier_for_symbol(sym, sector_rankings, sector_map=None):
    """Returns the factor multiplier (0.85..1.20) for a symbol based
    on its sector's ETF ranking. Returns 1.0 when the symbol is in
    an un-mapped sector (Crypto, Education, Other) — those get no
    boost or penalty."""
    sm = sector_map if sector_map is not None else SECTOR_MAP
    sector = sm.get(sym, "Other")
    etf = SECTOR_ETFS.get(sector)
    if not etf or etf not in sector_rankings:
        return 1.0
    return sector_rankings[etf].get("multiplier", 1.0)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def apply_factor_scores(picks, spy_bars, bars_map=None, data_dir=None):
    """Enriches picks with RS + sector factors and applies the multi-
    plier to existing strategy scores so downstream ranking picks up
    the signal without touching callsites.

    Args:
        picks: list of pick dicts (mutated in place)
        spy_bars: list of SPY daily bars (from fetch_historical_bars)
        bars_map: optional {symbol: bars} for per-pick RS computation.
                  Falls back to pick["bars"] if present, else skips RS.
        data_dir: for sector_rankings cache

    For each pick the following fields are added:
        rs_3m, rs_6m, rs_composite: decimals
        rs_score: normalized 0..100 score added to breakout_score +
                  pead_score (momentum strategies benefit from RS)
        sector_etf, sector_rank, sector_multiplier
        factor_adjusted: True flag so we can see this ran
    """
    if not picks:
        return picks
    sector_rankings = rank_sectors_by_momentum(data_dir=data_dir) or {}

    for pick in picks:
        sym = (pick.get("symbol") or "").upper()
        # --- Relative Strength ---
        pick_bars = None
        if bars_map and sym in bars_map:
            pick_bars = bars_map[sym]
        elif "bars" in pick:
            pick_bars = pick["bars"]
        rs = compute_relative_strength(pick_bars, spy_bars) if (pick_bars and spy_bars) else \
             {"rs_3m": 0.0, "rs_6m": 0.0, "rs_composite": 0.0}
        pick.update(rs)
        # Convert rs_composite into a 0..50 score bonus. rs of +10%
        # vs SPY over 3m is strong (50th percentile ~= 0). Clamp to
        # [-25, +25] so it never dominates the base strategy score.
        rs_bonus = max(-25, min(25, rs["rs_composite"] * 250))
        pick["rs_score"] = round(rs_bonus, 1)

        # --- Sector Rotation ---
        sector = SECTOR_MAP.get(sym, "Other")
        etf = SECTOR_ETFS.get(sector)
        sector_info = sector_rankings.get(etf, {}) if etf else {}
        mult = sector_info.get("multiplier", 1.0)
        pick["sector_etf"] = etf or ""
        pick["sector_rank"] = sector_info.get("rank", 0)
        pick["sector_multiplier"] = mult

        # Apply: boost momentum strategies (breakout, pead) by RS,
        # and scale all entry strategy scores by sector multiplier.
        for s_key in ("breakout_score", "pead_score"):
            if s_key in pick and isinstance(pick[s_key], (int, float)):
                pick[s_key] = round((pick[s_key] + rs_bonus) * mult, 2)
        for s_key in ("mean_reversion_score", "wheel_score"):
            if s_key in pick and isinstance(pick[s_key], (int, float)):
                # MR is counter-trend, doesn't benefit from RS.
                # Wheel cares more about IV than RS.
                # But both benefit from being in healthy sectors.
                pick[s_key] = round(pick[s_key] * mult, 2)

        pick["factor_adjusted"] = True

    return picks


if __name__ == "__main__":
    # Smoke test
    import random
    random.seed(0)
    spy = [{"c": 100 + i * 0.1 + random.uniform(-1, 1)} for i in range(130)]
    stock = [{"c": 100 + i * 0.2 + random.uniform(-1, 1)} for i in range(130)]  # outperforming
    rs = compute_relative_strength(stock, spy)
    print("RS:", rs)
    ranks = rank_sectors_by_momentum()
    print("Sector ranks (live):", json.dumps(ranks, indent=2)[:500] if ranks else "unavailable")
