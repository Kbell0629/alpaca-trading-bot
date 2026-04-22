"""
Round-51 tests: auto-activate portfolio calibration for existing users.

Covers:
  * migrate_guardrails_round51 — adopts tier defaults + backs up old
    guardrails + stamps idempotent
  * run_all_migrations — threads account_fetcher through
  * cloud_scheduler settled-funds gate integration (grep-level pin)
  * cloud_scheduler fractional routing integration (grep-level pin)
  * record_trade_close writes to settled_funds ledger on sells
"""
from __future__ import annotations

import json
import os
import sys as _sys


def _acct(multiplier, equity, **kw):
    a = {"multiplier": str(multiplier), "equity": str(equity),
         "cash": str(kw.get("cash", equity * 0.5)),
         "cash_withdrawable": str(kw.get("cash_withdrawable",
                                          kw.get("cash", equity * 0.5))),
         "pattern_day_trader": kw.get("pattern_day_trader", False),
         "shorting_enabled": kw.get("shorting_enabled", multiplier >= 2),
         "day_trades_remaining": kw.get("day_trades_remaining", None),
         "buying_power": str(kw.get("buying_power", equity * multiplier))}
    return a


# ========== migrate_guardrails_round51 ==========


def test_round51_no_file_skips(tmp_path):
    from migrations import migrate_guardrails_round51
    user = {"id": 1, "_data_dir": str(tmp_path)}
    result = migrate_guardrails_round51(
        str(tmp_path / "guardrails.json"),
        user, account_fetcher=lambda u: _acct(1, 10000))
    assert result == "no_file"


def test_round51_migrates_existing_user_to_tier_defaults(tmp_path):
    from migrations import migrate_guardrails_round51
    # Existing user with old guardrails — Cash Standard tier target
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({
            "max_position_pct": 0.07,
            "daily_loss_limit_pct": 0.03,  # should be preserved
            "earnings_exit_days_before": 1,  # should be preserved
        }, f)

    user = {"id": 1, "_data_dir": str(tmp_path)}
    result = migrate_guardrails_round51(
        gpath, user, account_fetcher=lambda u: _acct(1, 100000))
    assert result == "migrated"

    # Post-migration: tier defaults filled in
    with open(gpath) as f:
        g = json.load(f)
    assert g.get("_round51_tier_adopted") == "cash_standard"
    assert "fractional_enabled" in g
    assert "strategies_enabled" in g
    # User's non-tier keys preserved
    assert g.get("daily_loss_limit_pct") == 0.03
    assert g.get("earnings_exit_days_before") == 1
    # Stamp present
    from migrations import MIGRATION_ROUND51_CALIBRATION_ADOPT
    assert MIGRATION_ROUND51_CALIBRATION_ADOPT in g.get("_migrations_applied", [])

    # Backup file written
    assert os.path.exists(gpath + ".pre-round51.backup")


def test_round51_idempotent_second_run_noop(tmp_path):
    from migrations import migrate_guardrails_round51
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"max_position_pct": 0.07}, f)
    user = {"id": 1, "_data_dir": str(tmp_path)}
    first = migrate_guardrails_round51(
        gpath, user, account_fetcher=lambda u: _acct(1, 100000))
    assert first == "migrated"
    second = migrate_guardrails_round51(
        gpath, user, account_fetcher=lambda u: _acct(1, 100000))
    assert second == "already_applied"


def test_round51_no_tier_leaves_stamp_off(tmp_path):
    """If Alpaca /account returns None or equity < $500, we should NOT
    stamp — leave the door open to retry next boot when Alpaca is up."""
    from migrations import migrate_guardrails_round51, MIGRATION_ROUND51_CALIBRATION_ADOPT
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({"max_position_pct": 0.07}, f)
    user = {"id": 1, "_data_dir": str(tmp_path)}
    result = migrate_guardrails_round51(
        gpath, user, account_fetcher=lambda u: None)
    assert result == "no_tier"
    with open(gpath) as f:
        g = json.load(f)
    assert MIGRATION_ROUND51_CALIBRATION_ADOPT not in g.get("_migrations_applied", [])


