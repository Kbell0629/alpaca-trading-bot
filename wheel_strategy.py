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

# File locking — used to prevent races between wheel monitor ticks and
# human-triggered actions (e.g. Force Deploy). Unix only; on Windows this
# falls through to a no-op lock.
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

class _WheelLock:
    """Context manager that flock()s a wheel state file during read-modify-write."""
    def __init__(self, path):
        self.path = path
        self.fh = None
    def __enter__(self):
        if not _HAS_FCNTL:
            return self
        try:
            self.fh = open(self.path + ".lock", "w")
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            if self.fh:
                try: self.fh.close()
                except Exception: pass
                self.fh = None
        return self
    def __exit__(self, *a):
        if self.fh:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()
            except Exception:
                pass

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
    state["updated"] = datetime.now(timezone.utc).isoformat()
    _save_json(wheel_state_path(user, state["symbol"]), state)


def log_history(state, event, detail=None):
    """Append a timestamped event to the wheel's audit trail. Caps at HISTORY_MAX
    entries to prevent unbounded growth over months of monitoring."""
    hist = state.setdefault("history", [])
    hist.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "stage": state.get("stage"),
        "detail": detail,
    })
    if len(hist) > HISTORY_MAX:
        state["history"] = hist[-HISTORY_MAX:]


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


