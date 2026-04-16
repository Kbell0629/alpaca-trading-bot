# Burn-Down List — Bot Maturity Roadmap

Tracker for remaining improvements. Updated 2026-04-16.

---

## ✅ DONE (43 features delivered)

### Core Platform
- [x] 6 strategies (trailing stop, copy trading, wheel, mean reversion, breakout, short selling)
- [x] Cloud scheduler on Railway (24/7, no laptop needed)
- [x] Interactive dashboard with kill switch, voice control, PWA
- [x] Readme modal (📖 Help button)
- [x] Mobile-responsive design
- [x] Basic auth with timing-safe comparison
- [x] Push notifications (ntfy.sh) + email queue

### Safety (25+ guardrails)
- [x] Kill switch (manual + auto-trigger)
- [x] Daily loss limit (3%)
- [x] Max drawdown (10%)
- [x] Cooldown after loss (60 min)
- [x] Short-specific cooldown (48 hr)
- [x] Capital sustainability check
- [x] Max positions (5)
- [x] Max per stock (10%)
- [x] Max new per day (2)
- [x] Correlation enforcement (sector limits)
- [x] Earnings avoidance
- [x] Meme stock filter
- [x] Market hours check
- [x] Deployment lock
- [x] Market breadth guard
- [x] Data quality guard (|change|<100%, vol<100%)

### Scoring Intelligence
- [x] Multi-timeframe momentum (5d + 20d)
- [x] Relative volume
- [x] Sector diversification
- [x] Earnings date avoidance
- [x] Dynamic position sizing
- [x] Profit-taking ladder
- [x] SPY market regime
- [x] News sentiment
- [x] Daily P&L tracking
- [x] Backtesting

### Advanced Features
- [x] Technical indicators (RSI, MACD, Bollinger, ATR, Stochastic, OBV, VWAP)
- [x] Real-time price monitor
- [x] Economic calendar (FOMC, opex)
- [x] Social sentiment (StockTwits)
- [x] Options chain analysis (wheel)
- [x] Options flow tracking (C/P ratio)
- [x] Extended hours logic
- [x] Short selling (auto-deploy)
- [x] Portfolio correlation matrix
- [x] Tax-loss harvesting
- [x] Voice interface
- [x] Strategy marketplace (presets)
- [x] Dynamic strategy rotation
- [x] Earnings play module
- [x] Post-market news scanner
- [x] **Wheel strategy full autonomy** (2026-04-16) — cash-secured puts → covered calls → repeat, fully automated
- [x] **Settings modal + admin panel** (2026-04-16) — multi-user management, per-user Alpaca creds, admin user list
- [x] **Auto-deployer fallback pool 5→20** (2026-04-16) — doesn't give up when top picks blocked by guardrails
- [x] Market breadth filter
- [x] Volume profile breakouts
- [x] Partial profit taking
- [x] Trade heatmap
- [x] Monthly rebalancing
- [x] Paper vs live comparison
- [x] Self-learning engine
- [x] Performance scorecard
- [x] Error recovery (orphan detection)

---

## 🟡 HIGH PRIORITY (Would Meaningfully Improve Performance)

### 1. News Signals → Auto-Deployer Integration
**Status:** Data exists but not wired into deployer
**Impact:** Medium — per-pick news_sentiment already captures most of this
**Effort:** 30 min
**Plan:** In `run_auto_deployer`, check if `pick.symbol` appears in `dashboard_data.news_signals.actionable`. Boost score if bullish news, skip if bearish.

### 2. Pre-Market Gap Trading
**Status:** Extended hours module exists but unused by deployer
**Impact:** High — capture 2-5% gap moves on news
**Effort:** 2-3 hours
**Plan:** Before market open, scan news_signals for overnight catalysts. Place pre-market limit orders that execute at open.

### 3. Earnings Play Auto-Deploy
**Status:** `earnings_play.py` generates candidates but deployer doesn't use them
**Impact:** Medium — limited to pre-earnings window
**Effort:** 1 hour
**Plan:** Separate auto-deploy path that buys 2-3 days before earnings, sells morning of. Track `earnings_date` field.

### ✅ Wheel Strategy Auto-Deploy (DONE 2026-04-16)
**Status:** COMPLETE — fully autonomous wheel cycle in `wheel_strategy.py` + cloud_scheduler tasks.
**What shipped:**
- `wheel_strategy.py` (~700 lines): state machine (searching → put_active → shares_owned → call_active → searching), Alpaca options API integration, per-symbol state files with audit trail.
- Cloud scheduler: `run_wheel_auto_deploy` at 9:40 AM weekdays + `run_wheel_monitor` every 15 min.
- Safety rails: options level 2+, cash coverage, never sell call below cost basis, earnings avoidance 30d, max 2 concurrent, price $10-$50, min 0.5% premium yield, 50% profit buy-to-close.
- `/api/wheel-status` endpoint.
- Config toggle: `auto_deployer_config.wheel.enabled` (default true).

