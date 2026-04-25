"""Round-61 pt.18 — professional stepped trailing stop.

User request ("would like this to be a professional system"): replace
the flat `highest * (1 - trail)` trailing stop with a tier-aware
version that matches how institutional trend-following systems size
stops:

    Tier 1 (0 to +5%):   wide default trail (e.g. 8%) — breathing
                         room for the breakout retest.
    Tier 2 (+5 to +10%): STOP LOCKED TO ENTRY — no-loss guarantee.
    Tier 3 (+10 to +20%): 6% trail — lock in some gain.
    Tier 4 (+20%+):       4% trail — ride the big move tight.

This file pins:
  1. `_compute_stepped_stop(entry, extreme, default_trail, is_short)`
     — single source of truth for what the stop should be at any
     given profit level.
  2. Long trailing path in process_strategy_file uses the helper when
     rules.stepped_trail != False (default True).
  3. Short trailing path in process_short_strategy uses the same
     helper, inverting direction.
  4. state["profit_tier"] + state["break_even_triggered"] are tracked
     for audit + user notification on tier transitions.
"""
from __future__ import annotations


def _src(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------------
# _compute_stepped_stop — unit tests for each tier
# ----------------------------------------------------------------------------

def test_long_tier1_flat_trail_at_entry():
    import cloud_scheduler as cs
    # Position just entered, extreme = entry → profit 0% → Tier 1.
    new_stop, tier, trail = cs._compute_stepped_stop(
        entry=100.0, extreme_price=100.0, default_trail=0.08, is_short=False,
    )
    assert tier == 1
    assert trail == 0.08
    assert new_stop == 92.0  # 100 × 0.92


def test_long_tier1_at_plus_4_percent():
    import cloud_scheduler as cs
    # Highest 104 → profit 4% → still Tier 1.
    new_stop, tier, _ = cs._compute_stepped_stop(
        entry=100.0, extreme_price=104.0, default_trail=0.08, is_short=False,
    )
    assert tier == 1
    assert new_stop == 95.68  # 104 × 0.92


def test_long_tier2_break_even_at_plus_5():
    """The moment profit hits +5%, stop jumps to ENTRY (break-even
    guarantee). Does NOT use the default trail."""
    import cloud_scheduler as cs
    new_stop, tier, trail = cs._compute_stepped_stop(
        entry=100.0, extreme_price=105.0, default_trail=0.08, is_short=False,
    )
    assert tier == 2
    assert trail is None, "Tier 2 has no trail distance — stop is at entry"
    assert new_stop == 100.0


def test_long_tier2_break_even_at_plus_9_percent():
    import cloud_scheduler as cs
    new_stop, tier, _ = cs._compute_stepped_stop(
        entry=100.0, extreme_price=109.0, default_trail=0.08, is_short=False,
    )
    assert tier == 2
    assert new_stop == 100.0


def test_long_tier3_6pct_at_plus_10_percent():
    """At +10% profit, switch to 6% trail below highest."""
    import cloud_scheduler as cs
    new_stop, tier, trail = cs._compute_stepped_stop(
        entry=100.0, extreme_price=110.0, default_trail=0.08, is_short=False,
    )
    assert tier == 3
    assert trail == 0.06
    assert new_stop == 103.4  # 110 × 0.94


def test_long_tier4_4pct_at_plus_20_percent():
    """At +20%, tighten to 4% trail."""
    import cloud_scheduler as cs
    new_stop, tier, trail = cs._compute_stepped_stop(
        entry=100.0, extreme_price=120.0, default_trail=0.08, is_short=False,
    )
    assert tier == 4
    assert trail == 0.04
    assert new_stop == 115.2  # 120 × 0.96


def test_long_tier5_at_plus_50_percent():
    """Round-61 pt.64 added Tier 5 at +30% profit (3% trail). At +50%
    profit we're firmly in Tier 5 territory, not Tier 4."""
    import cloud_scheduler as cs
    new_stop, tier, _ = cs._compute_stepped_stop(
        entry=100.0, extreme_price=150.0, default_trail=0.08, is_short=False,
    )
    assert tier == 5
    assert new_stop == 145.5  # 150 × 0.97 (3% Tier-5 trail)


def test_stops_monotonically_increase_with_profit_long():
    """Sanity: as highest climbs, the stop price should never
    decrease. Any tier boundary that violates this would give back
    protection on the way up."""
    import cloud_scheduler as cs
    entry = 100.0
    prev_stop = -1
    for pct in range(0, 51):  # 0% to 50% in 1% steps
        highest = entry * (1 + pct / 100)
        stop, _, _ = cs._compute_stepped_stop(
            entry=entry, extreme_price=highest, default_trail=0.08, is_short=False,
        )
        assert stop >= prev_stop, (
            f"Stop went DOWN at profit={pct}%: {prev_stop} -> {stop}")
        prev_stop = stop


# ----------------------------------------------------------------------------
# Short-side mirror tests
# ----------------------------------------------------------------------------

def test_short_tier1_above_lowest():
    """Short just entered — stop is default_trail ABOVE lowest (entry)."""
    import cloud_scheduler as cs
    new_stop, tier, trail = cs._compute_stepped_stop(
        entry=100.0, extreme_price=100.0, default_trail=0.05, is_short=True,
    )
    assert tier == 1
    assert trail == 0.05
    assert new_stop == 105.0  # 100 × 1.05


def test_short_tier2_break_even_when_profit_5pct():
    """Short profit +5% = price dropped to 95. Stop jumps to entry
    (100) — covers at break-even if price recovers."""
    import cloud_scheduler as cs
    new_stop, tier, _ = cs._compute_stepped_stop(
        entry=100.0, extreme_price=95.0, default_trail=0.05, is_short=True,
    )
    assert tier == 2
    assert new_stop == 100.0


def test_short_tier3_at_plus_10_pct_profit():
    """Short profit +10% = price 90. 6% trail above = 90 × 1.06 = 95.4."""
    import cloud_scheduler as cs
    new_stop, tier, trail = cs._compute_stepped_stop(
        entry=100.0, extreme_price=90.0, default_trail=0.05, is_short=True,
    )
    assert tier == 3
    assert trail == 0.06
    assert new_stop == 95.4


def test_short_tier4_at_plus_20_pct_profit():
    import cloud_scheduler as cs
    new_stop, tier, trail = cs._compute_stepped_stop(
        entry=100.0, extreme_price=80.0, default_trail=0.05, is_short=True,
    )
    assert tier == 4
    assert trail == 0.04
    assert new_stop == 83.2  # 80 × 1.04


def test_short_stops_monotonically_decrease_with_profit():
    """For a short, stop moves DOWN as price drops (lowest drops).
    Never increase."""
    import cloud_scheduler as cs
    entry = 100.0
    prev_stop = 99999
    for pct in range(0, 51):
        lowest = entry * (1 - pct / 100)
        stop, _, _ = cs._compute_stepped_stop(
            entry=entry, extreme_price=lowest, default_trail=0.05, is_short=True,
        )
        assert stop <= prev_stop, (
            f"Short stop went UP at profit={pct}%: {prev_stop} -> {stop}")
        prev_stop = stop


# ----------------------------------------------------------------------------
# Defensive: bad inputs
# ----------------------------------------------------------------------------

def test_compute_stepped_stop_handles_zero_entry():
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        entry=0.0, extreme_price=100.0, default_trail=0.08, is_short=False,
    )
    # Falls back to flat trail off extreme (still places SOMETHING).
    assert tier == 1
    assert stop == 92.0


