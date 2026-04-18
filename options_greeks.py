#!/usr/bin/env python3
"""
options_greeks.py — Black-Scholes deltas for wheel-strike selection.

Round-11 Tier 3 addition. Replaces the wheel strategy's heuristic
"10% OTM" put-strike selection with proper delta targeting — the
industry-standard approach for option premium sellers.

Target delta range for cash-secured puts: 0.20-0.30 (20-30% probability
of assignment, rich premium, reasonable strike distance). Higher delta
= more premium but more assignment risk; lower delta = safer but too
little premium to bother.

Uses stdlib-only Black-Scholes (math.erf for N()), no scipy dependency.

Public API:

    put_delta(S, K, T, sigma, r=0.04) -> float
        Black-Scholes put delta. Negative (-0.25 means 25% assignment
        probability). We return abs value for easier comparison.

    call_delta(S, K, T, sigma, r=0.04) -> float
        Positive (0.20 means 20% assignment probability).

    find_target_delta_contract(contracts, current_price, target_delta=0.25,
                                tolerance=0.08, opt_type="put",
                                iv=0.30, r=0.04) -> dict or None
        Given a list of contract dicts (with strike_price, expiration_date,
        current_price, and optionally implied_volatility), return the one
        whose computed delta is closest to target_delta within tolerance.
        Returns None if nothing qualifies.

    delta_score_bonus(delta, target=0.25) -> float
        Convert |delta-target| distance to a 0..10 score bonus so the
        wheel's existing score_contract() can pick up delta targeting
        without being fully rewritten.
"""
from __future__ import annotations
import math
from datetime import datetime, date


def _norm_cdf(x):
    """Cumulative standard normal distribution using math.erf.
    N(x) = 0.5 * (1 + erf(x / sqrt(2)))"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(S, K, T, sigma, r):
    """Black-Scholes d1. Falls back gracefully on degenerate inputs."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    try:
        return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return 0.0


def put_delta(S, K, T, sigma, r=0.04):
    """Black-Scholes put delta. Returns |delta| (always positive, 0..1)
    so callers can compare directly to target without sign juggling.

    Args:
        S: underlying price
        K: strike price
        T: years to expiration (e.g. 21/365 for 3 weeks)
        sigma: implied volatility (decimal, e.g. 0.35 for 35%)
        r: risk-free rate (default 4%, close to current T-bill)
    """
    if T <= 0:
        return 1.0 if S < K else 0.0
    d1 = _d1(S, K, T, sigma, r)
    # put delta = N(d1) - 1; return abs value
    return abs(_norm_cdf(d1) - 1.0)


def call_delta(S, K, T, sigma, r=0.04):
    """Black-Scholes call delta (0..1)."""
    if T <= 0:
        return 1.0 if S > K else 0.0
    d1 = _d1(S, K, T, sigma, r)
    return _norm_cdf(d1)


def years_to_expiration(expiration_date_str, today=None):
    """Convert YYYY-MM-DD expiration to years-from-today (float).
    Returns 0 for past/invalid dates."""
    if not expiration_date_str:
        return 0.0
    try:
        exp = datetime.strptime(expiration_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 0.0
    _today = today or date.today()
    days = (exp - _today).days
    return max(0.0, days / 365.0)


def delta_score_bonus(delta, target=0.25, tolerance=0.08):
    """Convert delta-distance to a 0..10 bonus. Peaks at target
    delta (score 10), linearly declines to 0 at tolerance edge,
    returns -5 for contracts way outside range (too safe or too
    risky)."""
    if delta is None:
        return 0
    dist = abs(delta - target)
    if dist <= tolerance:
        # Peak 10 at target, 0 at tolerance edge
        return round(10.0 * (1.0 - dist / tolerance), 2)
    # Outside tolerance — negative bonus pushes these picks out
    return -5.0 if dist > 0.15 else 0.0


def find_target_delta_contract(contracts, current_price, target_delta=0.25,
                                 tolerance=0.08, opt_type="put",
                                 iv=0.30, r=0.04):
    """Pick the contract whose Black-Scholes delta is closest to target
    (within tolerance). If no contract in `contracts` carries an
    implied_volatility field we fall back to the caller-supplied `iv`
    estimate (typically the underlying's historical vol).

    Args:
        contracts: list of dicts, each with keys:
            strike_price, expiration_date, and optionally
            implied_volatility (decimal).
        current_price: underlying price
        target_delta: 0.25 = target 25% assignment probability
        tolerance: |actual-target| ≤ tolerance to qualify
        opt_type: "put" or "call"
        iv: fallback implied vol if contract doesn't carry its own
        r: risk-free rate

    Returns the closest-matching contract dict (enriched with
    computed_delta) or None if nothing qualifies.
    """
    delta_fn = put_delta if opt_type == "put" else call_delta
    best = None
    best_dist = float("inf")
    for c in contracts:
        try:
            K = float(c.get("strike_price") or 0)
            if K <= 0:
                continue
            T = years_to_expiration(c.get("expiration_date"))
            if T <= 0:
                continue
            contract_iv = c.get("implied_volatility")
            try:
                sigma = float(contract_iv) if contract_iv else float(iv)
            except (ValueError, TypeError):
                sigma = float(iv)
            if sigma <= 0:
                sigma = 0.30  # assume 30% if we really have nothing
            d = delta_fn(float(current_price), K, T, sigma, r)
            dist = abs(d - target_delta)
            if dist <= tolerance and dist < best_dist:
                enriched = dict(c)
                enriched["computed_delta"] = round(d, 4)
                best = enriched
                best_dist = dist
        except Exception:
            continue
    return best


if __name__ == "__main__":
    # Smoke test — HIMS short put example.
    # Current price $27.50, strike $27, ~20 days to expiration, 35% IV
    S, K, T, sigma = 27.50, 27.00, 20 / 365.0, 0.35
    d = put_delta(S, K, T, sigma)
    print(f"HIMS $27 put delta: {d:.4f}")
    print(f"Target-25 bonus: {delta_score_bonus(d, 0.25):.2f}")

    # Synthetic chain
    chain = [
        {"strike_price": 24, "expiration_date": "2026-05-08", "implied_volatility": 0.35},
        {"strike_price": 25, "expiration_date": "2026-05-08", "implied_volatility": 0.35},
        {"strike_price": 26, "expiration_date": "2026-05-08", "implied_volatility": 0.35},
        {"strike_price": 27, "expiration_date": "2026-05-08", "implied_volatility": 0.35},
        {"strike_price": 28, "expiration_date": "2026-05-08", "implied_volatility": 0.35},
    ]
    best = find_target_delta_contract(chain, 27.50, 0.25, opt_type="put")
    print(f"Best ~25-delta strike: {best}")
