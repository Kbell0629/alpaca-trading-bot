#!/usr/bin/env python3
"""
Wheel Strategy Automation.

The "wheel" is a recurring income strategy on stocks you'd be happy to own:

    Stage 1: Sell cash-secured PUT ~10% below current price, 14-45 DTE
      -> If expires worthless: keep premium, restart stage 1
      -> If assigned: you buy 100 shares at strike, go to stage 2

    Stage 2: Sell covered CALL ~10% above cost basis, 14-45 DTE
      -> If expires worthless: keep premium, sell another call
      -> If assigned: shares called away at strike, cycle complete, back to stage 1

This module provides the core primitives. Call sites live in cloud_scheduler.py:
  run_wheel_auto_deploy(user)  - daily ~9:40 AM ET, picks candidate + sells put
  run_wheel_monitor(user)      - every 15 min during market hours, manages state

All functions are stdlib-only and take a `user` dict with the private Alpaca
credentials injected by cloud_scheduler.get_all_users_for_scheduling().

State file format: {DATA_DIR}/users/{user_id}/strategies/wheel_{SYMBOL}.json
See WHEEL_STATE_TEMPLATE below for shape.
"""
import json
import os
import tempfile
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN

try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone.utc

def now_et():
    return datetime.now(ET_TZ)


# Phase 4 of the float->Decimal migration (see docs/DECIMAL_MIGRATION_PLAN.md).
# Highest-risk phase yet — wheel cycles chain cost basis across many legs
# (put-sell -> assignment -> call-sell -> expiry-or-assignment -> ...). Drift
# compounds over dozens of cycles; Decimal keeps the recorded PnL + cost basis
# exact to the cent. Parity fuzz test runs the 52-cycle synthetic wheel
# through old + new impls, asserts lifetime PnL matches to the penny.

_CENT = Decimal("0.01")


def _dec(v, default=Decimal("0")):
    """Coerce to Decimal via str() (avoids Decimal(float_x) footgun)."""
    if v is None or v == "":
        return default
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return default


def _to_cents_float(v):
    """Quantize to cents (banker's rounding) and emit a float for JSON."""
    if not isinstance(v, Decimal):
        v = _dec(v)
    return float(v.quantize(_CENT, rounding=ROUND_HALF_EVEN))


def _detect_split_since(symbol: str, since_iso: str) -> float:
    """Return the cumulative split ratio for `symbol` between `since_iso`
    and now, or 1.0 if no split (or the lookup failed).

    Used by the put-assignment anomaly guard in _advance_wheel_state_
    locked. If a 2:1 split happened while our put was active, shares
    double unexpectedly and we would mis-attribute assignment. With this
    helper, we detect the split and normalise baseline + expected_delta
    before deciding whether the put was actually assigned.

    Returns 1.0 (no-op multiplier) on any failure so the caller falls
    through to the existing FREEZE-state-for-manual-reconcile path.
    """
    if not since_iso:
        return 1.0
    try:
        opened = datetime.fromisoformat(since_iso)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=ET_TZ)
    except (ValueError, TypeError):
        return 1.0
    try:
        from yfinance_budget import yf_splits
        splits = yf_splits(symbol)
        if not splits:
            return 1.0
        cumulative = 1.0
        for split_dt, ratio in splits:
            # yfinance returns tz-naive UTC; compare against opened in UTC
            s_dt = split_dt.replace(tzinfo=timezone.utc) if split_dt.tzinfo is None else split_dt
            if s_dt >= opened and ratio > 0:
                cumulative *= float(ratio)
        return cumulative
    except Exception as e:
        # Any failure (network, malformed split row, import error) falls
        # through to the caller's FREEZE-state path. Surface to Sentry so
        # we notice systematic yfinance shape changes.
        try:
            from observability import capture_exception
            capture_exception(e, component="wheel_strategy",
                              fn="_detect_split_since", symbol=symbol)
        except Exception:
            pass
        return 1.0

# File locking — used to prevent races between wheel monitor ticks and
# human-triggered actions (e.g. Force Deploy). Unix only; on Windows this
# falls through to a no-op lock.
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

class _WheelLock:
    """Context manager that flock()s a wheel state file during read-modify-write.

    Hardened against two classes of partial-failure:
      1. `open()` succeeds but `flock()` fails — previously the file handle
         was closed, but any references to `self.fh` afterward would have
         flown past. Now `self.fh` is only set once flock succeeds.
      2. Process crash between __enter__ and __exit__ — the ".lock" file
         stays on disk but advisory-lock state is cleaned up by the kernel
         when the FD is closed (automatic on process exit). Stale .lock
         FILES never block a subsequent lock acquire because flock is
         file-descriptor-scoped, not file-name-scoped.
    """
    def __init__(self, path):
        self.path = path
        self.fh = None
    def __enter__(self):
        if not _HAS_FCNTL:
            return self
        fh = None
        try:
            fh = open(self.path + ".lock", "w")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            self.fh = fh  # only mark as held after flock succeeds
        except Exception:
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
            self.fh = None
        return self
    def __exit__(self, *a):
        if self.fh:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

# Wheel-specific safety rails
MIN_STOCK_PRICE = 10.0        # Below this, premium isn't worth the capital tie-up
MAX_STOCK_PRICE = 50.0        # Above this, 100 shares would be >5% of $100k
MIN_DTE = 14                  # Shorter DTE has too little premium
MAX_DTE = 45                  # Longer DTE ties up capital too long
TARGET_DTE = 21               # 3 weeks is the sweet spot for theta decay
PUT_STRIKE_PCT_BELOW = 0.10   # Sell puts 10% OTM (rough delta 0.25-0.30)
CALL_STRIKE_PCT_ABOVE = 0.10  # Sell calls 10% OTM on shares we own
MIN_OPEN_INTEREST = 50        # Liquidity: need someone on the other side
MIN_PREMIUM_PCT = 0.005       # Min premium as pct of strike (0.5% = ~12% annualized)
PROFIT_CLOSE_PCT = 0.50       # Buy to close at 50% profit (Tasty Trade rule of thumb)
EARNINGS_AVOID_DAYS = 30      # Skip wheels on stocks with earnings inside window
MAX_CONCURRENT_WHEELS = 2     # Cap capital tied up in wheels

WHEEL_STATE_TEMPLATE = {
    "symbol": None,
    "strategy": "wheel",
    "created": None,              # ISO date when cycle first started
    "updated": None,              # ISO timestamp of last state change
    "stage": "stage_1_searching", # See state machine in module docstring
    "shares_owned": 0,            # 0 in stage 1, 100+ in stage 2
    "shares_at_open": 0,          # Pre-put baseline share count — used to
                                  # distinguish put-assignment from pre-existing
                                  # shares the user already held.
    "cost_basis": None,           # Per-share, after accounting for put premiums
    "cycles_completed": 0,        # Full wheel rotations
    "total_premium_collected": 0.0,
    "total_realized_pnl": 0.0,    # Premium + stock gains combined
    "active_contract": None,      # See ACTIVE_CONTRACT_TEMPLATE
    "history": [],                # Audit trail of state transitions (capped)
    "deployer": "cloud_scheduler",
}

