"""Round-61 pt.21 — central constants, legacy-file migration, audit
endpoint, and auto_deployer strategy-list widening.

Implements the 5-point cleanup proposed in the pt.21 planning:
  1. Central `constants.STRATEGY_NAMES` + `CLOSED_STATUSES` so every
     consumer reads from the same source (prevents pt.16/19/20-class
     key drift).
  2. `error_recovery.migrate_legacy_short_sell_option_files` retires
     pre-pt.17 `short_sell_<OCC>.json` files and replaces them with
     `wheel_<UNDERLYING>.json`. Fixes DKNG + HIMS mis-routing
     observed in the user's production JSON audit.
  3. `/api/audit` endpoint + `audit_core.run_audit` — cross-checks
     positions/orders/strategy-files/journal/scorecard for
     inconsistencies, returns a structured severity-grouped report.
  4. Dashboard 🔍 Audit button shows the report in a modal.
  5. `migrate_auto_deployer_strategies_round61_pt21` widens
     `auto_deployer_config.strategies` to include wheel/pead/short_sell
     so those are also eligible for the unified deployer loop.
"""
from __future__ import annotations

import json


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# constants.py — single source of truth
# ----------------------------------------------------------------------------

def test_constants_module_exports_strategy_names():
    from constants import STRATEGY_NAMES
    # Every strategy the bot produces must be in the set.
    expected = {"trailing_stop", "breakout", "mean_reversion", "wheel",
                "short_sell", "pead", "copy_trading"}
    assert expected.issubset(STRATEGY_NAMES), (
        f"STRATEGY_NAMES missing expected entries. Got: {STRATEGY_NAMES}")


def test_constants_module_exports_closed_statuses():
    from constants import CLOSED_STATUSES
    expected = {"closed", "stopped", "cancelled", "canceled",
                "exited", "filled_and_closed"}
    assert expected.issubset(CLOSED_STATUSES)


def test_is_closed_status_case_insensitive():
    from constants import is_closed_status
    assert is_closed_status("closed") is True
    assert is_closed_status("CLOSED") is True
    assert is_closed_status("Closed") is True
    assert is_closed_status("cancelled") is True
    assert is_closed_status("filled_and_closed") is True
    assert is_closed_status("active") is False
    assert is_closed_status("") is False
    assert is_closed_status(None) is False
    # migrated is NOT in the closed set — it's a separate pt.21 marker
    assert is_closed_status("migrated") is False


def test_is_known_strategy_helper():
    from constants import is_known_strategy
    assert is_known_strategy("wheel") is True
    assert is_known_strategy("WHEEL") is True
    assert is_known_strategy("short_sell") is True
    assert is_known_strategy("unknown_strategy") is False
    assert is_known_strategy("") is False
    assert is_known_strategy(None) is False


def test_scorecard_buckets_derived_from_constants():
    """scorecard_core.STRATEGY_BUCKETS must now be built from
    constants.STRATEGY_NAMES so the pt.19-class bug (adding a new
    strategy but forgetting to update BUCKETS) can never happen."""
    from constants import STRATEGY_NAMES
    import scorecard_core as sc
    assert set(sc.STRATEGY_BUCKETS) == set(STRATEGY_NAMES), (
        f"STRATEGY_BUCKETS ({sc.STRATEGY_BUCKETS}) must match "
        f"STRATEGY_NAMES ({STRATEGY_NAMES})")


# ----------------------------------------------------------------------------
# error_recovery.migrate_legacy_short_sell_option_files
# ----------------------------------------------------------------------------

