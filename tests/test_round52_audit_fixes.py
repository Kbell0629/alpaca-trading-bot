"""
Round-52 tests: audit-surfaced fixes for rounds 50 + 51.

11 bugs found across 5 parallel audits. Tests here pin each fix so
future rounds can't silently regress them.

Fixes covered:
  * Short-sell tier gate — cash accounts with short_selling.enabled=true
    in config.json must NOT attempt shorts
  * File locks on fractional, pdt_tracker, settled_funds — concurrent
    RMW can't lose entries
  * Migration backup rollback — if main write fails after backup
    succeeds, the backup is removed so next boot retries cleanly
  * Fractional sub-$1 whole-share fallback — eligible symbols with
    target below $1 fractional min can still get 1 whole share
  * /tmp fallback removed — missing _data_dir raises ValueError
  * Tier log dedup — Calibrated tier only logs on state change
  * Recalibrate button debounce — concurrent clicks don't double-fire
"""
from __future__ import annotations

import json
import os
import sys as _sys
import threading
from datetime import date


# ===== Short-sell tier gate =====


def test_short_sell_gate_grep(monkeypatch):
    """Pin the tier-based short-sell gate in run_auto_deployer. Cash
    accounts must NOT reach the short-sell loop even if their
    auto_deployer_config.json has short_selling.enabled=true."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "s" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    src = open(cloud_scheduler.__file__).read()
    assert 'not TIER_CFG.get("short_enabled", False)' in src, (
        "Short-sell tier gate missing — cash accounts could bypass it")
    assert "Short selling skipped" in src


# ===== Fractional sub-$1 fallback =====


def test_fractional_sub_dollar_falls_back_to_whole_share():
    """If target < $1 (below Alpaca's fractional minimum) but
    whole-share is affordable, return the whole share instead of
    rejecting outright."""
    from fractional import size_position
    tier = {"fractional_default": True}
    # Symbol not fractionable means we skip fractional anyway — test the
    # path where fractional WOULD be attempted but min-notional blocks.
    # Use a fractionable symbol but target below $1 that still lets 1
    # whole share buy at $0.50:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        user = {"_data_dir": td}
        # Seed cache so TSLA is fractionable
        import time
        with open(os.path.join(td, "fractionable_cache.json"), "w") as f:
            json.dump({"cached_at": time.time(), "symbols": ["TSLA"]}, f)
        # target=$0.50, price=$0.40 — sub-$1 fractional min rejected,
        # but whole-share $0.40 <= $0.50 so 1 share is affordable
        r = size_position("TSLA", target_dollars=0.50, price=0.40,
                           user=user, tier_cfg=tier)
        assert r["qty"] == 1, (
            f"expected 1 whole share fallback, got {r['qty']} (reason: {r['reason']})")
        assert r["fractional"] is False


# ===== Migration backup rollback =====


def test_round51_migration_rollback_on_main_write_failure(tmp_path, monkeypatch):
    """If the backup writes successfully but the main guardrails
    write FAILS, the backup must be rolled back so next boot retries
    cleanly (without the 'backup already exists' guard preventing
    re-creation)."""
    from migrations import migrate_guardrails_round51
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"max_position_pct": 0.07}, f)
    backup = gpath + ".pre-round51.backup"
    assert not os.path.exists(backup)

    # Force the main write to fail AFTER the backup succeeds
    import migrations
    orig_save = migrations._save_json_atomic
    def _failing_save(path, data):
        if path == gpath:
            raise OSError("simulated disk full")
        return orig_save(path, data)
    monkeypatch.setattr(migrations, "_save_json_atomic", _failing_save)

    def fetcher(u):
        return {"multiplier": "1", "equity": "10000"}

    user = {"id": 1, "_data_dir": str(tmp_path)}
    result = migrate_guardrails_round51(gpath, user, account_fetcher=fetcher)
    assert result.startswith("error:"), f"expected error, got {result}"
    # Backup must be rolled back so next boot re-migrates
    assert not os.path.exists(backup), (
        "backup was NOT rolled back after main write failed — "
        "subsequent boots will skip creating fresh backup")


def test_round51_preserves_pre_existing_backup_on_failure(tmp_path, monkeypatch):
    """If backup ALREADY existed before this call (from a prior aborted
    migration), we must NOT delete it on main-write failure. Only
    backups we created in this call get rolled back."""
    from migrations import migrate_guardrails_round51
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"max_position_pct": 0.07}, f)
    backup = gpath + ".pre-round51.backup"
    # Pre-existing backup from an earlier run
    with open(backup, "w") as f:
        f.write('{"from_prior_run": true}')

    import migrations
    orig_save = migrations._save_json_atomic
    def _failing_save(path, data):
        if path == gpath:
            raise OSError("disk full")
        return orig_save(path, data)
    monkeypatch.setattr(migrations, "_save_json_atomic", _failing_save)

    def fetcher(u):
        return {"multiplier": "1", "equity": "10000"}

    user = {"id": 1, "_data_dir": str(tmp_path)}
    migrate_guardrails_round51(gpath, user, account_fetcher=fetcher)
    # Pre-existing backup still there with original content
    assert os.path.exists(backup)
    with open(backup) as f:
        assert "from_prior_run" in f.read()


# ===== /tmp fallback removal =====


def test_fractional_cache_path_raises_without_data_dir():
    from fractional import _cache_path
    try:
        _cache_path({})  # no _data_dir key
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "_data_dir" in str(e)


def test_settled_funds_ledger_path_raises_without_data_dir():
    from settled_funds import _ledger_path
    try:
        _ledger_path({})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "_data_dir" in str(e)


# ===== File locks on concurrent RMW =====


def test_settled_funds_record_sale_under_concurrent_writes(tmp_path):
    """20 threads each call record_sale() concurrently. Without the
    file lock, some writes would be lost due to the load-append-save
    race. With the lock, all 20 entries should be present."""
    from settled_funds import record_sale, _load_ledger
    user = {"_data_dir": str(tmp_path)}

    def worker(i):
        record_sale(user, f"SYM{i}", proceeds=100.0 + i,
                     sold_on=date.today())

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()

    ledger = _load_ledger(user)
    assert len(ledger) == 20, (
        f"expected 20 entries, got {len(ledger)} — lost entries from race. "
        "File lock missing or broken in settled_funds.record_sale.")


def test_pdt_log_under_concurrent_writes(tmp_path):
    """Same test for pdt_tracker.log_day_trade."""
    from pdt_tracker import log_day_trade
    user = {"_data_dir": str(tmp_path)}
    # Make sure data dir exists (log_day_trade checks os.path.isdir)
    os.makedirs(str(tmp_path), exist_ok=True)

    def worker(i):
        log_day_trade(user, f"SYM{i}", strategy="test")

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()

    log_path = str(tmp_path / "pdt_day_trades.json")
    assert os.path.exists(log_path)
    with open(log_path) as f:
        log = json.load(f)
    assert len(log) == 20, (
        f"expected 20 PDT log entries, got {len(log)} — lost entries from race")


# ===== Tier log dedup =====


def test_tier_log_dedup_grep(monkeypatch):
    """Pin that run_auto_deployer only logs Calibrated tier on state
    change (not every tick)."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "s" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    src = open(cloud_scheduler.__file__).read()
    # Look for the dedup pattern
    assert "_tier_key = f\"tier_last_" in src
    assert "_last_tier != _current_tier_str" in src


# ===== Recalibrate button debounce =====


def test_calibration_button_has_debounce():
    """Pin the debounce flag on loadCalibration in the dashboard JS."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "_calibrationInFlight" in src
    assert "if (_calibrationInFlight)" in src
    # Must also re-enable in finally
    assert "_calibrationInFlight = false" in src


# ===== Observability integration =====


def test_fractional_refresh_routes_errors_to_sentry_grep():
    """Fractional API errors should route through observability.capture_exception
    so systematic failures surface in Sentry."""
    with open("fractional.py") as f:
        src = f.read()
    assert "capture_exception" in src
    assert "fractional.refresh_cache" in src  # component tag


def test_settled_funds_record_sale_routes_errors_to_sentry_grep():
    with open("settled_funds.py") as f:
        src = f.read()
    assert "capture_exception" in src
    assert "settled_funds.record_sale" in src


# ===== Migration edge cases (audit-requested) =====


def test_round51_migration_malformed_guardrails(tmp_path):
    """If guardrails.json is valid JSON but NOT a dict (list, int, str),
    migration should treat it as 'no_file' and not crash."""
    from migrations import migrate_guardrails_round51
    gpath = str(tmp_path / "guardrails.json")
    # Valid JSON but not a dict
    with open(gpath, "w") as f:
        json.dump(["not", "a", "dict"], f)
    user = {"id": 1, "_data_dir": str(tmp_path)}
    result = migrate_guardrails_round51(
        gpath, user, account_fetcher=lambda u: {"multiplier": "1", "equity": "10000"})
    assert result == "no_file"


def test_round51_migration_account_fetcher_raises(tmp_path):
    """If account_fetcher raises instead of returning None, migration
    should catch the exception and return 'error:<type>' without
    stamping (so next boot retries)."""
    from migrations import migrate_guardrails_round51, MIGRATION_ROUND51_CALIBRATION_ADOPT
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"max_position_pct": 0.07}, f)
    user = {"id": 1, "_data_dir": str(tmp_path)}

    def raising_fetcher(u):
        raise ConnectionError("Alpaca unreachable")

    result = migrate_guardrails_round51(
        gpath, user, account_fetcher=raising_fetcher)
    assert result.startswith("error:"), f"got {result}"
    # Stamp NOT written — next boot retries
    with open(gpath) as f:
        g = json.load(f)
    assert MIGRATION_ROUND51_CALIBRATION_ADOPT not in g.get("_migrations_applied", [])


# ===== /api/calibration endpoint =====


def test_api_calibration_endpoint_returns_detected_shape(tmp_path, monkeypatch):
    """Pin the /api/calibration response shape — the dashboard renderer
    depends on these keys existing. A silent field removal would
    break the UI."""
    # Grep-level pin on server.py is the simplest test; the end-to-end
    # would need a real session cookie which our infra doesn't spin up
    # easily in unit tests.
    with open("server.py") as f:
        src = f.read()
    assert 'path == "/api/calibration"' in src
    assert "pc.detect_tier" in src
    assert "pc.calibration_summary" in src
    # Must return the raw Alpaca-reported state the UI surfaces
    assert '"equity"' in src
    assert '"buying_power"' in src
    assert '"day_trades_remaining"' in src


def test_api_calibration_handler_requires_auth(monkeypatch):
    """Confirm the endpoint branch is under the check_auth() block —
    can't be hit unauthenticated."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "s" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler", "server"):
        _sys.modules.pop(m, None)
    import server
    src = open(server.__file__).read()
    # Rough proxy: /api/calibration appears AFTER `if check_auth` fence
    idx_auth = src.find("def check_auth")
    idx_cal = src.find('path == "/api/calibration"')
    assert idx_cal > idx_auth
    # And the handler dereferences self.current_user (would be None
    # if unauth) — see the account fetch
    assert "self.current_user" in src[idx_cal:idx_cal+4000]
