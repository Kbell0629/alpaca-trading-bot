# Implementation Status — 20 Profit Features

Tracker for the 20 profit-enhancement features proposed after the initial build. Updated 2026-04-19.

## Rounds 14-17 audit + closeout (2026-04-19, second-half session)

PRs #28-31. Closes out every deferred item from rounds 12-13 plus the
new findings from a fresh top-to-bottom audit.

* **#28 — Round-14 audit fixes**: 8 parallel Explore agents found 7
  real bugs. Biggest: `observability.critical_alert` email path
  silently broken since round-11 (kill-switch trip never emailed
  operator). Plus CSRF cookie clearing on logout, Sentry quota guard,
  notify queue safety, ET-timezone counter, `track_record` HTML escape.
* **#29 — Round-15 closeout**: 11 deferred items including the
  smart_orders partial-fill blended cost basis bug (PnL drift ~0.8%
  per wheel cycle), Alpaca 401/403 auto-detect with per-day alert
  dedup, daily-loss alert wired into `critical_alert`, per-user
  isolation extracted to `per_user_isolation.py` with pinning tests,
  capital_check fallback ladder testable, dead Stock Watcher removed.
* **#30 — Round-16 architectural followup**: `state_recovery.py`
  boot-time validator (wheel + journal vs Alpaca positions); 36 new
  tests across previously-uncovered strategy modules.
* **#31 — Round-17 architectural refactor**: cloud_scheduler.py
  3800-LOC monolith split — Alpaca API plumbing extracted to
  `scheduler_api.py` with full backwards-compat re-exports.

* **#33 — Round-19 final self-audit polish**: audited the code I
  wrote in rounds 14-17 since nobody else had reviewed it; found
  `scheduler_api` DELETE+PATCH missing the rate-limit gate, and
  `options_analysis.analyze_wheel_candidates` crashing on empty-string
  strike_price. Both fixed + 13 new options tests. CI coverage floor
  bumped 15% → 20% (measured 25.4%).

**Test suite:** 229 → 423 passing (+194 over rounds 12-19).
**Coverage:** floor 20%, measured 25.4%.

See `GO_LIVE_CHECKLIST.md` for the pre-live gating list.

---

## Round-13 audit + cleanup (2026-04-19)

Follow-on to the round-12 sweep. 7 PRs: #17 (handler mixin tests),
#18 (scheduler cb-reset bug + 23 tests), #19 (smart_orders full-flow
tests), #20 (wheel stock-split auto-resolve), #21 (yfinance retry +
`_detect_split_since` exception scope), #22 (frontend/security bundle:
README XSS, API-key mask, WCAG contrast, iOS zoom, HSTS, Sentry PII
scrub, auth_mixin sanitization), #23 (math + peripheral: `iv_rank`
telemetry, news score cap, social recency, LLM malformed flag,
FOMC 2027, capitol hard-fail, notify DLQ keep-in-queue).

**Most consequential find:** `_cb_blocked()` was popping the initial
`{fails:N, open_until:0}` state on every non-open check, silently
resetting the circuit-breaker counter. Before PR #18 the breaker had
never tripped in production despite yfinance failures.

**Test suite:** 229 → 328 passing (+99 tests across round-12 + round-13).

See `GO_LIVE_CHECKLIST.md` for the pre-live gating list.