def test_compute_stepped_stop_handles_none_entry():
    import cloud_scheduler as cs
    stop, tier, _ = cs._compute_stepped_stop(
        entry=None, extreme_price=100.0, default_trail=0.08, is_short=False,
    )
    assert tier == 1


# ----------------------------------------------------------------------------
# Source pins — the helper must be used in the trailing paths
# ----------------------------------------------------------------------------

def test_long_path_uses_compute_stepped_stop():
    src = _src("cloud_scheduler.py")
    # The long trailing path is inside process_strategy_file — the
    # call to _compute_stepped_stop must appear in the file AND must
    # be in the long branch (not only the short one).
    assert "_compute_stepped_stop" in src
    # Must be used in both paths.
    occurrences = src.count("_compute_stepped_stop(")
    # Definition (1) + long path (1) + short path (1) = 3 minimum
    assert occurrences >= 3, (
        f"_compute_stepped_stop must be called from both long + short "
        f"trailing paths, got {occurrences} occurrences.")


def test_stepped_trail_opt_out_exists():
    src = _src("cloud_scheduler.py")
    assert 'rules.get("stepped_trail", True)' in src, (
        "Strategy files must be able to opt out via "
        "rules.stepped_trail=false. Default True — users get the "
        "professional behaviour without explicit enablement.")