def test_round51_cash_micro_gets_fractional_on(tmp_path):
    """Jon's $500 cash account (when he flips live) — Cash Micro tier.
    After migration, fractional_enabled should be True."""
    from migrations import migrate_guardrails_round51
    gpath = str(tmp_path / "guardrails.json")
    with open(gpath, "w") as f:
        json.dump({}, f)  # empty — simulate fresh user who already had the file
    user = {"id": 99, "_data_dir": str(tmp_path)}
    result = migrate_guardrails_round51(
        gpath, user, account_fetcher=lambda u: _acct(1, 600))
    assert result == "migrated"
    with open(gpath) as f:
        g = json.load(f)
    assert g["_round51_tier_adopted"] == "cash_micro"
    assert g["fractional_enabled"] is True
    assert g["max_positions"] == 2
    assert g["max_position_pct"] == 0.15


def test_round51_backup_not_overwritten_on_reruns(tmp_path):
    """If backup already exists (from a prior aborted run), don't
    overwrite it — protect whatever earliest snapshot we have."""
    from migrations import migrate_guardrails_round51
    gpath = str(tmp_path / "guardrails.json")
    backup = gpath + ".pre-round51.backup"
    # Pre-create backup with known content
    with open(gpath, "w") as f:
        f.write('{"max_position_pct": 0.10}')
    with open(backup, "w") as f:
        f.write('{"original_state": "preserved"}')

    user = {"id": 1, "_data_dir": str(tmp_path)}
    migrate_guardrails_round51(
        gpath, user, account_fetcher=lambda u: _acct(1, 100000))

    # Backup still has the ORIGINAL content, not overwritten
    with open(backup) as f:
        assert "preserved" in f.read()


# ========== run_all_migrations threads account_fetcher ==========


def test_run_all_migrations_passes_account_fetcher(tmp_path):
    """Pin that run_all_migrations has the new account_fetcher kwarg
    and passes it into migrate_guardrails_round51."""
    import migrations
    import inspect
    sig = inspect.signature(migrations.run_all_migrations)
    assert "account_fetcher" in sig.parameters


def test_run_all_migrations_calls_round51(tmp_path):
    import migrations
    # Set up a fake user + account
    user_dir = tmp_path / "u1"
    user_dir.mkdir()
    with open(user_dir / "guardrails.json", "w") as f:
        json.dump({"max_position_pct": 0.07}, f)
    users = [{"id": 1, "_data_dir": str(user_dir)}]

    def user_file_fn(u, name):
        return str(user_dir / name)

    def fetcher(u):
        return _acct(1, 100000)

    summary = migrations.run_all_migrations(users, user_file_fn,
                                              account_fetcher=fetcher)
    assert summary[1].get("round51_calibration_adopt") == "migrated"


def test_run_all_migrations_skips_round51_when_no_fetcher(tmp_path):
    """Without account_fetcher, round-51 is quietly skipped (tier
    unknown) — defers to next boot."""
    import migrations
    user_dir = tmp_path / "u1"
    user_dir.mkdir()
    with open(user_dir / "guardrails.json", "w") as f:
        json.dump({}, f)
    users = [{"id": 1, "_data_dir": str(user_dir)}]

    def user_file_fn(u, name):
        return str(user_dir / name)

    summary = migrations.run_all_migrations(users, user_file_fn,
                                              account_fetcher=None)
    assert "round51_calibration_adopt" not in summary[1]


# ========== Integration grep-level pins ==========