---


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
- **Screener optimized 2026-04-16** — 60-80s → 48-51s locally via parallel batch fetching (6 workers), parallel historical bars, parallel sector ETFs, sector rotation cached 1hr, ENRICH_TOP_N 100→50, parallel news/social sentiment.
- **Railway volume persistence 2026-04-16** — `DATA_DIR` abstraction across all runtime code. Railway volume `web-volume` mounted at `/data` via `railway volume add --mount-path /data`. `DATA_DIR=/data` env var set. users.db, users/, strategies/, and all runtime JSON now persist across redeploys. Solved ephemeral filesystem wipeouts.
- **SOXL orphan recovery 2026-04-16** — First-day trade (SOXL 117@$85.11, stop@$76.60) lost strategy file due to Railway redeploy before volume was set up. `recover_soxl.py` script rebuilds strategy file from live Alpaca position data.
- **Dashboard per-user picks fix 2026-04-16** — After multi-user + volume migrations, screener wrote to `users/{id}/dashboard_data.json` but dashboard read from shared path → showed 0 picks. Fixed: `get_dashboard_data(user_id=...)` reads per-user path first.
- **Force Deploy button 2026-04-16** — New `/api/force-auto-deploy` endpoint + ⚡ Force Deploy button in dashboard header. Bypasses once-per-day lock so user can trigger a full auto-deploy cycle on demand. Guardrails still apply.
- **Auto-deployer candidate pool 5→20 2026-04-16** — Bot was giving up when top 5 picks were blocked by guardrails. Now evaluates up to top 20 picks (configurable via `auto_deployer_config.candidate_pool_size`). Each skip logged with reason ("trying next pick"), and final summary emits full fallback chain + notifies user when all candidates blocked.
- **Dashboard header v2 2026-04-16** — Two-row layout: brand + action icons (Force Deploy/Voice/Help/Refresh/Kill Switch/User) on row 1; status chips (session, scheduler, regime, readiness, auto-deployer toggle, countdown, time) on row 2. Sticky with backdrop blur. Mobile-responsive.
- **Session chip bug fix 2026-04-16** — Header showed "CLOSED" during market hours because `extended_hours.py` returns `'market'` but chip only recognized `'open'/'market_open'/'regular'`. Added `'market'` to matching set.
- **Time format fix 2026-04-16** — "Updated 14:51:28" (UTC 24hr) → "Updated 10:51:28 AM ET" (12-hour Eastern) via `toLocaleTimeString` with `timeZone: America/New_York`.
- **Settings modal + Admin panel 2026-04-16** — Replaced "coming soon" alert with full 5-tab settings modal (Profile / Alpaca API / Notifications / Password / Danger Zone). Includes Test Connection button against `/api/account`, live endpoint confirmation warning, empty-field = keep-existing semantics. Admin-only Manage Users dropdown shows all users, deactivate/reactivate/force-reset-password with last-admin lockout prevention. New endpoints: `/api/delete-account`, `/api/admin/users`, `/api/admin/set-active`, `/api/admin/reset-password`.
- **Wheel strategy full autonomy 2026-04-16** ⭐ — `wheel_strategy.py` (~900 lines) implements complete state machine: sell cash-secured puts → handle assignment → sell covered calls → handle called-away → repeat. Cloud scheduler runs `run_wheel_auto_deploy` at 9:40 AM weekdays + `run_wheel_monitor` every 15 min. Safety rails, per-symbol file locking, `_wheel_deploy_in_flight` dedup, assignment detection via share-delta vs baseline (not presence check), HISTORY_MAX=500 cap.

## Round-12 Audit Sweep (2026-04-18 / 2026-04-19, 15 PRs)

Comprehensive forensic sweep across security, DB/concurrency, trading
logic, UI/UX/mobile, and test coverage (5 parallel Explore agents).
15 PRs merged; 110+ new regression tests; full details in `CLAUDE.md`.

**Headline find**: `portfolio_risk` beta-exposure safety rail had been
silently disabled in production since round-11. `run_auto_deployer`
referenced `factor_bypass` / `existing_positions` / `portfolio_value`
before they were defined; every call hit `NameError`, swallowed by the
outer try/except. Ruff F821 surfaced it in PR #15. Now live.

### PR-by-PR summary

