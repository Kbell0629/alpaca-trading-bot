"""Round-61 pt.59 — dead-money cutter + VWAP entry gate +
earnings-calendar static fallback.

Three pure modules + their wire-up to cloud_scheduler / auto-deployer.
Lazy-import pattern (CI-stable) per pt.52.
"""
from __future__ import annotations

from datetime import date


# ============================================================================
# dead_money.is_dead_money
# ============================================================================

def test_dead_money_basic_dead():
    """Held 10 days, moved 0% → dead."""
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-15",
        today=date(2026, 4, 25),
        entry_price=100.0,
        current_price=100.0,
    )
    assert out["is_dead"] is True
    assert out["days_held"] == 10
    assert out["pnl_pct"] == 0.0


def test_dead_money_too_recent():
    """Held 5 days, even if flat — not yet dead."""
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-20",
        today=date(2026, 4, 25),
        entry_price=100.0,
        current_price=100.0,
    )
    assert out["is_dead"] is False
    assert out["reason"] == "too_recent"
    assert out["days_held"] == 5


def test_dead_money_winner_not_dead():
    """Held 10 days, up 5% — not dead."""
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-15",
        today=date(2026, 4, 25),
        entry_price=100.0,
        current_price=105.0,
    )
    assert out["is_dead"] is False
    assert out["reason"] == "moved_enough"


def test_dead_money_loser_not_dead():
    """Held 10 days, down 5% — not dead (the 2% threshold is
    bidirectional)."""
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-15",
        today=date(2026, 4, 25),
        entry_price=100.0,
        current_price=95.0,
    )
    assert out["is_dead"] is False
    assert out["reason"] == "moved_enough"


def test_dead_money_at_2pct_boundary_not_dead():
    """Exactly 2% movement → not dead (boundary is exclusive)."""
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-15",
        today=date(2026, 4, 25),
        entry_price=100.0,
        current_price=102.0,
    )
    assert out["is_dead"] is False


def test_dead_money_just_under_2pct_dead():
    """1.9% movement at 10 days → dead."""
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-15",
        today=date(2026, 4, 25),
        entry_price=100.0,
        current_price=101.9,
    )
    assert out["is_dead"] is True
    assert "dead_money:" in out["reason"]


def test_dead_money_missing_created_date():
    import dead_money as dm
    out = dm.is_dead_money(
        created_str=None, today=date(2026, 4, 25),
        entry_price=100.0, current_price=100.0,
    )
    assert out["is_dead"] is False
    assert out["reason"] == "missing_created_date"


def test_dead_money_handles_date_object():
    import dead_money as dm
    out = dm.is_dead_money(
        created_str=date(2026, 4, 15),
        today=date(2026, 4, 25),
        entry_price=100.0, current_price=100.0,
    )
    assert out["is_dead"] is True


def test_dead_money_handles_iso_with_time():
    """`created` may include a T-time portion; parser should slice."""
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-15T16:05:00",
        today=date(2026, 4, 25),
        entry_price=100.0, current_price=100.0,
    )
    assert out["is_dead"] is True


def test_dead_money_zero_entry_price():
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-15",
        today=date(2026, 4, 25),
        entry_price=0,
        current_price=10.0,
    )
    assert out["is_dead"] is False
    assert out["reason"] == "zero_entry_price"


def test_dead_money_invalid_prices():
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-15",
        today=date(2026, 4, 25),
        entry_price="bad", current_price="also bad",
    )
    assert out["is_dead"] is False
    assert out["reason"] == "bad_price"


def test_dead_money_configurable_thresholds():
    """Caller can tune min_days_held + max_drift_pct."""
    import dead_money as dm
    out = dm.is_dead_money(
        created_str="2026-04-22",
        today=date(2026, 4, 25),
        entry_price=100.0, current_price=100.5,
        min_days_held=3, max_drift_pct=1.0,
    )
    assert out["is_dead"] is True


# ============================================================================
# vwap_gate
# ============================================================================

def test_vwap_offset_above():
    import vwap_gate as vg
    assert abs(vg.compute_vwap_offset_pct(101.0, 100.0) - 1.0) < 1e-9


def test_vwap_offset_below():
    import vwap_gate as vg
    assert abs(vg.compute_vwap_offset_pct(99.0, 100.0) - (-1.0)) < 1e-9


def test_vwap_offset_invalid():
    import vwap_gate as vg
    assert vg.compute_vwap_offset_pct("bad", 100.0) is None
    assert vg.compute_vwap_offset_pct(100.0, 0) is None
    assert vg.compute_vwap_offset_pct(100.0, -1) is None


def test_vwap_gate_breakout_blocked_when_above():
    """price 1.5% above vwap (> 0.5% tolerance) → blocked."""
    import vwap_gate as vg
    out = vg.evaluate_vwap_gate(
        strategy="breakout", price=101.5, vwap=100.0)
    assert out["allowed"] is False
    assert "above_vwap" in out["reason"]


def test_vwap_gate_breakout_allowed_within_tolerance():
    """price 0.3% above vwap (< 0.5% tolerance) → allowed."""
    import vwap_gate as vg
    out = vg.evaluate_vwap_gate(
        strategy="breakout", price=100.3, vwap=100.0)
    assert out["allowed"] is True


def test_vwap_gate_breakout_below_vwap_allowed():
    """price below vwap is always fine — that's the dip."""
    import vwap_gate as vg
    out = vg.evaluate_vwap_gate(
        strategy="breakout", price=98.0, vwap=100.0)
    assert out["allowed"] is True


def test_vwap_gate_no_op_for_other_strategies():
    """Non-breakout strategies get a no-op pass-through."""
    import vwap_gate as vg
    for strat in ("mean_reversion", "wheel", "pead",
                   "trailing_stop", "short_sell"):
        out = vg.evaluate_vwap_gate(
            strategy=strat, price=110.0, vwap=100.0)
        assert out["allowed"] is True
        assert out["reason"] == "not_breakout"


