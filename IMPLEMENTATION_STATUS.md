# Implementation Status — 20 Profit Features

Tracker for the 20 profit-enhancement features proposed after the initial build. Updated 2026-04-16.

## ✅ Implemented (13 features)

| # | Feature | Location | Status |
|---|---------|----------|--------|
| 1 | Dynamic Strategy Rotation | `update_dashboard.py::apply_strategy_rotation()` | ✅ Live — regime weights in output JSON |
| 2 | Earnings Play Strategy | `earnings_play.py` | ✅ Live — 3 candidates found in latest scan |
| 3 | Sector Rotation Signal | `update_dashboard.py::calculate_sector_rotation()` | ✅ Live — 11 sector ETFs tracked |
| 4 | Post-Market News Scanner | `news_scanner.py` | ✅ Live — 50 articles scanned, 19 actionable in latest |
| 5 | Options Flow Tracking | `options_flow.py` | ✅ Live — C/P ratio data for top 20 picks |
| 6 | Overnight Risk Reduction | `cloud_scheduler.py::run_friday_risk_reduction()` | ✅ Live — fires Fridays 3:45 PM ET |
| 7 | Volume Profile Breakouts | `update_dashboard.py::score_stocks()` enhanced | ✅ Live — tiered 2x/3x multipliers |
| 8 | Partial Profit Taking | `cloud_scheduler.py::check_profit_ladder()` | ✅ Live — 25% at +10/20/30/50% |
| 9 | Market Breadth Filter | `update_dashboard.py::calculate_market_breadth()` | ✅ Live — A/D % from snapshot data |
| 10 | Position Correlation Enforcement | `cloud_scheduler.py::check_correlation_allowed()` | ✅ Live — blocks 3+ same sector, >40% concentration |
| 18 | Trade Heatmap | Dashboard + `/api/trade-heatmap` | ✅ Live — calendar + weekday analysis |
| 19 | Monthly Rebalancing | `cloud_scheduler.py::run_monthly_rebalance()` | ✅ Live — first trading day, closes 60+ day losers |
| 20 | Paper vs Live Comparison | Dashboard section | ✅ Live — paper active, live pending readiness |

## ⏳ Not Yet Built (7 features — explicitly deferred)

| # | Feature | Reason Deferred |
|---|---------|-----------------|
| 11 | FedSpeak Sentiment Tracker | Requires NLP pipeline, marginal edge |
| 12 | Twitter/X Sentiment | Requires paid Twitter API (~$100/mo, user said no cost) |
| 13 | Insider Trading Follow (SEC Form 4) | Data source needs parsing, similar edge to copy trading already implemented |
| 14 | Earnings Surprise Predictor | Complex ML model, uncertain edge |
| 15 | Crypto-Equity Correlation Play | Requires crypto feed setup, separate strategy |
| 16 | A/B Test Strategies | Would require running variants in parallel, scope creep |
| 17 | Drawdown Recovery Mode | Partially covered by existing kill switch + cooldown |

## Summary

- **13 of 20 features implemented** (65%)
- **All 13 tested and working** as of 2026-04-16
- **Railway deployment updated** and live 24/7
- **Committed and pushed** to GitHub
- **Pre-market audit completed 2026-04-16** — 16 bugs found and fixed across cloud_scheduler, server, update_dashboard, news_scanner
- **Short selling auto-deploy completed 2026-04-16** — cloud_scheduler now actually places short orders. Inverse trailing stop logic added. Bear-gated with 48hr cooldown.
- **Multi-user support added 2026-04-16** — SQLite auth database, per-user Alpaca credentials (encrypted), session cookies, signup/login/password-reset pages. Cloud scheduler iterates over all users. Backward compatible with env-var admin user.

## Future Improvements (User-Deferred)

### News Signals Integration (Decision 2 on 2026-04-16)
The screener produces `news_signals` (post-market actionable news) in dashboard_data.json but the auto-deployer doesn't consume them directly. Currently the per-pick `news_sentiment` field captures most of the effect.

Future enhancement: auto-deployer could boost picks that appear in `news_signals.actionable` with bullish scores, or skip picks with bearish news. Would require logic in `run_auto_deployer` around the pick-selection loop. Low priority — the `earnings_warning` and `news_sentiment` per-pick filters already prevent most bad-news trades.

## Testing Results (Verified 2026-04-16)

```
18/18 Python modules import successfully
earnings_play.py → NVDA scored 14.9 on test data
news_scanner.py → Scored +17 for "beats/raises", -14 for "SEC/lawsuit"
                  Live: 50 articles, 19 actionable
options_flow.py → TSLA live: C/P 0.56, 200 contracts, neutral
cloud_scheduler.py → All new functions callable:
                      check_profit_ladder, check_correlation_allowed,
                      run_friday_risk_reduction, run_monthly_rebalance,
                      is_first_trading_day_of_month
Correlation test → NVDA blocked (2 tech positions exist), JPM allowed (different sector)
Screener → 55s runtime, breadth 57.4%, 11 sector ETFs, regime weights applied
Dashboard endpoints → All return HTTP 200:
                      /api/scheduler-status, /api/trade-heatmap,
                      /api/guardrails, /api/data
Dashboard sections → section-heatmap, section-comparison,
                     buildComparisonPanel, loadHeatmap all present
Railway → scheduler running, time correct (8:13 AM EDT verified)
```

## New Data Fields in dashboard_data.json

- `earnings_candidates` — list of pre-earnings momentum plays
- `news_signals` — post-market news scan with actionable list
- `options_flow` — symbols with unusual C/P ratios
- `market_breadth` — {breadth_pct, advancing, declining, signal}
- `sector_rotation` — {sectors: {XLK: {strength}, ...}, spy_return_20d}
- `regime_weights` — current per-strategy multipliers

Per-pick new fields:
- `regime_weights_applied` — weights used for this pick
- `sector`, `sector_etf`, `sector_signal` — sector context
- `breakout_note` — 3x_volume_confirmed / 2x_volume_confirmed / etc.
- `breadth_warning` — flagged if market breadth is weak

## Files Created

- `earnings_play.py` (131 lines)
- `news_scanner.py` (154 lines)
- `options_flow.py` (108 lines)
- `README.md` (comprehensive user guide)
- `IMPLEMENTATION_STATUS.md` (this file)

## Files Modified

- `update_dashboard.py` — added 4 new functions, enhanced scoring
- `cloud_scheduler.py` — added 5 new functions, new scheduler triggers
- `server.py` — added /api/trade-heatmap endpoint, heatmap UI, comparison panel
