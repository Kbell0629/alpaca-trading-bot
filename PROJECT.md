# Alpaca Stock Trading Bot

Autonomous stock trading bot using Alpaca paper trading API. Self-learning, auto-deploying, with kill switch and capital management. 6 strategies, 40+ features. **Multi-user** — anyone can sign up with their own Alpaca credentials.

**Dashboard:** https://stockbott.up.railway.app (login: set via Railway env vars `DASHBOARD_USER` / `DASHBOARD_PASS`)
**GitHub:** https://github.com/<your-username>/alpaca-trading-bot
**Local:** `python3 server.py` → http://localhost:8888

---

## Quick Resume (New Session)

1. Read this file first
2. Local dashboard: `cd "/Users/kevinbell/Alpaca Trading" && python3 server.py` → http://localhost:8888
3. Refresh screener: `python3 update_dashboard.py`
4. To make changes: edit, commit, push to GitHub → Railway auto-deploys

---

## Environment Variables

All secrets in `.env` (gitignored) and Railway variables. No hardcoded defaults.

| Variable | Purpose |
|----------|---------|
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | Alpaca API credentials |
| `ALPACA_ENDPOINT` | `https://paper-api.alpaca.markets/v2` (paper) |
| `ALPACA_DATA_ENDPOINT` | `https://data.alpaca.markets/v2` |
| `NTFY_TOPIC` | `alpaca-trading-bot-kevin` (push notifications) |
| `NOTIFICATION_EMAIL` | `se2login@gmail.com` |
| `DASHBOARD_USER` / `DASHBOARD_PASS` | Basic auth for dashboard |
| `DATA_DIR` | `/data` on Railway (volume mount for persistent storage). Locally unset (defaults to project dir). Holds `users.db`, `users/`, strategy files, `*.json` runtime data. |
| `MASTER_ENCRYPTION_KEY` | 64-char random key used to derive AES-256-GCM encryption key for Alpaca credentials. Required in production (`REQUIRE_MASTER_KEY=1`). |
| `REQUIRE_MASTER_KEY` | Set to `1` to fail-closed at import if `MASTER_ENCRYPTION_KEY` unset. Prevents silent plaintext fallback. |
| `SIGNUP_INVITE_CODE` | If set, new signups must provide matching code. Prevents public account creation. Current: `CDjKmmrQr_x4MKnjPb0fGw`. |
| `SIGNUP_DISABLED` | Set to `1` to block all new signups. |
| `FORCE_SECURE_COOKIE` | Set to `1` to always add `Secure` flag on session cookie (Railway adds automatically via `X-Forwarded-Proto`). |
| `ENABLE_BASIC_AUTH` | Set to `1` to re-enable Basic Auth fallback (disabled by default). |
| `ENABLE_CLOUD_SCHEDULER` | Default `true`. Set to `false` to disable background scheduler (debugging only). |
| `PORT` | `8888` (Railway sets automatically) |

**Account:** PA3N3JCNBP02 ($100k paper, 2x margin)
**Paper trading start:** 2026-04-15

---

## File Structure

### Core
- `server.py` — Dashboard server (localhost:8888 + Railway)
- `update_dashboard.py` — Full market screener (12k+ stocks)
- `PROJECT.md` — This file

### Analysis Modules
- `indicators.py` — RSI, MACD, Bollinger, ATR, Stochastic, VWAP, OBV
- `economic_calendar.py` — FOMC, opex, news events
- `social_sentiment.py` — StockTwits sentiment (free)
- `correlation.py` — Portfolio correlation matrix
- `options_analysis.py` — Options chain utilities (used by wheel_strategy)
- `wheel_strategy.py` — **Full wheel automation** (sell puts → assign → sell calls → repeat). State machine per symbol, all safety rails. Called by cloud_scheduler.run_wheel_auto_deploy and run_wheel_monitor.
- `extended_hours.py` — Pre-market / after-hours trading logic
- `short_strategy.py` — Short selling candidate identification

### Management Modules
- `capital_check.py` — Capital sustainability
- `tax_harvesting.py` — Tax-loss harvesting scanner
- `error_recovery.py` — Orphan position detection + auto-fix
- `notify.py` — Push notifications (ntfy.sh) + email queue
- `learn.py` — Self-learning engine (weekly weight adjustment)
- `update_scorecard.py` — Performance metrics (Sharpe/Sortino)
- `realtime.py` — Fast price poller (10s, for local use)

### Config Files
- `.env` — Local secrets (gitignored)
- `guardrails.json` — Kill switch, daily loss limit, drawdown, cooldowns
- `auto_deployer_config.json` — Auto-deployer settings + short selling toggle
- `accounts.json` — Paper + live account configs
- `strategies/trailing_stop.json` — Base trailing stop template
- `strategies/copy_trading.json` — Copy trading state
- `strategies/wheel_strategy.json` — Wheel strategy state
- `strategies/<strategy>_<SYMBOL>.json` — Per-position files (auto-created)

