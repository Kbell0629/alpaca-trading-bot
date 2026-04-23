# CLAUDE.md — Session Context

One-page answer to: what is this, what's the current state, how do I pick up?
Full history lives in `CHANGELOG.md`.

---

## What this is

Autonomous multi-user stock-trading bot. Screens ~12K US equities every 30 min,
auto-deploys top picks across 6 strategies, manages exits, handles kill-switch
+ drawdown guardrails, runs 24/7 on Railway.

- **Dashboard:** https://stockbott.up.railway.app
- **Repo:** `Kbell0629/alpaca-trading-bot`
- **Primary branch:** `main` (protected: PR + CI required, no force push)
- **Agent branch pattern:** `claude/<short-description>` → PR → squash-merge
- **Sessions:** paper-trading 30-day validation (started 2026-04-15, ends ~2026-05-15).
  Live-trading path wired but off.

## Alpaca account state

- Paper $100k seed, live toggleable via Settings → 🔴 Live Trading (OFF).
- `MASTER_ENCRYPTION_KEY` **required** on Railway (auth.py refuses boot without).
- `SENTRY_DSN`, `NTFY_TOPIC`, `GEMINI_API_KEY` set on Railway.

---

## Picking up a session

1. `git checkout main && git pull --ff-only`
2. `cat CLAUDE.md` (this file), `cat README.md`, `cat CHANGELOG.md`
3. `MASTER_ENCRYPTION_KEY=$(python3 -c 'print("e"*64)') python3 -m pytest tests/ --deselect tests/test_dashboard_data.py::test_trading_session_is_computed_live_not_from_stale_json --deselect tests/test_auth.py::test_password_strength_rejects_weak -q`
   — expect **779 passing, 2 deselected** after round-57.
4. `ruff check .` — clean.
5. Validate dashboard JS: `awk '/^<script>/,/^<\/script>/' templates/dashboard.html | grep -v '^<script>' | grep -v '^</script>' > /tmp/dash.js && node --check /tmp/dash.js`

**GitHub MCP is disconnected** every session. User opens PRs + merges manually
via web UI. Don't try `mcp__github__*` tools.

---

## Audit playbook (when user says "check for bugs")

1. Read this file + `git log --oneline main | head -30`.
2. Spawn 5 parallel Explore agents: security, DB/concurrency, trading logic,
   UI/UX/mobile, tests/ops. Give each <1000-word reply budget.
3. Triage: auto-fix clear wins in ONE PR; flag architecturally significant
   ones for user decision.
4. `ruff check . --select F821,B023` — historically surfaces real bugs
   (undefined names, loop captures).
5. **Verify every trading-logic claim against actual code** — that agent has
   a history of false positives (CHANGELOG round-22 + round-52 logged 8+
   each round that turned out to be misreadings).
6. Deliver report: "fixed", "needs your decision", "deferred".

---

## Current session state (2026-04-23 — round 61 money-path tests)

