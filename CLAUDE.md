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

### Known gaps — safe to defer, no active bugs

- **Structured logging phase 2**: `logging_setup.init()` installs a
  `builtins.print` shim so all existing prints get auto-classified
  JSON envelopes. Explicit `log.info()` migration still pending for
  ~400 prints in strategies / one-shots (`learn.py`,
  `update_dashboard.py`, peripheral strategy modules). Not blocking.
- **Test coverage**:
  - `cloud_scheduler.py` is 3800 LOC / 4 tests. Rate limiter, full
    tick, webhook paths all untested.
  - Handler mixins (`auth_mixin`, `strategy_mixin`, `actions_mixin`,
    `admin_mixin` — 2000 LOC) have 0 unit tests; only E2E boot smoke.
  - `smart_orders.place_smart_buy` full flow (timeout → cancel →
    settle → market fallback) untested.
  - Many strategy modules with zero tests: `pead_strategy`,
    `short_strategy`, `earnings_play`, `insider_signals`,
    `options_flow`, `options_analysis`.
- **Coverage ratchet**: CI floor is 15% (measured baseline ~19%).
  Nudge up when you add test suites; pyproject.toml omits one-shot
  CLIs from the measurement.
- **Stock-split auto-detection in wheels**: current behaviour is to
  FREEZE a wheel state when `share_delta >= 2*expected_delta`
  (PR #14). Auto-resolving a split via yfinance splits feed would
  be better but needs wheel-cycle testing.

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

## Last session state (2026-04-19 evening)

26 PRs total merged. Paper-trading 30-day validation window ongoing —
started 2026-04-15, ends ~2026-05-15.

**All code-side + operational-prereq work is complete.** User has:
  * Rotated Sentry DSN and set `SENTRY_DSN` on Railway
  * Set `MASTER_ENCRYPTION_KEY` on Railway (locked, do not rotate)
  * Wired notifications (ntfy.sh + Gmail + Sentry alerts)
  * PWA PNG icons live (#25)

Only remaining pre-live items are timeboxed user actions:
  1. Finish the 30-day paper validation window
  2. Generate dedicated live Alpaca keys + flip via Settings

Tests: **328 passing** locally (two sandbox-only failures in
`test_auth::test_password_strength_rejects_weak` and
`test_dashboard_data::...trading_session...` as documented).
Ruff clean. Coverage floor held at 15%.

See `GO_LIVE_CHECKLIST.md` for the pre-flip-to-live gating list.
