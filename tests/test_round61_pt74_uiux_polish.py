"""Round-61 pt.74 — UI/UX "Pro" polish.

Source-pin tests for the 10-item polish that landed in pt.74:
  Item 1: panel-tertiary tag for advanced sections
  Item 2: Focus Mode toggle (CSS class + JS handler + pill markup)
  Item 3: prefers-reduced-motion + soft-pulse class
  Item 4: header cluster grouping (CSS only, no DOM restructure)
  Item 5: skeleton loaders + last-updated freshness chips
  Item 6: auth pages — trust copy + password visibility toggle
  Item 7: risk badge at action sites (kill switch + close)
  Item 8: typography ramp consistent across auth templates
  Item 9: sticky table headers via .pt74-sticky-table
  Item 10: high-stakes copy pass (Close Position → "Close position")

The unit tests on the JS helpers themselves live in
``tests/js/pt74-uiux.test.js``.
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent
_DASH = (_HERE / "templates" / "dashboard.html").read_text()
_LOGIN = (_HERE / "templates" / "login.html").read_text()
_SIGNUP = (_HERE / "templates" / "signup.html").read_text()


# ============================================================================
# Item 3 — reduced motion + soft-pulse
# ============================================================================

def test_dashboard_has_prefers_reduced_motion_block():
    assert "@media (prefers-reduced-motion: reduce)" in _DASH


def test_dashboard_has_pt74_polish_block_marker():
    assert "Round-61 pt.74" in _DASH
    assert 'pt74-soft-pulse' in _DASH


def test_login_respects_prefers_reduced_motion():
    assert "prefers-reduced-motion" in _LOGIN


def test_signup_respects_prefers_reduced_motion():
    assert "prefers-reduced-motion" in _SIGNUP


# ============================================================================
# Item 2 — Focus Mode
# ============================================================================

def test_focus_mode_pill_in_header():
    assert 'id="focusModePill"' in _DASH
    assert 'onclick="toggleFocusMode()"' in _DASH
    assert 'aria-pressed="false"' in _DASH


def test_focus_mode_css_hides_decorative():
    # Multiple selectors should be hidden under body.focus-mode.
    assert "body.focus-mode" in _DASH
    assert "body.focus-mode .panel-tertiary" in _DASH


def test_toggle_focus_mode_persists_to_localStorage():
    assert "pt74_focusMode" in _DASH
    assert "localStorage.setItem('pt74_focusMode'" in _DASH


def test_focus_mode_restored_on_load():
    assert "applyFocusModeFromStorage" in _DASH


# ============================================================================
# Item 5 — skeleton + freshness chip
# ============================================================================

def test_skeleton_helper_defined():
    assert "function pt74RenderSkeleton" in _DASH
    assert "pt74-skeleton-grid" in _DASH
    assert "pt74-skeleton-card" in _DASH
    assert "pt74-skeleton-line" in _DASH


def test_freshness_chip_helper_defined():
    assert "function pt74FormatFreshness" in _DASH
    assert "function pt74RenderFreshnessChip" in _DASH
    assert "pt74-fresh-chip" in _DASH


def test_analytics_panel_uses_skeleton_loader():
    """The analytics panel's initial loading-state HTML now renders
    the skeleton instead of plain "Loading..." text."""
    idx = _DASH.find('id="analyticsPanel"')
    assert idx > 0
    block = _DASH[idx:idx + 600]
    assert "pt74RenderSkeleton" in block


def test_trades_panel_uses_skeleton_loader():
    idx = _DASH.find('id="tradesPanel"')
    assert idx > 0
    block = _DASH[idx:idx + 600]
    assert "pt74RenderSkeleton" in block


def test_analytics_fetch_records_freshness():
    assert "_analyticsLastFetchedAt" in _DASH
    assert "_analyticsLastFetchError" in _DASH


def test_trades_fetch_records_freshness():
    assert "_tradesLastFetchedAt" in _DASH
    assert "_tradesLastFetchError" in _DASH


# ============================================================================
# Item 6 — auth UX polish
# ============================================================================

def test_login_has_brand_lockup():
    assert "brand-lockup" in _LOGIN
    assert "brand-mark" in _LOGIN


def test_login_has_trust_copy():
    assert "pt74-trust-row" in _LOGIN
    assert "Paper-trading by default" in _LOGIN
    assert "encrypted at rest" in _LOGIN.lower()


def test_login_has_password_visibility_toggle():
    assert "pt74-pw-wrapper" in _LOGIN
    assert "pt74-pw-toggle" in _LOGIN
    assert 'aria-label="Show password"' in _LOGIN


def test_signup_has_brand_lockup():
    assert "brand-lockup" in _SIGNUP


def test_signup_has_trust_copy():
    assert "pt74-trust-row" in _SIGNUP
    assert "Paper-trading by default" in _SIGNUP


def test_signup_password_field_has_toggle():
    """Both the user password and the alpaca_secret have the toggle."""
    # Password input is wrapped in pt74-pw-wrapper at least 2x:
    # one for `password`, one for `alpaca_secret`.
    assert _SIGNUP.count("pt74-pw-wrapper") >= 2


def test_signup_username_pattern_unchanged():
    """Sanity: existing validation (3-30 chars, [A-Za-z0-9_-]) is intact."""
    assert 'minlength="3"' in _SIGNUP
    assert 'maxlength="30"' in _SIGNUP


# ============================================================================
# Item 7 — risk badge at action sites
# ============================================================================

def test_risk_badge_helper_defined():
    assert "function pt74RenderRiskBadge" in _DASH
    assert "pt74-risk-badge" in _DASH


def test_kill_switch_modal_renders_risk_badge():
    idx = _DASH.find('id="killSwitchModal"')
    assert idx > 0
    block = _DASH[idx:idx + 1500]
    assert 'id="killSwitchRiskBadge"' in block


def test_open_kill_switch_modal_populates_badge():
    idx = _DASH.find("function openKillSwitchModal")
    assert idx > 0
    block = _DASH[idx:idx + 800]
    assert "pt74RenderRiskBadge" in block
    assert "killSwitchRiskBadge" in block


def test_close_position_modal_renders_risk_badge():
    """The close-position modal also surfaces the paper/live badge."""
    idx = _DASH.find('id="closeModal"')
    assert idx > 0
    block = _DASH[idx:idx + 2000]
    assert 'id="closeRiskBadge"' in block


# ============================================================================
# Item 9 — sticky table headers
# ============================================================================

def test_sticky_table_class_defined():
    assert ".pt74-sticky-table thead" in _DASH


def test_positions_table_uses_sticky_class():
    # The positions table render uses the new class.
    assert "pt74-sticky-table" in _DASH


def test_orders_table_uses_sticky_class():
    """Two distinct tables use the sticky helper (positions + orders)."""
    assert _DASH.count("pt74-sticky-table") >= 2


# ============================================================================
# Item 4 — header cluster grouping
# ============================================================================

def test_header_actions_visual_clusters_defined():
    """CSS treats the focus pill as the first cluster anchor with a
    border-right separator, and the help/refresh pair gets a
    border-left to mark utilities."""
    assert ".header-actions > .focus-mode-pill" in _DASH
    assert ".header-actions > .help-btn-v2" in _DASH
    assert "border-right: 1px solid rgba(255,255,255,0.10)" in _DASH


# ============================================================================
# Item 1 — info hierarchy / collapse advanced
# ============================================================================

def test_panel_tertiary_class_used():
    assert ".panel-tertiary" in _DASH
    # At least one section is tagged.
    assert "panel-tertiary" in _DASH


def test_focus_mode_hides_advanced_sections():
    assert "body.focus-mode #section-scheduler" in _DASH
    assert "body.focus-mode #section-perf-attribution" in _DASH


def test_panel_tertiary_h3_advertises_advanced():
    """The CSS adds a "· advanced" hint after tertiary section
    headers via :after pseudo-element."""
    assert ".panel-tertiary > h3::after" in _DASH


# ============================================================================
# Item 10 — copy pass
# ============================================================================

def test_kill_switch_modal_copy_softened():
    """`EMERGENCY KILL SWITCH` (yelling) → `Emergency kill switch` +
    title-case cleanup elsewhere."""
    idx = _DASH.find('id="killSwitchModal"')
    block = _DASH[idx:idx + 2500]
    assert "Emergency kill switch" in block
    # The lower-case version of cancel/sell bullets is unchanged.
    assert "Activate kill switch" in block


def test_close_modal_copy_clarifies_action():
    """The default subtitle now describes WHAT a close does."""
    idx = _DASH.find('id="closeModalTitle"')
    block = _DASH[idx:idx + 600]
    assert "next available market price" in block


# ============================================================================
# Item 8 — typography ramp consistent across auth templates
# ============================================================================

def test_login_typography_ramp():
    """Login uses the SF Pro / -apple-system stack and the upgraded
    label / input typography that pt.74 shipped."""
    assert "'SF Pro Display','Segoe UI'" in _LOGIN
    assert 'letter-spacing:0.6px' in _LOGIN


def test_signup_typography_ramp():
    assert "'SF Pro Display','Segoe UI'" in _SIGNUP
    assert "letter-spacing:0.6px" in _SIGNUP


# ============================================================================
# JS load smoke check
# ============================================================================

def test_dashboard_js_window_exports_pt74_helpers():
    """The pt74 helpers are attached to `window` so external pages
    (e.g. embedded dashboards) and tests can reach them."""
    assert "window.pt74RenderSkeleton" in _DASH
    assert "window.pt74FormatFreshness" in _DASH
    assert "window.pt74RenderRiskBadge" in _DASH
    assert "window.toggleFocusMode" in _DASH