### 4. Dividend Capture Strategy
**Status:** Not built
**Impact:** Medium — ~0.5-1% per capture, multiple per month
**Effort:** 2 hours
**Plan:** Track ex-dividend dates. Buy day before, sell day after (if tax-favorable for "qualified dividends" — hold 61 days).

### 5. Pairs Trading
**Status:** Not built
**Impact:** Medium — market-neutral income
**Effort:** 4-5 hours
**Plan:** Identify correlated pairs (e.g., KO/PEP, AMD/NVDA). Go long laggard + short leader when spread diverges.

---

## 🟠 MEDIUM PRIORITY (Nice to Have)

### 6. Insider Trading Follow (SEC Form 4)
**Status:** Not built
**Impact:** ~8% annual outperformance (historical)
**Effort:** 4 hours
**Plan:** Poll SEC EDGAR API (free) for Form 4 filings. When 3+ insiders buy same stock in 30 days, strong signal.

### 7. FedSpeak Sentiment Tracker
**Status:** Not built
**Impact:** Low-medium — niche edge
**Effort:** 3 hours
**Plan:** Track speeches from federalreserve.gov. NLP sentiment scoring. Adjust positions 30 min before speaker events.

### 8. Crypto-Equity Correlation
**Status:** Not built
**Impact:** Medium — BTC moves often lead QQQ by 24hrs
**Effort:** 2 hours
**Plan:** Monitor BTC via Alpaca crypto feed. On 5%+ BTC moves, adjust tech-stock positions.

### 9. A/B Test Strategies
**Status:** Not built
**Impact:** Medium — optimization
**Effort:** 3-4 hours
**Plan:** Run 2 variants of same strategy (e.g., 5% vs 10% stops). Compare after 30 days, promote winner.

### 10. Drawdown Recovery Mode
**Status:** Partial (covered by kill switch)
**Impact:** Low — existing guardrails handle it
**Effort:** 1 hour
**Plan:** When portfolio drops 5%, auto-switch to Conservative preset for 7 days.

### 11. Advanced Entry Orders
**Status:** Bot uses market orders only
**Impact:** Medium — save 0.1-0.3% slippage per trade
**Effort:** 2 hours
**Plan:** Use limit orders at midpoint of bid/ask, wait 2 min for fill, fall back to market if not filled.

### 12. Position Scaling In
**Status:** Not built (have scaling OUT via profit ladder)
**Impact:** Medium — reduce average cost on winners
**Effort:** 2 hours
**Plan:** On strong signal confirmation (e.g., breakout + volume), add 25% to existing position.

---

## 🔵 LOW PRIORITY (Polish / Future)

### 13. Twitter/X Sentiment
**Status:** Not built (requires paid API)
**Cost:** ~$100/mo — **USER REJECTED**
**Plan:** Deferred indefinitely.

### 14. Earnings Surprise Predictor
**Status:** Not built
**Impact:** Uncertain
**Effort:** 10+ hours (ML model)
**Plan:** Complex. Needs 8+ signals (analyst revisions, pre-earnings options activity, etc.). High ambiguity.

### 15. Proper PWA Icons
**Status:** Placeholder gradient icons
**Effort:** 30 min
**Plan:** Design proper app icons (logo, branding).

### 16. Historical Performance Charts
**Status:** Only backtest chart exists
**Impact:** Informational only
**Effort:** 2 hours
**Plan:** Chart actual portfolio equity over time (from trade_journal snapshots).

### 17. Multi-User Support
**Status:** Single user
**Impact:** Only relevant if bot goes public
**Effort:** 20+ hours (auth, data isolation)
**Plan:** Not needed for personal use.

### 18. Custom Strategy Builder UI
**Status:** Strategies coded in Python
**Impact:** Only useful for non-coders
**Effort:** 10+ hours
**Plan:** Visual rule builder that writes Python.

---

## 🧠 LEARNING ENGINE IMPROVEMENTS

The self-learning engine is already set up and runs automatically every Friday 5 PM ET (via `cloud_scheduler.py → run_weekly_learning() → learn.py`). It writes `learned_weights.json` which the screener reads to adjust strategy multipliers, signal bonuses, price-range preferences, and hold-day recommendations. The screener applies these weights automatically.

Current safeguards: cold-start protection (needs 5+ trades per strategy), 20% max change per week, manual override flag available, confidence levels (low/medium/high) based on sample size.

### After 30 Days (~50-60 trades)

