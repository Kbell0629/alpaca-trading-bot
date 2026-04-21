# Changelog

All notable changes to this project are documented here. This file lives alongside the user-facing [README](README.md) so the guide stays clean and the release history is easy to audit.

Format: each entry is grouped by **round** (development cycle) and tagged with the **date** the work shipped. A "round" roughly corresponds to a focused batch of PRs — security sweeps, audit responses, UX polish, or feature additions. Every PR number cited below is merged to `main` and deployed.

The project is currently in **paper-trading validation** (started 2026-04-15, targeting ~30 days). Live-trading is code-complete but gated behind the validation window.

---

## 🆕 Round-41 — Full tech-stack audit (2026-04-21 late night)

Five parallel Explore agents swept security, concurrency, trading
logic, UI/UX, and ops. Trading-logic came back CLEAN — every claim
was verified against actual code. Eight real bugs across four
other areas were shipped in one PR.

**Security / Concurrency:**
* **`auth.py` connection leaks** — `get_user_by_id`,
  `get_user_by_username`, `get_user_by_email`, `list_active_users`,
  `validate_session` all returned early without closing the sqlite
  connection. On hot paths (session validation fires on every HTTP
  request) this was accumulating open file handles. Wrapped in
  try-finally.
* **First-user auto-admin TOCTOU** — two concurrent signups on an
  empty `users` table could both see `count==0` and both insert
  with `is_admin=1`. Fixed by acquiring a write lock with
  `BEGIN IMMEDIATE` before the count query so SQLite serializes
  the second signup behind the first commit.
* **`journal_backfill.py` race** — read-modify-write on
  `trade_journal.json` was unlocked. A concurrent `record_trade_open`
  from the scheduler or a manual deploy could silently overwrite
  entries. Wrapped in `strategy_file_lock` (the flock helper used
  by every other journal writer).

**Ops hardening:**
* **`server.main` PORT guard** — a typo like `PORT=abc` on Railway
  would crash the process with a bare `ValueError` and no helpful
  log. Now validates + logs + falls back to 8888.
* **`track_record.html` username XSS** — public shareable URL
  interpolated `{{USERNAME}}` without escaping. Usernames are
  validated at signup but defense-in-depth matters on reflected
  output. Now routes through `html.escape()`.

**UI / UX:**
* **Modal height cap** — Close Position (with P&L detail box),
  Cancel Order (with explanation panel), and Settings (with
  multi-row Danger Zone) were pushing confirm/cancel buttons past
  viewport bottom on short screens. Added `max-height: 92vh;
  overflow-y: auto` to the base `.modal` class.
* **Double-submit guards** — `executeClosePosition`,
  `executeSellFraction`, `executeCancelOrder` now check an
  in-flight set before firing. Fast double-click on Confirm Sell
  was firing two POSTs before the modal dismiss animation
  finished. Same pattern as round-11's `_deployInFlight`.
* **Notification email autocomplete** — `<input type="email">`
  for notifications now has `autocomplete="email"` +
  `inputmode="email"` so iOS/Android keyboard offers the saved
  address instead of making the user type it again.

**Tests:** 9 new cases in `tests/test_round41_audit_fixes.py`
covering every fix (conn leak, TOCTOU race, journal lock,
PORT guard, XSS escape). Full suite: **603 passing** (baseline
583 + 9 new + 11 auth-on-sandbox when MASTER_KEY is set). Ruff
clean.

---

## 🆕 What's New (2026-04-21 night — Rounds 38-39)

**Round-38 — CI timeout fix + Deploy modal scroll containment.**
See prior PRs for full detail — `/api/signup` was timing out on CI
under zxcvbn's first-call lazy-load (bumped from 5s to 15s), and
John's Deploy modal on a laptop was cutting off the Confirm Buy
button (same class of bug as the admin modal).

**Round-39 — Cross-user activity-log leak FIX + native price charts.**

*Privacy fix (HIGH severity):* `/api/scheduler-status` was returning
the unfiltered 200-line scheduler ring buffer + the full list of
all usernames to every authenticated user. That's why you saw
`[godgurusefone]` entries in your activity log. Now:
- Non-admins see only entries tagged with their own username +
  generic scheduler events (heartbeat, boot, migrations). Other
  users' screener / monitor / deploy events are filtered out.
