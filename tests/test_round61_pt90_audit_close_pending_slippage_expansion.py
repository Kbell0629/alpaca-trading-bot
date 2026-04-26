"""Round-61 pt.90 — audit close_pending detection + slippage
wiring expansion.

Two fixes in one PR.

1. Audit's HIGH `missing_stop` finding fires whenever a position
   has no STOP-type order. But if the user clicked Close (or
   the bot is closing for some other reason), a pending market /
   limit order on the position-closing side is in flight that
   will zero the position at next fill — no stop needed.
   Pt.90 detects that pending close and downgrades the finding
   from HIGH `missing_stop` to MEDIUM `close_pending` with a
   message that explains the situation.

2. Pt.84 wired slippage_tracker into the target_hit close path
   only. Pt.90 extends the wiring to bearish_news close (pt.71)
   and dead_money close (pt.59) so the slippage_summary panel
   populates from those exits too.
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent


# ============================================================================
# Audit close_pending detection
# ============================================================================

def test_audit_close_pending_downgrades_missing_stop():
    """Position with no stop but pending market close → MEDIUM
    close_pending, NOT HIGH missing_stop."""
    import audit_core
    positions = [{
        "symbol": "SOXL", "qty": "-29",
        "current_price": "100.0",
        "asset_class": "us_equity",
    }]
    orders = [{
        "symbol": "SOXL", "side": "buy", "type": "market",
        "qty": "29", "status": "accepted",
    }]
    report = audit_core.run_audit(
        positions=positions, orders=orders,
        strategy_files={}, journal={},
        scorecard={"updated": "2026-04-26T00:00:00"})
    findings = report["findings"]
    cats = [f["category"] for f in findings]
    assert "close_pending" in cats
    assert "missing_stop" not in cats
    cp = next(f for f in findings if f["category"] == "close_pending")
    assert cp["severity"] == "MEDIUM"
    assert "next fill" in cp["message"]


def test_audit_missing_stop_still_fires_when_no_close_pending():
    """Position with no stop AND no pending close → HIGH missing_stop
    (original behaviour preserved)."""
    import audit_core
    positions = [{
        "symbol": "AAPL", "qty": "10",
        "current_price": "180.0",
        "asset_class": "us_equity",
    }]
    orders = []   # no orders at all
    report = audit_core.run_audit(
        positions=positions, orders=orders,
        strategy_files={}, journal={},
        scorecard={"updated": "2026-04-26T00:00:00"})
    findings = report["findings"]
    cats = [f["category"] for f in findings]
    assert "missing_stop" in cats


def test_audit_close_pending_recognizes_limit_orders_too():
    """xh_close pre-market routing posts a LIMIT order, not market.
    Pt.90 recognises both as 'pending close'."""
    import audit_core
    positions = [{
        "symbol": "AAPL", "qty": "10",
        "current_price": "180.0",
        "asset_class": "us_equity",
    }]
    orders = [{
        "symbol": "AAPL", "side": "sell", "type": "limit",
        "qty": "10", "status": "accepted",
        "limit_price": "178.00",
    }]
    report = audit_core.run_audit(
        positions=positions, orders=orders,
        strategy_files={}, journal={},
        scorecard={"updated": "2026-04-26T00:00:00"})
    findings = report["findings"]
    cats = [f["category"] for f in findings]
    assert "close_pending" in cats


def test_audit_partial_close_does_NOT_downgrade():
    """If the pending order is for LESS than the full position, a
    stop is still needed for the residual exposure."""
    import audit_core
    positions = [{
        "symbol": "AAPL", "qty": "10",
        "current_price": "180.0",
        "asset_class": "us_equity",
    }]
    orders = [{
        "symbol": "AAPL", "side": "sell", "type": "market",
        "qty": "5", "status": "accepted",  # only half!
    }]
    report = audit_core.run_audit(
        positions=positions, orders=orders,
        strategy_files={}, journal={},
        scorecard={"updated": "2026-04-26T00:00:00"})
    findings = report["findings"]
    cats = [f["category"] for f in findings]
    assert "missing_stop" in cats
    assert "close_pending" not in cats


def test_audit_existing_stop_leaves_audit_silent():
    """Position WITH a stop → no missing_stop, no close_pending."""
    import audit_core
    positions = [{
        "symbol": "AAPL", "qty": "10",
        "current_price": "180.0",
        "asset_class": "us_equity",
    }]
    orders = [{
        "symbol": "AAPL", "side": "sell", "type": "stop",
        "qty": "10", "stop_price": "170.0",
        "status": "new",
    }]
    report = audit_core.run_audit(
        positions=positions, orders=orders,
        strategy_files={}, journal={},
        scorecard={"updated": "2026-04-26T00:00:00"})
    findings = report["findings"]
    cats = [f["category"] for f in findings]
    assert "missing_stop" not in cats
    assert "close_pending" not in cats


# ============================================================================
# Slippage wiring expansion (source-pin)
# ============================================================================

def test_bearish_news_close_passes_fill_prices():
    src = (_HERE / "cloud_scheduler.py").read_text()
    idx = src.find('"bearish_news"')
    assert idx > 0
    block = src[idx:idx + 1500]
    assert "entry_filled_price=" in block
    assert "exit_filled_price=" in block


def test_dead_money_close_passes_fill_prices():
    src = (_HERE / "cloud_scheduler.py").read_text()
    # Pt.90 marker confirms the wiring landed in the dead_money close
    # (file has multiple "dead_money" strings; the marker pinpoints
    # the close-flow site).
    assert "pt.90: slippage wiring for dead_money" in src
    # The kwargs land within the close call.
    idx = src.find("pt.90: slippage wiring for dead_money")
    block = src[idx:idx + 1500]
    assert "entry_filled_price=" in block
    assert "exit_filled_price=" in block


def test_target_hit_still_passes_fill_prices_pt84():
    """Pt.84 regression guard: target_hit close still passes both
    fill kwargs."""
    src = (_HERE / "cloud_scheduler.py").read_text()
    idx = src.find('"target_hit", qty=shares, side="sell"')
    assert idx > 0
    block = src[idx:idx + 600]
    assert "entry_filled_price=" in block
    assert "exit_filled_price=" in block
