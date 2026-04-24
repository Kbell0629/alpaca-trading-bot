"""Round-61 pt.22 — multi-contract wheel support.

Previously (pt.17): if the user had TWO short puts on the same
underlying (e.g. HIMS260508P00027000 + HIMS260515P00026000), the
orphan-adoption path created `wheel_HIMS.json` for the first
contract encountered and silently skipped the second ("wheel file
already exists, leaving alone"). The second contract stayed MANUAL
forever.

pt.22 fix: when the default `wheel_<UNDERLYING>.json` is already
tracking a DIFFERENT contract, create an indexed sibling file
`wheel_<UNDERLYING>__<YYMMDD><C|P><STRIKE8>.json`. Each file has
its own active_contract pinned to one OCC symbol; the wheel monitor
handles them independently. Dashboard's `_mark_auto_deployed` still
resolves both to the underlying (via the JSON "symbol" field +
double-underscore filename parser).
"""
from __future__ import annotations

import json


def test_second_contract_on_same_underlying_creates_indexed_file(
        tmp_path, monkeypatch):
    """Two HIMS short puts → wheel_HIMS.json for the first,
    wheel_HIMS__<contract>.json for the second."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    monkeypatch.setattr(er, "DATA_DIR", str(tmp_path))

    positions = [
        {
            "symbol": "HIMS260508P00027000",
            "qty": "-1", "avg_entry_price": "2.05",
            "current_price": "1.07",
        },
        {
            "symbol": "HIMS260515P00026000",
            "qty": "-1", "avg_entry_price": "1.80",
            "current_price": "0.95",
        },
    ]
    orders = []

    def _fake_get(url, timeout=15, max_retries=3):
        if "positions" in url:
            return positions
        if "orders" in url:
            return orders
        return []
    monkeypatch.setattr(er, "api_get_with_retry", _fake_get)
    monkeypatch.setattr(er, "api_post", lambda *a, **kw: {"id": "fake"})

    er.main()

    # First contract → default filename.
    assert (sdir / "wheel_HIMS.json").exists()
    first = json.loads((sdir / "wheel_HIMS.json").read_text())
    assert first["active_contract"]["contract_symbol"] in (
        "HIMS260508P00027000", "HIMS260515P00026000")

    # Second contract → indexed filename.
    indexed_files = [f for f in sdir.iterdir()
                     if f.name.startswith("wheel_HIMS__")
                     and f.name.endswith(".json")]
    assert len(indexed_files) == 1, (
        f"Expected 1 indexed wheel file for the second HIMS contract, "
        f"got: {[f.name for f in sdir.iterdir()]}")
    second = json.loads(indexed_files[0].read_text())
    # The indexed file tracks the OTHER contract.
    assert second["active_contract"]["contract_symbol"] != first["active_contract"]["contract_symbol"]
    # Both are proper wheel schemas.
    assert second["strategy"] == "wheel"
    assert second["stage"] == "stage_1_put_active"
    assert second["symbol"] == "HIMS"


def test_indexed_filename_uses_contract_details():
    """The indexed filename format is
    `wheel_<UNDERLYING>__<YYMMDD><P|C><STRIKE8>.json` so it's stable
    + reversible."""
    import error_recovery as er
    # _occ_parse produces: {underlying, expiration, right, strike}
    parsed = er._occ_parse("HIMS260515P00026000")
    assert parsed["underlying"] == "HIMS"
    assert parsed["expiration"] == "2026-05-15"
    assert parsed["strike"] == 26.0
    assert parsed["right"] == "put"
    # Expected filename shape: wheel_HIMS__260515P00026000.json


def test_dashboard_labels_both_contracts_as_auto_wheel(tmp_path, monkeypatch):
    """After two HIMS puts are adopted, _mark_auto_deployed should
    recognise both and label them AUTO+WHEEL (via the underlying
    lookup, not the contract-symbol lookup)."""
    import server
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    # Two wheel files: default + indexed.
    (sdir / "wheel_HIMS.json").write_text(json.dumps({
        "symbol": "HIMS", "strategy": "wheel", "status": "active",
        "stage": "stage_1_put_active",
        "active_contract": {"contract_symbol": "HIMS260508P00027000",
                            "type": "put", "strike": 27.0,
                            "expiration": "2026-05-08"},
    }))
    (sdir / "wheel_HIMS__260515P00026000.json").write_text(json.dumps({
        "symbol": "HIMS", "strategy": "wheel", "status": "active",
        "stage": "stage_1_put_active",
        "active_contract": {"contract_symbol": "HIMS260515P00026000",
                            "type": "put", "strike": 26.0,
                            "expiration": "2026-05-15"},
    }))

    positions = [
        {"symbol": "HIMS260508P00027000", "qty": "-1",
         "asset_class": "us_option", "current_price": "1.00",
         "avg_entry_price": "2.05"},
        {"symbol": "HIMS260515P00026000", "qty": "-1",
         "asset_class": "us_option", "current_price": "0.90",
         "avg_entry_price": "1.80"},
    ]
    result = server._mark_auto_deployed(positions, str(sdir), user_dir=str(tmp_path))
    # Both should be labeled AUTO + wheel.
    for p in result:
        assert p.get("_auto_deployed") is True, (
            f"{p['symbol']} should be AUTO, got: {p}")
        assert p.get("_strategy") == "wheel"


def test_audit_core_parses_indexed_wheel_filename():
    """audit_core._parse_strategy_filename must extract the
    underlying from wheel_<UNDERLYING>__<suffix>.json."""
    import audit_core
    prefix, sym = audit_core._parse_strategy_filename(
        "wheel_HIMS__260515P00026000.json")
    assert prefix == "wheel"
    assert sym == "HIMS"


def test_audit_core_still_parses_regular_wheel_filename():
    """Regular wheel_<UNDER>.json (no double underscore) still
    works."""
    import audit_core
    prefix, sym = audit_core._parse_strategy_filename("wheel_DKNG.json")
    assert prefix == "wheel"
    assert sym == "DKNG"


def test_audit_no_orphan_flag_when_indexed_wheel_exists(tmp_path):
    """If both default + indexed wheel files exist, audit must not
    flag either contract as orphan."""
    import audit_core
    strategy_files = {
        "wheel_HIMS.json": {
            "symbol": "HIMS", "strategy": "wheel", "status": "active",
            "stage": "stage_1_put_active",
            "active_contract": {"contract_symbol": "HIMS260508P00027000"},
        },
        "wheel_HIMS__260515P00026000.json": {
            "symbol": "HIMS", "strategy": "wheel", "status": "active",
            "stage": "stage_1_put_active",
            "active_contract": {"contract_symbol": "HIMS260515P00026000"},
        },
    }
    positions = [
        {"symbol": "HIMS260508P00027000", "qty": "-1",
         "asset_class": "us_option", "current_price": "1.00"},
        {"symbol": "HIMS260515P00026000", "qty": "-1",
         "asset_class": "us_option", "current_price": "0.90"},
    ]
    report = audit_core.run_audit(
        positions=positions,
        orders=[],
        strategy_files=strategy_files,
        journal={"trades": []},
        scorecard={},
    )
    orphans = [f for f in report["findings"]
               if f["category"] == "orphan_position"]
    assert not orphans, (
        f"Neither HIMS contract should be an orphan. Got: {orphans}")
