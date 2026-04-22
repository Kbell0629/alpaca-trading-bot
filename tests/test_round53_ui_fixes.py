"""
Round-53 tests: nav-tab active-state preservation + desktop modal
sizing CSS variable.

User-reported bugs:
  1. "Click Positions → scrolls to positions → 10s later the button
     goes back to Overview highlighted." Cause: renderDashboard
     rewrites the nav bar HTML on every auto-refresh and hardcoded
     Overview as the active tab. Round-53 adds
     `window._activeNavSection` state preserved across renders.
  2. "Menu on desktop should size to the monitor — we fixed this
     before but it went back to small." Cause: the base `.modal`
     CSS had `width: min(420px, calc(100vw - 24px))` which ignored
     inline `max-width:680px`. Round-53 switches to a CSS custom
     property (--modal-w) so individual modals can widen on desktop.
"""
from __future__ import annotations


def test_scroll_to_section_stashes_active_nav():
    """Pin that scrollToSection() remembers which section the user
    navigated to via window._activeNavSection. Without this, the
    10-second auto-refresh resets the highlight to Overview."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    # scrollToSection must write to the global state
    assert "window._activeNavSection = id" in src, (
        "scrollToSection must stash the active section for the "
        "next renderDashboard() rebuild to pick up")


def test_nav_tabs_respect_active_section_on_render():
    """Pin that the nav bar builder reads window._activeNavSection
    instead of hardcoding 'section-overview' as always-active."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    # The variable must be declared + used in the nav HTML builder
    assert "var _activeNav = window._activeNavSection || 'section-overview'" in src, (
        "Nav tab builder must read from the stashed active section, "
        "not hardcode Overview")
    # The helper function must check section equality
    assert "sectionId === _activeNav" in src


def test_nav_tabs_no_longer_hardcode_overview_active():
    """Pin: the old hardcoded 'nav-tab active' button for Overview
    is gone. If someone regresses this, Overview will always win
    the highlight on auto-refresh."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    # Old pattern: '<button class="nav-tab active" onclick="scrollToSection(\'section-overview\')">Overview</button>'
    bad_pattern = "class=\"nav-tab active\" onclick=\"scrollToSection('section-overview')\""
    assert bad_pattern not in src, (
        "Hardcoded 'nav-tab active' for Overview is back — will "
        "always reset highlight on auto-refresh. Use the _navBtn() "
        "helper + window._activeNavSection state.")


# ========== Desktop modal sizing ==========


def test_base_modal_uses_css_variable_for_width():
    """Pin: .modal CSS must use --modal-w custom property so
    individual modals can widen on desktop. Without this, the base
    `width: min(420px, ...)` caps every modal at 420px regardless
    of inline max-width."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "width: min(var(--modal-w, 500px), calc(100vw - 24px))" in src, (
        "Base .modal CSS must use --modal-w custom property")


def test_settings_modal_sets_modal_w_to_680():
    """Settings modal should widen to 680px on desktop."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert '--modal-w:680px' in src, (
        "Settings modal must set --modal-w:680px to widen on desktop")


def test_explicit_width_modals_scale_correctly():
    """Every modal with an inline `max-width:XXXpx` style should
    either (a) also set --modal-w:XXXpx via custom property, or
    (b) set `width:min(XXXpx, calc(100vw - 24px))` inline directly.
    Just `max-width:` alone does NOT work — base class's width caps
    it at 500px. Scans every inline modal style and verifies the
    pattern."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    import re
    pattern = re.compile(r'<div class="modal" style="([^"]*)"', re.MULTILINE)
    for m in pattern.finditer(src):
        style = m.group(1)
        mw_match = re.search(r'max-width\s*:\s*(\d+px)', style)
        if not mw_match:
            continue
        width = mw_match.group(1)
        has_modal_w = f"--modal-w:{width}" in style or f"--modal-w: {width}" in style
        has_explicit_width = "width:min(" in style or "width: min(" in style
        assert has_modal_w or has_explicit_width, (
            f"Modal with max-width:{width} needs --modal-w or "
            f"inline width:min() — otherwise base 500px caps it.")
