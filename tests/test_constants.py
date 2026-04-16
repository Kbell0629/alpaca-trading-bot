"""SECTOR_MAP consolidation + constants integrity."""


def test_sector_map_single_source(isolated_data_dir):
    """All three consumers point to the same SECTOR_MAP dict — not copies."""
    import constants
    import update_dashboard
    import update_scorecard
    assert update_dashboard.SECTOR_MAP is constants.SECTOR_MAP, \
        "update_dashboard.SECTOR_MAP is a separate copy (divergence risk)"
    assert update_scorecard.SECTOR_MAP is constants.SECTOR_MAP, \
        "update_scorecard.SECTOR_MAP is a separate copy"


def test_sector_map_has_expected_sectors(isolated_data_dir):
    import constants
    sectors = set(constants.SECTOR_MAP.values())
    expected = {"Tech", "Consumer", "Finance", "Healthcare", "Energy", "Industrial"}
    assert expected.issubset(sectors)


def test_profit_ladder_schedule(isolated_data_dir):
    import constants
    assert len(constants.PROFIT_LADDER) == 4
    gains = [r["gain_pct"] for r in constants.PROFIT_LADDER]
    assert gains == sorted(gains), "ladder must be monotonically increasing"
    # 4 × 25% = 100% — the full original position gets scaled out
    total = sum(r["sell_pct"] for r in constants.PROFIT_LADDER)
    assert total == 100


def test_earnings_pattern_matches(isolated_data_dir):
    import constants
    assert constants.EARNINGS_PATTERN.search("Q3 earnings report tomorrow")
    assert constants.EARNINGS_PATTERN.search("revenue report due Friday")
    assert not constants.EARNINGS_PATTERN.search("just had a meeting")
    assert constants.Q_PATTERN.search("Q1 results")
    assert constants.Q_PATTERN.search("fiscal Q4 guidance")
    assert not constants.Q_PATTERN.search("just some text")
