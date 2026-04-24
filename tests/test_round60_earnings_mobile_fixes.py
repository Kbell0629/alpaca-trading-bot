"""
Round-60 fixes from user's post-merge live-operations feedback:

  Fix 1a — ETFs skip earnings_exit entirely. SOXL/IBIT/MSOS/etc. don't
           have earnings and were spamming Sentry alerts.
  Fix 1b — Sentry breadcrumbs dedup per (symbol, error) per ET day.
           Pre-market AH monitor was firing 60+ alerts/morning; now
           one per unique failure per day.
  Fix 2  — Mobile jitter: hash-skip now normalises timestamp-only
           variations so quiet ticks skip the DOM replace entirely.
           In-place timestamp patches update freshness chips without
           touching scroll position.
  Fix 3  — Position Correlation section on mobile horizontally
           scrolls the sector rows (dollar column no longer truncated
           to "$23,5...").
  Fix 4  — Dashboard's "Win Rate" metric honours round-58's
           win_rate_pct_display field. Shows "N=2 Need 5+ trades"
           instead of alarmist "0%".
"""
from __future__ import annotations

import sys as _sys


def _reload_earnings(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "a" * 64)
    _sys.modules.pop("earnings_exit", None)
    import earnings_exit
    earnings_exit.clear_cache()
    return earnings_exit


# ========= Fix 1a: ETF skip list =========

def test_known_etfs_skip_earnings_lookup(monkeypatch):
    """SOXL / IBIT / MSOS / SPY must short-circuit in should_exit_for_
    earnings without calling yfinance."""
    ee = _reload_earnings(monkeypatch)

    call_count = {"n": 0}

    def _fake_fetch(sym):
        call_count["n"] += 1
        return None  # simulate a failure if it ever gets called

    monkeypatch.setattr(ee, "_fetch_next_earnings_from_yfinance", _fake_fetch)

    for etf in ("SOXL", "IBIT", "MSOS", "SPY", "QQQ", "XLK", "JETS"):
        should_exit, reason, days = ee.should_exit_for_earnings(
            etf, "breakout", days_before=1)
        assert should_exit is False
        assert reason is None
        assert days is None
    assert call_count["n"] == 0, (
        "ETFs must not hit yfinance — got " + str(call_count["n"]) +
        " fetches. Round-60 ETF skip list regression.")


def test_etf_list_is_frozenset():
    """_KNOWN_ETFS must be a frozenset — lookups are O(1) and immutable
    prevents accidental runtime mutation breaking the skip logic."""
    import earnings_exit
    assert isinstance(earnings_exit._KNOWN_ETFS, frozenset)
    # Core ETF coverage
    for sym in ("SOXL", "IBIT", "MSOS", "SPY", "QQQ", "XLK", "JETS",
                "IWM", "GLD", "VXX", "TQQQ"):
        assert sym in earnings_exit._KNOWN_ETFS, (
            f"Expected {sym} in _KNOWN_ETFS")


def test_etf_lookup_is_case_insensitive():
    import earnings_exit as ee
    assert ee._is_etf("soxl") is True
    assert ee._is_etf("SOXL") is True
    assert ee._is_etf("SoXl") is True
    # Stocks remain False
    assert ee._is_etf("INTC") is False
    assert ee._is_etf("CRDO") is False
    assert ee._is_etf("") is False
    assert ee._is_etf(None) is False


# ========= Fix 1b: Sentry dedup per (symbol, error) per day =========

def test_sentry_dedup_fires_only_once_per_symbol_per_day(monkeypatch):
    """Before round-60, every 5-min AH monitor tick fired a fresh
    breadcrumb. 60+ alerts per morning. Now only ONE alert fires per
    (symbol, error) per ET calendar day."""
    ee = _reload_earnings(monkeypatch)

    # Simulate a "network" fetch failure for INTC
    monkeypatch.setattr(ee, "_fetch_next_earnings_from_yfinance",
                         lambda s: None)
    ee._LAST_FETCH_ERR["INTC"] = "network:TimeoutError"

    fired = []

    class _FakeObs:
        @staticmethod
        def capture_message(msg, level="info", **ctx):
            fired.append((msg, ctx))

    # Patch observability module in sys.modules so the dynamic import
    # inside should_exit_for_earnings picks up our fake.
    _sys.modules["observability"] = _FakeObs

    for _ in range(10):
        ee.should_exit_for_earnings("INTC", "breakout", days_before=1)

    assert len(fired) == 1, (
        f"Expected exactly 1 Sentry fire for 10 calls, got {len(fired)}. "
        "Round-60 dedup regression — pre-market AH monitor will spam "
        "alerts again.")
    assert "INTC" in fired[0][0]
    assert fired[0][1].get("error") == "network:TimeoutError"

    _sys.modules.pop("observability", None)


