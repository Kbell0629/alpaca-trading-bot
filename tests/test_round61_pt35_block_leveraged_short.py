"""Round-61 pt.35 — block leveraged + inverse ETFs from short-sell entries.

User-reported SOXL incident (round-61 pt.30): the screener picked
SOXL (3x leveraged semis) as a short-sell candidate, and the position
lost ~$500 in 9 paper-trading days from a normal-sized adverse move.
Two structural problems with shorting leveraged/inverse ETFs:

  1. **Decay** — daily-reset products lose value to volatility drag
     even when the underlying is FLAT. A flat-price stretch turns
     against a short fundamentally differently from a normal stock.
     You can't short SOXL "expecting nothing to happen" — the product
     itself rolls daily.
  2. **Inverse signal logic error** — shorting an INVERSE ETF
     (SOXS, SQQQ, SDOW) is effectively going LONG the underlying.
     The screener's "bearish on SOXS" thesis is the SAME as
     "bullish on semis" but executed via a short. Always wrong on
     signal alignment.

Pt.35 adds a hard blocklist + filter at the top of
`identify_short_candidates`. Behavioural cost: zero on production
data because no leveraged ETF was a real short signal anyway; pure
loss prevention.
"""
from __future__ import annotations


# ============================================================================
# constants.LEVERAGED_OR_INVERSE_ETFS — the canonical list
# ============================================================================

def test_leveraged_etfs_set_includes_3x_long_semis():
    """SOXL is the prototype case (round-61 pt.30 incident). Pin it
    + the well-known siblings TQQQ/UPRO/TNA."""
    from constants import LEVERAGED_OR_INVERSE_ETFS
    for sym in ("SOXL", "TQQQ", "UPRO", "TNA", "FAS", "LABU"):
        assert sym in LEVERAGED_OR_INVERSE_ETFS, (
            f"{sym} (3x leveraged long) must be in the blocklist")


def test_leveraged_etfs_set_includes_3x_inverse_semis():
    """Inverse ETFs are a separate hazard: shorting SOXS = bullish
    semis. Pin SOXS/SQQQ/SPXU/SDOW so that logic error can't slip
    through."""
    from constants import LEVERAGED_OR_INVERSE_ETFS
    for sym in ("SOXS", "SQQQ", "SPXU", "SDOW", "TZA", "FAZ"):
        assert sym in LEVERAGED_OR_INVERSE_ETFS, (
            f"{sym} (3x leveraged inverse) must be in the blocklist")


def test_leveraged_etfs_set_includes_2x():
    """2x products have the same decay problem, just half-strength.
    Still fundamentally broken for short-selling."""
    from constants import LEVERAGED_OR_INVERSE_ETFS
    for sym in ("QLD", "SSO", "QID", "SDS", "DXD", "AGQ", "ZSL"):
        assert sym in LEVERAGED_OR_INVERSE_ETFS, (
            f"{sym} (2x leveraged) must be in the blocklist")


def test_leveraged_etfs_set_includes_volatility_products():
    """VXX/UVXY/VIXY are structurally short-vol-decay; shorting them
    looks like easy money but the next vol spike wipes you out."""
    from constants import LEVERAGED_OR_INVERSE_ETFS
    for sym in ("VXX", "UVXY", "VIXY"):
        assert sym in LEVERAGED_OR_INVERSE_ETFS


def test_leveraged_etfs_set_includes_single_stock_2x_3x():
    """The single-stock leveraged ETF universe (TSLL/NVDL/etc.) has
    the same decay structure as broad-market leveraged ETFs. Block
    them explicitly so a screener pick on a hot underlying like
    NVDA can't accidentally short the leveraged proxy."""
    from constants import LEVERAGED_OR_INVERSE_ETFS
    for sym in ("TSLL", "NVDL", "AMZU", "MSFU", "AAPU", "METU"):
        assert sym in LEVERAGED_OR_INVERSE_ETFS