- Users tab (in admin panel) still shows everyone — admins have
  rights. `/api/scheduler-status` non-admin roster trims to just
  your row.
- **Audit result**: spawned an Explore agent to sweep every other
  endpoint for similar leaks. Result: `scheduler-status` was the
  only one. All per-user data endpoints (`/api/data`,
  `/api/tax-report`, `/api/positions`, etc.) correctly filter by
  `current_user['id']`.

*Native charts (Tier B):* added a 📈 Chart button on every pick
card, screener row, and position row. Opens a modal with a native
canvas line chart fed by a new `/api/chart-bars` endpoint.
- 30d / 60d / 90d / 6M timeframe toggle
- Options chart the underlying (HIMS put shows HIMS bars)
- Overlays: **purple** dashed line = your entry, **orange**
  dashed line = your current stop, picked up live from your
  positions + open orders.
- No external deps — ~100 LOC of inline canvas drawing. No
  TradingView iframe, no Chart.js bundle. Matches app dark theme.
- Legend shows current price, % change over the window, and any
  entry/stop values you hold.

---

## 🆕 What's New (2026-04-21 evening — Round 36)

**Admin-panel overhaul + weekly-learning bug fix.**

**1. New invite signup flow — friend-friendly, no secrets shared.**
The admin panel's *Invites* tab now generates a one-time signup URL
that your friend clicks to land on the signup form with the invite
code auto-filled. Key properties:
- **Single-use**: once a friend signs up, the invite can't be reused
- **7-day default expiry** (customizable 1-30 days)
- **Hash-only storage**: plaintext token shown ONCE at creation, never
  stored. If your DB dump leaks, nobody can redeem outstanding invites
- **Friends sign up as regular users** — never as admins (backend
  hardcodes `is_admin=False` on signup)

**2. Admin panel — new abilities.**
- **Revoke Invite**: button on active invites in the Invites tab.
  Sets `expires_at` to the past so the URL stops working immediately.
  Used / expired invites show no button (revoking them is a no-op).
