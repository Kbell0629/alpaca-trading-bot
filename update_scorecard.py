#!/usr/bin/env python3
"""
Scorecard Updater — Reads trade journal, Alpaca account data, and positions,
then calculates all performance metrics and updates scorecard.json.

Also takes a daily snapshot and appends it to trade_journal.json.

Run: python3 "/Users/kevinbell/Alpaca Trading/update_scorecard.py"
"""

import contextlib
import json
import os
import tempfile
import time
import urllib.request
import urllib.error
from et_time import now_et

# Round-61 pt.95 (audit-sweep): replicate cloud_scheduler.strategy_file_lock
# locally so the journal RMW here contends on the SAME flock the
# scheduler thread + dashboard handler already use. Without this lock,
# a record_trade_close() that fires mid-run can be silently clobbered
# when this subprocess writes the older in-memory journal back at the
# end of the run.
try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:  # Windows / restricted images
    _fcntl = None
    _HAS_FCNTL = False


@contextlib.contextmanager
def _journal_lock(path):
    """Exclusive flock against ``<path>.lock`` matching the lock-file
    naming used in cloud_scheduler._StrategyFileLock so both contenders
    serialise on the same kernel-level lock. No-op on platforms without
    fcntl (best-effort)."""
    fh = None
    if _HAS_FCNTL and path:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fh = open(path + ".lock", "w")
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        except Exception:
            if fh:
                try: fh.close()
                except Exception: pass
            fh = None
    try:
        yield
    finally:
        if fh:
            try: _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            except Exception: pass
            try: fh.close()
            except Exception: pass

# Round-61 pt.7: pure scorecard math extracted to scorecard_core.py so
# pytest-cov can see it. This file still lives in coverage's `omit` list
# (it's a one-shot CLI spawned as a subprocess), so moving the math out
# was the only way to get it under coverage.
from scorecard_core import (
    _dec, _to_cents_float,
    calculate_metrics as _calc_metrics,
    take_daily_snapshot as _take_daily_snapshot,
)


def calculate_metrics(journal, scorecard, account, positions):
    """Thin compat wrapper — real logic in scorecard_core.calculate_metrics.

    Passes the production `now_et` + `position_sector.annotate_sector` +
    `constants.SECTOR_MAP` dependencies; the core module is dependency-free
    by default so tests can run without them.
    """
    try:
        from position_sector import annotate_sector as _annotate
    except Exception:
        _annotate = None
    return _calc_metrics(journal, scorecard, account, positions,
                         now_fn=now_et,
                         sector_map=SECTOR_MAP,
                         annotate_fn=_annotate)


def take_daily_snapshot(journal, account, positions, scorecard):
    """Thin compat wrapper — real logic in scorecard_core.take_daily_snapshot."""
    return _take_daily_snapshot(journal, account, positions, scorecard,
                                now_fn=now_et)


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

    # Save everything.
    # Round-61 pt.95 (audit-sweep): wrap the journal write in
    # ``_journal_lock`` so a concurrent ``record_trade_close()`` in
    # the scheduler thread can't be silently clobbered. The snapshot
    # we just took is the ONLY mutation we make to the journal — to
    # avoid losing trade rows that landed during our metrics run, we
    # reload the on-disk journal under the lock and merge our new
    # snapshot into THAT version's daily_snapshots before saving.
    print("\nSaving updated files...")
    safe_save_json(SCORECARD_PATH, updated_scorecard)
    print(f"  Saved: {SCORECARD_PATH}")
    with _journal_lock(JOURNAL_PATH):
        latest = load_json(JOURNAL_PATH) or {"trades": [], "daily_snapshots": []}
        # Carry over our snapshot list (which already includes the new
        # row + retention trim) but keep whatever trades the scheduler
        # wrote while we were computing metrics.
        latest["daily_snapshots"] = list(journal.get("daily_snapshots") or [])
        safe_save_json(JOURNAL_PATH, latest)
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
