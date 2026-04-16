# Burn-Down List — Bot Maturity Roadmap

Tracker for remaining improvements. Updated 2026-04-16.

---

## 📆 30-DAY PAPER-TRADING REVIEW — 2026-05-16

**When the 30-day window ends (first market day was 2026-04-16), the
review framework lives in the user's memory file
`thirty_day_review.md`**. Don't improvise the review — that file has:

- GREEN/YELLOW/RED outcome thresholds (win rate, Sharpe, drawdown)
- Which deferred features to build NEXT based on how the 30 days went
- Go-live migration checklist (endpoint swap, guardrails tuning,
  parallel run procedure)
- Rollback plan if the first live week goes sideways

Load that memory file FIRST if the user asks for a 30-day review,
readiness check, or "time to go live."

---

## 🛑 CLASSIFICATION GUIDE FOR AUDITORS — READ FIRST

**Everything in this file is a FEATURE ITEM, not a code defect.** New
audit passes (round 9+) should treat the open items below as
user-deferred product decisions, NOT as bugs to re-surface.

Key distinctions:
- **BUG / DEFECT** = code that behaves incorrectly vs its stated intent
  (e.g., "stop_scheduler was never imported and SIGTERM silently
  failed"). These belong in PROJECT.md's recent-changes log and get
  fixed immediately.
- **FEATURE / MATURITY ITEM** = code works as designed but the design
  could be smarter (e.g., "market orders work, but limit-at-mid would
  save slippage"). These live HERE. An auditor finding one should NOT
  file it as a bug.

**Blanket rule for future audits:** if an "issue" is already listed
in this file with a `Status:` line saying it's deferred, skipped, or
awaiting a user decision, DO NOT re-report it as a finding. List
reference number (e.g., "item #14 — deferred") and move on.

The explicit decisions the user has made:

| Status tag | Meaning |
|---|---|
| `DECISION: DEFERRED` | User chose not to implement; may revisit after N days of paper trading |
| `DECISION: SKIPPED` | User chose not to implement indefinitely (e.g., paid API they won't pay for) |
| `DECISION: PENDING` | Needs alignment with user before coding |
| `DONE` | Shipped — should be in the ✅ DONE list below |

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
- [x] **4-round forensic audit + fix** (2026-04-16) — auth, API isolation, UX, edge cases, financial math, client JS, operational resilience, security deep-dive. All P0/P1/critical P2 findings fixed.
- [x] **AES-256-GCM encryption for Alpaca credentials** (2026-04-16, commit `e391f79`) — via `cryptography` pip dep, backward-compat with legacy `ENC:` HMAC cipher, transparent upgrade on login.
- [x] **Credential-free backups** (2026-04-16) — admin downloadable backups have `alpaca_key_encrypted`/`alpaca_secret_encrypted` NULLED. Live DB keeps them for scheduler runtime.
- [x] **Persistent login rate limit** (2026-04-16) — SQLite `login_attempts` table. Brute-force lockout survives Railway redeploy.
- [x] **Operational hardening** (2026-04-16) — SIGTERM handler (clean scheduler shutdown + orphan-order prevention), SQLite WAL mode, `/healthz` endpoint, `python3 -u` for unbuffered Railway logs, Alpaca retry+backoff.
- [x] **Trade journal close writeback** (2026-04-16) — FIXES critical bug: win_rate/Sharpe/readiness/learning were all 0 forever because exits never wrote back. Now every exit path calls `record_trade_close()`.
- [x] **Profit ladder idempotency** (2026-04-16) — `client_order_id` prevents double-sell on transient Alpaca errors.
- [x] **Backtest uses OHLC** (2026-04-16) — intraday LOW for stop detection + open clamp for gap-down fills. Previously overstated returns.
- [x] **Round 5 — ET-only policy + 10 hardening fixes** (2026-04-16, `0d1f961`) — single ET timezone across the app, DST bug fix, healthz staleness, hashed reset tokens, audit log rotation.
- [x] **Round 5.2 — 10 decision-item fixes** (2026-04-16, `7c2c013`) — SECTOR_MAP consolidation, HTML extraction to templates/, 30-test pytest suite, HKDF ENCv3, zxcvbn password strength, random ntfy topics, partial god-function decomposition.
- [x] **Round 6 — thread safety + observability + partial-fill reconcile** (2026-04-16, `5e63488`) — `_cb_state`/`_api_cache` locked, circuit-breaker push alert, /api/version, WAL checkpoint before backup, 0o700 perms, 40 tests.
- [x] **Round 6.5 — DashboardHandler decomposition** (2026-04-16, `7016808`) — 4 mixin files, server.py 2927 → 1573 lines.
- [x] **Round 7 — P0 regression hotfix + subprocess boot test** (2026-04-16, `7708af3`+`af7db0a`) — lazy server proxy to fix circular import, subprocess healthz test that catches the launch-path class of bug.
- [x] **Round 8 — 11 to-do items closed** (2026-04-16, `a90ff83`) — see section below. 49 tests passing.

#### Round 8 (2026-04-16) — items closed from the to-do list

- [x] **#1 Legacy ENC cipher removed** — Railway CLI confirmed 0 rows. ~30 LOC of dead crypto deleted. ENCv2 kept decrypt-only as safety net.
- [x] **#2 Wheel dashboard card migration** — card now reads per-symbol state from /api/wheel-status instead of legacy single-cycle template.
- [x] **#3 Strategy-file locking** — new `strategy_file_lock` context manager (same pattern as wheel lock) wired into monitor_strategies + pause/stop handlers.
- [x] **#4 News signals → auto-deployer** — bearish news now skips deploys.
- [x] **#5 Per-strategy confidence in learn.py** — each strategy carries its own low/medium/high + weight_frozen flag.
- [x] **#6 E2E integration tests** — 4 subprocess-based tests covering signup/auth/weak-password paths.
- [x] **#7 Client-side zxcvbn meter** — signup + reset forms show strength as user types.
- [x] **#8 Test-Connection error hardening** — no more raw Alpaca/server text echoed to UI.
- [x] **#9 Task-staleness watchdog + heartbeat log** — per-task alerts during market hours; 2min heartbeat keeps /healthz green after-hours.
- [x] **#10 Market-breadth div/0** — verified already safe (false positive from agent).
- [x] **#11 Dashboard TZ (addLog)** — now renders ET explicitly.
- [x] **Bonus: SIGTERM handler fixed** — `stop_scheduler` was never imported; SIGTERM silently swallowed the NameError for months.
- [x] **Screener scoring tune** (`e70bcf2` → `b5348e1`, 2026-04-16) — live dashboard showed all top-12 picks were thin-float breakouts. Two guards shipped: `MIN_VOLUME` 100k → 300k (middle ground between original too-permissive and 500k too-strict), plus `breakout_score *= 0.5` when intraday volatility > 25%. 4 regression tests.
- [x] **Live-day UX fixes** (`b9e8264`, `1683791`, `21baa5b`, `bc8d765`, 2026-04-16) — stale "MARKET OPEN" badge, heatmap re-loading every refresh, daily-close skip after container restart (persistence + wider hour-gate + 4hr late-tolerance), "Days Tracked" off-by-one (Math.round → Math.floor), heatmap Best/Worst Day rendering with only 1 day of data.
- [x] **Round 9 final audit + mobile polish** (`c3f8276`, 2026-04-16) — last comprehensive pass. Fixed: subprocess scorecard path leak (scorecard was writing to shared /data/ instead of per-user path — caused dashboard staleness observed live today), TOCTOU races in should_run_interval/should_run_daily_at, `handle_force_daily_close` + `_refresh_cooldowns` missing locks, `pnlClass(0)` rendered green. Mobile: readme/deploy/settings modals full-screen on phones, tables horizontal-scroll with min-widths, 40px touch targets, 16px form font (iOS no-autozoom), iPhone SE ultra-narrow tier. 54 tests passing. See PROJECT.md for full details.
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

### ✅ 1. News Signals → Auto-Deployer Integration — DONE (Round 8, 2026-04-16)
**Status:** SHIPPED (commit `a90ff83`)
**Implementation:** `run_auto_deployer` in cloud_scheduler.py now reads
`picks_data.news_signals.actionable` and skips picks flagged **bearish**.
Per-pick `news_sentiment` remains as the softer signal layer.
NOT a bug anymore — do not re-flag.

### 2. Pre-Market Gap Trading
**Status:** `DECISION: DEFERRED` (user, 2026-04-16)
**Why deferred:** Adds complexity to a system just stabilized after 8
audit rounds. User wants to see 30 days of live-paper performance from
the existing 6 strategies before adding gap trading.
**Impact if built:** High — capture 2-5% gap moves on news
**Effort:** 2-3 hours
**Plan (when user approves):** Before market open, scan news_signals
for overnight catalysts. Place pre-market limit orders that execute
at open.
**NOT a bug** — do not file as an audit finding.

### 3. Earnings Play Auto-Deploy
**Status:** `DECISION: DEFERRED` (user, 2026-04-16) — blocked on data source
**Blocker:** `earnings_play.py` filters candidates by momentum but has
**no actual earnings-date data**. The current `find_upcoming_earnings()`
scans news headlines for "will report" which is too noisy to deploy
real money on. Safe auto-deploy requires an earnings-date calendar.
**Options the user must choose between:**
  - (a) Add AlphaVantage / FMP free-tier API key for earnings calendar
  - (b) Keep scanning Alpaca news with stricter regex (risky — false positives)
  - (c) Defer indefinitely (current choice)
**NOT a bug** — the module works for its current purpose (scoring);
auto-deploy just isn't wired pending the data source decision.

### ✅ Wheel Strategy Auto-Deploy (DONE 2026-04-16)
**Status:** COMPLETE — fully autonomous wheel cycle in `wheel_strategy.py` + cloud_scheduler tasks.
**What shipped:**
- `wheel_strategy.py` (~700 lines): state machine (searching → put_active → shares_owned → call_active → searching), Alpaca options API integration, per-symbol state files with audit trail.
- Cloud scheduler: `run_wheel_auto_deploy` at 9:40 AM weekdays + `run_wheel_monitor` every 15 min.
- Safety rails: options level 2+, cash coverage, never sell call below cost basis, earnings avoidance 30d, max 2 concurrent, price $10-$50, min 0.5% premium yield, 50% profit buy-to-close.
- `/api/wheel-status` endpoint.
- Config toggle: `auto_deployer_config.wheel.enabled` (default true).

### 4. Dividend Capture Strategy (NEW strategy — not built)
**Status:** `DECISION: DEFERRED` (user, 2026-04-16)
**Why deferred:** (a) Tax-treatment rules (61-day holding for "qualified
dividends") only matter with real money — irrelevant for paper. (b) Needs
ex-div calendar data source. (c) User wants to let the existing 6 strategies
prove themselves for 30 days first.
**Impact if built:** ~0.5-1% per capture, multiple per month
**Effort:** 2 hours
**Plan (if approved later):** Track ex-dividend dates. Buy day before,
sell day after. For live trading, enforce 61-day hold to qualify for
favorable tax treatment.
**NOT a bug** — feature request, not code defect.

### 5. Pairs Trading (NEW strategy — not built)
**Status:** `DECISION: DEFERRED` (user, 2026-04-16)
**Why deferred:** User wants to see the existing 6 strategies run for
30+ days before adding a market-neutral overlay. Also requires decision
on which pairs to track and their spread thresholds.
**Impact if built:** Medium — market-neutral income stream
**Effort:** 4-5 hours
**Plan (if approved later):** Identify correlated pairs (e.g., KO/PEP,
AMD/NVDA). Go long laggard + short leader when spread diverges by N
standard deviations.
**NOT a bug** — feature request.

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

### 11. Advanced Entry Orders (limit vs market)
**Status:** `DECISION: DEFERRED` for paper (user, 2026-04-16). Revisit before going live.
**Why deferred:** User prefers simple/reliable market orders during paper
phase. Trade-off: limit orders save 0.1-0.3% slippage but fills can fail,
requiring every strategy to handle "entry didn't fill" branch. 12 market-
order sites across cloud_scheduler, actions_mixin, strategy_mixin would
need updating — high blast radius during a stabilization period.
**Impact if built:** Medium — save 0.1-0.3% slippage per trade
**Effort:** 2 hours
**Plan (when user approves):** Limit at midpoint of bid/ask, wait 2
min for fill, fall back to market if not filled. Test on paper for a
week before enabling on live.
**NOT a bug** — market orders are the explicit current design.

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
**Status:** `DECISION: DEFERRED` (user, 2026-04-16) — revisit at ~60 days of trade history
**Why deferred:** Low value before ~60 days of real trades (need
enough skipped-picks data for the analysis to mean anything).
**What:** Log stocks the screener rated highly but didn't deploy (hit position limits, earnings warning, correlation block, etc). Track their 30-day performance.
**Why:** Bot currently only learns from trades that happened. Missing half the signal — stocks we ALMOST bought that then went up 20% are valuable training data too.
**Plan:** Add `near_misses.json` that stores top 5-10 picks that were skipped each day with reason. After 30 days, check their actual performance. If consistently profitable, the screener's scoring is validated. If losses, something's off.
**Effort:** 3-4 hours
**Impact:** Medium — doubles effective training data.
**NOT a bug** — future feature.

#### Time-of-Day Pattern Analysis
**What:** Analyze trade journal by entry time-of-day.
**Why:** "Morning trades (9:30-11:00 AM) have 65% win rate vs afternoon (2:00-4:00 PM) 40%" would be actionable.
**Plan:** Add hour-of-day field to journal entries. learn.py computes win rate by hour. If material difference, auto-adjust deployment time or avoid weak hours.
**Effort:** 2 hours
**Impact:** Low-medium — depends on whether a real pattern exists.

### After 90 Days (~150 trades)

#### Regime-Conditional Learning
**Status:** `DECISION: DEFERRED` (user, 2026-04-16) — revisit at ~100+ trades
**Why deferred:** Small-sample learning with regime splits amplifies
noise. Need ~100 trades across regimes before splitting is meaningful.
Also needs hysteresis rule decision (how much must SPY move before we
consider the regime flipped) to avoid flapping.
**What:** Learn separate weights for bull vs bear vs neutral markets instead of one global weight set.
**Why:** Current system learns "breakout has 67% win rate" — but that might be 80% in bull and 20% in bear markets, averaged together. Separating them would expose the real edge.
**Plan:** Track `market_regime` in journal entries (already stored). learn.py builds 3 separate weight sets. Screener applies the right set based on current regime.
**Effort:** 4-5 hours
**Impact:** High — addresses the biggest weakness of the current learner.
**NOT a bug** — future feature; current single-weights learning is intentional.

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
**Status:** `DECISION: DEFERRED` (user, 2026-04-16)
**Why deferred:** 60s polling is sufficient for paper trading. Slippage
at 60s-latency stop triggers is noise-level at the stop distances this
bot uses. Real-time WebSocket adds operational complexity (reconnect
logic, missed-event recovery, heartbeat handling) without material
upside until strategies prove tighter stop behavior matters.
**Currently polling:** every 60s during market hours
**Impact if built:** Low — 60s is fast enough
**Effort:** 6 hours
**Plan (if approved later):** Alpaca has WebSocket for live quotes.
Would reduce latency on stop triggers from 60s to milliseconds.
**NOT a bug** — 60s polling is the explicit current design.

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