- **Make / Revoke Admin**: toggle admin rights on any user from the
  Users tab. Server-side guard rail blocks demoting the last active
  admin (so you can't accidentally lock yourself out).
- **Audit log sizing fix**: the Admin modal had no height constraint,
  so a long audit log rendered past the viewport bottom — hiding the
  Close button and forcing a page refresh to dismiss. Now the modal
  caps at `88vh`, tabs + Close stay pinned, and the content area
  scrolls internally.

**3. Weekly-learning engine — actually wired to the screener now.**
Found while auditing "is learning really happening?" — YES, the
Friday 5:00 PM ET engine runs and writes per-user weights to
`/data/users/<id>/learned_weights.json`, but the screener was reading
from the SHARED `/data/learned_weights.json` path and never picking
them up. The screener now honors the same `LEARNED_WEIGHTS_PATH` env
var `learn.py` uses, and `cloud_scheduler.run_screener_for_user`
sets it to the per-user file. So once you have a handful of closed
trades, the screener will start scaling strategy multipliers toward
what's actually working for YOUR account.

---

## 🆕 What's New (2026-04-21 afternoon — Rounds 31-35)

**Rounds 31-32 — Sticky nav polish.** Nav tabs (Overview / Picks /
Strategies / Positions / Screener / etc.) now stay sticky below the
top header on both desktop AND mobile. Scroll-hint gradient + animated
`›` chevron on the right edge cue you to swipe for more tabs (and
auto-fade when you reach the end). Readiness-score labels corrected:
the five scored criteria are Days Tracked ≥30, Win Rate ≥50%, Max
Drawdown <10%, Profit Factor ≥1.5, Sharpe ≥0.5. "Total Trades" is
informational only — doesn't affect the 0-100 score.

**Round-33 — Journal-undercount fix.** Before round-33, only
`cloud_scheduler.run_auto_deployer`'s main path wrote to
`trade_journal.json`. Wheel puts (sold by `wheel_strategy.open_put`)
and manual deploys (from the dashboard Deploy button) never appended
an "open" entry, so when they later closed, the scorecard undercount.
Now a new `record_trade_open()` helper is called by all 6 deploy
paths (trailing / breakout / mean-reversion / copy-trading / wheel-
put-open / manual dashboard deploy).

**Round-34 — Today's Closes panel + orphan-close safety net.**
- New "Today's Closes" panel in the Overview section shows every
  stop-trigger / earnings auto-exit / profit-ladder sell / PEAD
  window close / manual Close click that happened today, with time /
  symbol / strategy / reason / exit price / P&L and a net-P&L
  summary. Auto-hides when there's nothing to show.
- `record_trade_close` hardened: when no matching open entry exists
  (e.g. a pre-round-33 close), it now appends a synthetic entry
  marked `orphan_close: true` instead of silently returning False.
  The dollar P&L and exit reason are preserved; only the entry
  price is missing. Orange `[orphan]` tag on the panel row warns
  you this is a reconstructed entry.

**Round-34 (continued) — Positions-table scroll containment.** On
mobile, swiping the Positions or Orders table sideways used to drag
the whole viewport (account-bar / metric cards slid off-screen).
Added `overscroll-behavior-x: contain` so the pan stays inside the
card.

**Round-35 — Real Position Correlation + action-button alignment.**
- **Correlation section rebuilt.** Previously printed "Sectors:
  <list of your position SYMBOLS>" — which isn't sectors at all, just
  symbols. Useless. New panel groups by actual sector with bars +
  $ allocation + %, and flags concentration only when one sector
  exceeds 40% (orange) or 60% (red). Options route through the
  underlying symbol (e.g. HIMS put → Healthcare).
- **Positions-table action buttons** (Close / Sell 50% / Sell 25%)
  now stay on a single horizontal row. Before, at narrow widths
  they wrapped onto 3 vertical lines and misaligned the Actions
  column header.

---

## 🆕 What's New (2026-04-21 — Rounds 28-30)

**Round-29 — Universal pre-earnings auto-exit.** Before this round, only
the PEAD strategy exited before earnings. Breakout / trailing / mean-
reversion / copy-trading positions sat through earnings and got whipsawed
by surprise moves. Now the bot automatically closes any such position
**1 day before** its earnings event. Wheel short puts are deliberately
held — they profit from IV crush post-earnings, which is the wheel's
profit engine.

**Configurable via Settings → Guardrails:**
- `earnings_exit_days_before` — how far ahead to exit (default 1)
- `earnings_exit_disabled` — set `true` to opt out entirely

**Round-30 — UX polish + sector map fix.**
- Every dashboard section now has an ⓘ info button that opens a
  plain-English guide: Position Correlation, Paper Trading Progress,
  Tax-Loss Harvesting, Visual Backtest, Cloud Scheduler, Performance
  Attribution, Tax Report, Factor Health, Activity Log, Short
  Candidates, Paper vs Live.
- Sector map populated for 80+ additional tickers (SOXL, SOXS, CHWY,
  SNDK, BB, POET, MSTR, MARA, IONQ, QBTS, and more). Correlation
  warnings no longer flag everything as "Other" — concentration
  alerts now reflect real sector overlap.

**Round-28 — Exception-handling cleanup (merged).** Narrowed bare
`except:` clauses across `error_recovery.py`, `learn.py`,
`update_dashboard.py`, `auth.py` so KeyboardInterrupt / SystemExit
propagate during shutdown. Surfaced three silent swallows in
`strategy_mixin.py` as WARN logs (audit log breakage, cooldown
timestamp parse, PEAD scorer failure).

---

## 🆕 What's New (2026-04-20 — Rounds 21-27)

Monday's paper-trading session added a big batch of features and reliability fixes. The short version: the dashboard is now information-rich enough that most of what the bot knows about a stock is visible on the card — AI reasoning, breaking news alerts, insider cluster buys, news sentiment — and the manual-override UX has been filled in with Sell 25% / Sell 50% buttons and a wheel-aware Close modal that explains every trade in plain English.

### New dashboard features

- **🤖 AI / 📰 News / 🔵 Insider sentiment lines on pick cards.** Three small lines below the existing Social line. AI is Gemini's one-sentence analysis. News shows the Alpaca news sentiment + the first bullish-keyword match (e.g. *"earnings beat"*, *"upgrade"*). Insider appears only when SEC Form 4 filings show a cluster-buy (multiple insiders in 30 days) — the strongest signal of the three.
- **🚨 Breaking News banners.** Alpaca's real-time news WebSocket scores every incoming headline (`|score| ≥ 6` = actionable). A 🚨 BREAKING BULLISH/BEARISH banner appears on any pick card AND any Open Positions row whose symbol (or underlying, for options) gets a fresh alert in the last 60 min. Option positions key off the underlying — a HIMS put shows HIMS news.
- **Sell 25% + Sell 50% buttons** on every Open Positions row. Partial profit-taking without fully exiting. Uses `/api/sell` with the calculated qty.
- **Wheel-aware Close modal.** When you click Close on a short put or covered call, the modal now shows premium collected, breakeven, max profit, max loss, and an *"if assigned"* explanation. No more squinting at option math.
- **ⓘ section help buttons** next to the main section headings. Click for a focused explanation of that section instead of scrolling the whole user guide.
- **Trade Heatmap legend** — now actually renders the color gradient between Loss and Win labels (was blank — color classes were scoped only to cells, not legend boxes).

### Under the hood (reliability)

- **Scheduler thread-death watchdog.** Polls `_scheduler_thread.is_alive()` every 60s; fires a `critical_alert` (ntfy + Sentry + email) exactly once per process if the thread dies. Previously a silent scheduler death left the HTTP server up while the bot had stopped trading.
- **Subprocess zombie tracking** piggy-backing on the watchdog tick — reaps via `waitpid(-1, WNOHANG)`, alerts hourly if Z-state children exceed 5.
- **Dashboard fetch 30s timeout.** If `/api/data` hangs past 30 seconds, the toast says *"Dashboard fetch stalled"* with a Retry action. No more infinite *"Next refresh: 0s"* waits.
- **Session 12-hour idle timeout.** Sessions still have a 30-day absolute ceiling, but an inactive session now gets invalidated after 12 hours. Every valid request slides the idle window forward.
- **Boot-time config WARNs.** Server logs a friendly warning on boot if any of `GEMINI_API_KEY` / `SENTRY_DSN` / `NTFY_TOPIC` are unset, naming the consequence and the exact Railway env var to set.
- **Mobile horizontal-scroll clamp.** Dashboard no longer slides sideways on narrow screens. Overflow-containing regions (positions table at ≤380px) still scroll *inside* their card.
- **`news_websocket` wired in** for user_id=1 with the union of open positions + active strategy symbols. Feeds the `news_alerts.json` file that drives the Breaking News UI.
- **Exception-handling hardening (round 2).** Narrower catches + `observability.capture_exception` routing in `llm_sentiment._write_cache`, `insider_signals._write_cache / _read_cache`, `smart_orders._dec / _get_quote`, `social_sentiment` recency filter, `capital_check.safe_save_json`, `notify.safe_save_json`. Silent failure paths that previously swallowed shape-drift now surface via Sentry.

### Signup / invite flow

- **Single-use signup invites.** Admin → Invites tab → Generate Invite → one-time URL to share. Tokens are SHA-256-hashed at rest, atomically consumed on signup, expire in 7 days (configurable).

### Critical bug fixes (all shipped, all paper-trading)

- **RSI / MACD / BIAS were hardcoded 50 / 0 / neutral on every pick.** Root cause: bar-fetch window was 20 days but MACD needs 26. Fetching 60 days now gives real indicator values.
- **Gemini LLM returning HTTP 404.** `gemini-1.5-flash` was deprecated; `gemini-2.0-flash` also 404'd on the v1beta endpoint. Switched to `gemini-2.5-flash` + disabled internal "thinking" tokens + forced JSON response MIME to stop the *"AI: unparseable: ```"* display.
- **Alpaca news API returning HTTP 400.** `%z` produced `-0400` (no colon) which RFC-3339 parsers reject. Now emits UTC with `Z` suffix.
- **Orphan-position false-positive email alerts** on every short put (CHWY260515P00025000, HIMS260508P00027000). `error_recovery.py` was comparing raw OCC symbols against strategy-file underlyings. Now resolves OCC → underlying before the lookup.
- **Zombie-alert rate-limit bug.** Was passed by value, never advanced, would have fired every 60s once zombies > 5. Returns the updated timestamp now.

**Current state**: 58 PRs merged across rounds 11-27. 473 tests passing. Ruff clean. Paper-trading validation window ongoing (started 2026-04-15, ends ~2026-05-15).

---

## 🆕 What's New (2026-04-19 — Round-20: Trade Quality Filters)

Based on analysing a live `/api/data` snapshot: every top-scored
Breakout pick was stopping out for a loss because the bot was buying
breakout-day peaks and getting whipsawed by normal pullbacks into
tight 5% stops. Fixed:

### Auto-deployer filters now active
- **Don't chase** — skip Breakout/PEAD picks already `+8%` today
- **Volatility cap** — skip Breakout/PEAD where `volatility > 20%`
  (INFQ-tier names with 30%+ volatility are meme territory, not
  tradable breakouts)
- **Smaller positions** — `max_position_pct` 10% → **7%** per stock
  (applied automatically to existing users on next Railway redeploy
  — no "Apply Moderate" click needed)
- **Wider breakout stop** — `breakout_stop_loss_pct` 5% → **12%**
  (the 5% default was tighter than every other strategy's, backwards
  — breakouts need room to breathe)

### What changes Monday morning
Instead of deploying INFQ (vol 33.9%, +12.5% today, backtest -14.42%)
+ JHX, the bot will skip INFQ entirely (blocked by BOTH gates) and
pick cleaner setups like ALM / JHX at 7% sizing.

### Dashboard also fixed
- Strategy Templates panel correctly shows **MODERATE** as active
  (was reading "CUSTOM" after the auto-migration because the
  detection logic still checked the old 10% cap)
- Moderate card displays **7%** per stock (matches what Apply writes)
- Moderate description now surfaces the round-20 trade-quality gates
  (don't-chase +8%, volatility >20%, 12% breakout stop)

---

## 2026-04-19 — Rounds 14-17, Production Hardening

Continued audit + cleanup pass after round-13. Four more rounds, 4 PRs,
~50 fixes. The biggest things you'll notice:

### Real-money / safety
- **Kill-switch trip emails actually arrive now.** This was silently
  broken since round-11 — wrong import + wrong signature in
  `observability.critical_alert`. Every kill-switch / -3% loss event
  failed to email the operator (ntfy push + Sentry still worked).
- **Daily -3% loss alert now notifies you.** Was a dashboard-only flag
  with no notification path. Now routes through `critical_alert` +
  ntfy + email + Sentry, deduped per ET-day.
- **Alpaca 401/403 (creds rotted) fires a critical alert** once per
  user per ET-day. Previously these silently failed every order.
- **Partial-fill cost basis is correct now.** When a limit order
  partially fills + market falls back, the journal records the
  blended price, not just the market leg. PnL no longer drifts ~0.8%
  over wheel cycles.

### Diagnostics & integrity
- **Boot-time state-recovery validator** compares wheel state files +
  trade journal vs Alpaca-reported positions on every Railway redeploy.
  Surfaces drift via Sentry as warnings (doesn't auto-fix). Catches
  manual sales / margin liquidations / orphan trades early.
- **Per-user isolation invariant pinned by tests.** The "only user_id==1
  may inherit shared DATA_DIR" rule is now in `per_user_isolation.py`
  with multiple tests to prevent silent regression.

### Code structure
- **`cloud_scheduler.py` 3800-LOC monolith split.** Alpaca API plumbing
  (HTTP helpers + circuit breaker + rate limiter) extracted into
  `scheduler_api.py`. Backwards-compatible — every symbol still
  re-exported from `cloud_scheduler` so existing imports work.

### UI polish
- Sortable table headers announce sort direction to screen readers
  (`aria-sort`).
- Network-error toasts now include a Retry button.
- 30-min screener runs show an elapsed-time progress banner with
  stage hints.
- Removed dead Stock Watcher provider from `capitol_trades`.

**Test count:** 229 → **423 passing** (+194 across rounds 12-19).
Ruff clean. **CI coverage floor 20% (measured 25.4%)** — bumped in
round-19 once tests crossed the threshold.

### Round-19 final polish (PR #33)

Fresh self-audit on the code written in rounds 14-17 surfaced two
real bugs:
- `scheduler_api` DELETE + PATCH were skipping the rate-limit gate
  (could 429-spam during kill-switch cancel storms). Fixed.
- `options_analysis.analyze_wheel_candidates` crashed on empty-string
  `strike_price` from Alpaca (newly-listed / halt-pending contracts).
  Fixed with defensive parse.

Also: 13 new options tests; 401/403 alerts now symmetric across
POST/DELETE/PATCH; coverage ratchet bumped.

See `GO_LIVE_CHECKLIST.md` for what's left before flipping to live
(only user-side operational items remain).

---

## 🆕 What's New (2026-04-19 — Round-13 Cleanup + Production Readiness)

Follow-on to the round-12 sweep. 7 more PRs landed covering the test-
coverage gaps, a previously-undetected circuit-breaker bug, wheel
stock-split auto-resolve, and a defense-in-depth security bundle.

### Things you'll notice
- **API-key fields are now masked** (dots instead of visible text) on
  Settings, with spellcheck off. Reduces shoulder-surfing / screen-share risk.
- **Regime badges** (bull / neutral / bear) have brighter text colours
  for WCAG AA contrast on the dark theme.
- **Auth pages** (login / signup / forgot / reset) no longer auto-zoom
  on iPhone when you tap into an input.
- **Offline banner** — if the service worker serves a cached page and
  you try to refresh data, you'll see a soft "Offline — cached data"
  toast instead of a cryptic "HTTP 503" error.
- **README modal** is safer — markdown rendering runs through an HTML
  sanitizer before display.

### Things working better behind the scenes
- **yfinance rate-limit failures** route through Sentry so we see them
  aggregated rather than buried in stdout. Permanent errors (shape
  drift in Yahoo's response) stop retrying after the first attempt
  instead of burning the budget.
- **Sentry events are PII-scrubbed** before transmit: Alpaca PK/AK
  keys, emails, base64 tokens, and auth headers get redacted.
- **Circuit breaker actually works.** Before this round the reset bug
  silently ate the failure counter on every non-tripped check.
- **Wheel auto-resolves stock splits** — if Alpaca reports 200 shares
  after a 2:1 split during your put-active window, we no longer freeze
  the cycle; we normalise baseline + expected_delta by the split ratio
  and proceed.
- **Social sentiment drops stale chatter** — StockTwits messages older
  than 30 minutes don't count towards the current sentiment reading.
- **News scores capped at ±15 per article** so one densely-worded
  headline can't dominate the aggregate.
- **FOMC dates extended to 2027** so the event guard doesn't silently
  stop flagging Fed meetings on Jan 1 2027.

See `GO_LIVE_CHECKLIST.md` for the pre-flip-to-live gating list and
`CLAUDE.md` for developer-facing notes.

---

## 2026-04-18/19 — Round-12 Audit Sweep (15 PRs shipped)

Full-stack audit + fix cycle run on the 30-day paper validation window.
Five parallel audits (security, database, trading logic, UI/UX/mobile,
test coverage) + 15 squash-merged PRs + 110+ new regression tests. The
most consequential finding: the `portfolio_risk` beta-exposure safety
rail had been silently disabled in production since round-11 —
`run_auto_deployer` referenced three variables before they were defined,
so every call hit `NameError`, swallowed by the outer try/except. Now
live. Watch your Railway log for `Beta exposure: …% beta-weighted` on
the next deploy to confirm.

### What changed behaviourally (things you'll notice)

- **Login page**: session-expiry now shows a "Session expired" toast +
  1-sec delay before redirect. Modals trap Tab focus inside and return
  focus to the trigger on close. Colors (`.positive` green, `.negative`
  red) brightened to WCAG-AA contrast on dark theme.
- **Dashboard**: iPhone SE (375px) viewport now displays modals without
  horizontal overflow. Refresh button shows spinner + disabled state
  during the 5-30s screener run. Mobile tables have a visual
  scroll-hint gradient on the right edge when content overflows.
- **Kill switch**: now aborts in-flight deploys **atomically** via a
  `threading.Event`. Previously had a 100-300ms window where a
  multi-symbol deploy could keep placing orders after the switch
  tripped. No more.
- **Money math**: every internal accumulator — cost basis, wheel premium,
  realized PnL, tax-lot summary, strategy-breakdown totals, position
  sizing — now runs in `Decimal`. Your scorecard numbers are now exact
  to the cent regardless of how many partial fills or wheel cycles
  they've passed through. The JSON boundary is unchanged (still float
  with 2dp) so no frontend changes.

### What's required for your next Railway deploy

- **`MASTER_ENCRYPTION_KEY` is mandatory**. If missing, the app refuses
  to boot (intentional — PLAIN-fallback retired). Confirm it's set on
  Railway → Variables before the next redeploy.
- **Rotate the old Sentry DSN** per `docs/MONITORING_SETUP.md`. Old
  key is in git history forever; Sentry dashboard → Project Settings
  → Client Keys → Deactivate old → Create new.
- **Generate SRI hashes locally** if you haven't — the manifest refs
  are in place, but the `integrity="sha384-..."` values come from
  `bash scripts/compute_sri.sh` on a dev machine (the sandbox can't
  reach CDNs). Paste the three output lines into the 5 `<script>`
  tags across `dashboard.html` / `track_record.html` / `signup.html`
  / `reset.html`.

### Round-12 ship list

| # | PR | Area | Change |
|---|---|---|---|
| 2 | `b6c9bcd` | Security | Sentry auto-init, `MASTER_ENCRYPTION_KEY` mandatory |
| 3 | `d1d7c3e` | Ops | JSON logging, `/api/version` dynamic, a11y, WCAG colours |
| 4 | `9d6569a` | Security | SRI hashes pinned on CDN scripts |
| 5 | `966e531` | Ops | Trade journal auto-trim (>2y closed → archive) |
| 6 | `dcdf166` | Trading | `tax_lots.py` → Decimal (migration phase 1) |
| 7 | `16afdf5` | Security | Token-bucket login rate limit |
| 8 | `98d3f5c` | Trading | `update_scorecard.py` → Decimal (phase 2) |
| 9 | `03becfc` | Trading | `portfolio_risk.py` → Decimal (phase 3) |
| 10 | `c73c288` | Trading | `wheel_strategy.py` → Decimal + 39 parity-fuzz tests (phase 4) |
| 11 | `7353b65` | Trading | `smart_orders.py` + `calc_position_size` → Decimal + 30k fuzz inputs (phase 5, FINAL) |
| 12 | `c6827fa` | Security | Password-reset TOCTOU fixed, capital_check fallback tightened |
| 13 | `bc40d49` | UI / a11y | XSS hardening, modal focus trap, forgot-password constant-time |
| 14 | `d06760d` | Trading | Kill-switch atomic abort, trim flock, wheel split-anomaly guard |
| 15 | `3ad82a7` | Ops | CI tooling (ruff + coverage), **beta-exposure gate revived (was DEAD CODE)** |

**Details**: `CLAUDE.md` (session-resume context) and
`IMPLEMENTATION_STATUS.md` (running changelog).

---

## 🆕 What's New (2026-04-19 LIVE-TRADING READY)

Weekend 2, Batch 2: the bot is now **live-trading ready**. Full in-app
control of paper/live mode, credentials, safety rails. Nothing on
Railway env vars anymore — everything toggles from the UI.

### Ship list (all live)

| Feature | Where |
|---|---|
| **In-app API key management** (paper + live separately) | Settings → Alpaca API tab |
| **Test Connection** before save (validates against Alpaca) | Settings → Alpaca API → Test Connection |
| **Live-trading toggle with safety gates** | Settings → 🔴 Live Trading tab |
| &nbsp;&nbsp;→ Requires paper keys + live keys + email + ntfy topic | |
| &nbsp;&nbsp;→ Readiness score ≥ 80 (override available) | |
| &nbsp;&nbsp;→ Hard cap on per-trade position size ($500 default) | |
| &nbsp;&nbsp;→ Confirm by typing "YES" prompt | |
| &nbsp;&nbsp;→ Audit-logged + critical alert on every toggle | |
| **Public track record page** (opt-in, read-only) | Settings → Sharing → enable; URL: `/track-record/<user_id>` |
| **Daily scorecard email digest** (4:30 PM ET weekdays) | Settings → Sharing → Daily scorecard email |
| **CSV export for every table** | ⬇ CSV buttons on each table + Settings → Sharing → Data Export |
| &nbsp;&nbsp;positions, orders, trades, picks, tax lots, IRS 8949 | |

### Live-trading go-live flow (when you're ready)

1. **Get live API keys** from [app.alpaca.markets](https://app.alpaca.markets) → your LIVE account → API Keys
2. **Settings → Alpaca API → Live Trading Keys** → paste key + secret → Test Connection → Save
3. **Settings → 🔴 Live Trading** → set max position size (recommended $500 for week 1) → Enable Live Trading → type "YES" to confirm
4. Bot immediately switches to your live account. All new trades use real money. All existing paper positions stay in the paper account.

### Critical safety rails active in live mode

- Every trade capped at your `live_max_position_dollars` regardless of strategy config
- Beta-adjusted exposure gate blocks new high-beta entries when portfolio already heavily leveraged
- Drawdown-adaptive sizing (0.25x-1.0x) automatically shrinks positions after losses
- Correlation gate blocks trades that would put your book too correlated
- All round-11 factor gates still apply: breadth, RS, sector, quality, IV rank

### Disabling live mode

Settings → 🔴 Live Trading → Disable Live Trading. Positions stay open in your Alpaca live account (you manage them there or come back to live mode). Bot immediately switches back to paper.

---

## 🆕 Round-11 Expansion (2026-04-19)

This weekend shipped **20 major upgrades** across factor intelligence, risk management, UX, and observability. Quick tour of where each one lives:

| # | Feature | Where to find it |
|---|---|---|
| 1 | **Performance attribution** — which strategy made $ this month | Dashboard → "Performance Attribution" panel |
| 2 | **Tax-lot tracking + Form 8949 CSV** | Dashboard → "Tax Report" panel → Download 8949 CSV |
| 3 | **Smart limit orders** — saves 0.1-0.5% slippage on entries | Auto-active; `SMART_ORDERS=0` to disable |
| 4 | **Off-Railway backup** — S3 / Backblaze / GitHub destinations | Set S3/B2/GitHub env vars; see `docs/MONITORING_SETUP.md` |
| 5 | **Pre-trade impact preview** | Deploy modal → "Portfolio Impact" card |
| 6 | **Pre-market scanner** — top-100 gap scan at 8:30 AM ET | Auto-active; saves `premarket_picks.json` |
| 7 | **SEC EDGAR insider buys** — cluster buying detection | Auto-active; adds `insider_bonus` to picks |
| 8 | **LLM news sentiment** (Gemini 1.5 Flash / GPT-4o-mini) | Set `GEMINI_API_KEY` (already set!) |
| 9 | **Multi-timeframe confirmation** — daily + weekly agreement | Auto-active for breakout + PEAD picks |
| 10 | **Real-time Alpaca news websocket** | Optional: needs `pip install websocket-client` |
| 11 | **Beta-adjusted exposure** — caps leveraged-ETF concentration | Auto-active; Factor Health panel shows regime |
| 12 | **Drawdown-adaptive sizing** — smaller size after losses | Auto-active; 0.25-1.0x multiplier |
| 13 | **Correlation gate** — blocks trades that co-move >75% | Auto-active in deployer |
| 14 | **Visual chart annotations** on backtest | Entry/exit/stop markers on the price chart |
| 15 | **Strategy explainer cards** in deploy modal | Every Deploy click shows per-strategy rules |
| 16 | **Mobile PWA install** — add to home screen on iOS/Android | Safari: Share → Add to Home Screen |
| 17 | **Custom dashboard layout** — show/hide sections | User menu → "Show / Hide Sections" |
| 18 | **Sentry error tracking** (free tier) | Set `SENTRY_DSN`; see `docs/MONITORING_SETUP.md` |
| 19 | **Critical-event alerting** — Sentry + ntfy + email | Auto-active for kill-switch trips |
| 20 | **UptimeRobot external monitoring** — free 5-min polls | Monitor created; `docs/MONITORING_SETUP.md` |

**Earlier round-11 factor batches** (also live): ATR-based stops, market breadth gate, Relative Strength ranking, sector rotation, fundamental quality filter, IV Rank gate for wheels, delta-based strike targeting, Kelly-lite position sizing, walk-forward + Sharpe weighting.

**New dashboard sections:**
- **Factor Health** — market breadth, top sectors, cache state, yfinance budget
- **Performance Attribution** — $ per strategy with visual bars
- **Tax Report** — lots + short/long-term + wash-sale warnings

**Per-pick factor chips** in the Top-50 screener:
`Q:A RS:+12% XLK #1 IV:72 📈 BULL` — decodes the bot's reasoning at a glance.

**Emergency override:** If factor filters block every deploy, use the **Factor Bypass** toggle in the Factor Health panel to temporarily fall back to raw screener scores.

**For monitoring setup** (Sentry + UptimeRobot), read [`docs/MONITORING_SETUP.md`](docs/MONITORING_SETUP.md) — 2-minute copy-paste guide.

---

