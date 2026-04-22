#!/usr/bin/env python3
"""
Scorecard Updater — Reads trade journal, Alpaca account data, and positions,
then calculates all performance metrics and updates scorecard.json.

Also takes a daily snapshot and appends it to trade_journal.json.

Run: python3 "/Users/kevinbell/Alpaca Trading/update_scorecard.py"
"""

import json
import math
import os
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from et_time import now_et


# Phase 2 of the float->Decimal migration (see docs/DECIMAL_MIGRATION_PLAN.md).
# Money aggregates (profit factor sums, strategy breakdown totals, peak/current
# portfolio value, largest win/loss) now run in Decimal. Ratios and percentages
# (sharpe, sortino, return%, win_rate%) stay as float — they're proportional,
# not money, and Decimal's lack of native exp/sqrt makes it a poor fit for
# statistical formulas. Return-dict types are unchanged: every consumer still
# sees float values on the JSON boundary.
_CENT = Decimal("0.01")


def _dec(v, default=Decimal("0")):
    """Coerce to Decimal WITHOUT going through float.

    Decimal(float_x) preserves IEEE 754 imprecision into the Decimal
    value; Decimal(str(x)) gets the human-readable form. Always use str.
    """
    if v is None or v == "":
        return default
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return default


def _to_cents_float(v):
    """Quantize a Decimal to cents (banker's rounding) and emit a float
    for JSON serialisation. The rounded float representation carries at
    most 2 dp of meaningful precision so the IEEE 754 boundary crossing
    is bounded well below $0.01."""
    if not isinstance(v, Decimal):
        v = _dec(v)
    return float(v.quantize(_CENT, rounding=ROUND_HALF_EVEN))


def load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())
load_dotenv()


def safe_save_json(path, data):
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp_path, path)
    except Exception:
        # Narrow from bare except so KeyboardInterrupt / SystemExit
        # propagate instead of hitting the cleanup+re-raise branch.
        try: os.unlink(tmp_path)
        except OSError: pass
        raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR is where persistent runtime data lives. On Railway, set to a volume mount
# path (e.g. /data). Locally defaults to BASE_DIR so nothing changes.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

# Round-9 fix: when cloud_scheduler.run_daily_close spawns this script
# as a subprocess, it passes per-user paths via env vars so scorecard +
# trade_journal are written to the user's own /data/users/{id}/ dir,
# not the shared /data/ legacy paths. Before this fix, daily close
# wrote to /data/scorecard.json while the dashboard read from
# /data/users/1/scorecard.json — the two files drifted (observed today:
# shared had last_updated=17:16 current_value=$100,332 while per-user
# had 16:05 $100,378). Env vars win; shared paths are the fallback for
# env-mode / dev runs without a user context.
JOURNAL_PATH = os.environ.get("JOURNAL_PATH", os.path.join(DATA_DIR, "trade_journal.json"))
SCORECARD_PATH = os.environ.get("SCORECARD_PATH", os.path.join(DATA_DIR, "scorecard.json"))
STRATEGIES_DIR = os.environ.get("STRATEGIES_DIR", os.path.join(DATA_DIR, "strategies"))

API_ENDPOINT = os.environ.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets/v2")
API_KEY = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# Sector map now lives in constants.py — single source of truth shared
# with update_dashboard.py and cloud_scheduler.py. Previously this was
# a separate copy that could drift out of sync on sector edits.
from constants import SECTOR_MAP


def api_get(url, timeout=15):
    """Make an authenticated GET request to Alpaca API (legacy, no retry)."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def api_get_with_retry(url, max_retries=3, timeout=15):
    """Make an authenticated GET request with retry logic for 429/5xx."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"error": str(e)}


