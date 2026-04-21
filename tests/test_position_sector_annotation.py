"""
Round-35 tests: server-side sector annotation for Position Correlation panel.

Before round-35, the Position Correlation section printed
"Sectors: <list of position symbols>" — which wasn't showing sectors
at all, just symbols. Useless.

The fix annotates each position with `_sector` + `_underlying` using
constants.SECTOR_MAP, resolving OCC option symbols to their underlying
so a HIMS put → Healthcare.

Tests target the standalone position_sector.annotate_sector helper so
they don't have to reload server (which drags auth / sqlite init).

Covered:
  * Equity positions get the correct sector
  * OCC-format option positions resolve to underlying → underlying's sector
  * Malformed / unknown symbols bucket as "Other"
  * Preserves pre-existing position fields
  * Empty / non-list input returns unchanged
"""
from __future__ import annotations


# ---------- equity sector lookup ----------


def test_annotate_sector_assigns_tech_to_soxl():
    from position_sector import annotate_sector
    positions = [{"symbol": "SOXL", "asset_class": "us_equity",
                  "qty": "117", "market_value": "11434.41"}]
    out = annotate_sector(positions)
    assert out[0]["_sector"] == "Tech"
    assert out[0]["_underlying"] == "SOXL"


def test_annotate_sector_assigns_intc_to_tech():
    from position_sector import annotate_sector
    positions = [{"symbol": "INTC", "asset_class": "us_equity"}]
    out = annotate_sector(positions)
    assert out[0]["_sector"] == "Tech"


def test_annotate_sector_assigns_usar_to_materials():
    from position_sector import annotate_sector
    positions = [{"symbol": "USAR", "asset_class": "us_equity"}]
    out = annotate_sector(positions)
    assert out[0]["_sector"] == "Materials"


# ---------- OCC option underlying resolution ----------


def test_annotate_sector_resolves_hims_put_to_healthcare():
    from position_sector import annotate_sector
    positions = [{"symbol": "HIMS260508P00027000",
                  "asset_class": "us_option",
                  "qty": "-1", "market_value": "-151"}]
    out = annotate_sector(positions)
    assert out[0]["_underlying"] == "HIMS"
    assert out[0]["_sector"] == "Healthcare"


def test_annotate_sector_resolves_chwy_put_to_consumer():
    from position_sector import annotate_sector
    positions = [{"symbol": "CHWY260515P00025000",
                  "asset_class": "us_option"}]
    out = annotate_sector(positions)
    assert out[0]["_underlying"] == "CHWY"
    assert out[0]["_sector"] == "Consumer"


# ---------- unknown symbols fall back to Other ----------


def test_annotate_sector_unknown_symbol_buckets_as_other():
    from position_sector import annotate_sector
    positions = [{"symbol": "ZZZZZ", "asset_class": "us_equity"}]
    out = annotate_sector(positions)
    assert out[0]["_sector"] == "Other"
    assert out[0]["_underlying"] == "ZZZZZ"


def test_annotate_sector_malformed_option_falls_back_to_symbol():
    from position_sector import annotate_sector
    positions = [{"symbol": "NOT_AN_OPTION", "asset_class": "us_option"}]
    out = annotate_sector(positions)
    # Malformed OCC → treat whole symbol as lookup key → Other
    assert out[0]["_sector"] == "Other"


# ---------- doesn't clobber existing fields ----------


def test_annotate_sector_preserves_pre_existing_fields():
    from position_sector import annotate_sector
    positions = [{
        "symbol": "SOXL", "asset_class": "us_equity",
        "qty": "117", "market_value": "11434.41",
        "_auto_deployed": True, "_strategy": "trailing_stop",
    }]
    out = annotate_sector(positions)
    assert out[0]["symbol"] == "SOXL"  # untouched
    assert out[0]["qty"] == "117"
    assert out[0]["_auto_deployed"] is True
    assert out[0]["_strategy"] == "trailing_stop"
    assert out[0]["_sector"] == "Tech"  # new


def test_annotate_sector_empty_list_returns_unchanged():
    from position_sector import annotate_sector
    assert annotate_sector([]) == []


def test_annotate_sector_non_list_returns_unchanged():
    from position_sector import annotate_sector
    assert annotate_sector(None) is None


# ---------- realistic 5-position portfolio ----------


def test_annotate_sector_full_portfolio():
    """The actual 5-position snapshot the user had when they flagged
    the useless correlation panel."""
    from position_sector import annotate_sector
    positions = [
        {"symbol": "CHWY260515P00025000", "asset_class": "us_option", "market_value": "-33"},
        {"symbol": "HIMS260508P00027000", "asset_class": "us_option", "market_value": "-151"},
        {"symbol": "INTC", "asset_class": "us_equity", "market_value": "4190"},
        {"symbol": "SOXL", "asset_class": "us_equity", "market_value": "11434"},
        {"symbol": "USAR", "asset_class": "us_equity", "market_value": "3384"},
    ]
    out = annotate_sector(positions)
    sectors = [p["_sector"] for p in out]
    assert sectors == ["Consumer", "Healthcare", "Tech", "Tech", "Materials"]
