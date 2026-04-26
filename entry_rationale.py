"""Round-61 pt.82 — per-trade entry-rationale audit.

Every closed trade has an `exit_reason` field but not an
`entry_rationale`. Recording the WHY at deploy time turns
post-mortems from guesswork into structured queries:

  * "Why did we buy SOXL on Tuesday?" → look at entry_rationale.
  * "Did the +RS picks outperform the -RS picks?" → aggregate by
    rationale.rs_score across closed trades.
  * Future weekly self-learning loop can correlate WHICH entry
    conditions (high score, bullish news, low VWAP offset, etc.)
    actually predict winners and re-weight scoring accordingly.

Pure module — caller passes a screener `pick` dict (and optional
sizing + regime metadata) and gets back a structured rationale
dict ready to embed in the trade-journal entry.

Use:
    >>> from entry_rationale import build_entry_rationale
    >>> r = build_entry_rationale(pick, sizing_info=size, regime="bull-strong")
    >>> r["headline"]
    "score=485 RS=+8 sector=Technology(strong) news=bullish vwap=+0.3% conf=4/5"
"""
from __future__ import annotations

from typing import Mapping, Optional


# Confluence signals — same set the pt.58 confluence-multiplier
# uses, so a closed trade's rationale tells you exactly which
# signals lit up at deploy time.
_SIGNAL_KEYS = (
    "best_score",
    "news_sentiment",
    "llm_sentiment_score",
    "insider_data",
    "mtf_alignment",
)


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _format_score(score) -> str:
    s = _safe_float(score)
    if s is None:
        return "?"
    return f"{s:.0f}"


def _count_lit_signals(pick: Mapping) -> int:
    """Subset of confluence_count from position_sizing — counts
    POSITIVE signals lit on this pick. 0..5."""
    if not isinstance(pick, Mapping):
        return 0
    count = 0
    score = _safe_float(pick.get("best_score"))
    if score is not None and score >= 100:
        count += 1
    if (pick.get("news_sentiment") or "").lower() == "bullish":
        count += 1
    llm = _safe_float(pick.get("llm_sentiment_score"))
    if llm is not None and llm >= 0.5:
        count += 1
    insider = pick.get("insider_data")
    if isinstance(insider, Mapping) and insider.get("has_cluster_buy"):
        count += 1
    if pick.get("mtf_alignment") == "bullish":
        count += 1
    return count


def build_entry_rationale(pick: Optional[Mapping],
                            *,
                            sizing_info: Optional[Mapping] = None,
                            regime: Optional[str] = None,
                            sector_returns: Optional[Mapping] = None,
                            ) -> dict:
    """Return a structured rationale dict to embed into the trade
    journal entry.

    Args:
      pick: the screener pick dict (best_score, daily_change,
        sector, news_sentiment, etc.).
      sizing_info: optional `compute_full_size` result dict
        (kelly_multiplier, correlation_multiplier, ...).
      regime: optional 5-tier regime label ("bull-strong" / etc).
      sector_returns: optional ``{sector_name: pct_return}`` so
        we can record whether the pick deployed into a strong
        or weak sector.

    Returns:
      {
        "score": float | None,
        "rs_score": float | None,
        "sector": str,
        "sector_strength": str,        # strong/neutral/weak/unknown
        "news_sentiment": str,
        "vwap_offset_pct": float | None,
        "atr_pct": float | None,
        "regime": str,
        "kelly_mult": float | None,
        "correlation_mult": float | None,
        "drawdown_mult": float | None,
        "adv_mult": float | None,
        "confluence_count": int,
        "filter_reasons": list,        # any chips that fired but
                                       # didn't BLOCK the deploy
        "headline": str,               # human one-liner
      }
    """
    if not isinstance(pick, Mapping):
        pick = {}
    sizing_info = sizing_info if isinstance(sizing_info, Mapping) else {}

    score = _safe_float(pick.get("best_score"))
    rs = _safe_float(pick.get("rs_score") or pick.get("relative_strength"))
    sector = pick.get("sector") or "Unknown"
    sector_str = "unknown"
    if isinstance(sector_returns, Mapping) and sector in sector_returns:
        ret = _safe_float(sector_returns[sector])
        if ret is not None:
            if ret >= 5:
                sector_str = "strong"
            elif ret <= -5:
                sector_str = "weak"
            else:
                sector_str = "neutral"
    news = (pick.get("news_sentiment") or "neutral").lower()
    vwap_offset = _safe_float(pick.get("vwap_offset_pct"))
    atr_pct = _safe_float(pick.get("atr_pct"))

    confluence = _count_lit_signals(pick)

    # Pull multipliers from sizing_info if present.
    kelly_mult = _safe_float(sizing_info.get("kelly_multiplier"))
    corr_mult = _safe_float(sizing_info.get("correlation_multiplier"))
    dd_mult = _safe_float(sizing_info.get("drawdown_multiplier"))
    adv_mult = _safe_float(sizing_info.get("adv_multiplier"))

    # filter_reasons that fired but didn't gate the deploy (e.g.
    # gap_penalty_applied is informational, not blocking).
    fr = pick.get("filter_reasons")
    informational = []
    if isinstance(fr, list):
        for r in fr:
            informational.append(str(r))

    headline = _build_headline(
        score=score, rs=rs, sector=sector,
        sector_strength=sector_str, news=news,
        vwap=vwap_offset, confluence=confluence,
        kelly_mult=kelly_mult,
    )

    return {
        "score": score,
        "rs_score": rs,
        "sector": sector,
        "sector_strength": sector_str,
        "news_sentiment": news,
        "vwap_offset_pct": vwap_offset,
        "atr_pct": atr_pct,
        "regime": regime or "unknown",
        "kelly_mult": kelly_mult,
        "correlation_mult": corr_mult,
        "drawdown_mult": dd_mult,
        "adv_mult": adv_mult,
        "confluence_count": confluence,
        "filter_reasons": informational,
        "headline": headline,
    }