def test_sentry_dedup_fires_separately_for_different_errors(monkeypatch):
    """A symbol can have DIFFERENT error types hit in the same day
    (e.g., first network: then later shape_drift:). We want ONE alert
    per (symbol, error) pair — same error = deduped, different error
    = fires."""
    ee = _reload_earnings(monkeypatch)
    monkeypatch.setattr(ee, "_fetch_next_earnings_from_yfinance",
                         lambda s: None)

    fired = []

    class _FakeObs:
        @staticmethod
        def capture_message(msg, level="info", **ctx):
            fired.append((msg, ctx))

    _sys.modules["observability"] = _FakeObs

    # First: network error
    ee._LAST_FETCH_ERR["INTC"] = "network:TimeoutError"
    ee.should_exit_for_earnings("INTC", "breakout", days_before=1)
    ee.should_exit_for_earnings("INTC", "breakout", days_before=1)  # dedup

    # Second: shape drift — should fire (different error)
    ee._LAST_FETCH_ERR["INTC"] = "shape_drift:KeyError"
    ee.should_exit_for_earnings("INTC", "breakout", days_before=1)
    ee.should_exit_for_earnings("INTC", "breakout", days_before=1)  # dedup

    assert len(fired) == 2, (
        f"Expected 2 fires (one per unique error), got {len(fired)}")
    _sys.modules.pop("observability", None)


def test_sentry_dedup_does_not_fire_on_no_future_unreported(monkeypatch):
    """Legitimate 'no upcoming earnings in the next 8 scheduled events'
    stays quiet — it's not a fetch failure."""
    ee = _reload_earnings(monkeypatch)
    monkeypatch.setattr(ee, "_fetch_next_earnings_from_yfinance",
                         lambda s: None)

    fired = []

    class _FakeObs:
        @staticmethod
        def capture_message(msg, level="info", **ctx):
            fired.append((msg, ctx))

    _sys.modules["observability"] = _FakeObs

    ee._LAST_FETCH_ERR["INTC"] = "no_future_unreported"
    for _ in range(5):
        ee.should_exit_for_earnings("INTC", "breakout", days_before=1)

    assert len(fired) == 0, (
        "'no_future_unreported' is legitimate quiet — must not fire")
    _sys.modules.pop("observability", None)


# ========= Fix 2: Mobile jitter — timestamp-aware hash-skip =========

def test_hash_skip_normalises_timestamp_variation():
    """The normalised hash input must strip 'Updated ...', 'Ns ago',
    and the Last-updated title attribute so tick-only changes don't
    trigger a full DOM replace (which causes scroll jitter on mobile)."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    # The normalising regex must be present
    assert "_normHash" in src
    assert "Updated [^<]*<" in src or "Updated .*<" in src
    assert ">\\d+s ago<" in src
    assert ">\\d+m ago<" in src
    assert ">\\d+h ago<" in src


def test_hash_skip_in_place_patch_branch_present():
    """On quiet tick (hash matches), the in-place update branch must
    patch the 'Updated' chip + freshness chips without touching scroll."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    assert "_lastAppNormHash" in src
    # In-place rewrite path
    assert ".outerHTML = freshnessChip" in src
    # Last-data-refresh chip uses textContent update
    assert '_updatedEls' in src


