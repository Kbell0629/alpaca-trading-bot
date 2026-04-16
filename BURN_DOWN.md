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

### Documentation
- [ ] Record demo video of the dashboard
- [ ] Document each notification type with example

---

## 🎯 RECOMMENDED NEXT BUILDS (In Order)

After 30 days of paper trading data, build in this order:

1. **News Signals Integration** (quick win, 30 min) — already have data
2. **Pre-Market Gap Trading** (big potential, 2-3 hours)
3. **Earnings Play Auto-Deploy** (module exists, just needs wiring)
4. **Limit Order Entry** (saves 0.1-0.3% per trade)
5. **Real-Time WebSocket** (if paper profitability proves strategy, reduce latency)

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
| Learning | Weekly weight adjustment | 8/10 |
| **Overall** | **Production-ready for paper** | **8.9/10** |

**Verdict:** Bot is sophisticated and safe. Next priority is letting it run 30 days to gather real performance data before adding more complexity.