def _build_headline(*, score, rs, sector, sector_strength, news,
                      vwap, confluence, kelly_mult) -> str:
    parts = [f"score={_format_score(score)}"]
    if rs is not None:
        parts.append(f"RS={rs:+.0f}")
    if sector and sector != "Unknown":
        if sector_strength != "unknown":
            parts.append(f"sector={sector}({sector_strength})")
        else:
            parts.append(f"sector={sector}")
    if news != "neutral":
        parts.append(f"news={news}")
    if vwap is not None:
        parts.append(f"vwap={vwap:+.1f}%")
    if kelly_mult is not None and kelly_mult != 1.0:
        parts.append(f"kelly={kelly_mult:.2f}x")
    parts.append(f"conf={confluence}/5")
    return " ".join(parts)


def format_rationale(rationale: Mapping) -> str:
    """Single-line human-readable string for log entries / emails.
    Identical to the `headline` field but accepts a None / empty
    rationale gracefully."""
    if not isinstance(rationale, Mapping):
        return "(no rationale)"
    headline = rationale.get("headline")
    if headline:
        return str(headline)
    return "(no rationale)"


def aggregate_winners_vs_losers(journal: Optional[Mapping]) -> dict:
    """Walk closed trades; bucket by win/loss and report mean
    rationale fields per bucket. Useful for the future
    meta-learning loop and the analytics hub.

    Returns:
      {
        "winners": {count, mean_score, mean_rs, mean_confluence,
                     mean_kelly, ...},
        "losers": {...same shape},
        "delta": {score: winner-loser, ...}  # signed differences
      }
    """
    out = {
        "winners": {"count": 0, "mean_score": 0.0, "mean_rs": 0.0,
                     "mean_confluence": 0.0, "mean_kelly": 0.0},
        "losers": {"count": 0, "mean_score": 0.0, "mean_rs": 0.0,
                    "mean_confluence": 0.0, "mean_kelly": 0.0},
        "delta": {},
    }
    if not isinstance(journal, Mapping):
        return out
    trades = journal.get("trades") or []
    if not isinstance(trades, list):
        return out
    bucket_w = {"score": [], "rs": [], "conf": [], "kelly": []}
    bucket_l = {"score": [], "rs": [], "conf": [], "kelly": []}
    for t in trades:
        if not isinstance(t, Mapping):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        rat = t.get("entry_rationale")
        if not isinstance(rat, Mapping):
            continue
        try:
            pnl = float(t.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        bucket = bucket_w if pnl > 0 else bucket_l
        s = _safe_float(rat.get("score"))
        if s is not None:
            bucket["score"].append(s)
        rs = _safe_float(rat.get("rs_score"))
        if rs is not None:
            bucket["rs"].append(rs)
        c = _safe_float(rat.get("confluence_count"))
        if c is not None:
            bucket["conf"].append(c)
        k = _safe_float(rat.get("kelly_mult"))
        if k is not None:
            bucket["kelly"].append(k)

    def _mean(xs):
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    out["winners"] = {
        "count": len(bucket_w["score"]),
        "mean_score": _mean(bucket_w["score"]),
        "mean_rs": _mean(bucket_w["rs"]),
        "mean_confluence": _mean(bucket_w["conf"]),
        "mean_kelly": _mean(bucket_w["kelly"]),
    }
    out["losers"] = {
        "count": len(bucket_l["score"]),
        "mean_score": _mean(bucket_l["score"]),
        "mean_rs": _mean(bucket_l["rs"]),
        "mean_confluence": _mean(bucket_l["conf"]),
        "mean_kelly": _mean(bucket_l["kelly"]),
    }
    out["delta"] = {
        "score": round(out["winners"]["mean_score"]
                          - out["losers"]["mean_score"], 3),
        "rs": round(out["winners"]["mean_rs"]
                       - out["losers"]["mean_rs"], 3),
        "confluence": round(out["winners"]["mean_confluence"]
                                - out["losers"]["mean_confluence"], 3),
        "kelly": round(out["winners"]["mean_kelly"]
                          - out["losers"]["mean_kelly"], 3),
    }
    return out
