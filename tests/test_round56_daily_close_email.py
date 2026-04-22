"""
Round-56 tests: daily-close email display fixes.

User forwarded a screenshot of their end-of-day email showing:
  • HIMS260508P00027000 +28.29% +$58.00 (-1 sh)

Two bugs:
  1. "sh" label on an option (it's a contract, not a share).
  2. `{sym:<6}` truncates OCC symbols visually in fixed-width clients
     (OCC is 17-18 chars — the column is 3× wider than the format).
  3. "-1 sh" for a short position is confusing; prefixing "short"
     keeps the qty magnitude but reads naturally.

Round-56 adds a `_display_label(sym, qty)` closure inside
`_build_daily_close_report` that returns a friendly display string +
qty-with-noun-suffix tuple. Preserves all upstream math (pct / $ P&L
unchanged — the bug was display only).
"""
from __future__ import annotations

import sys as _sys


def _reload(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "d" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


def _fake_user():
    return {"id": 99, "username": "round56-test", "_data_dir": "/tmp/r56"}


def _mk_report(cs, monkeypatch, positions):
    """Call _build_daily_close_report with monkeypatched user_api_get
    so we don't need real Alpaca creds. /positions returns the test
    list; /orders returns []."""
    calls = {"positions": positions}

    def fake_get(user, path):
        if "/positions" in path:
            return calls["positions"]
        return []  # /orders?status=... → []

    monkeypatch.setattr(cs, "user_api_get", fake_get)
    user = _fake_user()
    account = {"portfolio_value": 102571.21, "last_equity": 102104.16,
               "cash": 75653.22, "buying_power": 160289.80}
    scorecard = {"current_value": 102571.21, "win_rate_pct": 55,
                 "readiness_score": 62, "total_trades": 24, "days_tracked": 7}
    guardrails = {"peak_portfolio_value": 102571.21,
                  "daily_starting_value": 102104.16}
    return cs._build_daily_close_report(user, account, scorecard,
                                          guardrails,
                                          daily_starting_value=102104.16)


def test_occ_option_not_labelled_sh(monkeypatch):
    """HIMS short put must NOT display as '(-1 sh)' — it's a contract."""
    cs = _reload(monkeypatch)
    positions = [{
        "symbol": "HIMS260508P00027000",
        "qty": "-1",
        "unrealized_pl": "58.00",
        "unrealized_plpc": "0.2829",
    }]
    text = _mk_report(cs, monkeypatch, positions)
    assert "(-1 sh)" not in text, (
        "Option position must not use 'sh' (shares) label — got: " + text)
    # Must call it a contract
    assert "contract" in text, "Option display must label it 'contract'"
    # Must identify the underlying
    assert "HIMS" in text
    # Must surface the right (put)
    assert "put" in text.lower()


def test_occ_short_prefix_and_contract_noun(monkeypatch):
    """Short option: prefix 'short' + singular 'contract' for qty=1."""
    cs = _reload(monkeypatch)
    positions = [{
        "symbol": "HIMS260508P00027000",
        "qty": "-1",
        "unrealized_pl": "58.00",
        "unrealized_plpc": "0.2829",
    }]
    text = _mk_report(cs, monkeypatch, positions)
    # "short 1 contract" — prefix + singular noun
    assert "short 1 contract" in text, (
        "Short-1-contract display malformed: " + text)


def test_occ_long_multiple_contracts_plural(monkeypatch):
    """Long long-call, qty>1, must pluralize 'contracts'."""
    cs = _reload(monkeypatch)
    positions = [{
        "symbol": "AAPL260519C00200000",
        "qty": "5",
        "unrealized_pl": "125.00",
        "unrealized_plpc": "0.1000",
    }]
    text = _mk_report(cs, monkeypatch, positions)
    assert "5 contracts" in text, "Plural 'contracts' must appear for qty=5"
    assert "short" not in text.split("AAPL")[1][:40], (
        "Long position must NOT be prefixed 'short'")


def test_occ_strike_and_expiry_rendered(monkeypatch):
    """OCC strike $27 + expiry 260508 + underlying HIMS + right 'put'
    should all appear for HIMS260508P00027000."""
    cs = _reload(monkeypatch)
    positions = [{
        "symbol": "HIMS260508P00027000",
        "qty": "-1",
        "unrealized_pl": "58.00",
        "unrealized_plpc": "0.2829",
    }]
    text = _mk_report(cs, monkeypatch, positions)
    assert "HIMS" in text
    assert "put" in text.lower()
    assert "260508" in text, "expiry YYMMDD must be visible"
    assert "$27" in text, "strike $27 must be rendered (no mid-decimal)"


def test_equity_position_still_uses_sh(monkeypatch):
    """Equity stays 'sh'. Regression check we didn't break stocks."""
    cs = _reload(monkeypatch)
    positions = [{
        "symbol": "SOXL",
        "qty": "117",
        "unrealized_pl": "2723.76",
        "unrealized_plpc": "0.2735",
    }]
    text = _mk_report(cs, monkeypatch, positions)
    assert "117 sh" in text, "Equity long position must say 'N sh'"
    assert "contract" not in text.split("SOXL")[1][:40], (
        "Equity must NOT say 'contract'")


def test_short_equity_prefix(monkeypatch):
    """Short stock (qty < 0) — prefix 'short' on the qty suffix,
    never negative magnitude."""
    cs = _reload(monkeypatch)
    positions = [{
        "symbol": "TSLA",
        "qty": "-10",
        "unrealized_pl": "-150.00",
        "unrealized_plpc": "-0.05",
    }]
    text = _mk_report(cs, monkeypatch, positions)
    assert "short 10 sh" in text
    assert "-10 sh" not in text, "Negative qty must not appear as '-N sh'"


def test_mixed_portfolio_sorts_correctly(monkeypatch):
    """The P&L sort + winner/loser split stays correct with mixed
    equity + option positions after display changes. (Math untouched.)"""
    cs = _reload(monkeypatch)
    positions = [
        {"symbol": "SOXL", "qty": "117", "unrealized_pl": "2723.76",
         "unrealized_plpc": "0.2735"},
        {"symbol": "HIMS260508P00027000", "qty": "-1",
         "unrealized_pl": "58.00", "unrealized_plpc": "0.2829"},
        {"symbol": "USAR", "qty": "145", "unrealized_pl": "217.67",
         "unrealized_plpc": "0.0631"},
        {"symbol": "XYZ", "qty": "10", "unrealized_pl": "-50.00",
         "unrealized_plpc": "-0.025"},
    ]
    text = _mk_report(cs, monkeypatch, positions)
    # Winners block contains all 3 winners
    assert "Top winners" in text
    # HIMS (option, highest pct) first among winners
    winners_block = text.split("Top winners:")[1].split("Top losers:")[0]
    assert winners_block.index("HIMS") < winners_block.index("SOXL")
    assert winners_block.index("SOXL") < winners_block.index("USAR")
    # Losers block has XYZ
    assert "Top losers" in text
    assert "XYZ" in text.split("Top losers:")[1]


def test_total_unrealized_math_unchanged(monkeypatch):
    """Round-56 is display-only. Total unrealized P&L still sums
    across all positions untouched."""
    cs = _reload(monkeypatch)
    positions = [
        {"symbol": "AAPL", "qty": "10", "unrealized_pl": "100.00",
         "unrealized_plpc": "0.05"},
        {"symbol": "HIMS260508P00027000", "qty": "-1",
         "unrealized_pl": "58.00", "unrealized_plpc": "0.2829"},
    ]
    text = _mk_report(cs, monkeypatch, positions)
    # $158 total (100 + 58). _fmt_signed_money prefixes + and commas.
    assert "+$158" in text, "Total unrealized should sum correctly"


def test_pct_and_dollar_pnl_untouched(monkeypatch):
    """The +X.XX% and +$X.XX values were correct — round-56 must not
    alter them. Pin the format at the row level."""
    cs = _reload(monkeypatch)
    positions = [{
        "symbol": "HIMS260508P00027000",
        "qty": "-1",
        "unrealized_pl": "58.00",
        "unrealized_plpc": "0.2829",
    }]
    text = _mk_report(cs, monkeypatch, positions)
    # The HIMS row must still show +28.29% and +$58.00
    hims_row = [ln for ln in text.split("\n") if "HIMS" in ln][0]
    assert "+28.29%" in hims_row, f"% lost during refactor: {hims_row}"
    assert "+$58.00" in hims_row, f"$ lost during refactor: {hims_row}"


def test_no_occ_parse_crash_on_unexpected_shape(monkeypatch):
    """Defensive: non-OCC-shaped symbol with letters+digits must NOT
    crash the OCC regex branch. Fall back to equity display."""
    cs = _reload(monkeypatch)
    # Looks optiony but isn't valid OCC (wrong digit count)
    positions = [{
        "symbol": "ODDX123",
        "qty": "5",
        "unrealized_pl": "10.00",
        "unrealized_plpc": "0.01",
    }]
    text = _mk_report(cs, monkeypatch, positions)
    # Non-OCC → treated as equity → "5 sh"
    assert "ODDX123" in text
    assert "5 sh" in text


def test_sym_column_wider_than_6(monkeypatch):
    """The old :<6 width cramped OCC symbols in the positions block.
    The new format uses :<22}. (The open-orders block later in the
    report still uses the short-sym form — Alpaca reports OCC rarely
    there and we preserve the layout; this test targets positions.)"""
    cs = _reload(monkeypatch)
    import cloud_scheduler as _cs_src
    import inspect
    src = inspect.getsource(_cs_src._build_daily_close_report)
    # Find the positions block — between 'POSITIONS HELD' marker and the
    # next divider section ('TODAY\\'S ACTIVITY').
    start = src.find("POSITIONS HELD")
    end = src.find("TODAY'S ACTIVITY")
    assert start > 0 and end > start, "could not locate positions block"
    pos_src = src[start:end]
    # Strip comment lines — my own fix includes "Previously `{sym:<6}`"
    # in its explanation comment. We care about live format strings.
    non_comment = "\n".join(
        ln for ln in pos_src.splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "{sym:<6}" not in non_comment, (
        "Positions block must not use old narrow {sym:<6} column")
    assert ":<22}" in non_comment, (
        "Positions block must pad display column to >=22 chars")
