"""Round-61 pt.17 — adaptive stops + OCC-option orphan adoption.

Two bugs exposed by pt.15 + pt.16 shipping:

1. **Stops placed below market price.** After SOXL (short, entry
   $110.65) moved against the user to $129.31, error_recovery's
   cookie-cutter `stop = entry * 1.10` produced $121.72 — a BUY stop
   BELOW current market, which Alpaca rejects. Result: position
   adopted, file created, no protective stop ever placed.

2. **OCC option orphans mis-handled.** User's HIMS short put (OCC
   `HIMS260508P00027000`) went through `create_orphan_strategy` →
   `short_sell_HIMS260508P00027000.json`. monitor_strategies'
   short-sell path assumes equity tickers + share quantities, not
   options contracts. And the dashboard kept showing MANUAL because
   the wheel monitor wasn't managing it.

Fixes:
  1. `create_orphan_strategy` adaptive stop — for short use
     `max(entry*1.10, current*1.05)`, for long use
     `min(entry*0.90, current*0.95)`, so the stop is always on the
     protective side of current market price.
  2. OCC orphans route to a new branch: create `wheel_<UNDERLYING>.json`
     in `stage_1_put_active` (short put) or `stage_2_call_active`
     (covered call), populate `active_contract` from the OCC parse.
     Long options are skipped (no strategy exists for long premium).
  3. New helper `_occ_parse(sym)` returns
     `{underlying, expiration, right, strike}` from an OCC symbol.
"""
from __future__ import annotations

import json


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# _occ_parse helper
# ----------------------------------------------------------------------------

def test_occ_parse_returns_structured_fields():
    import error_recovery as er
    p = er._occ_parse("HIMS260508P00027000")
    assert p == {
        "underlying": "HIMS",
        "expiration": "2026-05-08",
        "right": "put",
        "strike": 27.0,
    }


def test_occ_parse_handles_call():
    import error_recovery as er
    p = er._occ_parse("AAPL260619C00200000")
    assert p["underlying"] == "AAPL"
    assert p["right"] == "call"
    assert p["strike"] == 200.0
    assert p["expiration"] == "2026-06-19"


def test_occ_parse_returns_none_on_non_occ():
    import error_recovery as er
    assert er._occ_parse("AAPL") is None
    assert er._occ_parse("") is None
    assert er._occ_parse(None) is None
    assert er._occ_parse("HIMS260508X00027000") is None  # X isn't C or P


# ----------------------------------------------------------------------------
# Adaptive stops
# ----------------------------------------------------------------------------

def test_short_orphan_underwater_stop_is_above_current_price():
    """SOXL-style bug: entry $110.65, current $129.31. The stop
    MUST be above current ($135.78 = $129.31*1.05), not at $121.72
    (entry*1.10) which Alpaca would reject."""
    import error_recovery as er
    strat = er.create_orphan_strategy("SOXL", -29, 129.31, 110.65)
    stop = strat["state"]["current_stop_price"]
    assert stop > 129.31, (
        f"Short stop must be ABOVE current price for the buy-stop "
        f"to protect, got {stop} with current=$129.31")
    # Should be approximately current * 1.05 = 135.78
    assert 135 <= stop <= 137, f"Expected ~$135.78, got {stop}"


def test_short_orphan_fresh_position_uses_entry_based_stop():
    """Fresh short (current ≈ entry): tighter entry*1.10 wins over
    current*1.05 since max() picks the higher."""
    import error_recovery as er
    strat = er.create_orphan_strategy("SOXL", -29, 110.00, 110.65)
    stop = strat["state"]["current_stop_price"]
    # entry*1.10 = 121.72; current*1.05 = 115.50 → pick entry-based
    assert stop == round(110.65 * 1.10, 2)


def test_long_orphan_underwater_stop_is_below_current_price():
    """Long entry $100, current $85 (losing). Stop MUST be below
    current ($85*0.95=$80.75), not at $90 (entry*0.90) which is
    ABOVE current and would reject (or trigger immediately)."""
    import error_recovery as er
    strat = er.create_orphan_strategy("XYZ", 100, 85.00, 100.00)
    stop = strat["state"]["current_stop_price"]
    assert stop < 85.00, (
        f"Long sell-stop must be BELOW current, got {stop}")
    assert 80 <= stop <= 81, f"Expected ~$80.75, got {stop}"


def test_long_orphan_winning_uses_entry_based_stop():
    """Long in profit (current > entry): entry*0.90 is below
    current*0.95 → min picks entry-based (tighter loss cap)."""
    import error_recovery as er
    strat = er.create_orphan_strategy("XYZ", 100, 120.00, 100.00)
    stop = strat["state"]["current_stop_price"]
    # entry*0.90 = 90; current*0.95 = 114 → pick entry-based
    assert stop == round(100.00 * 0.90, 2)


# ----------------------------------------------------------------------------
# OCC orphan → wheel synthesis
# ----------------------------------------------------------------------------

