"""
Round-61 pt.6 prep: pin the dollar/percent normalization in the
renderDashboard _normHash regex.

Background: R60 introduced `_lastAppNormHash` to skip full innerHTML
replacement on quiet ticks (timestamp-only changes). But during market
hours, price ticks change dollar amounts ($192.40 → $192.41) and
percentages (+1.7% → +1.8%) in position/account cells every few seconds.
Those diffs made normHash mismatch → full #app rewrite every 10s →
visible scroll jitter ("the screen is still jumping around when it
refreshes" — user report 2026-04-24).

The fix extends the normalization regex to also strip dollar amounts
and percentages from the hash input. Price-only ticks then flow
through the quiet-tick branch (no innerHTML swap = stable scroll).

These assertions catch any regression that removes or weakens the
stripping, which would bring the jitter back.
"""
from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DASH = os.path.join(_HERE, "templates", "dashboard.html")


def _src():
    with open(_DASH) as f:
        return f.read()


def test_normhash_strips_dollar_amounts():
    """Pattern must cover plain $123.45 and signed +/-$123.45 cases
    so position-table price/value cells don't break the hash."""
    src = _src()
    # The dollar-strip regex line. Accepts single or double quotes,
    # leading characters (class chars, quantifier, optional leading sign)
    # may vary — we just need to ensure SOMETHING strips dollar amounts.
    assert r"\$-?[\d,]+\.\d{2}" in src, (
        "renderDashboard _normHash regex must strip dollar amounts — "
        "price-tick cells leak through otherwise and cause scroll jitter")


def test_normhash_strips_percentages():
    """Percentage strip must cover both decimal (+/-0.00%) and
    integer (+/-10%) forms. Day-change cells use decimals, readiness
    cards use integers."""
    src = _src()
    assert r"\d+\.\d+%" in src, (
        "renderDashboard _normHash regex must strip decimal percentages")
    assert r"\d+%" in src, (
        "renderDashboard _normHash regex must strip integer percentages")


def test_normhash_still_strips_freshness_chips():
    """R60 freshness-chip stripping must coexist with the pt.6 dollar/
    percent stripping — both are needed for the full quiet-tick path."""
    src = _src()
    assert ">_ ago<" in src, (
        "R60 freshness-chip normalization regressed")
    assert "Updated <" in src, (
        "R60 'Updated' timestamp normalization regressed")


def test_normhash_applied_inside_renderDashboard():
    """The pattern must live inside renderDashboard — regexes applied
    elsewhere don't help the jitter."""
    src = _src()
    rd_start = src.find("function renderDashboard")
    assert rd_start > 0
    rd_block = src[rd_start:rd_start + 120_000]  # generous window
    # All three normalization components must be inside the function
    assert "_normHash" in rd_block
    assert r"\$-?[\d,]+\.\d{2}" in rd_block
    assert "_normHash !== window._lastAppNormHash" in rd_block


# Round-61 pt.6 prep — second jitter pass: the user reported the
# screen STILL jumped after the renderDashboard normHash strip
# (#107 merged but jitter persisted). Root cause: refreshFactorHealth
# ran every 10s with a freshness chip ("Xs ago") embedded in its
# HTML, so the panel-level _lastHtml check mismatched every tick →
# panel rewrite → outer layout reflow → scroll jump (especially
# visible from the Recent Activity panel above it). Plus, even
# panels that DO have stable HTML can still cause sibling layout
# shifts unless the browser is told their internals are contained.

def test_factor_health_uses_normalized_hash():
    """refreshFactorHealth's hash-skip must strip the freshness chip
    so the auto-refresh tick doesn't repaint the panel every 10s."""
    src = _src()
    # Hint string from the new hash-skip block
    assert 'panel._lastNormHtml' in src, (
        "refreshFactorHealth must use a normalized hash (panel._lastNormHtml) "
        "so the freshness chip's 'Xs ago' text doesn't trigger a rewrite "
        "every 10s")
    assert '<div class="factor-card-age">#</div>' in src, (
        "factor-card-age (freshness chip) must be replaced with a stable "
        "placeholder in the hash so chip-only ticks skip the rewrite")


# Round-61 pt.6 prep #3 — user reported AFTER #107 and #108 shipped:
# "still refreshes it scrolls somewhere and then goes right back to
#  where I was before it refreshed"
# Diagnosis: the scroll-save-restore pattern was working, but the
# intermediate frame where #app.innerHTML had just been assigned
# (destroying all children → document height collapses → browser
# clamps scrollY) was painting before the sync scrollTo restore
# landed. Fix: use atomic children swap via <template> +
# replaceChildren so #app never goes empty, PLUS set min-height:
# 100vh on #app so the document height can never collapse below
# viewport even momentarily.

def test_render_dashboard_uses_atomic_replacechildren():
    """renderDashboard must swap #app's contents atomically via
    replaceChildren (or fallback) so there's no frame where #app is
    empty and document height collapses."""
    src = _src()
    rd_start = src.find("function renderDashboard")
    assert rd_start > 0
    rd_block = src[rd_start:rd_start + 150_000]
    # The atomic swap path
    assert "_appEl.replaceChildren" in rd_block, (
        "renderDashboard must use replaceChildren for the content swap "
        "(prevents the intermediate-empty-frame scroll flicker)")
    # Must build children in a <template>, not assign innerHTML directly
    assert 'document.createElement("template")' in rd_block or \
           "document.createElement('template')" in rd_block, (
        "The swap must stage HTML in a template element first")


def test_app_has_min_viewport_height():
    """#app must declare min-height: 100vh so the document body
    can't collapse below the viewport during a content swap."""
    src = _src()
    # Find the #app rule
    idx = src.find("#app {")
    assert idx > 0, "missing #app CSS rule"
    block = src[idx:idx + 400]
    assert "min-height: 100vh" in block, (
        "#app must declare min-height: 100vh — without this, the "
        "document height can collapse during the content swap and "
        "the browser will clamp scrollY, causing the user-reported "
        "scroll-jump-then-restore flicker")
    assert "overflow-anchor: auto" in block, (
        "#app must declare overflow-anchor: auto to give the browser's "
        "native scroll anchoring a stable container to work from")


def test_section_layout_containment_present():
    """Every section that auto-refreshes every 10s must declare
    `contain: layout style` so internal repaints can't reflow
    sibling sections — even a panel that DOES rewrite cannot then
    shift the user's scroll position. Browser-native containment is
    cheap and the right tool here."""
    src = _src()
    # Each refreshing section's CSS rule must include the contain.
    # We assert the keyword is paired with each of the four section
    # classes that fire on the 10s tick.
    for cls in (".scheduler-section", ".factor-health-section",
                ".comparison-section", ".heatmap-section",
                ".activity-log"):
        idx = src.find(cls + " {")
        if idx < 0:
            idx = src.find(cls + " {", 0)
        if idx < 0:
            # CSS may use single-line `.x {`. Accept inline form too.
            idx = src.find(cls + " ")
        assert idx > 0, f"missing CSS rule for {cls}"
        block = src[idx:idx + 800]
        assert "contain: layout style" in block, (
            f"{cls} must declare contain: layout style (jitter prevention)")
        assert "overflow-anchor: auto" in block, (
            f"{cls} must declare overflow-anchor: auto (jitter prevention)")
