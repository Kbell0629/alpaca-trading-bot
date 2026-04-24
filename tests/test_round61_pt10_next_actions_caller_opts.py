"""
Round-61 pt.10 regression pin.

User screenshot: the "What Happens at Market Open" timeline showed
`Auto-Deployer` + `Screener refresh` as OFF even though the Auto
toggle at the top of the dashboard was ON.

Root cause: pt.8 batch-10 (#133) extracted `buildNextActionsPanel`
into `dashboard_render_core.js`. The extracted function reads
`autoDeployerEnabled` / `killSwitchActive` / `guardrailsData` via
the `opts` parameter OR falls back to `window.<name>`. But the
inline dashboard script keeps those as module-local `let` variables
— they're NOT on `window`. So the fallback always got `undefined`
→ falsy → every ON/OFF badge that depended on `autoDeployerEnabled`
rendered OFF.

Fix (this PR): inline callers MUST pass the current values as `opts`.

These pins lock that contract so a future refactor that drops the
opts argument fails loudly instead of silently rendering stale state.
"""
from __future__ import annotations


def _dashboard_src():
    with open("templates/dashboard.html") as f:
        return f.read()


def test_buildNextActionsPanel_caller_passes_autoDeployerEnabled():
    """The inline caller of buildNextActionsPanel MUST pass
    autoDeployerEnabled via opts — reading it from the module-local let
    instead of relying on the window-fallback (which is always
    undefined)."""
    src = _dashboard_src()
    assert "buildNextActionsPanel(d, {" in src, (
        "Inline caller must pass an opts object to buildNextActionsPanel "
        "so the extracted function sees the current autoDeployerEnabled / "
        "killSwitchActive / guardrails values. Without opts, the "
        "extracted module falls back to window.<name> which isn't set "
        "(module-local `let`s don't auto-attach to window).")
    assert "autoDeployerEnabled: autoDeployerEnabled" in src
    assert "killSwitchActive: killSwitchActive" in src
    assert "guardrails: guardrailsData" in src


def test_buildGuardrailMeters_caller_passes_guardrails():
    """Same pattern for buildGuardrailMeters: the 4th arg `guardrails`
    must be passed explicitly rather than relying on window fallback."""
    src = _dashboard_src()
    assert "buildGuardrailMeters(dailyPnlPct, portfolioValue, lastEquity, guardrailsData)" in src, (
        "Inline caller must pass guardrailsData as the 4th positional "
        "arg so the extracted function doesn't try to read "
        "window.guardrailsData (which isn't set).")


def test_render_core_next_actions_still_has_opts_fallback():
    """The extracted function should still have the window-fallback as a
    SAFETY NET (so it doesn't crash if a future caller forgets opts),
    but the inline caller is now the canonical path."""
    with open("static/dashboard_render_core.js") as f:
        src = f.read()
    # The function signature must accept opts
    assert "function buildNextActionsPanel(d, opts)" in src
    # And it must read from opts first, window second (fallback)
    assert "opts.autoDeployerEnabled" in src
    assert "opts.killSwitchActive" in src
    assert "opts.guardrails" in src
