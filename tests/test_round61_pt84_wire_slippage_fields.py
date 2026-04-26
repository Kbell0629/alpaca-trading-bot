"""Round-61 pt.84 — wire slippage_tracker into the fill path.

Pt.80 shipped the slippage_tracker module + the Analytics Hub
`slippage_summary` key, but no call site populated the journal's
slippage fields, so the panel returned an empty aggregate.
Pt.84 closes that loop:

  1. The auto-deployer's journal append now records
     `entry_expected_price` (the screener-time price).
  2. `record_trade_close` accepts new `entry_filled_price` and
     `exit_filled_price` kwargs; when supplied alongside the
     stored expected price, it computes signed slippage_bps via
     `slippage_tracker.compute_slippage_bps` and writes the four
     slippage fields back onto the closed journal entry.
  3. The target-hit close path passes the real Alpaca fill prices
     through (entry = position avg_entry_price; exit = close
     order's filled_avg_price).

Once trades close through that path, the Analytics Hub's
`slippage_summary` panel (pt.80) shows actual realized slippage
vs the 10-bps backtest assumption.
"""
from __future__ import annotations

import json
import os
import tempfile


# ============================================================================
# record_trade_close: new kwargs persist slippage fields
# ============================================================================

def _make_user_with_open_trade(monkeypatch, *, expected=100.0,
                                  qty=10, strategy="breakout"):
    """Create a tmp-isolated user with one OPEN journal entry that
    has `entry_expected_price` populated (mimicking the pt.84
    auto-deployer journal append)."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "a" * 64)
    tmp = tempfile.mkdtemp(prefix="pt84-")
    monkeypatch.setenv("DATA_DIR", tmp)
    # Pop cached modules so they bind to the new DATA_DIR.
    import sys
    for m in ("auth", "et_time", "constants", "cloud_scheduler"):
        sys.modules.pop(m, None)
    user_dir = os.path.join(tmp, "users", "1")
    os.makedirs(user_dir, exist_ok=True)
    journal_path = os.path.join(user_dir, "trade_journal.json")
    journal = {"trades": [{
        "timestamp": "2026-04-25T10:00:00",
        "symbol": "AAPL", "side": "buy", "qty": qty,
        "price": expected,
        "entry_expected_price": expected,
        "strategy": strategy, "status": "open",
    }]}
    with open(journal_path, "w") as fh:
        json.dump(journal, fh)
    user = {
        "id": 1, "username": "tester",
        "_data_dir": user_dir,
    }
    return user, journal_path


def test_record_trade_close_writes_entry_slippage(monkeypatch):
    """When `entry_filled_price` is passed alongside an open entry
    that has `entry_expected_price`, slippage_bps lands on the
    closed entry."""
    user, jpath = _make_user_with_open_trade(monkeypatch, expected=100.0)
    import cloud_scheduler as cs
    cs.record_trade_close(
        user, "AAPL", "breakout",
        exit_price=110.0, pnl=100.0,
        exit_reason="target_hit", qty=10, side="sell",
        entry_filled_price=100.05,   # 5 bps adverse for a buy
        exit_filled_price=110.0,
    )
    with open(jpath) as fh:
        j = json.load(fh)
    t = j["trades"][0]
    assert t["status"] == "closed"
    assert t["entry_filled_price"] == 100.05
    assert t["entry_slippage_bps"] == 5.0


def test_record_trade_close_writes_exit_slippage(monkeypatch):
    """When `exit_filled_price` is passed and `exit_price` is the
    expected/quote price, exit_slippage_bps lands on the entry."""
    user, jpath = _make_user_with_open_trade(monkeypatch, expected=100.0)
    import cloud_scheduler as cs
    cs.record_trade_close(
        user, "AAPL", "breakout",
        exit_price=110.0, pnl=100.0,
        exit_reason="target_hit", qty=10, side="sell",
        entry_filled_price=100.0,
        exit_filled_price=109.89,   # received less on sell → adverse
    )
    with open(jpath) as fh:
        j = json.load(fh)
    t = j["trades"][0]
    assert t["exit_filled_price"] == 109.89
    assert t["exit_slippage_bps"] == 10.0
    assert t["exit_expected_price"] == 110.0


def test_record_trade_close_skips_slippage_when_data_missing(monkeypatch):
    """No fill_price kwargs → no slippage fields. Old call sites
    keep working unchanged."""
    user, jpath = _make_user_with_open_trade(monkeypatch, expected=100.0)
    import cloud_scheduler as cs
    cs.record_trade_close(
        user, "AAPL", "breakout",
        exit_price=110.0, pnl=100.0,
        exit_reason="target_hit", qty=10, side="sell",
    )
    with open(jpath) as fh:
        j = json.load(fh)
    t = j["trades"][0]
    assert "entry_slippage_bps" not in t
    assert "exit_slippage_bps" not in t


def test_record_trade_close_short_close_polarity(monkeypatch):
    """For a short close (side='buy' on the BUY-to-cover), buying
    HIGHER than expected is adverse → positive bps."""
    user, jpath = _make_user_with_open_trade(
        monkeypatch, expected=100.0, strategy="short_sell")
    import cloud_scheduler as cs
    cs.record_trade_close(
        user, "AAPL", "short_sell",
        exit_price=90.0, pnl=100.0,
        exit_reason="target_hit", qty=-10, side="buy",
        entry_filled_price=99.95,   # short entry: sold at 99.95 vs expected 100
        exit_filled_price=90.10,    # short exit: bought at 90.10 vs expected 90
    )
    with open(jpath) as fh:
        j = json.load(fh)
    t = j["trades"][0]
    # Short entry side = sell. Sell at 99.95 vs expected 100 → bad.
    assert t["entry_slippage_bps"] > 0
    # Short close = buy. Bought at 90.10 vs expected 90 → bad.
    assert t["exit_slippage_bps"] > 0


# ============================================================================
# Source-pin: auto-deployer adds entry_expected_price to journal append
# ============================================================================

def test_auto_deployer_journal_append_stores_entry_expected_price():
    """Pt.84: the journal entry now carries `entry_expected_price`
    so the close path can compute slippage_bps."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    idx = src.find('"_screener_score"')
    assert idx > 0
    block = src[idx:idx + 1500]
    assert '"entry_expected_price"' in block


