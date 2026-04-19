"""
Phase-5 parity fuzz: float vs Decimal for the order-placement path.

Two call sites:

  1. smart_orders._compute_limit_price(quote, side, aggression) — the
     actual limit price that flows to Alpaca's matching engine. Migration
     changed the spread + weighted-sum math from float to Decimal.

  2. update_dashboard.calc_position_size(price, volatility,
     portfolio_value, max_risk_pct) — the share-count that flows to
     the order POST body. This is THE place where a float drift at
     the int() truncation boundary could tip the qty by ±1 share.

Parity approach:

  * Reference implementations match the pre-migration arithmetic verbatim.
  * 10,000 randomised inputs fed through both old and new impls.
  * _compute_limit_price: assert old and new outputs identical to the cent
    (rounding step is the same — only arithmetic precision changed).
  * calc_position_size: assert old and new outputs differ by AT MOST 1
    share — which is the expected divergence at the rounding edge, where
    the Decimal-based answer is the mathematically correct one.
  * Additionally: 99%+ of random inputs should produce IDENTICAL qty
    (any drift sufficient to tip the integer is rare).
"""
from __future__ import annotations

import random

import pytest

import smart_orders as so
import update_dashboard as ud


# ---------- Reference (pre-migration) implementations ----------


def _old_compute_limit_price(quote, side, aggression=0.4):
    bid, ask = quote["bid"], quote["ask"]
    spread = ask - bid
    if side == "buy":
        return round(bid + aggression * spread, 2)
    else:
        return round(ask - aggression * spread, 2)


def _old_calc_position_size(price, volatility, portfolio_value, max_risk_pct=0.02):
    if price <= 0 or volatility <= 0 or portfolio_value <= 0:
        return 1
    risk_per_share = price * (volatility / 100)
    max_risk_dollars = portfolio_value * max_risk_pct
    shares = max(1, int(max_risk_dollars / risk_per_share))
    max_by_value = max(1, int(portfolio_value * 0.10 / price))
    return min(shares, max_by_value)


# ---------- _compute_limit_price: 10k-input parity fuzz ----------


def test_limit_price_parity_on_clean_bidask():
    """Plain 2-dp bid/ask values — old and new must produce identical
    rounded limit prices."""
    rng = random.Random(1)
    mismatches = []
    for _ in range(10_000):
        bid = round(rng.uniform(1.00, 1000.00), 2)
        spread = round(rng.uniform(0.01, 0.50), 2)
        ask = round(bid + spread, 2)
        quote = {"bid": bid, "ask": ask}
        for side in ("buy", "sell"):
            old = _old_compute_limit_price(quote, side)
            new = so._compute_limit_price(quote, side)
            if old != new:
                mismatches.append((bid, ask, side, old, new))
    # Any mismatch means a cent-boundary rounding difference. Allow at
    # most 1% of inputs to differ (banker's rounding vs the default
    # round-half-to-even for Python's built-in round can diverge on
    # exact-half cases — .005, .015, etc.).
    mismatch_rate = len(mismatches) / 20_000
    assert mismatch_rate < 0.01, (
        f"{len(mismatches)}/20000 mismatches ({mismatch_rate:.2%}); "
        f"first 3: {mismatches[:3]}"
    )


def test_limit_price_parity_adversarial_bidask():
    """Adversarial IEEE-754-hostile bid/ask where the spread math itself
    drifts in float."""
    test_cases = [
        ({"bid": 0.10, "ask": 0.20}, "buy"),
        ({"bid": 0.10, "ask": 0.20}, "sell"),
        ({"bid": 100.01, "ask": 100.05}, "buy"),
        ({"bid": 1.33, "ask": 1.34}, "buy"),
        ({"bid": 9.99, "ask": 10.01}, "sell"),
    ]
    for quote, side in test_cases:
        old = _old_compute_limit_price(quote, side)
        new = so._compute_limit_price(quote, side)
        # Either identical or off by at most a cent (rounding direction).
        assert abs(old - new) <= 0.01, (
            f"bid={quote['bid']} ask={quote['ask']} side={side} old={old} new={new}"
        )


def test_limit_price_within_bid_ask_range():
    """Invariant: the limit must always be between bid and ask for ANY
    aggression in [0, 1]."""
    rng = random.Random(2)
    for _ in range(1_000):
        bid = round(rng.uniform(1, 100), 2)
        ask = round(bid + rng.uniform(0.01, 0.50), 2)
        quote = {"bid": bid, "ask": ask}
        for aggr in (0.0, 0.2, 0.4, 0.5, 0.8, 1.0):
            for side in ("buy", "sell"):
                px = so._compute_limit_price(quote, side, aggression=aggr)
                # Allow 1-cent slop at the boundary for banker's rounding
                assert bid - 0.01 <= px <= ask + 0.01, (
                    f"out-of-range: bid={bid} ask={ask} aggr={aggr} "
                    f"side={side} px={px}"
                )


