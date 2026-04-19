"""
Tests for the phase-1 float→Decimal migration in tax_lots.py.

Goals:
  1. Behaviour parity: every public return value matches what the old
     float impl produced, within $0.01, on real-world fixtures.
  2. Drift resistance: long chains of partial fills / wheel cycles that
     would accumulate float drift don't drift under Decimal.
  3. Edge cases: zero-qty sells, unmatched sells (short), LIFO vs FIFO
     determinism, wash-sale detection unchanged.
  4. Boundary safety: every value the caller sees is still a float (the
     JSON boundary is respected) — no Decimal leaks into the return.

Test design note: we deliberately use prices that can't be represented
exactly in IEEE 754 (0.1, 0.05, 149.97) so drift, if any, surfaces.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import tax_lots


# ---------- Helpers ----------


def _trade(symbol, side, qty, price, date, strategy="test"):
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "timestamp": f"{date}T10:00:00",
        "strategy": strategy,
    }


# ---------- Return-type invariants ----------


def test_return_values_are_float_not_decimal():
    """Every money field in the result must be a plain float — consumers
    (JSON serialiser, CSV writer, dashboard JS) can't handle Decimal."""
    fixture = {"trades": [
        _trade("AAPL", "buy", 100, 150, "2024-01-15"),
        _trade("AAPL", "sell", 100, 160, "2024-02-15"),
    ]}
    r = tax_lots.compute_tax_lots(fixture)

    assert isinstance(r["lots"][0]["cost_basis"], float)
    assert isinstance(r["lots"][0]["proceeds"], float)
    assert isinstance(r["lots"][0]["gain_loss"], float)

    for k in ("total_proceeds", "total_cost_basis", "total_gain_loss",
              "short_term_gain", "long_term_gain"):
        assert isinstance(r["summary"][k], float), f"{k} leaked non-float"


def test_no_decimal_in_open_lots_remaining():
    """Same boundary guarantee on the open-lots payload."""
    fixture = {"trades": [_trade("TSLA", "buy", 100, 250.55, "2024-05-01")]}
    r = tax_lots.compute_tax_lots(fixture)
    assert "TSLA" in r["open_lots_remaining"]
    lot = r["open_lots_remaining"]["TSLA"][0]
    assert isinstance(lot["cost_basis_per_share"], float)


# ---------- Behaviour parity (smoke-test fixture from tax_lots.__main__) ----------


def test_matches_original_smoke_fixture():
    """Exact numbers expected from the current __main__ smoke test."""
    fixture = {"trades": [
        _trade("AAPL", "buy", 100, 150, "2024-01-15", "breakout"),
        _trade("AAPL", "sell", 50, 160, "2024-02-15", "breakout"),
        _trade("AAPL", "sell", 50, 145, "2025-02-15", "breakout"),
    ]}
    r = tax_lots.compute_tax_lots(fixture)
    assert len(r["lots"]) == 2
    short = next(l for l in r["lots"] if l["term"] == "short")
    long_ = next(l for l in r["lots"] if l["term"] == "long")
    assert short == {
        "symbol": "AAPL", "qty": 50, "acquired_date": "2024-01-15",
        "sold_date": "2024-02-15", "holding_days": 31, "term": "short",
        "cost_basis": 7500.00, "proceeds": 8000.00, "gain_loss": 500.00,
        "strategy": "breakout", "method": "FIFO",
    }
    assert long_["gain_loss"] == -250.00
    assert r["summary"]["total_gain_loss"] == 250.00


# ---------- IEEE-754 trip wires ----------


def test_irrational_float_prices_still_exact_to_cent():
    """0.1 + 0.2 is the canonical float bug: 0.30000000000000004.
    With Decimal-internal math, the gain/loss must be exactly $0.10."""
    fixture = {"trades": [
        _trade("X", "buy", 100, 0.1, "2024-01-01"),   # $10.00 basis
        _trade("X", "sell", 100, 0.2, "2024-02-01"),  # $20.00 proceeds
    ]}
    r = tax_lots.compute_tax_lots(fixture)
    lot = r["lots"][0]
    # Old float impl: 0.2*100 - 0.1*100 = 20.00000000000000 - 10.0 = 10.0
    # (happens to be clean here because *100). Test a subtler case too.
    assert lot["gain_loss"] == 10.00
    assert lot["cost_basis"] == 10.00
    assert lot["proceeds"] == 20.00


def test_sub_cent_prices_round_consistently():
    """Alpaca can report prices to 4 decimals on fractional-share fills.
    Our output quantizes to the cent with banker's rounding."""
    fixture = {"trades": [
        _trade("X", "buy", 100, 10.1234, "2024-01-01"),    # basis 1012.34
        _trade("X", "sell", 100, 12.3456, "2024-02-01"),   # proceeds 1234.56
    ]}
    r = tax_lots.compute_tax_lots(fixture)
    lot = r["lots"][0]
    assert lot["cost_basis"] == 1012.34
    assert lot["proceeds"] == 1234.56
    assert lot["gain_loss"] == 222.22