#### Review learn.py's output
**What:** Manually review the first substantial learning cycle.
**Why:** Catch overfitting before it propagates. With limited data, bot might learn patterns that are just noise.
**Plan:** Read `learned_weights.json`. If any multiplier seems too aggressive (e.g., 0.5x on a strategy after 5 trades), set `manual_override: true` to pause learning. Consider tightening the cap from 20% to 10% weekly change.

#### Cap multiplier aggressiveness
**What:** Reduce max per-cycle change from 20% to 10% until confidence = "high".
**Why:** Small samples lead to wild swings. Medium confidence = tighter bounds.
**Effort:** 10 min
**Impact:** Low — just adds stability.

### After 60 Days (~100-130 trades)

#### Failed Opportunity Tracking
**What:** Log stocks the screener rated highly but didn't deploy (hit position limits, earnings warning, correlation block, etc). Track their 30-day performance.
**Why:** Bot currently only learns from trades that happened. Missing half the signal — stocks we ALMOST bought that then went up 20% are valuable training data too.
**Plan:** Add `near_misses.json` that stores top 5-10 picks that were skipped each day with reason. After 30 days, check their actual performance. If consistently profitable, the screener's scoring is validated. If losses, something's off.
**Effort:** 3-4 hours
**Impact:** Medium — doubles effective training data.

#### Time-of-Day Pattern Analysis
**What:** Analyze trade journal by entry time-of-day.
**Why:** "Morning trades (9:30-11:00 AM) have 65% win rate vs afternoon (2:00-4:00 PM) 40%" would be actionable.
**Plan:** Add hour-of-day field to journal entries. learn.py computes win rate by hour. If material difference, auto-adjust deployment time or avoid weak hours.
**Effort:** 2 hours
**Impact:** Low-medium — depends on whether a real pattern exists.

### After 90 Days (~150 trades)

#### Regime-Conditional Learning
**What:** Learn separate weights for bull vs bear vs neutral markets instead of one global weight set.
**Why:** Current system learns "breakout has 67% win rate" — but that might be 80% in bull and 20% in bear markets, averaged together. Separating them would expose the real edge.
**Plan:** Track `market_regime` in journal entries (already stored). learn.py builds 3 separate weight sets. Screener applies the right set based on current regime.
**Effort:** 4-5 hours
**Impact:** High — addresses the biggest weakness of the current learner.

#### Signal Discovery
**What:** Don't just boost/penalize EXISTING signals — discover NEW ones.
**Why:** Current bot can say "RSI oversold helps." It can't discover "stocks that dropped on Mondays after 10 AM win 75% of the time."
**Plan:** Feature-engineer 20-30 candidate signals (day-of-week, time-of-day, bollinger position, price vs 200 SMA, etc). learn.py tests each for correlation with wins. Statistically significant ones (p<0.05 with enough sample) get promoted to active signals.
**Effort:** 6-8 hours
**Impact:** High — but risk of false discoveries with small samples.

### After 6+ Months (~500+ trades)

#### Replace Signal Boosts With ML Model
**What:** Instead of hand-crafted boost rules, train a real model (logistic regression or gradient boosting) to predict trade outcomes.
**Why:** Current rule-based learning hits ceiling around 500 trades. A real model can find subtle interactions (e.g., "RSI oversold + high volume BUT ONLY in bear markets").
**Plan:** Use scikit-learn (stdlib-compatible via pip if we allow deps). Train on trade_journal features → outcome. Score picks with predicted win probability.
**Effort:** 10-15 hours. Requires allowing `scikit-learn` dependency (breaks "stdlib only" rule).
**Impact:** Potentially very high, but risk of overfitting amplified.

#### Walk-Forward Testing
**What:** Periodically split historical trades into train/test sets. Verify learned weights actually generalize.
**Why:** Without walk-forward validation, we don't know if learned weights are real patterns or coincidences.
**Plan:** Every month, use oldest 80% of trades for training, newest 20% for testing. If test performance matches train performance, weights are valid. If test performance is much worse, we're overfitting.
**Effort:** 4 hours
**Impact:** Medium — prevents confident deployment of bad rules.

### Meta Improvements

#### Confidence-Weighted Deployment
**What:** When learning confidence is "low" (< 20 trades), cap how much the bot trusts learned weights. As confidence rises, increase trust.
**Why:** Currently learn.py caps at 1.5x/0.5x regardless of sample size. 2 wins out of 3 shouldn't trigger 1.2x boost.
**Plan:** Apply a confidence-based dampener: `adjusted_multiplier = 1.0 + (multiplier - 1.0) * confidence_weight` where confidence_weight is 0.3 at low, 0.7 at medium, 1.0 at high.
**Effort:** 1 hour
**Impact:** Medium — prevents premature adjustments.

