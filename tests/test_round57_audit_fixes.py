"""
Round-57 tests: full tech-stack audit fixes.

Fixes covered:
  1. cloud_scheduler.py:1891 — stop-triggered last_loss_time RMW now locked
  2. cloud_scheduler.py:2860 — run_daily_close RMW now locked
  3. server.py /api/calibration/override RMW now locked
  4. server.py /api/calibration/reset RMW now locked
  5. cloud_scheduler.py — AH monitor raise failures surface to Sentry
  6. server.py /api/calibration/override now has 3s per-user rate limit
  7. daily-close email safe on portfolio_value=None / =0
  8. /api/data exposes extended_hours_trailing so the dashboard can
     render the ⚡ AH TRAILING indicator during pre/post-market.
"""
from __future__ import annotations

import json
import os
import sys as _sys


def _reload(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "f" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


# ========= Concurrency (guardrails RMW locks) =========

def test_stop_triggered_last_loss_time_writes_under_lock():
    """cloud_scheduler.py — stop-triggered branch must write guardrails
    inside `with strategy_file_lock(gpath):` so a concurrent handler
    POST can't race. Grep the code for the pattern."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    # Locate the stop_triggered branch near last_loss_time + verify
    # the lock is held when guardrails.json is written.
    idx = src.find('guardrails["last_loss_time"]')
    assert idx > 0, "stop-triggered branch moved"
    window = src[max(0, idx - 400):idx + 120]
    assert "strategy_file_lock(gpath)" in window, (
        "last_loss_time RMW must be inside strategy_file_lock — round-57")


def test_run_daily_close_writes_under_lock():
    """cloud_scheduler.py — run_daily_close's daily_starting_value +
    peak_portfolio_value RMW must run under strategy_file_lock."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    idx = src.find('guardrails["daily_starting_value"] = None')
    assert idx > 0, "run_daily_close RMW moved"
    window = src[max(0, idx - 300):idx + 300]
    assert "strategy_file_lock(gpath)" in window, (
        "run_daily_close RMW must be inside strategy_file_lock — round-57")


def test_calibration_override_writes_under_lock():
    """server.py — /api/calibration/override must hold
    strategy_file_lock across the guardrails RMW."""
    with open("server.py") as f:
        src = f.read()
    idx = src.find('"/api/calibration/override"')
    assert idx > 0, "endpoint moved"
    # End of the handler is at the next `if path ==` — bound the window
    # there so we don't accidentally find the lock in a sibling handler.
    next_path = src.find('if path == "/api/calibration/reset"', idx)
    assert next_path > idx
    window = src[idx:next_path]
    assert "strategy_file_lock" in window, (
        "calibration/override must use strategy_file_lock — round-57")


def test_calibration_reset_writes_under_lock():
    with open("server.py") as f:
        src = f.read()
    idx = src.find('"/api/calibration/reset"')
    assert idx > 0
    next_path = src.find('elif path ==', idx)
    # Fall back to a generous window if no next_path sentinel matches
    if next_path < 0:
        next_path = idx + 8000
    window = src[idx:next_path]
    assert "strategy_file_lock" in window, (
        "calibration/reset must use strategy_file_lock — round-57")


def test_calibration_override_fetches_account_outside_lock():
    """The /account network call must happen OUTSIDE the flock so we
    don't hold the lock during 100-500ms of network I/O."""
    with open("server.py") as f:
        src = f.read()
    idx = src.find('"/api/calibration/reset"')
    assert idx > 0
    window = src[idx:idx + 4000]
    # Find positions of detect_tier call and strategy_file_lock
    tier_pos = window.find("detect_tier(account)")
    lock_pos = window.find("strategy_file_lock")
    assert tier_pos > 0 and lock_pos > 0
    assert tier_pos < lock_pos, (
        "/account fetch + detect_tier must happen BEFORE the lock, "
        "not inside it — round-57")


# ========= Rate limiting =========

def test_calibration_override_rate_limit_dict_exists():
    """server.py must define the per-user rate-limit state dict."""
    with open("server.py") as f:
        src = f.read()
    assert "_CALIBRATION_OVERRIDE_LAST_WRITE" in src, (
        "rate-limit state dict missing — round-57 fix reverted")


def test_calibration_override_returns_429_when_rate_limited():
    """The handler must return HTTP 429 when the per-user cooldown is
    violated (3s)."""
    with open("server.py") as f:
        src = f.read()
    idx = src.find('"/api/calibration/override"')
    next_path = src.find('if path == "/api/calibration/reset"', idx)
    assert next_path > idx
    window = src[idx:next_path]
    assert "429" in window, "calibration/override must 429 on rate-limit"
    assert "rate_limited" in window, (
        "response must flag rate_limited:true for UI discrimination")


# ========= Sentry breadcrumb for AH raise failures =========

def test_ah_monitor_failed_stop_raise_captures_message():
    """When a trailing-stop raise fails (in either session), we must
    call observability.capture_message with tags so the operator can
    debug via Sentry instead of scrolling logs."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    idx = src.find('WARN stop raise failed')
    assert idx > 0, "stop-raise-failed log line moved"
    window = src[idx:idx + 1500]
    assert "capture_message" in window, (
        "failed trailing-stop raise must call observability.capture_message")
    assert "trailing_stop_raise_failed" in window, (
        "capture_message must include the trailing_stop_raise_failed "
        "event tag so Sentry dashboards can filter it")
    assert "session" in window and ("AH" in window or "market" in window), (
        "Sentry tag must include the session (AH vs market) so the "
        "operator can distinguish which window the failure happened in")


# ========= Daily-close email edge cases =========

def test_daily_close_handles_portfolio_value_none(monkeypatch):
    """account.portfolio_value = None must not crash the report."""
    cs = _reload(monkeypatch)
    monkeypatch.setattr(cs, "user_api_get", lambda u, p: [])
    account = {"portfolio_value": None, "last_equity": None,
               "cash": None, "buying_power": None}
    scorecard = {"current_value": 0}
    text = cs._build_daily_close_report(
        {"id": 1, "username": "x", "_data_dir": "/tmp"},
        account, scorecard, {}, daily_starting_value=None)
    assert "DAILY CLOSE SUMMARY" in text
    assert "Closing value" in text


def test_daily_close_handles_portfolio_value_zero(monkeypatch):
    """Zero portfolio must not divide-by-zero."""
    cs = _reload(monkeypatch)
    monkeypatch.setattr(cs, "user_api_get", lambda u, p: [])
    account = {"portfolio_value": 0, "last_equity": 0,
               "cash": 0, "buying_power": 0}
    scorecard = {"current_value": 0}
    text = cs._build_daily_close_report(
        {"id": 1, "username": "x", "_data_dir": "/tmp"},
        account, scorecard, {"peak_portfolio_value": 0},
        daily_starting_value=0)
    # Must not raise. The division guard protects day_pct + dd_pct.
    assert "Closing value" in text


# ========= /api/data exposes extended_hours_trailing =========

def test_api_data_exposes_extended_hours_trailing_flag():
    """server.py's /api/data handler must include the
    extended_hours_trailing field so the dashboard can render the
    ⚡ AH TRAILING indicator."""
    with open("server.py") as f:
        src = f.read()
    idx = src.find('data["session_mode"]')
    assert idx > 0
    window = src[idx:idx + 1500]
    assert "extended_hours_trailing" in window, (
        "/api/data must expose extended_hours_trailing for the "
        "dashboard AH indicator — round-57")


def test_api_data_extended_hours_trailing_defaults_true_if_missing():
    """If guardrails.json lacks the key entirely, the field must come
    back as True (default ON). Verify the logic by reading the source
    — unit-testing the full HTTP handler needs DB boot."""
    with open("server.py") as f:
        src = f.read()
    # Find the /api/data block where the flag is exposed
    data_idx = src.find('data["session_mode"]')
    assert data_idx > 0
    window = src[data_idx:data_idx + 2500]
    # The exposure code should have the True default both for the
    # file-exists branch (via .get(key, True)) and the file-missing
    # branch (explicit True assignment).
    assert "get(\"extended_hours_trailing\", True)" in window, (
        "extended_hours_trailing default must be True when key absent")
    # And the else / exception branches must also default True so a
    # missing file or read error doesn't silently render 'AH OFF' chip
    assert 'data["extended_hours_trailing"] = True' in window, (
        "missing-file / read-error path must also default the flag to True")


# ========= Dashboard JS surfaces the indicator =========

def test_dashboard_ah_trailing_chip_present_in_html():
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "AH TRAILING" in src, (
        "dashboard must render the ⚡ AH TRAILING chip during pre/post-market")
    # The chip must be conditional on session + opt-out
    assert "extended_hours_trailing" in src, (
        "chip render condition must check extended_hours_trailing flag")


# ========= Slider touch-target CSS =========

def test_enrichment_panels_use_hash_skip():
    """Round-57: all panels that re-render every 10s tick must hash-skip
    identical output. Before this fix, each render wrote innerHTML
    wholesale even when the HTML was unchanged, causing scroll-jitter
    when the user was scrolled into that section. Now every final
    render path caches `el._lastHtml` and skips the DOM write on
    identical output. Same pattern as renderDashboard's
    `window._lastAppHtml` from round-54.

    Covered: renderHeatmap, refreshPerfAttribution, refreshTaxReport,
    refreshFactorHealth, refreshSchedulerStatus, renderLog."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    occurrences = src.count("_lastHtml !==")
    assert occurrences >= 6, (
        f"expected >=6 _lastHtml hash-skip guards on enrichment panels, "
        f"found {occurrences} — round-57 jitter fix")


def test_nav_tabs_wrap_on_desktop():
    """Round-57: nav-tabs wrap onto multiple rows at desktop widths
    (≥1024px) instead of forcing horizontal scroll when there's plenty
    of room. User flagged the scroll-bar on a wide monitor. Mobile
    (<1024px) still uses overflow-x: auto."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    # Look for the desktop media query that unlocks flex-wrap
    assert "@media (min-width: 1024px)" in src
    # And that it does so on .nav-tabs specifically
    idx = src.find("@media (min-width: 1024px)")
    window = src[idx:idx + 500]
    assert ".nav-tabs" in window
    assert "flex-wrap: wrap" in window
    assert "overflow-x: visible" in window
    # Hide the scroll chevron when no scroll is needed
    assert "display: none" in window


def test_range_slider_has_touch_height():
    """input[type='range'] CSS must set height >= 32px so iOS users can
    actually drag the thumb. Browser default is ~14-18px which is
    below the Apple HIG 44pt touch target."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    idx = src.find('input[type="range"]')
    assert idx > 0, "range slider CSS block missing"
    window = src[idx:idx + 2000]
    # Touch-target height on the container
    assert "height: 32px" in window, (
        "range slider container must be >=32px tall for iOS touch")
    # Visible track + thumb styling for WCAG contrast
    assert "accent-color: var(--blue)" in window
    assert "::-webkit-slider-thumb" in window