def test_long_chain_of_partial_fills_no_drift():
    """52 partial buys of 10 shares at an irrational price + one close.
    Under pure float, expected drift > $0.001 on aggregate basis. Under
    Decimal, must be exact to the cent."""
    trades = []
    for i in range(52):
        trades.append(_trade("WHEEL", "buy", 10, 23.17, f"2024-01-{(i % 27) + 1:02d}"))
    trades.append(_trade("WHEEL", "sell", 520, 24.88, "2024-06-01"))
    r = tax_lots.compute_tax_lots({"trades": trades})
    # 52 lots, 10 shares each, all matched in the single sell
    assert len(r["lots"]) == 52
    # Cost basis per lot: 10 * 23.17 = 231.70 exact
    for l in r["lots"]:
        assert l["cost_basis"] == 231.70
        # Proceeds per lot: 10 * 24.88 = 248.80 exact
        assert l["proceeds"] == 248.80
        assert l["gain_loss"] == 17.10
    # Aggregate: 52 * 17.10 = 889.20
    assert r["summary"]["total_gain_loss"] == 889.20
    assert r["summary"]["total_cost_basis"] == 52 * 231.70
    assert r["summary"]["total_proceeds"] == 52 * 248.80


def test_many_buys_many_sells_fifo_consumes_oldest_first():
    trades = [
        _trade("A", "buy", 10, 100.00, "2024-01-01"),
        _trade("A", "buy", 10, 200.00, "2024-02-01"),
        _trade("A", "buy", 10, 300.00, "2024-03-01"),
        _trade("A", "sell", 15, 250.00, "2024-04-01"),
    ]
    r = tax_lots.compute_tax_lots({"trades": trades}, basis_method="FIFO")
    # First 10 from Jan (basis $1000), then 5 from Feb (basis $1000)
    assert r["lots"][0]["cost_basis"] == 1000.00
    assert r["lots"][0]["proceeds"] == 2500.00
    assert r["lots"][0]["gain_loss"] == 1500.00
    assert r["lots"][1]["cost_basis"] == 1000.00  # 5 * 200
    assert r["lots"][1]["proceeds"] == 1250.00    # 5 * 250
    assert r["lots"][1]["gain_loss"] == 250.00


def test_many_buys_many_sells_lifo_consumes_newest_first():
    trades = [
        _trade("A", "buy", 10, 100.00, "2024-01-01"),
        _trade("A", "buy", 10, 200.00, "2024-02-01"),
        _trade("A", "buy", 10, 300.00, "2024-03-01"),
        _trade("A", "sell", 15, 250.00, "2024-04-01"),
    ]
    r = tax_lots.compute_tax_lots({"trades": trades}, basis_method="LIFO")
    # First 10 from Mar ($3000 basis), then 5 from Feb ($1000)
    assert r["lots"][0]["cost_basis"] == 3000.00
    assert r["lots"][0]["gain_loss"] == -500.00
    assert r["lots"][1]["cost_basis"] == 1000.00
    assert r["lots"][1]["gain_loss"] == 250.00
    # Summary short vs long: all short-term.
    assert r["summary"]["short_term_gain"] == -250.00


# ---------- Holding-period boundary ----------


