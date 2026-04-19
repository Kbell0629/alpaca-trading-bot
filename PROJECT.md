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
| `MASTER_ENCRYPTION_KEY` | 64-char random key used to derive the AES-256-GCM encryption key for Alpaca credentials. **Required in every environment** — the app refuses to boot if unset. The old `REQUIRE_MASTER_KEY` toggle has been retired (plaintext fallback is no longer a supported mode). |
| `SIGNUP_INVITE_CODE` | If set, new signups must provide matching code. Prevents public account creation. Current: `CDjKmmrQr_x4MKnjPb0fGw`. |
| `SIGNUP_DISABLED` | Set to `1` to block all new signups. |
| `FORCE_SECURE_COOKIE` | Set to `1` to always add `Secure` flag on session cookie (Railway adds automatically via `X-Forwarded-Proto`). |
| `ENABLE_BASIC_AUTH` | Set to `1` to re-enable Basic Auth fallback (disabled by default). |
| `ENABLE_CLOUD_SCHEDULER` | Default `true`. Set to `false` to disable background scheduler (debugging only). |
| `LOG_LEVEL` | Root logger level for the JSON formatter (`DEBUG`/`INFO`/`WARNING`/`ERROR`). Defaults to `INFO`. Set to `DEBUG` for noisier Railway logs during a triage session. |
| `PORT` | `8888` (Railway sets automatically) |
| **Round-11 factor modules** | |
| `GEMINI_API_KEY` | Google Gemini 1.5 Flash for LLM news sentiment (set). Fallback order: Gemini → Groq → OpenAI → Anthropic. |
| `OPENAI_API_KEY` / `GROQ_API_KEY` / `ANTHROPIC_API_KEY` | Alternative LLM providers; set to switch from Gemini. |
| `EDGAR_USER_AGENT` | SEC EDGAR User-Agent header for insider-signal polling (recommended: `alpaca-bot/1.0 yourname@example.com`) |
| `SMART_ORDERS` | `1` (default) enables limit-at-mid with market fallback. Set `0` to use plain market orders. |
| `SMART_ORDER_TIMEOUT` / `SMART_MAX_SPREAD` | Tunable: 90s and 0.5% defaults. |
| **Round-11 monitoring** | |
| `SENTRY_DSN` | Sentry error tracking DSN. No-op if unset. See `docs/MONITORING_SETUP.md`. |
| **Round-11 off-site backup** | |
| `S3_BACKUP_BUCKET` + `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | Push daily backup to S3. |
| `B2_BUCKET` + `B2_KEY_ID` + `B2_APPLICATION_KEY` | Push to Backblaze B2 (cheapest option). |
| `GITHUB_BACKUP_TOKEN` + `GITHUB_BACKUP_REPO` | Push to GitHub release assets (free, no signup beyond GitHub). |

**Account:** PA3N3JCNBP02 ($100k paper, 2x margin)
**Paper trading start:** 2026-04-15

---

## File Structure

### Core
- `server.py` — Dashboard server (localhost:8888 + Railway)
- `update_dashboard.py` — Full market screener (12k+ stocks)
- `PROJECT.md` — This file
- `CLAUDE.md` — **Session-resume context for Claude Code**. Read first on every new session. Changelog + active state + audit playbook.
- `logging_setup.py` — Central JSON-logger init. Monkey-patches `builtins.print` so legacy prints auto-emit JSON envelopes + timestamps. Sentry LoggingIntegration auto-captures via stdlib.
- `trade_journal.py` — Trade-journal trim + archive management. Daily 3:15 AM ET flock-serialized trim moves closed trades >2y old into `trade_journal_archive.json`.
- `pyproject.toml` — ruff + pytest + coverage config (round-12). Coverage ratchet floor at 15% (measured ~19%); nudge up as gaps close.

### Analysis Modules
- `indicators.py` — RSI, MACD, Bollinger, ATR, Stochastic, VWAP, OBV
- `economic_calendar.py` — FOMC, opex, news events
- `social_sentiment.py` — StockTwits sentiment (free)
- `correlation.py` — Portfolio correlation matrix
- `options_analysis.py` — Options chain utilities (used by wheel_strategy)
- `wheel_strategy.py` — **Full wheel automation** (sell puts → assign → sell calls → repeat). State machine per symbol, all safety rails. Called by cloud_scheduler.run_wheel_auto_deploy and run_wheel_monitor.
- `extended_hours.py` — Pre-market / after-hours trading logic
- `short_strategy.py` — Short selling candidate identification

### Round-11 Factor Modules (added 2026-04-18 weekend expansion)
These sit between screener and auto-deployer. Each is self-contained with 24h caches and fails-soft on yfinance errors. See `project_status.md` for full details.
- `risk_sizing.py` — ATR(14) + volatility-aware stop sizing + vol-parity position multiplier. Screener attaches `atr_pct` to each pick; `cloud_scheduler` computes ATR-based stops in place of fixed 10%.
- `market_breadth.py` — % of S&P 500 top-100 above their 50dma. `run_auto_deployer` blocks breakout + PEAD deploys when breadth < 40%. Cached in `DATA_DIR/market_breadth.json`.
- `factor_enrichment.py` — Relative Strength (3m/6m vs SPY) + SPDR sector-ETF rotation ranking. `apply_factor_scores()` mutates pick dicts with `rs_composite`, `sector_multiplier`, etc.
- `quality_filter.py` — yfinance fundamentals (ROE / D-E / FCF) → quality tier A-D. Plus bullish-news keyword scanner (upgrade, beats, FDA, etc). Cached per-symbol in `quality_cache.json`.
- `iv_rank.py` — Historical volatility rank as free IV rank proxy. `wheel_strategy.open_short_put` hard-blocks if `hv_rank < 30`. Cached in `iv_rank_cache.json`.
- `options_greeks.py` — Black-Scholes put/call delta (stdlib math.erf). `wheel_strategy.score_contract` targets 0.25-delta puts instead of arbitrary % OTM.
- `yfinance_budget.py` — Shared 30req/60s rate limiter + exp-backoff retry + circuit breaker across all factor modules. `yf_download`, `yf_ticker_info`, `yf_history` wrappers.

### Round-11 Expansion Modules (added 2026-04-19, items 1-20)
Six additional modules + integrations layered on top of the factor batches:
- `tax_lots.py` — FIFO cost-basis tax lot accounting + Form 8949 CSV export + wash-sale detection. `/api/tax-report` + `/api/tax-report.csv` endpoints render "Tax Report" panel.
- `smart_orders.py` — `place_smart_buy` / `place_smart_sell` — limit-at-mid with 90s timeout + market fallback. Saves 0.1-0.5% slippage per round-trip. Wired into `cloud_scheduler.run_auto_deployer`. Disable with `SMART_ORDERS=0`.
- `offsite_backup.py` — Push daily backup archive to S3 / Backblaze / GitHub. Auto-detects destination from env vars; no-op if none set. Called from `backup.create_backup` after local archive lands.
- `premarket_scanner.py` — 8:30 AM ET scan of top-100 liquidity names for >2% gaps + >50K premarket volume. Saves `premarket_picks.json` which the 9:45 AM deployer prioritizes.
- `insider_signals.py` — SEC EDGAR Form 4 polite RSS poller (1 req/sec, 24h cache). Cluster buys (3+ filers in 30d) get +10..+15 bonus.
- `llm_sentiment.py` — Multi-provider LLM news scoring (Gemini 1.5 Flash → GPT-4o-mini → Groq → Claude Haiku). Detected from env var; 1h cache. ~$0.01/day at current volume.
- `multi_timeframe.py` — Daily + weekly trend confirmation for breakouts/PEAD. `confirm_breakout()` adds +10/-10/+5 score bonus.
- `news_websocket.py` — Alpaca real-time news stream (optional, needs `websocket-client`). Background thread; LLM-scores headlines and alerts on |score| >= 6.
- `portfolio_risk.py` — Beta-adjusted exposure regimes, drawdown-adaptive sizing, correlation gates.
- `observability.py` — Sentry SDK init + `critical_alert()` multi-channel helper (Sentry + ntfy + email). Auto-init via `SENTRY_DSN`.

### Static assets (`static/`)
- `manifest.json` — PWA manifest (installs dashboard as a standalone app on phones)
- `service-worker.js` — Offline cache + push-notification handler
- `icon.svg` — Vector app icon

### Docs (`docs/`)
- `MONITORING_SETUP.md` — 2-minute setup guide for Sentry (DSN pre-provisioned), UptimeRobot, and ntfy critical alerts
- `legal/TERMS_OF_SERVICE.md` — SaaS ToS draft (needs lawyer review before publishing)
- `legal/PRIVACY_POLICY.md` — GDPR+CCPA privacy draft
- `legal/DISCLAIMER.md` — Trading disclaimer (not investment advice)
- `legal/README.md` — Lawyer review checklist + business setup costs

### Live-trading infrastructure (2026-04-19 LIVE batches)
All controlled from Settings modal — no env var changes required:
- **auth.py migration**: added `alpaca_live_{key,secret}_encrypted`, `live_mode`, `live_enabled_at`, `live_max_position_dollars`, `track_record_public`, `scorecard_email_enabled` columns to `users` table. Idempotent ALTER TABLE on boot.
- **auth.get_user_alpaca_creds(user_id)**: returns LIVE endpoint + creds when `user.live_mode == 1`, paper otherwise. Callers (scheduler, handlers) don't need to know which mode is active.
- **auth.save_user_alpaca_creds(paper_key=, paper_secret=, live_key=, live_secret=)**: partial updates.
- **auth.set_live_mode(user_id, enabled, max_position_dollars=)**: toggle helper.
- **Routes (server.py)**:
  - `/api/test-alpaca-keys` — POST {api_key, api_secret, mode} — validates against Alpaca before saving
  - `/api/save-alpaca-keys` — POST {api_key, api_secret, mode} — tested + encrypted save
  - `/api/toggle-live-mode` — POST {enable, confirm_understand_risks, live_max_position_dollars, override_readiness}
  - `/api/toggle-track-record-public` — POST {enable}
  - `/api/toggle-scorecard-email` — POST {enable}
  - `/track-record/<user_id>` — PUBLIC, no auth, only renders if `track_record_public=1`
  - `/api/export/{positions,orders,trades,picks,tax-lots}.csv` — session auth required
- **cloud_scheduler.run_auto_deployer**: enforces `live_max_position_dollars` cap when `user.live_mode=True`.
- **cloud_scheduler.run_scorecard_digest(user)**: 4:30 PM ET weekdays, opt-in via `scorecard_email_enabled`. Uses `notification_templates.scorecard_digest`.
- **templates/track_record.html**: public, read-only, Chart.js equity curve + 8 stat cards + strategy breakdown. CSP-locked, no PII.

### Handler additions (handlers/auth_mixin.py)
- `handle_test_alpaca_keys(body)` — dry-run /account call
- `handle_save_alpaca_keys(body)` — verify-then-save
- `handle_toggle_live_mode(body)` — readiness gate + audit log + critical_alert
- `handle_toggle_track_record_public(body)` / `handle_toggle_scorecard_email(body)`

### Management Modules
- `capital_check.py` — Capital sustainability
- `error_recovery.py` — Orphan position detection + auto-fix
- `notify.py` — Push notifications (ntfy.sh) + email queue
- `learn.py` — Self-learning engine (weekly weight adjustment). Round-11: 90-day walk-forward + Sharpe-weighted multipliers (was all-time + win-rate only).
- `update_scorecard.py` — Performance metrics (Sharpe/Sortino)
- `notification_templates.py` — Rich email templates (position opened, target hit, kill switch, etc.)
- `email_sender.py` — Gmail SMTP queue drainer, runs every 60s via scheduler

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
- `handlers/` — `auth_mixin.py`, `admin_mixin.py`, `strategy_mixin.py`, `actions_mixin.py`. DashboardHandler inherits from all four via MRO. Add new endpoints to the matching mixin, not to server.py.
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

> **Full 30-day review + go-live framework lives in memory:**
> `~/.claude/projects/-Users-kevinbell-Alpaca-Trading/memory/thirty_day_review.md`.
> That file has the GREEN/YELLOW/RED outcome thresholds, the
> deferred-feature revisit priority, and a detailed live-migration
> checklist. Load it first when the 30-day window ends (2026-05-16)
> or when the user asks about going live.

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

- **2026-04-18/19:** **Round-12 forensic audit sweep** — 15 PRs merged to `main`, 110+ new regression tests. Five parallel audits (security, database, trading logic, UI/UX/mobile, test coverage). Shipped: (1) complete float→Decimal migration across all 5 money-math phases (tax_lots, update_scorecard, portfolio_risk, wheel_strategy, smart_orders + calc_position_size) with 80+ parity tests; (2) atomic kill-switch abort via `threading.Event`; (3) password-reset TOCTOU fix via atomic UPDATE; (4) login token-bucket rate limit; (5) trade-journal auto-trim daily 3:15 AM ET + archive file; (6) structured JSON logging with `logging_setup.py` module; (7) PR-template ruff + coverage ratchet in CI; (8) full XSS hardening + modal focus trap + WCAG-AA colour tokens + iPhone-SE-responsive modals; (9) **revived the `portfolio_risk` beta-exposure safety rail** which had been silently disabled in prod since round-11 — `run_auto_deployer` hit `NameError` on every call (ruff F821 caught it); (10) wheel stock-split anomaly guard (freezes state when `share_delta >= 2*expected_delta` rather than record a wrong cost basis). Session details in `CLAUDE.md`; PR-by-PR list in `IMPLEMENTATION_STATUS.md`.
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
- **2026-04-16 (PM):** GitGuardian alert scrubbed (`349eb68`) — real paper-account API key had been used as a literal test value in `test_encrypt_decrypt_roundtrip_encv3`; replaced with `"FAKE-TEST-KEY-NOT-A-REAL-SECRET"`. Paper-account only, rotation is optional.
- **2026-04-16 (PM):** Round-6 audit (commit `5e63488`): `_cb_state` + `_api_cache` now properly locked (two real races under `ThreadingHTTPServer`); circuit-breaker trips and scheduler-loop fatal exceptions now push-notify the user (previously silent); `/healthz` null/lock-safe; partial entry fills now reconcile `initial_qty → filled_qty` so profit ladder sizes correctly; `PRAGMA wal_checkpoint` before every `conn.backup()`; per-user directories chmod 0o700; `admin_audit_log` GC batched to avoid long write locks; `/api/version` endpoint added; password fields get `autocomplete` attrs, Alpaca credential fields get `autocomplete="off" spellcheck="false"`; 10 new regression tests added (**total 40, all passing**).
- **2026-04-16 (PM):** Round-6.5: decomposed `DashboardHandler` (2320 lines) into 4 mixin files — `handlers/auth_mixin.py`, `admin_mixin.py`, `strategy_mixin.py`, `actions_mixin.py`. server.py 2927→1573 lines. Test count 41.
- **2026-04-16 (PM):** Round-7 audit (commit `95475a9`): caught **P0 regression from round-6.5** — the mixin extraction script left undefined global references (BASE_DIR, load_json, now_et, re, subprocess, threading, etc.) in every mixin file. Any HTTP request hitting those paths would NameError at runtime. The 41 prior tests all passed because they only checked method EXISTENCE, not body resolution. Fixed via `import server` late-binding + prefixed references + added missing stdlib imports. New AST-walking test (`test_mixin_files_have_no_undefined_names`) catches this class of bug. Also: strategy-name normalization in scorecard (fixes silent per-strategy undercount), dashboard kill-switch debounce + error-response handling, refreshData concurrent-fetch debounce. Test count 42.
- **2026-04-16 (PM):** Round-7 HOTFIX chain (`af7db0a` + `7708af3`): the `import server` pattern crashed on Railway because server.py is launched as `__main__` (not `server`). Replaced with a lazy `_ServerProxy` that resolves via `sys.modules` at first attribute access. Added `tests/test_boot.py` — 3 subprocess-launched tests that detect broken import topologies before deploy. Proven to catch the regression class by temporarily reintroducing the bug and watching the test fail with the exact ImportError traceback. Test count 45.
- **2026-04-16 (PM):** Round-7 mop-up (`e355fa1`): SIGTERM handler silently swallowed `NameError: stop_scheduler not defined` since the feature shipped. One-word import fix — Railway redeploys now actually get a clean scheduler stop instead of SIGKILL.
- **2026-04-16 (PM):** Round-8 to-do-list burndown (`a90ff83`): closed **11 items** from the user's follow-up list — #1 legacy ENC cipher removed (Railway-confirmed 0 rows), #2 wheel dashboard card migrated to /api/wheel-status, #3 strategy-file locking for non-wheel strategies, #4 news signals wired into auto-deployer, #5 per-strategy confidence in learn.py, #6 E2E integration tests (4 subprocess tests), #7 client-side zxcvbn password meter, #8 Test-Connection error hardening, #9 task-staleness watchdog + after-hours heartbeat, #10 market-breadth div/0 verified safe, #11 dashboard addLog now renders ET. **8 remaining items explicitly classified as `DECISION: DEFERRED` feature requests in BURN_DOWN.md — NOT bugs — so future audits shouldn't re-surface them.** Test count 49.
- **2026-04-16 (PM):** Live-day fixes (`b9e8264`, `1683791`, `b5348e1`, `e70bcf2`, `21baa5b`, `bc8d765`): stale "MARKET OPEN" badge (trading_session now live-computed per request), heatmap flicker (loadHeatmap called post-renderDashboard), daily-close-skip-after-restart (persistence + 4hr late-tolerance window + wider outer-gate hour 16-19), `/api/force-daily-close` admin trigger, screener scoring guards (MIN_VOLUME 100k→300k + breakout_score × 0.5 when volatility > 25%), Days Tracked Math.round→Math.floor off-by-one, heatmap Best/Worst Day rendering with 1-day data.
- **2026-04-16 (PM):** Round-9 final audit + mobile polish (`c3f8276`): subprocess scorecard-path leak fixed (was writing to shared /data/scorecard.json instead of per-user path — caused today's dashboard staleness), TOCTOU races in should_run_interval/should_run_daily_at (lock was acquired after read), `_refresh_cooldowns` unlocked compare-and-set, `handle_force_daily_close` mutated `_last_runs` without lock, `pnlClass(0)` returned green instead of neutral. Mobile: readme/settings/deploy modals full-screen on phones, screener + positions tables horizontal-scroll with min-widths, 40px touch targets, 16px form inputs (iOS auto-zoom fix), iPhone SE (<380px) ultra-narrow tier. Test count 54.

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