def test_deployer_has_settled_funds_gate(monkeypatch):
    """Pin: run_auto_deployer consults settled_funds.can_deploy before
    sizing a buy. If someone tears this out, cash users could trigger
    Good Faith Violations."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "h" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    src = open(cloud_scheduler.__file__).read()
    assert "settled_funds_required" in src
    assert "can_deploy(" in src
    assert "import settled_funds as _sf" in src


def test_deployer_has_fractional_routing(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "h" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    src = open(cloud_scheduler.__file__).read()
    assert "import fractional as _fr" in src
    assert "_fr.size_position(" in src
    assert "fractional=_use_fractional" in src


def test_record_trade_close_writes_to_settled_funds(tmp_path, monkeypatch):
    """Pin: when a long position closes (side='sell'), record_trade_close
    adds an entry to the settled-funds ledger so the next cash-account
    deploy respects the T+1 rule."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "h" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler as cs

    # Seed a minimal trade_journal.json so record_trade_close has
    # something to close (otherwise it hits the orphan-close path;
    # settled_funds still records).
    user_dir = tmp_path
    user = {"id": 1, "username": "test", "_data_dir": str(user_dir)}
    with open(os.path.join(str(user_dir), "trade_journal.json"), "w") as f:
        json.dump({"trades": [{
            "symbol": "AAPL", "strategy": "breakout",
            "side": "buy", "price": 100, "qty": 10,
            "status": "open", "timestamp": "2026-04-22T10:00:00-04:00",
        }]}, f)
    monkeypatch.setattr(cs, "user_file",
                         lambda u, fn: os.path.join(str(user_dir), fn))

    cs.record_trade_close(user, "AAPL", "breakout",
                            exit_price=105.0, pnl=50.0,
                            exit_reason="stop", qty=10, side="sell")

    # Verify settled_funds ledger has an entry
    ledger_path = os.path.join(str(user_dir), "settled_funds_ledger.json")
    assert os.path.exists(ledger_path), \
        "settled_funds ledger not created after a sell"
    with open(ledger_path) as f:
        ledger = json.load(f)
    assert len(ledger) == 1
    assert ledger[0]["symbol"] == "AAPL"
    assert ledger[0]["amount"] == 1050.0  # 105 × 10


def test_profit_ladder_has_pdt_guard(monkeypatch):
    """Pin: check_profit_ladder consults pdt_tracker before firing a
    same-day intraday sell. For margin-<$25k users near the 3-in-5
    limit this prevents tripping Alpaca's PDT flag."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "h" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    src = open(cloud_scheduler.__file__).read()
    # The profit-ladder path must import pdt_tracker and call
    # can_day_trade with a buffer
    assert "import pdt_tracker as _pdt" in src
    assert "_pdt.can_day_trade(_tier, buffer=1)" in src
    assert "_pdt.is_day_trade(" in src


def test_monitor_strategies_stashes_tier_cfg(monkeypatch):
    """Monitor detects tier once per tick and stashes on user dict so
    downstream exit paths can read it."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "h" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    src = open(cloud_scheduler.__file__).read()
    assert 'user["_tier_cfg"]' in src
    assert "_pc.apply_user_overrides" in src


def test_record_trade_close_buy_side_does_not_record_sale(tmp_path, monkeypatch):
    """Short-cover closes (side='buy') must NOT add to settled_funds
    ledger — only actual sales of long positions generate proceeds."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "h" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler as cs

    user_dir = tmp_path
    user = {"id": 1, "username": "test", "_data_dir": str(user_dir)}
    with open(os.path.join(str(user_dir), "trade_journal.json"), "w") as f:
        json.dump({"trades": [{
            "symbol": "TSLA", "strategy": "short_sell",
            "side": "sell_short", "price": 250, "qty": -1,
            "status": "open", "timestamp": "2026-04-22T10:00:00-04:00",
        }]}, f)
    monkeypatch.setattr(cs, "user_file",
                         lambda u, fn: os.path.join(str(user_dir), fn))

    cs.record_trade_close(user, "TSLA", "short_sell",
                            exit_price=240.0, pnl=10.0,
                            exit_reason="cover", qty=-1, side="buy")

    ledger_path = os.path.join(str(user_dir), "settled_funds_ledger.json")
    # Ledger should NOT exist (or be empty) — short cover doesn't generate
    # settled funds
    assert not os.path.exists(ledger_path), \
        "short cover incorrectly recorded in settled_funds ledger"