def test_normal_stocks_NOT_in_leveraged_set():
    """Pin the negative case: a normal large-cap stock must NOT be
    in the blocklist. A bug that silently treats AAPL as a leveraged
    ETF would block all AAPL shorts forever — surface it loudly."""
    from constants import LEVERAGED_OR_INVERSE_ETFS
    for sym in ("AAPL", "MSFT", "GOOG", "META", "NVDA", "TSLA",
                 "AMD", "INTC", "BABA", "AMZN", "XOM"):
        assert sym not in LEVERAGED_OR_INVERSE_ETFS, (
            f"{sym} must NOT be in the leveraged-ETF blocklist")


def test_non_leveraged_etfs_NOT_in_blocklist():
    """Plain (1x) sector ETFs and broad-market funds are NOT in the
    blocklist — they don't have the daily-reset decay problem.
    Whether to short them is a separate decision; pt.35 only blocks
    LEVERAGED + INVERSE products."""
    from constants import LEVERAGED_OR_INVERSE_ETFS
    for sym in ("SPY", "QQQ", "IWM", "XLK", "XLF", "VTI", "GLD",
                 "TLT", "IBIT"):
        assert sym not in LEVERAGED_OR_INVERSE_ETFS, (
            f"{sym} (plain 1x ETF) should not be in the blocklist — "
            "pt.35 only targets leveraged + inverse products")


def test_is_leveraged_or_inverse_etf_returns_true_for_blocked():
    from constants import is_leveraged_or_inverse_etf
    assert is_leveraged_or_inverse_etf("SOXL") is True
    assert is_leveraged_or_inverse_etf("SQQQ") is True
    assert is_leveraged_or_inverse_etf("UVXY") is True


def test_is_leveraged_or_inverse_etf_case_insensitive():
    from constants import is_leveraged_or_inverse_etf
    assert is_leveraged_or_inverse_etf("soxl") is True
    assert is_leveraged_or_inverse_etf(" SOXL ") is True
    assert is_leveraged_or_inverse_etf("Soxl") is True


def test_is_leveraged_or_inverse_etf_returns_false_for_normal():
    from constants import is_leveraged_or_inverse_etf
    assert is_leveraged_or_inverse_etf("AAPL") is False
    assert is_leveraged_or_inverse_etf("SPY") is False
    assert is_leveraged_or_inverse_etf("QQQ") is False


def test_is_leveraged_or_inverse_etf_handles_empty_input():
    from constants import is_leveraged_or_inverse_etf
    assert is_leveraged_or_inverse_etf("") is False
    assert is_leveraged_or_inverse_etf(None) is False
    assert is_leveraged_or_inverse_etf(0) is False


# ============================================================================
# short_strategy.identify_short_candidates — filter behaviour
# ============================================================================

def _strong_short_pick(symbol, momentum=-20, daily=-5):
    """Build a screener pick that, ABSENT the pt.35 filter, would
    score high enough to be a short candidate. Used to prove the
    filter is the only reason a leveraged ETF gets dropped."""
    return {
        "symbol": symbol,
        "price": 100.0,
        "momentum_20d": momentum,
        "daily_change": daily,
        "volatility": 30,
        "rsi": 75,
        "macd_histogram": -2,
        "overall_bias": "bearish",
        "news_sentiment": "negative",
        "relative_volume": 2.0,
    }


def test_soxl_blocked_from_short_candidates():
    """The user-reported SOXL incident — even with strong bearish
    signals, SOXL must not appear in short candidates."""
    from short_strategy import identify_short_candidates
    picks = [_strong_short_pick("SOXL")]
    candidates = identify_short_candidates(picks)
    assert candidates == [], (
        "Pt.35: SOXL must NEVER be selected as a short candidate "
        "regardless of signal strength.")


