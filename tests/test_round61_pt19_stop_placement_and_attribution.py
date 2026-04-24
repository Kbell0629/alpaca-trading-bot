"""Round-61 pt.19 — fixes for 3 user-reported issues after pt.17/18 deploy:

1. **SOXL adopted but no BUY stop at Alpaca.** The monitor's initial
   cover-stop placement in `process_short_strategy` used
   `entry * (1 + stop_pct)` which placed the stop BELOW current
   market for an already-underwater short (SOXL entry $110.65, stop
   $121.72, current $129.13 — Alpaca rejects). Fix: mirror the
   error_recovery adaptive formula so the monitor's initial stop is
   always on the protective side of current price.

2. **HIMS short put still MANUAL after "Adopt MANUAL → AUTO" click.**
   The grace-period filter in `error_recovery.main()` skipped HIMS
   because the user had a pre-existing BUY stop at $2.35 — the
   filter treated that as an "in-progress entry" and aborted
   orphan adoption. Fix: exempt shorts and OCC options from the
   grace-period filter since their BUY orders are always risk-mgmt,
   never entries.

3. **Performance attribution silently drops short_sell trades.**
   `STRATEGY_BUCKETS` in scorecard_core.py listed
   trailing_stop/copy_trading/wheel/mean_reversion/breakout/pead but
   NOT short_sell. `build_strategy_breakdown` skips any trade whose
   strategy doesn't match a bucket, so every closed short trade
   silently vanished from the scorecard.
"""
from __future__ import annotations

import json


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# Fix 1: monitor short-cover-stop is adaptive
# ----------------------------------------------------------------------------

def test_short_cover_stop_uses_adaptive_formula_in_monitor():
    """The initial cover-stop placement in process_short_strategy
    must use max(entry*(1+pct), current*1.05), not the flat
    entry-based formula."""
    src = _src("cloud_scheduler.py")
    # Find the short cover-stop placement block.
    idx = src.find("Place initial stop-buy (cover) ABOVE entry")
    assert idx > 0, "Short cover-stop placement block missing"
    block = src[idx:idx + 1500]
    # Pin the adaptive formula bits.
    assert "max(entry_stop, current_stop)" in block, (
        "Monitor's initial short cover-stop must use "
        "max(entry_stop, current_stop) to avoid placing below current "
        "market on an already-underwater short.")
    assert "price * 1.05" in block
    # The old bare formula must be gone.
    assert "stop_price = round(entry * (1 + stop_pct), 2)" not in block, (
        "Old flat formula `entry * (1 + stop_pct)` must be replaced — "
        "it rejects on underwater shorts.")


def test_long_initial_stop_uses_adaptive_formula_in_monitor():
    """Same bug on the long side for a position that's underwater
    before the monitor first runs (e.g. gapped down overnight).
    min(entry*(1-pct), current*0.95) so the stop is always below
    current."""
    src = _src("cloud_scheduler.py")
    idx = src.find("Place initial stop (regular-hours only")
    assert idx > 0
    block = src[idx:idx + 1500]
    assert "min(entry_stop, current_stop)" in block, (
        "Long side must also use the adaptive formula.")
    assert "price * 0.95" in block
    assert "stop_price = round(entry * (1 - stop_pct), 2)" not in block


# ----------------------------------------------------------------------------
# Fix 2: grace-period filter exempts shorts + OCC
# ----------------------------------------------------------------------------

