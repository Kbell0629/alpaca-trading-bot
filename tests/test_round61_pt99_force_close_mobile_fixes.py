"""Round-61 pt.99 — Force Daily Close button + mobile dropdown / nav fixes.

Two user-reported issues from a mobile screenshot of the audit modal:

1. The audit's STALE_SCORECARD finding says "Trigger via Settings →
   Force Daily Close." The endpoint ``/api/force-daily-close`` exists
   in server.py and ``handle_force_daily_close`` exists in
   ``actions_mixin.py``, but no button in the dashboard called it.
   Pt.99 adds the button to the audit modal next to "Re-run Audit"
   and "🧹 Clean Up Ghosts".

2. Mobile formatting: the user-menu dropdown (Show/Hide Sections,
   Users) was anchored ``right: 0`` which on narrow viewports made
   it extend off the LEFT edge of the screen because the user-menu
   button itself wraps to the LEFT side of the row. Pt.99 flips to
   ``left: 0`` on <600px so the dropdown extends right of the
   button instead.

3. Mobile nav-tabs: the active tab pill (e.g. "Positions") was
   getting partially obscured by the ``.nav-tabs-wrap::after``
   chevron / gradient on narrow screens. Pt.99 calls
   ``scrollIntoView({inline: 'center'})`` after marking active so
   the active tab is always fully visible.
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent
_DASH = (_HERE / "templates" / "dashboard.html").read_text()
_AUDIT_CORE = (_HERE / "audit_core.py").read_text()


# ============================================================================
# Force Daily Close button
# ============================================================================

def test_audit_modal_has_force_daily_close_button():
    """Pt.99: the audit modal text in audit_core.py tells the user
    to "Trigger via Settings → Force Daily Close" — that button
    must actually exist in the audit modal so the instruction is
    actionable."""
    idx = _DASH.find('id="auditModal"')
    assert idx > 0
    end = _DASH.find('</div>\n</div>', idx)
    modal = _DASH[idx:end + 100]
    assert "Force Daily Close" in modal
    assert "onclick=\"forceDailyClose()" in modal


def test_force_daily_close_helper_defined():
    assert "async function forceDailyClose" in _DASH
    assert "/api/force-daily-close" in _DASH


def test_force_daily_close_uses_csrf_token():
    """All state-changing POSTs must carry the CSRF token."""
    idx = _DASH.find("async function forceDailyClose")
    assert idx > 0
    fn_block = _DASH[idx:idx + 1500]
    assert "X-CSRF-Token" in fn_block
    assert "getCSRFToken()" in fn_block


def test_force_daily_close_reruns_audit_on_success():
    """After success the audit modal should refresh so the user
    sees STALE_SCORECARD clear without manually clicking Re-run."""
    idx = _DASH.find("async function forceDailyClose")
    fn_block = _DASH[idx:idx + 1500]
    assert "runStateAudit" in fn_block


def test_audit_message_still_references_force_daily_close():
    """If the audit message text changes wording, this test pings
    the source so pt.99's button label can stay in sync."""
    assert "Force Daily Close" in _AUDIT_CORE


# ============================================================================
# User-dropdown mobile overflow fix
# ============================================================================

def test_user_dropdown_uses_left_anchor_on_mobile():
    """Pt.99: on <600px the dropdown must anchor to the LEFT of the
    user-menu button so it doesn't extend off-screen to the left."""
    idx = _DASH.find(".user-dropdown {")
    assert idx > 0
    # Look for the @media block targeting the dropdown.
    media_block = _DASH[idx:idx + 1500]
    assert "@media (max-width: 600px)" in media_block
    assert ".user-dropdown { right: auto; left: 0;" in media_block \
        or "right:auto;left:0" in media_block.replace(" ", "")


# ============================================================================
# Nav-tabs scroll-into-view fix
# ============================================================================

def test_scroll_to_section_centers_active_tab():
    """Pt.99: scrollToSection must call scrollIntoView on the active
    tab so it's not hidden under the chevron gradient on mobile."""
    idx = _DASH.find("function scrollToSection")
    assert idx > 0
    fn_block = _DASH[idx:idx + 2500]
    assert "activeTab" in fn_block
    assert "scrollIntoView" in fn_block
    # Must use inline: 'center' (not the default 'nearest') so the
    # active tab is centered horizontally in the nav-tabs scroller.
    assert "inline: 'center'" in fn_block or "inline:'center'" in fn_block


def test_scroll_to_section_marks_active_tab_first():
    """The class change must happen BEFORE the actual scrollIntoView
    call so the browser scrolls to the correct element. (We match
    the call signature, not the bare word — the function header
    comment also mentions ``scrollIntoView`` historically.)"""
    idx = _DASH.find("function scrollToSection")
    fn_block = _DASH[idx:idx + 2500]
    add_idx = fn_block.find("classList.add('active')")
    scroll_idx = fn_block.find("activeTab.scrollIntoView")
    assert add_idx > 0 and scroll_idx > 0
    assert add_idx < scroll_idx