def test_vwap_gate_fails_open_when_no_vwap():
    """No VWAP data (caller's fetch failed) → allow with reason."""
    import vwap_gate as vg
    out = vg.evaluate_vwap_gate(
        strategy="breakout", price=100.0, vwap=None)
    assert out["allowed"] is True
    assert "fail_open" in out["reason"]


def test_vwap_gate_configurable_tolerance():
    import vwap_gate as vg
    # Same 1% offset; tighter tolerance → blocked.
    out_tight = vg.evaluate_vwap_gate(
        strategy="breakout", price=101.0, vwap=100.0,
        tolerance_pct=0.5)
    out_loose = vg.evaluate_vwap_gate(
        strategy="breakout", price=101.0, vwap=100.0,
        tolerance_pct=2.0)
    assert out_tight["allowed"] is False
    assert out_loose["allowed"] is True


def test_vwap_gate_returns_offset_in_dict():
    import vwap_gate as vg
    out = vg.evaluate_vwap_gate(
        strategy="breakout", price=101.5, vwap=100.0)
    assert "vwap_offset_pct" in out
    assert abs(out["vwap_offset_pct"] - 1.5) < 1e-6


# ============================================================================
# earnings_calendar_static
# ============================================================================

def test_static_calendar_has_aapl_msft():
    import earnings_calendar_static as ecs
    assert ecs.has_static_entry("AAPL") is True
    assert ecs.has_static_entry("aapl") is True   # case-insensitive
    assert ecs.has_static_entry("MSFT") is True


def test_static_calendar_size_at_least_20():
    """Sample of S&P 100 should have plenty of entries."""
    import earnings_calendar_static as ecs
    assert ecs.static_calendar_size() >= 20


def test_next_earnings_returns_static_when_within_horizon():
    import earnings_calendar_static as ecs
    out = ecs.next_earnings_date(
        "AAPL", as_of=date(2026, 4, 25), max_days_ahead=30)
    assert out is not None
    assert out >= date(2026, 4, 25)


def test_next_earnings_etf_explicit_none():
    """SOXL (leveraged ETF) → static entry is None → returns None."""
    import earnings_calendar_static as ecs
    assert ecs.next_earnings_date(
        "SOXL", as_of=date(2026, 4, 25)) is None


def test_next_earnings_unknown_symbol_falls_back_to_yfinance():
    import earnings_calendar_static as ecs

    def fake_yfinance(sym):
        return "2026-05-10"

    out = ecs.next_earnings_date(
        "SOMENEWTICKER", as_of=date(2026, 4, 25),
        yfinance_lookup_fn=fake_yfinance)
    assert out == date(2026, 5, 10)


def test_next_earnings_yfinance_fail_returns_none():
    import earnings_calendar_static as ecs

    def fake_yfinance(sym):
        raise RuntimeError("network down")

    out = ecs.next_earnings_date(
        "SOMENEWTICKER", as_of=date(2026, 4, 25),
        yfinance_lookup_fn=fake_yfinance)
    assert out is None


def test_next_earnings_yfinance_returns_garbage():
    import earnings_calendar_static as ecs

    def fake_yfinance(sym):
        return "not-a-date"

    out = ecs.next_earnings_date(
        "SOMENEWTICKER", as_of=date(2026, 4, 25),
        yfinance_lookup_fn=fake_yfinance)
    assert out is None


def test_next_earnings_filters_past_dates():
    """A static date in the past should NOT be returned."""
    import earnings_calendar_static as ecs
    out = ecs.next_earnings_date(
        "JPM", as_of=date(2026, 5, 1), max_days_ahead=30)
    # JPM had 2026-04-13 → past → return None.
    assert out is None


def test_next_earnings_horizon_is_configurable():
    """A static entry beyond the requested horizon should NOT be
    returned, even if it's in the future."""
    import earnings_calendar_static as ecs
    # NVDA's static entry is 2026-05-22 → 27 days from 2026-04-25.
    near = ecs.next_earnings_date(
        "NVDA", as_of=date(2026, 4, 25), max_days_ahead=10)
    far = ecs.next_earnings_date(
        "NVDA", as_of=date(2026, 4, 25), max_days_ahead=60)
    assert near is None
    assert far == date(2026, 5, 22)


def test_next_earnings_handles_datetime_from_yfinance():
    import earnings_calendar_static as ecs
    from datetime import datetime as _dt

    def fake(sym):
        return _dt(2026, 5, 10, 16, 0)

    out = ecs.next_earnings_date(
        "X", as_of=date(2026, 4, 25), yfinance_lookup_fn=fake)
    assert out == date(2026, 5, 10)


# ============================================================================
# Wire-up source pins
# ============================================================================

def test_cloud_scheduler_imports_dead_money():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    assert "import dead_money" in src
    assert "is_dead_money" in src
    assert "dead_money" in src


def test_dead_money_only_runs_for_non_pead_strategies():
    """Source pin: the dead-money block must explicitly skip PEAD
    so the 30-60d drift window doesn't false-positive."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    # Find the dead-money block by anchor.
    idx = src.find("dead-money cutter")
    assert idx > 0
    block = src[idx:idx + 1500]
    assert 'strategy_type != "pead"' in block or \
            "strategy_type != 'pead'" in block


def test_dead_money_is_extended_hours_safe():
    """Dead-money block must NOT run during extended_hours (Alpaca
    rejects market orders in pre/AH; we'd just pile up errors)."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    idx = src.find("dead-money cutter")
    block = src[idx:idx + 1500]
    assert "not extended_hours" in block