def test_break_even_triggered_flag_tracked():
    """State must record break_even_triggered so the dashboard /
    scorecard can distinguish 'stopped at entry' (no-loss) from
    'stopped below entry' (real loss)."""
    src = _src("cloud_scheduler.py")
    assert 'break_even_triggered' in src
    assert 'state["profit_tier"]' in src, (
        "profit_tier must be persisted so tier transitions don't "
        "re-fire notifications on every 60s monitor tick.")


def test_tier_transition_notifies_user():
    src = _src("cloud_scheduler.py")
    assert "BREAK-EVEN LOCKED" in src, (
        "Tier 2 transition must surface via notify_user so the user "
        "sees the break-even lock event in their notification feed.")


# ----------------------------------------------------------------------------
# Realistic scenario — CRDO case the user flagged
# ----------------------------------------------------------------------------

def test_crdo_scenario_current_state_still_tier1():
    """CRDO right now: entry $189.15, highest (≈current) $197.74,
    profit +4.55%. Still Tier 1 — stop = highest × 0.92 = $181.92."""
    import cloud_scheduler as cs
    new_stop, tier, _ = cs._compute_stepped_stop(
        entry=189.15, extreme_price=197.74, default_trail=0.08, is_short=False,
    )
    assert tier == 1
    assert 181 < new_stop < 182  # matches user's observed $181.72


def test_crdo_scenario_at_205_break_even_locked():
    """If CRDO climbs to $205 (= +8.38% from entry), stop should
    JUMP to $189.15 (break-even). Previously at flat 8% it would be
    $188.60 (close to break-even but not guaranteed)."""
    import cloud_scheduler as cs
    new_stop, tier, _ = cs._compute_stepped_stop(
        entry=189.15, extreme_price=205.0, default_trail=0.08, is_short=False,
    )
    assert tier == 2
    assert new_stop == 189.15


def test_crdo_scenario_at_220_tier3():
    """$220 = +16.3% profit → Tier 3, 6% trail. Stop = 220 × 0.94."""
    import cloud_scheduler as cs
    new_stop, tier, _ = cs._compute_stepped_stop(
        entry=189.15, extreme_price=220.0, default_trail=0.08, is_short=False,
    )
    assert tier == 3
    assert new_stop == round(220.0 * 0.94, 2)  # 206.80


def test_crdo_scenario_at_230_tier4():
    """$230 = +21.6% profit → Tier 4, 4% trail."""
    import cloud_scheduler as cs
    new_stop, tier, _ = cs._compute_stepped_stop(
        entry=189.15, extreme_price=230.0, default_trail=0.08, is_short=False,
    )
    assert tier == 4
    assert new_stop == round(230.0 * 0.96, 2)  # 220.80