#### Dashboard Learning Tab
**What:** New dashboard section showing: last 8 weekly learning cycles, current multipliers, which signals are boost/penalty, confidence trend.
**Why:** You only see weekly notifications. A visual dashboard would make it obvious when learning is working vs off-the-rails.
**Plan:** New nav tab "Learning". Reads `learned_weights.json` + history. Chart.js line chart of multiplier evolution.
**Effort:** 3 hours
**Impact:** Low — transparency only, no direct profit impact.

---

## 🚀 ADVANCED / SPECULATIVE

### 19. Machine Learning Price Prediction
**Plan:** Train LSTM/Transformer on historical data. High complexity, uncertain edge, risky.

### 20. Reinforcement Learning Strategy Selection
**Plan:** RL agent learns which strategy works in which regime. Research project, not production.

### 21. Alternative Data Sources
**Plan:** Satellite imagery (parking lots), credit card data, app store rankings. Most require paid APIs.

### 22. Smart Order Routing
**Plan:** Break large orders across exchanges for best execution. Alpaca routes this automatically for us, mostly irrelevant.

### 23. Cross-Asset Arbitrage
**Plan:** ETFs vs component stocks, ADRs vs home-country stocks. Low margin, high speed requirements.

### 24. Real-Time WebSocket Streaming
**Status:** Currently polling every 60s
**Impact:** Low — 60s is fast enough
**Effort:** 6 hours
**Plan:** Alpaca has WebSocket for live quotes. Would reduce latency on stop triggers from 60s to milliseconds.

### 25. Brokerage Diversification
**Plan:** Run parallel on Alpaca + IBKR + Robinhood. Complex, marginal benefit.

---

## 📋 OPERATIONAL IMPROVEMENTS

### Going Live Preparation
- [ ] Hit 80/100 readiness score (30 days of paper profitability)
- [ ] Switch Railway env to live Alpaca endpoint
- [ ] Update guardrails to live_mode_settings
- [ ] Run paper in parallel for 1 week to validate
- [ ] Set up tax accountant / TurboTax import

### Monitoring
- [ ] Set up Railway alerts for deployment failures
- [ ] Monitor Alpaca API status page
- [ ] Weekly review of learned_weights.json changes

### Persistence (DONE 2026-04-16)
- [x] Railway volume `web-volume` mounted at `/data` (created via `railway volume add --mount-path /data`)
- [x] `DATA_DIR=/data` env var set on Railway
- [x] All persistent files (users.db, users/, strategies/, guardrails.json, scorecard.json, trade_journal.json, email_queue.json, notification_log.json, learned_weights.json, auto_deployer_config.json, capital_status.json, dashboard_data.json) now route through `DATA_DIR`
- [x] SOXL strategy file recovery script (`recover_soxl.py`) written; repopulates trailing_stop state from Alpaca after Railway redeploys wiped the file
- [ ] (Future) Daily backup/export of `users.db` and `users/` out of the volume — Railway volumes are NOT backed up

### Documentation
- [ ] Record demo video of the dashboard
- [ ] Document each notification type with example

---

## 🎯 RECOMMENDED NEXT BUILDS (In Order)

After 30 days of paper trading data, build in this order:

1. **News Signals Integration** (quick win, 30 min) — already have data
2. **Limit Order Entry** (saves 0.1-0.3% per trade, 2 hr)
3. **Review Learning Engine Output** (30 min manual review — see Learning Engine Improvements section above)
4. **Pre-Market Gap Trading** (big potential, 2-3 hours)
5. **Earnings Play Auto-Deploy** (module exists, just needs wiring)

After 60 days:

6. **Failed Opportunity Tracking** (doubles effective training data for the learner)
7. **Confidence-Weighted Learning** (prevents premature weight adjustments)

After 90 days:

8. **Regime-Conditional Learning** (highest-impact learning improvement)
9. **Real-Time WebSocket** (if paper profitability proves strategy, reduce latency)

Everything else is lower priority until we see how the bot performs on real data.

---

## 📊 MATURITY SCORECARD

| Domain | Status | Score |
|--------|--------|-------|
| Strategy diversity | 6 strategies active | 9/10 |
| Safety rails | 25+ guardrails | 10/10 |
| Automation | 24/7 cloud, no laptop | 10/10 |
| Intelligence | 10 scoring improvements | 9/10 |
| Data sources | Alpaca + StockTwits (free) | 7/10 |
| UX | Interactive dashboard, voice | 9/10 |
| Performance tracking | Scorecard + journal + heatmap | 9/10 |
| Learning | Weekly weight adjustment (automated, safe defaults, 8 improvement paths identified) | 8/10 |
| **Overall** | **Production-ready for paper** | **8.9/10** |

**Verdict:** Bot is sophisticated and safe. Next priority is letting it run 30 days to gather real performance data before adding more complexity.
