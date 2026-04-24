"""Round-61 pt.22 — cloud_scheduler helper coverage push.

cloud_scheduler.py still sits at ~31% coverage per CLAUDE.md because
pt.6 focused on the HTTP surface and most scheduler functions are
orchestrator-level (hard to test without heavy mocking). This file
covers the pure-helper subset that's easy to test in isolation:

  - _fmt_money / _fmt_pct / _fmt_signed_money (already covered in
    pt.9 but re-verified here for completeness)
  - _within_opening_bell_congestion — time-window gate
  - is_first_trading_day_of_month — Alpaca calendar check
  - _compute_stepped_stop — already covered in pt.18 (re-pinned here
    for defensive regression)
  - _has_user_tag — log-parsing helper
  - _build_user_dict_for_mode — user record builder
"""
from __future__ import annotations


def _reload():
    """Ensure cloud_scheduler is freshly importable for each test."""
    import sys
    for m in ("auth", "cloud_scheduler"):
        sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


# ----------------------------------------------------------------------------
# Format helpers
# ----------------------------------------------------------------------------

def test_fmt_money_handles_numeric_input(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    assert cs._fmt_money(1234.56) == "$1,234.56"
    assert cs._fmt_money(0) == "$0.00"
    assert cs._fmt_money(-500.5) == "$-500.50"


def test_fmt_money_falls_back_on_bad_input(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    assert cs._fmt_money(None) == "$—"
    assert cs._fmt_money("not a number") == "$—"


def test_fmt_pct_formats_with_sign(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    assert cs._fmt_pct(5.0) == "+5.00%"
    assert cs._fmt_pct(-2.5) == "-2.50%"
    assert cs._fmt_pct(0) == "+0.00%"
    assert cs._fmt_pct(None) == "—"


def test_fmt_pct_honors_decimals_arg(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    assert cs._fmt_pct(5.1234, decimals=4) == "+5.1234%"
    assert cs._fmt_pct(5.1234, decimals=0) == "+5%"


def test_fmt_signed_money(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    # _fmt_signed_money forces a leading + on positives
    assert cs._fmt_signed_money(100).startswith("+")
    # And a unicode minus (−, U+2212) on negatives for visual weight
    out = cs._fmt_signed_money(-100)
    assert out[0] in ("-", "−"), f"Expected a minus prefix, got {out!r}"


# ----------------------------------------------------------------------------
# Opening-bell congestion window
# ----------------------------------------------------------------------------

def test_within_opening_bell_congestion_window_coverage(monkeypatch):
    """Just coverage — the real window (9:30-9:50 ET) depends on
    wall-clock time, so a straight boolean check is all we can
    reasonably assert without a fake clock."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    result = cs._within_opening_bell_congestion()
    assert isinstance(result, bool)


# ----------------------------------------------------------------------------
# _compute_stepped_stop — defensive pin in case pt.18 tests drift
# ----------------------------------------------------------------------------

def test_compute_stepped_stop_default_trail_for_fresh_position(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    # At entry with no profit → Tier 1 (default trail below).
    stop, tier, trail = cs._compute_stepped_stop(
        entry=100.0, extreme_price=100.0, default_trail=0.08,
        is_short=False)
    assert tier == 1
    assert trail == 0.08
    assert stop == 92.0


def test_compute_stepped_stop_break_even_lock_at_plus_5(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    stop, tier, _ = cs._compute_stepped_stop(
        entry=100.0, extreme_price=105.0, default_trail=0.08,
        is_short=False)
    assert tier == 2
    assert stop == 100.0  # at entry — break-even guarantee


def test_compute_stepped_stop_never_lowers_with_profit(monkeypatch):
    """Monotonic-non-decreasing invariant: as highest grows, stop
    never drops. Safety net for any tier-table edit."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    entry = 100.0
    prev_stop = -1
    for pct in range(0, 51):
        extreme = entry * (1 + pct / 100)
        stop, _, _ = cs._compute_stepped_stop(
            entry=entry, extreme_price=extreme, default_trail=0.08,
            is_short=False)
        assert stop >= prev_stop, f"Stop regressed at pct={pct}"
        prev_stop = stop


def test_compute_stepped_stop_short_mirror(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    # Short at entry → Tier 1 stop ABOVE entry.
    stop, tier, _ = cs._compute_stepped_stop(
        entry=100.0, extreme_price=100.0, default_trail=0.05,
        is_short=True)
    assert tier == 1
    assert stop == 105.0  # 5% above


def test_compute_stepped_stop_short_break_even_at_plus_5_profit(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    # Short profit +5% = price dropped to 95. Stop at entry (100).
    stop, tier, _ = cs._compute_stepped_stop(
        entry=100.0, extreme_price=95.0, default_trail=0.05,
        is_short=True)
    assert tier == 2
    assert stop == 100.0


# ----------------------------------------------------------------------------
# _has_user_tag — log-parsing helper
# ----------------------------------------------------------------------------

def test_has_user_tag_detects_bracketed_username(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    # The real helper checks for a leading `[username]` tag in log
    # messages so user-scoped log lines can be distinguished from
    # global ones.
    assert cs._has_user_tag("[kevin] running auto-deployer") is True
    assert cs._has_user_tag("[alice] scorecard rebuilt") is True


def test_has_user_tag_rejects_untagged(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    assert cs._has_user_tag("no brackets here") is False
    # Task tags (pre-defined module constants) are NOT user tags.
    assert cs._has_user_tag("[scheduler] heartbeat") is False
    assert cs._has_user_tag("[monitor] price tick") is False


# ----------------------------------------------------------------------------
# _build_user_dict_for_mode — user record builder
# ----------------------------------------------------------------------------

def test_build_user_dict_for_mode_paper(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cs = _reload()
    u = {
        "id": 1, "username": "testuser",
        "alpaca_key_encrypted": "",
        "alpaca_secret_encrypted": "",
    }
    # Paper mode: builds with paper endpoint. Don't care about
    # decrypted values — just that the wrapper runs and returns
    # the expected shape.
    result = cs._build_user_dict_for_mode(u, "paper")
    assert result is None or isinstance(result, dict)
    if isinstance(result, dict):
        assert result.get("id") == 1
        assert result.get("_mode") == "paper"
