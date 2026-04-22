"""
Round-58 tests: fixes found by auditing the user's live /api/data dump.

Covered:
  1. Correlation warning resolves OCC option symbols to underlying for
     sector lookup (was counting HIMS260508P00027000 under "Other").
  2. SECTOR_MAP now covers every symbol surfaced in picks (CRDO, FSLY,
     MRVL, ALAB, etc.) that previously fell through to "Other".
  3. Screener mirrors the deployer's don't-chase + volatility gates —
     picks that the deployer would reject are tagged will_deploy=False
     with filter_reasons populated.
  4. /api/data tags picks already in the caller's open positions with
     already_held=True (CRDO, FSLY, SOXL, etc. in the user dump).
  5. Scorecard win-rate displays "insufficient sample" when
     total_trades < 5 (was showing 0% on N=2 which looks catastrophic).
  6. Insider data total_value_usd = None (not 0) so dashboards can
     render "—" instead of a misleading "$0 of insider buying" next to
     buy_count > 0.
  7. /api/data filters picks missing enrichment (momentum=0, no
     technical block) — tail picks 51+ arrived with default values
     that made them look like real picks.
  8. earnings_exit surfaces fetch failures to Sentry. Previously, a
     yfinance network hiccup or shape drift silently returned None and
     the pre-earnings exit rule fail-opened — INTC sat through earnings
     because the fetch failed and nobody knew.
"""
from __future__ import annotations


# ========= Fix 1: Correlation OCC → underlying sector =========

def test_correlation_warning_resolves_option_underlying():
    """HIMS260508P00027000 (OCC put) must be grouped under Healthcare
    (HIMS underlying), not "Other". Source-level grep — the real
    integration test is run via update_scorecard below."""
    with open("update_scorecard.py") as f:
        src = f.read()
    # The correlation guard must import annotate_sector from position_sector
    assert "from position_sector import annotate_sector" in src
    # And must use _underlying / _sector fields for the grouping
    assert '_sector' in src
    assert '_underlying' in src


def test_correlation_end_to_end_groups_option_by_underlying():
    """Integration: given a mixed portfolio of HIMS put + CRDO + FSLY,
    the correlation engine should NOT fire a 'Other: HIMS option +
    CRDO + FSLY' warning. CRDO/FSLY are Tech; HIMS put is Healthcare."""
    import position_sector
    annotated = position_sector.annotate_sector([
        {"symbol": "HIMS260508P00027000", "asset_class": "us_option"},
        {"symbol": "CRDO", "asset_class": "us_equity"},
        {"symbol": "FSLY", "asset_class": "us_equity"},
    ])
    sectors = [p["_sector"] for p in annotated]
    assert "Healthcare" in sectors, (
        "HIMS put must resolve to Healthcare, got sectors: " + str(sectors))
    assert sectors.count("Other") <= 1, (
        "After round-58 SECTOR_MAP additions, at most 1 of these 3 "
        "should be 'Other'. Got: " + str(sectors))


# ========= Fix 2: SECTOR_MAP additions =========

def test_sector_map_covers_user_dump_picks():
    """Every ticker the screener surfaced in 2026-04-22 /api/data that
    has a well-known sector must now map to that sector, not 'Other'."""
    from constants import SECTOR_MAP
    # Tech names the user saw as "Other"
    assert SECTOR_MAP.get("CRDO") == "Tech"
    assert SECTOR_MAP.get("FSLY") == "Tech"
    assert SECTOR_MAP.get("MRVL") == "Tech"
    assert SECTOR_MAP.get("ALAB") == "Tech"
    assert SECTOR_MAP.get("ANET") == "Tech"
    assert SECTOR_MAP.get("LRCX") == "Tech"
    # Healthcare
    assert SECTOR_MAP.get("ERAS") == "Healthcare"
    # Airlines / travel
    assert SECTOR_MAP.get("LUV") == "Industrial"
    assert SECTOR_MAP.get("DAL") == "Industrial"
    assert SECTOR_MAP.get("ALK") == "Industrial"
    # Finance
    assert SECTOR_MAP.get("COF") == "Finance"
    # Consumer
    assert SECTOR_MAP.get("LEVI") == "Consumer"
    assert SECTOR_MAP.get("KSS") == "Consumer"


# ========= Fix 3: Screener mirrors deployer gates =========

