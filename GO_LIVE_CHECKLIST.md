# Pre-Live-Trading Checklist

Use this before flipping the Settings → 🔴 Live Trading toggle.
Paper-trading mode has forgiving edges that real money does not.

Last updated: 2026-04-19 (end of round-19 final polish).
**Code-side work is complete. Only timeboxed user actions remain.**

**Current `main` HEAD**: `648339d` (round-19).
**Tests**: 423 passing. **Coverage**: floor 20%, measured 25.4%.

---

## 1. Paper validation window

**Status:** ⏳ In progress (started 2026-04-15, ends ~2026-05-15)

**Don't flip until:**
- [ ] 30 consecutive days of paper trading completed without a
      guardrail trip you don't understand
- [ ] Scorecard shows positive expectancy after fees (check
      `/api/scorecard` or the Dashboard → Performance card)
- [ ] Max drawdown stayed under 10% for the window
- [ ] Wheel strategy: at least one full cycle (put-open → assignment
      → call-open → close) completed cleanly
- [ ] Kill switch tested end-to-end at least once (trip it from
      Settings, confirm scheduler aborts within ~1 tick, re-enable)

---

## 2. Operational prerequisites (user-only, out of bot scope)

### MASTER_ENCRYPTION_KEY
- [x] Set on Railway Variables tab — confirmed by user.
- [x] **Locked in** — DO NOT rotate. A change invalidates every
      stored credential; every user would have to re-enter their
      Alpaca keys from Settings.

### Sentry DSN rotation
- [x] DSN rotated. Old key deactivated in Sentry; new `SENTRY_DSN`
      set in Railway env vars.