| PR | Subject | Notable artifacts |
|---|---|---|
| #2 | Sentry exception wiring + `MASTER_ENCRYPTION_KEY` mandatory | `observability.capture_exception` routed from every catch-all; PLAIN-fallback retired in auth.py |
| #3 | Structured logging (JSON) + `/api/version` dynamic + a11y contrast + SRI prereqs | `logging_setup.py` new module, `__version__="0.11.0"`, `--green-text` / `--red-text` WCAG-AA tokens |
| #4 | SRI hashes pinned on CDN scripts | integrity="sha384-..." on Chart.js / marked / zxcvbn |
| #5 | Trade journal trim + structured-print shim + test cleanup | `trade_journal.py` new; 3:15 AM daily trim; `builtins.print` monkey-patch → logger |
| #6 | Decimal migration phase 1 (`tax_lots.py`) | `_dec()` + `_to_cents_float()` helpers, 24 new tests |
| #7 | Login token-bucket rate limit (BURST=10, REFILL=0.2/s) | `auth._login_bucket_consume`, 13 new tests |
| #8 | Decimal phase 2 (`update_scorecard.py`) | profit_factor + strategy_breakdown accumulators Decimal-internal, 21 tests |
| #9 | Decimal phase 3 (`portfolio_risk.py`) | portfolio_beta + beta_adjusted_exposure Decimal-weighted, 15 tests |
| #10 | Decimal phase 4 (`wheel_strategy.py`) | premium + cost-basis + stock-PnL accumulators Decimal; 52-cycle + 250-random-cycle fuzz (39 tests) |
| #11 | Decimal phase 5 (`smart_orders` + `calc_position_size`) — FINAL | 30k fuzz inputs; ≤1-share divergence; 10 tests |
| #12 | Password-reset TOCTOU + session IP norm + `capital_check` fallback ladder + coid entropy 6→12 + modal responsive + SW JSON + session-expiry toast + aria-label critical buttons | 9 regression tests (incl. 2-thread TOCTOU race) |
| #13 | XSS hardening on error paths + modal focus trap + forgot-password constant-time + Chart.js cleanup review + PWA icon refs | focus-visible CSS, prefers-reduced-motion, scroll-hint fade |
| #14 | Kill-switch `threading.Event` atomic abort + trim `fcntl.flock` + wheel anomalous-share-delta guard (stock-split safety) | 7 tests incl. cross-thread abort + 2:1 split scenario |
| #15 | CI tooling (`ruff check .`, `--cov-fail-under=15`) + **4 latent bugs** (beta-exposure gate dead code, 3× bare `user_data_dir(user)` NameErrors, loop-capture lambda) | `pyproject.toml` new; 4 regression tests incl. AST-walk guard |

### Money-math migration — complete

All five phases of `docs/DECIMAL_MIGRATION_PLAN.md` merged:
`tax_lots.py` → `update_scorecard.py` → `portfolio_risk.py` →
`wheel_strategy.py` → `smart_orders.py + calc_position_size`. Every
money accumulator is now Decimal-internal; JSON boundary unchanged.

### Operational follow-ups (user TODOs)

- Rotate Sentry DSN (old one in git history; see `docs/MONITORING_SETUP.md`)
- Generate PNG icons for PWA (sandbox can't; `manifest.json` currently
  references single SVG which works on Chrome / Edge but not iOS)
- Run `bash scripts/compute_sri.sh` locally → paste the three
  integrity hashes into the 5 `<script>` tags

### Test coverage — known gaps

- `cloud_scheduler.py` (3800 LOC, 4 tests) — rate limiter, full tick,
  webhook paths
- `handlers/*.py` (2000 LOC, 0 unit tests) — only E2E boot smoke
- `smart_orders.place_smart_buy` full flow (timeout → cancel → settle
  → market fallback) — only the pure `_compute_limit_price` is
  parity-tested
- Strategy modules with zero tests: `pead_strategy`, `short_strategy`,
  `earnings_play`, `insider_signals`, `options_flow`, `options_analysis`

CI coverage floor is **15%** (measured ~19%). Ratchet up as gaps close.

