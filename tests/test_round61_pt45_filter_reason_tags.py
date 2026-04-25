"""Round-61 pt.45 — surface "why filtered" reasons on screener picks.

User asked why high-scored picks (POET 458, AMD 172, INTC 155) didn't
appear in the Top 3 even though MRVL/TXN/NVDA scored lower. Answer:
they were filtered by deploy-time gates (don't-chase, already-held,
sector-cap), but the screener table didn't surface the WHY.

Pt.45 bridges all filter reasons into the unified `filter_reasons`
list and renders them as chips in the dashboard's screener table.

Sources of filter reasons:
  * chase_block (round-20)        — daily_change > 8% on Breakout/PEAD
  * volatility_block (round-20)   — volatility > 20% on Breakout/PEAD
  * already_held (NEW)            — symbol in user's open positions
  * below_50ma / above_50ma (NEW) — pt.39 trend filter bridge
  * breakout_unconfirmed (NEW)    — pt.40 single-day breakout bridge
"""
from __future__ import annotations

import pathlib

_HERE = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (_HERE / name).read_text()


# ============================================================================
# update_dashboard.py source pins
# ============================================================================

def test_already_held_filter_added_to_filter_reasons():
    """When a pick's symbol is in the user's positions, append
    `already_held` to filter_reasons."""
    src = _src("update_dashboard.py")
    assert 'reasons.append("already_held")' in src
    assert "held_symbols" in src
    # Built from positions_list
    assert ("held_symbols = {(p.get(\"symbol\") or \"\").upper()" in src
            or "held_symbols" in src)


def test_pt39_trend_filter_bridged_to_filter_reasons():
    """The trend filter from pt.39 sets `_filtered_by_trend`. Pt.45
    bridges that into the `filter_reasons` list so the dashboard
    chip render can show it."""
    src = _src("update_dashboard.py")
    assert 'reasons.append("below_50ma")' in src
    assert 'reasons.append("above_50ma")' in src
    assert '_filtered_by_trend' in src


def test_pt40_breakout_unconfirmed_bridged_to_filter_reasons():
    """Pt.40 sets `_breakout_unconfirmed`. Bridge into filter_reasons."""
    src = _src("update_dashboard.py")
    assert 'reasons.append("breakout_unconfirmed")' in src
    assert '_breakout_unconfirmed' in src


def test_chase_and_volatility_blocks_still_present():
    """Don't regress the existing round-20 chase_block + volatility_block
    tags. These were the original filter_reasons entries."""
    src = _src("update_dashboard.py")
    assert 'chase_block' in src
    assert 'volatility_block' in src


def test_will_deploy_set_from_filter_reasons():
    """`will_deploy = not reasons` so any non-empty reasons list
    flips the deploy flag false."""
    src = _src("update_dashboard.py")
    assert 'p["will_deploy"] = not reasons' in src


# ============================================================================
# Dashboard JS source pins
# ============================================================================

def test_dashboard_renders_filter_reason_chips():
    """The screener table render path must consume `p.filter_reasons`
    and emit chips so users see the WHY."""
    src = _src("templates/dashboard.html")
    assert "filter_reasons" in src
    assert "_reasonLabels" in src


def test_dashboard_filter_chips_have_tooltip():
    """Each filter chip must have a `title` attribute explaining the
    block — the chip's short label isn't enough on its own."""
    src = _src("templates/dashboard.html")
    # The chip-render block lives near the screener table builder
    idx = src.find("_filterReasons")
    assert idx > 0
    body = src[idx:idx + 1800]
    assert "title=" in body
    assert "won\\'t be auto-deployed" in body or "Filtered:" in body


def test_dashboard_recognises_all_known_filter_reasons():
    """Every filter reason emitted by the server side must have a
    label entry (or fall back to the raw code). Pin each known
    reason so a future addition doesn't silently render as a code."""
    src = _src("templates/dashboard.html")
    # _reasonLabels object should at least mention these keys
    assert "'already_held'" in src
    assert "'below_50ma'" in src
    assert "'above_50ma'" in src
    assert "'breakout_unconfirmed'" in src


def test_dashboard_chase_and_volatility_chips_have_dynamic_label():
    """chase_block / volatility_block include the live value in the
    server-side string (e.g. "chase_block (+12.3% intraday)"). The
    chip render strips the prefix and keeps the value."""
    src = _src("templates/dashboard.html")
    idx = src.find("_filterReasons")
    body = src[idx:idx + 1800]
    # Both prefixes referenced
    assert "chase_block" in body
    assert "volatility_block" in body
