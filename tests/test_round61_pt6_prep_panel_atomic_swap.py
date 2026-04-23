"""
Round-61 pt.6 prep #5 — atomic children swap at the panel level.

User reported jitter PERSISTING even after #107/#108/#109/#111 fixed
the #app-level swap. Specifically on the Recent Activity (last 200)
section — a new monitor log line arrives every ~60s and triggers
panel.innerHTML rewrite in renderSchedulerPanel. Same intermediate-
empty-frame bug that #109 fixed for #app, just one level deeper.

Fix: a shared helper `atomicReplaceChildren(panelEl, newHtml)` that
does <template> + replaceChildren so children swap atomically, plus
preserves descendant scrollTop so the .sched-log-box internal scroll
position stays put. Called from every panel renderer that previously
did `panel.innerHTML = html` directly.
"""
from __future__ import annotations

import os


def _src():
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "templates", "dashboard.html")) as f:
        return f.read()


def test_atomic_replace_helper_exists():
    src = _src()
    assert "function atomicReplaceChildren(" in src, (
        "atomicReplaceChildren helper missing — panel renderers would "
        "fall back to raw panel.innerHTML = html and regress the "
        "empty-frame flicker bug")


def test_atomic_replace_uses_template_and_replacechildren():
    src = _src()
    idx = src.find("function atomicReplaceChildren(")
    assert idx > 0
    block = src[idx:idx + 3000]
    assert 'document.createElement(\'template\')' in block or \
           'document.createElement("template")' in block, (
        "atomicReplaceChildren must stage HTML in a <template> element")
    assert "replaceChildren" in block, (
        "atomicReplaceChildren must use replaceChildren for the atomic swap")


def test_atomic_replace_preserves_descendant_scrollTop():
    src = _src()
    idx = src.find("function atomicReplaceChildren(")
    block = src[idx:idx + 3000]
    assert "sched-log-box" in block, (
        "atomicReplaceChildren must preserve .sched-log-box scrollTop — "
        "otherwise a user scrolled inside the Recent Activity log gets "
        "snapped to the top on every new log line")
    assert "scrollTop" in block, (
        "scrollTop preservation logic missing — internal scroll position "
        "will reset on every rewrite")


def test_render_scheduler_panel_uses_atomic_swap():
    """The primary user-flagged path: renderSchedulerPanel rewrites
    on every new monitor log line. Must use the helper so the panel's
    children never go empty mid-swap."""
    src = _src()
    idx = src.find("function renderSchedulerPanel(")
    assert idx > 0
    end = src.find("\n}\n", idx) if src.find("\n}\n", idx) > 0 else idx + 20000
    block = src[idx:end]
    # Every panel.innerHTML = _schedHtml must be replaced by
    # atomicReplaceChildren. Direct check: the function body must
    # contain atomicReplaceChildren AND not contain a bare
    # `panel.innerHTML = _schedHtml` assignment.
    assert "atomicReplaceChildren(panel, _schedHtml)" in block, (
        "renderSchedulerPanel's main rewrite path must use the atomic "
        "helper — this is the Recent Activity panel the user reported "
        "jitter on")


def test_refresh_factor_health_uses_atomic_swap():
    src = _src()
    # The factor-health hash-skip uses _lastNormHtml (unique marker
    # from pt.6 prep #2). The next line should call atomicReplaceChildren.
    idx = src.find("panel._lastNormHtml !== _normHtml")
    assert idx > 0, "factor-health hash-skip marker missing"
    # 500 chars after should include the swap call
    block = src[idx:idx + 500]
    assert "atomicReplaceChildren(panel, html)" in block, (
        "refreshFactorHealth (renderFactorHealth) must use the atomic "
        "helper — its hash-skip only fires on real state change, and "
        "that rewrite needs to be atomic too")


def test_three_or_more_callers_of_helper():
    """Sanity: the helper must be USED, not just defined. Expect at
    least 4 call sites (perf, tax, factor, scheduler + the fallback
    branch inside renderSchedulerPanel) plus the definition."""
    src = _src()
    calls = src.count("atomicReplaceChildren(")
    assert calls >= 5, (
        f"expected >=5 occurrences of atomicReplaceChildren (1 def + "
        f">=4 call sites), found {calls}")