def test_migration_retrofits_short_sell_occ_to_wheel(tmp_path, monkeypatch):
    """The DKNG scenario: pre-pt.17 error_recovery wrote
    short_sell_DKNG260515P00021000.json for a short put. After pt.21
    migration, a wheel_DKNG.json should exist with stage_1_put_active,
    and the old file should be marked migrated."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))

    # Seed the legacy file.
    legacy = {
        "symbol": "DKNG260515P00021000",
        "strategy": "short_sell",
        "status": "active",
        "entry_price_estimate": 1.00,
        "initial_qty": 1,
        "state": {
            "entry_fill_price": 1.00,
            "shares_shorted": 1,
            "cover_order_id": "existing-cover-order-id",
            "current_stop_price": 1.10,
        },
    }
    (sdir / "short_sell_DKNG260515P00021000.json").write_text(json.dumps(legacy))

    events = er.migrate_legacy_short_sell_option_files()

    # wheel file should now exist.
    assert (sdir / "wheel_DKNG.json").exists(), (
        f"Migration should create wheel_DKNG.json. Events: {events}")
    wheel = json.loads((sdir / "wheel_DKNG.json").read_text())
    assert wheel["stage"] == "stage_1_put_active"
    assert wheel["active_contract"]["contract_symbol"] == "DKNG260515P00021000"
    assert wheel["active_contract"]["strike"] == 21.0
    assert wheel["active_contract"]["type"] == "put"
    assert wheel["active_contract"]["expiration"] == "2026-05-15"
    # Legacy file retired.
    legacy_after = json.loads((sdir / "short_sell_DKNG260515P00021000.json").read_text())
    assert legacy_after["status"] == "migrated"
    assert legacy_after["_migrated_to"] == "wheel_DKNG.json"


def test_migration_preserves_existing_wheel_file(tmp_path, monkeypatch):
    """If wheel_<UNDERLYING>.json already exists, migration must not
    overwrite it — the wheel monitor owns that state. The legacy file
    should still be retired."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))

    existing_wheel = {
        "symbol": "HIMS",
        "strategy": "wheel",
        "status": "active",
        "stage": "stage_1_put_active",
        "_sentinel": "existing wheel state",
    }
    (sdir / "wheel_HIMS.json").write_text(json.dumps(existing_wheel))
    legacy = {
        "symbol": "HIMS260508P00027000",
        "strategy": "short_sell",
        "status": "active",
        "entry_price_estimate": 2.05,
        "state": {"shares_shorted": 1, "entry_fill_price": 2.05},
    }
    (sdir / "short_sell_HIMS260508P00027000.json").write_text(json.dumps(legacy))

    er.migrate_legacy_short_sell_option_files()

    wheel_after = json.loads((sdir / "wheel_HIMS.json").read_text())
    assert wheel_after.get("_sentinel") == "existing wheel state"
    legacy_after = json.loads((sdir / "short_sell_HIMS260508P00027000.json").read_text())
    assert legacy_after["status"] == "migrated"


def test_migration_skips_equity_short_sell_files(tmp_path, monkeypatch):
    """Non-OCC short_sell files (e.g. short_sell_SOXL.json) are
    legitimate equity shorts. Must NOT be migrated."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))

    (sdir / "short_sell_SOXL.json").write_text(json.dumps({
        "symbol": "SOXL", "strategy": "short_sell", "status": "active"}))

    er.migrate_legacy_short_sell_option_files()

    # SOXL file unchanged, no wheel file.
    soxl = json.loads((sdir / "short_sell_SOXL.json").read_text())
    assert soxl["status"] == "active"
    assert not (sdir / "wheel_SOXL.json").exists()


def test_migration_skips_already_migrated_files(tmp_path, monkeypatch):
    """Idempotency — running migration twice must not double-retire."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))

    (sdir / "short_sell_HIMS260508P00027000.json").write_text(json.dumps({
        "symbol": "HIMS260508P00027000", "strategy": "short_sell",
        "status": "migrated", "_migrated_to": "wheel_HIMS.json"}))

    events = er.migrate_legacy_short_sell_option_files()
    # No events — file was already migrated.
    assert events == []


# ----------------------------------------------------------------------------
# audit_core.run_audit — behavioral
# ----------------------------------------------------------------------------

def test_audit_clean_state_returns_no_findings():
    import audit_core
    report = audit_core.run_audit(
        positions=[{"symbol": "CRDO", "qty": "18", "current_price": "197.00",
                     "asset_class": "us_equity"}],
        orders=[{"symbol": "CRDO", "side": "sell", "type": "stop",
                 "stop_price": "180.00"}],
        strategy_files={"breakout_CRDO.json": {"symbol": "CRDO",
                                                 "strategy": "breakout",
                                                 "status": "active"}},
        journal={"trades": []},
        scorecard={},
    )
    assert report["clean"] is True
    assert report["findings"] == []