def _make_tmp_dirs(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    return tmp_path, sdir


def _run_main(monkeypatch, tmp_path, sdir, position_list, orders=None):
    """Drive error_recovery.main() with mocked Alpaca calls + a
    clean STRATEGIES_DIR."""
    import error_recovery as er
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    monkeypatch.setattr(er, "DATA_DIR", str(tmp_path))

    def _fake_get(url, timeout=15, max_retries=3):
        if "positions" in url:
            return position_list
        if "orders" in url:
            return orders or []
        return []
    monkeypatch.setattr(er, "api_get_with_retry", _fake_get)
    monkeypatch.setattr(er, "api_post", lambda *a, **kw: {"id": "fake"})

    er.main()
    return sdir


def test_occ_short_put_orphan_creates_wheel_file(tmp_path, monkeypatch):
    _, sdir = _make_tmp_dirs(tmp_path)
    _run_main(monkeypatch, tmp_path, sdir, [{
        "symbol": "HIMS260508P00027000",
        "qty": "-1",
        "avg_entry_price": "2.05",
        "current_price": "1.07",
    }])
    # Expected: wheel_HIMS.json, NOT short_sell_HIMS260508P00027000.json
    assert (sdir / "wheel_HIMS.json").exists(), (
        "OCC short put orphan must synthesize wheel_<UNDERLYING>.json")
    assert not (sdir / "short_sell_HIMS260508P00027000.json").exists(), (
        "OCC orphans must NOT go through the equity short-sell path — "
        "monitor_strategies' short-sell logic doesn't understand "
        "contracts vs shares.")
    wheel = json.loads((sdir / "wheel_HIMS.json").read_text())
    assert wheel["stage"] == "stage_1_put_active"
    assert wheel["active_contract"]["contract_symbol"] == "HIMS260508P00027000"
    assert wheel["active_contract"]["strike"] == 27.0
    assert wheel["active_contract"]["type"] == "put"
    assert wheel["active_contract"]["expiration"] == "2026-05-08"


def test_occ_short_put_orphan_does_not_overwrite_existing_wheel(tmp_path, monkeypatch):
    """If wheel_<UNDERLYING>.json already exists, leave it alone —
    the wheel monitor may already be tracking this contract."""
    _, sdir = _make_tmp_dirs(tmp_path)
    existing = {
        "symbol": "HIMS", "strategy": "wheel", "status": "active",
        "stage": "stage_1_put_active", "shares_owned": 0,
        "_sentinel": "pre-existing wheel — must not be overwritten",
    }
    (sdir / "wheel_HIMS.json").write_text(json.dumps(existing))
    _run_main(monkeypatch, tmp_path, sdir, [{
        "symbol": "HIMS260508P00027000",
        "qty": "-1",
        "avg_entry_price": "2.05",
        "current_price": "1.07",
    }])
    after = json.loads((sdir / "wheel_HIMS.json").read_text())
    assert after.get("_sentinel"), (
        "Existing wheel file must NOT be overwritten by orphan adoption "
        "— the wheel monitor owns that state.")


def test_occ_long_option_orphan_skipped(tmp_path, monkeypatch):
    """Long premium (positive qty on an option) — no strategy in
    this codebase manages it, skip cleanly."""
    _, sdir = _make_tmp_dirs(tmp_path)
    _run_main(monkeypatch, tmp_path, sdir, [{
        "symbol": "SPY260619C00450000",
        "qty": "1",
        "avg_entry_price": "3.50",
        "current_price": "4.00",
    }])
    # Nothing should have been created.
    assert list(sdir.iterdir()) == []


def test_equity_short_orphan_still_uses_short_sell_prefix(tmp_path, monkeypatch):
    """Sanity check: non-OCC short stays on the equity short-sell
    path (pt.17 doesn't regress pt.15)."""
    _, sdir = _make_tmp_dirs(tmp_path)
    _run_main(monkeypatch, tmp_path, sdir, [{
        "symbol": "SOXL",
        "qty": "-29",
        "avg_entry_price": "110.65",
        "current_price": "129.31",
    }])
    assert (sdir / "short_sell_SOXL.json").exists()
    # And the stop is adaptive — ABOVE current price.
    state = json.loads((sdir / "short_sell_SOXL.json").read_text())
    assert state["state"]["current_stop_price"] > 129.31


# ----------------------------------------------------------------------------
# Source pins
# ----------------------------------------------------------------------------

def test_error_recovery_has_occ_parse_helper():
    src = _src("error_recovery.py")
    assert "def _occ_parse(sym)" in src


def test_error_recovery_has_adaptive_stop_logic():
    src = _src("error_recovery.py")
    assert "current_price_f" in src
    assert "max(entry_stop, current_stop)" in src
    assert "min(entry_stop, current_stop)" in src


def test_error_recovery_routes_occ_orphans_to_wheel_file():
    src = _src("error_recovery.py")
    assert "wheel_{underlying}.json" in src
    assert "stage_1_put_active" in src
    assert "stage_2_call_active" in src
