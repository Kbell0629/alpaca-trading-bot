"""Round-61 pt.20 — strategy-badge gaps + orphan-notification spam.

User-reported after pt.19 deploy:
1. SOXL shows AUTO but no strategy badge (expected SHORT SELL pill).
   Root cause: dashboard JS `stratLabelMap` used key `'short'` but
   backend writes `'short_sell'` (from filename
   `short_sell_SOXL.json`).
2. Every 10-min orphan-adoption tick fires a warning-level push
   notification "Error recovery found N orphan positions: ..." —
   spammy when the same symbols keep appearing (user has a
   permanently-unadopted position because of the OCC-multi-contract
   limitation from pt.17).

Fixes:
1. `stratLabelMap` + `stratColorMap`: add `short_sell`, plus
   `wheel_strategy` / `wheel_auto_deploy` aliases for any drift
   between filename / journal / deployer naming.
2. `error_recovery.py`: switch notification type from `"alert"` to
   `"info"` (adoption is routine, not an emergency) and dedup
   against the previous run's symbol set so the same orphan list
   doesn't spam notifications every 10 minutes.
"""
from __future__ import annotations

import json
import os


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# Fix 1: strategy-label map covers all backend-produced keys
# ----------------------------------------------------------------------------

def test_dashboard_strat_label_map_includes_short_sell():
    """Backend writes `_strategy='short_sell'` for positions managed
    by `short_sell_<SYM>.json`. The map MUST have this key or the
    pill badge silently won't render."""
    src = _src("templates/dashboard.html")
    # The key must appear in the stratLabelMap definition.
    assert "'short_sell': 'Short Sell'" in src, (
        "stratLabelMap must map 'short_sell' (the backend's actual "
        "key) to a human label. Prior bug was the key 'short' which "
        "the backend never emits.")


def test_dashboard_strat_color_map_includes_short_sell():
    src = _src("templates/dashboard.html")
    assert "'short_sell': '#10b981'" in src


def test_dashboard_strat_label_map_includes_wheel_aliases():
    """`_strategy` can be `wheel` (from filename), `wheel_strategy`
    (legacy journal), or `wheel_auto_deploy` (newer journal). All
    three should resolve to the WHEEL badge."""
    src = _src("templates/dashboard.html")
    for alias in ("'wheel': 'Wheel'",
                  "'wheel_strategy': 'Wheel'",
                  "'wheel_auto_deploy': 'Wheel'"):
        assert alias in src, f"Missing alias in stratLabelMap: {alias}"


def test_dashboard_strat_label_map_still_maps_short_for_backward_compat():
    """Don't break any older rendered payload that still uses the
    bare 'short' key (some dev/test fixtures use it)."""
    src = _src("templates/dashboard.html")
    assert "'short': 'Short Sell'" in src


# ----------------------------------------------------------------------------
# Fix 2: orphan notification is info-level + deduped
# ----------------------------------------------------------------------------

def test_orphan_notification_uses_info_severity():
    """'Alert' severity triggers the warning-chrome push every run,
    which is spammy when adoption is the normal healthy state."""
    src = _src("error_recovery.py")
    # Make sure the old `"alert"` severity isn't the chosen one for
    # the orphan summary.
    idx = src.find("Send notification for orphans found")
    assert idx > 0
    block = src[idx:idx + 2000]
    assert '"--type", "info"' in block, (
        "Orphan-summary notification must be info-level, not alert.")


def test_orphan_notification_dedups_against_previous_run():
    """Same orphan list two runs in a row = skip the push. Forces a
    re-notification only when the list CHANGES."""
    src = _src("error_recovery.py")
    assert ".orphan_notif_last.json" in src, (
        "Dedup state must persist across runs via a file — in-memory "
        "state is lost because error_recovery.py is invoked as a "
        "subprocess per user.")
    assert "if dedup_key != last_key" in src, (
        "Dedup decision must compare sorted-symbols-this-run against "
        "the persisted set.")


def test_orphan_dedup_behavioral_same_set_suppresses(tmp_path, monkeypatch):
    """Drive error_recovery.main() twice with the same orphan list.
    Second run must NOT fire a notification subprocess."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    monkeypatch.setattr(er, "DATA_DIR", str(tmp_path))

    positions = [{
        "symbol": "UNADOPTABLE", "qty": "100",
        "avg_entry_price": "50.00", "current_price": "51.00",
    }]
    orders = []

    def _fake_get(url, timeout=15, max_retries=3):
        if "positions" in url:
            return positions
        if "orders" in url:
            return orders
        return []
    monkeypatch.setattr(er, "api_get_with_retry", _fake_get)
    monkeypatch.setattr(er, "api_post", lambda *a, **kw: {"id": "fake"})

    popens = []
    class _FakePopen:
        def __init__(self, *a, **kw):
            popens.append(a[0] if a else kw.get("args"))
    monkeypatch.setattr(er.subprocess, "Popen", _FakePopen)

    er.main()
    first_run_notifications = len(popens)
    assert first_run_notifications >= 1, "First run should notify"

    # Ensure the strategy file was created so it won't be flagged as
    # orphan next run (via round-61 pt.16 filter).
    # Actually, the test symbol UNADOPTABLE has no OCC / qty sign
    # that routes to a wheel — it takes the equity long path, so a
    # trailing_stop_UNADOPTABLE.json file gets written and removed
    # from orphan list. Simulate a persistently-orphaned symbol
    # instead by deleting the created file between runs.
    for f in sdir.iterdir():
        f.unlink()

    popens.clear()
    er.main()
    # Same orphan list as before → no new notification.
    notification_subprocs = [p for p in popens
                             if p and "notify.py" in (p[1] if len(p) > 1 else "")]
    assert not notification_subprocs, (
        f"Second run with same orphan set should NOT re-fire the "
        f"push notification. Got subprocesses: {popens}")


def test_orphan_dedup_fires_when_set_changes(tmp_path, monkeypatch):
    """Different orphan list must re-notify (dedup is per-set, not
    per-ever-notified)."""
    import error_recovery as er
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    monkeypatch.setattr(er, "STRATEGIES_DIR", str(sdir))
    monkeypatch.setattr(er, "DATA_DIR", str(tmp_path))

    # Seed the dedup file with a different prior set.
    with open(os.path.join(str(tmp_path), ".orphan_notif_last.json"), "w") as f:
        json.dump({"symbols": ["PRIOR_UNADOPTABLE"],
                   "last_notified": "2026-04-24T00:00:00-04:00"}, f)

    positions = [{
        "symbol": "NEWORPHAN", "qty": "100",
        "avg_entry_price": "50.00", "current_price": "51.00",
    }]

    def _fake_get(url, timeout=15, max_retries=3):
        if "positions" in url:
            return positions
        if "orders" in url:
            return []
        return []
    monkeypatch.setattr(er, "api_get_with_retry", _fake_get)
    monkeypatch.setattr(er, "api_post", lambda *a, **kw: {"id": "fake"})

    popens = []
    class _FakePopen:
        def __init__(self, *a, **kw):
            popens.append(a[0] if a else kw.get("args"))
    monkeypatch.setattr(er.subprocess, "Popen", _FakePopen)

    er.main()
    notification_subprocs = [p for p in popens
                             if p and "notify.py" in (p[1] if len(p) > 1 else "")]
    assert notification_subprocs, (
        "Different orphan set vs. last run → MUST re-notify.")
    # And the persisted state should now reflect the new set.
    with open(os.path.join(str(tmp_path), ".orphan_notif_last.json")) as f:
        saved = json.load(f)
    assert saved["symbols"] == ["NEWORPHAN"]