def load_json(path):
    """Load a JSON file, returning None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def is_market_open():
    """Check if market is open using Alpaca /v2/clock endpoint."""
    result = api_get_with_retry(f"{API_ENDPOINT}/clock")
    if isinstance(result, dict) and "error" not in result:
        is_open = result.get("is_open", False)
        next_open = result.get("next_open", "")
        next_close = result.get("next_close", "")
        if is_open:
            return True, f"Market OPEN (closes {next_close})"
        else:
            return False, f"Market CLOSED (opens {next_open})"
    return False, f"Could not determine market hours: {result.get('error', 'unknown')}"


def calculate_metrics(journal, scorecard, account, positions):
    """Calculate all performance metrics from trade journal and account data.

    Phase-2 migration note: money aggregates run in Decimal internally, ratios
    stay as float. Return-dict types unchanged — every caller still sees
    rounded float values on the JSON boundary.
    """
    trades = journal.get("trades", [])
    snapshots = journal.get("daily_snapshots", [])

    # Current account values — Decimal-internal; serialised at output.
    _pv_raw = _dec(account.get("portfolio_value", 0))
    starting_capital_d = _dec(scorecard.get("starting_capital", 100000))
    portfolio_value_d = _pv_raw if _pv_raw != Decimal("0") else starting_capital_d
    cash_d = _dec(account.get("cash", 0))
    starting_capital = _to_cents_float(starting_capital_d)  # kept as float for downstream

    # Count trades
    total_trades = len(trades)
    open_trades = sum(1 for t in trades if t.get("status") == "open")
    closed_trades = sum(1 for t in trades if t.get("status") == "closed")

    # Win/loss analysis on closed trades
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    winning_trades = sum(1 for t in closed if t["pnl"] > 0)
    losing_trades = sum(1 for t in closed if t["pnl"] <= 0)

    win_rate = (winning_trades / closed_trades * 100) if closed_trades > 0 else 0

    # Average win/loss percentages (percentages are proportional, stay float).
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]

    avg_win_pct = (sum(t.get("pnl_pct", 0) for t in wins) / len(wins)) if wins else 0
    avg_loss_pct = (sum(t.get("pnl_pct", 0) for t in losses) / len(losses)) if losses else 0

    # Profit factor — Decimal sum avoids compounding float drift across many
    # closed trades. The ratio itself is dimensionless so stays float.
    total_wins_d = sum((_dec(t["pnl"]) for t in wins), Decimal("0"))
    total_losses_d = abs(sum((_dec(t["pnl"]) for t in losses), Decimal("0")))
    if total_losses_d > 0:
        profit_factor = float(total_wins_d / total_losses_d)
    elif total_wins_d > 0:
        profit_factor = float(total_wins_d)
    else:
        profit_factor = 0

    # Largest win/loss — pick the max Decimal, serialise to cents on output.
    largest_win_d = max((_dec(t["pnl"]) for t in wins), default=Decimal("0"))
    largest_loss_d = min((_dec(t["pnl"]) for t in losses), default=Decimal("0"))

    # Average holding days
    holding_days = []
    for t in closed:
        if t.get("timestamp") and t.get("exit_timestamp"):
            try:
                entry_dt = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
                exit_dt = datetime.fromisoformat(t["exit_timestamp"].replace("Z", "+00:00"))
                holding_days.append((exit_dt - entry_dt).total_seconds() / 86400)
            except (ValueError, TypeError):
                pass
    avg_holding = (sum(holding_days) / len(holding_days)) if holding_days else 0

    # Max drawdown from daily snapshots — peak tracked as Decimal so the
    # compared values stay exact; drawdown % itself is float (proportional).
    peak_d = starting_capital_d
    max_dd = 0.0
    for snap in snapshots:
        val_d = _dec(snap.get("portfolio_value", 0))
        if val_d > peak_d:
            peak_d = val_d
        if peak_d > 0:
            dd = float((peak_d - val_d) / peak_d) * 100
            if dd > max_dd:
                max_dd = dd

    # Also check current value against peak
    peak_value_d = max(peak_d, portfolio_value_d,
                        _dec(scorecard.get("peak_value", starting_capital)))
    if peak_value_d > 0:
        current_dd = float((peak_value_d - portfolio_value_d) / peak_value_d) * 100
        max_dd = max(max_dd, current_dd)

    # Sharpe and Sortino ratios from daily returns
    daily_returns = []
    if len(snapshots) >= 2:
        for i in range(1, len(snapshots)):
            prev_val = snapshots[i - 1].get("portfolio_value", 0)
            curr_val = snapshots[i].get("portfolio_value", 0)
            if prev_val > 0:
                daily_returns.append(curr_val / prev_val - 1)

    n = len(daily_returns)
    sharpe = 0
    sortino = 0
    if n >= 2:
        rf_daily = 0.00016  # ~4% annual risk-free rate / 252
        mean_ret = sum(daily_returns) / n
        variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (n - 1)  # sample variance
        std_ret = math.sqrt(variance) if variance > 0 else 0

        if std_ret > 0:
            sharpe = ((mean_ret - rf_daily) / std_ret) * math.sqrt(252)

        # Sortino: only downside deviation (divide by total N, not len(neg_returns))
        neg_returns = [r for r in daily_returns if r < 0]
        if neg_returns:
            neg_variance = sum(r ** 2 for r in neg_returns) / n  # divide by total N
            neg_std = math.sqrt(neg_variance)
            if neg_std > 0:
                sortino = ((mean_ret - rf_daily) / neg_std) * math.sqrt(252)

    # Total return — Decimal math eliminates the compounding drift seen
    # on long-running accounts; percentage serialised as float.
    if starting_capital_d > 0:
        total_return_pct = float(
            (portfolio_value_d - starting_capital_d) / starting_capital_d
        ) * 100
    else:
        total_return_pct = 0

    # Strategy breakdown — per-strategy PnL accumulates across many trades,
    # so this is one of the places float drift shows up most visibly.
    # Internal pnl field stays as Decimal until final serialisation.
    strategy_breakdown = {
        "trailing_stop": {"trades": 0, "wins": 0, "pnl": Decimal("0")},
        "copy_trading": {"trades": 0, "wins": 0, "pnl": Decimal("0")},
        "wheel": {"trades": 0, "wins": 0, "pnl": Decimal("0")},
        "mean_reversion": {"trades": 0, "wins": 0, "pnl": Decimal("0")},
        "breakout": {"trades": 0, "wins": 0, "pnl": Decimal("0")},
        # Round-10: PEAD (Post-Earnings Drift) — without this bucket,
        # every PEAD trade routes through record_trade_close with
        # strategy="pead" and the `if strat in strategy_breakdown`
        # filter silently drops it, skewing win-rate math and making
        # PEAD invisible in the scorecard CLI summary.
        "pead": {"trades": 0, "wins": 0, "pnl": Decimal("0")},
    }
    # Normalize strategy name to match the canonical lowercase-underscore
    # form used as keys above. Without this, a journal entry with
    # strategy="Copy Trading" or "trailing-stop" falls through the bucket
    # and that trade never shows up in the per-strategy breakdown —
    # silently undercounting performance for weeks before anyone notices.
    # Round-7 audit find.
    def _normalize_strategy_name(s):
        if not s:
            return ""
        return str(s).strip().lower().replace(" ", "_").replace("-", "_")

    for t in trades:
        strat = _normalize_strategy_name(t.get("strategy", ""))
        if strat in strategy_breakdown:
            strategy_breakdown[strat]["trades"] += 1
            if t.get("status") == "closed" and t.get("pnl") is not None:
                strategy_breakdown[strat]["pnl"] += _dec(t["pnl"])
                if t["pnl"] > 0:
                    strategy_breakdown[strat]["wins"] += 1

    # Normalise strategy_breakdown pnl to cents-rounded float for the
    # consumer (dashboard JS, scorecard CSV, Sentry tag). This is the
    # phase-2 JSON-boundary contract.
    for _name, _row in strategy_breakdown.items():
        _row["pnl"] = _to_cents_float(_row["pnl"])

    # A/B Testing: compare strategy pairs with 5+ trades each
    ab_testing = {}
    strat_names = list(strategy_breakdown.keys())
    for i in range(len(strat_names)):
        for j in range(i + 1, len(strat_names)):
            a_name = strat_names[i]
            b_name = strat_names[j]
            a = strategy_breakdown[a_name]
            b = strategy_breakdown[b_name]
            if a["trades"] >= 5 and b["trades"] >= 5:
                a_win_rate = (a["wins"] / a["trades"] * 100) if a["trades"] > 0 else 0
                b_win_rate = (b["wins"] / b["trades"] * 100) if b["trades"] > 0 else 0
                a_avg_pnl = a["pnl"] / a["trades"] if a["trades"] > 0 else 0
                b_avg_pnl = b["pnl"] / b["trades"] if b["trades"] > 0 else 0

                if a_avg_pnl > b_avg_pnl:
                    winner = a_name
                elif b_avg_pnl > a_avg_pnl:
                    winner = b_name
                else:
                    winner = "tie"

                ab_testing[f"{a_name}_vs_{b_name}"] = {
                    a_name: {"trades": a["trades"], "win_rate": round(a_win_rate, 1), "avg_pnl": round(a_avg_pnl, 2)},
                    b_name: {"trades": b["trades"], "win_rate": round(b_win_rate, 1), "avg_pnl": round(b_avg_pnl, 2)},
                    "better_avg_pnl": winner,
                }

    # Correlation Guard (Improvement 9)
    # Round-58: resolve OCC option symbols to their underlying for sector
    # lookup. Before this, an option like "HIMS260508P00027000" wasn't in
    # SECTOR_MAP so it fell through to "Other" and triggered false "3+
    # positions in Other sector" warnings when mixed with other sector-
    # unmapped equities. For display, surface the underlying (e.g. "HIMS")
    # rather than the 17-char OCC symbol which nobody recognises.
    correlation_warning = None
    if positions and isinstance(positions, list):
        try:
            from position_sector import annotate_sector as _annotate
            _annotated = _annotate([dict(p) for p in positions])  # copy; don't mutate scorecard input
        except Exception:
            _annotated = None

        sector_positions = {}
        for idx, p in enumerate(positions):
            sym = p.get("symbol", "")
            if _annotated and idx < len(_annotated):
                ap = _annotated[idx]
                sector = ap.get("_sector") or "Other"
                display_sym = ap.get("_underlying") or sym
            else:
                sector = SECTOR_MAP.get(sym, "Other")
                display_sym = sym
            sector_positions.setdefault(sector, []).append(display_sym)

        concentrated_sectors = {s: syms for s, syms in sector_positions.items() if len(syms) >= 3}
        if concentrated_sectors:
            warnings = []
            for sector, syms in concentrated_sectors.items():
                warnings.append(f"{sector}: {', '.join(syms)} ({len(syms)} positions)")
            correlation_warning = {
                "warning": "Sector concentration detected (3+ positions in same sector)",
                "sectors": concentrated_sectors,
                "details": warnings,
            }

    # Readiness score
    days_tracked = len(snapshots)
    readiness_score = 0
    criteria = scorecard.get("readiness_criteria", {})

    if days_tracked >= criteria.get("min_days", 30):
        readiness_score += 20
    if win_rate >= criteria.get("min_win_rate", 50):
        readiness_score += 20
    if max_dd < criteria.get("max_drawdown", 10):
        readiness_score += 20
    if profit_factor >= criteria.get("min_profit_factor", 1.5):
        readiness_score += 20
    if sharpe >= criteria.get("min_sharpe", 0.5):
        readiness_score += 20

    ready_for_live = readiness_score >= 80

    # Build updated scorecard — emit money fields through _to_cents_float
    # so the JSON boundary stays stable (float with 2dp).
    now_str = now_et().isoformat()
    updated = {
        "start_date": scorecard.get("start_date", "2026-04-15"),
        "starting_capital": starting_capital,
        "current_value": _to_cents_float(portfolio_value_d),
        "total_return_pct": round(total_return_pct, 2),
        "total_trades": total_trades,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win_pct, 2),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "largest_win": _to_cents_float(largest_win_d),
        "largest_loss": _to_cents_float(largest_loss_d),
        "max_drawdown_pct": round(max_dd, 2),
        "peak_value": _to_cents_float(peak_value_d),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "avg_holding_days": round(avg_holding, 1),
        "strategy_breakdown": strategy_breakdown,
        "ab_testing": ab_testing,
        "ready_for_live": ready_for_live,
        "readiness_score": readiness_score,
        "readiness_criteria": criteria if criteria else scorecard.get("readiness_criteria", {}),
        "last_updated": now_str,
    }

    if correlation_warning:
        updated["correlation_warning"] = correlation_warning

    return updated


def take_daily_snapshot(journal, account, positions, scorecard):
    """Create a daily snapshot and append to journal."""
    today = now_et().strftime("%Y-%m-%d")
    snapshots = journal.get("daily_snapshots", [])

    # Don't duplicate today's snapshot
    if snapshots and snapshots[-1].get("date") == today:
        print(f"  Snapshot for {today} already exists, updating it.")
        snapshots.pop()

    portfolio_value = float(account.get("portfolio_value", 0))
    cash = float(account.get("cash", 0))
    positions_count = len(positions) if isinstance(positions, list) else 0

    # Calculate daily P&L
    starting_capital = scorecard.get("starting_capital", 100000)
    prev_value = starting_capital
    if snapshots:
        prev_value = snapshots[-1].get("portfolio_value", starting_capital)

    daily_pnl = portfolio_value - prev_value
    daily_pnl_pct = (daily_pnl / prev_value * 100) if prev_value > 0 else 0

    total_pnl = portfolio_value - starting_capital
    total_pnl_pct = (total_pnl / starting_capital * 100) if starting_capital > 0 else 0

    # Max drawdown
    peak = max(starting_capital, scorecard.get("peak_value", starting_capital))
    for s in snapshots:
        v = s.get("portfolio_value", 0)
        if v > peak:
            peak = v
    if portfolio_value > peak:
        peak = portfolio_value
    max_dd = (peak - portfolio_value) / peak * 100 if peak > 0 else 0

    # Count trades closed today
    trades = journal.get("trades", [])
    closed_today = sum(
        1 for t in trades
        if t.get("status") == "closed"
        and t.get("exit_timestamp", "").startswith(today)
    )
    wins_today = sum(
        1 for t in trades
        if t.get("status") == "closed"
        and t.get("exit_timestamp", "").startswith(today)
        and t.get("pnl", 0) > 0
    )
    losses_today = sum(
        1 for t in trades
        if t.get("status") == "closed"
        and t.get("exit_timestamp", "").startswith(today)
        and t.get("pnl", 0) <= 0
    )
    open_trade_count = sum(1 for t in trades if t.get("status") == "open")

    snapshot = {
        "date": today,
        "portfolio_value": round(portfolio_value, 2),
        "cash": round(cash, 2),
        "positions_count": positions_count,
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(daily_pnl_pct, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "open_trades": open_trade_count,
        "closed_today": closed_today,
        "wins_today": wins_today,
        "losses_today": losses_today,
    }

    snapshots.append(snapshot)
    # Retention: daily_snapshots appends one row per run (minutes-scale
    # cadence from the scheduler). Unbounded growth here has produced
    # multi-MB journal files in long-running deployments, which slows the
    # load+dump on every tick. Cap to the last ~2 years of dailies so the
    # Performance tab still has enough history for charts without blowing
    # up disk / CPU.
    MAX_SNAPSHOTS = 800
    if len(snapshots) > MAX_SNAPSHOTS:
        snapshots = snapshots[-MAX_SNAPSHOTS:]
    journal["daily_snapshots"] = snapshots
    return snapshot


def main():
    print("=" * 60)
    print("SCORECARD UPDATER")
    print("=" * 60)

    # Market hours check using Alpaca /v2/clock
    market_open, market_status = is_market_open()
    print(f"\nMarket status: {market_status}")
    if not market_open:
        print("  Note: Market is currently closed. Running with latest available data.")

    # Load existing data
    journal = load_json(JOURNAL_PATH) or {"trades": [], "daily_snapshots": []}
    scorecard = load_json(SCORECARD_PATH) or {}

    # Lifetime stats (strategy_breakdown, total_pnl, win rate) must include
    # archived closed trades too — otherwise trimming in trade_journal.py
    # would silently erase history from the scorecard. Snapshots (the
    # daily equity curve) stay on the live journal only; they're already
    # capped at 800 rows inside take_snapshot().
    try:
        import trade_journal as _tj
        _arch_trades = []
        _arch_path = _tj.archive_path_for(JOURNAL_PATH)
        if os.path.exists(_arch_path):
            _arch_doc = load_json(_arch_path) or {}
            _arch_trades = list(_arch_doc.get("trades") or [])
        if _arch_trades:
            # Pre-pend archive trades onto the in-memory journal so every
            # downstream reader in calculate_metrics sees full history.
            # We don't rewrite the on-disk live file — this merge is
            # read-only, scoped to this calculation.
            journal = {**journal,
                       "trades": _arch_trades + list(journal.get("trades") or [])}
    except Exception as _e:
        # Scorecard can tolerate a failed archive read — just omit archived
        # trades and compute lifetime stats on the live window.
        print(f"  WARN: archived-trades load failed ({_e}); lifetime stats "
              f"will be live-window only.", flush=True)

    # Fetch live data from Alpaca
    print("\nFetching account data from Alpaca...")
    account = api_get_with_retry(f"{API_ENDPOINT}/account")
    if isinstance(account, dict) and "error" in account:
        print(f"  ERROR fetching account: {account['error']}")
        account = {}
    else:
        pv = float(account.get("portfolio_value", 0))
        cash = float(account.get("cash", 0))
        print(f"  Portfolio value: ${pv:,.2f}")
        print(f"  Cash: ${cash:,.2f}")

    print("Fetching positions...")
    positions = api_get_with_retry(f"{API_ENDPOINT}/positions")
    if isinstance(positions, dict) and "error" in positions:
        print(f"  ERROR fetching positions: {positions['error']}")
        positions = []
    elif isinstance(positions, list):
        print(f"  Open positions: {len(positions)}")
        for p in positions:
            sym = p.get("symbol", "?")
            qty = p.get("qty", 0)
            upl = float(p.get("unrealized_pl", 0))
            print(f"    {sym}: {qty} shares, P&L ${upl:,.2f}")
    else:
        positions = []

    # Calculate all metrics
    print("\nCalculating performance metrics...")
    updated_scorecard = calculate_metrics(journal, scorecard, account, positions)

    # Take daily snapshot
    print("\nTaking daily snapshot...")
    snapshot = take_daily_snapshot(journal, account, positions, updated_scorecard)
    print(f"  Date: {snapshot['date']}")
    print(f"  Portfolio: ${snapshot['portfolio_value']:,.2f}")
    print(f"  Daily P&L: ${snapshot['daily_pnl']:,.2f} ({snapshot['daily_pnl_pct']:+.2f}%)")
    print(f"  Total P&L: ${snapshot['total_pnl']:,.2f} ({snapshot['total_pnl_pct']:+.2f}%)")

    # Save everything
    print("\nSaving updated files...")
    safe_save_json(SCORECARD_PATH, updated_scorecard)
    print(f"  Saved: {SCORECARD_PATH}")
    safe_save_json(JOURNAL_PATH, journal)
    print(f"  Saved: {JOURNAL_PATH}")

    # Print summary
    print("\n" + "=" * 60)
    print("PERFORMANCE SUMMARY")
    print("=" * 60)
    print(f"  Portfolio Value:    ${updated_scorecard['current_value']:,.2f}")
    print(f"  Total Return:       {updated_scorecard['total_return_pct']:+.2f}%")
    print(f"  Total Trades:       {updated_scorecard['total_trades']}")
    print(f"  Open / Closed:      {updated_scorecard['open_trades']} / {updated_scorecard['closed_trades']}")
    print(f"  Win Rate:           {updated_scorecard['win_rate_pct']:.1f}%")
    print(f"  Profit Factor:      {updated_scorecard['profit_factor']:.2f}")
    print(f"  Max Drawdown:       {updated_scorecard['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe Ratio:       {updated_scorecard['sharpe_ratio']:.2f}")
    print(f"  Sortino Ratio:      {updated_scorecard['sortino_ratio']:.2f}")
    print(f"  Avg Holding Days:   {updated_scorecard['avg_holding_days']:.1f}")
    print(f"  Readiness Score:    {updated_scorecard['readiness_score']}/100")
    print(f"  Ready for Live:     {'YES' if updated_scorecard['ready_for_live'] else 'NO'}")

    if updated_scorecard.get("correlation_warning"):
        cw = updated_scorecard["correlation_warning"]
        print(f"\n  CORRELATION WARNING: {cw['warning']}")
        for detail in cw.get("details", []):
            print(f"    - {detail}")

    if updated_scorecard.get("ab_testing"):
        print(f"\n  A/B Testing Results:")
        for pair, result in updated_scorecard["ab_testing"].items():
            print(f"    {pair}: better = {result['better_avg_pnl']}")

    # Strategy breakdown
    print(f"\n  Strategy Breakdown:")
    for strat, data in updated_scorecard["strategy_breakdown"].items():
        if data["trades"] > 0:
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0
            print(f"    {strat}: {data['trades']} trades, {wr:.0f}% win rate, P&L ${data['pnl']:,.2f}")

    print(f"\n  Snapshots recorded: {len(journal.get('daily_snapshots', []))}")
    print(f"  Last updated: {updated_scorecard['last_updated']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
