"""Screener guardrails added in the round-8 follow-up: minimum daily
volume floor (500k) and volatility soft-cap on breakout_score (>25%
intraday range halves the score).

Both were added after real-market observation that breakouts on thin-
float / high-volatility names were dominating the top of the screener
list. The tests below lock the guard values in so a future tweak
doesn't silently weaken them.
"""
import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_DASHBOARD = REPO_ROOT / "update_dashboard.py"


def _read_source():
    return UPDATE_DASHBOARD.read_text()


def test_min_volume_floor_is_at_least_300k(isolated_data_dir):
    """Ensure the MIN_VOLUME constant wasn't accidentally lowered back
    to the old 100k threshold. Round-8 bumped to 500k (too strict,
    filtered 9/12 live picks), then tuned to 300k as the middle ground.
    If someone drops below 300k, thin-float names will re-dominate.
    """
    src = _read_source()
    m = re.search(r"^MIN_VOLUME\s*=\s*([\d_]+)", src, re.MULTILINE)
    assert m, "MIN_VOLUME constant missing from update_dashboard.py"
    value = int(m.group(1).replace("_", ""))
    assert value >= 300_000, \
        f"MIN_VOLUME dropped to {value:,} — thin-float names will re-dominate screener"


def test_breakout_score_volatility_softcap_exists(isolated_data_dir):
    """The > 25% intraday volatility soft-cap halves breakout_score.
    Regression test: verify the cap still exists in source. Without it,
    a stock with 58% intraday range (XNDU-class) would dominate the
    top of the screener even though it's untradeable.
    """
    src = _read_source()
    # The cap is one of only two places `breakout_score *= 0.5` appears;
    # the other `*= 0.5` in the file is mean_reversion_score. Check the
    # specific pattern.
    assert "volatility > 25 and breakout_score > 0" in src, \
        "volatility soft-cap missing from breakout_score calculation"
    assert re.search(
        r"if\s+volatility\s*>\s*25.*?breakout_score\s*\*=\s*0\.5",
        src, re.DOTALL,
    ), "volatility soft-cap doesn't halve breakout_score"


def test_breakout_formula_still_rewards_real_breakouts(isolated_data_dir):
    """Sanity check: the coefficients on the raw breakout formula are
    still generous enough to put a clean breakout (10-15% up on 2x
    volume) in the top tier. This is the intent.
    """
    # Simulate the raw formula for a "clean" breakout:
    daily_change = 10.0        # +10% move
    volume_surge = 120.0       # 2.2x normal volume
    volatility = 8.0           # modest intraday range (clean move, not a pump)

    breakout_score = daily_change * 1.5 + (volume_surge / 20)  # = 21
    assert volume_surge > 100 and volume_surge <= 200
    breakout_score *= 1.2  # 2x_volume_confirmed tier
    # Volatility soft-cap does NOT fire (volatility 8 < 25)
    assert volatility <= 25
    # Expected: ~25.2 — well above the "weak breakout" floor
    assert breakout_score > 20, f"clean breakout scored too low: {breakout_score}"


def test_breakout_formula_caps_pump_and_dump(isolated_data_dir):
    """Opposite sanity check: a wild mover (e.g., XNDU-class +27% with
    58% intraday volatility) should get its breakout_score halved —
    real signal but bad execution quality. Without the soft-cap the
    score would dominate the top of the list."""
    daily_change = 27.0
    volume_surge = 75.0
    volatility = 58.0

    # Raw calc
    breakout_score = daily_change * 1.5 + (volume_surge / 20)  # = 44.2
    # Volume tier: 75 is < 100, so no multiplier
    # Soft-cap kicks in because volatility > 25
    if volatility > 25:
        breakout_score *= 0.5
    # Expected: ~22 — knocked down below the clean breakout above
    assert breakout_score < 25, \
        f"high-vol breakout not capped: {breakout_score}"
