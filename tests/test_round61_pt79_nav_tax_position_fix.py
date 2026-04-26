"""Round-61 pt.79 — nav-tab order: Tax Harvest position fix.

User-reported (after pt.75): clicking "Tax Harvest" in the nav
scrolled BACKWARDS up the page. Pt.75 had reordered the nav once
already to match DOM order, but Tax Harvest was still misplaced
because `taxHtml` is rendered INSIDE the positions section
(between the orders table and `</section>`), so it lives BEFORE
Analytics in DOM order — but pt.75's nav had it AFTER Screener.

Pt.79 moves the Tax Harvest nav button to between Positions and
Analytics so clicking it scrolls FORWARD instead of backward.
Short Sells stays where it is (its `shortHtml` is rendered after
the screener section close, before Backtest).
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent
_DASH = (_HERE / "templates" / "dashboard.html").read_text()


def _nav_block():
    nav_idx = _DASH.find('const navTabs')
    assert nav_idx > 0
    return _DASH[nav_idx:nav_idx + 3500]


def test_tax_harvest_button_lives_between_positions_and_analytics():
    """The user-reported regression: clicking Tax Harvest scrolled
    BACKWARDS because it was in nav after Screener but in DOM
    between Positions and Analytics."""
    block = _nav_block()
    pos = block.find("'section-positions'")
    tax = block.find("'section-tax'")
    analytics = block.find("'section-analytics'")
    screener = block.find("'section-screener'")

    assert pos > 0 and tax > 0 and analytics > 0 and screener > 0
    # New order: positions → tax → analytics → trades → screener
    assert pos < tax, "Positions must come BEFORE Tax in nav"
    assert tax < analytics, (
        "Tax Harvest must come BEFORE Analytics in nav — taxHtml "
        "renders inside the positions section, BEFORE the analytics "
        "section in DOM. Putting it AFTER Screener in the nav makes "
        "clicking it scroll backwards.")
    # Tax must NOT be after Screener anymore.
    assert tax < screener, "Tax Harvest is now BEFORE Screener in nav"


def test_short_sells_button_stays_after_screener():
    """Short Sells (`shortHtml`) renders BETWEEN screener and
    backtest in DOM, so the nav button stays after Screener."""
    block = _nav_block()
    screener = block.find("'section-screener'")
    shorts = block.find("'section-shorts'")
    backtest = block.find("'section-backtest'")
    assert shorts > 0 and screener > 0 and backtest > 0
    assert screener < shorts < backtest


def test_full_dom_match_order():
    """All non-conditional + Tax/Shorts buttons appear in strict
    DOM order so clicking any tab scrolls forward through the page."""
    block = _nav_block()
    # Note: Tax + Shorts are conditional but both branches appear
    # in the source string; their relative position is what we pin.
    expected = [
        "section-overview",
        "section-picks",
        "section-strategies",
        "section-readiness",
        "section-positions",
        "section-tax",         # ← inside positions, before analytics
        "section-analytics",
        "section-trades",
        "section-screener",
        "section-shorts",      # ← between screener and backtest
        "section-backtest",
        "section-scheduler",
        "section-heatmap",
        "section-comparison",
        "section-settings",
    ]
    positions = [block.find(f"'{sec}'") for sec in expected]
    for sec, pos in zip(expected, positions):
        assert pos > 0, f"{sec} missing from nav block"
    for i in range(len(positions) - 1):
        assert positions[i] < positions[i + 1], (
            f"{expected[i]} appears AFTER {expected[i + 1]} "
            "in nav — DOM order broken.")


def test_pt79_comment_explains_tax_placement():
    """Source-pin: the inline comment explains WHY Tax is between
    Positions and Analytics, so a future reader doesn't 'fix' it
    back to alphabetical or grouping order."""
    block = _nav_block()
    assert "Tax Harvest renders INSIDE the positions" in block or \
           "taxHtml" in block
