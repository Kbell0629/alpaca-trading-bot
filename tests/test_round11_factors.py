#!/usr/bin/env python3
"""
Round-11 factor module unit + integration tests.

Covers:
  - risk_sizing: ATR + stop-sizing + vol-parity multiplier
  - market_breadth: regime classification + cache I/O
  - factor_enrichment: RS computation + sector multiplier math
  - quality_filter: scoring math + bullish news regex
  - iv_rank: HV + HV-rank computation
  - options_greeks: Black-Scholes delta for puts + calls
  - yfinance_budget: rate-limit sliding window, circuit breaker

All tests use SYNTHETIC data — no network calls, no yfinance. They
lock in the math so refactors don't silently change behavior.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# Ensure project root on path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================================
# risk_sizing
# ============================================================================

class TestRiskSizing(unittest.TestCase):
    def setUp(self):
        import risk_sizing
        self.mod = risk_sizing
        # Synthetic bars — consistent $1 range per day
        self.stable_bars = [{"o": 100, "h": 101, "l": 99, "c": 100} for _ in range(30)]
        # Volatile bars — $5 range per day
        self.volatile_bars = [{"o": 100, "h": 105, "l": 95, "c": 100} for _ in range(30)]

    def test_atr_on_stable_bars(self):
        atr = self.mod.compute_atr(self.stable_bars, period=14)
        # True range on each bar = max(h-l, |h-prev_c|, |l-prev_c|) = 2
        # ATR should be ~2
        self.assertAlmostEqual(atr, 2.0, places=1)

    def test_atr_on_volatile_bars(self):
        atr = self.mod.compute_atr(self.volatile_bars, period=14)
        # TR = 10 per bar
        self.assertAlmostEqual(atr, 10.0, places=1)

    def test_atr_insufficient_data(self):
        self.assertEqual(self.mod.compute_atr([], 14), 0.0)
        self.assertEqual(self.mod.compute_atr(self.stable_bars[:5], 14), 0.0)

    def test_atr_pct(self):
        pct = self.mod.atr_pct(self.stable_bars, period=14, current_price=100)
        self.assertAlmostEqual(pct, 0.02, places=2)

    def test_atr_based_stop_pct_clamps(self):
        # Very stable bars — ATR * 2.5 = 5% → hits floor
        s = self.mod.atr_based_stop_pct(self.stable_bars, multiplier=2.5,
                                         floor_pct=0.05, cap_pct=0.15,
                                         current_price=100)
        self.assertEqual(s, 0.05)  # floored
        # Very volatile bars — ATR * 2.5 = 25% → hits cap
        s = self.mod.atr_based_stop_pct(self.volatile_bars, multiplier=2.5,
                                         floor_pct=0.05, cap_pct=0.15,
                                         current_price=100)
        self.assertEqual(s, 0.15)

    def test_volatility_position_multiplier(self):
        # base 2% ATR == current 2% ATR → multiplier = 1.0
        m = self.mod.volatility_position_multiplier(self.stable_bars, base_atr_pct=0.02)
        self.assertAlmostEqual(m, 1.0, places=1)
        # Higher vol → multiplier < 1 (smaller positions)
        m = self.mod.volatility_position_multiplier(self.volatile_bars, base_atr_pct=0.02)
        self.assertLess(m, 1.0)


# ============================================================================
# market_breadth
# ============================================================================

class TestMarketBreadth(unittest.TestCase):
    def test_classify_regime(self):
        import market_breadth as mb
        self.assertEqual(mb._classify_regime(80), "strong")
        self.assertEqual(mb._classify_regime(55), "healthy")
        self.assertEqual(mb._classify_regime(30), "weak")

    def test_cache_io_roundtrip(self):
        import market_breadth as mb
        with tempfile.TemporaryDirectory() as d:
            path = mb._cache_path(d)
            mb._save_cache(path, {"breadth_pct": 55.0, "regime": "healthy"})
            loaded = mb._load_cache(path)
            self.assertEqual(loaded["breadth_pct"], 55.0)
            self.assertEqual(loaded["regime"], "healthy")

    def test_should_block_breakouts_uses_cache(self):
        import market_breadth as mb
        with tempfile.TemporaryDirectory() as d:
            # Write a stale-but-recent healthy breadth cache
            from et_time import now_et
            mb._save_cache(mb._cache_path(d), {
                "breadth_pct": 70.0,
                "above_50dma": 70, "total": 100,
                "regime": "strong",
                "computed_at": now_et().isoformat(),
            })
            # With healthy breadth, breakouts should NOT be blocked
            self.assertFalse(mb.should_block_breakouts(data_dir=d, breadth_threshold=40))
            # Write a weak breadth cache
            mb._save_cache(mb._cache_path(d), {
                "breadth_pct": 25.0,
                "above_50dma": 25, "total": 100,
                "regime": "weak",
                "computed_at": now_et().isoformat(),
            })
            self.assertTrue(mb.should_block_breakouts(data_dir=d, breadth_threshold=40))


# ============================================================================
# factor_enrichment (RS + sector rotation math)
# ============================================================================

class TestFactorEnrichment(unittest.TestCase):
    def test_rs_outperformer(self):
        import factor_enrichment as fe
        # SPY returns ~10% over 60d
        spy = [{"c": 100 + i * 0.17} for i in range(130)]
        # Stock returns ~30% over 60d
        stock = [{"c": 100 + i * 0.5} for i in range(130)]
        rs = fe.compute_relative_strength(stock, spy)
        self.assertGreater(rs["rs_3m"], 0.05)
        self.assertGreater(rs["rs_composite"], 0.05)

    def test_rs_underperformer(self):
        import factor_enrichment as fe
        spy = [{"c": 100 + i * 0.2} for i in range(130)]
        stock = [{"c": 100 + i * 0.1} for i in range(130)]
        rs = fe.compute_relative_strength(stock, spy)
        self.assertLess(rs["rs_3m"], 0)

    def test_rs_on_insufficient_data(self):
        import factor_enrichment as fe
        rs = fe.compute_relative_strength([], [])
        self.assertEqual(rs["rs_composite"], 0.0)
        rs = fe.compute_relative_strength([{"c": 100}] * 3, [{"c": 100}] * 3)
        self.assertEqual(rs["rs_composite"], 0.0)

    def test_sector_multiplier_lookup(self):
        import factor_enrichment as fe
        rankings = {"XLK": {"sector": "Tech", "rank": 1, "multiplier": 1.20}}
        # Tech stock → XLK → boost
        self.assertEqual(fe.sector_multiplier_for_symbol("AAPL", rankings), 1.20)
        # Unmapped sector → neutral
        self.assertEqual(fe.sector_multiplier_for_symbol("UNKNOWN_TICKER", rankings), 1.0)


# ============================================================================
# quality_filter
# ============================================================================

class TestQualityFilter(unittest.TestCase):
    def test_score_fundamentals_tier_a(self):
        import quality_filter as qf
        score = qf._score_fundamentals({
            "roe": 0.22, "debt_equity": 0.5, "fcf_positive": True,
        })
        # +10 (ROE) +5 (D/E) +10 (FCF) = 25 → tier A
        self.assertEqual(score, 25)
        self.assertEqual(qf._classify_tier(score), "A")

    def test_score_fundamentals_tier_d(self):
        import quality_filter as qf
        score = qf._score_fundamentals({
            "roe": -0.05, "debt_equity": 5.0, "fcf_positive": False,
        })
        # -5 + -5 + -10 = -20 → tier D
        self.assertEqual(score, -20)
        self.assertEqual(qf._classify_tier(score), "D")

    def test_score_fundamentals_missing_fields(self):
        import quality_filter as qf
        # All None → 0 (neutral)
        self.assertEqual(qf._score_fundamentals({
            "roe": None, "debt_equity": None, "fcf_positive": None,
        }), 0)

    def test_bullish_news_detects_keywords(self):
        import quality_filter as qf
        r = qf.bullish_news_bonus([
            {"headline": "AAPL upgraded to Buy, price target raised"},
            {"headline": "AAPL beats estimates, raises guidance"},
        ])
        self.assertGreater(r["bonus"], 5)
        self.assertTrue(r["has_bullish_catalyst"])
        self.assertIn("upgraded", r["matched_keywords"])

    def test_bullish_news_empty(self):
        import quality_filter as qf
        r = qf.bullish_news_bonus([])
        self.assertEqual(r["bonus"], 0)
        self.assertFalse(r["has_bullish_catalyst"])

    def test_bullish_news_caps_at_15(self):
        import quality_filter as qf
        # Stack many keywords — bonus capped
        r = qf.bullish_news_bonus([
            {"headline": "upgrade beats estimates raises guidance fda approval contract win"},
        ])
        self.assertLessEqual(r["bonus"], 15)


# ============================================================================
# iv_rank
# ============================================================================

class TestIVRank(unittest.TestCase):
    def test_compute_hv_on_constant_series(self):
        import iv_rank
        # Constant prices → zero volatility
        closes = [100.0] * 30
        self.assertEqual(iv_rank.compute_hv(closes, 20), 0.0)

    def test_compute_hv_on_volatile_series(self):
        import iv_rank
        import random
        random.seed(42)
        closes = [100.0]
        for _ in range(30):
            closes.append(closes[-1] * (1 + random.gauss(0, 0.02)))
        hv = iv_rank.compute_hv(closes, 20)
        # ~2% daily stdev → annualized ~31% (2% * sqrt(252) ≈ 0.31)
        self.assertGreater(hv, 0.15)
        self.assertLess(hv, 0.6)

    def test_hv_rank_range(self):
        import iv_rank
        import random
        random.seed(1)
        closes = [100.0]
        for _ in range(300):
            closes.append(closes[-1] * (1 + random.gauss(0, 0.02)))
        rank = iv_rank.compute_hv_rank(closes)
        self.assertGreaterEqual(rank, 0)
        self.assertLessEqual(rank, 100)

    def test_should_sell_put(self):
        import iv_rank
        self.assertFalse(iv_rank.should_sell_put(20, threshold=40))
        self.assertTrue(iv_rank.should_sell_put(50, threshold=40))
        self.assertFalse(iv_rank.should_sell_put(None, threshold=40))

    def test_iv_rank_score_bonus(self):
        import iv_rank
        # Low rank → penalty
        self.assertLess(iv_rank.iv_rank_score_bonus(15), 0)
        # High rank → bonus
        self.assertGreater(iv_rank.iv_rank_score_bonus(75), 5)
        # None → neutral
        self.assertEqual(iv_rank.iv_rank_score_bonus(None), 0)


# ============================================================================
# options_greeks
# ============================================================================

class TestOptionsGreeks(unittest.TestCase):
    def test_put_delta_atm(self):
        import options_greeks as og
        # ATM put, 30d to expiry, 30% vol
        d = og.put_delta(100, 100, 30 / 365.0, 0.30)
        # ATM put delta ~0.47
        self.assertGreater(d, 0.4)
        self.assertLess(d, 0.55)

    def test_put_delta_deep_otm(self):
        import options_greeks as og
        # Deep OTM — strike 80, price 100 — delta should be low
        d = og.put_delta(100, 80, 30 / 365.0, 0.30)
        self.assertLess(d, 0.15)

    def test_put_delta_at_expiration(self):
        import options_greeks as og
        # T=0, S > K → put worthless (delta 0)
        self.assertEqual(og.put_delta(100, 90, 0, 0.3), 0.0)
        # T=0, S < K → put ITM (delta 1.0)
        self.assertEqual(og.put_delta(85, 90, 0, 0.3), 1.0)

    def test_call_delta_atm(self):
        import options_greeks as og
        d = og.call_delta(100, 100, 30 / 365.0, 0.30)
        self.assertGreater(d, 0.45)
        self.assertLess(d, 0.6)

    def test_find_target_delta_contract(self):
        import options_greeks as og
        chain = [
            {"strike_price": 90, "expiration_date": "2099-01-01", "implied_volatility": 0.30},
            {"strike_price": 95, "expiration_date": "2099-01-01", "implied_volatility": 0.30},
            {"strike_price": 100, "expiration_date": "2099-01-01", "implied_volatility": 0.30},
        ]
        # Target 0.25 delta put — should prefer a lower strike
        best = og.find_target_delta_contract(chain, 100, target_delta=0.25,
                                               tolerance=0.3, opt_type="put")
        self.assertIsNotNone(best)
        self.assertIn("computed_delta", best)

    def test_delta_score_bonus(self):
        import options_greeks as og
        # At target → full bonus
        self.assertAlmostEqual(og.delta_score_bonus(0.25, 0.25), 10.0, places=1)
        # Outside tolerance → negative
        self.assertLess(og.delta_score_bonus(0.5, 0.25, tolerance=0.05), 0)


# ============================================================================
# yfinance_budget
# ============================================================================

class TestYfinanceBudget(unittest.TestCase):
    def setUp(self):
        # Reset module state between tests
        import yfinance_budget as yb
        yb._request_times.clear()
        yb._cb_failures = 0
        yb._cb_tripped_until = 0.0

    def test_rate_limit_allows_initial_requests(self):
        import yfinance_budget as yb
        # First few requests should pass immediately
        t0 = time.time()
        for _ in range(5):
            yb._wait_for_slot()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0)

    def test_circuit_breaker_trips(self):
        import yfinance_budget as yb
        for _ in range(yb.CB_FAILURE_THRESHOLD):
            yb._record_failure()
        self.assertTrue(yb._circuit_open())

    def test_circuit_breaker_resets_on_success(self):
        import yfinance_budget as yb
        yb._record_failure()
        yb._record_failure()
        yb._record_success()
        self.assertFalse(yb._circuit_open())

    def test_stats_returns_expected_shape(self):
        import yfinance_budget as yb
        s = yb.stats()
        self.assertIn("requests_in_window", s)
        self.assertIn("window_limit", s)
        self.assertIn("circuit_open", s)


# ============================================================================
# INTEGRATION SMOKE: factor pipeline on synthetic picks
# ============================================================================

class TestFactorPipelineIntegration(unittest.TestCase):
    """End-to-end: feed synthetic picks through the factor enrichment
    functions and verify each pick gets the expected fields attached
    without errors. Does NOT call yfinance."""

    def test_apply_factor_scores_does_not_crash(self):
        import factor_enrichment as fe
        picks = [
            {"symbol": "AAPL", "breakout_score": 50, "pead_score": 30,
             "mean_reversion_score": 20, "wheel_score": 40},
            {"symbol": "MSFT", "breakout_score": 45, "pead_score": 25,
             "mean_reversion_score": 35, "wheel_score": 55},
        ]
        spy_bars = [{"c": 100 + i * 0.1} for i in range(130)]
        bars_map = {"AAPL": [{"c": 100 + i * 0.2} for i in range(130)]}
        # Should not throw even with missing sector_rankings
        fe.apply_factor_scores(picks, spy_bars, bars_map=bars_map)
        self.assertIn("rs_composite", picks[0])
        self.assertIn("factor_adjusted", picks[0])
        self.assertTrue(picks[0]["factor_adjusted"])

    def test_apply_quality_filter_graceful_on_missing_news(self):
        import quality_filter as qf
        picks = [
            {"symbol": "AAPL", "breakout_score": 50, "pead_score": 30,
             "mean_reversion_score": 20, "wheel_score": 40},
        ]
        # Patch the yfinance call to avoid network
        with patch.object(qf, "_fetch_fundamentals_live",
                           return_value={"roe": 0.2, "debt_equity": 0.5,
                                         "fcf_positive": True}):
            with tempfile.TemporaryDirectory() as d:
                qf.apply_quality_filter(picks, data_dir=d, news_map={})
                self.assertIn("quality_tier", picks[0])
                self.assertEqual(picks[0]["quality_tier"], "A")

    def test_atr_stop_computation_pipeline(self):
        """Simulate what the deployer does: pick has atr_pct, we compute
        a volatility-sized stop."""
        import risk_sizing as rs
        bars = [{"o": 100, "h": 103, "l": 97, "c": 100} for _ in range(30)]
        pct = rs.atr_pct(bars, period=14, current_price=100)
        self.assertGreater(pct, 0)
        stop = rs.atr_based_stop_pct(bars, multiplier=2.5, floor_pct=0.05,
                                      cap_pct=0.15, current_price=100)
        self.assertGreaterEqual(stop, 0.05)
        self.assertLessEqual(stop, 0.15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
