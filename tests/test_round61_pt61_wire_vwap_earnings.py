"""Round-61 pt.61 — wire pt.59's VWAP gate + earnings calendar
helpers into the auto-deployer.

Pt.59 built ``vwap_gate.py`` and ``earnings_calendar_static.py`` as
pure modules but didn't wire either into the deploy path. Pt.61
plugs them into ``cloud_scheduler.run_auto_deployer``:

1. **VWAP gate** runs after chase_block / volatility_block, ONLY
   for breakout picks. Fetches today's 5-min bars from Alpaca's
   data endpoint, computes VWAP via ``indicators.vwap``, blocks
   the deploy if ``price > VWAP × 1.005``. Fails OPEN on any
   data-fetch error.

2. **Earnings calendar** runs after the VWAP gate. Calls
   ``earnings_calendar_static.next_earnings_date(symbol,
   max_days_ahead=3)``; if the static calendar says earnings
   within 3 days, blocks the deploy (PEAD exempt — it WANTS
   post-earnings drift, but only after the report has dropped).

Tests use the lazy-import pattern (CI-stable, pt.52).
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# Source pins — VWAP gate is in the deployer
# ============================================================================

def test_auto_deployer_imports_vwap_gate():
    src = _src("cloud_scheduler.py")
    assert "import vwap_gate" in src


def test_auto_deployer_imports_indicators_vwap():
    src = _src("cloud_scheduler.py")
    # Different import style accepted; just look for the helper name.
    assert "vwap" in src.lower()


def test_auto_deployer_calls_evaluate_vwap_gate():
    src = _src("cloud_scheduler.py")
    assert "evaluate_vwap_gate" in src


def test_auto_deployer_vwap_gate_only_for_breakout():
    """The VWAP gate block must be inside an `if best_strat ==
    "breakout":` so other strategies pass through unchanged."""
    src = _src("cloud_scheduler.py")
    idx = src.find("VWAP-relative entry gate")
    assert idx > 0
    block = src[idx:idx + 2500]
    assert 'best_strat == "breakout"' in block or \
            "best_strat == 'breakout'" in block


def test_auto_deployer_vwap_gate_fetches_today_bars():
    src = _src("cloud_scheduler.py")
    idx = src.find("VWAP-relative entry gate")
    block = src[idx:idx + 2500]
    # 5-min bars from data endpoint.
    assert "timeframe=5Min" in block
    # Hits the data endpoint, not trading.
    assert "data.alpaca.markets" in block or "_data_endpoint" in block


def test_auto_deployer_vwap_gate_records_skip_reason():
    src = _src("cloud_scheduler.py")
    idx = src.find("VWAP-relative entry gate")
    block = src[idx:idx + 2500]
    assert "above_vwap" in block
    assert "skip_reasons.append" in block


def test_auto_deployer_vwap_gate_fails_open_on_error():
    """Exception inside the VWAP block should NOT prevent the deploy
    — it should fall through to the rest of the loop."""
    src = _src("cloud_scheduler.py")
    idx = src.find("VWAP-relative entry gate")
    block = src[idx:idx + 2500]
    # Try/except with a fail-open log.
    assert "VWAP gate fetch" in block
    assert "allowing through" in block


# ============================================================================
# Source pins — earnings calendar in the deployer
# ============================================================================

def test_auto_deployer_imports_earnings_calendar():
    src = _src("cloud_scheduler.py")
    assert "import earnings_calendar_static" in src


def test_auto_deployer_calls_next_earnings_date():
    src = _src("cloud_scheduler.py")
    assert "next_earnings_date" in src


def test_auto_deployer_earnings_block_3_day_horizon():
    src = _src("cloud_scheduler.py")
    idx = src.find("earnings-calendar pre-flight")
    assert idx > 0
    block = src[idx:idx + 1500]
    assert "max_days_ahead=3" in block


def test_auto_deployer_earnings_block_pead_exempt():
    """PEAD WANTS post-earnings drift — should not be earnings-blocked."""
    src = _src("cloud_scheduler.py")
    idx = src.find("earnings-calendar pre-flight")
    block = src[idx:idx + 1500]
    assert 'best_strat != "pead"' in block or \
            "best_strat != 'pead'" in block


def test_auto_deployer_earnings_block_records_reason():
    src = _src("cloud_scheduler.py")
    idx = src.find("earnings-calendar pre-flight")
    block = src[idx:idx + 1500]
    assert "earnings_in_3d" in block
    assert "skip_reasons.append" in block


def test_auto_deployer_earnings_check_fails_open_on_error():
    src = _src("cloud_scheduler.py")
    idx = src.find("earnings-calendar pre-flight")
    block = src[idx:idx + 1500]
    assert "earnings calendar" in block.lower()
    assert "allowing through" in block


# ============================================================================
# vwap_gate + earnings_calendar_static integration smoke
# ============================================================================

def test_vwap_gate_and_earnings_static_both_importable():
    """Sanity: both modules import cleanly. No circular deps from
    being wired into cloud_scheduler."""
    import vwap_gate
    import earnings_calendar_static
    assert hasattr(vwap_gate, "evaluate_vwap_gate")
    assert hasattr(earnings_calendar_static, "next_earnings_date")


def test_vwap_gate_breakout_block_with_realistic_data():
    """End-to-end: realistic vwap + price → expected block decision."""
    import vwap_gate as vg
    # Stock trading at $101 vs VWAP $100 → 1% above → blocked.
    out = vg.evaluate_vwap_gate(
        strategy="breakout", price=101.0, vwap=100.0)
    assert out["allowed"] is False


def test_vwap_gate_breakout_allow_with_realistic_data():
    """Stock at $100.30 vs VWAP $100 → 0.3% above → within tolerance."""
    import vwap_gate as vg
    out = vg.evaluate_vwap_gate(
        strategy="breakout", price=100.30, vwap=100.0)
    assert out["allowed"] is True


def test_earnings_static_aapl_within_30_days():
    """Sanity on the integration: AAPL has a static entry that
    covers a 30-day horizon from session start."""
    import earnings_calendar_static as ecs
    from datetime import date
    out = ecs.next_earnings_date(
        "AAPL", as_of=date(2026, 4, 25), max_days_ahead=30)
    assert out is not None


def test_earnings_static_unknown_symbol_returns_none():
    """A symbol with no static entry and no yfinance fallback
    returns None so the deploy is allowed through."""
    import earnings_calendar_static as ecs
    from datetime import date
    out = ecs.next_earnings_date(
        "ZZZUNKNOWN", as_of=date(2026, 4, 25))
    assert out is None