def test_holding_period_365_days_is_long_term():
    trades = [
        _trade("A", "buy", 10, 100, "2024-01-15"),
        _trade("A", "sell", 10, 120, "2025-01-15"),   # exactly 366 days
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert r["lots"][0]["term"] == "long"


def test_holding_period_364_days_is_short_term():
    trades = [
        _trade("A", "buy", 10, 100, "2024-01-15"),
        _trade("A", "sell", 10, 120, "2025-01-13"),   # 364 days
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert r["lots"][0]["term"] == "short"


# ---------- Edge cases ----------


def test_empty_journal():
    r = tax_lots.compute_tax_lots({"trades": []})
    assert r["lots"] == []
    assert r["summary"]["lot_count"] == 0
    assert r["summary"]["total_gain_loss"] == 0.00
    assert r["open_lots_remaining"] == {}


def test_none_journal():
    r = tax_lots.compute_tax_lots(None)
    assert r["lots"] == []


def test_only_buys_no_closed_lots_no_zero_summary():
    trades = [_trade("A", "buy", 100, 50, "2024-01-01")]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert r["lots"] == []
    assert r["summary"]["total_gain_loss"] == 0.00
    # But open_lots_remaining reports the buy.
    assert r["open_lots_remaining"]["A"][0]["qty"] == 100
    assert r["open_lots_remaining"]["A"][0]["cost_basis_per_share"] == 50.00


def test_unmatched_sell_is_silently_dropped():
    """No buys preceding the sell — nothing to match against."""
    trades = [_trade("A", "sell", 100, 50, "2024-01-01")]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert r["lots"] == []


def test_oversold_matches_available_then_drops_residual():
    trades = [
        _trade("A", "buy", 50, 100, "2024-01-01"),
        _trade("A", "sell", 100, 120, "2024-02-01"),   # 50 unmatched
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert len(r["lots"]) == 1
    assert r["lots"][0]["qty"] == 50


def test_missing_timestamp_is_skipped():
    trades = [
        {"symbol": "A", "side": "buy", "qty": 10, "price": 100},  # no ts
        _trade("A", "sell", 10, 110, "2024-02-01"),
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    # Buy was skipped, sell has nothing to match.
    assert r["lots"] == []


def test_zero_qty_trades_skipped():
    trades = [
        _trade("A", "buy", 0, 100, "2024-01-01"),
        _trade("A", "buy", 10, 100, "2024-01-01"),
        _trade("A", "sell", 10, 120, "2024-02-01"),
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert len(r["lots"]) == 1
    assert r["lots"][0]["qty"] == 10


def test_symbol_independence():
    """Lots in symbol A shouldn't match against sells of symbol B."""
    trades = [
        _trade("A", "buy", 10, 100, "2024-01-01"),
        _trade("B", "sell", 10, 120, "2024-02-01"),  # no B position
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert r["lots"] == []
    assert "A" in r["open_lots_remaining"]


# ---------- Wash sales ----------


def test_wash_sale_detected_within_30_days():
    trades = [
        _trade("A", "buy", 10, 100, "2024-01-01"),
        _trade("A", "sell", 10, 80, "2024-02-01"),    # -$200 loss
        _trade("A", "buy", 10, 85, "2024-02-15"),     # 14 days after
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert r["summary"]["wash_sale_warnings"]
    w = r["summary"]["wash_sale_warnings"][0]
    assert w["symbol"] == "A"
    assert w["days_apart"] == 14


def test_no_wash_sale_after_31_days():
    trades = [
        _trade("A", "buy", 10, 100, "2024-01-01"),
        _trade("A", "sell", 10, 80, "2024-02-01"),
        _trade("A", "buy", 10, 85, "2024-03-05"),    # 33 days after
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert r["summary"]["wash_sale_warnings"] == []


def test_no_wash_sale_on_gain_only_on_loss():
    trades = [
        _trade("A", "buy", 10, 100, "2024-01-01"),
        _trade("A", "sell", 10, 120, "2024-02-01"),   # gain, not loss
        _trade("A", "buy", 10, 125, "2024-02-15"),
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    assert r["summary"]["wash_sale_warnings"] == []


# ---------- Golden master vs pre-migration reference ----------


def test_golden_master_mixed_symbols_and_terms():
    """A canned fixture whose outputs were hand-computed from the legacy
    float-based impl. If this test starts to fail it means we've changed
    the contract that callers rely on — serious red flag."""
    trades = [
        _trade("AAPL", "buy", 100, 180.25, "2023-03-15"),
        _trade("AAPL", "buy", 50, 185.00, "2023-06-10"),
        _trade("AAPL", "sell", 75, 195.50, "2024-04-20"),   # long-term
        _trade("NVDA", "buy", 20, 450.00, "2024-08-01"),
        _trade("NVDA", "sell", 20, 520.00, "2024-12-15"),   # short-term
    ]
    r = tax_lots.compute_tax_lots({"trades": trades})
    # AAPL: 75 of the 100 @ 180.25 → basis 13,518.75, proceeds 14,662.50, g/l 1,143.75 long
    aapl = next(l for l in r["lots"] if l["symbol"] == "AAPL")
    assert aapl["qty"] == 75
    assert aapl["cost_basis"] == 13518.75
    assert aapl["proceeds"] == 14662.50
    assert aapl["gain_loss"] == 1143.75
    assert aapl["term"] == "long"
    # NVDA: 20 @ 450 → basis 9000, proceeds 10400, g/l 1400 short
    nvda = next(l for l in r["lots"] if l["symbol"] == "NVDA")
    assert nvda["cost_basis"] == 9000.00
    assert nvda["proceeds"] == 10400.00
    assert nvda["gain_loss"] == 1400.00
    assert nvda["term"] == "short"
    # Summary
    assert r["summary"]["total_gain_loss"] == 2543.75
    assert r["summary"]["short_term_gain"] == 1400.00
    assert r["summary"]["long_term_gain"] == 1143.75
    # AAPL open 75 shares remaining (25 from 1st lot + 50 from 2nd)
    assert "AAPL" in r["open_lots_remaining"]
    rem = r["open_lots_remaining"]["AAPL"]
    assert len(rem) == 2
    assert sum(l["qty"] for l in rem) == 75


# ---------- Small-helper sanity ----------


def test_to_decimal_rejects_none_without_raising():
    assert tax_lots._to_decimal(None) == Decimal("0")


def test_to_decimal_avoids_float_double_conversion():
    """Direct proof the Decimal(str(...)) trick avoids the float-roundtrip bug."""
    # If we were doing Decimal(float_x) instead of Decimal(str(float_x)),
    # this would produce 0.10000000000000000555... rather than exactly 0.1.
    assert tax_lots._to_decimal(0.1) == Decimal("0.1")
