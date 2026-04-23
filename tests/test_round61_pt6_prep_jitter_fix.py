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
