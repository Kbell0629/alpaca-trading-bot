# CLAUDE.md — Session Context

This file is read first by Claude Code when it opens this repo. It's the
one-page answer to "what is this project, what's the current state, and
how do I pick up where the last session left off?"

---

## What this is

Autonomous multi-user stock-trading bot. Screens ~12K US equities every
30 min, auto-deploys top picks across 6 strategies, manages exits,
handles kill-switch + drawdown guardrails, and runs 24/7 on Railway.

- **Dashboard**: https://stockbott.up.railway.app
- **Repo**: `Kbell0629/alpaca-trading-bot`
- **Primary branch**: `main` (protected: PR + CI required, no force push)
- **Claude agent branch pattern**: `claude/<short-description>` — PR +
  squash-merge after CI green.
- **Sessions**: paper-trading for the **30-day validation window**
  (started 2026-04-15). Live-trading path is wired but off.

## Alpaca account state

- Paper account active, $100k seed
- Live trading toggleable in-app (Settings → 🔴 Live Trading) — OFF
- `MASTER_ENCRYPTION_KEY` is **required** on Railway (auth.py refuses
  to boot without it — the PLAIN-fallback path was retired in PR #2)

---

## How to pick up the work

**Every session, start with these checks:**

1. `git checkout main && git pull` — current HEAD should have the
   round-12 audit sweep merged (commits `#2` through `#15`).
2. `cat CLAUDE.md` (this file) + `cat README.md` for user-facing state.
3. `cat IMPLEMENTATION_STATUS.md` for the running changelog.
4. `cat docs/DECIMAL_MIGRATION_PLAN.md` if the user asks about money math.
5. `MASTER_ENCRYPTION_KEY=<any-64-char-string> python3 -m pytest tests/`
   to confirm the test suite is green (expect ~240 passing; two sandbox-
   only failures don't count — see "known test quirks" below).

**Common next-steps the user may ask about** (ordered by likelihood):

- "Anything bugged?" — run the audit playbook in the `Audit playbook`
  section below.
- "Fix X" — open a feature branch, write the fix + tests, PR, merge.
- "Ship it" — look at current merged PRs list with `git log --oneline
  main | head -20`.
- "Ruff + coverage status" — `ruff check .` should be clean; CI floor
  is 15% (ratchet baseline; nudge up when you add tests).

---

## The round-12 audit sweep (what shipped in the last session)

Session-long audit across 5 parallel Explore agents (security, database,
trading logic, UI/mobile, test coverage). 15 PRs merged to main, 110+
tests added. **The most consequential finding**: the `portfolio_risk`
beta-exposure safety rail had been silently disabled in production since
round-11 because `run_auto_deployer` referenced three variables before
they were defined — `except Exception` swallowed the NameError on every
run. Fixed in PR #15 (`ruff check` F821 surfaced it).

### Shipped this round

| PR | Commit | Subject |
|---|---|---|
| #2 | `b6c9bcd` | Sentry wiring + MASTER_KEY mandatory |
| #3 | `d1d7c3e` | JSON logging + `/api/version` + a11y + contrast + SRI prep |
| #4 | `9d6569a` | SRI hashes pinned |
| #5 | `966e531` | Journal trim + structured-print shim + test cleanup |
| #6 | `dcdf166` | Decimal phase 1 (`tax_lots.py`) |
| #7 | `16afdf5` | Login token-bucket rate limit |
| #8 | `98d3f5c` | Decimal phase 2 (`update_scorecard.py`) |
| #9 | `03becfc` | Decimal phase 3 (`portfolio_risk.py`) |
| #10 | `c73c288` | Decimal phase 4 (`wheel_strategy.py`) + 39 fuzz tests |
| #11 | `7353b65` | Decimal phase 5 (`smart_orders` + `calc_position_size`) + 30k fuzz |
| #12 | `c6827fa` | Password-reset TOCTOU, session IP norm, capital_check fallback, coid entropy, modal responsive, SW JSON |
| #13 | `bc40d49` | XSS hardening, modal focus trap, forgot-password constant-time |
| #14 | `d06760d` | Kill-switch `threading.Event`, trim file lock, wheel split guard |
| #15 | `3ad82a7` | CI tooling (ruff + coverage ratchet) + **4 latent bugs** (beta-exposure gate was DEAD CODE in prod) |

### Key behaviour changes to know about

1. **Logging**: everything emits JSON envelopes now. Railway's log
   viewer parses + pretty-prints them. See `logging_setup.py` — the
   `init()` call monkey-patches `builtins.print` to route through
   `logging.getLogger(caller_module)` with level auto-classified
   from `[ERROR]` / `[WARN]` prefixes.
2. **All money math is Decimal-internal**. `tax_lots.py`,
   `update_scorecard.py`, `portfolio_risk.py`, `wheel_strategy.py`,
   `smart_orders.py`, `update_dashboard.calc_position_size` all use
   `_dec()` + `_to_cents_float()` helpers. JSON boundary unchanged
   (still float-with-2dp). See `docs/DECIMAL_MIGRATION_PLAN.md`.
3. **Kill switch**: `cloud_scheduler.request_deploy_abort()` sets a
   `threading.Event` that in-flight deploy loops check between
   symbols. Atomic abort, no more 100-300ms window.
4. **Trade journal trims itself** at 3:15 AM ET daily. Closed trades
   >2 years old move to `trade_journal_archive.json` in the same
   dir. `DashboardHandler._load_full_journal()` reads both for
   lifetime stats (tax/export paths); `/api/trade-heatmap` reads
   live-only.
5. **Password-reset TOCTOU**: `auth.consume_reset_token` now does
   atomic `UPDATE ... WHERE used=0` before changing password. Two
   concurrent reset attempts can't both succeed.
6. **Login rate limit**: in-memory token bucket (BURST=10, REFILL=0.2/s
   per `(ip, username)`) in front of the SQLite 5-per-15-min window.
7. **Beta-exposure gate**: FIXED. Previously dead code. Now fires
   correctly in `run_auto_deployer`. Watch for the `Beta exposure:
   …% beta-weighted` log line to confirm it's running.

---

## Outstanding items (user-flagged or deferred)

### Flagged for user input — all cleared (2026-04-19)

- ~~**PWA icons**: manifest needed iOS PNG fallbacks~~ — DONE in PR #25.
- ~~**Sentry DSN rotation**: the leaked DSN in early docs~~ — DONE,
  user rotated + set new `SENTRY_DSN` on Railway.
- ~~**MASTER_ENCRYPTION_KEY** set on Railway~~ — DONE, user confirmed
  variable is set. DO NOT rotate (invalidates stored creds).
- ~~**Notifications wiring** (ntfy + email + Sentry alerts)~~ — DONE.

Only remaining pre-live prereqs live in `GO_LIVE_CHECKLIST.md`:
finish 30-day paper validation window + generate dedicated live
Alpaca keys. Both are user-only operational steps.

### Known gaps — closed out in rounds 13-17

| Item | Status | Round |
|---|---|---|
| `cloud_scheduler.py` 3800 LOC monolith | **Split**: API helpers + CB + RL extracted to `scheduler_api.py` | #17 |
| Handler mixin tests (auth/strategy/actions/admin) | **21 tests** in `test_handler_mixins.py` | #12 |
| `smart_orders.place_smart_buy` full flow | **14 tests** in `test_smart_orders_full_flow.py` | #12 |
| Strategy modules zero tests | **18 tests** added across pead/short/earnings/insider | #16 |
| Wheel stock-split auto-resolve | **Done**: `_detect_split_since` + `yf_splits` | #13 |
| 5-layer state without reconciliation | **Boot-time validator** in `state_recovery.py` | #16 |
| Critical-alert email broken (silent since round-11) | **Fixed**: import + signature | #14 |
| Daily -3% loss alert had no notification | **Wired** through `critical_alert` | #15 |
| 401/403 silent credential rot | **Detect + per-day alert dedup** | #15 |
| smart_orders partial-fill cost basis drift | **Blended avg** in buy + sell | #15 |
| `_load_with_shared_fallback` untested invariant | **Pinning tests** in `per_user_isolation.py` | #15 |

### Truly remaining gaps (defer + accept)

- **Coverage ratchet**: CI floor is 15% (measured baseline ~21% after
  rounds 13-17). Nudge up when you add test suites; pyproject.toml
  omits one-shot CLIs from the measurement.
- **Structured logging phase 2**: `logging_setup.init()` installs a
  `builtins.print` shim so all existing prints get auto-classified
  JSON envelopes. Explicit `log.info()` migration still pending for
  ~400 prints in strategies / one-shots. Not blocking — shim handles it.
- **`options_flow` + `options_analysis`** unit tests — ~90% network-
  bound. Pure-helper tests would have low marginal value vs the
  full-mock investment.

### Accept-as-is (not bugs)

- forgot-password enumeration via rate-limit exhaustion (bucket mitigates)
- kill-switch latency up to one scheduler tick (~100ms — acceptable scale)
- `_ServerProxy` late-binding circular-import workaround (unavoidable
  because server.py runs as `__main__`)

### Explicitly won't fix (user's own audit flagged them as skip)

These are not bugs — the user / audit explicitly classified them as
accept-as-is:
- forgot-password enumeration via brute-force rate-limit exhaustion
  (mitigated by bucket)
- kill-switch latency up to one scheduler tick (~100ms — acceptable
  for paper + single-user-ish scale)

---

## Audit playbook (when user says "check for bugs")

This is the playbook the last session ran. Reuse for future audit
requests:

1. **Read this file**, then `git log --oneline main | head -30` to see
   what's shipped recently.
2. **Spawn 5 parallel Explore agents** (use `subagent_type: Explore`)
   scoped to: security, database/concurrency, trading logic,
   UI/UX/mobile, test coverage. Give each <1000-word reply budget.
3. **Triage**: auto-fix the clear-cut wins in ONE PR; flag
   architecturally significant ones for user decision.
4. **Run `ruff check . --select F821,B023`** — these two rules
   historically surface real bugs (undefined names, loop captures).
5. **Run full pytest** — must stay green (two sandbox-only failures
   on local are expected; CI on GitHub passes all).
6. **Deliver a report** with three sections: "fixed", "needs your
   decision", "deferred with known gaps".

---

## Stack / conventions

- Python 3.12 (CI), 3.11 (sandbox tested)
- stdlib-only where possible. External deps in `requirements.txt`:
  `cryptography>=42.0.0`, `zxcvbn>=4.4.28`, `sentry-sdk` (optional),
  `pytest` (dev), `pytest-cov` (dev), `ruff` (dev)
- SQLite (via `sqlite3`) for auth, sessions, audit log, login attempts
- All per-user runtime state in `$DATA_DIR/users/<id>/`
- JSON files for trade journal, wheel state, guardrails, scorecard
- HTTP via `http.server.ThreadingHTTPServer` (no framework)
- Background scheduler via `threading` (no celery / rq)
- Frontend: single-page dashboard in `templates/dashboard.html` (~6K
  LOC inline JS+CSS) — vanilla JS, no build step

**ET everywhere**. Never `datetime.now(timezone.utc)`; always
`from et_time import now_et`. Money always Decimal-internal on compute
paths; float on JSON boundary.

**Per-user file isolation is security-critical**. Migration from
shared `DATA_DIR` is RESTRICTED to `user_id == 1` (the bootstrap
admin). Regression here has caused cross-user auto-trading before.

---

## Known test quirks

- `tests/test_auth.py::test_password_strength_rejects_weak` FAILS
  locally because the sandbox lacks `zxcvbn`; `auth.check_password_
  strength` falls back to 8-char-minimum which "password" passes.
  CI has zxcvbn and the test passes.
- `tests/test_dashboard_data.py::test_trading_session_is_computed_
  live_not_from_stale_json` FAILS locally because the sandbox blocks
  outbound network; test tries `alpaca_get("/account")`. CI blocks
  this too — CI deselects the test explicitly.
- Both are pre-existing and unrelated to recent PRs.

---

## Deploy mechanics

- Push to `main` → Railway auto-deploys within ~2 min.
- Confirm via `curl https://stockbott.up.railway.app/api/version` —
  `commit` field should match current `git rev-parse HEAD`.
- Railway env vars live in the Variables tab. Required:
  `MASTER_ENCRYPTION_KEY` (mandatory, boot-blocking) + Alpaca credentials.
- Any `MASTER_ENCRYPTION_KEY` change **invalidates all stored
  credentials** (users must re-enter via Settings).

---

## File-layout cheat sheet

```
/                          project root
├── server.py              HTTP server + handler + scheduler boot
├── cloud_scheduler.py     Background scheduler (3800 LOC, 58 fns)
├── auth.py                SQLite auth + token bucket + sessions
├── observability.py       Sentry wrapper + critical_alert()
├── logging_setup.py       JSON log formatter + print shim
├── trade_journal.py       Journal trim/archive + flock
├── update_dashboard.py    Screener (runs as subprocess every 30min)
├── update_scorecard.py    Scorecard rebuild (daily close)
├── smart_orders.py        Limit-at-mid with market fallback
├── wheel_strategy.py      Options wheel state machine
├── tax_lots.py            FIFO tax-lot reconciler
├── portfolio_risk.py      Beta-exposure + drawdown sizing
├── risk_sizing.py         ATR stops + vol-parity sizing
├── handlers/
│   ├── auth_mixin.py      Login, signup, settings, reset
│   ├── admin_mixin.py     /api/admin/* (user management)
│   ├── strategy_mixin.py  Deploy / pause / stop / preset
│   └── actions_mixin.py   Refresh, kill-switch, close, cancel
├── templates/             Jinja-free HTML (loaded at boot)
├── static/                PWA manifest + service-worker + SVG icon
├── tests/                 pytest suite
├── docs/                  DECIMAL_MIGRATION_PLAN, MONITORING_SETUP
├── scripts/
│   └── compute_sri.sh     Generate SRI hashes from a dev machine
├── pyproject.toml         ruff + pytest + coverage config
└── .github/workflows/ci.yml  lint + pytest + coverage ratchet
```

---

## The round-13 cleanup (follow-on to round-12, 2026-04-19)

Follow-up session that landed the test-coverage gaps flagged at the
end of round-12 plus a focused production-readiness pass.

### Shipped this round

| PR | Commit | Subject |
|---|---|---|
| #17 | `4e06420` | Handler-mixin unit tests (21 cases on csrf / session cookie / input validation) |
| #18 | `7853b2a` | **Latent cb-reset bug** + 23 scheduler helper tests. `_cb_blocked()` was popping initial `{fails:N, open_until:0}` state on every non-open check, silently resetting the counter. Circuit breaker never tripped. |
| #19 | `db66769` | `smart_orders.place_smart_buy/sell` full-flow tests (14 cases: cancel-race, partial fill, coid format) |
| #20 | `7e5b490` | Wheel stock-split auto-resolve (`_detect_split_since` + `yf_splits` helper). Anomaly guard now normalises share counts by split ratio instead of always freezing. |
| #21 | `8fa2706` | Exception-handling hardening: `yfinance_budget._call_with_retry` fails fast on permanent errors + routes failures to Sentry; `wheel_strategy._detect_split_since` wraps full computation |
| #22 | `b72f9f9` | Frontend/security bundle (8 fixes): README XSS scrub (DOMParser allowlist), API-key `type=password`, regime WCAG AA contrast, iOS zoom prevention, SW offline toast, HSTS header, Sentry `before_send` PII scrub, `auth_mixin` verify-keys generic error |
| #23 | _pending_ | Math + peripheral bundle (7 fixes): `iv_rank` rate-limit flag + telemetry, `news_scanner` ±15 cap, `social_sentiment` 30-min recency filter, `llm_sentiment` `malformed` flag, `economic_calendar` FOMC 2027, `capitol_trades` hard-fail when disabled, `notify` DLQ overflow kept in main queue on write-fail |
| #24 | _this PR_ | Docs refresh + go-live checklist |

### Key behaviour changes round-13

1. **yfinance retry loop** is now two-tier: ValueError / TypeError /
   AttributeError / KeyError bypass the retry budget entirely — they
   indicate shape drift, not a transient hiccup. Network/HTTPError
   still get the 4-attempt exponential backoff. Final failure pings
   `observability.capture_exception`, not just stdout.
2. **Sentry PII scrub** wired via `before_send=_scrub_pii` in
   `observability.init_sentry`. Strips PK/AK keys, emails, base64
   tokens, and auth headers (`APCA-API-KEY-ID`, `Authorization`,
   `Cookie`, `X-CSRF-Token`) from every event. Drops the event on
   scrub-error rather than sending unscrubbed.
3. **Circuit breaker finally works**. Before PR #18, every non-open
   check was popping the initial state entry and silently resetting
   the fail counter → the breaker never tripped in production.
   Added `tests/test_cloud_scheduler_helpers.py` to pin the
   fails-accumulate-until-threshold contract.
4. **Wheel stock splits** auto-resolve via `yfinance_budget.yf_splits`
   when the anomaly guard sees `share_delta >= 2 * expected_delta`.
   Baseline + expected_delta normalise by the cumulative split ratio
   and the assignment branch fires. Falls back to the freeze path if
   yfinance returns empty / malformed data.
5. **README sanitizer** in the dashboard: `marked.parse()` output
   passes through a DOMParser allowlist (`_README_ALLOWED_TAGS` +
   `_README_ALLOWED_ATTRS`) that strips `<script>`, on*-handlers,
   and `javascript:`/`data:`/`vbscript:` URIs. Fallback path uses
   textContent instead of raw-markdown innerHTML.
6. **Input UX**: all auth templates (login/signup/forgot/reset) use
   `font-size:16px` so iOS Safari doesn't auto-zoom. API-key fields
   are `type=password` + `spellcheck=false`.

---

## Rounds 14-17 (2026-04-19, second-half session)

Three additional audit/cleanup rounds + an architectural refactor
landed after round-13. This is what each round delivered:

### Round-14 audit (PR #28)

Eight parallel Explore agents covering security, trading logic,
concurrency, UI/UX, ops, integrations, tests, and a fresh-eyes
architectural review. 7 fixes shipped, 9 false positives documented:

* `observability.critical_alert` email path **completely broken since
  round-11** — wrong import + wrong signature. Every kill-switch trip
  silently failed to email the operator. Fixed.
* `handle_logout` cleared session cookie but left CSRF cookie alive
  for 30 days. Fixed.
* Sentry `ignore_errors` only listed BrokenPipe variants → URLError /
  TimeoutError noise burning the 5K/month free quota.
* `notify` queue had no hard cap when DLQ persistently fails (memory).
* `notify.log_notification` did read-modify-write without flock (race).
* `llm_sentiment` daily counter used naive UTC not ET.
* `track_record` HTML didn't escape strategy names (defensive).

### Round-15 closeout (PR #29)

11 deferred items from round-14 + new findings during verification:

* **`smart_orders` partial-fill blended cost basis** — `filled_avg_price`
  recorded only the market leg, ignoring the (often better) limit
  partial fill. Drifted PnL ~0.8% over wheel cycles. Fixed with
  `(limit_qty*limit_px + market_qty*market_px) / total` blend.
* **Alpaca 401/403 auto-detect** — fires `critical_alert` once per
  user per ET-day so credential rot doesn't silently fail orders.
* **Daily -3% loss alert now notifies** — was a print + dashboard
  flag with NO notification path. Now wired through `critical_alert`.
* **Per-user isolation** — extracted `_load_with_shared_fallback` to
  `per_user_isolation.py` with pinning tests so the user_id==1-only
  invariant can't silently regress.
* aria-sort on sortable headers; retry button on network errors;
  screener progress banner.
* notify + observability error logs scrub NTFY topic.
* Subprocess SIGKILL fallback for screener timeouts + scheduler
  shutdown.
* `capital_check._compute_reserved_by_orders` extracted + tested
  (prevents silent over-leverage on live-quote fetch failure).
* Stock Watcher dead-code removed from `capitol_trades`.

### Round-16 (PR #30)

State-recovery validator + strategy module test coverage:

* **`state_recovery.py`** — boot-time consistency check that compares
  wheel state files + trade journal vs Alpaca-reported positions.
  Doesn't auto-fix; logs drift via `observability.capture_message`
  so the operator notices before drift becomes real-money damage.
  Wired into `start_scheduler()` as a daemon thread.
* **18 strategy tests** for previously-uncovered modules:
  `pead_strategy`, `short_strategy`, `earnings_play`, `insider_signals`.
  18 state_recovery tests too.

### Round-17 (PR #31)

`cloud_scheduler.py` 3800-LOC monolith split:

* Extracted Alpaca API plumbing (`user_api_*`, `_cb_*`, `_rl_*`,
  `_alert_alpaca_auth_failure`) into **`scheduler_api.py`** (~330 LOC).
* `cloud_scheduler` re-exports every symbol via `from scheduler_api
  import ...` — same objects shared (not copies), so `_cb_state` /
  `_rl_state` / dedup dicts stay coherent across import sites.
* Lazy import of `notify_user` inside `_cb_record_failure` to avoid
  the `cloud_scheduler ↔ scheduler_api` import cycle.
* 7 contract tests in `test_scheduler_api_extraction.py` pin the
  re-export surface (callable presence + same-object identity).

### Round-19 (PR #33) — final self-audit polish

After round-17 shipped, I did a fresh audit on the code I wrote this
session since nobody else had reviewed it. Found two real bugs:

* **`scheduler_api.user_api_delete` + `user_api_patch` skipped the
  rate-limit gate.** Inherited from the pre-extract cloud_scheduler
  code. A kill-switch cancel storm or trailing-stop raise pass could
  exceed Alpaca's 200/min budget and get 429-throttled. Both now
  acquire from the token bucket with 2s wait_max.
* **`options_analysis.analyze_wheel_candidates` crashed on empty-
  string `strike_price`.** Alpaca's contracts endpoint occasionally
  returns this on newly-listed or halt-pending contracts. `float("")`
  raised ValueError and killed the loop. Now defensive parse → skip
  row via the existing `if not strike` guard.

Also polished:
* `user_api_delete` + `user_api_patch` now fire `_alert_alpaca_auth_
  failure` on 401/403 — previously only POST did.
* `user_api_get` dead `last_err` local removed (F841).
* 13 new options_flow + options_analysis tests.
* CI coverage floor bumped 15% → 20% (measured 25.4%).

---

## Last session state (2026-04-19 night — END OF SESSION)

**33 PRs total merged across rounds 11-19.** Paper-trading 30-day
validation window ongoing — started 2026-04-15, ends ~2026-05-15.

**All code-side + operational-prereq work is complete.** User has:
  * Rotated Sentry DSN and set `SENTRY_DSN` on Railway
  * Set `MASTER_ENCRYPTION_KEY` on Railway (locked, do not rotate)
  * Wired notifications (ntfy.sh + Gmail + Sentry alerts)
  * PWA PNG icons live (PR #25)

Only remaining pre-live items are timeboxed user actions:
  1. Finish the 30-day paper validation window
  2. Generate dedicated live Alpaca keys + flip via Settings

Tests: **431 passing** locally (two sandbox-only failures in
`test_auth::test_password_strength_rejects_weak` and
`test_dashboard_data::...trading_session...` as documented).
Ruff clean. **Coverage floor 20% (measured 25.4%)** — bumped from 15%
in round-19 once tests crossed the threshold.

**Current `main` HEAD:** `763a029` (round-20 + Moderate preset detect fix).

### Round-20 trade-quality layer (2026-04-19 night → Monday open prep)

Triggered by analysing a real `/api/data` snapshot — every top-scored
Breakout pick had `backtest.stopped_out: true` with negative return.
Bot was chasing breakout-day peaks (stocks already +8-12% intraday)
and getting whipsawed by normal pullbacks into tight 5% stops.

Shipped in PRs #35-#38:

| PR | What |
|---|---|
| #35 | Dashboard filters past economic-calendar events + recomputes `days_away` from event.date client-side (was showing "0d away" for Friday's opex on Sunday) |
| #36 | `run_auto_deployer` don't-chase gate (`daily_change > 8%` skip on Breakout/PEAD); volatility cap (`volatility > 20%` skip); `max_position_pct` 0.10 → 0.07 in preset + config; `breakout_stop_loss_pct` 0.05 → 0.12 (old value was TIGHTER than default, backwards) |
| #37 | `migrations.py` + boot-time `run_all_migrations` hooked into state-recovery thread. Auto-migrates every user's `guardrails.json` from `max_position_pct: 0.10` → `0.07` idempotently (stamped with `_migrations_applied`). User no longer needs to click Apply Moderate to get the new cap. |
| #38 | Dashboard "Currently running" preset detector was reading CUSTOM because `detectActivePreset` still matched Moderate only on 0.10. Now accepts either 0.07 OR 0.10 during the rollout window. Moderate preset card display: `10%` → `7%`; description expanded to surface round-20 trade-quality gates. |

Net: Monday's deploy pipeline will skip INFQ-class picks (vol 33.9%,
already +12.5% today — blocked by BOTH new gates), prefer lower-vol
breakouts like ALM / JHX, size at 7% not 10%, stop at 12% not 5%.
Dashboard correctly detects Moderate as active (no more "CUSTOM").

---

## Rounds 21-22 (2026-04-20, day-after-round-20 session)

Ran intensively during Monday's paper-trading hours — started with a
live `/api/data` snapshot, surfaced 12 real bugs over the course of
the session, shipped fixes in 11 PRs.

### Shipped this round

| PR | Subject |
|---|---|
| #40 | `getCSRFToken is not defined` — blocked every Settings save |
| #41 | Activity log: 200-entry buffer + taller scroll box |
| #42 | Activity ring buffer persisted to JSON (survives Railway redeploy) |
| #43 | Exception-handling hardening (round 2) — 6 sites + 15 tests |
| #44 | `.score-label` width so MOMENTUM fits on pick cards |
| #45 | Round-21 trio: Gemini 2.0, auto_deployer_config migration, news_scanner error surfacing |
| #46 | 🤖 AI / 📰 News / 🔵 Insider sentiment lines on pick cards |
| #47 | Gemini `gemini-2.0-flash` → `gemini-2.5-flash` + Alpaca news RFC-3339 `Z` suffix |
| #48 | **Two high-impact fixes**: Gemini `maxOutputTokens 100→256` + `thinkingBudget:0` + `responseMimeType:json` (fixed `AI: unparseable: ```` ``` ```` ` display); AND `fetch_bars_for_picks(days=20→60)` so MACD-26 EMA has enough history (RSI/MACD/BIAS were all `50 / 0 / neutral` defaults on every pick) |
| #49 | `llm_sentiment` cache self-heals on `malformed:true` entries |
| #50 | Sell 25% button on Open Positions (generalised Sell Half → Sell Fraction modal) |
| #51 | **Round-22 audit sweep** — 6 fixes from 5 parallel Explore-agent audits |

### Key behaviour changes 21-22

1. **Gemini 2.5-flash** is the default LLM, configurable via `GEMINI_MODEL` env var. `thinkingBudget:0` disables chain-of-thought for this binary classifier (saves tokens). `responseMimeType:"application/json"` bypasses markdown-fence wrapping. `maxOutputTokens:256` gives headroom. Cache auto-invalidates malformed entries on next read (self-heal on deploy).
2. **Alpaca news API** — `start` parameter now uses UTC with `Z` suffix. Previously `-0400` (no colon) was RFC-3339 invalid and returned HTTP 400 silently.
3. **Pick-card technical indicators** now show real RSI/MACD/BIAS (were hardcoded defaults due to bar-fetch window being shorter than MACD's 26-EMA requirement).
4. **Pick cards** gained three new sentiment lines: 🤖 AI (Gemini reasoning), 📰 News (Alpaca news sentiment + first bullish keyword), 🔵 Insider (SEC Form 4 cluster buys). All three are render-if-data-exists and use `esc()` on free-form strings.
5. **Positions table** gained a Sell 25% button alongside existing Close / Sell 50%. Backed by existing `/api/sell` endpoint (no backend change).
6. **`/api/force-auto-deploy` 30s cooldown** per user — authenticated DoS guard (round-22 audit finding).
7. **`_counter.json` race fixed** with `fcntl.flock` — LLM cost counter was losing increments under parallel screener threads.

### Round-22 audit findings (CLAUDE.md playbook executed)

5 parallel Explore agents covered security, DB/concurrency, trading
logic, UI/UX/mobile, production-readiness. 25+ findings triaged into:

**Fixed in PR #51** (6 items):
  * `update_scorecard.safe_save_json` bare-except narrowing (last site)
  * `llm_sentiment._bump_call_counter` flock race
  * `estimate_daily_cost` ET vs UTC date mismatch
  * Positions-table `.btn-sm` 32px → 40px on mobile
  * Sentiment-line contrast (WCAG AA)
  * `/api/force-auto-deploy` per-user 30s cooldown

**Need user decision** (flagged in PR #51 body):
  * **Session idle timeout** — sessions persist 30 days with no
    activity-based expiry. Should we add a 12-hr idle logout?
    Convenience vs. lost-laptop risk.
  * **Boot-time config WARNs** — GEMINI_API_KEY / SENTRY_DSN /
    NTFY_TOPIC are silently optional. Add boot-time warnings when
    any are unset? (Noise vs. visibility.)
  * **`news_websocket.py` module** — code exists but never wired
    into `cloud_scheduler`. Wire / feature-flag / delete?

**Deferred with known gaps** (bigger items needing their own PR):
  * Scheduler thread-death monitor (HIGH pre-live) — needs a
    separate monitor thread in `start_scheduler` that fires
    `critical_alert` if `_scheduler_thread.is_alive()` goes False.
  * Alpaca news WebSocket `on_error` handler (MEDIUM, feature dormant).
  * Subprocess zombie tracking (MEDIUM).
  * HTTP timeout centralisation in `constants.py` (MEDIUM).
  * Auto-refresh countdown retry-on-stall (MEDIUM) — if fetch hangs
    past 30s, show retry UI instead of an infinite `0s`.
  * Positions-table 375px overflow on iPhone SE (LOW).
  * Positions-fetch loading state (LOW).

**False alarms from trading-logic agent** (verified in PR #51 body):
Agent claimed 7 "critical" trading bugs — all turned out to be
misreadings of the code. `partial-fill stop orphan`, `trailing-stop
gap-down`, `double-stop stacking`, `option multiplier missing`, and
`cost-basis quantization drift` were all spot-checked against the
actual code and the handling is already correct. Notes in PR body
for future reference.

---

---

## Rounds 23-25 (2026-04-20 late, same day as 21-22)

After the round-22 audit landed (PR #51), user asked "anything else
needed to fix?" — which triggered rounds 23, 24, and 25. 5 more PRs.

### Shipped this round

| PR | Subject |
|---|---|
| #52 | Round-23: session 12-hr idle timeout + boot config WARNs + wire news_websocket into scheduler |
| #53 | Round-24: scheduler death watchdog + subprocess zombie tracking + HTTP timeout constants + fetch-stall retry + 375px positions overflow fix + Breaking News banner on pick cards |
| #XX | Round-25: zombie rate-limit bug fix + Breaking News badge on position rows + 6 new tests + docs refresh |

### Key behaviour changes 23-25

1. **Session 12-hour idle timeout** (round-23). `sessions.last_activity_at`
   column added; `validate_session` rejects any session idle > 12 hr
   (configurable via `SESSION_IDLE_HOURS` env var) and slides the
   window forward on every valid request. 30-day absolute ceiling
   still applies on top.

2. **Boot-time config WARN** (round-23). `server.main()` logs a
   helpful WARN for each of `GEMINI_API_KEY`, `SENTRY_DSN`,
   `NTFY_TOPIC` that's unset — names the consequence and the exact
   Railway env-var fix.

3. **news_websocket wired** (round-23). Alpaca real-time news stream
   runs for user_id=1. `|score| >= 6` alerts go to
   `users/1/news_alerts.json` and ntfy. Gated by
   `ENABLE_NEWS_WEBSOCKET` env var (default "true" —
   websocket-client is in requirements.txt). Single-stream design
   limit documented; multi-user would need news_websocket module
   refactor.

4. **Scheduler death watchdog** (round-24). Daemon thread polls
   `_scheduler_thread.is_alive()` every 60s and fires `critical_alert`
   (ntfy + Sentry + email) once per process if the thread dies.
   Previously a silent scheduler death left the HTTP server up but
   the bot had stopped trading — operator wouldn't know.

5. **Subprocess zombie tracking** (round-24, bug-fixed in round-25).
   Piggy-backs on the watchdog tick. Reaps via `waitpid(-1, WNOHANG)`,
   counts Z-state children via `/proc` (Linux-only; silent skip
   elsewhere), alerts hourly when count > 5. Round-24 first cut had
   a rate-limit bug — passed `_last_zombie_alert_ts` by value so the
   1-hour limit never engaged. Round-25 refactored to return the
   updated timestamp so the watchdog's local advances correctly.

6. **Dashboard fetch 30s timeout** (round-24). `refreshData` wraps
   the `/api/data` fetch in an `AbortController`; on stall the toast
   says "Dashboard fetch stalled (>30s). Network issue?" with a
   Retry action, and `_refreshInFlight` is cleared so the next tick
   fires cleanly. No more infinite "Next refresh: 0s" hang.

7. **iPhone SE positions table fix** (round-24). `.table-card` at
   ≤380px viewports gets `overflow-x: auto` + `min-width: 520px`,
   and `.btn-sm` shrinks to 36px min-height with tighter padding.
   Close / Sell 50% / Sell 25% all fit on one row + horizontal
   scroll handles the remaining overflow.

8. **🚨 Breaking News on pick cards AND position rows** (round-24 + 25).
   - New `GET /api/news-alerts?minutes=60` returns the user's recent
     websocket-scored alerts.
   - `refreshData` fetches in parallel, populates
     `window._newsAlertsBySymbol`.
   - `buildBreakingNews(p)` adds a 🚨 BREAKING BULLISH/BEARISH banner
     to any TOP pick whose symbol has a `|score| >= 6` alert in the
     last 60 min.
   - Round-25: same lookup is also used in the positions-row renderer
     — a held position like SOXL / INTC now shows a 🚨 BULL / 🚨 BEAR
     badge next to the symbol when fresh news arrives. For option
     positions the badge is keyed off the underlying (HIMS put shows
     HIMS news).

### Round-25 test coverage additions

New file `tests/test_round25_followups.py` pins:
  * Session idle timeout rejects stale sessions (last_activity_at >
    SESSION_IDLE_HOURS ago) even if expires_at is still in the future.
  * Session validation SLIDES the window forward (timestamp advances
    on each successful check).
  * `news_websocket.get_recent_alerts` returns [] on empty dir, filters
    by max_age_minutes, tolerates malformed `received_at` entries.
  * `_check_subprocess_zombies` returns a float timestamp in all paths
    — prevents the round-24 rate-limit regression from recurring.

461 passing locally (was 455 before round-25).

### Round-25 follow-on fixes (same day, after PR #54 merged)

| PR | Subject |
|---|---|
| #55 | `error_recovery.py` OCC option false-positive orphan alerts |
| #56 | Heatmap legend row renders blank (color-class scoping bug) |

PR #55: The orphan-position check in `error_recovery.py` compared
positions' raw Alpaca symbols against strategy-file underlying
symbols. For options, Alpaca returns OCC-format (`CHWY260515P00025000`)
while wheel files key off the underlying (`CHWY`). Every active wheel
put/call was flagged and emailed as an orphan every run. Added
`_is_occ_option_symbol` + `_occ_underlying` helpers and 4 tests.

PR #56: Heatmap Loss/Win legend rendered as blank space because the
color classes (`.loss-big`, `.win-big` etc.) were scoped only to
`.heatmap-cell` — the legend uses `.heatmap-legend-box`. Double-
selected both. Cosmetic only; the data was always correct.

465 passing locally (461 + 4 OCC option tests).

---

## Last session state (2026-04-20 — END OF SESSION, after round-25 followups)

**58 PRs total merged** across rounds 11-25 (including rounds 23-24 +
the round-25 sweep + PRs #55 / #56 follow-up fixes). Paper-trading
validation window still active (started 2026-04-15, ends ~2026-05-15).

**Current `main` HEAD:** `c00442d` (PR #56 heatmap legend fix).

**All code-side pre-live items are shipped.** Only remaining items
are operational:
  1. Finish 30-day paper validation window (~2026-05-15).
  2. Generate dedicated live Alpaca keys.
  3. Flip Settings → 🔴 Live Trading.

**Test suite:** 465 passing locally (455 baseline + 6 round-25
session-idle/news-alerts/watchdog tests + 4 OCC-option tests). Ruff
clean on all Python files touched this session.

**User's open positions as of end of session:**
  * SOXL 117 shares @ $85.11 entry. Stop trailing at $90.76, locking in
    ~$660 minimum profit. Trailing strategy working as designed.
  * INTC 63 shares @ $66.66. Stop at $61.34.
  * HIMS 260508P00027000 (-1 short put).
  * CHWY 260515P00025000 (-1 short put).
  * Portfolio: ~$101k on $100k seed, +1.1% since 2026-04-15 start.
  * Trade Heatmap: 3 trading days, 3 wins, $1,226.65 total P&L.

### Picking this up from a new session

1. `git pull --ff-only` on `main`. HEAD should be at `c00442d` or later.
2. `MASTER_ENCRYPTION_KEY=<64hex> python3 -m pytest tests/ --ignore=tests/test_auth.py --ignore=tests/test_dashboard_data.py` — expect **465 passing**.
3. `ruff check .` — expect ~109 known findings (mostly F401 unused
   imports, low priority). None in critical modules.
4. Read this file + `README.md` + `GO_LIVE_CHECKLIST.md`.
6. If the user says "audit again", follow the playbook (5-8 parallel
   Explore agents, triage into fix/deferred/false-positive, verify
   trading-logic claims against actual code — that agent has a
   history of false-positives, see PR #51 body for examples).

See `GO_LIVE_CHECKLIST.md` for the pre-flip-to-live gating list.
