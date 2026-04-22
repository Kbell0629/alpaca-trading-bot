"""
Round-54 tests: per-key calibration overrides + hash-skip jitter fix.

User wanted control over every calibration knob with warnings/popups
for risky values, plus wanted the desktop jumping-on-refresh problem
actually solved (rounds 47 + 48 didn't eliminate it for all users).

Three features under test:

  1. POST /api/calibration/override — writes one key to guardrails.json
     after server-side validation. Blocks Alpaca-rule violations
     (e.g., short_enabled=True on cash account). Returns an error code
     that the UI surfaces as a popup.

  2. POST /api/calibration/reset — reverts tier-adopted keys back to
     calibrated defaults without touching user-customized risk keys.

  3. Hash-skip render: renderDashboard() only replaces DOM when the
     output HTML has changed since the last tick → zero repaint on
     quiet ticks → no jitter.
"""
from __future__ import annotations


# ========== Override endpoint contract ==========


def test_override_endpoint_is_registered():
    with open("server.py") as f:
        src = f.read()
    assert '/api/calibration/override' in src
    assert '/api/calibration/reset' in src


def test_override_endpoint_validates_key_whitelist():
    """Random keys must be rejected — only the explicit ALLOWED_KEYS
    list can be written via this endpoint. Prevents a malicious (or
    buggy) caller from writing arbitrary fields to guardrails.json."""
    with open("server.py") as f:
        src = f.read()
    assert 'ALLOWED_KEYS = {' in src
    for k in ("max_positions", "max_position_pct", "min_stock_price",
              "fractional_enabled", "wheel_enabled", "short_enabled",
              "strategies_enabled"):
        assert f'"{k}"' in src, f"override must allow {k}"


def test_override_endpoint_blocks_shorts_on_cash():
    """Alpaca rule: cash accounts can't short. If user tries
    short_enabled=True on a cash account, the endpoint must return
    blocked_by_alpaca_rule=True so the UI shows a hard-block popup."""
    with open("server.py") as f:
        src = f.read()
    assert "blocked_by_alpaca_rule" in src
    # The gate must check the detected tier's short_enabled
    assert 'tier.get("short_enabled", False)' in src


def test_override_endpoint_range_validates_max_position_pct():
    with open("server.py") as f:
        src = f.read()
    # Must reject values outside 0..0.5
    assert '"max_position_pct must be 0-0.50 (0-50%)"' in src


def test_override_endpoint_range_validates_max_positions():
    with open("server.py") as f:
        src = f.read()
    assert '"max_positions must be 1-50"' in src


def test_override_endpoint_logs_audit():
    """Every override should go into the admin audit log so operators
    can review what risk changes happened when."""
    with open("server.py") as f:
        src = f.read()
    # The override handler logs via auth.log_admin_action
    idx_override = src.find('path == "/api/calibration/override"')
    assert idx_override > 0
    assert 'log_admin_action' in src[idx_override:idx_override+6000]


# ========== Reset endpoint ==========


def test_reset_endpoint_preserves_risk_keys():
    """Reset revers TIER-adopted keys only — user's customized
    daily_loss_limit_pct, earnings_exit_*, kill_switch_* etc. must
    be UNTOUCHED. Grep-level pin that the reset loop uses the same
    key list as the migration (not a wholesale replace)."""
    with open("server.py") as f:
        src = f.read()
    # The reset loop touches only the tier-adopted keys
    idx_reset = src.find('path == "/api/calibration/reset"')
    assert idx_reset > 0
    reset_block = src[idx_reset:idx_reset+4000]
    # Must iterate the tier-adopted keys (same as round-51 migration)
    for k in ("max_positions", "max_position_pct", "min_stock_price",
              "fractional_enabled", "wheel_enabled", "short_enabled",
              "strategies_enabled"):
        assert f'"{k}"' in reset_block, f"reset loop missing {k}"
    # Must NOT WRITE to risk keys (appears in comment = OK; appears in
    # code like `guardrails["daily_loss_limit_pct"] = ...` = BAD).
    # Look for the assignment pattern specifically.
    assert 'guardrails["daily_loss_limit_pct"]' not in reset_block
    assert 'guardrails["earnings_exit_' not in reset_block
    assert 'guardrails["kill_switch' not in reset_block


# ========== Frontend UI pins ==========


def test_calibration_ui_has_sliders():
    """Pin the override UI controls — sliders for positions/pct/price,
    toggles for fractional/wheel/shorts."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert 'id="cal_maxPos"' in src
    assert 'id="cal_maxPct"' in src
    assert 'id="cal_minPx"' in src
    assert 'saveCalibrationOverride' in src
    assert 'toggleStrategy' in src
    assert 'resetCalibrationToDefaults' in src


def test_calibration_ui_has_risk_warnings():
    """Pin that the UI surfaces warnings before submitting risky
    overrides — user-facing confirm() dialogs for:
      * max_position_pct > 15%
      * max_positions > 12
      * short_enabled going OFF → ON
      * fractional going ON → OFF"""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "newValue > 0.15" in src
    assert "newValue > 12" in src
    assert "short_enabled" in src and "unlimited loss risk" in src


# ========== Hash-skip jitter fix ==========


def test_render_hash_skip_present():
    """Pin the hash-skip: renderDashboard only writes to app.innerHTML
    when the output string changed from the previous render. Eliminates
    jitter on quiet ticks (no new trades, unchanged data)."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    # The hash-skip uses a cached _lastAppHtml string on window
    assert "_lastAppHtml" in src
    assert "_appHtml !== window._lastAppHtml" in src
    # The string is built then compared — not directly assigned
    assert "var _appHtml =" in src


def test_render_still_runs_scroll_restore_when_html_changed():
    """Pin that sync scroll restore is still INSIDE the if-changed
    block so it only fires when DOM was actually touched. Scroll
    restore on unchanged renders would cause its own mini-jitter."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    # Rough structural check: scroll restore must be under the
    # _appHtml !== check
    idx_check = src.find("_appHtml !== window._lastAppHtml")
    assert idx_check > 0
    following = src[idx_check:idx_check+2000]
    assert "scrollTo(0, _preRenderScrollY)" in following
