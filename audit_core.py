"""Round-61 pt.21: state-consistency audit.

Pure helper module — given a snapshot of positions, orders, strategy
files, and the trade journal, produces a structured audit report
flagging every inconsistency between them. Stays pure so it can be
unit-tested without the HTTP stack.

Checks performed:

  1. **Orphan positions** — a position in Alpaca with no matching
     strategy file on disk + no matching open journal entry. User
     sees "MANUAL" in the dashboard for these. Auto-adoption should
     pick them up every 10 min; if one lingers, something is wrong.

  2. **Ghost strategy files** — an ACTIVE strategy file on disk with
     no matching Alpaca position. Means the position closed but the
     file wasn't cleaned up. Monitor will try to manage a non-existent
     position.

  3. **Legacy OCC mis-routing** — `short_sell_<OCC>.json` files still
     on disk. pt.17 + pt.21 should have migrated these to wheel files.
     If any remain, the retrofit didn't run.

  4. **Stop-order coverage** — every non-wheel long position must have
     a SELL stop; every equity short must have a BUY stop. Missing
     stops = unprotected downside.

  5. **Stop-price sanity** — for LONGS, the stop must be BELOW current
     price (sell-stops above market trigger immediately). For SHORTS,
     the stop must be ABOVE current price (buy-stops below market are
     rejected by Alpaca). pt.17/19 fixed the initial placement logic,
     but stale stops from pre-pt.17 may still be in an invalid
     position relative to current price.

  6. **Strategy-name drift** — every journal entry's strategy field
     and every strategy file's prefix must be a known name. Unknown
     names fall through STRATEGY_BUCKETS and silently vanish from
     the scorecard.

  7. **Scorecard freshness** — scorecard.last_updated older than 48h
     means daily_close hasn't run. Performance attribution is stale.

Each issue gets a severity: HIGH (money at risk — missing stop,
invalid stop), MEDIUM (data integrity — mis-routed file, stale
scorecard), LOW (hygiene — ghost file). Report groups by severity.
"""
from __future__ import annotations

import os
import re
from typing import Iterable


_OCC_RE = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")


def _is_occ(sym) -> bool:
    return bool(sym and _OCC_RE.match(str(sym)))


def _occ_underlying(sym):
    m = _OCC_RE.match(str(sym or ""))
    return m.group(1) if m else None


def _parse_strategy_filename(fname: str):
    """Reverse of `{prefix}_{SYMBOL}.json`. Returns (prefix, symbol)
    or (None, None) if unparseable.

    Round-61 pt.22: also handles the indexed wheel pattern
    `wheel_<UNDERLYING>__<CONTRACT_SUFFIX>.json` for multi-contract
    wheel positions. In that case the returned symbol is the
    underlying (anything before the double-underscore), not the
    contract suffix — callers want to look up by underlying.
    """
    if not fname.endswith(".json"):
        return None, None
    stem = fname[:-5]
    if "_" not in stem:
        return None, None
    # Strategy names can contain underscores (e.g. short_sell,
    # mean_reversion), so we try longest-match against known prefixes
    # rather than a naive rpartition. Falls back to rpartition for
    # anything we don't recognise.
    try:
        from constants import STRATEGY_FILE_PREFIXES
        prefixes = sorted(STRATEGY_FILE_PREFIXES, key=len, reverse=True)
    except ImportError:
        prefixes = ("trailing_stop", "mean_reversion", "short_sell",
                    "copy_trading", "breakout", "wheel", "pead")
    for p in prefixes:
        if stem.startswith(p + "_"):
            remainder = stem[len(p) + 1:]
            # pt.22: handle `wheel_HIMS__<contract>` → symbol="HIMS".
            if "__" in remainder:
                remainder = remainder.split("__", 1)[0]
            return p, remainder
    # Fallback: first-underscore partition.
    prefix, _, sym = stem.partition("_")
    return prefix, sym


