"""Round-61 pt.72 — live-mode promotion gate.

Paper validation runs from 2026-04-15 → ~2026-05-15. Right now
flipping the live-mode toggle in the dashboard is a manual eyeball
click — nothing prevents enabling it during a drawdown or before
enough trades have closed to validate edge.

This module exposes ``check_live_mode_readiness(journal,
account, audit_findings)`` that returns ``{ready, blockers,
warnings, summary}``. Callers (the dashboard's mode-toggle
handler) check ``ready`` before allowing the flip.

Pure module — caller passes already-loaded data. No I/O.

Default gates (all must pass):
  * Min closed trades (default 30) — enough for win-rate to
    stabilize statistically
  * Min win rate (default 45%) — bot must show edge
  * Min Sharpe (default 0.5) — risk-adjusted return must be
    positive (positive return per unit of volatility)
  * Max drawdown (default 15%) — not currently in a meltdown
  * No HIGH-severity audit findings — state is clean

Use:
    >>> from live_mode_gate import check_live_mode_readiness
    >>> result = check_live_mode_readiness(journal, account, audit)
    >>> if not result["ready"]:
    ...     return {"error": "Cannot enable live: " + result["summary"]}
"""
from __future__ import annotations

from typing import Mapping, Optional


# Default thresholds. Conservative — better to delay live trading
# than enable it too early.
DEFAULT_MIN_CLOSED_TRADES: int = 30
DEFAULT_MIN_WIN_RATE: float = 0.45
DEFAULT_MIN_SHARPE: float = 0.5
DEFAULT_MAX_DRAWDOWN_PCT: float = 15.0


def _count_closed_trades(journal: Optional[Mapping]) -> int:
    if not isinstance(journal, Mapping):
        return 0
    trades = journal.get("trades") or []
    if not isinstance(trades, list):
        return 0
    n = 0
    for t in trades:
        if not isinstance(t, Mapping):
            continue
        if (t.get("status") or "open").lower() == "closed":
            n += 1
    return n


def _compute_win_rate(journal: Optional[Mapping]) -> float:
    if not isinstance(journal, Mapping):
        return 0.0
    trades = journal.get("trades") or []
    if not isinstance(trades, list):
        return 0.0
    wins = 0
    closed = 0
    for t in trades:
        if not isinstance(t, Mapping):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        closed += 1
        try:
            pnl = float(t.get("pnl") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        if pnl > 0:
            wins += 1
    if closed == 0:
        return 0.0
    return wins / closed


def _max_drawdown_pct(journal: Optional[Mapping]) -> float:
    """Walk all closed trades chronologically, track the peak-to-
    trough cumulative P&L drawdown as a percentage of peak."""
    if not isinstance(journal, Mapping):
        return 0.0
    trades = journal.get("trades") or []
    if not isinstance(trades, list):
        return 0.0
    rows = []
    for t in trades:
        if not isinstance(t, Mapping):
            continue
        if (t.get("status") or "open").lower() != "closed":
            continue
        try:
            pnl = float(t.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        rows.append((t.get("exit_timestamp") or "", pnl))
    if not rows:
        return 0.0
    rows.sort(key=lambda r: r[0])
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for _ts, pnl in rows:
        cum += pnl
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 2)


def _count_high_audit_findings(audit_findings) -> int:
    if not audit_findings:
        return 0
    if isinstance(audit_findings, Mapping):
        findings = audit_findings.get("findings") or []
    else:
        findings = audit_findings
    if not isinstance(findings, list):
        return 0
    n = 0
    for f in findings:
        if isinstance(f, Mapping):
            sev = (f.get("severity") or "").upper()
            if sev == "HIGH":
                n += 1
    return n


def check_live_mode_readiness(
        journal: Optional[Mapping],
        scorecard: Optional[Mapping] = None,
        audit_findings=None,
        *,
        min_closed_trades: int = DEFAULT_MIN_CLOSED_TRADES,
        min_win_rate: float = DEFAULT_MIN_WIN_RATE,
        min_sharpe: float = DEFAULT_MIN_SHARPE,
        max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
        ) -> dict:
    """Return ``{ready, blockers, warnings, summary, metrics}``.

    Args:
      journal: trade_journal.json contents.
      scorecard: scorecard.json contents (for sharpe_ratio).
      audit_findings: list of {severity, message} dicts (HIGH = blocker).
      min_*: tunable thresholds.

    Each blocker is a dict ``{"key": str, "message": str,
    "actual": ..., "required": ...}``. ``warnings`` is the same
    shape but doesn't gate.
    """
    blockers = []
    warnings = []
    closed = _count_closed_trades(journal)
    win_rate = _compute_win_rate(journal)
    drawdown = _max_drawdown_pct(journal)
    sharpe = 0.0
    if isinstance(scorecard, Mapping):
        try:
            sharpe = float(scorecard.get("sharpe_ratio") or 0)
        except (TypeError, ValueError):
            sharpe = 0.0
    high_findings = _count_high_audit_findings(audit_findings)

    if closed < min_closed_trades:
        blockers.append({
            "key": "insufficient_trades",
            "message": (f"Need ≥{min_closed_trades} closed trades, "
                          f"have {closed}"),
            "actual": closed, "required": min_closed_trades,
        })
    if win_rate < min_win_rate:
        blockers.append({
            "key": "low_win_rate",
            "message": (f"Win rate {win_rate:.0%} < required "
                          f"{min_win_rate:.0%}"),
            "actual": round(win_rate, 4),
            "required": min_win_rate,
        })
    if sharpe < min_sharpe:
        blockers.append({
            "key": "low_sharpe",
            "message": (f"Sharpe {sharpe:.2f} < required "
                          f"{min_sharpe:.2f}"),
            "actual": round(sharpe, 4),
            "required": min_sharpe,
        })
    if drawdown > max_drawdown_pct:
        blockers.append({
            "key": "high_drawdown",
            "message": (f"Max drawdown {drawdown:.1f}% > "
                          f"allowed {max_drawdown_pct:.1f}%"),
            "actual": drawdown, "required": max_drawdown_pct,
        })
    if high_findings > 0:
        blockers.append({
            "key": "high_audit_findings",
            "message": (f"{high_findings} HIGH-severity audit "
                          f"finding(s) — resolve before going live"),
            "actual": high_findings, "required": 0,
        })

    # Soft warnings — don't block but flag.
    if closed >= min_closed_trades and closed < min_closed_trades * 1.5:
        warnings.append({
            "key": "borderline_sample",
            "message": (f"Sample size {closed} just over minimum — "
                          "consider waiting for more data"),
        })
    if min_win_rate <= win_rate < min_win_rate + 0.05:
        warnings.append({
            "key": "borderline_win_rate",
            "message": (f"Win rate {win_rate:.0%} just over the "
                          f"{min_win_rate:.0%} bar"),
        })

    metrics = {
        "closed_trades": closed,
        "win_rate": round(win_rate, 4),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown_pct": drawdown,
        "high_audit_findings": high_findings,
    }
    if blockers:
        summary = (f"NOT READY for live trading: "
                    f"{len(blockers)} blocker(s) — "
                    + "; ".join(b["message"] for b in blockers))
    else:
        summary = (f"READY for live trading. {closed} trades, "
                    f"win {win_rate:.0%}, sharpe {sharpe:.2f}, "
                    f"max DD {drawdown:.1f}%.")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "summary": summary,
        "metrics": metrics,
    }