### Runtime State (gitignored)
- `dashboard_data.json` — Latest screener output
- `scorecard.json` — Running performance metrics
- `trade_journal.json` — Every trade with reasoning
- `learned_weights.json` — Strategy multipliers from learning engine
- `capital_status.json` — Latest capital check
- `notification_log.json` — Notification history
- `email_queue.json` — Pending email notifications

### Infrastructure
- `Procfile` — `python3 -u server.py` (unbuffered for Railway log forwarding)
- `railway.json` — Railway deploy config. `healthcheckPath: /healthz`.
- `requirements.txt` — `cryptography>=42.0.0,<46.0.0` (AES-GCM) + `zxcvbn>=4.4.28,<5.0.0` (password strength). Both degrade gracefully if not installed.
- `manifest.json` — PWA manifest
- `icon-192.png` / `icon-512.png` — PWA icons
- `templates/` — `dashboard.html`, `login.html`, `signup.html`, `forgot.html`, `reset.html`. Loaded once at server import.
- `tests/` — pytest suite (run: `python3 -m pytest tests/`)
- `et_time.py` — shared ET helper. **Never reach for `datetime.now(timezone.utc)`; use `from et_time import now_et`.**
- `constants.py` — SECTOR_MAP + PROFIT_LADDER + keyword lists (single source of truth for all consumers)

---

## 6 Strategies

| # | Strategy | Signal | Stop | Best For |
|---|----------|--------|------|----------|
| 1 | **Trailing Stop** | Strong uptrend + momentum | 10%, trails 5% below peak | Riding trends |
| 2 | **Copy Trading** | Politician disclosures (Capitol Trades) | 10% per position | Insider edge |
| 3 | **Wheel** | High volatility, $10-$50 price | Built into options | Premium income |
| 4 | **Mean Reversion** | 15%+ drop on no bad news | 10%, targets 20-day avg | Oversold bounces |
| 5 | **Breakout** | 20-day high on 2x+ volume | Tight 5% | Explosive moves |
| 6 | **Short Selling** | Bear market only (SPY 20d < -3%) | 8% tight | Bear plays |

---

## Scheduled Tasks (Claude Code)

| Task ID | Schedule | Purpose |
|---------|----------|---------|
| `auto-deployer` | 9:35 AM ET weekdays | Brain: screen, pick, deploy top 2 stocks |
| `strategy-monitor` | Every 5 min, market hours | Universal monitor for all positions |
| `copy-trading-monitor` | 9:35 AM ET weekdays | Scan Capitol Trades, copy politician moves |
| `wheel-strategy-monitor` | Every 15 min, market hours | Manage put/call selling cycle |
| `daily-close-summary` | 4:05 PM ET weekdays | Update scorecard, snapshot, recovery |
| `weekly-learning` | Friday 2 PM PT | Self-learning weight adjustment |

**Note:** Tasks run via Claude Code on user's laptop. For 24/7 cloud execution, see "Cloud Scheduler (Pending)" below.

---

## Dashboard Features

**Header:** Kill switch, auto-deployer toggle, voice control, trading session badge, readiness score
**Top 3 Picks:** Cards with tech indicators (RSI/MACD/bias), social sentiment, 5 strategy score bars, deploy button
**6 Active Strategy Cards:** Status badges, pause/stop buttons, ON/OFF toggle for shorts
**Positions Table:** Close/Sell Half with P&L confirmation modals
**Orders Table:** Cancel with confirmation
**Full Screener:** Top 50 sortable with per-row Deploy
**Short Candidates Section:** Only active in bear markets
**Tax-Loss Harvesting:** Opportunities with replacement suggestions
**Visual Backtest:** Interactive stock selector with equity curve chart
**Readiness Scorecard:** Progress toward going live (80/100 target)
**Economic Calendar Banner:** FOMC/opex alerts
**Strategy Marketplace:** Conservative/Moderate/Aggressive presets + export/import
**Activity Log:** Color-coded with timestamps
**Correlation Warnings:** Sector concentration alerts
**Mobile PWA:** Installable on phone

All financial actions require confirmation modals with plain-English P&L estimates.

---

## Safety & Guard Rails

| Rail | Setting | Enforced By |
|------|---------|-------------|
| Kill switch | Dashboard button | All trading tasks check first |
| Daily loss limit | 3% | Auto-triggers kill switch |
| Max drawdown | 10% | Circuit breaker from peak |
| Max positions | 5 | Blocks new deploys |
| Max per stock | 10% of portfolio | Position sizing |
| Capital check | Before every deploy | capital_check.py |
| Cooldown | 60 min after loss | last_loss_time check |
| Earnings filter | Auto-skip | News API scan |
| Bear market | Shorts only, others pause | market_regime check |
| Short-specific | 48hr cooldown after loss | last_short_loss_time |
| Meme filter | Skip shorts on meme stocks | StockTwits buzz check |