def test_inverse_etf_blocked_from_short_candidates():
    """Shorting SOXS = going long semis. Always wrong on signal
    alignment."""
    from short_strategy import identify_short_candidates
    picks = [_strong_short_pick("SOXS")]
    assert identify_short_candidates(picks) == []


def test_volatility_etf_blocked_from_short_candidates():
    from short_strategy import identify_short_candidates
    picks = [_strong_short_pick("VXX"), _strong_short_pick("UVXY")]
    assert identify_short_candidates(picks) == []


def test_normal_stock_still_passes_through():
    """Regression pin: the filter must not reject normal stocks.
    A strong bearish signal on AAPL/AMD must still produce a
    candidate."""
    from short_strategy import identify_short_candidates
    picks = [_strong_short_pick("AMD", momentum=-20, daily=-5)]
    candidates = identify_short_candidates(picks)
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "AMD"


def test_mixed_picks_filter_only_drops_leveraged():
    """When the screener delivers a mix of leveraged ETFs + normal
    stocks, only the leveraged ones get dropped."""
    from short_strategy import identify_short_candidates
    picks = [
        _strong_short_pick("SOXL"),   # blocked
        _strong_short_pick("AMD"),    # passes
        _strong_short_pick("TQQQ"),   # blocked
        _strong_short_pick("INTC"),   # passes
        _strong_short_pick("SQQQ"),   # blocked (inverse)
    ]
    candidates = identify_short_candidates(picks)
    symbols = [c["symbol"] for c in candidates]
    assert "SOXL" not in symbols
    assert "TQQQ" not in symbols
    assert "SQQQ" not in symbols
    assert "AMD" in symbols
    assert "INTC" in symbols


def test_filter_logs_blocked_symbols():
    """Operator visibility: the filter must surface blocked symbols
    in the log output so the user can see WHY a leveraged ETF
    didn't show up in the short-candidates list. Use io.StringIO
    redirect rather than pytest's capsys fixture — capsys interacts
    badly with pytest-cov in some CI environments."""
    import io, contextlib
    from short_strategy import identify_short_candidates
    picks = [
        _strong_short_pick("SOXL"),
        _strong_short_pick("SQQQ"),
        _strong_short_pick("AMD"),
    ]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        identify_short_candidates(picks)
    out = buf.getvalue()
    assert "SOXL" in out
    assert "SQQQ" in out
    assert "Skipped leveraged" in out or "leveraged" in out.lower()


def test_filter_silent_when_no_blocked_picks():
    """If all picks are normal stocks, the filter should NOT emit a
    log line — quiet operation when nothing is filtered."""
    import io, contextlib
    from short_strategy import identify_short_candidates
    picks = [_strong_short_pick("AMD"), _strong_short_pick("INTC")]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        identify_short_candidates(picks)
    out = buf.getvalue()
    assert "Skipped leveraged" not in out


def test_filter_runs_BEFORE_score_check():
    """The filter must short-circuit before any signal/score logic
    runs — even a leveraged ETF with a weak score (would fail the
    minimum threshold anyway) should be filtered, not slipped
    through to the threshold check."""
    from short_strategy import identify_short_candidates
    # Weak signal — would fail score>=10 check on its own
    weak_pick = {
        "symbol": "SOXL", "price": 100,
        "momentum_20d": -6,  # barely bearish
        "daily_change": -1,
        "rsi": 50, "macd_histogram": 0,
        "overall_bias": "neutral", "news_sentiment": "neutral",
        "relative_volume": 1.0,
    }
    candidates = identify_short_candidates([weak_pick])
    assert candidates == []


# ============================================================================
# Source-pin
# ============================================================================

def test_short_strategy_imports_blocklist_from_constants():
    """Pin that short_strategy.py uses the canonical constants list,
    not a private copy. Round-61 pt.21 SSOT rule."""
    import pathlib
    src = pathlib.Path("short_strategy.py").read_text()
    assert "is_leveraged_or_inverse_etf" in src
    assert "constants" in src