---

## Forensic Audits (4 rounds, 2026-04-16 PM)

### Round 1 — Auth + API + multi-user isolation
Found and fixed: shared file storage across users, SSRF via alpaca_endpoint, unauthenticated signup, Basic Auth brute-forceable, MASTER_KEY silent plaintext fallback, session invalidation, Secure cookie flag, esc() quote escaping, username regex enforcement, XSS in admin panel, wheel file locking, history cap.

### Round 2 — P2 cleanup
PBKDF2 200k → 600k (versioned hash, transparent rehash on login), DST via zoneinfo, CSRF double-submit cookie + global fetch wrapper, error message sanitization, reset URL via stdin (not argv), wheel malformed file warning, Friday risk reduction strategy-file update, auto-deployer N+1 fix.

### Round 3 — UX + edge cases + E2E flows
Critical catch: my own "Auto Deploy OFF fix" introduced cross-user migration leak — friend signing up would inherit Kevin's `auto_deployer_config.json` with `enabled:true` and strategy files. **Would have caused real financial harm.** Fixed: migration restricted to user_id=1. Also fixed: get_dashboard_data fell back to shared picks; learn.py bypassed per-user paths; wheel open_short_put race with Force Deploy; Force Deploy cleared daily lock; nav Settings tab pointed to wrong section; --blue CSS var undefined; Scheduler panel stuck loading; Kill switch didn't close options; market clock fate-shared users[0]; /api/refresh unrate-limited; correlation_id never surfaced.

### Round 4 — Four parallel deep-dives
**Financial math**: `record_trade_close()` helper wired into every exit path (stop fill, target hit, short cover, max-hold, mean-rev target). Previously win_rate/Sharpe/readiness/learning were all stuck at 0 forever because journal entries never flipped to closed. Profit ladder uses `client_order_id` for idempotency (double-sell bug on transient errors fixed). daily_starting_value set once per ET date. Backtest uses OHLC with intraday LOW for stop detection (not daily close).

**Client JS**: fmtMoney guards NaN, voice command guards null price, jsStr() helper for onclick user data, Escape key closes any modal, click-outside closes any modal, 401 response stops countdown + redirects to /login.

**Operational**: SIGTERM handler (stop scheduler + shutdown server cleanly on Railway redeploy), SQLite WAL mode + busy_timeout, `/healthz` endpoint wired to `railway.json.healthcheckPath`, `python -u` in Procfile + railway.json, Alpaca retry+backoff (0.5s/1s/2s on 5xx + timeouts), memory caps on `_LOGIN_ATTEMPTS` + `_api_cache`.

**Security**: ntfy_topic regex validation, admin panel masks ntfy_topic, admin can't reset another admin's password, logout moved to POST+CSRF, login timing-safe (dummy PBKDF2 on user-not-found), security headers (X-Frame-Options DENY, CSP, Referrer-Policy, Permissions-Policy), users.db chmod 0600, tar extract filter, backup concurrent lock, Alpaca error responses sanitized.

### Round 4 cleanup (final) — commit `e391f79`
- **AES-256-GCM** replaces custom HMAC stream cipher. `cryptography>=42.0.0` pip dep. Ciphertext prefix `ENCv2:`. Legacy `ENC:` still decrypts; transparent upgrade on next login.
- **Backup archives strip Alpaca credentials** — admin can't extract keys offline via backup download. Live DB keeps them for runtime decrypt.
- **Login attempts persisted to SQLite** (`login_attempts` table) — brute-force lockout survives Railway redeploys. 24hr retention, opportunistic GC.

**UI fixes Day 1 PM**: Timeline chronological ordering, scroll offset for sticky header, dashboard schedule matches actual scheduler times, Settings tab renamed to Templates + new ⚙️ Account tab opens modal, backtest dropdown shows all top-50 picks with on-demand `/api/compute-backtest` endpoint.

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