def score_contract(contract, quote, target_strike, current_price, opt_type):
    """Composite score: favor near-target strike, target-DTE, premium yield, liquidity.
    Higher is better. Returns None if contract should be rejected outright.
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

    # Distance from target strike (closer = better)
    dist_pct = abs(strike - target_strike) / current_price if current_price else 1.0
    dist_score = max(0.0, 10.0 - dist_pct * 100)

    # DTE score (peaks at TARGET_DTE)
    dte_score = max(0.0, 10.0 - abs(dte - TARGET_DTE) / 3.0)

    # Premium yield score (higher = better, but cap at 5% = full points)
    yield_score = min(10.0, premium_pct * 200)  # 0.005 -> 1.0, 0.05 -> 10.0

    # Liquidity bonus
    liq_score = min(5.0, oi / 200.0)

    return round(dist_score + dte_score + yield_score + liq_score, 2)


def find_best_contract(user, symbol, opt_type, target_strike, current_price):
    """Pick the highest-scoring contract for the sell-to-open.
    Returns {contract, quote, score} or None if nothing viable.
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
        # Only look at strikes within ~20% of target to limit quote calls
        if abs(strike - target_strike) / current_price > 0.10:
            continue

        quote = get_option_quote(user, c["symbol"])
        if not quote:
            continue
        score = score_contract(c, quote, target_strike, current_price, opt_type)
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

    # Concurrent-wheel cap
    if count_active_wheels(user) >= MAX_CONCURRENT_WHEELS:
        return False, f"Already at max {MAX_CONCURRENT_WHEELS} concurrent wheels", None

    # Find best put contract
    target_strike = current_price * (1 - PUT_STRIKE_PCT_BELOW)
    best = find_best_contract(user, symbol, "put", target_strike, current_price)
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
    now_iso = datetime.now(timezone.utc).isoformat()

    state = dict(WHEEL_STATE_TEMPLATE)
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
    now_iso = datetime.now(timezone.utc).isoformat()

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
        """When our sell-to-open fills, record the premium."""
        received = round(fill_price * 100 * qty, 2)
        state["total_premium_collected"] = round(state.get("total_premium_collected", 0) + received, 2)
        state["total_realized_pnl"] = round(state.get("total_realized_pnl", 0) + received, 2)
        state["active_contract"]["premium_received"] = received
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

    # --- If we have an active (filled) contract, check if we should buy-to-close or if assigned/expired ---
    if contract_meta.get("status") == "active":
        # Check current mid-quote — can we buy-to-close at profit target?
        current_quote = get_option_quote(user, contract_sym)
        if current_quote:
            premium_originally = contract_meta.get("premium_received", 0) / 100.0 / contract_meta.get("quantity", 1)
            current_ask = current_quote["ask"]
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
                if share_delta >= expected_delta:
                    # Put was assigned — attribute the new 100×qty shares to us
                    # Cost basis = strike - premium per share (on ONLY the new shares)
                    cost_basis = contract_meta["strike"] - (contract_meta["premium_received"] / 100 / contract_meta.get("quantity", 1))
                    state["shares_owned"] = expected_delta  # only count the newly-assigned shares
                    state["cost_basis"] = round(cost_basis, 4)
                    state["stage"] = "stage_2_shares_owned"
                    contract_meta["status"] = "assigned"
                    contract_meta["closed_at"] = datetime.now(timezone.utc).isoformat()
                    log_history(state, "put_assigned", {
                        "shares_owned_now_total": share_qty,
                        "shares_at_open_baseline": baseline,
                        "assigned_shares": expected_delta,
                        "cost_basis": state["cost_basis"],
                        "strike": contract_meta["strike"],
                    })
                    state["active_contract"] = None
                    events.append(f"{symbol}: put ASSIGNED — now own {expected_delta} new shares @ cost basis ${state['cost_basis']:.2f}")
                else:
                    # Put expired worthless — keep premium, restart stage 1
                    contract_meta["status"] = "expired"
                    contract_meta["closed_at"] = datetime.now(timezone.utc).isoformat()
                    log_history(state, "put_expired_worthless", {"premium_kept": contract_meta["premium_received"]})
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
                    # Call was assigned — shares called away
                    qty_called = contract_meta.get("quantity", 1)
                    stock_pnl = (contract_meta["strike"] - state.get("cost_basis", 0)) * 100 * qty_called
                    state["total_realized_pnl"] = round(state.get("total_realized_pnl", 0) + stock_pnl, 2)
                    contract_meta["status"] = "assigned"
                    contract_meta["closed_at"] = datetime.now(timezone.utc).isoformat()
                    log_history(state, "call_assigned", {
                        "strike": contract_meta["strike"],
                        "cost_basis": state.get("cost_basis"),
                        "stock_pnl": stock_pnl,
                        "shares_before": call_open_shares,
                        "shares_after": share_qty,
                    })
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
                    contract_meta["closed_at"] = datetime.now(timezone.utc).isoformat()
                    log_history(state, "call_expired_worthless", {"premium_kept": contract_meta["premium_received"]})
                    state["stage"] = "stage_2_shares_owned"
                    state["active_contract"] = None
                    events.append(f"{symbol}: call expired worthless — kept ${contract_meta['premium_received']:.2f}, will sell another call")

    # --- If close order was submitted, check if it filled ---
    if contract_meta.get("status") == "closing" and contract_meta.get("close_order_id"):
        close_order = _fetch_order(contract_meta["close_order_id"])
        if "error" not in close_order and close_order.get("status") == "filled":
            close_price = float(close_order.get("filled_avg_price") or 0)
            close_cost = close_price * 100 * contract_meta.get("quantity", 1)
            # Realized premium profit = original premium - close cost
            net_premium = contract_meta.get("premium_received", 0) - close_cost
            state["total_realized_pnl"] = round(state.get("total_realized_pnl", 0) - close_cost, 2)
            contract_meta["status"] = "closed"
            contract_meta["closed_at"] = datetime.now(timezone.utc).isoformat()
            log_history(state, f"{contract_meta['type']}_bought_to_close", {
                "close_price": close_price,
                "close_cost": close_cost,
                "net_premium": net_premium,
            })
            # Transition stages
            if stage == "stage_1_put_active":
                state["stage"] = "stage_1_searching"
                state["cycles_completed"] = state.get("cycles_completed", 0) + 1
            elif stage == "stage_2_call_active":
                state["stage"] = "stage_2_shares_owned"
            state["active_contract"] = None
            events.append(f"{symbol}: closed {contract_meta['type']} for net ${net_premium:.2f} premium profit")

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