**Branch:** merged to main. Round-61 split into three PRs: **pt.1 #102**
(monitor_strategies + check_profit_ladder, 29 tests), **pt.2 #103**
(run_auto_deployer + wheel state machine, 42 tests), **pt.3** (this PR —
docs + coverage ratchet + behavioral coverage top-up). Rounds 58-60 all
merged earlier today (PRs #99-#101).

### Round-61 — Money-path test coverage (Option A)
User picked Option A from the coverage-options discussion: focus-fire
tests on the highest-risk uncovered money paths rather than a full
push to 80%. Four targets picked for their history of real-money bugs:

**`monitor_strategies` (13 grep-pin tests, pt.1):** kill_switch early
return pre-API, max_drawdown + daily_loss breach flows (RMW under
lock, notify_rich, critical_alert, _flatten_all_user), peak +
daily_starting_value seeding under lock, main-loop skip lists
(copy_trading + wheel_strategy) + status gate, top-level try/except
isolation, AH mode paused/short/wheel skips, _tier_cfg stashed in
both branches.

**`check_profit_ladder` (16 behavioral tests, pt.1):** rung ordering
10→20→30→50 firing exactly once per call, 25% of initial_qty anchor
(not current shares), client_order_id ET-date format for idempotency,
Alpaca 422 dedup, stop resize (PATCH first, cancel+replace fallback,
cancel on zero remaining), PDT gate (skip / bypass / fail-open).

**`run_auto_deployer` (19 grep-pin tests, pt.2):** kill_switch pre-API,
enabled=False early return, cooldown parse fail-CLOSED (R3), tier cash
short-disable (R52), LIVE_MODE_AT_START snapshot (R45), factor_bypass,
weak-breadth skip, daily_starting_value once per ET day, capital_check
per-user path (R10), beta-exposure after positions (R12), per-pick
enforcement of block_all, correlation for longs+shorts, tier-log dedup,
tier defaults respect user overrides, calibration fail-open.

**Wheel state machine (23 grep-pin tests, pt.2):** four stage names
stable, open_short_put/open_covered_call write sites, canceled/expired/
rejected order stage reset, transitional statuses stay pending (R10),
DELTA-based put assignment (R43), cost_basis = strike - prem/share
Decimal-safe, cost_basis cleared to None when shares hit 0, call
assignment completes cycle, call-expired → stage_2_shares_owned,
PROFIT_CLOSE_PCT + bid fallback (R10), external-close pre-expiry only
(R42), anomalous-delta freeze without split (R12), split auto-resolve
(R13), every close path journals, HISTORY_MAX cap, premium accumulator
Decimal-safe.

**Coverage honesty:** 53 of the 71 tests are grep-pins (pattern
assertions on source). They DO catch regressions from refactor-renames,
accidental guard removal, and CLAUDE.md invariant drift — but they don't
move pytest-cov the way behavioral tests do. Only the 16 profit_ladder
tests + 2 daily-close edge cases are real behavioral coverage. Expected
CI test count: 891 passing after pt.2.

### Previously in-flight (now merged)
Rounds 54-60 all merged via PRs #97-#101, 2026-04-22 / 2026-04-23.
Round-57 audit sweep (detailed below) was the last big regression-fix
round before the coverage-testing sprint.

### Round-57 — Full tech-stack audit fixes
5 parallel Explore agents ran. 13 real bugs triaged, 3 false positives verified.

**Concurrency (4 unlocked `guardrails.json` RMW sites fixed):**
- `cloud_scheduler.py:1891` — stop-triggered `last_loss_time` write now
  inside `strategy_file_lock(gpath)`.
- `cloud_scheduler.py:2857` — `run_daily_close` `daily_starting_value` +
  `peak_portfolio_value` update now locked. Fetches `/account` OUTSIDE
  the lock (100-500ms network call would block the monitor otherwise).
- `server.py` `/api/calibration/override` + `/api/calibration/reset` —
  both handlers now wrap RMW in `strategy_file_lock`. Tier detection
  runs OUTSIDE the lock for same reason.

**Rate limit:** `/api/calibration/override` per-user cooldown = 3s
(`_CALIBRATION_OVERRIDE_LAST_WRITE` module dict). Scripted loops / double-
clicks return HTTP 429 + `rate_limited:true`.

**Observability:** Failed trailing-stop raises now call
`observability.capture_message("trailing_stop_raise_failed", ...)` with
`session` (AH vs market) + `symbol` + attempted new_stop. Operator can
debug in Sentry UI instead of scrolling logs.

**UX:**
- `<input type="range">` CSS: 32px container height, 22px custom thumb
  with `accent-color: var(--blue)` + WCAG-AA contrast on dark bg.
  iOS users can actually drag them now.
- Dashboard header: `⚡ AH TRAILING` chip renders during pre/post-market
  when `guardrails.extended_hours_trailing != false`. User can see the
  bot is actively watching, not sleeping.
- Calibration hierarchy text wrapper: `role="note" aria-live="polite"
  aria-atomic="true"` so screen readers announce it.

**Data exposure:** `/api/data` now returns `extended_hours_trailing`
(defaults True) so the dashboard can render the AH chip.

**Tests:** 14 new in `tests/test_round57_audit_fixes.py` — lock presence
grep-level pins, 429 response pin, Sentry breadcrumb pin, daily-close
`portfolio_value=None/0` runtime edge cases, slider touch-height CSS,
AH chip HTML. 779 passing total (was 765 pre-round-57), 2 deselected.
Ruff clean. Dashboard JS `node --check` clean.

### Previously in-flight (now merged)
Round-54 (calibration overrides + jitter fix) + round-55 (AH trailing)
+ round-56 (daily-close email OCC option labeling) all merged via
PR #97, commit `a10d00b`, 2026-04-22.

### Round-54 — Calibration per-key overrides + desktop jitter fix
- `POST /api/calibration/override` — whitelist + range validation + Alpaca-rule
  blocks (cash can't enable shorts, margin <$25k can't disable PDT, etc.)
- `POST /api/calibration/reset` — reverts tier-adopted keys only (preserves
  user risk-preference keys: daily_loss_limit_pct, earnings_exit, kill_switch)
- Settings → Calibration tab: sliders + toggles + strategy pills + warnings
- **Jitter FIX**: `window._lastAppHtml` hash-skip — only touches
  `app.innerHTML` when output string differs. Zero repaint on quiet ticks.
- 11 new tests in `test_round54_calibration_overrides.py`

### Round-55 — After-hours trailing-stop tightening
- Monitor every 5 min in pre-market (4–9:30 AM ET) + post-market (4–8 PM ET),
  **stops-only mode**.
- Trailing-stop raise fires on AH pops; stop order stays GTC + triggers at
  next regular-hours cross.
- AH mode SKIPS: daily-loss kill-switch, initial stop placement, profit-take
  ladder, mean-reversion, PEAD, earnings-exit, short-sells, wheel files
  (thin-book paths).
- Opt-out: `guardrails.extended_hours_trailing = false` (default True).
- 10 new tests in `test_round55_after_hours_trailing.py`

### Round-56 daily-close email (merged as part of PR #97)
User forwarded an email screenshot showing `HIMS260508P00027000 ... (-1 sh)`
which should have read as `HIMS put 260508 $27 ... (short 1 contract)`.
New `_display_label(sym, qty)` closure in `_build_daily_close_report`
detects OCC format and renders underlying + expiry + strike + right +
singular/plural contract noun. 11 tests. Math (%, $, sort) untouched.

---

## Architectural invariants (CRITICAL — don't regress)

### Post-61 (money-path test coverage — Option A)
- `tests/test_round61_*.py` pin money-path invariants at the source-string
  level. A refactor that renames `strategy_file_lock` or moves the
  `kill_switch` guard will break these — BY DESIGN. Read the failing
  assertion's docstring before "fixing" the test; it documents which
  past bug the invariant prevents.
- `check_profit_ladder` MUST keep the rung order `[10, 20, 30, 50]` at
  25% of `initial_qty` each. Changing this changes every open wheel's
  exit plan — needs a product-level decision, not a refactor.
- `check_profit_ladder` MUST return after firing ONE rung per call.
  Firing all rungs at once on a single spike sells into thin upper-book
  liquidity. The `return  # One level per check` comment is the pin.
- `check_profit_ladder` `client_order_id = ladder-<sym>-L<pct>-<ET_YYYYMMDD>`
  format MUST be stable. Alpaca dedup key depends on it — changing the
  format loses idempotency across a retry window.
- Wheel stage strings (`stage_1_searching`, `stage_1_put_active`,
  `stage_2_shares_owned`, `stage_2_call_active`) are persisted in every
  live `wheel_*.json`. Renaming any of them breaks every existing state
  file on disk. Never rename without a migration.
- Call assignment MUST reset `state["cost_basis"] = None` when
  `shares_owned` hits 0. Without this, the next cycle's assignment math
  inherits stale cost basis → wrong tax lot on every wheel past #1.

### Post-60 (post-deploy user feedback fixes)
- ETFs (SPY/QQQ/SOXL/IBIT/MSOS/XL*/TQQQ/VIXY/GLD/TLT/JETS/ARKK/etc.) MUST short-circuit in `earnings_exit.should_exit_for_earnings` via the `_KNOWN_ETFS` frozenset. They have no earnings — fetching wastes rate limit + emits false-positive Sentry alerts.
- `earnings_exit_fetch_failed` Sentry breadcrumbs MUST dedup per `(symbol, error)` per ET calendar day via `_CAPTURED_TODAY`. The pre-market AH monitor fires 12+ times per hour; without dedup that's 60+ alerts per morning per failing symbol.
- `renderDashboard` MUST compare a *normalised* hash (`_lastAppNormHash`) that strips tick-varying timestamp text. Raw-string hash triggered a full DOM replace on every 10s tick → mobile scroll jitter.
- The in-place patch branch (quiet-tick fallback) MUST update `[title="Last data refresh"]` + `.data-freshness[data-label]` elements via `textContent` / `outerHTML`. Don't touch `innerHTML` of `#app` in this branch — it's the whole point of skipping.
- `freshnessChip(updatedAt, label)` MUST emit `data-label="..."` when label is non-empty so the in-place patch can regenerate the chip.
- Dashboard panels showing win rate (Readiness card + Paper-vs-Live comparison) MUST branch on `sc.win_rate_reliable`. When false, render `N=X` + "Need 5+" prompt — never anchor on alarmist `0%` from a 2-trade sample.
- Position Correlation rows MUST have `min-width:420px` and their wrapper MUST have `overflow-x:auto`. Without these, mobile viewports (≤380px) truncate the dollar column.

### Post-59 (final pre-live fixes)
- `insider_signals.parse_form4_purchase_value` MUST cache parse errors + bad accessions but NOT cache network errors. Form 4s never change post-filing; transient errors deserve retry next screener tick.
- Only transaction code "P" (open-market purchase) counts toward `total_value_usd`. Adding A/M/G/F/S would inflate the number with compensation/exercise/sale events — those aren't bullish signals.
- `_FORM4_XML_BUDGET_PER_CALL = 5` per `fetch_insider_buys`. Lowering loses signal; raising slows every screener run by ~1s per extra fetch.
- `migrations._user_migration_lock` MUST acquire per user (not globally) so one user's slow round-51 fetch doesn't block other users' migrations.
- CI `--cov-fail-under` MUST never decrease. Rounds that add tests should ratchet it up. Currently at 32% (actual 35% per round-61 measurement).

### Post-58 (JSON-audit fixes)
- `update_scorecard` correlation guard MUST route through `position_sector.annotate_sector` (OCC options resolve to underlying for sector lookup).
- `constants.SECTOR_MAP` is the single source of truth. Missing tickers surface as "Other" which breaks the correlation guard — add them here, not in consumers.
- Screener-annotated `will_deploy=False` picks MUST sort after `will_deploy=True` so the "top N" reflects what the deployer will pick up.
- `/api/data` MUST filter picks that have no real screener enrichment (no technical, zero momentum, zero recommended_shares) — the default-value tail is noise.
- `earnings_exit._fetch_next_earnings_from_yfinance` fetch failures MUST stamp `_LAST_FETCH_ERR[symbol]` with a distinct reason, and `should_exit_for_earnings` MUST emit `capture_message(event="earnings_exit_fetch_failed")` on any non-`no_future_unreported` failure. Silent fail-open held INTC through earnings in 2026-04.
- `insider_data.total_value_usd` MUST be `null` (not `0`) — we don't parse Form 4 XML yet, and `0` reads as "no insider buying" when `buy_count > 0`.

### Post-57 (audit fixes)
- Every `guardrails.json` RMW MUST hold `strategy_file_lock(gpath)` across
  read + write. Slow ops (Alpaca `/account` fetches, tier detection) MUST
  happen OUTSIDE the lock.
- `/api/calibration/override` MUST rate-limit at 3s per user/mode (bucket
  in `_CALIBRATION_OVERRIDE_LAST_WRITE`). Don't remove — cheap DoS mitigation.
- Failed trailing-stop raises MUST fire `observability.capture_message`
  with `event=trailing_stop_raise_failed` + `session` tag.
- `/api/data` MUST expose `extended_hours_trailing` (default True) so the
  dashboard AH chip renders correctly.
- `input[type=range]` CSS MUST keep `height: 32px` + `accent-color` +
  custom webkit/moz thumb styling. Browser default is unusable on iOS.

### Post-55 (after-hours monitor)
- `monitor_strategies(user, extended_hours=True)` MUST only raise trailing
  stops. No new buys, profit-takes, short covers. `test_ah_mode_skips_profit_ladder`
  pins the call-site gate.
- AH tick uses `should_run_interval(f"monitor_eh_{uid}", 300)` — 5-min key,
  distinct from 60s `monitor_{uid}`.

### Post-54 (calibration overrides + jitter)
- `window._lastAppHtml` hash-skip guards BOTH DOM write AND scroll restore.
- `/api/calibration/override` validates via whitelist + range checks +
  Alpaca-rule block. Don't remove.

### Post-52 (audit fixes)
- `fractional._cache_path`, `settled_funds._ledger_path` MUST raise
  `ValueError` on missing `_data_dir`. The /tmp fallback is gone — a
  programming bug elsewhere should not silently cross-contaminate users.
- All 3 new modules' RMW paths MUST go through module-local `_file_lock(path)`.
- `migrate_guardrails_round51` MUST track `backup_created_this_call` to avoid
  deleting backups created in prior calls.
- Short-sell block in `run_auto_deployer` MUST check `TIER_CFG.short_enabled`
  BEFORE consulting user config. Cash accounts can't override.
- Tier log dedup via `_last_runs` — only log on state change.

### Post-51 (activation for existing users)
- `migrate_guardrails_round51` MUST write backup before overwriting; MUST NOT
  overwrite existing backup file.
- `record_trade_close` MUST only record proceeds when `side="sell"` (long
  close). Short covers (`side="buy"`) do NOT generate settled cash.
- `run_auto_deployer` fractional routing MUST pass `fractional=True` to
  `smart_orders.place_smart_buy` (routes to market).
- PDT guard in `check_profit_ladder` MUST use buffer=1.
- All round-51 hooks MUST fail OPEN on exception — advisory code never blocks trading.

### Post-50 (portfolio calibration)
- `portfolio_calibration.detect_tier` MUST return None for equity <$500.
- Cash accounts MUST NEVER receive `short_enabled=True` even via user override.
- `pdt_tracker.can_day_trade` MUST check `pdt_applies` before consulting
  `day_trades_remaining`.
- `settled_funds.can_deploy` MUST passthrough for margin (no T+1 constraint).
  95% buffer on cash must stay.
- `fractional.size_position` MUST return `order_type_hint='market'` when
  `fractional=True`.
- `server.py` LOC cap 3000 (test_round6). Bump with care.

### Post-48 (cross-user privacy)
- `notify.py` MUST read `NOTIFICATION_EMAIL` from env — never hardcode.
  `test_notify_no_longer_has_hardcoded_recipient` fires on regression.
- `cloud_scheduler.notify_user` MUST pass BOTH `NOTIFICATION_EMAIL` and
  `DATA_DIR` per-user in subprocess env.
- `email_sender.drain_all` MUST NOT drain shared root queue — quarantine-only.
- `/api/scheduler-status` with `is_admin=True` returns unfiltered ONLY if `?all=1`.
- Enrichment fetches (wheel-status, news-alerts) MUST NOT call
  `renderDashboard()` from .then() callbacks. Store data, let next tick render.

### Post-46 (dual-mode paper + live)
- `auth.user_data_dir(user_id, mode="paper")` default stays `users/<id>/`.
  No migration, back-compat.
- `user["_mode"]` injected by `_build_user_dict_for_mode` is source of truth.
- Dedup keys like `_wheel_deploy_in_flight`, `_cb_state`, `_auth_alert_dates`:
  paper keeps plain `user_id`; live uses `f"{id}:live"`.
- `notify_user` prefixes live messages with `[LIVE]`.
- Session-mode fallback: if `session_mode == "live"` but no live keys saved,
  `check_auth` silently falls back to paper view.

### General
- **ET everywhere**. Never `datetime.now(timezone.utc)`; always
  `from et_time import now_et`.
- **Money always Decimal-internal** on compute paths; float on JSON boundary.
- **Per-user file isolation is security-critical**. Migration from shared
  DATA_DIR is RESTRICTED to `user_id == 1` (bootstrap admin). Regression here
  has caused cross-user auto-trading before.

---

## Deploy mechanics

- Push to `main` → Railway auto-deploys within ~2 min.
- Confirm: `curl https://stockbott.up.railway.app/api/version` — `commit`
  field matches `git rev-parse HEAD`.
- Required Railway env vars: `MASTER_ENCRYPTION_KEY` (boot-blocking) + Alpaca
  creds.
- Changing `MASTER_ENCRYPTION_KEY` **invalidates all stored credentials**.

---

## Stack / conventions

- Python 3.12 (CI), 3.11 (sandbox)
- stdlib-first. External: `cryptography>=42`, `zxcvbn>=4.4.28`, `sentry-sdk`
  (optional), `pytest`, `pytest-cov`, `ruff`
- SQLite via `sqlite3` for auth/sessions/audit log
- Per-user runtime state in `$DATA_DIR/users/<id>/`
- JSON files: trade journal, wheel state, guardrails, scorecard
- HTTP via `http.server.ThreadingHTTPServer` (no framework)
- Threading for scheduler (no celery/rq)
- Frontend: single `templates/dashboard.html` (~6K LOC inline JS+CSS,
  vanilla, no build). Always `node --check` extracted JS before merging
  (round-27 lesson).

---

## File layout cheat sheet

```
server.py              HTTP server + handler + scheduler boot
cloud_scheduler.py     Background scheduler (~4000 LOC, 60+ fns)
scheduler_api.py       Alpaca API helpers + CB + rate limiter (extracted R17)
auth.py                SQLite auth + token bucket + sessions
observability.py       Sentry wrapper + critical_alert()
logging_setup.py       JSON log formatter + print shim
trade_journal.py       Journal trim/archive + flock
update_dashboard.py    Screener (runs as subprocess every 30min)
update_scorecard.py    Scorecard rebuild (daily close)
smart_orders.py        Limit-at-mid with market fallback
wheel_strategy.py      Options wheel state machine
tax_lots.py            FIFO tax-lot reconciler
portfolio_risk.py      Beta-exposure + drawdown sizing
risk_sizing.py         ATR stops + vol-parity sizing
portfolio_calibration.py   Tier detection from Alpaca /account (R50)
fractional.py          Fractionable-asset cache + sizing (R50)
pdt_tracker.py         Pattern Day Trader rule awareness (R50)
settled_funds.py       T+1 settled-cash ledger (R50)
migrations.py          Boot-time migrations (R37)
state_recovery.py      Boot-time consistency validator
error_recovery.py      Orphan detection
extended_hours.py      get_trading_session() — pre_market/market/after_hours/closed
earnings_exit.py       Pre-earnings close (R29)
handlers/
  auth_mixin.py        Login, signup, settings, reset
  admin_mixin.py       /api/admin/* (user management)
  strategy_mixin.py    Deploy / pause / stop / preset
  actions_mixin.py     Refresh, kill-switch, close, cancel
templates/             Jinja-free HTML (loaded at boot)
static/                PWA manifest + SW + SVG icon
tests/                 pytest suite (~745 passing after R54-55)
docs/                  DECIMAL_MIGRATION_PLAN, MONITORING_SETUP
```

---

## Known test quirks

- `tests/test_auth.py::test_password_strength_rejects_weak` FAILS locally
  (sandbox lacks zxcvbn, falls back to 8-char min; "password" passes). CI has
  zxcvbn.
- `tests/test_dashboard_data.py::test_trading_session_is_computed_live_not_from_stale_json`
  FAILS locally (sandbox blocks outbound network). CI deselects explicitly.

Both pre-existing and unrelated to any recent PR.

---

## Tests that `importlib.reload()` auth-dependent modules

MUST set `MASTER_ENCRYPTION_KEY` via monkeypatch — pass locally (env var set
in shell) but fail CI. Use the `_reload(monkeypatch)` pattern:

```python
def _reload(monkeypatch):
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        _sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler
```

Round-46 CI hotfix + round-55 tests use this pattern.

---

## Round history (condensed — see CHANGELOG.md for full detail)

| Rounds | Headline |
|---|---|
| R11-12 | Audit sweep; beta-exposure gate fix (was dead code); 110+ tests |
| R13-17 | Decimal migration; exception hardening; cloud_scheduler split → scheduler_api.py |
| R18-22 | Handler-mixin tests; smart_orders full-flow; partial-fill blended cost basis; Gemini 2.5-flash; sentiment lines on pick cards |
| R23-28 | Session idle timeout; scheduler death watchdog; subprocess zombie tracking; 🚨 Breaking News on pick cards + positions; universal earnings exit (R29); signup invites (R26); exception-handling round-2 (R28) |
| R29-35 | Pre-earnings exit for all strategies; mobile scroll polish; journal undercount fix (R33); Today's Closes panel + orphan close (R34); real Position Correlation by sector (R35) |
| R36-41 | Admin-panel Revoke Invite + Make/Revoke Admin; weekly learning path bug; dual-mode paper+live (R45); auto-orphan-fix (R44); wheel close journaling (R42); GDPR export + delete (R40); R41 full audit — 8 real bugs + 0 false positives |
| R42-46 | R42 wheel close journaling; R44 auto-orphan-fix + scroll jitter; R45 dual-mode paper+live; R46 dual-mode audit fixes + CI hotfix |
| R47-48 | R48 CRITICAL cross-user privacy fixes (hardcoded email recipient, shared email queue) |
| R49-52 | R49 staleness tuning; R50 portfolio auto-calibration (6 tiers, $500-$1M+); R51 activation for existing users; R52 full tech-stack audit (11 fixes, 16 tests) |
| R53 | Nav-tab active state + desktop modal sizing |
| R54 | Calibration per-key overrides + hash-skip jitter fix (THIS PR) |
| R55 | After-hours trailing-stop tightening (THIS PR) |

---

## Likely next-session topics

- User flagged daily-close math concern with screenshot — investigate.
- R54-55 PR merge + Railway deploy verification.
- Round-56 candidates: **daily close math fix** (user-flagged),
  options after-hours handling (wheel currently skipped in R55).
- User is running paper validation — no blocking work queued beyond that
  + live-key rotation on 2026-05-15.

See `GO_LIVE_CHECKLIST.md` for pre-flip-to-live gating list.
