"""Round-61 pt.64 — daily backups + dead-money notification +
risk-parity allocator + aggressive trailing tier 5.

Item A — daily backups: verify the backup INCLUDES list captures
the per-user state files (`learned_params.json`, `picks_history.json`)
through the recursive `users/` include.

Item B — dead-money notification template.

Item C — risk-parity weight allocator.

Item D — _compute_stepped_stop tier 5 (aggressive 3% trail at +30%).
"""
from __future__ import annotations


# ============================================================================
# Item A: daily backup includes per-user state
# ============================================================================

def test_backup_includes_users_directory():
    """The recursive `users/` include captures every per-user JSON
    (learned_params, picks_history, journal, scorecard, etc.)."""
    import backup
    assert "users/" in backup.INCLUDES


def test_backup_includes_users_db():
    import backup
    assert "users.db" in backup.INCLUDES


def test_backup_includes_scheduler_last_runs():
    """scheduler_last_runs.json must be in the includes so a restore
    doesn't re-fire daily tasks that already completed today."""
    import backup
    assert "scheduler_last_runs.json" in backup.INCLUDES


# ============================================================================
# Item B: dead-money exit notification
# ============================================================================

def test_dead_money_exit_returns_subj_and_body():
    import notification_templates as nt
    subj, body = nt.dead_money_exit(
        symbol="AAPL", strategy="breakout",
        entry_price=100.0, exit_price=101.0,
        shares=10, pnl=10.0, pnl_pct=1.0,
        hold_days=12,
    )
    assert isinstance(subj, str)
    assert isinstance(body, str)
    assert "AAPL" in subj
    assert "Dead-money" in subj or "dead-money" in subj.lower()


def test_dead_money_exit_subject_includes_held_days():
    import notification_templates as nt
    subj, _body = nt.dead_money_exit(
        symbol="MSFT", strategy="breakout",
        entry_price=200.0, exit_price=201.0,
        shares=5, pnl=5.0, pnl_pct=0.5,
        hold_days=15,
    )
    assert "15d" in subj or "15 days" in subj.lower()


def test_dead_money_exit_body_explains_rationale():
    import notification_templates as nt
    _subj, body = nt.dead_money_exit(
        symbol="X", strategy="breakout",
        entry_price=100.0, exit_price=100.5,
        shares=10, pnl=5.0, pnl_pct=0.5,
        hold_days=10,
    )
    # Should explain WHY we closed, not just THAT we closed.
    assert "stagnant" in body.lower() or "stopped doing" in body.lower()
    assert "WHAT JUST HAPPENED" in body
    assert "WHAT HAPPENS NEXT" in body


def test_dead_money_exit_body_mentions_redeploy():
    """User wants to know what happens to the freed cash."""
    import notification_templates as nt
    _subj, body = nt.dead_money_exit(
        symbol="X", strategy="breakout",
        entry_price=100.0, exit_price=99.0,
        shares=10, pnl=-10.0, pnl_pct=-1.0,
        hold_days=12,
    )
    assert "auto-deployer" in body.lower() or "redeploy" in body.lower()


def test_dead_money_exit_is_not_a_stop_message():
    """The body should explicitly distinguish from stop-loss."""
    import notification_templates as nt
    _subj, body = nt.dead_money_exit(
        symbol="X", strategy="breakout",
        entry_price=100.0, exit_price=100.2,
        shares=10, pnl=2.0, pnl_pct=0.2,
        hold_days=11,
    )
    assert "NOT a stop-loss" in body or "not a stop" in body.lower()