def run_audit(positions: Iterable[dict],
              orders: Iterable[dict],
              strategy_files: dict,  # {fname: loaded_json_dict}
              journal: dict,         # {trades: [...], ...}
              scorecard: dict):
    """Produce an audit report. Each finding is a dict with:
      severity: 'HIGH' | 'MEDIUM' | 'LOW'
      category: short category string
      message:  human-readable sentence
      symbol:   optional symbol involved
    """
    findings = []
    positions = list(positions or [])
    orders = list(orders or [])
    strategy_files = strategy_files or {}
    journal = journal or {}
    scorecard = scorecard or {}

    # Build lookup helpers
    active_strategy_by_sym = {}  # symbol (possibly underlying) -> (fname, data)
    legacy_occ_files = []
    try:
        from constants import is_closed_status
    except ImportError:
        is_closed_status = lambda s: str(s or "").strip().lower() in {
            "closed", "stopped", "cancelled", "canceled",
            "exited", "filled_and_closed"}

    for fname, data in strategy_files.items():
        data = data or {}
        status = data.get("status") or ""
        prefix, sym = _parse_strategy_filename(fname)
        if prefix is None:
            continue
        # Flag legacy OCC mis-routing — pt.21 migration retires these.
        if prefix == "short_sell" and _is_occ(sym):
            if not is_closed_status(status) and str(status).lower() != "migrated":
                legacy_occ_files.append(fname)
        # Only non-closed/non-migrated files count as "active" for the map.
        if is_closed_status(status) or str(status).lower() == "migrated":
            continue
        key_sym = (data.get("symbol") or sym or "").upper()
        if key_sym:
            active_strategy_by_sym[key_sym] = (fname, data)

    # Journal-open entries keyed by (symbol, underlying).
    journal_open_symbols = set()
    try:
        from constants import STRATEGY_NAMES
    except ImportError:
        STRATEGY_NAMES = frozenset({"trailing_stop", "breakout",
                                     "mean_reversion", "wheel",
                                     "short_sell", "pead", "copy_trading"})
    unknown_strategies_in_journal = set()
    for t in (journal.get("trades") or []):
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "open") != "open":
            continue
        sym = (t.get("symbol") or "").upper()
        if sym:
            journal_open_symbols.add(sym)
            if _is_occ(sym):
                underlying = _occ_underlying(sym)
                if underlying:
                    journal_open_symbols.add(underlying)
        strat_name = str(t.get("strategy") or "").strip().lower()
        if strat_name and strat_name not in STRATEGY_NAMES:
            unknown_strategies_in_journal.add(strat_name)

    # Build orders-by-symbol map.
    orders_by_sym = {}
    for o in orders:
        if not isinstance(o, dict):
            continue
        osym = (o.get("symbol") or "").upper()
        orders_by_sym.setdefault(osym, []).append(o)

    # ---------- Check 1: orphan positions ----------
    for p in positions:
        if not isinstance(p, dict):
            continue
        sym = (p.get("symbol") or "").upper()
        if not sym:
            continue
        asset_class = (p.get("asset_class") or "").lower()
        lookup = sym
        if asset_class == "us_option":
            lookup = _occ_underlying(sym) or sym
        in_strategy_map = lookup in active_strategy_by_sym or sym in active_strategy_by_sym
        in_journal = lookup in journal_open_symbols or sym in journal_open_symbols
        if not in_strategy_map and not in_journal:
            findings.append({
                "severity": "HIGH",
                "category": "orphan_position",
                "message": (f"Position {sym} has no active strategy file and "
                            "no matching open journal entry. Dashboard will "
                            "show MANUAL. Next orphan-adoption tick "
                            "(within 10 min) should claim it."),
                "symbol": sym,
            })

    # ---------- Check 2: legacy OCC mis-routing ----------
    for fname in legacy_occ_files:
        findings.append({
            "severity": "MEDIUM",
            "category": "legacy_occ_mis_routed",
            "message": (f"Legacy file {fname} is still on disk. pt.21 "
                        "migration should have retired it. If this "
                        "appears after a deploy, run the migration "
                        "step manually or check error_recovery logs."),
            "symbol": fname,
        })

    # ---------- Check 3: ghost strategy files ----------
    position_symbols = set()
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        if not sym:
            continue
        position_symbols.add(sym)
        if (p.get("asset_class") or "").lower() == "us_option":
            u = _occ_underlying(sym)
            if u:
                position_symbols.add(u)
    for sym, (fname, _data) in active_strategy_by_sym.items():
        if sym not in position_symbols:
            findings.append({
                "severity": "LOW",
                "category": "ghost_strategy_file",
                "message": (f"Active strategy file {fname} references "
                            f"{sym}, but no matching Alpaca position. "
                            "Position closed without the file being "
                            "marked closed."),
                "symbol": sym,
            })

    # ---------- Check 4 + 5: stop coverage + sanity ----------
    for p in positions:
        if not isinstance(p, dict):
            continue
        sym = (p.get("symbol") or "").upper()
        asset_class = (p.get("asset_class") or "").lower()
        # Wheel positions intentionally don't have equity-style stops.
        # Skip if a wheel file owns the underlying.
        underlying = _occ_underlying(sym) if asset_class == "us_option" else sym
        wheel_owned = False
        if underlying:
            wheel_tuple = active_strategy_by_sym.get(underlying)
            if wheel_tuple and wheel_tuple[1].get("strategy") == "wheel":
                wheel_owned = True
        if wheel_owned:
            continue
        try:
            qty = float(p.get("qty") or 0)
            current = float(p.get("current_price") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0 or current == 0:
            continue
        is_short = qty < 0
        expected_side = "buy" if is_short else "sell"
        sym_orders = orders_by_sym.get(sym, [])
        stop_orders = [o for o in sym_orders
                       if (o.get("type") or "").lower() in ("stop", "stop_limit", "trailing_stop")
                       and (o.get("side") or "").lower() == expected_side]
        if not stop_orders:
            findings.append({
                "severity": "HIGH",
                "category": "missing_stop",
                "message": (f"Position {sym} (qty {int(qty)}) has NO "
                            f"{expected_side.upper()} stop at Alpaca. "
                            "Unprotected downside — next monitor tick "
                            "should place one."),
                "symbol": sym,
            })
        else:
            for o in stop_orders:
                try:
                    stop_price = float(o.get("stop_price") or 0)
                except (TypeError, ValueError):
                    continue
                if stop_price == 0:
                    continue
                if is_short and stop_price <= current:
                    findings.append({
                        "severity": "HIGH",
                        "category": "invalid_stop_price",
                        "message": (f"{sym} short cover-stop is ${stop_price:.2f} "
                                    f"but current price is ${current:.2f}. "
                                    "Stop must be ABOVE current for a buy-"
                                    "stop to protect against adverse moves."),
                        "symbol": sym,
                    })
                elif (not is_short) and stop_price >= current:
                    findings.append({
                        "severity": "HIGH",
                        "category": "invalid_stop_price",
                        "message": (f"{sym} long sell-stop is ${stop_price:.2f} "
                                    f"but current price is ${current:.2f}. "
                                    "Stop would trigger immediately."),
                        "symbol": sym,
                    })

    # ---------- Check 6: unknown strategy names in journal ----------
    for strat_name in sorted(unknown_strategies_in_journal):
        findings.append({
            "severity": "MEDIUM",
            "category": "unknown_strategy_name",
            "message": (f"Journal contains trades with strategy='{strat_name}' "
                        "which isn't in constants.STRATEGY_NAMES. Those trades "
                        "won't appear in the scorecard performance attribution."),
            "symbol": None,
        })

    # ---------- Check 7: scorecard freshness ----------
    # Round-61 pt.50: "fresh" is measured in TRADING DAYS, not wall
    # clock hours. A 64-hour gap that spans Sat + Sun is normal — the
    # daily_close task only runs Mon-Fri. The audit must NOT flag a
    # weekend gap as a failure. Only fire when ≥ 2 trading days have
    # passed without an update.
    last = scorecard.get("last_updated")
    if last:
        try:
            from datetime import datetime, timedelta
            try:
                ts = datetime.fromisoformat(last)
            except ValueError:
                ts = None
            if ts:
                try:
                    from et_time import now_et
                    now = now_et()
                except ImportError:
                    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
                delta = now - ts
                hours = int(delta.total_seconds() / 3600)
                missed_closes = _trading_closes_between(ts, now)
                # Flag only if ≥2 daily_close runs were expected to
                # have fired since the last update. One missed close
                # could be a hiccup or a half-day; two means the task
                # really hasn't run.
                if missed_closes >= 2:
                    findings.append({
                        "severity": "MEDIUM",
                        "category": "stale_scorecard",
                        "message": (
                            f"Scorecard last updated {hours}h ago "
                            f"({missed_closes} expected daily_close "
                            f"runs missed). Trigger via Settings -> "
                            f"Force Daily Close."),
                        "symbol": None,
                    })
        except Exception:
            pass

    # Summary counts
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        counts[f.get("severity", "LOW")] = counts.get(f.get("severity", "LOW"), 0) + 1
    return {
        "findings": findings,
        "counts": counts,
        "clean": not findings,
    }


# ============================================================================
# Round-61 pt.50: trading-day awareness for the scorecard freshness check
# ============================================================================

# US equity market closures for 2026/2027. NYSE official holiday calendar.
# Source: https://www.nyse.com/markets/hours-calendars
_US_MARKET_HOLIDAYS = frozenset({
    # 2026
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
    # 2027
    "2027-01-01",
    "2027-01-18",
    "2027-02-15",
    "2027-03-26",  # Good Friday
    "2027-05-31",
    "2027-06-18",  # Juneteenth (observed)
    "2027-07-05",  # Independence Day (observed)
    "2027-09-06",
    "2027-11-25",
    "2027-12-24",  # Christmas (observed)
})


def _is_trading_day(d) -> bool:
    """True if `d` is a US equity-market trading day (Mon-Fri AND not
    a NYSE holiday). Accepts ``date`` or ``datetime``."""
    try:
        from datetime import date, datetime
    except ImportError:
        return True
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        return True
    if d.weekday() >= 5:
        return False
    if d.isoformat() in _US_MARKET_HOLIDAYS:
        return False
    return True


def _trading_closes_between(start, end, *, close_hour: int = 16,
                              close_minute: int = 5) -> int:
    """Number of expected daily_close runs that should have fired
    in the (start, end] window. daily_close fires at 4:05 PM ET on
    each trading day.

    A daily_close at trading day D contributes to the count if:
      * D is a trading day (weekday + not a holiday)
      * The close timestamp `D 16:05` falls strictly AFTER `start`
        and AT-OR-BEFORE `end`.

    Returns 0 if start >= end.
    """
    from datetime import datetime, time, timedelta
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return 0
    if start >= end:
        return 0
    # Strip tzinfo for the date math — both should be in the same
    # zone (ET); if they differ in tz, fall back to naive
    # comparison via the .replace(tzinfo=None) below.
    start_naive = start.replace(tzinfo=None) if start.tzinfo else start
    end_naive = end.replace(tzinfo=None) if end.tzinfo else end
    count = 0
    cur = start_naive.date()
    last = end_naive.date()
    while cur <= last:
        if _is_trading_day(cur):
            close_dt = datetime.combine(
                cur, time(close_hour, close_minute))
            if close_dt > start_naive and close_dt <= end_naive:
                count += 1
        cur += timedelta(days=1)
    return count


def load_strategy_files(strategies_dir: str) -> dict:
    """Convenience helper — read every *.json in STRATEGIES_DIR into a
    dict keyed by filename. Silent on read errors (treats as missing)."""
    import json
    result = {}
    if not strategies_dir or not os.path.isdir(strategies_dir):
        return result
    try:
        for fname in os.listdir(strategies_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(strategies_dir, fname)) as f:
                    result[fname] = json.load(f)
            except (OSError, ValueError):
                result[fname] = None
    except OSError:
        pass
    return result
