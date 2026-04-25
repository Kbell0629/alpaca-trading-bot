"""Round-61 pt.75 — nav-tab DOM-order match + cancel-scan reliability fix.

Two user-reported issues:
  1. Clicking nav tabs scrolled the page jumping around because nav
     order didn't match the section DOM order.
  2. SOXL "insufficient qty available" close kept failing despite
     pt.69's retry-with-backoff. Root cause: Alpaca's orders endpoint
     silently excluded "accepted"-status orders when filtered by
     `?symbols=`. The cancel scan returned 0, so retry never fired.
     Fix: drop the URL filter; trust the existing client-side filter.
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent
_DASH = (_HERE / "templates" / "dashboard.html").read_text()
_ACT = (_HERE / "handlers" / "actions_mixin.py").read_text()


# ============================================================================
# Item 1: nav order matches DOM order
# ============================================================================

def test_nav_tab_order_matches_dom():
    """The first 8 always-rendered nav tabs must appear in the same
    order they do in the DOM (overview, picks, strategies, readiness,
    positions, analytics, trades, screener)."""
    nav_idx = _DASH.find('const navTabs')
    assert nav_idx > 0
    nav_block = _DASH[nav_idx:nav_idx + 2500]

    expected_order = [
        "section-overview",
        "section-picks",
        "section-strategies",
        "section-readiness",
        "section-positions",
        "section-analytics",
        "section-trades",
        "section-screener",
    ]
    positions = [nav_block.find(f"'{sec}'") for sec in expected_order]
    # All present.
    for sec, pos in zip(expected_order, positions):
        assert pos > 0, f"{sec} missing from nav block"
    # Strictly ascending — i.e. matches DOM order.
    for i in range(len(positions) - 1):
        assert positions[i] < positions[i + 1], (
            f"{expected_order[i]} appears AFTER {expected_order[i + 1]} "
            "in nav, but BEFORE it in DOM order — re-order the nav.")


def test_nav_readiness_before_positions():
    """Specific ordering check for the user-reported jump: Readiness
    used to come AFTER Backtest in the nav but it lives in the page
    BEFORE positions. Pt.75 fixed this."""
    nav_idx = _DASH.find('const navTabs')
    nav_block = _DASH[nav_idx:nav_idx + 2500]
    readiness_pos = nav_block.find("'section-readiness'")
    positions_pos = nav_block.find("'section-positions'")
    backtest_pos = nav_block.find("'section-backtest'")
    assert readiness_pos > 0 and positions_pos > 0 and backtest_pos > 0
    assert readiness_pos < positions_pos
    assert positions_pos < backtest_pos


def test_nav_analytics_after_positions():
    """Analytics is rendered AFTER positions in the DOM (pt.46
    placement). Nav order should match."""
    nav_idx = _DASH.find('const navTabs')
    nav_block = _DASH[nav_idx:nav_idx + 2500]
    positions_pos = nav_block.find("'section-positions'")
    analytics_pos = nav_block.find("'section-analytics'")
    assert positions_pos > 0 < analytics_pos
    assert positions_pos < analytics_pos


def test_nav_trailing_section_order():
    """Backtest → Scheduler → Heatmap → Comparison → Settings is the
    DOM order for the trailing decorative section block."""
    nav_idx = _DASH.find('const navTabs')
    nav_block = _DASH[nav_idx:nav_idx + 2500]
    trailing = ["section-backtest", "section-scheduler", "section-heatmap",
                  "section-comparison", "section-settings"]
    positions = [nav_block.find(f"'{sec}'") for sec in trailing]
    for i in range(len(positions) - 1):
        assert positions[i] < positions[i + 1]


# ============================================================================
# Item 2: cancel-scan no longer uses the unreliable ?symbols= filter
# ============================================================================

def test_cancel_scan_drops_symbols_url_filter():
    """The user-reported SOXL bug was caused by Alpaca's orders
    endpoint silently excluding 'accepted'-status orders when
    filtered by ?symbols=. Pt.75 dropped that URL filter and now
    fetches ALL open orders + filters client-side."""
    idx = _ACT.find("def _cancel_pending_sell_orders")
    assert idx > 0
    body = _ACT[idx:idx + 2000]
    # The actual URL construction line must NOT include &symbols=.
    # Match the f-string inside the function body, ignoring any
    # mention in the docstring.
    url_line_idx = body.find('f"{handler.user_api_endpoint}/orders')
    assert url_line_idx > 0
    url_line_end = body.find('"', url_line_idx + 50)
    url_line = body[url_line_idx:url_line_end + 1]
    assert "&symbols=" not in url_line, (
        "Cancel-scan URL still has ?symbols= filter — "
        "Alpaca's orders endpoint silently excludes 'accepted'-"
        "status orders when filtered, causing the SOXL bug.")
    # Must still fetch open orders.
    assert "?status=open" in url_line
    # Client-side filter still in place.
    assert 'o.get("symbol")' in body


def test_cancel_scan_keeps_client_side_symbol_filter():
    """We trust the in-loop comparison `o.get("symbol") != symbol`
    to filter. That hasn't changed."""
    idx = _ACT.find("def _cancel_pending_sell_orders")
    body = _ACT[idx:idx + 2000]
    assert 'o.get("symbol") or ""' in body
    assert "symbol.upper()" in body


def test_cancel_scan_still_returns_count_and_error():
    """Return shape unchanged — `(int, Optional[str])`."""
    idx = _ACT.find("def _cancel_pending_sell_orders")
    body = _ACT[idx:idx + 2000]
    # Both early-return paths still return (0, "...")
    assert "return 0, " in body
    # The success path returns (cancelled, None)
    assert "return cancelled, None" in body


def test_cancel_scan_limit_bumped_to_200():
    """Without the symbol filter we fetch all open orders. Bump the
    limit to 200 so accounts with lots of pending stops still work."""
    idx = _ACT.find("def _cancel_pending_sell_orders")
    body = _ACT[idx:idx + 2000]
    assert "limit=200" in body


def test_cancel_scan_documents_pt75_change():
    """The docstring should explain WHY we dropped the URL filter
    (so the next reader doesn't re-add it as a "perf optimization")."""
    idx = _ACT.find("def _cancel_pending_sell_orders")
    body = _ACT[idx:idx + 2000]
    assert "pt.75" in body
    assert "accepted" in body  # explains the Alpaca quirk