def test_grace_period_filter_exempts_shorts(tmp_path, monkeypatch):
    """A short position with a pre-existing BUY order (e.g. a user-
    placed protective stop) must NOT be skipped by the grace-period
    filter — those buys are risk-mgmt, not entries."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    monkeypatch.setattr(er, "DATA_DIR", str(tmp_path))

    # Short position + a pre-existing BUY stop order on the same symbol.
    positions = [{
        "symbol": "SOXL", "qty": "-29",
        "avg_entry_price": "110.65", "current_price": "129.13",
    }]
    orders = [{
        "symbol": "SOXL", "side": "buy", "type": "stop",
        "stop_price": "135.00", "id": "pre-existing",
    }]

    def _fake_get(url, timeout=15, max_retries=3):
        if "positions" in url:
            return positions
        if "orders" in url:
            return orders
        return []
    monkeypatch.setattr(er, "api_get_with_retry", _fake_get)
    monkeypatch.setattr(er, "api_post", lambda *a, **kw: {"id": "fake"})

    er.main()

    # Adoption should have proceeded despite the BUY stop.
    assert (sdir / "short_sell_SOXL.json").exists(), (
        "Shorts must bypass the grace-period filter — a BUY order on a "
        "short symbol is risk-mgmt, not an in-progress entry.")


def test_grace_period_filter_exempts_occ_options(tmp_path, monkeypatch):
    """Short put with a pre-existing BUY stop (user's manual risk-mgmt)
    must still get adopted to wheel_<UNDERLYING>.json."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    monkeypatch.setattr(er, "DATA_DIR", str(tmp_path))

    positions = [{
        "symbol": "HIMS260508P00027000", "qty": "-1",
        "avg_entry_price": "2.05", "current_price": "0.88",
    }]
    orders = [{
        "symbol": "HIMS260508P00027000", "side": "buy", "type": "stop",
        "stop_price": "2.35", "id": "user-placed",
    }]

    def _fake_get(url, timeout=15, max_retries=3):
        if "positions" in url:
            return positions
        if "orders" in url:
            return orders
        return []
    monkeypatch.setattr(er, "api_get_with_retry", _fake_get)
    monkeypatch.setattr(er, "api_post", lambda *a, **kw: {"id": "fake"})

    er.main()

    assert (sdir / "wheel_HIMS.json").exists(), (
        "OCC option orphans must bypass the grace-period filter so "
        "user-placed risk stops don't lock the bot out of adoption.")
    wheel = json.loads((sdir / "wheel_HIMS.json").read_text())
    assert wheel["stage"] == "stage_1_put_active"
    assert wheel["active_contract"]["contract_symbol"] == "HIMS260508P00027000"


def test_grace_period_still_blocks_long_orphans_with_pending_buys(tmp_path, monkeypatch):
    """Sanity check: the exemption is ONLY for shorts + OCC. A long
    orphan with a pending BUY order should still be skipped (that's
    the bot mid-entry)."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    monkeypatch.setattr(er, "DATA_DIR", str(tmp_path))

    positions = [{
        "symbol": "XYZ", "qty": "100",
        "avg_entry_price": "50.00", "current_price": "51.00",
    }]
    orders = [{
        "symbol": "XYZ", "side": "buy", "type": "market",
        "id": "in-progress-entry",
    }]

    def _fake_get(url, timeout=15, max_retries=3):
        if "positions" in url:
            return positions
        if "orders" in url:
            return orders
        return []
    monkeypatch.setattr(er, "api_get_with_retry", _fake_get)
    monkeypatch.setattr(er, "api_post", lambda *a, **kw: {"id": "fake"})

    er.main()

    # Long orphan with pending buy → still skipped.
    assert not (sdir / "trailing_stop_XYZ.json").exists()


def test_error_recovery_grace_period_source_pin():
    """Source-level pin that the exemption logic is present."""
    src = _src("error_recovery.py")
    assert "is_short = qty_f < 0" in src
    assert "_is_occ_option_symbol(sym)" in src
    # And the exemption must gate the filter, not just be present.
    idx = src.find("Grace period: skip if there are pending buy orders")
    assert idx > 0
    block = src[idx:idx + 1200]
    assert "if not is_short and not is_option" in block, (
        "Grace-period filter must be gated behind `not is_short and "
        "not is_option` so shorts and OCC options bypass it.")


# ----------------------------------------------------------------------------
# Fix 3: short_sell included in STRATEGY_BUCKETS
# ----------------------------------------------------------------------------

def test_strategy_buckets_includes_short_sell():
    import scorecard_core as sc
    assert "short_sell" in sc.STRATEGY_BUCKETS, (
        "short_sell was silently dropped from performance attribution "
        "because it wasn't in STRATEGY_BUCKETS. Add it so closed short "
        "trades actually appear in the scorecard breakdown.")


def test_build_strategy_breakdown_counts_short_sell_trades():
    """Given a closed short_sell trade in the journal, the
    breakdown must include a short_sell bucket with the P&L."""
    from scorecard_core import build_strategy_breakdown
    trades = [
        {"strategy": "short_sell", "status": "closed", "pnl": -50.00},
        {"strategy": "short_sell", "status": "closed", "pnl": 120.00},
        {"strategy": "breakout", "status": "closed", "pnl": 80.00},
    ]
    buckets = build_strategy_breakdown(trades)
    assert "short_sell" in buckets
    assert buckets["short_sell"]["trades"] == 2
    assert buckets["short_sell"]["wins"] == 1  # the +120 trade
    assert abs(buckets["short_sell"]["pnl"] - 70.00) < 0.01  # -50 + 120


def test_build_strategy_breakdown_handles_short_sell_alongside_others():
    """Mixed-strategy journal: all buckets present and totals correct."""
    from scorecard_core import build_strategy_breakdown
    trades = [
        {"strategy": "breakout", "status": "closed", "pnl": 100.00},
        {"strategy": "breakout", "status": "closed", "pnl": -30.00},
        {"strategy": "wheel", "status": "closed", "pnl": 25.00},
        {"strategy": "short_sell", "status": "closed", "pnl": -100.00},
        {"strategy": "short_sell", "status": "closed", "pnl": 200.00},
    ]
    buckets = build_strategy_breakdown(trades)
    assert buckets["breakout"]["trades"] == 2
    assert buckets["wheel"]["trades"] == 1
    assert buckets["short_sell"]["trades"] == 2
    assert buckets["short_sell"]["wins"] == 1
    assert abs(buckets["short_sell"]["pnl"] - 100.00) < 0.01
