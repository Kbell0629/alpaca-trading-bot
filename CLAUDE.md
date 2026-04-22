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

## Rounds 26-28 (2026-04-20 night → 2026-04-21)

Three more rounds landed after round-25 followups:

### Round-26 (PR #58) — single-use signup invites

Admin-generated, DB-backed. Plaintext token shown once; only SHA-256
hash stored. `auth.create_invite` / `check_invite` / `consume_invite`
/ `list_invites` helpers; atomic `UPDATE ... WHERE used_at IS NULL`
prevents double-use under races. Signup flow accepts either the
legacy env-var `SIGNUP_CODE` OR a DB invite token. 8 tests.

### Round-27 (PRs #59, #60, #61) — mobile + UX polish

* **PR #59**: mobile horizontal-scroll — `html { overflow-x: hidden }`
  + `body { overflow-x: clip }` clamps overflow at viewport level.
* **PR #60**: per-section ⓘ help buttons + wheel-aware Close modal
  (show realized put premium / option P&L breakdown before confirming)
  + README sync.
* **PR #61**: JS SyntaxError hotfix — `const`/`var` identifier
  collisions in the close-modal option branch broke the dashboard's
  initial render with "Loading...". Renamed collisions to
  `optPnlColor` / `optPnlWord` / `optBtn`. Lesson learned: run
  `node --check` on extracted dashboard JS before merging.

### Round-30 (in flight on `claude/round29-earnings-exit-all-strategies`, appended after merge)

UX polish + correlation-warning accuracy fix. Triggered by a mobile
screenshot showing the FULL STOCK SCREENER section had an ⓘ info
button but the Position Correlation warning above it did not —
inconsistent ⓘ coverage across sections.

**What shipped:**

* **ⓘ on every dashboard section.** Previously only Top Picks /
  Active Strategies / Positions / Screener / Heatmap had the guide
  button. Now added: Position Correlation, Paper Trading Progress,
  Tax-Loss Harvesting, Short Candidates, Visual Backtest, Cloud
  Scheduler, Performance Attribution, Tax Report, Factor Health,
  Paper vs Live Comparison, Activity Log. 11 new SECTION_GUIDES
  entries.
* **SOXL / SOXS / SOXX sector map fix** — these were bucketed as
  "Other" in correlation warnings instead of "Tech". 80+ ticker
  entries added to `constants.SECTOR_MAP` covering the full
  screener output: leveraged semi ETFs, crypto miners (bucketed
  under Finance), quantum / nuclear / satellite plays, and 2026
  IPO names. Eliminates the false "3+ positions in same sector"
  warning where the sector was actually just "Other → unknown".

Dashboard JS validated via `node --check` after extraction
(round-27 lesson from PR #61: always check extracted JS when
editing the inline script blocks).

### Round 36 (2026-04-21 evening, in flight on `claude/admin-panel-improvements`)

User asked three things after round-35 shipped:
1. Verify friends who signup via invite get regular-user (not admin)
2. Audit the "Manage Users" admin panel for missing functions
3. Fix the Audit Log tab which renders past the viewport bottom and
   hides the Close button, forcing a page refresh to dismiss
Plus: "check if the weekly learning is actually happening."

**What shipped:**

* **Signup creates regular user — verified.** `handle_signup` in
  `handlers/auth_mixin.py` calls `auth.create_user()` WITHOUT an
  `is_admin` arg. auth.py defaults `is_admin=False`. Only the FIRST
  user ever created auto-promotes (line 511-512 of auth.py). Friends
  who sign up via invite always get `is_admin=0`.

* **`auth.revoke_invite(token_hash)`** — atomic UPDATE that sets
  `expires_at` to one second in the past on any UNUSED invite.
  `check_invite` then returns "expired" on any subsequent attempt.
  Used / missing hashes are left alone (returns False).

* **`auth.set_user_admin(user_id, is_admin)`** — promote or demote.
  DB-layer guard rail: counts other active admins; if zero, refuses
  to demote. Inactive admins don't count (so a paused admin can't
  "save" you from locking yourself out).

* **`handle_admin_revoke_invite` + `handle_admin_set_admin`** in
  `handlers/admin_mixin.py`. Both gated on `is_admin` check, both
  emit admin-audit-log entries with actor + target + detail. Routed
  as `/api/admin/revoke-invite` and `/api/admin/set-admin`.