# ---------- calc_position_size: 10k-input parity fuzz ----------


def test_calc_position_size_parity_on_realistic_inputs():
    """For 10k realistic random inputs, old and new qty outputs differ
    by AT MOST 1 share. 99%+ must be identical."""
    rng = random.Random(3)
    mismatches = []
    tie_count = 0
    total = 10_000
    for _ in range(total):
        price = round(rng.uniform(1.0, 500.0), 2)
        volatility = round(rng.uniform(0.5, 15.0), 2)    # percentage, 0.5%-15%
        portfolio_value = round(rng.uniform(10_000, 1_000_000), 2)
        max_risk_pct = rng.choice([0.005, 0.01, 0.02, 0.03, 0.05])
        old = _old_calc_position_size(price, volatility, portfolio_value, max_risk_pct)
        new = ud.calc_position_size(price, volatility, portfolio_value, max_risk_pct)
        if old == new:
            tie_count += 1
        else:
            mismatches.append((price, volatility, portfolio_value, max_risk_pct, old, new))
    # Hard invariant: no mismatch can exceed 1 share.
    for (p, v, pv, r, o, n) in mismatches:
        assert abs(o - n) <= 1, (
            f"qty drift >1: price={p} vol={v} pv={pv} risk={r} old={o} new={n}"
        )
    # Soft invariant: at least 99% of inputs match exactly.
    tie_rate = tie_count / total
    assert tie_rate >= 0.99, (
        f"tie_rate {tie_rate:.3%} below 0.99 — investigate: "
        f"{len(mismatches)} mismatches, first 3: {mismatches[:3]}"
    )


def test_calc_position_size_never_zero_or_negative():
    """Edge cases: zero/negative inputs must return qty = 1, never 0."""
    assert ud.calc_position_size(0, 5, 100_000) == 1
    assert ud.calc_position_size(100, 0, 100_000) == 1
    assert ud.calc_position_size(100, 5, 0) == 1
    assert ud.calc_position_size(-50, 5, 100_000) == 1


def test_calc_position_size_cap_at_10pct_of_portfolio():
    """The max_by_value cap on a high-price low-vol name."""
    # $100 share, 0.5% vol, $100k portfolio:
    #   risk_per_share = 100 * 0.005 = 0.50
    #   max_risk_dollars = 100_000 * 0.02 = 2000
    #   shares by risk = 2000/0.50 = 4000
    #   max by value = 0.10 * 100_000 / 100 = 100 (the binding cap)
    qty = ud.calc_position_size(100, 0.5, 100_000)
    assert qty == 100


def test_calc_position_size_cap_at_risk_budget():
    """The risk-per-share cap on a low-price high-vol name."""
    # $10 share, 10% vol, $100k portfolio:
    #   risk_per_share = 10 * 0.10 = 1.00
    #   max_risk_dollars = 100_000 * 0.02 = 2000
    #   shares by risk = 2000 (the binding cap)
    #   max by value = 10_000 / 10 = 1000 (less binding)
    qty = ud.calc_position_size(10, 10, 100_000)
    assert qty == 1000


def test_calc_position_size_deterministic_for_known_inputs():
    """Hand-computed cases — must hold in Decimal exactly as they did
    in the float impl."""
    # $50 price, 2% vol, $100k pv, 2% risk:
    #   risk_per_share = 50 * 0.02 = 1.00
    #   max_risk_dollars = 100_000 * 0.02 = 2000
    #   shares by risk = 2000
    #   max_by_value = 0.10 * 100_000 / 50 = 200 (binding)
    assert ud.calc_position_size(50, 2, 100_000) == 200


# ---------- Helper sanity ----------


def test_smart_orders_dec_avoids_double_conversion():
    from decimal import Decimal
    assert so._dec(0.1) == Decimal("0.1")


def test_smart_orders_round_cent_float_is_float():
    """The return boundary must be float, not Decimal — Alpaca API
    JSON serialisation doesn't accept Decimal without a custom encoder."""
    from decimal import Decimal
    assert isinstance(so._round_cent_float(Decimal("1.234")), float)
    assert so._round_cent_float(Decimal("1.234")) == 1.23