HISTORY_MAX = 500  # Cap history[] to prevent unbounded growth over months

ACTIVE_CONTRACT_TEMPLATE = {
    "contract_symbol": None,      # e.g. "SOFI260501P00018000"
    "type": None,                 # "put" or "call"
    "strike": None,
    "expiration": None,           # YYYY-MM-DD
    "dte_at_open": None,
    "quantity": 1,                # Number of contracts (each = 100 shares)
    "premium_received": None,     # Total $ received (limit_price × qty × 100)
    "limit_price_used": None,
    "open_order_id": None,
    "close_order_id": None,
    "opened_at": None,            # ISO timestamp
    "closed_at": None,
    "status": "pending",          # pending | active | closed | assigned | expired
}


# ============================================================================
# FILE HELPERS
# ============================================================================
def _user_strategies_dir(user):
    """Return the strategies dir for a user, creating if needed."""
    d = user.get("_strategies_dir") or os.path.join(user.get("_data_dir") or DATA_DIR, "strategies")
    os.makedirs(d, exist_ok=True)
    return d


def _user_file(user, filename):
    return os.path.join(user.get("_data_dir") or DATA_DIR, filename)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_json(path, data):
    """Atomic JSON write via temp file + rename."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.rename(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise


def list_wheel_files(user):
    """Return list of (filename, state_dict) for all wheel files this user has.
    Logs a WARN to stderr for any wheel_*.json that fails to parse so a
    corrupted state file doesn't silently cause positions to go unmanaged.
    """
    import sys as _sys
    sdir = _user_strategies_dir(user)
    results = []
    try:
        for f in os.listdir(sdir):
            if f.startswith("wheel_") and f.endswith(".json") and f != "wheel_strategy.json":
                path = os.path.join(sdir, f)
                state = _load_json(path)
                if state is None:
                    print(f"[wheel] WARN: malformed state file {path} — SKIPPING. Positions for this symbol will not be managed until fixed.", file=_sys.stderr, flush=True)
                    continue
                if state.get("strategy") != "wheel":
                    print(f"[wheel] WARN: {path} missing 'strategy':'wheel' — skipping.", file=_sys.stderr, flush=True)
                    continue
                results.append((f, state))
    except FileNotFoundError:
        pass
    return results


def wheel_state_path(user, symbol):
    return os.path.join(_user_strategies_dir(user), f"wheel_{symbol}.json")


def save_wheel_state(user, state):
    """Write state and append to history."""
    state["updated"] = now_et().isoformat()
    _save_json(wheel_state_path(user, state["symbol"]), state)


def log_history(state, event, detail=None):
    """Append a timestamped event to the wheel's audit trail. Caps at HISTORY_MAX
    entries to prevent unbounded growth over months of monitoring."""
    hist = state.setdefault("history", [])
    hist.append({
        "ts": now_et().isoformat(),
        "event": event,
        "stage": state.get("stage"),
        "detail": detail,
    })
    if len(hist) > HISTORY_MAX:
        state["history"] = hist[-HISTORY_MAX:]


# Round-42: wheel closes need to hit the trade journal so the scorecard /
# dashboard "closed positions" / "Today's Closes" panel see them. Before
# this round, wheel_strategy updated the wheel state file + audit history
# on every exit path (assigned, expired, bought-to-close, externally
# closed) but NEVER called record_trade_close — so the journal ended up
# with orphan "open" entries that silently vanished when the option
# disappeared from Alpaca.
#
# Helper centralises the boilerplate so all exit paths use the same
# contract-symbol / strategy / side convention as the record_trade_open
# call in open_short_put.
def _journal_wheel_close(user, contract_meta, exit_price, pnl, exit_reason):
    """Record a close for a wheel option leg in trade_journal.json.

    Never blocks the state-machine — journal-write failure is logged but
    swallowed (same pattern as the open-side record_trade_open in
    open_short_put). Callers don't have to try/except.
    """
    try:
        from cloud_scheduler import record_trade_close
        contract_sym = contract_meta.get("contract_symbol") if isinstance(contract_meta, dict) else None
        if not contract_sym:
            return
        record_trade_close(
            user, contract_sym, "wheel",
            exit_price=exit_price,
            pnl=pnl,
            exit_reason=exit_reason,
            qty=-int(contract_meta.get("quantity", 1)),  # short: negative qty
            side="buy",  # buy-to-close a short option
        )
    except Exception:
        pass  # journaling is best-effort; don't break wheel state machine


# ============================================================================
# ALPACA API HELPERS (user-scoped)
# ============================================================================
def _headers(user):
    return {
        "APCA-API-KEY-ID": user["_api_key"],
        "APCA-API-SECRET-KEY": user["_api_secret"],
    }


def _api_get(user, path, timeout=15):
    """GET from trading or data endpoint based on path prefix."""
    if path.startswith("http"):
        url = path
    elif "/stocks/" in path or "/options/quotes" in path or "/options/bars" in path:
        url = user["_data_endpoint"] + path
    else:
        url = user["_api_endpoint"] + path
    req = urllib.request.Request(url, headers=_headers(user))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def _api_post(user, path, payload, timeout=15):
    url = path if path.startswith("http") else user["_api_endpoint"] + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**_headers(user), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        # Capture error body if available
        try:
            body = e.read().decode() if hasattr(e, "read") else str(e)
        except Exception:
            body = str(e)
        return {"error": body}


# ============================================================================
# OPTIONS DATA HELPERS
# ============================================================================
def get_stock_price(user, symbol):
    """Return latest trade price (or None)."""
    data = _api_get(user, f"/stocks/{symbol}/snapshot")
    if "error" in data:
        return None
    price = (data.get("latestTrade") or {}).get("p") or \
            (data.get("latestBar") or {}).get("c") or \
            (data.get("latestQuote") or {}).get("ap")
    try:
        return float(price) if price else None
    except (TypeError, ValueError):
        return None


def fetch_option_contracts(user, symbol, opt_type, min_dte=MIN_DTE, max_dte=MAX_DTE):
    """Return list of contracts matching symbol/type within the DTE window."""
    today = date.today()
    min_exp = (today + timedelta(days=min_dte)).isoformat()
    max_exp = (today + timedelta(days=max_dte)).isoformat()

    all_contracts = []
    next_token = None
    # Paginate — Alpaca returns up to 200 per page
    _MAX_PAGES = 10
    pages_fetched = 0
    for _ in range(_MAX_PAGES):  # safety cap (2000 contracts)
        params = {
            "underlying_symbols": symbol,
            "type": opt_type,
            "expiration_date_gte": min_exp,
            "expiration_date_lte": max_exp,
            "status": "active",
            "limit": "200",
        }
        if next_token:
            params["page_token"] = next_token
        path = "/options/contracts?" + urllib.parse.urlencode(params)
        result = _api_get(user, path)
        if "error" in result:
            break
        contracts = result.get("option_contracts", [])
        all_contracts.extend(contracts)
        pages_fetched += 1
        next_token = result.get("next_page_token")
        if not next_token:
            break
    # Warn if we hit the cap — means the chain was likely truncated.
    if pages_fetched >= _MAX_PAGES and next_token:
        import sys as _sys
        print(f"[wheel] WARN: {symbol} {opt_type} chain TRUNCATED at {len(all_contracts)} contracts (hit _MAX_PAGES={_MAX_PAGES}). Some contracts may not have been considered.", file=_sys.stderr, flush=True)
    return all_contracts


def get_option_quote(user, contract_symbol):
    """Return {bid, ask, mid} for a contract or None."""
    path = f"/v1beta1/options/quotes/latest?symbols={contract_symbol}&feed=indicative"
    url = "https://data.alpaca.markets" + path
    result = _api_get(user, url)
    quote = (result.get("quotes") or {}).get(contract_symbol)
    if not quote:
        return None
    bid = float(quote.get("bp") or 0)
    ask = float(quote.get("ap") or 0)
    if bid <= 0 and ask <= 0:
        return None
    mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (ask or bid)
    return {"bid": bid, "ask": ask, "mid": mid}


def score_contract(contract, quote, target_strike, current_price, opt_type,
                    underlying_iv=None):
    """Composite score: favor target delta (0.20-0.30 for puts, 0.15-0.25
    for calls), target-DTE, premium yield, liquidity.
    Higher is better. Returns None if contract should be rejected outright.

    Round-11 Tier 3: delta-targeting replaces pure strike-distance as the
    primary ranking factor. Industry standard for option selling —
    targets specific assignment-probability buckets rather than an
    arbitrary % OTM.
    """
    try:
        strike = float(contract["strike_price"])
        oi = int(contract.get("open_interest") or 0)
        exp = datetime.strptime(contract["expiration_date"], "%Y-%m-%d").date()
        dte = (exp - date.today()).days
    except (KeyError, ValueError, TypeError):
        return None

    if oi < MIN_OPEN_INTEREST:
        return None

    premium = quote.get("bid") or 0  # Use bid for sell side (conservative)
    if premium <= 0:
        return None

    # Premium yield: how much we collect vs capital at risk
    if opt_type == "put":
        # Capital at risk = strike × 100 (assignment cost)
        premium_pct = premium / strike if strike else 0
    else:
        # For calls, capital at risk is opportunity cost — compare to current price
        premium_pct = premium / current_price if current_price else 0

    if premium_pct < MIN_PREMIUM_PCT:
        return None

    # Distance from target strike (closer = better) — kept as secondary signal
    dist_pct = abs(strike - target_strike) / current_price if current_price else 1.0
    dist_score = max(0.0, 10.0 - dist_pct * 100)

    # DTE score (peaks at TARGET_DTE)
    dte_score = max(0.0, 10.0 - abs(dte - TARGET_DTE) / 3.0)

    # Premium yield score (higher = better, but cap at 5% = full points)
    yield_score = min(10.0, premium_pct * 200)  # 0.005 -> 1.0, 0.05 -> 10.0

    # Liquidity bonus
    liq_score = min(5.0, oi / 200.0)

    # Round-11: Black-Scholes delta score. Target 0.25 for puts
    # (25% assignment probability is the sweet spot — rich premium
    # without excessive put-buyer edge). 0.20 for calls (slightly
    # safer, prevents premature called-away on rising names).
    delta_score = 0.0
    computed_delta = None
    try:
        from options_greeks import put_delta, call_delta, delta_score_bonus
        T = max(1, dte) / 365.0
        # Pull contract's implied_volatility if present; fall back to
        # underlying's 20-day HV as a proxy (computed by caller).
        iv_raw = contract.get("implied_volatility")
        try:
            sigma = float(iv_raw) if iv_raw else None
        except (ValueError, TypeError):
            sigma = None
        if not sigma or sigma <= 0:
            sigma = float(underlying_iv) if underlying_iv else 0.30
        if opt_type == "put":
            computed_delta = put_delta(current_price, strike, T, sigma)
            delta_score = delta_score_bonus(computed_delta, target=0.25, tolerance=0.10)
        else:
            computed_delta = call_delta(current_price, strike, T, sigma)
            delta_score = delta_score_bonus(computed_delta, target=0.20, tolerance=0.10)
    except Exception:
        delta_score = 0.0

    total = dist_score + dte_score + yield_score + liq_score + delta_score
    return round(total, 2)


def find_best_contract(user, symbol, opt_type, target_strike, current_price,
                         underlying_iv=None):
    """Pick the highest-scoring contract for the sell-to-open.
    Returns {contract, quote, score} or None if nothing viable.

    Round-11 Tier 3: `underlying_iv` (decimal HV) is passed through to
    score_contract() so the delta calculation has a sigma estimate
    even when the contract payload doesn't carry implied_volatility.
    """
    contracts = fetch_option_contracts(user, symbol, opt_type)
    best = None
    for c in contracts:
        # Quick pre-filter: strike must be on the right side of current price
        try:
            strike = float(c["strike_price"])
        except (KeyError, ValueError):
            continue
        if opt_type == "put" and strike >= current_price:
            continue  # Want OTM puts (strike < price)
        if opt_type == "call" and strike <= current_price:
            continue  # Want OTM calls (strike > price)
        # Round-11: widen the strike-search window from ±10% to ±15%
        # of current price so the delta-targeting has enough strikes
        # to find the 0.20-0.30 sweet spot even on low-vol names.
        if abs(strike - target_strike) / current_price > 0.15:
            continue

        quote = get_option_quote(user, c["symbol"])
        if not quote:
            continue
        score = score_contract(c, quote, target_strike, current_price, opt_type,
                                underlying_iv=underlying_iv)
        if score is None:
            continue
        if best is None or score > best["score"]:
            best = {"contract": c, "quote": quote, "score": score}
    return best


# ============================================================================
# SAFETY CHECKS
# ============================================================================
def options_trading_allowed(user):
    """Confirm the account has options approval."""
    acct = _api_get(user, "/account")
    if "error" in acct:
        return False, f"Account check failed: {acct['error']}"
    try:
        level = int(acct.get("options_approved_level") or 0)
    except (TypeError, ValueError):
        level = 0
    if level < 2:
        return False, f"Account options level {level} < 2 required for cash-secured puts"
    return True, None


def cash_covered(user, strike, qty=1):
    """Check that account has enough cash to buy 100×qty shares at strike if assigned."""
    acct = _api_get(user, "/account")
    if "error" in acct:
        return False, f"Account fetch failed: {acct['error']}"
    try:
        cash = float(acct.get("cash") or 0)
    except (TypeError, ValueError):
        cash = 0
    needed = strike * 100 * qty
    if cash < needed:
        return False, f"Cash ${cash:,.0f} < required ${needed:,.0f} for assignment coverage"
    return True, None


def has_earnings_soon(pick):
    """Check the pick's earnings_warning flag or days_to_earnings field."""
    if pick.get("earnings_warning"):
        return True
    days = pick.get("days_to_earnings")
    if isinstance(days, (int, float)) and 0 <= days <= EARNINGS_AVOID_DAYS:
        return True
    return False