---

## Notifications

Free via ntfy.sh. Install app on phone, subscribe to topic `alpaca-trading-bot-kevin`.

| Type | Priority | When |
|------|----------|------|
| trade | Normal | New position opened |
| exit | Normal | Profit taken |
| stop | High | Stop-loss fired |
| alert | Urgent | Drawdown/correlation warnings |
| kill | Max | Kill switch activated |
| daily | Low | 4 PM ET close summary |
| learn | Low | Weekly learning update |

Important types also queue emails to se2login@gmail.com.

---

## 10 Scoring Improvements + 15 Advanced Features

**Scoring:** Multi-timeframe momentum, relative volume, sector diversification, earnings avoidance, dynamic position sizing, profit-taking ladder, SPY market regime, news sentiment, daily P&L tracking, backtesting

**Advanced:** Technical indicators, real-time monitor, economic calendar, social sentiment, options chain, extended hours, short selling, correlation matrix, mobile PWA, visual backtesting, multi-account, tax-loss harvesting, voice interface, strategy marketplace

---

## Architecture

```
Morning (9:35 AM ET):
  auto-deployer → capital_check.py (safety)
                → update_dashboard.py (screen 12k stocks, ~47s)
                → Pick top 2, deploy via Alpaca API
                → Log to trade_journal.json
                → notify.py (push + email)
                → update_scorecard.py
                → error_recovery.py

During Market Hours:
  strategy-monitor (5 min) → Manage all active positions
  wheel-strategy-monitor (15 min) → Manage options cycle

Market Close (4:05 PM ET):
  daily-close-summary → Snapshot + scorecard + readiness check + summary

Weekly (Friday 2 PM PT):
  weekly-learning → Analyze journal → Adjust weights
```

---

## Cloud Scheduler (LIVE)

`cloud_scheduler.py` runs as a background thread inside server.py on Railway. Bot is fully autonomous 24/7 — laptop not needed.

**Task schedule:**
- Auto-deployer (trailing/breakout/mean-rev): weekdays 9:35 AM ET (top-20 candidate pool with fallback)
- **Wheel auto-deploy: weekdays 9:40 AM ET** (sells cash-secured puts on wheel candidates)
- **Wheel monitor: every 15 min during market hours** (assignment, expiration, buy-to-close)
- Screener: every 30 min during market hours
- Strategy monitor: every 60s during market hours (stops, ladders, targets)
- Friday risk reduction: 3:45 PM ET (scale out of winners before weekend)
- Daily close: weekdays 4:05 PM ET
- Weekly learning: Fridays 5:00 PM ET
- Monthly rebalance: first trading day 9:45 AM ET

**Claude Code tasks are DISABLED** to prevent duplicate trades. Cloud scheduler is the single source of truth.

**Dashboard:**
- "24/7 CLOUD" badge in header with pulse animation when running
- "Scheduler" nav tab → full status panel with task grid, last run times, live log feed
- `/api/scheduler-status` endpoint returns running state + recent_logs

**To disable:** Set `ENABLE_CLOUD_SCHEDULER=false` in Railway env vars, redeploy.

---

## Going Live ($5k Account)

When readiness score hits 80/100 after 30 days:
1. Create live Alpaca account, fund $5k
2. Generate live API keys
3. Update Railway env vars:
   - `ALPACA_ENDPOINT=https://api.alpaca.markets/v2`
   - New live key and secret
4. Update `guardrails.json` to live_mode_settings (max 3 positions, 5% per stock, 1 new/day)
5. Keep paper running in parallel to validate

---

## Common Commands

```
/stock-bot                          # Launch bot skill
"Run trailing stop on NVDA"         # Deploy trailing stop
"Start wheel on SOFI"               # Deploy wheel
"Set up copy trading"               # Start copy trading
"Kill switch"                       # Emergency stop
"What's my P&L?"                    # Check positions
"Check readiness score"             # Ready for live?
"Refresh dashboard"                 # Force screener rerun
```

---

## Recent Major Changes