def test_record_trade_close_signature_has_fill_kwargs():
    """Pt.84: signature accepts entry_filled_price + exit_filled_price."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    sig_idx = src.find("def record_trade_close")
    sig_block = src[sig_idx:sig_idx + 500]
    assert "entry_filled_price" in sig_block
    assert "exit_filled_price" in sig_block


def test_target_hit_close_passes_fill_prices():
    """Source-pin: at least one close call site passes the new
    fill-price kwargs through."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    # Look for the target_hit close that passes both kwargs.
    idx = src.find('"target_hit", qty=shares, side="sell",')
    assert idx > 0
    block = src[idx:idx + 600]
    assert "entry_filled_price=" in block
    assert "exit_filled_price=" in block


def test_close_path_documents_pt84():
    """Docstring on record_trade_close mentions pt.84 + slippage."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    sig_idx = src.find("def record_trade_close")
    block = src[sig_idx:sig_idx + 2500]
    assert "pt.84" in block
    assert "slippage" in block.lower()


# ============================================================================
# Pure-module discipline: slippage_tracker still has no top-level imports
# of cloud_scheduler / auth / server (regression guard for pt.80).
# ============================================================================

def test_slippage_tracker_still_pure():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "slippage_tracker.py").read_text()
    forbidden = ("import cloud_scheduler", "from cloud_scheduler",
                  "import auth\n", "from auth import")
    for f in forbidden:
        assert f not in src