def count_active_wheels(user):
    """How many non-searching wheel cycles does this user already have?"""
    wheels = list_wheel_files(user)
    return sum(1 for _, s in wheels if s.get("stage") != "stage_1_searching")


# ============================================================================
# STAGE 1: SELL CASH-SECURED PUT
# ============================================================================
def open_short_put(user, pick):
    """Given a screener pick, open a cash-secured put cycle.

    Returns (success, message, state_file_path). Writes a wheel_{SYMBOL}.json
    on success. Does NOT wait for fill — monitor will transition state on fill.
    """
    symbol = pick.get("symbol")
    if not symbol:
        return False, "No symbol in pick", None

    # Already have a wheel on this symbol?
    existing = _load_json(wheel_state_path(user, symbol))
    if existing and existing.get("stage") != "stage_1_searching":
        return False, f"Wheel already active on {symbol} at stage {existing.get('stage')}", None

    # Options approval
    ok, reason = options_trading_allowed(user)
    if not ok:
        return False, reason, None

    # Price range
    current_price = float(pick.get("price") or 0) or get_stock_price(user, symbol)
    if not current_price or current_price < MIN_STOCK_PRICE or current_price > MAX_STOCK_PRICE:
        return False, f"{symbol} price ${current_price} outside wheel range ${MIN_STOCK_PRICE}-${MAX_STOCK_PRICE}", None

    # Earnings filter
    if has_earnings_soon(pick):
        return False, f"{symbol} has earnings within {EARNINGS_AVOID_DAYS} days — skipping to avoid IV crush", None

    # Round-11 Tier 2: IV rank gate. Historical volatility rank as a
    # proxy for IV rank — only sell puts when premium is "rich" (HV
    # rank >= 30). Published option-seller data: IV rank < 30 leaves
    # 50%+ of the edge on the table. Falls through if hv_rank not
    # attached (screener didn't enrich this symbol) so legacy behaviour
    # preserved for edge cases.
    try:
        _hv = pick.get("hv_rank")
        if _hv is not None and _hv < 30:
            return False, (f"{symbol} HV rank {_hv:.0f} < 30 — premium too thin, "
                            "skipping (wheel edge lives in rich-IV regimes)"), None
    except Exception:
        pass

    # Concurrent-wheel cap
    if count_active_wheels(user) >= MAX_CONCURRENT_WHEELS:
        return False, f"Already at max {MAX_CONCURRENT_WHEELS} concurrent wheels", None

    # Find best put contract. Round-11 Tier 3: pass underlying HV as
    # IV proxy so the delta-targeting in score_contract has a sigma
    # when the contract payload doesn't include implied_volatility.
    target_strike = current_price * (1 - PUT_STRIKE_PCT_BELOW)
    _uly_iv = None
    try:
        _hv = pick.get("current_hv")
        if _hv and float(_hv) > 0:
            _uly_iv = float(_hv)
    except (TypeError, ValueError):
        _uly_iv = None
    best = find_best_contract(user, symbol, "put", target_strike, current_price,
                                underlying_iv=_uly_iv)
    if not best:
        return False, f"No viable put contracts for {symbol} near ${target_strike:.2f}", None

    contract = best["contract"]
    quote = best["quote"]
    strike = float(contract["strike_price"])

    # Cash-covered check against this specific strike
    ok, reason = cash_covered(user, strike, qty=1)
    if not ok:
        return False, reason, None

    # Use mid price (rounded to nickel) as limit — between bid and ask
    mid = quote["mid"]
    limit_price = round(max(mid, 0.05) * 20) / 20  # round to nearest $0.05
    if limit_price < 0.05:
        return False, f"Premium ${mid:.3f} too low to bother", None

    order_payload = {
        "symbol": contract["symbol"],
        "qty": "1",
        "side": "sell",
        "type": "limit",
        "limit_price": f"{limit_price:.2f}",
        "time_in_force": "day",
        "order_class": "simple",
    }
    # Snapshot baseline share count BEFORE placing the put, so at expiration
    # we can compare the delta to detect actual assignment vs. pre-existing
    # shares the user already held for other reasons.
    positions_now = _api_get(user, "/positions")
    baseline_shares = 0
    if isinstance(positions_now, list):
        for p in positions_now:
            if p.get("symbol") == symbol:
                try: baseline_shares = int(float(p.get("qty") or 0))
                except (TypeError, ValueError): baseline_shares = 0
                break

    order_resp = _api_post(user, "/orders", order_payload)
    if "error" in order_resp or "id" not in order_resp:
        return False, f"Order rejected: {order_resp}", None

    # Build state file
    exp = contract["expiration_date"]
    dte = (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days
    premium_received = round(limit_price * 100, 2)  # one contract = 100 shares
    now_iso = now_et().isoformat()

    import copy as _copy
    state = _copy.deepcopy(WHEEL_STATE_TEMPLATE)  # avoid shared history[] refs across wheels
    state["symbol"] = symbol
    state["created"] = now_iso
    state["stage"] = "stage_1_put_active"
    state["shares_at_open"] = baseline_shares
    state["active_contract"] = {
        **ACTIVE_CONTRACT_TEMPLATE,
        "contract_symbol": contract["symbol"],
        "type": "put",
        "strike": strike,
        "expiration": exp,
        "dte_at_open": dte,
        "quantity": 1,
        "premium_received": premium_received,
        "limit_price_used": limit_price,
        "open_order_id": order_resp["id"],
        "opened_at": now_iso,
        "status": "pending",
    }
    state["history"] = [{
        "ts": now_iso,
        "event": "sell_to_open_put",
        "stage": state["stage"],
        "detail": {
            "contract": contract["symbol"],
            "strike": strike,
            "expiration": exp,
            "limit_price": limit_price,
            "premium_if_filled": premium_received,
            "current_stock_price": current_price,
            "score": best["score"],
        }
    }]
    save_wheel_state(user, state)

    # Round-33: log to the per-user trade journal so the scorecard's
    # total_trades counter + win-rate math reflect wheel deploys.
    # Previously only run_auto_deployer's main equity path appended
    # here, so wheel puts were invisible to the readiness gauge.
    try:
        from cloud_scheduler import record_trade_open
        record_trade_open(
            user, contract["symbol"], "wheel",
            price=limit_price, qty=-1, side="sell_short",
            reason=(f"Sold-to-open put. Strike ${strike}, exp {exp}, "
                    f"premium ${premium_received:.2f}"),
            deployer="wheel_auto_deploy",
            extra={"underlying": symbol, "option_side": "put"},
        )
    except Exception:
        pass  # never block the trade on a journal-write hiccup

    return True, (
        f"Sold-to-open {contract['symbol']} @ ${limit_price:.2f} "
        f"(premium ${premium_received:.2f} if filled). Stage: put active."
    ), wheel_state_path(user, symbol)


# ============================================================================
# STAGE 2: SELL COVERED CALL
# ============================================================================
def open_covered_call(user, state):
    """When we're holding shares (stage_2_shares_owned), sell a call against them.
    Never sells below cost basis (locks in loss if called away).
    """
    symbol = state["symbol"]
    shares = state.get("shares_owned", 0)
    if shares < 100:
        return False, f"Need at least 100 shares to sell a call, have {shares}"

    cost_basis = state.get("cost_basis")
    if not cost_basis:
        return False, "No cost basis recorded — refusing to sell call"

    current_price = get_stock_price(user, symbol)
    if not current_price:
        return False, f"Could not fetch {symbol} price"

    # Target strike = 10% above cost basis, not below current price
    target_strike = max(cost_basis * (1 + CALL_STRIKE_PCT_ABOVE), current_price * 1.02)

    best = find_best_contract(user, symbol, "call", target_strike, current_price)
    if not best:
        return False, f"No viable call contracts for {symbol} near ${target_strike:.2f}"

    contract = best["contract"]
    quote = best["quote"]
    strike = float(contract["strike_price"])

    # SAFETY: never sell a call with strike below cost basis
    if strike < cost_basis:
        return False, f"Call strike ${strike:.2f} < cost basis ${cost_basis:.2f} — refusing"

    mid = quote["mid"]
    limit_price = round(max(mid, 0.05) * 20) / 20
    if limit_price < 0.05:
        return False, f"Premium ${mid:.3f} too low"

    contracts_qty = shares // 100  # cover as many as we have shares for
    order_payload = {
        "symbol": contract["symbol"],
        "qty": str(contracts_qty),
        "side": "sell",
        "type": "limit",
        "limit_price": f"{limit_price:.2f}",
        "time_in_force": "day",
        "order_class": "simple",
    }
    order_resp = _api_post(user, "/orders", order_payload)
    if "error" in order_resp or "id" not in order_resp:
        return False, f"Call order rejected: {order_resp}"

    exp = contract["expiration_date"]
    dte = (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days
    premium_received = round(limit_price * 100 * contracts_qty, 2)
    now_iso = now_et().isoformat()

    # Snapshot shares at call-open so at expiration we can detect called-away
    # by delta (shares decreased by qty*100) instead of absolute zero (which
    # false-positives if the user sold manually in another tab).
    shares_at_call_open = int(shares)

    state["stage"] = "stage_2_call_active"
    state["active_contract"] = {
        **ACTIVE_CONTRACT_TEMPLATE,
        "contract_symbol": contract["symbol"],
        "type": "call",
        "strike": strike,
        "expiration": exp,
        "dte_at_open": dte,
        "quantity": contracts_qty,
        "premium_received": premium_received,
        "limit_price_used": limit_price,
        "open_order_id": order_resp["id"],
        "opened_at": now_iso,
        "status": "pending",
        "shares_at_call_open": shares_at_call_open,
    }
    log_history(state, "sell_to_open_call", {
        "contract": contract["symbol"],
        "strike": strike,
        "expiration": exp,
        "limit_price": limit_price,
        "premium_if_filled": premium_received,
        "cost_basis": cost_basis,
        "score": best["score"],
    })
    save_wheel_state(user, state)
    return True, (
        f"Sold-to-open {contract['symbol']} @ ${limit_price:.2f} × {contracts_qty} "
        f"(premium ${premium_received:.2f} if filled). Stage: call active."
    )


# ============================================================================
# MONITOR — DRIVES THE STATE MACHINE
# ============================================================================
def advance_wheel_state(user, state):
    """Inspect current Alpaca state and advance the wheel's state file as needed.
    Returns a list of human-readable events for logging.

    Uses a file lock to prevent races between the 15-min monitor tick and
    human-triggered actions (Force Deploy) that could otherwise double-count
    premium or submit duplicate buy-to-close orders.
    """
    events = []
    symbol = state["symbol"]
    # Lock the wheel file for the duration of this read-modify-write
    lock_path = wheel_state_path(user, symbol)
    with _WheelLock(lock_path):
        return _advance_wheel_state_locked(user, state, events)


def _advance_wheel_state_locked(user, state, events):
    """Inner — runs while holding the lock. Factored out to keep indentation sane."""
    symbol = state["symbol"]
    stage = state.get("stage", "stage_1_searching")
    contract_meta = state.get("active_contract") or {}
    contract_sym = contract_meta.get("contract_symbol")

    # Helpers
    def _update_totals_on_premium_fill(fill_price, qty):
        """When our sell-to-open fills, record the premium.

        Phase-4 migration: the accumulators `total_premium_collected` and
        `total_realized_pnl` compound across every leg of every cycle
        across the lifetime of a wheel position. Float drift here is
        the single biggest source of dashboard/1099 mismatch on a
        multi-year wheel. Decimal math inside; float on state persist.
        """
        received_d = _dec(fill_price) * _dec(100) * _dec(qty)
        prev_premium_d = _dec(state.get("total_premium_collected", 0))
        prev_pnl_d = _dec(state.get("total_realized_pnl", 0))
        state["total_premium_collected"] = _to_cents_float(prev_premium_d + received_d)
        state["total_realized_pnl"] = _to_cents_float(prev_pnl_d + received_d)
        state["active_contract"]["premium_received"] = _to_cents_float(received_d)
        state["active_contract"]["status"] = "active"

    def _fetch_order(order_id):
        return _api_get(user, f"/orders/{order_id}")

    # --- Check fill status of any pending open order ---
    if contract_meta.get("status") == "pending" and contract_meta.get("open_order_id"):
        order = _fetch_order(contract_meta["open_order_id"])
        if "error" not in order:
            status = order.get("status", "")
            if status == "filled":
                fill_price = float(order.get("filled_avg_price") or contract_meta.get("limit_price_used") or 0)
                qty = int(float(order.get("filled_qty") or contract_meta.get("quantity") or 1))
                _update_totals_on_premium_fill(fill_price, qty)
                log_history(state, f"{contract_meta['type']}_filled", {
                    "fill_price": fill_price,
                    "premium_received": state["active_contract"]["premium_received"],
                })
                events.append(f"{symbol}: {contract_meta['type']} filled @ ${fill_price:.2f} — premium ${state['active_contract']['premium_received']:.2f}")
            elif status in ("canceled", "expired", "rejected"):
                log_history(state, f"{contract_meta['type']}_open_order_{status}", {"order_id": order.get("id")})
                state["active_contract"] = None
                # Reset stage to searching/shares_owned based on context
                if stage == "stage_1_put_active":
                    state["stage"] = "stage_1_searching"
                elif stage == "stage_2_call_active":
                    state["stage"] = "stage_2_shares_owned"
                events.append(f"{symbol}: {contract_meta['type']} open order {status} — resetting")
                save_wheel_state(user, state)
                return events
            # Round-10: log-only handling for in-flight / transitional
            # states. Without this, values like partially_filled,
            # pending_new, accepted, done_for_day silently fell through
            # and the status stayed "pending" forever.
            elif status in ("partially_filled", "pending_new", "accepted",
                             "pending_cancel", "replaced", "done_for_day",
                             "pending_replace"):
                log_history(state, f"{contract_meta['type']}_pending", {
                    "status": status, "order_id": order.get("id"),
                })
                # Stay pending; next tick re-checks.
                events.append(f"{symbol}: {contract_meta['type']} open order status={status}, waiting")

    # --- If we have an active (filled) contract, check if we should buy-to-close or if assigned/expired ---
    if contract_meta.get("status") == "active":
        # Round-42: external-close detection. If the option contract is
        # NOT in Alpaca's positions but wheel_strategy still thinks it's
        # active, an external event closed it — most commonly the
        # protective native stop order firing (Alpaca-side fill), but
        # also covers manual closes from the Alpaca web UI.
        #
        # Only runs PRE-expiration. After the expiry date, the dedicated
        # assignment/expired-worthless branch below handles position
        # disappearance (assignment produces underlying shares; worthless
        # just leaves no trace). Running this check post-expiry would
        # mis-journal an assignment as an "external close".
        try:
            _exp_date = datetime.strptime(contract_meta["expiration"], "%Y-%m-%d").date()
        except Exception:
            _exp_date = None
        _pre_expiry = (_exp_date is None) or (date.today() <= _exp_date)
        _positions_now = _api_get(user, "/positions") if _pre_expiry else None
        if _pre_expiry and isinstance(_positions_now, list):
            _still_open = any(
                p.get("symbol") == contract_sym for p in _positions_now
            )
            if not _still_open:
                # Try to find the most recent fill for this contract so the
                # journal gets a real exit_price. Falls back to None if the
                # activities endpoint doesn't return anything.
                _exit_price = None
                try:
                    _acts = _api_get(
                        user,
                        f"/account/activities/FILL?symbol={contract_sym}",
                    )
                    if isinstance(_acts, list) and _acts:
                        _buys = [a for a in _acts if (a.get("side") or "").lower() == "buy"]
                        if _buys:
                            _exit_price = float(_buys[0].get("price") or 0) or None
                except Exception:
                    pass
                _prem_total = contract_meta.get("premium_received", 0)
                _qty = contract_meta.get("quantity", 1)
                if _exit_price is not None:
                    _close_cost = _exit_price * 100 * _qty
                    _net = _prem_total - _close_cost
                else:
                    _net = None
                log_history(state, f"{contract_meta['type']}_closed_externally", {
                    "exit_price": _exit_price,
                    "premium_received": _prem_total,
                    "net_premium": _net,
                    "hint": "position missing from Alpaca /positions — likely native stop order filled",
                })
                _journal_wheel_close(
                    user, contract_meta,
                    exit_price=_exit_price if _exit_price is not None else 0.0,
                    pnl=_net if _net is not None else 0,
                    exit_reason=(
                        f"{contract_meta['type']} closed externally "
                        f"(Alpaca native stop / manual close). "
                        f"Exit ~${_exit_price:.2f}" if _exit_price is not None
                        else f"{contract_meta['type']} closed externally (Alpaca native stop / manual close)"
                    ),
                )
                contract_meta["status"] = "closed_externally"
                contract_meta["closed_at"] = now_et().isoformat()
                if _exit_price is not None:
                    contract_meta["external_close_price"] = _exit_price
                if stage == "stage_1_put_active":
                    state["stage"] = "stage_1_searching"
                elif stage == "stage_2_call_active":
                    state["stage"] = "stage_2_shares_owned"
                state["active_contract"] = None
                events.append(
                    f"{symbol}: {contract_meta['type']} closed externally "
                    f"(position missing from Alpaca) — journaled + state reset"
                )
                save_wheel_state(user, state)
                return events

        # Check current mid-quote — can we buy-to-close at profit target?
        current_quote = get_option_quote(user, contract_sym)
        if current_quote:
            premium_originally = contract_meta.get("premium_received", 0) / 100.0 / contract_meta.get("quantity", 1)
            # Round-10: fall back to bid when ask is missing / zero
            # (deep-ITM, weekend snapshot, or illiquid near expiration).
            # Without this the 50%-profit close never triggered on
            # contracts that were deeply profitable (ask=0 means "nobody
            # wants to sell it to us" which implies deep OTM/profitable).
            current_ask = current_quote.get("ask") or 0
            if current_ask <= 0:
                current_ask = current_quote.get("bid") or 0.01
            # 50% profit = current ask is <= 50% of original premium
            if current_ask > 0 and premium_originally > 0 and current_ask <= premium_originally * (1 - PROFIT_CLOSE_PCT):
                # Buy to close
                close_payload = {
                    "symbol": contract_sym,
                    "qty": str(contract_meta.get("quantity", 1)),
                    "side": "buy",
                    "type": "limit",
                    "limit_price": f"{round(current_ask * 20) / 20:.2f}",
                    "time_in_force": "day",
                    "order_class": "simple",
                }
                close_resp = _api_post(user, "/orders", close_payload)
                if "id" in close_resp:
                    contract_meta["close_order_id"] = close_resp["id"]
                    contract_meta["status"] = "closing"
                    log_history(state, "buy_to_close_submitted", {
                        "ask": current_ask,
                        "limit_price": close_payload["limit_price"],
                        "profit_pct": round((1 - current_ask / premium_originally) * 100, 1),
                    })
                    events.append(f"{symbol}: submitted buy-to-close at {round((1 - current_ask/premium_originally)*100, 1)}% profit")
                    save_wheel_state(user, state)
                    return events

        # Check expiration — has the contract expired?
        try:
            exp = datetime.strptime(contract_meta["expiration"], "%Y-%m-%d").date()
        except Exception:
            exp = None

        if exp and date.today() > exp:
            # Contract expired — check if assigned by looking at position
            positions = _api_get(user, "/positions")
            has_shares = False
            share_qty = 0
            if isinstance(positions, list):
                for p in positions:
                    if p.get("symbol") == symbol:
                        has_shares = True
                        share_qty = int(float(p.get("qty") or 0))
                        break

            if stage == "stage_1_put_active":
                # Detect assignment by DELTA not presence: if shares increased
                # by >= 100 × contract qty compared to shares_at_open baseline,
                # the put was assigned. Otherwise shares were already there
                # (user's pre-existing holdings) or came from elsewhere.
                baseline = int(state.get("shares_at_open", 0) or 0)
                expected_delta = 100 * contract_meta.get("quantity", 1)
                share_delta = share_qty - baseline
                # Round-12 audit fix: guard against stock-split anomalies.
                # If the share delta is suspiciously larger than expected
                # (e.g., 2:1 split during the put window would show delta
                # ~200 on a qty=1 put — expected_delta=100 but double that),
                # do NOT auto-advance state with a potentially-wrong cost
                # basis.
                #
                # Round-13: auto-resolve if yfinance confirms a split
                # happened between put-open and now. If split ratio R
                # found, normalise baseline + expected_delta by R and
                # retry the assignment check. If no split found, FREEZE
                # state for manual reconciliation (existing behaviour).
                if share_delta >= expected_delta * 2:
                    split_ratio = _detect_split_since(
                        symbol, contract_meta.get("opened_at")
                    )
                    if split_ratio and split_ratio > 1.0:
                        # Auto-resolve: the split multiplied shares by
                        # ratio R, so baseline should now be baseline*R.
                        # The new expected_delta becomes expected_delta*R
                        # (our put covers contract_qty*100 pre-split
                        # shares, which are contract_qty*100*R post-split).
                        adj_baseline = int(round(baseline * split_ratio))
                        adj_expected = int(round(expected_delta * split_ratio))
                        adj_share_delta = share_qty - adj_baseline
                        log_history(state, "split_auto_resolved", {
                            "split_ratio": split_ratio,
                            "pre_split_baseline": baseline,
                            "post_split_baseline": adj_baseline,
                            "pre_split_expected_delta": expected_delta,
                            "post_split_expected_delta": adj_expected,
                            "observed_share_qty": share_qty,
                        })
                        events.append(
                            f"{symbol}: stock split detected (ratio {split_ratio:g}x) "
                            f"— auto-adjusting baseline {baseline}→{adj_baseline} "
                            f"and expected_delta {expected_delta}→{adj_expected}"
                        )
                        # Adjust state + contract fields so downstream
                        # code treats the post-split numbers as canonical.
                        state["shares_at_open"] = adj_baseline
                        contract_meta["quantity"] = int(round(
                            contract_meta.get("quantity", 1) * split_ratio
                        ))
                        baseline = adj_baseline
                        expected_delta = adj_expected
                        share_delta = adj_share_delta
                        # Fall through to the normal assignment-detection
                        # branches below using the adjusted numbers.
                    else:
                        log_history(state, "anomalous_share_delta_no_auto_advance", {
                            "shares_owned_now": share_qty,
                            "shares_at_open_baseline": baseline,
                            "expected_delta": expected_delta,
                            "actual_delta": share_delta,
                            "hint": "possible stock split, manual trade, or DRIP — "
                                    "yfinance saw NO recent split. Reconcile cost_basis "
                                    "+ cycles_completed by hand then clear this state.",
                        })
                        events.append(
                            f"{symbol}: WARN anomalous share delta ({share_delta} vs "
                            f"expected {expected_delta}). No split detected via "
                            f"yfinance. Wheel state FROZEN — check manually."
                        )
                        save_wheel_state(user, state)
                        return events
                if share_delta >= expected_delta:
                    # Put was assigned — attribute the new 100×qty shares to us.
                    # Cost basis = strike - premium per share (on ONLY the new shares).
                    # Phase-4 migration: Decimal in; 4dp quantized float out. Cost
                    # basis is used DOWNSTREAM in call-assignment PnL math, where
                    # float drift would compound. Keep the Decimal version around
                    # in a sidecar field so the subsequent computation can avoid
                    # re-converting via the persisted 4dp float.
                    strike_d = _dec(contract_meta["strike"])
                    qty_d = _dec(contract_meta.get("quantity", 1))
                    prem_per_share_d = _dec(contract_meta["premium_received"]) / _dec(100) / qty_d
                    cost_basis_d = strike_d - prem_per_share_d
                    state["shares_owned"] = expected_delta
                    # State serialisation: cost_basis stored as 4dp float for
                    # backward-compat with existing wheel files on disk.
                    state["cost_basis"] = float(cost_basis_d.quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_EVEN
                    ))
                    state["stage"] = "stage_2_shares_owned"
                    contract_meta["status"] = "assigned"
                    contract_meta["closed_at"] = now_et().isoformat()
                    log_history(state, "put_assigned", {
                        "shares_owned_now_total": share_qty,
                        "shares_at_open_baseline": baseline,
                        "assigned_shares": expected_delta,
                        "cost_basis": state["cost_basis"],
                        "strike": contract_meta["strike"],
                    })
                    _journal_wheel_close(
                        user, contract_meta,
                        exit_price=0.0,  # put settled via assignment, not traded back
                        pnl=contract_meta.get("premium_received", 0),
                        exit_reason=f"put assigned at strike ${contract_meta['strike']:.2f} — full premium kept",
                    )
                    state["active_contract"] = None
                    events.append(f"{symbol}: put ASSIGNED — now own {expected_delta} new shares @ cost basis ${state['cost_basis']:.2f}")
                else:
                    # Put expired worthless — keep premium, restart stage 1
                    contract_meta["status"] = "expired"
                    contract_meta["closed_at"] = now_et().isoformat()
                    log_history(state, "put_expired_worthless", {"premium_kept": contract_meta["premium_received"]})
                    _journal_wheel_close(
                        user, contract_meta,
                        exit_price=0.0,
                        pnl=contract_meta.get("premium_received", 0),
                        exit_reason="put expired worthless — full premium kept",
                    )
                    state["stage"] = "stage_1_searching"
                    state["active_contract"] = None
                    state["cycles_completed"] = state.get("cycles_completed", 0) + 1
                    events.append(f"{symbol}: put expired worthless — kept ${contract_meta['premium_received']:.2f} premium")

            elif stage == "stage_2_call_active":
                # Detect call assignment by DELTA: shares decreased by >= 100 × qty
                # compared to the count at call-open time.
                call_open_shares = int(contract_meta.get("shares_at_call_open", state.get("shares_owned", 0)) or 0)
                expected_called_away = 100 * contract_meta.get("quantity", 1)
                share_decrease = call_open_shares - share_qty
                if share_decrease >= expected_called_away:
                    # Call was assigned — shares called away.
                    # Phase-4: Decimal math so the stock-leg PnL doesn't drift
                    # when summed into the lifetime total across many cycles.
                    qty_called = contract_meta.get("quantity", 1)
                    stock_pnl_d = (
                        (_dec(contract_meta["strike"]) - _dec(state.get("cost_basis", 0)))
                        * _dec(100) * _dec(qty_called)
                    )
                    stock_pnl = _to_cents_float(stock_pnl_d)
                    prev_pnl_d = _dec(state.get("total_realized_pnl", 0))
                    state["total_realized_pnl"] = _to_cents_float(prev_pnl_d + stock_pnl_d)
                    contract_meta["status"] = "assigned"
                    contract_meta["closed_at"] = now_et().isoformat()
                    log_history(state, "call_assigned", {
                        "strike": contract_meta["strike"],
                        "cost_basis": state.get("cost_basis"),
                        "stock_pnl": stock_pnl,
                        "shares_before": call_open_shares,
                        "shares_after": share_qty,
                    })
                    _journal_wheel_close(
                        user, contract_meta,
                        exit_price=0.0,
                        pnl=contract_meta.get("premium_received", 0),
                        exit_reason=f"call assigned — shares called away at strike ${contract_meta['strike']:.2f}; stock P&L ${stock_pnl:.2f}",
                    )
                    state["shares_owned"] = max(0, state.get("shares_owned", 0) - expected_called_away)
                    if state["shares_owned"] == 0:
                        state["cost_basis"] = None
                    state["cycles_completed"] = state.get("cycles_completed", 0) + 1
                    state["stage"] = "stage_1_searching"  # Full rotation — ready for new put
                    state["active_contract"] = None
                    events.append(f"{symbol}: call ASSIGNED — {expected_called_away} shares called away @ ${contract_meta['strike']:.2f}. Stock P&L ${stock_pnl:.2f}. Cycle #{state['cycles_completed']} complete.")
                else:
                    # Call expired worthless — keep premium, can sell another call
                    contract_meta["status"] = "expired"
                    contract_meta["closed_at"] = now_et().isoformat()
                    log_history(state, "call_expired_worthless", {"premium_kept": contract_meta["premium_received"]})
                    _journal_wheel_close(
                        user, contract_meta,
                        exit_price=0.0,
                        pnl=contract_meta.get("premium_received", 0),
                        exit_reason="call expired worthless — full premium kept",
                    )
                    state["stage"] = "stage_2_shares_owned"
                    state["active_contract"] = None
                    events.append(f"{symbol}: call expired worthless — kept ${contract_meta['premium_received']:.2f}, will sell another call")

    # --- If close order was submitted, check if it filled ---
    if contract_meta.get("status") == "closing" and contract_meta.get("close_order_id"):
        close_order = _fetch_order(contract_meta["close_order_id"])
        if "error" not in close_order and close_order.get("status") == "filled":
            close_price = float(close_order.get("filled_avg_price") or 0)
            # Phase-4 migration: close_cost and net_premium compound into
            # total_realized_pnl every time we roll or close a leg. Decimal
            # prevents drift across multi-year wheel lifetimes.
            close_cost_d = _dec(close_price) * _dec(100) * _dec(contract_meta.get("quantity", 1))
            close_cost = _to_cents_float(close_cost_d)
            net_premium_d = _dec(contract_meta.get("premium_received", 0)) - close_cost_d
            net_premium = _to_cents_float(net_premium_d)
            prev_pnl_d = _dec(state.get("total_realized_pnl", 0))
            state["total_realized_pnl"] = _to_cents_float(prev_pnl_d - close_cost_d)
            contract_meta["status"] = "closed"
            contract_meta["closed_at"] = now_et().isoformat()
            log_history(state, f"{contract_meta['type']}_bought_to_close", {
                "close_price": close_price,
                "close_cost": close_cost,
                "net_premium": net_premium,
            })
            _journal_wheel_close(
                user, contract_meta,
                exit_price=close_price,
                pnl=net_premium,
                exit_reason=f"{contract_meta['type']} bought-to-close @ ${close_price:.2f} (profit-target fill)",
            )
            # Transition stages
            if stage == "stage_1_put_active":
                state["stage"] = "stage_1_searching"
                state["cycles_completed"] = state.get("cycles_completed", 0) + 1
            elif stage == "stage_2_call_active":
                state["stage"] = "stage_2_shares_owned"
            state["active_contract"] = None
            events.append(f"{symbol}: closed {contract_meta['type']} for net ${net_premium:.2f} premium profit")
        elif ("error" not in close_order
              and close_order.get("status") in ("canceled", "expired", "rejected", "done_for_day")):
            # Round-10: close order didn't fill and is now terminal.
            # Reset status so next tick re-evaluates the 50%-profit
            # threshold and can submit a fresh BTC. Without this,
            # status="closing" stuck forever and the wheel stalled.
            contract_meta["status"] = "active"
            contract_meta.pop("close_order_id", None)
            log_history(state, f"{contract_meta['type']}_btc_unfilled", {
                "reason": close_order.get("status"),
            })
            events.append(f"{symbol}: BTC order {close_order.get('status')} — returning to active, will retry")

    save_wheel_state(user, state)
    return events


# ============================================================================
# SCREENER INTEGRATION
# ============================================================================
def find_wheel_candidates(picks_data, max_candidates=20):
    """From dashboard_data.json picks, filter to wheel-eligible candidates.
    Returns list of picks sorted by wheel_score, filtered by safety rules.
    """
    candidates = []
    for p in picks_data.get("picks", []):
        if p.get("best_strategy") != "Wheel Strategy":
            continue
        price = float(p.get("price") or 0)
        if price < MIN_STOCK_PRICE or price > MAX_STOCK_PRICE:
            continue
        if has_earnings_soon(p):
            continue
        # Prefer higher wheel_score / best_score
        candidates.append(p)
    candidates.sort(key=lambda x: x.get("wheel_score") or x.get("best_score") or 0, reverse=True)
    return candidates[:max_candidates]


if __name__ == "__main__":
    # Self-test: dry-run finding candidates against a dashboard_data.json
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DATA_DIR, "dashboard_data.json")
    data = _load_json(path) or {}
    cands = find_wheel_candidates(data)
    print(f"Wheel candidates from {path}: {len(cands)}")
    for c in cands:
        print(f"  {c.get('symbol'):<6} ${c.get('price',0):<8.2f} score={c.get('best_score',0):.1f} wheel_score={c.get('wheel_score',0)}")
    print(f"\nSafety rails:")
    print(f"  Price range: ${MIN_STOCK_PRICE}-${MAX_STOCK_PRICE}")
    print(f"  DTE: {MIN_DTE}-{MAX_DTE} (target {TARGET_DTE})")
    print(f"  Put strike: {PUT_STRIKE_PCT_BELOW*100:.0f}% OTM")
    print(f"  Min premium yield: {MIN_PREMIUM_PCT*100:.1f}% of strike")
    print(f"  Close at: {PROFIT_CLOSE_PCT*100:.0f}% profit")
    print(f"  Max concurrent: {MAX_CONCURRENT_WHEELS}")
    print(f"  Earnings avoidance: {EARNINGS_AVOID_DAYS} days")