def test_freshness_chip_emits_data_label():
    """freshnessChip must emit data-label='...' when given a label, so
    the in-place patch can regenerate just the right chip.

    Round-61 pt.8 Option B: freshnessChip moved from inline dashboard.html
    into static/dashboard_render_core.js. Check the new location; if the
    function isn't there, also fall back to dashboard.html in case a
    future refactor inlines it again.
    """
    paths = ["static/dashboard_render_core.js", "templates/dashboard.html"]
    combined = ""
    for p in paths:
        try:
            with open(p) as f:
                combined += f.read() + "\n"
        except FileNotFoundError:
            pass
    assert 'data-label="' in combined, (
        "freshnessChip output must include data-label for the in-place "
        "patch path")
    assert "var labelAttr = label" in combined, (
        "The labelAttr computation must live in the function body so the "
        "data-label is emitted conditionally on the label argument")


# ========= Fix 3: Position Correlation mobile scroll =========

def test_position_correlation_rows_have_min_width_and_scroll_wrap():
    """Each sector row needs a min-width so the money column stays
    readable, AND the list wraps in overflow-x:auto so narrow mobile
    viewports can swipe horizontally rather than truncate to '$23,5...'."""
    with open("templates/dashboard.html") as f:
        src = f.read()
    # Look in the correlation section specifically
    start = src.find("class=\"correlation-section\"")
    # If the class lookup misses because the string is dynamic, fall
    # back to the sectorsList block.
    if start < 0:
        start = src.find("sectorsList.map")
    assert start > 0, "correlation rows block moved"
    window = src[max(0, start - 2000):start + 3000]
    assert "min-width:420px" in window, (
        "Correlation rows need min-width to keep the dollar column "
        "readable on mobile — round-60")
    assert "overflow-x:auto" in window, (
        "Correlation rows must be wrapped in overflow-x:auto for "
        "mobile swipe — round-60")


# ========= Fix 4: Dashboard reads win_rate_pct_display =========

def _dashboard_sources():
    """Round-61 pt.8 Option B: panel renderers are split between
    inline dashboard.html (Readiness card) and the extracted
    static/dashboard_render_core.js (Comparison panel). Grep
    against the CONCATENATED source so invariants survive the
    extraction boundary."""
    import pathlib
    out = []
    for p in ("templates/dashboard.html", "static/dashboard_render_core.js"):
        try:
            out.append(pathlib.Path(p).read_text())
        except FileNotFoundError:
            pass
    return "\n".join(out)


def test_dashboard_readiness_uses_win_rate_reliable_flag():
    """Round-58 plumbed win_rate_reliable + win_rate_sample_size +
    win_rate_display_note through /api/data. Round-60 actually wires
    those into the dashboard UI. Round-61 pt.8 extracted the
    comparison panel into dashboard_render_core.js; the pin greps
    both sources so refactors don't break the invariant."""
    src = _dashboard_sources()
    assert "sc.win_rate_reliable" in src
    assert "sc.win_rate_sample_size" in src
    assert "sc.win_rate_display_note" in src
    # The readiness + comparison panels must branch on win_rate_reliable
    reliable_checks = src.count("win_rate_reliable")
    assert reliable_checks >= 2, (
        f"Expected >=2 win_rate_reliable branches (readiness + "
        f"comparison panel), got {reliable_checks}")


def test_dashboard_shows_sample_size_instead_of_zero():
    """When win_rate_reliable is False, the UI must render 'N=X' +
    the display note, not '0%'."""
    src = _dashboard_sources()
    assert "N=" in src and "sc.win_rate_sample_size" in src
    assert "Need 5+" in src, (
        "UI must prompt the user that 5+ closed trades are needed "
        "— round-60 copy")


# ========= Integration: force_refresh + get_last_fetch_error still work =========

def test_force_refresh_still_operational(monkeypatch):
    """force_refresh from round-58 still bypasses the cache + ETF skip
    (since force_refresh is operator-initiated)."""
    ee = _reload_earnings(monkeypatch)
    fetched = []
    monkeypatch.setattr(ee, "_fetch_next_earnings_from_yfinance",
                         lambda s: (fetched.append(s), None)[1])
    # force_refresh must still fetch even for SOXL (operator asked for it)
    ee.force_refresh("SOXL")
    assert "SOXL" in fetched, (
        "force_refresh must bypass the ETF skip — operator may want "
        "to verify the fetch layer works against a known-empty symbol")