def test_screener_flags_chase_violators():
    """update_dashboard must annotate breakout picks with daily_change >
    8% as filter_reasons=['chase_block']."""
    with open("update_dashboard.py") as f:
        src = f.read()
    assert "chase_block" in src, (
        "update_dashboard must annotate chase-block picks — round-58")
    assert "will_deploy" in src
    assert 'daily_change"' in src or "daily_change'" in src


def test_screener_flags_volatility_violators():
    """Same gate for volatility > 20%."""
    with open("update_dashboard.py") as f:
        src = f.read()
    assert "volatility_block" in src


def test_screener_demotes_blocked_picks():
    """Blocked picks (will_deploy=False) must sort AFTER deployable
    picks so the 'top picks' rendering doesn't lead with garbage."""
    with open("update_dashboard.py") as f:
        src = f.read()
    assert "_deployable" in src and "_blocked" in src
    assert "top_candidates = _deployable + _blocked" in src


# ========= Fix 4: already_held annotation =========

def test_api_data_annotates_already_held():
    """server.py's /api/data must tag each pick with already_held=True
    when the caller is currently long (or short) the underlying."""
    with open("server.py") as f:
        src = f.read()
    # Grep-level pin: the annotation block must reference already_held
    # and the position-underlying lookup
    idx = src.find("already_held")
    assert idx > 0, "already_held annotation missing — round-58"
    window = src[idx:idx + 1500]
    assert "_underlying" in window


# ========= Fix 5: win-rate suppression on small sample =========

def test_api_data_suppresses_win_rate_small_sample():
    """scorecard.win_rate_pct_display is None when total_trades < 5."""
    with open("server.py") as f:
        src = f.read()
    assert "win_rate_pct_display" in src
    assert "win_rate_display_note" in src
    assert "win_rate_sample_size" in src


# ========= Fix 6: insider dollar-value display =========

def test_insider_total_value_is_null_not_zero():
    """insider_signals must emit total_value_usd=None (not 0) so the
    dashboard can render '—' instead of a misleading '$0'."""
    import insider_signals as _isig
    # Inspect the source: the 0-value literal on total_value_usd must be
    # gone and the None (+ value_parse_status) field must be present
    import inspect
    src = inspect.getsource(_isig)
    assert '"total_value_usd": 0' not in src, (
        "total_value_usd=0 still present — round-58 fix reverted")
    assert '"total_value_usd": None' in src
    assert '"value_parse_status"' in src


# ========= Fix 7: /api/data filters un-enriched picks =========

def test_api_data_filters_unenriched_picks():
    """Picks with no technical / zero momentum / zero recommended_shares
    must be dropped from /api/data. The screener enriches only top-50
    but wrote all ~431 to dashboard_data.json with default values."""
    with open("server.py") as f:
        src = f.read()
    assert "_has_technical" in src
    assert "_has_momentum" in src
    assert 'setdefault("enriched", True)' in src


# ========= Fix 8: earnings_exit LOUD on fetch failure =========

def test_earnings_exit_fetch_failure_is_loud():
    """When yfinance returns None for an eligible strategy due to a
    fetch error (shape drift, network, no yfinance installed), the
    caller MUST emit a Sentry breadcrumb so the operator knows the
    earnings gate isn't working. Legitimate 'no upcoming earnings'
    stays quiet."""
    with open("earnings_exit.py") as f:
        src = f.read()
    assert "_LAST_FETCH_ERR" in src
    assert "earnings_exit_fetch_failed" in src
    # capture_message call with the event tag
    assert "capture_message" in src
    # And preserve the legitimate quiet path
    assert "no_future_unreported" in src


def test_earnings_exit_has_force_refresh_operator_tool():
    """Round-58: add a force_refresh(symbol) so the operator can bust
    the 4-hour cache to verify the earnings rule is working for a
    specific position."""
    import earnings_exit
    assert hasattr(earnings_exit, "force_refresh")
    assert hasattr(earnings_exit, "get_last_fetch_error")


def test_earnings_exit_fetch_err_tracks_error_type():
    """_LAST_FETCH_ERR records the failure reason distinguishing
    'no_future_unreported' (legitimate quiet) from shape_drift / network
    / import errors (LOUD)."""
    with open("earnings_exit.py") as f:
        src = f.read()
    # Each failure branch must stamp a distinct reason
    assert '"yfinance_not_installed"' in src
    assert '"shape_drift:' in src
    assert '"network:' in src
    assert '"empty_result"' in src
    assert '"no_future_unreported"' in src
