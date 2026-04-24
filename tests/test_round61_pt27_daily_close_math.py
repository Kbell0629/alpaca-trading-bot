"""Round-61 pt.27 — daily-close email "Today" math uses last_equity.

User-reported via the Apr 24 daily-close email: "Today: +$39.29
(+0.04%)" was shown despite total unrealized being +$750.17 on the
same day. The two numbers measure different things (today-delta vs.
cumulative-since-entry), but a +$39 daily delta was clearly wrong
given the CRDO +3.5% / INTC +22% / HIMS +43.9% moves visible on
the dashboard the same day.

Root cause: `_build_daily_close_report` used
`guardrails.daily_starting_value` as the baseline for the "Today"
delta. That value is captured by the monitor's FIRST tick of the
day (see cloud_scheduler.py:1222-1223). If the monitor didn't run
immediately at 9:30 AM open (e.g. bot was mid-deploy or the
scheduler was in a heartbeat-gap), daily_starting_value captures a
mid-day value, missing the earlier intraday gains.

Fix: use Alpaca's `last_equity` field (yesterday's close) as the
baseline. It's Alpaca's canonical "previous day's closing equity"
and always reflects the full trading day's delta. Fall back to
daily_starting_value only if last_equity is missing (defensive —
Alpaca always populates it).
"""
from __future__ import annotations


def _reload():
    import sys
    for m in ("auth", "cloud_scheduler"):
        sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


def test_today_uses_last_equity_not_daily_starting_value(monkeypatch):
    """Regression pin for the user-reported Apr 24 scenario.

    Apr 24 actual values:
      account.portfolio_value = 103133.02
      account.last_equity     = 102425.35   (yesterday's close)
      guardrails.daily_starting_value = 103214.08  (monitor captured late)

    Expected: Today = portfolio_value - last_equity = +$707.67
    Old (buggy): Today = portfolio_value - daily_starting_value = -$81.06
                 (or as user saw with slightly different numbers: +$39.29)
    """
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()

    # Mock out the /positions call so the report builder doesn't try
    # to hit Alpaca — we're only testing the Today math here.
    monkeypatch.setattr(cs, "user_api_get", lambda u, url: [])

    account = {
        "portfolio_value": "103133.02",
        "last_equity": "102425.35",
        "cash": "98412.82",
        "buying_power": "181064.61",
    }
    scorecard = {"current_value": 103133.02}
    guardrails = {
        "daily_starting_value": 103214.08,
        "peak_portfolio_value": 103253.37,
    }
    user = {"id": 1, "username": "testuser"}

    report = cs._build_daily_close_report(
        user, account, scorecard, guardrails,
        daily_starting_value=103214.08,
    )

    # Find the Today line.
    today_line = None
    for line in report.split("\n"):
        if line.startswith("Today:"):
            today_line = line
            break
    assert today_line is not None, f"Today line missing: {report}"

    # Must reflect the full day: 103133.02 - 102425.35 = +$707.67
    # (not +$39.29 or similar from the daily_starting_value baseline)
    assert "+$707.67" in today_line or "707." in today_line, (
        f"Today line must use last_equity baseline (~+$707.67), not "
        f"daily_starting_value. Got: {today_line!r}")


def test_today_falls_back_to_daily_starting_value_when_last_equity_missing(monkeypatch):
    """Defensive: if Alpaca didn't return last_equity (very rare),
    fall back to the existing daily_starting_value path."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    monkeypatch.setattr(cs, "user_api_get", lambda u, url: [])

    account = {
        "portfolio_value": "103133.02",
        # No last_equity!
        "cash": "98412.82",
        "buying_power": "181064.61",
    }
    scorecard = {}
    guardrails = {
        "daily_starting_value": 102500.00,
        "peak_portfolio_value": 103253.37,
    }
    user = {"id": 1, "username": "testuser"}
    report = cs._build_daily_close_report(
        user, account, scorecard, guardrails,
        daily_starting_value=102500.00,
    )
    today_line = [L for L in report.split("\n")
                  if L.startswith("Today:")][0]
    # Fallback baseline: 103133.02 - 102500.00 = +$633.02
    assert "+$633.02" in today_line or "633." in today_line, (
        f"Fallback to daily_starting_value when last_equity missing. "
        f"Got: {today_line!r}")


def test_today_shows_zero_when_both_baselines_missing(monkeypatch):
    """If neither last_equity nor daily_starting_value is available,
    don't divide by zero — just show $0.00."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    cs = _reload()
    monkeypatch.setattr(cs, "user_api_get", lambda u, url: [])

    account = {"portfolio_value": "103133.02", "cash": "0", "buying_power": "0"}
    scorecard = {}
    guardrails = {}
    user = {"id": 1, "username": "testuser"}
    report = cs._build_daily_close_report(
        user, account, scorecard, guardrails,
        daily_starting_value=None,
    )
    today_line = [L for L in report.split("\n")
                  if L.startswith("Today:")][0]
    assert "0.00" in today_line, f"Expected $0.00 fallback, got: {today_line!r}"


def test_source_pin_last_equity_is_preferred():
    """Source-pin: `last_equity` must be tried BEFORE
    daily_starting_value in the baseline selection."""
    with open("cloud_scheduler.py") as f:
        src = f.read()
    # Find the _build_daily_close_report function body.
    idx = src.find("def _build_daily_close_report")
    assert idx > 0
    # Look forward for the baseline-selection block.
    body = src[idx:idx + 3000]
    assert "start_val = last_equity if last_equity else" in body, (
        "start_val selection must prefer last_equity (yesterday's "
        "close from Alpaca) over daily_starting_value (monitor's "
        "first-tick snapshot, which can be mid-day).")