def test_cloud_scheduler_uses_dead_money_template():
    """Source pin: the dead-money exit block in process_strategy_file
    should call notification_templates.dead_money_exit, not just
    notify_user with a plain string."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
            / "cloud_scheduler.py").read_text()
    idx = src.find("dead-money cutter")
    assert idx > 0
    block = src[idx:idx + 3000]
    assert "dead_money_exit" in block
    assert "notify_rich" in block


# ============================================================================
# Item C: risk-parity weights
# ============================================================================

def test_compute_strategy_volatility_no_journal():
    import risk_parity as rp
    assert rp.compute_strategy_volatility(None) == {}


def test_compute_strategy_volatility_filters_unknown_strategies():
    import risk_parity as rp
    journal = {"trades": [
        {"status": "closed", "strategy": "weird_one", "pnl": 5.0}
    ]}
    out = rp.compute_strategy_volatility(journal)
    assert "weird_one" not in out


def test_compute_strategy_volatility_groups_per_strategy():
    import risk_parity as rp
    journal = {"trades": [
        {"status": "closed", "strategy": "breakout", "pnl": 10},
        {"status": "closed", "strategy": "breakout", "pnl": -5},
        {"status": "closed", "strategy": "wheel", "pnl": 3},
    ]}
    out = rp.compute_strategy_volatility(journal)
    assert out["breakout"]["trade_count"] == 2
    assert out["wheel"]["trade_count"] == 1


def test_compute_strategy_volatility_skips_open_trades():
    import risk_parity as rp
    journal = {"trades": [
        {"status": "open", "strategy": "breakout", "pnl": None},
    ]}
    out = rp.compute_strategy_volatility(journal)
    assert out == {}


def test_risk_parity_weights_equal_when_no_data():
    """Empty journal → equal weights across all strategies."""
    import risk_parity as rp
    weights = rp.compute_risk_parity_weights({}, strategies=("a", "b", "c"))
    assert all(abs(w - 0.3333) < 0.01 for w in weights.values())
    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_risk_parity_weights_inverse_to_volatility():
    """Strategy with higher σ gets LESS weight."""
    import risk_parity as rp
    # breakout: pnls swing wildly (high σ); wheel: tight (low σ).
    journal = {"trades": [
        # 5 wide-swing breakout trades
        {"status": "closed", "strategy": "breakout", "pnl": p}
        for p in [50, -40, 30, -25, 20]
    ] + [
        # 5 tight wheel trades
        {"status": "closed", "strategy": "wheel", "pnl": p}
        for p in [3, 2, 4, 3, 2]
    ]}
    weights = rp.compute_risk_parity_weights(
        journal, strategies=("breakout", "wheel"))
    # wheel should get much more weight (lower σ → higher 1/σ)
    assert weights["wheel"] > weights["breakout"]
    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_risk_parity_weights_sum_to_one():
    import risk_parity as rp
    weights = rp.compute_risk_parity_weights(
        {"trades": []},
        strategies=("breakout", "wheel", "mean_reversion"))
    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_risk_parity_weights_strategy_below_min_trades_uses_fallback():
    """A strategy with only 2 trades shouldn't dominate weights —
    the fallback_vol levels the playing field."""
    import risk_parity as rp
    trades = [
        # 2 wheel trades (below MIN_TRADES_FOR_PARITY=5).
        {"status": "closed", "strategy": "wheel", "pnl": 3},
        {"status": "closed", "strategy": "wheel", "pnl": 2},
    ]
    # 5 breakout trades.
    for p in [10, -5, 8, -3, 4]:
        trades.append(
            {"status": "closed", "strategy": "breakout", "pnl": p})
    journal = {"trades": trades}
    weights = rp.compute_risk_parity_weights(
        journal, strategies=("breakout", "wheel"))
    # Both should have non-zero weight even though wheel's sample is small.
    assert weights["wheel"] > 0
    assert weights["breakout"] > 0


def test_explain_weights_human_readable():
    import risk_parity as rp
    s = rp.explain_weights({"breakout": 0.3, "wheel": 0.7})
    assert "breakout" in s
    assert "wheel" in s
    assert "%" in s
    # Higher-weight strategy listed first.
    assert s.index("wheel") < s.index("breakout")


def test_explain_weights_empty():
    import risk_parity as rp
    assert rp.explain_weights({}) == "(no weights computed)"


# ============================================================================
# Item D: aggressive tier 5 trailing at +30% profit
# ============================================================================

def test_stepped_stop_tier_5_at_30_pct_long():
    """Long with 30%+ profit → Tier 5 (3% trail)."""
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        100.0, 135.0, 0.05, is_short=False)
    assert tier == 5
    assert trail == 0.03
    # 135 * 0.97 = 130.95
    assert abs(stop - 130.95) < 0.01


def test_stepped_stop_tier_5_at_30_pct_short():
    """Short with 30%+ profit (price dropped 30%+) → Tier 5."""
    import cloud_scheduler as cs
    stop, tier, trail = cs._compute_stepped_stop(
        100.0, 65.0, 0.05, is_short=True)
    assert tier == 5
    assert trail == 0.03
    # 65 * 1.03 = 66.95
    assert abs(stop - 66.95) < 0.01


def test_stepped_stop_at_30_pct_boundary_is_tier_5():
    """Exactly 30% profit → Tier 5."""
    import cloud_scheduler as cs
    _stop, tier, _ = cs._compute_stepped_stop(
        100.0, 130.0, 0.05, is_short=False)
    assert tier == 5


def test_stepped_stop_just_below_30_is_tier_4():
    """29.5% profit stays in Tier 4."""
    import cloud_scheduler as cs
    _stop, tier, _ = cs._compute_stepped_stop(
        100.0, 129.0, 0.05, is_short=False)
    assert tier == 4


def test_stepped_stop_huge_profit_capped_at_tier_5():
    """100%+ profit also returns tier 5 (no tier 6)."""
    import cloud_scheduler as cs
    _stop, tier, trail = cs._compute_stepped_stop(
        100.0, 250.0, 0.05, is_short=False)
    assert tier == 5
    assert trail == 0.03
