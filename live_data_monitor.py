"""Round-61 pt.63 — live-data divergence monitor.

Detects when the bot's last-seen price for a symbol diverges
materially from Alpaca's most-recent latest_trade. Catches:

  * **Stale quotes** — Alpaca's data feed lagged or returned a
    cached value while the trading endpoint moved.
  * **Halted stocks** — bot has a price from minutes ago, real
    trade is unknown.
  * **Bad fixtures** — yfinance / cached snapshot returned a
    split-adjusted or split-unadjusted value vs. live.
  * **Manual intervention** — user closed/changed something
    outside the bot.

Pure module — caller passes both prices in. Exposes:

  * ``compute_divergence_pct(bot_price, live_price)`` — raw delta
    in percent.
  * ``classify_divergence(bot_price, live_price, *, threshold_pct=2.0)``
    returns ``{"diverged": bool, "delta_pct": float|None,
    "severity": "ok"|"warn"|"alert"}``.
  * ``check_position_divergence(positions, latest_trade_fn, *,
    threshold_pct=2.0)`` — the full sweep used by the monitor
    cycle.

Severity tiers:
  * ``ok``     — |Δ| < threshold (default 2%)
  * ``warn``   — threshold ≤ |Δ| < 2 × threshold (4% default)
  * ``alert``  — |Δ| ≥ 2 × threshold (push notification fires)
"""
from __future__ import annotations

from typing import Optional, Callable, Iterable, Mapping


DEFAULT_DIVERGENCE_THRESHOLD_PCT: float = 2.0


def compute_divergence_pct(bot_price, live_price) -> Optional[float]:
    """Return ``(live - bot) / bot * 100`` in percent. None on bad
    inputs (zero or negative bot_price, non-numeric).
    """
    try:
        bp = float(bot_price)
        lp = float(live_price)
    except (TypeError, ValueError):
        return None
    if bp <= 0:
        return None
    return (lp - bp) / bp * 100


def classify_divergence(bot_price, live_price,
                          *,
                          threshold_pct: float = DEFAULT_DIVERGENCE_THRESHOLD_PCT,
                          ) -> dict:
    """Classify the divergence into ok / warn / alert tiers.

    Returns:
      {
        "diverged": bool,            # True if abs(delta) >= threshold
        "delta_pct": float|None,     # signed (positive = live higher)
        "severity": str,             # "ok" / "warn" / "alert"
        "bot_price": float|None,
        "live_price": float|None,
      }
    """
    delta = compute_divergence_pct(bot_price, live_price)
    out = {
        "diverged": False,
        "delta_pct": delta,
        "severity": "ok",
        "bot_price": _safe_float(bot_price),
        "live_price": _safe_float(live_price),
    }
    if delta is None:
        return out
    abs_delta = abs(delta)
    if abs_delta >= 2 * threshold_pct:
        out["severity"] = "alert"
        out["diverged"] = True
    elif abs_delta >= threshold_pct:
        out["severity"] = "warn"
        out["diverged"] = True
    else:
        out["severity"] = "ok"
    return out


def _safe_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_position_divergence(positions: Iterable[Mapping],
                                latest_trade_fn: Callable[[str], Optional[float]],
                                *,
                                threshold_pct: float = DEFAULT_DIVERGENCE_THRESHOLD_PCT,
                                ) -> dict:
    """Sweep positions, comparing each position's current_price (the
    last value the bot's snapshot saw) against the latest_trade_fn
    result for that symbol.

    Args:
      positions: iterable of position dicts. Expected fields:
        ``symbol``, ``current_price`` (or fallback to
        ``avg_entry_price``).
      latest_trade_fn: callable(symbol_str) → latest trade price
        from Alpaca data API. None on lookup failure.
      threshold_pct: divergence threshold in percent.

    Returns:
      {
        "checked": int,                    # positions inspected
        "divergent": int,                  # positions tripping threshold
        "alerts": list[divergence_dict],   # severity == "alert"
        "warnings": list[divergence_dict], # severity == "warn"
        "errors": int,                     # latest_trade_fn failures
      }

    Best-effort — any per-symbol exception is swallowed and counted
    as an error. The aggregate result is suitable for both a
    dashboard panel + an alert hook.
    """
    out = {
        "checked": 0, "divergent": 0,
        "alerts": [], "warnings": [], "errors": 0,
    }
    for pos in positions or []:
        if not isinstance(pos, Mapping):
            continue
        symbol = (pos.get("symbol") or "").upper()
        if not symbol:
            continue
        bot_price = pos.get("current_price")
        if bot_price is None:
            bot_price = pos.get("avg_entry_price")
        try:
            live = latest_trade_fn(symbol)
        except Exception:
            out["errors"] += 1
            continue
        if live is None:
            out["errors"] += 1
            continue
        result = classify_divergence(
            bot_price, live, threshold_pct=threshold_pct)
        result["symbol"] = symbol
        out["checked"] += 1
        if result["severity"] == "alert":
            out["divergent"] += 1
            out["alerts"].append(result)
        elif result["severity"] == "warn":
            out["divergent"] += 1
            out["warnings"].append(result)
    return out
