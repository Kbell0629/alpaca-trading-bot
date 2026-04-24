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
3. `MASTER_ENCRYPTION_KEY=$(python3 -c 'print("e"*64)') python3 -m pytest tests/ --deselect tests/test_dashboard_data.py::test_trading_session_is_computed_live_not_from_stale_json --deselect tests/test_auth.py::test_password_strength_rejects_weak --deselect tests/test_audit_round12_scheduler_latent.py::test_ruff_clean_on_real_bug_rules -q`
   — expect **1337 passing, 3 deselected** after round-61 pt.6 + follow-ups (PRs #116 + #117).
4. `ruff check .` — clean.
5. Validate dashboard JS: `awk '/^<script>/,/^<\/script>/' templates/dashboard.html | grep -v '^<script>' | grep -v '^</script>' > /tmp/dash.js && node --check /tmp/dash.js`

**GitHub MCP reconnects late in some sessions.** Use web UI for PRs when
`mcp__github__*` tools aren't in the tool list. PRs that modify
`.github/workflows/*.yml` require one-click workflow approval at
https://github.com/Kbell0629/alpaca-trading-bot/actions before CI runs.

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

## Current session state (2026-04-24 — round 61 pt.7 in progress)

**Pt.7 kickoff (#119) merged to main.** `screener_core.py` extracted
from `update_dashboard.py` with 7 pure functions — `pick_best_entry_strategy`,
`trading_day_fraction_elapsed`, `score_stocks` (the 190-line heart
of the screener), `apply_market_regime`, `apply_sector_diversification`,
`calc_position_size`, `compute_portfolio_pnl`. Update_dashboard.py
stays in the `omit` list (still I/O-heavy) but `screener_core.py`
is NOT omitted so pytest-cov can see the pure math. **Still to do
in pt.7**: behavioral tests for screener_core + extract
`scorecard_core.py` from `update_scorecard.py`. Test count still
~1392 (CI green; no behavior change from the extraction).

### What landed in round 61

**Coverage push (5 PRs, 258 tests):**

| PR | Theme | Tests | Status |
|---|---|---|---|
| #102 pt.1 | `monitor_strategies` + `check_profit_ladder` grep-pins + behavioral | +29 | ✅ merged |
| #103 pt.2 | `run_auto_deployer` + wheel state machine grep-pins | +42 | ✅ merged |
| #104 pt.3 | CLAUDE.md/CHANGELOG sync + floor 30→32 + monitor_strategies behavioral | +7 | ✅ merged |
| #105 pt.4 | PDT/settled-funds/fractional + auto_deployer behavioral + wheel helpers | +118 | ✅ merged |
| #106 pt.5 | scheduler_api + yfinance_budget behavioral + initial scheduler-jitter fix + floor 32→36 | +62 | ✅ merged |

**Bug fixes (8 PRs after the live-traffic shakedown):**

| PR | What |
|---|---|
| #107 | Jitter fix #1: strip `$` and `%` from `renderDashboard` normHash (price-tick rewrite prevention) |
| #108 | Jitter fix #2: CSS `contain: layout style` on every refreshing section + normalized hash-skip in `refreshFactorHealth` (sibling reflow fix) |
| #109 | Jitter fix #3: atomic children swap via `<template>` + `replaceChildren` for `#app` + `min-height: 100vh` on `#app` (empty-frame collapse fix) |
| #110 | `[orphan]` close tag + TRAILING STOP label on short SOXL fixes (error_recovery journals open + `_mark_auto_deployed` skips closed strategy files) |
| #111 | Jitter fix #4: preserve async-panel content (`schedulerPanel`, `factorHealthPanel`, etc.) across `#app` rewrites so `<div>Loading…</div>` placeholders don't flash |
| #112 | Email notifications: short-force-cover now emails (`info` → `exit`) + new `/api/email-status` endpoint + dashboard 📧 chip |
| #113 | Jitter fix #5: atomic children swap helper applied at the panel level (renderSchedulerPanel + refreshPerfAttribution + refreshTaxReport + refreshFactorHealth) — fixes Recent Activity log-line jitter |
| #114 | AUTO/MANUAL mislabel fix: `_mark_auto_deployed` falls back to `trade_journal.json` when no strategy file matches a position |
| #115 | Docs sync PR closing out round-61: CLAUDE.md/CHANGELOG/README updated to reflect every PR shipped |
| #116 pt.6 | **Mock WSGI harness for DashboardHandler** — `tests/conftest.py::http_harness` subclasses `DashboardHandler` without the socket, auto-injects session + CSRF cookies. +200 tests. `server.py` 7% → 42%; handlers/*.py 0% → 13-44%. Total 39.79% → **47.19%**. Floor 36 → **45**. |
| #117 pt.6 follow-ups | Admin-and-target pattern lets admin endpoints run against REAL user IDs (not just auth gates): set-active, reset-password, update-user, delete-user, set-admin, create-backup. Plus invite lifecycle end-to-end, user toggles (live-mode, track-record-public, scorecard-email), kill-switch round-trip, force-auto-deploy, force-daily-close, forgot-password, logout-clears-server-session. `handlers/admin_mixin.py` 33% → **74%**; `handlers/actions_mixin.py` 38% → **56%**. Total 47.19% → **51.04%**. +57 tests. |
| #118 docs sync | CLAUDE.md + CHANGELOG.md + README.md updated for pt.6 close-out. New "Testing (For Developers)" README section with http_harness usage template. |
| #119 pt.7 kickoff | **Extract `screener_core.py`** from update_dashboard.py — 7 pure functions (pick_best_entry_strategy, trading_day_fraction_elapsed, score_stocks [190 LOC], apply_market_regime, apply_sector_diversification, calc_position_size, compute_portfolio_pnl). External deps (pead, capitol_trades) injected as callables. update_dashboard.py still in `omit` but screener_core.py is NOT omitted → pytest-cov can see the math. No behavior change; CI green. |

**Total round-61: 18 PRs shipped. Coverage 34% → 51.04%. Floor 30% → 45%.**

### Mock WSGI harness (post-pt.6 — use it for any HTTP endpoint test)

The `http_harness` fixture in `tests/conftest.py` is the template for
every future HTTP-endpoint test. Usage:

```python
def test_my_endpoint(http_harness):
    http_harness.create_user()  # seeds user #1 as admin, gets cookie + CSRF
    resp = http_harness.get("/api/my-endpoint")
    assert resp["status"] == 200
    assert resp["body"]["foo"] == "bar"
    # Or for POSTs (CSRF + JSON body auto-injected):
    resp = http_harness.post("/api/my-endpoint", body={"k": "v"})
```

`resp` shape: `{"status": int, "headers": dict, "headers_list": list,
"body": parsed-json-or-str, "raw": bytes}`. The fixture also includes
`http_harness.logout()` to drop the session for anonymous-access tests.

The pattern for the `BaseHTTPRequestHandler` subclass override (skip
`__init__`, override `send_response`/`send_header`/`end_headers` to
record instead of socket-write) lives in the fixture — next agents
don't need to re-derive it.

### Module coverage milestones

- `server.py` 7% → **47%** (via mock WSGI harness, pt.6 + follow-ups)
- `handlers/admin_mixin.py` 0% → **74%** (admin-and-target pattern exercises real user IDs)
- `handlers/actions_mixin.py` 0% → **56%** (kill-switch, force-deploy, force-daily-close, refresh)
- `handlers/strategy_mixin.py` 0% → **49%** (deploy validation, pause/stop, apply-preset, toggle-short-selling)
- `handlers/auth_mixin.py` 0% → **49%** (login/signup/logout, password change, settings update, invite consumption)
- `scheduler_api.py` 41% → ~85% • `yfinance_budget.py` 61% → 92%
- `wheel_strategy.py` 33% → 47% (helpers covered; state-machine
  transitions still grep-pinned only)
- `pdt_tracker.py` 60% → >90% • `settled_funds.py` 76% → >90%
- `fractional.py` 60% → >80%
- `cloud_scheduler.py` ~31% (mostly flat — pt.6 didn't touch it
  directly; behavioral tests hit kill-switch + drawdown paths only.
  A future pt could target the 60+ remaining fns in this file.)

### Scroll-jitter fix history (FIVE rounds — mandatory reading before touching refresh)

The jitter took five iterative fixes because it came from five
independent failure modes. **All five remain necessary; do not
"simplify" any of them.** When the user reports refresh-related
issues, check this list first — most likely the same patterns.

1. **R60** — added `_lastAppNormHash` to `renderDashboard` stripping
   timestamps from the hash so quiet ticks skip `#app.innerHTML` swap.
2. **R61 #107** — extended the same regex to strip `$X.XX` and `±X.X%`
   from the hash. Price-tick-only changes now flow through the quiet
   branch instead of triggering full rewrites every 10s.
3. **R61 #108** — added CSS `contain: layout style` + `overflow-anchor:
   auto` on every refreshing section. Plus normalized hash-skip in
   `refreshFactorHealth` (it embeds a freshness chip that was making
   the panel rewrite every 10s). Browser-native containment so panel-
   internal updates don't reflow siblings.
4. **R61 #109** — atomic children swap for `#app`: parse new HTML into
   a `<template>` element, then transfer via `replaceChildren` in one
   atomic DOM op. `#app.innerHTML = x` was destroying children first
   then rebuilding, leaving an empty-frame where document height
   collapsed and the browser clamped scrollY. Plus `#app { min-height:
   100vh; overflow-anchor: auto; }` as a second line of defense.
5. **R61 #111** — preserve **async-populated panel content** across
   the `#app` swap. Snapshot each async panel's current `innerHTML`
   before `replaceChildren`, transplant into the new template's matching
   placeholder. User was seeing "Loading scheduler status" flash
   during refresh because the new `#app` shell had empty placeholders
   for `schedulerPanel`, `factorHealthPanel`, etc.
6. **R61 #113** — atomic children swap **at the panel level too**.
   `renderSchedulerPanel`, `refreshPerfAttribution`, `refreshTaxReport`,
   `refreshFactorHealth` were all using `panel.innerHTML = html`
   directly. Same empty-frame bug as #109, just one level deeper.
   New helper `atomicReplaceChildren(panelEl, newHtml)` does the
   `<template>` + `replaceChildren` swap AND preserves descendant
   `scrollTop` (so `.sched-log-box` internal scroll stays put).

If jitter resurfaces on a NEW area, check whether it's a 6th failure
mode or a regression of one of the above. The pinning tests are in
`tests/test_round61_pt6_prep_*.py` — they assert source patterns so
a refactor that "simplifies" any of these will fail loudly.

### Bug fixes from the live-traffic shakedown (must-not-regress)

- **#110 — `[orphan]` close tag**: `error_recovery.py` now appends a
  `trade_journal.json` open entry alongside any strategy file it
  creates. Without this, when the recovered position later closes via
  stop-trigger, `record_trade_close` can't find a matching open and
  falls into the synthetic `orphan_close` path. Look for
  `auto_recovered=True` flag on backfilled opens.
- **#110 — `_mark_auto_deployed` skips closed strategy files**: stale
  `trailing_stop_SOXL.json` (status=closed) was claiming the SOXL
  symbol over the active `short_sell_SOXL.json`. Status-skip list:
  `closed/stopped/cancelled/canceled/exited/filled_and_closed`. Also:
  `short_sell` priority bumped to 2 (same tier as `trailing_stop`).
- **#114 — AUTO/MANUAL journal fallback**: `_mark_auto_deployed` now
  falls back to `trade_journal.json` when no strategy file matches.
  Recognizes `deployer in (cloud_scheduler, wheel_strategy,
  error_recovery)`. Walks newest→oldest with `setdefault` so the
  most-recent open wins on re-opens. Strategy files still preferred
  when both exist. Option positions try BOTH the OCC contract symbol
  and the underlying for the journal lookup.
- **#112 — Email diagnostics**: new `/api/email-status` endpoint
  returns `{enabled, queued, sent_today, failed_recent, last_sent_at,
  recipient, dead_letter_count}`. Dashboard 📧 header chip shows
  state at a glance: 📧 OFF (red, SMTP creds missing), 📧 NO ADDR
  (orange, recipient not set), 📧 N STUCK (orange, >10 unsent), 📧
  N queued (dim), 📧 N today (green). Click to see full diagnostic.
  Short force-cover now emails (`exit` not `info`).

### Road to 80% coverage (the path forward)

| Phase | Target | Scope | Est effort | Status |
|---|---|---|---|---|
| **pt.5** | 39% | scheduler_api + yfinance_budget + helper modules | 1 PR | ✅ done |
| **pt.6** | ~65% | **Mock WSGI harness for `server.py` + `handlers/*.py`.** Biggest lever left — `server.py` was 1708 statements at 7%. Built the harness in `tests/conftest.py::http_harness` + 260 endpoint tests across #116 (base) + #117 (follow-ups). Landed at **51%** total (below the 65% target because cloud_scheduler.py is still ~31% — not within pt.6's scope). | 2 PRs, 1 day | ✅ done (51% actual) |
| **pt.7** | ~78% | Refactor `update_dashboard.py` + `update_scorecard.py` to extract pure scoring math into `screener_core.py` / `scorecard_core.py`. Un-omit from coverage. Requires source changes — needs careful review. | 1-2 PRs, 2-3 days | **in progress** — `screener_core.py` landed via #119; behavioral tests + `scorecard_core.py` still to do |
| **pt.8 (optional, parallel)** | adds JS coverage | Vitest + jsdom for `templates/dashboard.html`'s ~6000 LOC. Without this, Python-only caps ~78%. | 1 PR, 1-2 days | optional |
| **pt.9 (stretch)** | `cloud_scheduler.py` deeper | Still ~31% after pt.6 because pt.6 focused on the HTTP surface. Could add behavioral tests for the 60+ remaining scheduler functions (run_daily_close, process_strategy_file branches, wheel orchestration). Adds +5-8 points. | 1 PR, ~1 day | future |

### pt.6 DONE — harness + test pattern reference

The harness lives at `tests/conftest.py::http_harness`. Five test files
use it and should be the template for any future HTTP-endpoint test:
  * `tests/test_round61_pt6_harness_smoke.py` — 6 smoke tests
  * `tests/test_round61_pt6_wsgi_endpoints.py` — 76 auth-gate + shape
  * `tests/test_round61_pt6_wsgi_authed_flows.py` — 82 authed flows
  * `tests/test_round61_pt6_strategy_mixin_flows.py` — 21 deploy paths
  * `tests/test_round61_pt6_followup_admin_actions.py` — 57 admin flows

Usage:

```python
def test_my_endpoint(http_harness):
    http_harness.create_user()  # user #1 is auto-admin
    resp = http_harness.get("/api/my-endpoint")
    assert resp["status"] == 200
    assert resp["body"]["foo"] == "bar"
    # POSTs auto-inject CSRF cookie + header:
    resp = http_harness.post("/api/my-endpoint", body={"k": "v"})
```

For admin tests, use the **admin-and-target pattern** (see
`test_round61_pt6_followup_admin_actions.py::TestAdminSetActive._create_admin_and_target`):
create admin1 + target user, logout, re-auth as admin so admin
endpoints have a real target user ID to operate on.

### pt.7 IN PROGRESS — screener_core.py landed, more extraction + tests next

**Kickoff (#119) SHIPPED:** `screener_core.py` now hosts the pure
scoring math extracted from `update_dashboard.py`. Functions:

  * `pick_best_entry_strategy(scores, entry_strategies)` — argmax
  * `trading_day_fraction_elapsed(now=None)` — session time math
  * `score_stocks(snapshots, *, entry_strategies, sector_map,
                    min_price, min_volume, copy_trading_enabled,
                    pead_enabled, day_fraction=None,
                    pead_score_fn=None, copy_score_fn=None)`
    — the 190-line heart of the screener. External deps
    (pead, capitol_trades) injected as callables so the core module
    has zero network/disk dependencies.
  * `apply_market_regime(picks, regime)` — bias annotation
  * `apply_sector_diversification(picks, max_per_sector, top_n)`
  * `calc_position_size(price, volatility, portfolio_value, max_risk_pct)`
  * `compute_portfolio_pnl(positions, portfolio_value)`

`update_dashboard.py` still in `omit` list — imports from
`screener_core` via thin compat wrappers. `screener_core.py` is NOT
omitted — pytest-cov sees every statement.

**Still to do in pt.7:**
1. Write behavioral tests for `screener_core.py` (pure functions, so
   cheap — should hit 90%+ coverage in one test file, adding
   ~3-5 percentage points to total coverage)
2. Extract `scorecard_core.py` from `update_scorecard.py` using the
   same pattern; add tests (~2-3 more points)
3. Measure total coverage impact; ratchet CI floor 45 → 48-50

**Payoff when complete:** total coverage 51% → ~56-60%. Remaining
gap to 80% filled by pt.8 (Vitest for dashboard JS).

### Structural limits (won't go away with more Python tests)

- **Dashboard JS (~6000 LOC) is invisible to `pytest-cov`.** Until
  pt.8 (Vitest), total coverage caps around 75-80% even with perfect
  Python. Test quality is still improving; the number is just capped.
- **Subprocess-driven modules** (`update_dashboard.py`,
  `update_scorecard.py`, `capital_check.py`) need the un-omit
  refactor before coverage even counts them.
- **`server.py` now 47%** — pt.6 moved the big lever. Further gains
  on server.py from here need either pt.7 (refactor) or more happy-
  path tests that drive Alpaca-facing code (stubbed).

### Picking up pt.7 (checklist for next session)

1. `git pull --ff-only origin main`
2. `cat CLAUDE.md` (this section first — it's the current plan)
3. `cat CHANGELOG.md` (skim last 3 rounds)
4. Confirm tests pass: `MASTER_ENCRYPTION_KEY=$(python3 -c 'print("a"*64)') pytest tests/ --deselect tests/test_dashboard_data.py::test_trading_session_is_computed_live_not_from_stale_json --deselect tests/test_auth.py::test_password_strength_rejects_weak --deselect tests/test_audit_round12_scheduler_latent.py::test_ruff_clean_on_real_bug_rules -q` — expect **1337 passing, 3 deselected**
5. Start pt.7 work (refactor `update_dashboard.py` + `update_scorecard.py` to extract pure scoring into `screener_core.py` / `scorecard_core.py`; un-omit from coverage). This is a source refactor — read the "pt.7 specific handoff" section above BEFORE touching production logic.

### Previously in-flight (now merged)
Rounds 54-60 all merged via PRs #97-#101, 2026-04-22 / 2026-04-23.
Round-61 pt.1-3 merged via #102/#103/#104, 2026-04-23.
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
