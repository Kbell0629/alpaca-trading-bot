"""
Round-61 pt.6 prep #4 — preserve async-populated panel content across
renderDashboard #app rewrites.

User report (2026-04-24, after #107/#108/#109 merged):
> "This is happening on the Recent Activity (last 200) section. it
>  refreshes and then scrolls and comes back sometimes it says loading
>  scheduler status in the scheduler area after it refreshes when it
>  jumps back to that section after the refresh"

The "Loading scheduler status" flash is the smoking gun. renderDashboard
builds a fresh #app shell that contains EMPTY placeholder <div>s for
each async-populated panel (schedulerPanel, factorHealthPanel,
perfAttributionPanel, taxReportPanel, heatmapContent, logEntries).
After replaceChildren swaps in the new shell, those panels show the
"Loading..." placeholder until the async refresh* call repopulates
them ~500ms–2s later. During the gap:
  1. Panel height collapses (placeholder is ~80px; full content is 500+)
  2. Document height shrinks
  3. Browser's scroll anchor shifts to maintain position relative to
     the next stable element — user sees scroll jump
  4. Once async fetch completes, panel grows back, scroll shifts again
  5. Net effect: visible jitter, brief "Loading..." flash

Fix: before replaceChildren, snapshot each panel's current innerHTML
into a cache. Find the matching placeholder in the new template and
transplant the cached content INTO the new placeholder. After the swap,
restore each panel's _lastHtml cache so hash-skip logic still works.

These assertions pin the fix so a future refactor can't silently drop
the panel-preservation path.
"""
from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DASH = os.path.join(_HERE, "templates", "dashboard.html")


def _src():
    with open(_DASH) as f:
        return f.read()


def test_render_dashboard_snapshots_async_panels():
    """The panel-preservation array must list every async-populated
    panel. Missing one means that panel still shows its loading
    placeholder during the swap → scroll jitter returns."""
    src = _src()
    # Find the panel cache array
    idx = src.find("_asyncPanelIds")
    assert idx > 0, (
        "_asyncPanelIds list missing — renderDashboard must cache each "
        "async panel's content before the replaceChildren swap")
    # All panels that have async refresh functions must be in the list
    window = src[idx:idx + 600]
    for panel_id in ("schedulerPanel", "factorHealthPanel",
                      "perfAttributionPanel", "taxReportPanel",
                      "heatmapContent", "logEntries"):
        assert panel_id in window, (
            f"{panel_id} missing from _asyncPanelIds — its content "
            "won't be preserved across #app rewrites, causing the "
            '"Loading..." flash + scroll jitter user reported')


def test_render_dashboard_skips_loading_placeholder_cache():
    """If the source panel IS currently showing a loading placeholder,
    there's nothing valuable to preserve — don't cache it. Prevents
    re-injecting a Loading placeholder into the new panel on initial
    page loads."""
    src = _src()
    assert 'indexOf("empty-state loading")' in src, (
        "the loading-placeholder skip check must stay — without it, "
        "initial page loads (before any panel populates) would cache "
        "the Loading placeholder and re-inject it forever")


def test_render_dashboard_transplants_cache_before_swap():
    """Cached content must be transplanted INTO the new template's
    matching placeholder BEFORE replaceChildren fires. If we did it
    after, there'd be a frame where the panels are empty."""
    src = _src()
    # Find the transplant loop
    idx = src.find("_panelCache")
    assert idx > 0
    # The transplant should be BEFORE the _appEl.replaceChildren call
    transplant_idx = src.find('_newPlaceholder.innerHTML = _panelCache')
    replace_idx = src.find('_appEl.replaceChildren')
    assert 0 < transplant_idx < replace_idx, (
        "cached panel HTML must be transplanted into the new template "
        "BEFORE replaceChildren so the swap is seamless")


def test_render_dashboard_restores_lastHtml_cache():
    """After the swap, each panel's _lastHtml cache must be restored
    so subsequent hash-skip logic (renderSchedulerPanel, refreshFactorHealth,
    etc.) doesn't false-mismatch on the next tick (their _lastHtml was
    on the now-detached old DOM element)."""
    src = _src()
    # The restoration loop
    assert "_newEl._lastHtml = _panelCache" in src, (
        "_lastHtml cache must be restored on the new panel elements "
        "after the swap — otherwise every panel's hash check thinks "
        "the world changed on the next tick and repaints")