- [ ] Confirm the PII scrub is live by triggering a test error and
      checking the Sentry UI shows `[REDACTED_KEY]` / `[REDACTED]`
      in auth headers (optional — do once you've got a real event)

### Alpaca live credentials
- [ ] Generate dedicated **live** API keys at
      <https://app.alpaca.markets/brokerage/dashboard/overview>
      (paper keys won't work against the live endpoint)
- [ ] Test them via Settings → 🔴 Live Trading → "Verify keys"
      BEFORE saving (the verify step is the round-13 hardened path;
      generic error message on failure, Sentry gets the detail)
- [ ] Funded with only what you're willing to lose in the first
      week (suggest: ≤ 10% of your intended deployment size)

### PWA icons
- [x] PNG icons generated and wired: `static/icon-192.png` (Android),
      `static/icon-512.png` (splash/high-DPI), `static/apple-touch-
      icon.png` (iOS 180×180). Listed in `manifest.json` alongside
      the original SVG. Shipped in PR #25.

---

## 3. Code / infrastructure checklist

All boxes below are satisfied as of 2026-04-19 unless noted.

### Money math
- [x] All strategy computation paths use Decimal internally
      (`_dec()` + `_to_cents_float()` in `tax_lots.py`,
      `update_scorecard.py`, `portfolio_risk.py`, `wheel_strategy.py`,
      `smart_orders.py`, `update_dashboard.calc_position_size`)
- [x] JSON boundary stays float-with-2dp (no API contract changes)
- [x] 80+ parity + fuzz tests green (`tests/test_decimal_*`)

### Safety rails
- [x] Beta-exposure gate live (round-12 PR #15 un-deaded it)
- [x] Drawdown sizing scaled via `portfolio_risk.compute_drawdown_multiplier`
- [x] Kill-switch atomic via `threading.Event` (no 100-300ms
      in-flight order window)
- [x] Wheel anomaly freeze + split auto-resolve (round-13 PR #20)
- [x] Daily journal trim at 3:15 AM ET (prevents unbounded growth)

### Security
- [x] MASTER_KEY mandatory; PLAIN credential fallback retired
- [x] AES-GCM ENCv3 for Alpaca creds; HKDF-derived keys
- [x] PBKDF2-600k password hashing
- [x] CSRF double-submit cookie + SameSite=Strict
- [x] Login rate limit (BURST=10, REFILL=0.2/s per (ip, user))
      in front of SQLite 5-per-15-min window
- [x] Password-reset TOCTOU guard (atomic UPDATE WHERE used=0)
- [x] Session fixation defence (invalidate pre-existing sessions on
      login)
- [x] HSTS header (`max-age=31536000; includeSubDomains`)
- [x] CSP + X-Frame-Options DENY + Referrer-Policy
- [x] SRI hashes pinned on chart.js / marked / zxcvbn CDNs
- [x] README XSS allowlist scrubber (round-13 PR #22)
- [x] Sentry PII scrub (`before_send`) hiding keys / emails /
      auth headers (round-13 PR #22)

### Exception handling
- [x] `server.py` catch-alls route through `observability.capture_exception`
- [x] `yfinance_budget._call_with_retry` fails fast on permanent
      errors; final failures go to Sentry (round-13 PR #21)
- [x] `wheel_strategy._detect_split_since` full-body guard
- [x] Circuit-breaker actually trips (round-13 PR #18)

### Tests
- [x] 328 tests passing (local; CI adds zxcvbn test → 329+)
- [x] 2 known-sandbox failures documented in `CLAUDE.md` (auth
      strength needs zxcvbn; account-live test needs network)
- [x] Ruff clean, coverage floor 15% enforced in CI
- [x] Handler mixins covered (round-13 PR #17)
- [x] `smart_orders.place_smart_buy/sell` full-flow covered (PR #19)
- [x] Scheduler cb-reset covered (PR #18)
- [x] Strategy modules — pead, short, earnings, insider (round-16 PR #30)
- [x] State-recovery validator (round-16 PR #30) — boot-time wheel +
      journal vs Alpaca position consistency check
- [x] capital_check fallback ladder pinning tests (round-15 PR #29)
- [x] Per-user isolation invariant pinning tests (round-15 PR #29)
- [x] Scheduler-API extraction contract tests (round-17 PR #31)

### Notification + alert wiring (verified post round-15)
- [x] Kill-switch trip → ntfy push + email + Sentry (round-14 PR #28
      fixed the email path, was silently broken since round-11)
- [x] Daily -3% loss → critical_alert wired (round-15 PR #29 — was
      a dashboard-only flag with no notification path)
- [x] Alpaca 401/403 (cred rot) → critical_alert with per-day dedup
      (round-15 PR #29)
- [x] yfinance circuit-breaker open → notify_user push (round-12)
- [x] Scheduler down >5 min → /healthz returns 503 → Railway alert

### Deploy
- [x] Procfile + railway.json + nixpacks.toml present
- [x] `/api/version` returns git commit SHA dynamically
- [x] JSON-structured logging via `logging_setup.init()`
- [x] Railway auto-deploys on push to `main`

### Architecture
- [x] cloud_scheduler.py monolith split — Alpaca API plumbing in
      `scheduler_api.py` (round-17 PR #31)
- [x] State-recovery boot validator (`state_recovery.py`, round-16 PR #30)
- [x] Per-user isolation extracted (`per_user_isolation.py`, round-15 PR #29)

---

## 4. The actual flip

When everything above is green:

1. Trigger a final paper-trading clean close: Settings → Stop all
   strategies → wait for "no open orders"
2. Settings → 🔴 Live Trading → paste live keys → Verify → Save
3. Confirm dashboard header now shows `live` not `paper`
4. Redeploy **one** strategy (suggest: Wheel on a single ticker
   you know well) with a small position size
5. Watch for 48h before scaling up or adding strategies

---

## 5. Rollback plan

If anything looks off after live flip:

1. Kill switch (Settings → Kill Switch → confirm)
2. Settings → Live Trading → Switch back to Paper
3. Ntfy / email will notify you of the flip
4. All open live orders get cancelled in the next tick
5. Paper-mode state resumes from where it was pre-flip

---

## 6. Things we intentionally didn't fix

Called out so future you / future Claude doesn't re-open these:

- **Forgot-password enumeration via rate-limit exhaustion** — mitigated
  by the token bucket; marginal risk for the user base size
- **Kill-switch latency up to one scheduler tick** (~100ms) —
  acceptable for paper + single-user-ish scale
- **400+ explicit `log.info()` migrations** from print-shim — the
  shim handles it correctly; explicit migration is cosmetic
- **Sortino divisor** — standard formulation uses total N, not
  `len(neg_returns)`; round-13 audit called it a false positive

---

## 7. Ongoing monitoring (after flip)

- [x] Notifications wired — ntfy.sh push + email via Gmail MCP
      + Sentry alerts (Project Settings → Alerts) are all active.
- [ ] Verify ntfy.sh receives the first live-mode kill-switch or
      daily-summary push after the flip (one-time confirmation)
- [ ] Verify daily-close email lands in `notification_email` after
      the first live-mode close
- [ ] Check `/api/admin/health` daily for the first week
- [ ] Watch scheduler latency in the logs — flag if any tick takes
      > 60s (indicates rate-limit saturation or infinite loop)