def test_audit_flags_orphan_position():
    import audit_core
    report = audit_core.run_audit(
        positions=[{"symbol": "SOXL", "qty": "-29", "current_price": "129.00",
                     "asset_class": "us_equity"}],
        orders=[],
        strategy_files={},
        journal={"trades": []},
        scorecard={},
    )
    findings = [f for f in report["findings"]
                if f["category"] == "orphan_position"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["symbol"] == "SOXL"


def test_audit_flags_legacy_occ_mis_routing():
    import audit_core
    report = audit_core.run_audit(
        positions=[{"symbol": "DKNG260515P00021000", "qty": "-1",
                     "current_price": "0.95",
                     "asset_class": "us_option"}],
        orders=[],
        strategy_files={
            "short_sell_DKNG260515P00021000.json": {
                "symbol": "DKNG260515P00021000",
                "strategy": "short_sell", "status": "active"},
        },
        journal={"trades": []},
        scorecard={},
    )
    findings = [f for f in report["findings"]
                if f["category"] == "legacy_occ_mis_routed"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "MEDIUM"


def test_audit_skips_migrated_files():
    import audit_core
    report = audit_core.run_audit(
        positions=[{"symbol": "DKNG260515P00021000", "qty": "-1",
                     "current_price": "0.95",
                     "asset_class": "us_option"}],
        orders=[{"symbol": "DKNG260515P00021000", "side": "buy",
                 "type": "stop", "stop_price": "1.10"}],
        strategy_files={
            # Migrated legacy file — should not count as active
            "short_sell_DKNG260515P00021000.json": {
                "status": "migrated"},
            # New wheel file — counts as active
            "wheel_DKNG.json": {"symbol": "DKNG", "strategy": "wheel",
                                 "status": "active"},
        },
        journal={"trades": []},
        scorecard={},
    )
    # No legacy_occ_mis_routed (that file is marked migrated)
    assert not any(f["category"] == "legacy_occ_mis_routed"
                   for f in report["findings"])
    # No orphan (wheel_DKNG.json claims it)
    assert not any(f["category"] == "orphan_position"
                   for f in report["findings"])


def test_audit_flags_missing_stop():
    import audit_core
    report = audit_core.run_audit(
        positions=[{"symbol": "SOXL", "qty": "-29", "current_price": "129.00",
                     "asset_class": "us_equity"}],
        orders=[{"symbol": "SOXL", "side": "buy", "type": "limit",
                 "limit_price": "94.00"}],  # limit only, no stop
        strategy_files={"short_sell_SOXL.json": {"symbol": "SOXL",
                                                   "strategy": "short_sell",
                                                   "status": "active"}},
        journal={"trades": []},
        scorecard={},
    )
    findings = [f for f in report["findings"]
                if f["category"] == "missing_stop"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["symbol"] == "SOXL"


def test_audit_flags_invalid_short_stop_below_market():
    """Short cover-stop must be ABOVE current. Stale stops from
    pre-pt.17 may be below market."""
    import audit_core
    report = audit_core.run_audit(
        positions=[{"symbol": "SOXL", "qty": "-29", "current_price": "129.00",
                     "asset_class": "us_equity"}],
        orders=[{"symbol": "SOXL", "side": "buy", "type": "stop",
                 "stop_price": "121.72"}],  # BELOW current — invalid
        strategy_files={"short_sell_SOXL.json": {"symbol": "SOXL",
                                                   "strategy": "short_sell",
                                                   "status": "active"}},
        journal={"trades": []},
        scorecard={},
    )
    findings = [f for f in report["findings"]
                if f["category"] == "invalid_stop_price"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"


def test_audit_flags_unknown_strategy_name_in_journal():
    import audit_core
    report = audit_core.run_audit(
        positions=[],
        orders=[],
        strategy_files={},
        journal={"trades": [
            {"symbol": "FOO", "strategy": "imaginary_strategy",
             "status": "open", "deployer": "cloud_scheduler"},
        ]},
        scorecard={},
    )
    findings = [f for f in report["findings"]
                if f["category"] == "unknown_strategy_name"]
    assert len(findings) == 1


def test_audit_flags_stale_scorecard():
    import audit_core
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(hours=72)
    report = audit_core.run_audit(
        positions=[],
        orders=[],
        strategy_files={},
        journal={"trades": []},
        scorecard={"last_updated": old.isoformat()},
    )
    findings = [f for f in report["findings"]
                if f["category"] == "stale_scorecard"]
    assert len(findings) == 1


def test_audit_does_not_flag_wheel_positions_for_missing_stop():
    """Wheel-managed positions don't use equity stops by design."""
    import audit_core
    report = audit_core.run_audit(
        positions=[{"symbol": "DKNG260515P00021000", "qty": "-1",
                     "current_price": "0.95",
                     "asset_class": "us_option"}],
        orders=[],
        strategy_files={
            "wheel_DKNG.json": {"symbol": "DKNG", "strategy": "wheel",
                                 "status": "active",
                                 "stage": "stage_1_put_active"},
        },
        journal={"trades": []},
        scorecard={},
    )
    assert not any(f["category"] == "missing_stop"
                   for f in report["findings"])


# ----------------------------------------------------------------------------
# /api/audit endpoint
# ----------------------------------------------------------------------------

def test_audit_endpoint_requires_auth(http_harness):
    http_harness.create_user()
    http_harness.logout()
    resp = http_harness.post("/api/audit", body={})
    assert resp["status"] in (401, 403)


def test_audit_endpoint_returns_structured_report(http_harness):
    http_harness.create_user()
    resp = http_harness.post("/api/audit", body={})
    assert resp["status"] == 200
    assert resp["body"].get("success") is True
    report = resp["body"].get("report") or {}
    assert "findings" in report
    assert "counts" in report
    assert "clean" in report


def test_audit_endpoint_route_exists():
    src = _src("server.py")
    assert '"/api/audit"' in src
    assert "handle_state_audit" in src


# ----------------------------------------------------------------------------
# Dashboard UI
# ----------------------------------------------------------------------------

def test_dashboard_has_audit_button():
    src = _src("templates/dashboard.html")
    assert 'onclick="runStateAudit()"' in src


def test_dashboard_has_runStateAudit_js():
    src = _src("templates/dashboard.html")
    assert "async function runStateAudit" in src
    assert "/api/audit" in src


# ----------------------------------------------------------------------------
# auto_deployer_config strategy-list widening
# ----------------------------------------------------------------------------

def test_migration_widens_strategies_list(tmp_path):
    import migrations
    config = {
        "strategies": ["trailing_stop", "mean_reversion", "breakout"],
        "_migrations_applied": [],
    }
    path = tmp_path / "auto_deployer_config.json"
    path.write_text(json.dumps(config))
    result = migrations.migrate_auto_deployer_strategies_round61_pt21(str(path))
    assert result == "migrated"
    after = json.loads(path.read_text())
    # Original entries preserved, new ones added.
    for s in ("trailing_stop", "mean_reversion", "breakout",
              "wheel", "pead", "short_sell", "copy_trading"):
        assert s in after["strategies"], f"Missing: {s}"
    assert "round61_pt21_auto_deployer_strategies_full" in after["_migrations_applied"]


def test_migration_idempotent(tmp_path):
    import migrations
    path = tmp_path / "auto_deployer_config.json"
    path.write_text(json.dumps({
        "strategies": ["trailing_stop"],
        "_migrations_applied": ["round61_pt21_auto_deployer_strategies_full"],
    }))
    result = migrations.migrate_auto_deployer_strategies_round61_pt21(str(path))
    assert result == "already_applied"


def test_migration_handles_missing_file(tmp_path):
    import migrations
    result = migrations.migrate_auto_deployer_strategies_round61_pt21(
        str(tmp_path / "nonexistent.json"))
    assert result == "no_file"
