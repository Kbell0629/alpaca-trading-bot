"""
Tests for the round-25 OCC-option orphan-check fix in error_recovery.

Bug: the orphan check keyed off the raw position symbol. For option
contracts Alpaca returns OCC-format symbols like "CHWY260515P00025000",
but wheel strategy files are saved under the UNDERLYING ("CHWY"). The
map lookup missed, and every active wheel put was flagged as an orphan
with a false-positive critical_alert email.

Fix: if the position symbol matches the OCC regex, resolve to the
underlying before the strategy-file lookup.
"""
from __future__ import annotations

import error_recovery


def test_is_occ_option_symbol_matches_standard_shape():
    assert error_recovery._is_occ_option_symbol("CHWY260515P00025000")
    assert error_recovery._is_occ_option_symbol("HIMS260508P00027000")
    assert error_recovery._is_occ_option_symbol("AAPL260117C00200000")
    assert error_recovery._is_occ_option_symbol("SPY260620P00450000")


def test_is_occ_option_symbol_rejects_equities_and_garbage():
    assert not error_recovery._is_occ_option_symbol("AAPL")
    assert not error_recovery._is_occ_option_symbol("SOXL")
    assert not error_recovery._is_occ_option_symbol("")
    assert not error_recovery._is_occ_option_symbol(None)
    # Wrong shape
    assert not error_recovery._is_occ_option_symbol("CHWY260515X00025000")
    # Trailing chars
    assert not error_recovery._is_occ_option_symbol("CHWY260515P00025000X")
    # Too-long underlying
    assert not error_recovery._is_occ_option_symbol("ABCDEFGH260515P00025000")


def test_occ_underlying_extracts_correctly():
    assert error_recovery._occ_underlying("CHWY260515P00025000") == "CHWY"
    assert error_recovery._occ_underlying("HIMS260508P00027000") == "HIMS"
    assert error_recovery._occ_underlying("AAPL260117C00200000") == "AAPL"
    assert error_recovery._occ_underlying("SPY260620P00450000") == "SPY"


def test_occ_underlying_returns_none_for_non_options():
    assert error_recovery._occ_underlying("AAPL") is None
    assert error_recovery._occ_underlying("") is None
    assert error_recovery._occ_underlying(None) is None