- **2026-04-16:** Built all 6 strategies, 40+ features, cloud hosting, PWA, voice control
- **2026-04-16:** Removed all hardcoded secrets, scrubbed git history
- **2026-04-16:** Full audit: fixed 57 + 20 + 10 issues across code, configs, UI
- **2026-04-16:** Added short selling as 6th strategy with bear-market gating + ON/OFF toggle
- **2026-04-16:** Interactive backtest with stock selector + plain-language explanation
- **2026-04-16:** Built cloud_scheduler.py — bot now 24/7 autonomous on Railway, laptop not required
- **2026-04-16:** Disabled all Claude Code scheduled tasks to prevent duplicates
- **2026-04-16:** Dashboard Scheduler tab with live task status + log feed
- **2026-04-16 (PM):** Multi-user support complete (auth.py, sessions, password reset, per-user Alpaca creds)
- **2026-04-16 (PM):** Railway volume persistence via DATA_DIR abstraction
- **2026-04-16 (PM):** Screener parallelized (60-80s → 50s)
- **2026-04-16 (PM):** Auto-deployer fallback pool 5→20 picks
- **2026-04-16 (PM):** Header v2 redesign + time format fixes
- **2026-04-16 (PM):** **Settings modal + Admin panel** — full multi-user UI
- **2026-04-16 (PM):** **Wheel strategy full autonomy** — wheel_strategy.py + cloud_scheduler tasks. Sell puts → assign → sell calls → repeat, fully automated with safety rails.
- **2026-04-16 (PM):** 4 parallel forensic audits. Critical finds: cross-user migration leak (r3), trade journal writeback missing (r4 fin), profit ladder non-idempotent (r4 fin), ntfy_topic spoofing (r4 sec), logout CSRF, admin cross-admin pw reset. All fixed.
- **2026-04-16 (PM):** Final hardening commit `e391f79`: AES-256-GCM (`ENCv2:`) for Alpaca credentials, backups now strip credential columns, login rate limit persisted to SQLite `login_attempts` table (survives Railway redeploy), SIGTERM handler, SQLite WAL, `/healthz` endpoint.
- **2026-04-16 (PM):** Round-5 forensic audit (Opus 4.7, commit `0d1f961`): **ET-only policy** across entire codebase via new `et_time.py` helper — no `datetime.now(timezone.utc)` anywhere in runtime code; DST bug in `extended_hours.get_trading_session()` (hardcoded `-4h` broke EST) fixed; `cleanup_expired_sessions` finally wired (was dead code); `admin_audit_log` 90d rotation; `password_resets` GC; reset tokens SHA-256 hashed at rest; `/healthz` staleness check (5-min threshold); profit ladder client_order_id switched UTC-date → ET-date; cooldown parse error now fail-closed; per-user Alpaca circuit breaker (5 fails → 5-min cool-off); `socket.setdefaulttimeout(30)` safety net; email queue overflow WARN; wheel lock hardened; `cryptography<46.0.0` pin; load_json logs malformed files.
- **2026-04-16 (PM):** Round-5.2 decision-item fixes (commit `7c2c013`): SECTOR_MAP consolidated to `constants.py` (single source of truth — 3 consumers now import it); dashboard HTML extracted to `templates/*.html` (**server.py 7508 → 2870 lines**); baseline pytest suite added (`tests/`, 30 tests); HKDF-SHA256 key derivation rolled out as `ENCv3:` with transparent upgrade on login (legacy ENCv2 stays decrypt-only); zxcvbn password strength enforced (score ≥3, ≥10 chars, username/email as user_inputs); new signups get random `alpaca-bot-<12char>` ntfy topics instead of guessable username-based ones; `get_dashboard_data` decomposed into `_resolve_user_paths` / `_fetch_live_alpaca_state` / `_load_with_shared_fallback` / `_load_overlay_files`; diagnostic reports distribution of PLAIN/ENC/ENCv2/ENCv3 rows at startup.

---

## Known Issues / Decisions Pending

1. **PWA icons** — gradient placeholders. Could design proper icons later.
2. **Options API** — wheel strategy falls back to simulation if Alpaca options API unavailable.
3. **Duplicate SECTOR_MAP** — exists in update_dashboard.py and update_scorecard.py. Not a bug, could refactor to shared module.
4. **Short selling auto-deploy** — cloud scheduler logs short candidates but doesn't auto-deploy yet (TODO marker in code). Manual deploy from dashboard works.

---

## First Market Day Expectations

**Date:** 2026-04-16 (tomorrow at bot build time)

| Time (ET) | Expected Event |
|---|---|
| 9:30 AM | Market opens. Cloud scheduler detects via Alpaca /v2/clock |
| ~9:30-9:35 | First screener run (~47 sec, screens 12k+ stocks) |
| 9:35 AM | Auto-deployer fires: capital check → pick top 2 → place market buys → log → notify |
| 9:36+ | Strategy monitor every 60s: places stops as buys fill, manages trailing |
| Every 30 min | Screener refreshes |
| Every 15 min | Wheel strategy check (if active) |
| 4:00 PM | Market closes |
| 4:05 PM | Daily close summary → push + email notification |

User will receive push notifications on phone (ntfy app, topic `alpaca-trading-bot-kevin`) for trade events.