* **Dashboard Invites tab**: added Actions column with a "Revoke"
  button shown only for `status === 'active'` invites. `token_hash`
  now included in the list response (admin has rights anyway; hash
  alone can't redeem, only the plaintext can).

* **Dashboard Users tab**: added "Make Admin" / "Revoke Admin"
  buttons alongside Deactivate + Reset Password. Server-side guard
  rail prevents the "demote the last admin" footgun; UI confirms
  before sending.

* **Admin modal sizing fix**: wrapped `#adminPanelModal .modal` in a
  flex column (header / scrollable content / footer) with
  `max-height:88vh`. Audit log content now scrolls INSIDE the
  modal instead of pushing the Close button past viewport bottom.

* **Weekly learning path bug**: `update_dashboard.py:1649` read from
  `DATA_DIR/learned_weights.json` (shared path), but
  `cloud_scheduler.run_weekly_learning` passed per-user
  `LEARNED_WEIGHTS_PATH` to `learn.py`. So learn.py wrote weights
  per-user that the screener never read back. Fixed:
  - `update_dashboard.py` now honors `LEARNED_WEIGHTS_PATH` env var
  - `cloud_scheduler.run_screener_for_user` now sets the env var
  Tests added (grep-level + functional).

12 new tests across `test_admin_invite_revoke.py` (10) and
`test_learned_weights_path_wiring.py` (2). 532 passing locally.
Ruff clean on all touched files.

### Rounds 31-35 (2026-04-21 afternoon, all merged or in flight)

After round-30 shipped, the paper-trading window surfaced a stream
of small-but-real UX + data-integrity bugs during Tuesday's
live-session. Five more rounds fixed them:

**Round-31 (PR #66) — sticky nav-tabs overlapped sticky header +
broke on mobile.** `.nav-tabs { top: 0 }` collided with `.header-v2
{ top: 0 }`, so the tabs hid behind the header. Mobile override
forced `position: relative` which undid the sticky entirely. Fixed:
tabs now stick at `top: var(--sticky-header-height)` which a runtime
ResizeObserver keeps updated as the header wraps / banners appear.

**Round-32 (PRs #67-68) — nav chevron drift + readiness label
mismatch.** The `›` scroll hint was an `::after` on the scroller
with `right: 0` — so it drifted into the middle of the tab list on
swipe. Removed, then re-added properly in round-33 (below) wrapped
in a sticky non-scrolling container. Also: the "Total Trades /
Target: 20" label didn't match the backend scoring (backend checks
5 criteria not including trade count). Changed to "Informational"
with a tooltip. Fixed the `paper-progress` and `comparison` section
guides to list the 5 real criteria with correct thresholds.

**Round-33 (same PR as round-32 continuations) — journal undercount
+ scroll-hint wrapper.** Only `run_auto_deployer`'s main equity path
wrote `trade_journal.json` open-entries. Wheel `open_put` and
dashboard manual deploys never journaled, so closes had nothing to
match and the scorecard undercount. New `record_trade_open()`
helper in `cloud_scheduler.py`; wired into `wheel_strategy.open_put`
and a `_record_manual_deploy()` method on `StrategyHandlerMixin`
that each of the 4 real-position deploy paths calls. Plus a new
`.nav-tabs-wrap` sticky parent with a proper right-edge gradient +
animated chevron that fades out when the user scrolls to end.

**Round-34 (PRs #69, #70/#71) — positions-table overscroll + Today's
Closes panel + orphan close.** Three fixes landed across two PRs:

* PR #69: `.table-card { overscroll-behavior-x: contain }` so a
  horizontal swipe on Positions/Orders tables doesn't drag the
  whole viewport sideways on iOS/Android.
* PR #70/71: New "Today's Closes" panel in Overview, fed by a new
  `todays_closes.py` scanner and an `_scan_todays_closes` helper in
  `server.py`. Shows every close that happened today with time /
  symbol / strategy / reason / exit / P&L + net P&L summary. Panel
  auto-hides when zero closes today.
* Same PR: `record_trade_close` hardened to append a synthetic
  orphan entry (flagged `orphan_close: true`) when no matching open
  exists, instead of silently returning False. Orange `[orphan]`
  tag in the dashboard warns that the entry price is missing. This
  is the exact gap that was hiding the 10:59 AM 2026-04-21 close
  from the scorecard.

**Round-35 (PR in flight on `claude/position-correlation-redesign`)
— real Position Correlation + action-button alignment.** Two user-
flagged UX issues:

* Correlation section was printing "Sectors: <list of position
  SYMBOLS>" — not sectors at all, just symbols. New panel groups by
  real sector with bars, $ allocation, and % per sector. Fires a
  warning only when top sector ≥ 40% (orange) or ≥ 60% (red).
  Options route via underlying for sector lookup (HIMS put →
  Healthcare, CHWY put → Consumer). Backed by a new
  `position_sector.annotate_sector` helper that reuses
  `constants.SECTOR_MAP` (which round-30 populated).
* Positions-table action buttons (Close / Sell 50% / Sell 25%)
  were stacking vertically on narrow screens, which made the
  Actions column grow tall and misalign the header. Wrapped in a
  `.pos-action-btns` flex-nowrap container so they stay on one row.

11 new tests in `test_position_sector_annotation.py` pin the sector
lookup + option underlying resolution + "Other" fallback. 520
passing locally (was 509 after round-34). Ruff clean.

### Round-29 (merged as PR after round-28)

Universal pre-earnings exit for non-PEAD equity strategies. Triggered
by spotting INTC (earnings April 23, user held 63 shares from the
breakout deploy). Before round-29 only the PEAD strategy exited
positions before earnings; breakout / trailing-stop / mean-reversion /
copy-trading positions sat through earnings and regularly got
whipsawed by surprise moves.

**What shipped:**

* **New module `earnings_exit.py`** — yfinance-backed next-earnings
  lookup (cached 4 hours — the monitor loop runs every 30 min per
  symbol), `should_exit_for_earnings()` decision helper, strategy
  allow-list.
* **`process_strategy_file()` hook in `cloud_scheduler.py`** — after
  the PEAD block and before profit-ladder. Fires a market sell + stop
  cancel + rich notification.
* **Guardrails key `earnings_exit_days_before`** (default 1). Operator
  can widen the buffer (3-5 days), or disable entirely via
  `earnings_exit_disabled: true`.
* **Boot-time migration** (`migrations.migrate_guardrails_round29`)
  stamps the default onto every existing user's guardrails.json
  without overwriting custom values.

**Scope choices (best-for-profits):**

* **1 day before** earnings (not 3-5). Keeps more of the pre-earnings
  momentum we deployed for; gives slippage buffer for after-hours
  surprise.
* **Full close** (not partial). Earnings is binary — partial exposure
  is the worst of both worlds.
* **Applies to:** `trailing_stop`, `breakout`, `mean_reversion`,
  `copy_trading`.
* **Does NOT apply to:** `wheel` (short puts over earnings capture IV
  crush — the wheel's profit engine), `pead` (has its own rule via
  `exit_before_next_earnings_days`).

**INTC handling:** bot will close INTC automatically on April 22 (1
day before April 23 earnings) once the PR merges + Railway deploys.

16 new tests in `tests/test_earnings_exit.py`. All pass. 496 passing
locally (was 480 after round-28). Ruff clean.

### Round-28 (PR #63, merged)

Follow-on to PR #43's exception-handling hardening. Audit surfaced
sites the prior sweep missed:

* **safe_save_json narrowings**: three sibling copies of the same
  atomic-write helper (`error_recovery.py`, `learn.py`,
  `update_dashboard.py`) still had bare `except:` on the outer
  clause. Signals (KeyboardInterrupt / SystemExit) used to get
  caught, passed through the tmp-unlink cleanup, then re-raised —
  blocking graceful shutdown for a few ms. Narrowed to
  `except Exception:` + inner cleanup to `except OSError:`. Also
  `update_dashboard.py:2470` inline HTML-tmp cleanup.
* **auth.py:557 conn.close bare except** narrowed to
  `(sqlite3.Error, OSError)`. DB-close failures still absorbed, but
  KeyboardInterrupt during finally now propagates.
* **handlers/strategy_mixin.py three silent swallows → log.warning**:
  - Line 74 (cooldown timestamp malformed): bypassing the loss-cooldown
    gate now surfaces at WARN so a corrupted `guardrails.json` doesn't
    silently disable the gate.
  - Line 89 (admin audit-log failure): audit trail breakage now
    visible — was silent for any compliance / trace regression.
  - Line 472 (pead_strategy scorer fails): split into `ImportError`
    (stays silent — slim deploys) and `Exception` (WARN). A broken
    scorer was silently stranding every PEAD deploy with no
    earnings-exit signal.

7 new tests in `tests/test_exception_handling_round28.py`. All pass.
Ruff clean on touched files.

---

## Last session state (2026-04-22 late evening — END OF SESSION, after round-44 auto-orphan-fix + scroll jitter)

**81 PRs merged + round-44 in flight** (round-43 superseded by
round-44 — see notes below).

**Current `main` HEAD:** `4a9b2cb` (PR #81 round-42 wheel close
journaling). Round-44 branch
`claude/round44-auto-orphan-fix-scroll-jitter` — awaiting manual
PR merge via web UI.

### Round-44 (this round) — two user-requested UX fixes

**1. Orphan wheel closes now auto-fix in every wheel monitor tick.**
Round-43's first draft shipped a button + endpoint. User pushed back
("I don't want a button just fix it"). Round-44 drops the button +
endpoint and wires `wheel_open_backfill.backfill_wheel_opens(user)`
into the TAIL of `run_wheel_monitor`. Idempotent + cheap (no Alpaca
calls). New orphans get paired with their opens within one monitor
cycle. **Important:** if round-43 is NOT merged, round-44 includes
the `wheel_open_backfill.py` module + tests so it's standalone.
If round-43 IS merged first, round-44 is a clean diff that drops
the button + wires the auto-call. Either merge order is safe.

**2. Dashboard stops "jumping around" during auto-refresh.**
User noticed the 30s auto-refresh caused the viewport to shift.
Root cause: `refreshData` fires, replaces section innerHTMLs, some
grow/shrink in height, and the user's scroll position looks
different. Fixed with:
  * `overflow-anchor: auto` CSS on `<body>` — modern browsers
    auto-compensate for above-viewport DOM height shifts.
  * JS save/restore in `renderDashboard()` — captures
    `window.scrollY` + `document.activeElement.id` + input
    `selectionStart/End` at the top of render; restores all three
    in a `requestAnimationFrame` after paint. Only restores if
    drifted >10px so it doesn't fight scrollToTop / anchor scroll.

Mid-typing cursor position is preserved too — no more losing focus
every 30s while filling in the notification email field.

### Round-43 status

Round-43's PR (button + admin endpoint for manual orphan fix) was
pushed but user wanted the automatic version instead. Round-44
supersedes it. Either:
  * **Close round-43 PR + merge round-44** (clean): recommended.
  * **Merge round-43 then round-44**: button gets added then removed.
    Net effect same; harmless.

### Round-42 recap

Wheel close journaling (5 exit paths + external-close detection for
Alpaca native stop fires). Merged as PR #81, commit `4a9b2cb`.

---

## Last session state (2026-04-22 evening — pre-44, pre-43, post-42)

**79 PRs merged + round-42 in flight.** Paper-trading 30-day
validation window ongoing (started 2026-04-15, ends ~2026-05-15).

**Current `main` HEAD:** `c878929` (PR #79 round-41 full tech-stack
audit — merged during this session). Round-42 branch
`claude/round42-wheel-close-journaling` — awaiting manual PR merge
via web UI (GitHub MCP still disconnected).

### Round-42 (this round) — wheel closes now journal properly

**Motivating bug (user-reported this session):** User noticed CHWY
short put disappeared from Positions + wasn't in closed positions.
Alpaca screenshots showed the `Stop @ $0.35 buy 1.00` order had
filled — the protective stop correctly closed the short put. The
bug was the *journal layer*: `wheel_strategy.py` updated its own
state file on every exit path but **never called
`record_trade_close`**. Asymmetric with round-33's
`record_trade_open` wiring.

**What shipped:**
* `_journal_wheel_close()` helper in `wheel_strategy.py` — keys off
  OCC contract symbol + strategy="wheel" + side="buy" (short cover).
* 5 exit paths now journal: `put_assigned`, `put_expired_worthless`,
  `call_assigned`, `call_expired_worthless`, `{type}_bought_to_close`.
* **NEW** external-close detection. Pre-expiration, if the contract
  is missing from `/positions` but wheel state says active, fetch
  the fill price from `/account/activities/FILL?symbol=<OCC>` and
  journal the close + reset stage. This is what catches the CHWY
  case on the next scheduler tick.
* Gated to pre-expiration only so it doesn't mis-journal an
  assignment (post-expiry, the dedicated assignment branch handles
  cost-basis + stage transition).
* 6 new tests in `tests/test_round42_wheel_close_journaling.py`.
  609 passing total (603 + 6). Ruff clean.

**Operator impact:** Once Railway picks up this deploy, CHWY will
journal on the next scheduler tick — expect to see the close in
Today's Closes + Closed Positions within ~30 minutes of the deploy.

**All code-side pre-live items shipped.** Only remaining:
  1. Finish 30-day paper validation window (~2026-05-15).
  2. Generate dedicated live Alpaca keys.
  3. Flip Settings → 🔴 Live Trading.

**Test suite:** **609 passing** locally (603 post-41 baseline + 6
round-42 wheel close journaling) minus the two documented sandbox-
only failures. Ruff clean. Coverage floor 20% (measured ~29%).

### Round-40 (PR #78, merged) — deferred items + GDPR

Closed out the 5 remaining deferred items + GDPR-style export:

* **`auth.delete_user`** — cascades user row, data dir, invites,
  sessions, audit log. Guard rails: refuses self-delete, refuses
  last-active-admin.
* **`auth.export_user_data`** — ZIP bundle (profile sanitized,
  strategies, trade journal, audit log). GDPR-ready for when the
  app ships as a subscription.
* **`journal_backfill.py`** — one-shot synthesizer for "open"
  entries missing from pre-round-33 deploys. Idempotent; uses
  Alpaca `avg_entry_price` as authoritative entry. Integrated
  with the admin panel and CLI path.
* **Options flow + analysis test coverage** — 13 pure-logic tests
  pinning the C/P ratio thresholds and wheel-candidate scoring
  (previously network-bound ~0% coverage).
* **Scheduler activity admin drill-down** — admin panel now shows
  the full unfiltered activity log (per-user tab continues to
  filter for non-admins — round-39 privacy fix respected).
* **Coverage ratchet** bumped 20% → 25% (measured ~29.55%).

### Round-41 (branch `claude/round41-full-audit`, commit 7f790c4) — full tech-stack audit

5 parallel Explore agents swept security, concurrency, trading
logic, UI/UX, ops. Trading-logic came back **CLEAN** (verified
each claim against actual code — no false positives this time,
unlike round-22). 8 real bugs across 4 other areas:

**Security / concurrency:**
* `auth.py` — 5 sqlite conn leaks (`get_user_by_id`,
  `get_user_by_username`, `get_user_by_email`,
  `list_active_users`, `validate_session`) wrapped in try-finally
  with narrowed `(sqlite3.Error, OSError)` on close.
* `create_user` first-user-admin TOCTOU — two concurrent signups
  on an empty `users` table could both see `count==0` and both
  become admin. Fixed via `cur.execute("BEGIN IMMEDIATE")` before
  the count query. Test with 5 parallel threads confirms exactly 1
  admin.
* `journal_backfill.py` — RMW on `trade_journal.json` was
  unlocked. Concurrent `record_trade_open` from scheduler / manual
  deploy could drop entries. Wrapped in `strategy_file_lock`
  (same flock helper every other journal writer uses). Network
  I/O (Alpaca positions fetch) stays OUTSIDE the lock.

**Ops hardening:**
* `server.main` `PORT` env var guarded — was naked `int()` that
  would crash on `PORT=abc` typo. Now validates range + logs +
  falls back to 8888.
* `track_record.html` `{{USERNAME}}` now `html.escape()`'d.
  Public shareable URL — defense-in-depth XSS hardening.

**UI / UX:**
* Base `.modal` gains `max-height:92vh; overflow-y:auto` — Close
  Position (with P&L detail), Cancel Order (with explanation),
  Settings (with Danger Zone) were pushing buttons past viewport
  bottom on short screens.
* `executeClosePosition`, `executeSellFraction`,
  `executeCancelOrder` gain in-flight sets — fast double-click was
  firing 2 POSTs before modal dismiss animation finished. Same
  pattern as round-11's `_deployInFlight`.
* Notification email `<input>` gets `autocomplete="email"` +
  `inputmode="email"` so mobile keyboards offer saved address.

**Tests:** 9 new cases in `tests/test_round41_audit_fixes.py`
covering every fix (conn-leak regression, TOCTOU race with 5
parallel threads, journal-lock presence assertion, PORT guard,
XSS-escape assertion, backfill functional contract).

### Key behaviour changes this session (round 40-41)

1. **Admin panel** now has Delete User + Export User Data buttons
   (round-40). Deletes cascade through every per-user file; export
   returns a sanitized ZIP bundle.
2. **Concurrent signup now serializes** (round-41). If two people
   hit `/signup` at the same moment on a fresh install, SQLite's
   `BEGIN IMMEDIATE` lock ensures only the first one becomes
   admin. Previously this was a theoretical race; likely never
   triggered but now pinned.
3. **Dashboard modals always fit the viewport** (round-41). No
   more scrolling the whole page to reach a confirm button.
4. **Dashboard action buttons can't double-fire** (round-41). Fast
   double-clicks on Close / Sell / Cancel are idempotent.

### Picking this up from a new session

1. `git pull --ff-only` on `main`. HEAD should be at `3e40c67`
   (PR #78 round-40). Round-41 branch is
   `claude/round41-full-audit` at `7f790c4` — if not yet merged,
   open https://github.com/Kbell0629/alpaca-trading-bot/pull/new/claude/round41-full-audit
2. `MASTER_ENCRYPTION_KEY=<64hex> python3 -m pytest tests/ --ignore=tests/test_dashboard_data.py -q` — expect **609 passing**.
   (If you drop `--ignore=tests/test_auth.py` the sandbox without
   zxcvbn will fail 1 test — expected, see "known test quirks".)
3. `ruff check .` — clean on current main + round-41 branch.
4. Read this file + `README.md` + `CHANGELOG.md`
   + `GO_LIVE_CHECKLIST.md`.
5. If the user says "audit again", follow the playbook (5-8 parallel
   Explore agents, triage into fix/deferred/false-positive, verify
   trading-logic claims against actual code — that agent has a
   history of false-positives, see CHANGELOG round-22 for examples).
6. **GitHub MCP stays disconnected** every session. User opens PRs
   + merges manually via the web UI. Don't try `mcp__github__*` —
   they're not loaded. Document + move on.

### Likely next-session topics

Based on unmerged branches + open threads:
- **Round-41 PR merge verification** (the CI run on `claude/round41-full-audit`).
- **INTC April 22 auto-close verification** — user should see a
  ntfy push + an entry in the Today's Closes panel.
- User is running low on Claude tokens until Thursday 10pm —
  plan work accordingly; a new session may start after refresh.
- User may ask about a **new feature** (chart export? alert
  webhook? Discord integration?) — no blocking work queued.

See `GO_LIVE_CHECKLIST.md` for the pre-flip-to-live gating list.

**User's open positions as of end of session (2026-04-21 late):**
  * SOXL 117 shares @ $85.11 entry. Stop at $94.83 (raised
    intraday from $91.42; ~$1,140 profit locked above cost).
  * INTC 63 shares @ $66.66 — **AUTO-CLOSES 2026-04-22** via
    round-29 earnings-exit (INTC earnings 2026-04-23, 1-day
    buffer). Stop $62.04.
  * HIMS 260508P00027000 (-1 short put). Stop $2.25. Wheel —
    through earnings by design.
  * CHWY 260515P00025000 — **STOPPED OUT 2026-04-22** at $0.35
    (Alpaca native stop fired). Round-42 external-close detection
    will journal it on next scheduler tick.
  * USAR 145 shares — auto-deployed breakout 2026-04-21 10:27 AM
    ET. Stop $22.35.
  * Portfolio: ~$101,242 on $100k seed.

