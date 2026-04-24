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
   — expect **1802 passing, 3 deselected** after round-61 pt.32 (1746 + 17 pt.30 + 13 pt.31 + 26 pt.32).
4. `ruff check .` — clean.
5. Validate dashboard JS: `awk '/^<script>/,/^<\/script>/' templates/dashboard.html | grep -v '^<script>' | grep -v '^</script>' > /tmp/dash.js && node --check /tmp/dash.js`
6. `npm ci && npx vitest run` — expect **341 JS tests passing** (29 files).
   Run `node --check static/dashboard_render_core.js` after any edit to
   the extracted module.

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

## Current session state (2026-04-24 — round 61 pt.10-32 SHIPPED)

**Pt.32 landed (PR #158):** orchestrator coverage. Source-pin tests
for the four scheduler orchestrators that drive every live trade —
the gaps that earlier rounds didn't fill. Existing
`test_round61_auto_deployer.py` covers `run_auto_deployer`
extensively; pt.32 adds parallel coverage for `run_daily_close`
(subprocess wrapper + per-user env var construction + RMW lock
ordering + email queueing), `run_wheel_auto_deploy` (kill-switch +
auto-deployer-disabled + wheel-disabled gating + dedup via
`_wheel_deploy_in_flight` + mid-loop kill-switch abort + per-day
cap), and `run_wheel_monitor` (kill-switch + `list_wheel_files`
iteration + `advance_wheel_state` dispatch + stage-2 covered-call
auto-pilot + wheel-open backfill at end-of-tick). Plus
cross-orchestrator invariants: every orchestrator must log with
`[username]` prefix, every orchestrator wraps its body in
try/except so a single tick can't kill the scheduler thread.
+26 source-pin tests in `tests/test_round61_pt32_orchestrator_coverage.py`.

**Pt.31 landed (PR #157):** partial-qty sell paths must shrink the
protective stop BEFORE placing the sell. User-reported every
Friday at 3:45 PM ET on INTC: 63 shares long, sell-stop reserved
all 63, trim of 31 failed with HTTP 403 / `alpaca_code=40310000`
"insufficient qty available for order (requested: 31, available: 0)".
Same bug class as the SOXL pt.30 cover-stop loop, just on the
long side and in a different code path.
  **Fix:** new helper `_shrink_stop_before_partial_exit` PATCHes
  the stop's qty to `remaining` (or cancels and lets the caller
  re-place if PATCH fails). Two paths fixed:
    * `check_profit_ladder` — sells 25%/25%/25%/25% of original at
      +10/+20/+30/+50% profit; each rung's market sell would 403
      because the trailing stop reserved the whole position.
    * `run_friday_risk_reduction` — sells half of any +20% winner
      before weekend; same 403.
  Already-correct paths (mean-reversion target, PEAD time/earnings,
  universal pre-earnings, monthly rebalance) left untouched and
  pinned via source assertions.
  +13 tests in `tests/test_round61_pt31_partial_sell_qty_reservation.py`.

**Pt.30 landed (PR #156):** position-drift guard + qty-available
retry. User-reported SOXL stuck retrying a cover-stop every monitor
tick with HTTP 403 / `alpaca_code=40310000` "insufficient qty
available for order (requested: 29, available: 0)" despite the
short being live at -29 shares. Two fixes in one PR:
  1. **Drift guard:** at the top of `process_short_strategy` and
     `process_strategy_file`, query `/positions/{symbol}`. On
     positive evidence of drift (404 error dict, qty=0, or
     abs(qty)<state-shares) close/sync the strategy file. Fails
     OPEN on transient errors (circuit breaker, rate limit, 5xx).
     Symmetric on long + short paths.
  2. **Qty-available retry:** when cover-stop placement fails with
     a 40310000-style error, query `/orders?status=open` for the
     symbol, cancel any open BUY orders (they're competing for the
     same short-cover qty), retry placement once. Fixed the live
     SOXL loop where a leftover `target_order_id` BUY limit at
     $94.05 was reserving all 29 shares and blocking every
     cover-stop attempt.
  +17 tests in `tests/test_round61_pt30_position_drift.py`.

**Pt.27 landed (PR #153):** daily-close email "Today" fix. User
forwarded Apr 24 close email showing `+$39.29` when actual day
P&L was `+$707.67` (CRDO +3.5% / INTC +22% / HIMS +43.9%). Root
cause: `_build_daily_close_report` used
`guardrails.daily_starting_value` as the baseline, which is
captured by the monitor's FIRST tick of the day — if the monitor
didn't fire at 9:30 AM open (mid-deploy, heartbeat gap at the
bell), it captured a mid-day value and missed the morning move.
For Apr 24: `daily_starting_value=$103,214` (mid-morning) vs
Alpaca `last_equity=$102,425.35` (true yesterday's close). Close
$103,133 − $103,214 = −$81 (shown as +$39 after rounding), vs
correct $103,133 − $102,425 = +$707.67.
  **Fix:** swap priority in `_build_daily_close_report` to prefer
  Alpaca's `last_equity` (canonical yesterday's close, always
  full-day), fall back to `daily_starting_value` only when
  `last_equity` is missing.
  +4 tests in `tests/test_round61_pt27_daily_close_today.py`
  (regression pin for exact Apr 24 numbers + fallback + zero
  safety + source ordering pin).

**Pt.26 landed (PR #152):** aggressive open-orders cross-check +
loud placement-failure logging. SOXL STILL showed missing_stop
HIGH after pt.25. Pt.24/25 handled dead-status list + error
dicts, but missed these edge cases:
  * Order in `accepted` / `pending_new` that silently expired
  * Single `/orders/{id}` lookup returns stale/partial data not
    matching any known dead alias
  * Order was replaced/canceled outside the monitor loop and
    Alpaca's status propagation hasn't caught up
  **Fix 1:** cross-check against Alpaca's open-orders ground
  truth (`/orders?status=open&symbols=`). If the persisted
  `cover_order_id` / `stop_order_id` isn't in that list, reset
  + retry regardless of what the individual order lookup said.
  **Fix 2:** loud placement-failure logging. Before pt.26, when
  Alpaca rejected a stop (insufficient margin, shorting
  restricted, invalid qty), the failure was silently swallowed.
  Now every rejection logs the Alpaca response so the user /
  operator can diagnose.
  +5 tests in `tests/test_round61_pt26_open_orders_crosscheck.py`.

**Pt.25 landed (PR #151):** fix pt.24 unreachable-elif + remove
ghost-cleanup grace period. Two regressions from pt.24 deploy:
  1. SOXL still showed missing_stop HIGH. Pt.24's short-side
     `cover_order_id` liveness check had unreachable code:
     Alpaca error responses (`{"error": "..."}`) hit the first
     dict branch, but `get("status")` returned None → empty
     string → no dead-status match → no reset → cover_order_id
     stayed forever. Fix: check `"error"` key FIRST inside the
     dict branch. Normalized long-side `stop_order_id` path to
     the same shape.
  2. 🧹 Clean Up Ghosts always reported "Closed 0, skipped 2"
     — `error_recovery.py`'s periodic Check 3 touches these
     files every 10 min (`safe_save_json`'s atomic-rename bumps
     mtime even on no-ops), so they never fell outside the
     10-min grace window. Fix: remove the grace-period filter
     from the USER-TRIGGERED cleanup path. Clicking the button
     is explicit intent. Scheduled Check 3 keeps its grace
     period. Pending-sell check remains.
  +9 tests (5 E2E in `test_round61_pt25_soxl_stop_e2e.py` +
  4 in `test_round61_pt25_force_ghost_cleanup.py`).

**Pt.24 landed (PR #150):** 3 live audit findings (SOXL + CORZ +
AXTI). User ran 🔍 Audit and modal showed:
  * HIGH missing_stop: SOXL (qty -29, no BUY stop)
  * LOW ghost_strategy_file: breakout_CORZ.json
  * LOW ghost_strategy_file: breakout_AXTI.json
  **Fix 1:** `process_short_strategy` used `if not
  state.get("cover_order_id"):` to gate placement. If a prior
  placement was rejected (invalid price pre-pt.19, auth failure,
  rate limit) or later canceled, the stale order-id stayed in
  state forever → placement skipped forever. Before trusting the
  persisted id, query Alpaca — if status is `canceled/rejected/
  expired/replaced/done_for_day` or lookup returns error (404 on
  non-existent id), reset `cover_order_id=None` so the next
  block places a fresh stop. Same treatment for long-side
  `stop_order_id`. (Note: this pt.24 check had an unreachable
  elif — fixed in pt.25.)
  **Fix 2:** `error_recovery.py` Check 3 marks stale files
  closed but only runs inside `run_daily_close` (4:05 PM ET) +
  the "Adopt MANUAL → AUTO" path. User sees ghosts in the audit
  modal and wants to clear them NOW. New
  `/api/close-ghost-strategies` endpoint + 🧹 Clean Up Ghosts
  button in the audit modal runs Check 3's exact logic in-
  request. Modal auto-re-runs audit after cleanup.
  +10 tests in `tests/test_round61_pt24_audit_findings_fix.py`.

**Pt.23 landed (PR #149):** self-audit catch — multi-contract
wheel state-overwrite bug introduced by pt.22.
`wheel_strategy.save_wheel_state(user, state)` derived the write
path via `wheel_state_path(user, state["symbol"])` where
`state["symbol"]` is the underlying (e.g. "HIMS"). So for any
state loaded from `wheel_HIMS__260515P26.json` (the pt.22
indexed sibling file), saving would write to `wheel_HIMS.json`
(the default file). On every 15-min wheel-monitor tick both
files got loaded, both writes landed on `wheel_HIMS.json`,
last-write-wins → indexed file frozen forever. Multi-contract
wheel support was silently broken until pt.23.
  **Fix:** `list_wheel_files` stamps a `_state_file` marker on
  each loaded state dict with the filename it came from.
  `save_wheel_state` reads the marker and writes back to the
  same file. Falls back to default `wheel_<UNDERLYING>.json`
  when marker is absent (fresh state from
  `run_wheel_auto_deploy`). Marker stripped before persist so
  it doesn't leak into the JSON schema.
  +5 tests in `tests/test_round61_pt23_wheel_state_per_file.py`.

**Pt.22 landed (PR #148):** defensive hardening + audit-modal fix.
Six-in-one PR addressing the 4 gaps flagged after pt.21 close-out +
the audit button being broken:

  1. **Audit modal visibility fix.** Pt.21 used
     `class="modal-backdrop"` which didn't exist in the stylesheet —
     button said "Running..." but modal never appeared. Rewrote to
     use the shared `.modal-overlay.active` pattern + `openModal()`
     helper that every other modal uses. Static `<div
     id="auditModal">` markup + dynamic body fill via
     `renderAuditReport()`.
  2. **Silent `except Exception: pass` ratchet.** New test
     `test_round61_pt22_no_silent_except.py` scans 9 auth/trading
     files (auth.py, cloud_scheduler.py, handlers/*, wheel,
     scheduler_api, smart_orders, error_recovery) and fails CI if
     the count increases. Baseline frozen at current production
     numbers. Opt-out per line via `# noqa: silent-except` marker
     comment. Fixes the pt.13-class bug forever (PanicException
     silently swallowed).
  3. **Production snapshot regression test.** New fixture
     `tests/fixtures/production_snapshot_scrubbed.json` with all
     IDs/usernames/emails redacted. Accompanying test runs
     `audit_core.run_audit` against it and pins the exact
     findings expected (5 HIGH orphans + SOXL missing_stop + 1
     MEDIUM stale scorecard). Any future audit-rule change that
     breaks detection on this known-bad state fails CI.
  4. **Multi-contract wheel support.** Pt.17 created one
     `wheel_<UNDERLYING>.json` per underlying — a user with TWO
     short puts on the same symbol (e.g. HIMS May 8 $27 + May 15
     $26) got the second contract left as an orphan. Pt.22
     creates an indexed sibling file
     `wheel_<UNDERLYING>__<YYMMDD><C|P><STRIKE8>.json` when the
     default file is already tracking a different contract. Both
     the dashboard (`_mark_auto_deployed`) and the audit
     (`audit_core._parse_strategy_filename`) now handle the
     double-underscore pattern so both contracts resolve to the
     underlying for labelling.
  5. **JS coverage threshold in CI.** `vitest.config.js` now
     declares a `thresholds` block + CI runs
     `npx vitest run --coverage`. Floor starts at 0% (vm-executed
     code isn't instrumented by V8) with a clear comment on how
     to raise it by switching to `@vitest/coverage-istanbul` in a
     follow-up. Ratchet infrastructure is in place.
  6. **cloud_scheduler helper coverage.** 13 new tests covering
     `_fmt_money`, `_fmt_pct`, `_fmt_signed_money`,
     `_within_opening_bell_congestion`, `_compute_stepped_stop`
     (defensive pin in case pt.18 tests drift), `_has_user_tag`,
     `_build_user_dict_for_mode`.

  +31 tests in 4 new test files. Docs updated.

**Pt.21 landed (PR #147):** consolidated cleanup + audit
infrastructure. User requested a way to prevent the key-drift
bug class that spawned pt.16/19/20, plus fix two production data
issues from the JSON audit (DKNG + HIMS OCC options mis-routed
through the short_sell path). Five-part change:

  1. **`constants.STRATEGY_NAMES` + `CLOSED_STATUSES`** —
     single source of truth imported by `server._mark_auto_deployed`,
     `error_recovery.list_strategy_files`, `scorecard_core.STRATEGY_BUCKETS`.
     Prior to pt.21 these were three independent hard-coded lists
     that had to be updated in lockstep; pt.19/20 each landed because
     someone missed a spot. Now there's one place.

  2. **`error_recovery.migrate_legacy_short_sell_option_files`** —
     boot-time retrofit. Scans STRATEGIES_DIR for legacy
     `short_sell_<OCC>.json` files, creates matching
     `wheel_<UNDERLYING>.json` (in `stage_1_put_active` for puts,
     `stage_2_call_active` for covered calls) with active_contract
     populated from the OCC parse, and marks the old file
     `status: "migrated"` so it stops being picked up by either the
     dashboard labeller or the short-sell monitor. Idempotent — runs
     every error_recovery invocation, no-ops if nothing to migrate.
     Fixes user-reported DKNG + HIMS mis-routing.

  3. **`/api/audit` endpoint + `audit_core.run_audit`** — pure
     helper that cross-checks positions / orders / strategy files /
     trade journal / scorecard for 7 categories of inconsistency
     (orphan position, legacy OCC mis-routing, ghost strategy file,
     missing stop, invalid stop price relative to current, unknown
     strategy name in journal, stale scorecard). Returns structured
     severity-grouped findings. Endpoint lives at `/api/audit` with
     a 🔍 Audit button in the Positions section header — one click
     surfaces every inconsistency in plain English.

  4. **Dashboard audit modal** — modal with HIGH/MEDIUM/LOW pills
     and colour-coded findings. No need to grep log files.

  5. **`migrate_auto_deployer_strategies_round61_pt21`** — per-user
     boot migration widens `auto_deployer_config.strategies` from
     the historical `[trailing_stop, mean_reversion, breakout]` to
     include all 7 strategies (adds wheel, pead, short_sell,
     copy_trading). Idempotent, never removes user-added strategies.

  +26 tests in `tests/test_round61_pt21_audit_and_migration.py`.

**Pt.20 landed (PR #146):** dashboard strategy-label map missing
the `'short_sell'` key (backend writes that; map had `'short'`).
Also switched orphan-found notifications from `warning` to `info`
severity + dedup by symbol set. Affected users: anyone with a
SHORT position saw AUTO badge without the SHORT SELL pill.
  +8 tests.

**Pt.19 landed (PR #145):** three user-reported issues from the
pt.17/18 deploy:
  1. SOXL adopted (AUTO) but no BUY stop. Monitor's initial
     cover-stop placement used `entry * (1 + stop_pct)` which
     was below current market for the underwater short → Alpaca
     rejected every placement. Fix: adaptive formula
     `max(entry*(1+pct), current*1.05)`.
  2. HIMS short put still MANUAL after "Adopt" click. Grace-
     period filter treated the user's manual BUY stop as an
     "in-progress entry" and skipped adoption. Fix: exempt
     shorts + OCC options from the grace-period filter.
  3. `short_sell` was missing from `scorecard_core.STRATEGY_BUCKETS`
     → every closed short trade silently dropped from performance
     attribution.
  +9 tests.

**Pt.18 landed (PR #144):** professional stepped trailing stop —
replaces the flat `highest * (1 - trail)` formula with tier-based
risk management used by institutional trend-following systems.

Tiers (measured from entry using highest_seen for longs / lowest_seen
for shorts):
  * **Tier 1 (0 to +5%)** — default trail (usually 8%) for breathing
    room during the breakout retest
  * **Tier 2 (+5 to +10%)** — **STOP LOCKED TO ENTRY** — no-loss
    guarantee from here. One-time user notification fires + state
    records `break_even_triggered: true`.
  * **Tier 3 (+10 to +20%)** — 6% trail: lock in some gain while
    still allowing a pullback.
  * **Tier 4 (+20%+)** — 4% trail: ride the big move tight.

Single source of truth: `cloud_scheduler._compute_stepped_stop(entry,
extreme, default_trail, is_short)`. Used by BOTH the long trailing
path (`process_strategy_file`) and short trailing path
(`process_short_strategy`) so tier logic stays in one place.

Opt-out: strategy files with `rules.stepped_trail=false` revert to
flat trail. Default `True` — users get professional behaviour
without explicit enablement.

State tracks `profit_tier` + `break_even_triggered` for audit. Tier
transitions log + notify on first entry into Tier 2/3/4 per position.

  +23 tests in `tests/test_round61_pt18_stepped_trail.py`.

**Pt.17 landed (PR #143):** adaptive stops + OCC-option orphan
adoption. Two bugs from pt.15 deploy:
  1. SOXL's cookie-cutter `stop = entry*1.10` put buy-stop BELOW
     current market (short was deeply underwater). Fix: adaptive
     formula `max(entry*1.10, current*1.05)` (short) /
     `min(entry*0.90, current*0.95)` (long). Applied in both
     `create_orphan_strategy` AND Check 2 Missing Stop-Loss.
  2. HIMS OCC short-put was routed to `short_sell_<OCC>.json` which
     monitor_strategies' equity short path can't handle. Fix: new
     `_occ_parse(sym)` helper; OCC orphans route to
     `wheel_<UNDERLYING>.json` in `stage_1_put_active` (short put)
     or `stage_2_call_active` (covered call). Long options skipped
     (no strategy for long premium).
  +14 tests in `tests/test_round61_pt17_adaptive_stops_and_options.py`.

**Pt.16 landed (PR #142):** URGENT — pt.15's "Adopt MANUAL → AUTO"
button returned "No MANUAL positions found" even though the
dashboard clearly showed MANUAL. Root cause: `error_recovery.list_strategy_files`
was returning ALL files (including closed ones) while the dashboard's
`_mark_auto_deployed` filters closed files (round-61 #110). Fix:
match the same closed-status set in both code paths (case-insensitive).
monitor_strategies already filters on the active side so both files
can coexist.
  +6 tests in `tests/test_round61_pt16_orphan_closed_skip.py`.

**Pt.15 landed (PR #141):** user-reported SOXL short labeled
MANUAL despite being opened by the auto-deployer. Root cause:
`error_recovery.py` (orphan adoption → synthesizes strategy files
for positions without one) only ran ONCE per day inside
`run_daily_close`. Between 4:05 PM ET closes, any position opened
outside the bot or whose strategy file got cleaned up sat
unmanaged for up to 23.5 hours. Fix:
  1. `cloud_scheduler.run_orphan_adoption(user)` — per-user wrapper
     around `error_recovery.py` subprocess (same isolation pattern
     as `run_daily_close`).
  2. Scheduled every 10 min during market hours via
     `should_run_interval(f"adopt_orphans_{uid}", 600)`.
  3. On-demand endpoint `/api/adopt-orphans` + "🤖 Adopt MANUAL →
     AUTO" button in the Positions header.
  +10 tests in `tests/test_round61_pt15_auto_orphan_adoption.py`.

**Pt.14 landed (PR #140):** URGENT — start.sh was running Nix's
system `python3` instead of `/opt/venv/bin/python` where pip had
installed cryptography (and everything else). Pt.13's red banner
pinpointed it: "Boot-time import error: ModuleNotFoundError: No
module named 'cryptography'". Fix: start.sh prefers the venv
interpreter with a loud WARNING fallback if `/opt/venv` is missing,
AND does a boot-time `AESGCM` import smoke check so Railway's log
shows success/failure BEFORE the server starts handling requests.
  +4 tests in `tests/test_round61_pt14_venv_python_fix.py`.

**Pt.13 landed (PR #139):** URGENT — user confirmed
`MASTER_ENCRYPTION_KEY` unchanged but save still failed. Exposed
the real cause: cryptography import was silently failing at boot
(`except Exception: pass` swallowed it). Fix:
  1. Use `except BaseException` (pyo3 PanicException is
     BaseException, not Exception).
  2. Capture the exception text into `_AESGCM_IMPORT_ERROR`
     module-level var.
  3. Print `[auth] CRITICAL: ...` to stderr at module load.
  4. `encrypt_secret` RuntimeError now includes the captured error.
  5. `_fetch_live_alpaca_state` appends an `encryption: ...` entry
     to `api_errors` when `_HAS_AESGCM` is False.
  6. Dedicated red dashboard banner for `encryption:` errors.
  7. `requirements.txt` pinned cryptography <44.0.0 + added cffi
     explicitly. nixpacks.toml added libffi.
  +10 tests in `tests/test_round61_pt13_crypto_import_diag.py`.

**Pt.12 in flight (PR #138):** URGENT user-reported "Save failed"
with no detail when re-entering Alpaca keys, even though Test
Connection PASSED with the same keys. Three fixes:
  1. `handle_save_alpaca_keys` differentiates HTTP 401/403/429/5xx
     with hint copy + wraps `auth.save_user_alpaca_creds` in
     try/except so the user sees the actual exception body.
  2. New `/api/test-saved-alpaca-keys` endpoint tests the
     currently-saved keys (instead of asking user to re-paste).
     Detects MASTER_ENCRYPTION_KEY-rotation case.
  3. "Test Saved Keys (PAPER)" / "(LIVE)" buttons in Settings →
     Alpaca API.
  +11 tests in `tests/test_round61_pt12_alpaca_key_diag.py`.
  `tests/test_round6.py` LOC cap 3300 → 3310.

**Pt.11 landed (PR #137):** URGENT user-reported "$0.00 + 100%
drawdown" false alarm. Alpaca `/account` returned an error →
dashboard rendered $0 everywhere → drawdown calc `(peak - 0)/peak
= 100%` → red "Approaching max drawdown limit!" banner. Two fixes:
  1. `buildGuardrailMeters` treats `portfolioValue <= 0` as "no
     data" — shows "—/10% limit", meter at 0%, suppresses warnings.
  2. Top-of-page orange banner when `d.api_errors.account` exists,
     pointing user at Settings → Alpaca API. Server already
     populated `api_errors` — render layer was dropping it.
  +4 JS tests. Python 1561 → unchanged. JS 388 → 392.

**Pt.10 landed (PR #136):** more cloud_scheduler helper extractions
+ batch-10 panel renderer extractions. Caught regression: extracted
`buildNextActionsPanel` reads `window.autoDeployerEnabled` but
inline `let` doesn't attach to window. Fix: inline callers pass
opts object explicitly:
```js
buildNextActionsPanel(d, {
    autoDeployerEnabled: autoDeployerEnabled,
    killSwitchActive: killSwitchActive,
    guardrails: guardrailsData,
})
```
Python 1536 → 1561 (+25).

**Pt.9 landed (PR #134)**: cloud_scheduler helper coverage + a
user-reported calibration error-message fix. Python tests 1490 →
1536 (+46). cloud_scheduler.py coverage ~31% → ~40% locally.

**Pt.9 covered helpers:** `_fmt_money` / `_fmt_pct` / `_fmt_signed_money`,
`check_correlation_allowed`, deploy-abort round-trip, `should_run_interval`,
`should_run_daily_at`, `_clear_daily_stamp`, `_within_opening_bell_congestion`,
`_has_user_tag`, `_build_user_dict_for_mode`, `strategy_file_lock`,
`is_first_trading_day_of_month`.

**Calibration UX fix (user-reported):** user saw "Equity below $500
or Alpaca /account returned no data" on a $103k paper account. Root
cause: the handler lumped THREE failure modes (API error, low equity,
missing equity field) into one message. `/api/calibration` now
differentiates + points at the fix location (Settings → Alpaca API).
Pins in `tests/test_round61_pt9_calibration_error_message.py`.

**Option B extraction landed (#132 + #133).** `static/dashboard_render_core.js`
now holds **28 pure helpers** (19 from #132 + 9 panel helpers from
#133). Test loader (`tests/js/loadDashboardJs.js`) reads both files;
`tests/js/loadRenderCore.js` lets tests hit the extracted module in
isolation. `templates/dashboard.html` went 9693 → 8866 LOC (-827
lines of inline duplicated code).

**Extracted helpers (28, ~100 direct-import tests):**
  * XSS: `esc`, `jsStr`
  * Formatters: `fmtMoney`, `fmtPct`, `pnlClass`, `fmtUpdatedET`,
    `fmtAuditTime`, `fmtRelative`
  * OCC parser: `_occParse`
  * Scheduler: `SCHEDULED_TIME_MAP`, `parseSchedTs`, `latestForTask`,
    `fmtSchedLast`
  * Regime / heatmap / freshness: `getMarketRegime`, `heatmapColor`,
    `freshnessChip`
  * Preset detection: `detectActivePreset`
  * README sanitizer: `_sanitizeReadmeHtml` + `_README_ALLOWED_TAGS`
    + `_README_ALLOWED_ATTRS`
  * Section visibility: `getHiddenSections`, `setHiddenSections`,
    `toggleSectionId`
  * Section help: `sectionHelpButton`
  * Panel renderers: `buildGuardrailMeters`, `buildTodaysClosesPanel`,
    `buildShortStrategyCard`, `buildComparisonPanel`,
    `buildStrategyTemplates`, `buildNextActionsPanel`

**Extraction bug caught during Option B:** the original inline `jsStr`
escaped `&` → `&`; the first draft of the extraction missed it.
Fixed before any inline duplicate was removed. Regression-guard test
now exists.

**Test counts (on `main` after pt.14; pt.15 pending merge):**
  * Python: **1586 passing**, 3 deselected (pt.15 adds +10)
  * JS: **392 passing** across 31 files
  * CI coverage floor: 50% Python (ratcheted up from 48% in #133)

**Picking up next session:**
1. `git pull --ff-only origin main` after #138 merges
2. Verify Railway deploy: `curl https://stockbott.up.railway.app/api/version`
3. Have user click "Test Saved Keys (PAPER)" — pinpoints whether
   keys decrypt + Alpaca accepts them. Most likely cause if it
   fails: MASTER_ENCRYPTION_KEY rotated on Railway → re-enter keys
   via Settings → Alpaca API form.

**pt.10 in flight** (branch `claude/round-61-pt10-more-helpers`):
more `cloud_scheduler.py` helpers + remaining render surface.
Candidates: `user_file` / `user_strategies_dir` / `load_json` / `save_json`
round-trip tests + any more panel renderers we haven't moved.

**Pt.10 follow-ups (future PRs):**
  * Add Vitest `--coverage` with a threshold floor in CI.
  * Orchestrator coverage (`run_auto_deployer`, `run_daily_close`,
    wheel flow) — needs heavier mocks; save for after pt.10.

---

## Previous session state (2026-04-24 — round 61 pt.8 BATCH-9)

Section-visibility helpers (+8, total JS tests 285). Shipped via #131.

## Previous session state (2026-04-24 — round 61 pt.8 BATCH-8)

short-card / comparison / templates / help-button (+36, total 277). #130.

## Previous session state (2026-04-24 — round 61 pt.8 BATCH-7)

sanitize / preset / panels (+36, total 241). #129.

## Previous session state (2026-04-24 — round 61 pt.8 AUDIT)

Full 5-agent tech-stack audit. Results:
  * Security: CLEAN
  * DB/concurrency: **2 HIGH bugs — fixed in #127**
    * `cloud_scheduler.py:run_friday_risk_reduction` RMW without lock
    * `cloud_scheduler.py:run_monthly_rebalance` RMW without lock
    Both wrap in `strategy_file_lock(sf_path)` now. Pins in
    `tests/test_round61_pt8_audit_concurrency_fixes.py`.
  * Trading logic: CLEAN (all post-55..61 invariants verified)
  * UX: 3 flags → 2 fixed (a11y label `for=` on signup invite_code +
    reset password), 1 rejected (hypothetical Chart.js destroy)
  * Tests/ops: CLEAN

## Previous session state (2026-04-24 — round 61 pt.8 BATCH-6)

toggleAutoDeployer + kill-switch modal + misc (+24, total 205). #128.

## Previous session state (2026-04-24 — round 61 pt.8 BATCH-5)

sell-fraction / cancel-order / fmtSchedLast (+21, total 181). #126.

## Previous session state (2026-04-24 — round 61 pt.8 BATCH-4)

closePositionModal wheel math + modal lifecycle (+24, total 160). #125.

---

## Previous session state (2026-04-24 — round 61 pt.8 BATCH-3)

**Pt.8 batch-3 on `claude/round-61-pt8-batch3`.** Pure test-only
PR. JS tests 99 → **136** (+37) across 4 new files:
  * `toast` (16) — info/success/error/warning branches, XSS on
    message + correlation ID, retry-button callback + removal,
    retry-throws-swallowed, `toastFromApiError` dispatch paths.
  * `log` (8) — newest-first order, type→class, XSS escape, 20-
    entry visible cap, Round-57 hash-skip quiet-tick pin.
  * `fmtRelative` (8) — "just now" / "Xm" / "Xh" / "Xd" bucket
    boundaries, unix-seconds input, "future" / "never" / garbage.
  * `scrollToSection` (5) — Round-53 active-tab + `_activeNavSection`
    pins.

Loader now exposes `toastFromApiError` + `fmtRelative`. `let`-scoped
module state (activityLog) still not resetable — tests assert on
relative position instead of absolute counts.

**Pt.8 follow-ups (future PRs):** renderPortfolioImpact.colorFor
math, openClosePositionModal OCC breakdown math (short put
assignment, covered call assignment), possibly JS coverage
threshold in CI once a stable floor is established.

---

## Previous session state (2026-04-24 — round 61 pt.8 BATCH-2)

**Pt.8 batch-2 on `claude/round-61-pt8-batch2`.** Builds on the
kickoff (#122). JS tests 68 → 99 (+31) across 3 new files:
`atomicReplaceChildren` (9, pins all 5 jitter-fix failure modes),
`freshnessChip` (12, pins the post-60 `data-label` invariant +
age-tier classes + XSS escape), `focusables` (10, `_focusablesIn`
for modal keyboard accessibility).

**Loader fix:** stubs `setTimeout` alongside `setInterval` so the
dashboard's delayed `measureStickyHeader(500ms)` doesn't fire after
jsdom teardown. Silences the "document is not defined" async
uncaught-exception noise that was flagging every batch with a
spurious "1 error" summary.

**User-reported AUTO/MANUAL fix:** screenshot showed CRDO / DKNG put
/ HIMS put / INTC / SOXL all labeled MANUAL. Root cause for the
wheel puts: `wheel_strategy.py:791` writes
`deployer="wheel_auto_deploy"` to the journal, but
`server._mark_auto_deployed`'s allowlist only recognized three
values. Added `"wheel_auto_deploy"` to the tuple + source + behavioral
pins. Equity positions use `"cloud_scheduler"` (already in list) —
if still MANUAL it's a stale-file + trimmed-journal edge case.

**Pt.8 follow-ups (next PRs):** renderDashboard core path,
openClosePositionModal wheel option math (short put assignment,
covered call), scroll helpers, toast / log. Add JS coverage
threshold to CI once we have a stable floor.

---

## Previous session state (2026-04-24 — round 61 pt.8 KICKOFF)

**Pt.8 (Vitest + jsdom for dashboard JS) kicked off on
`claude/round-61-pt8-vitest`.** Lands the JS test infrastructure
that pt.5/6/7 couldn't reach: ~7000 LOC of inline JS in
`templates/dashboard.html` is finally testable.

  * `package.json` + `vitest.config.js` + `tests/js/loadDashboardJs.js`
    — extracts the inline `<script>` from dashboard.html, runs it in
    jsdom via `vm.runInThisContext` so its top-level `function`
    declarations attach to the global scope. Stubs out Chart/marked
    CDN libs, no-op-ifies setInterval, scaffolds toastContainer/app/
    logPanel DOM nodes, swallows the auto-`fetch()` so `init()`
    doesn't pollute test output.
  * 4 starter test files (68 tests passing): esc/jsStr (XSS escapes),
    fmtMoney/fmtPct/pnlClass/fmtUpdatedET (format helpers), `_occParse`
    (OCC option-symbol parser), parseSchedTs/latestForTask
    (scheduler timestamp helpers).
  * CI extended: `.github/workflows/ci.yml` adds Node 20 setup +
    `npm ci` + `npm test` step after the existing pytest job.

**Two user-reported regressions piggybacked in this PR:**
  * Mobile admin → Users table: buttons stacked vertically (40px
    each × 6 actions = 240px+ per row). Wrapped in `.admin-actions`
    flex-wrap container, hide Email/Logins on mobile, hide
    Role/Last-Login on ultra-narrow.
  * Paper-vs-Live "Win Rate 0%" anchored on missing
    `win_rate_reliable` API field. Both panels now defensive: derive
    reliability from `closed_trades >= 5` directly so the post-60
    invariant ("show N=X, Need 5+ when sample is small") holds even
    when the backend field is missing/wrong.

**Pt.8 follow-ups (next PRs):** ratchet JS coverage by adding more
test files (renderDashboard core, openClosePositionModal OCC math,
atomicReplaceChildren scroll preservation). Then add a JS coverage
threshold to CI similar to Python's `--cov-fail-under`.

---

## Previous session state (2026-04-24 — round 61 pt.7 CLOSE-OUT)

**Pt.7 kickoff (#119) + follow-up (claude/continue-pt7-xEG9a) both
landed.** `screener_core.py` and `scorecard_core.py` both exist,
both are outside the coverage omit list, both have dedicated
behavioral test files:
  * `tests/test_round61_pt7_screener_core.py` — 62 tests covering
    `pick_best_entry_strategy`, `trading_day_fraction_elapsed`,
    `score_stocks` (breakout/wheel/mean-reversion tiers, volatility
    soft-cap, injected copy + pead score-fns with exception swallow,
    sector + sort), `apply_market_regime`,
    `apply_sector_diversification`, `calc_position_size`,
    `compute_portfolio_pnl`. 98% line+branch coverage.
  * `tests/test_round61_pt7_scorecard_core.py` — 85 tests covering
    Decimal helpers, status counts, win/loss stats, profit factor,
    max drawdown (three-way peak), Sharpe + Sortino, strategy
    breakdown, A/B testing, correlation-warning (injected annotator),
    readiness, snapshot retention, full orchestrator via
    `calculate_metrics` + `take_daily_snapshot`. 99% line+branch.
  * Round-58 pin (`test_correlation_warning_resolves_option_underlying`)
    updated to grep both files — update_scorecard.py for the
    injected annotator import, scorecard_core.py for the
    `_underlying`/`_sector` grouping logic.
  * **Coverage 51.04% → ~53%; CI floor 45 → 48.** Floor leaves
    headroom above local measurements for CI-environment drift.
  * Test count 1392 → **1484** (3 deselected unchanged).

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

### pt.7 DONE — screener_core + scorecard_core both extracted + tested

**Both cores shipped:**
  * `screener_core.py` (7 pure fns, extracted #119, 98% coverage via
    62 tests in `tests/test_round61_pt7_screener_core.py`)
  * `scorecard_core.py` (17 pure fns, extracted in the pt.7 follow-up,
    99% coverage via 85 tests in
    `tests/test_round61_pt7_scorecard_core.py`)

`update_dashboard.py` and `update_scorecard.py` stay in `omit` list
(both still serve as subprocess entry points); both import from the
respective core modules via thin compat wrappers that inject
production dependencies (`now_et`, `constants.SECTOR_MAP`,
`position_sector.annotate_sector`).

**Coverage impact:** 51.04% → ~53%. CI floor ratcheted 45 → 48.
Test count 1392 → 1484.

**For the next agent — how to extend this pattern:** if you need to
test previously-omitted subprocess logic, mirror the pt.7 approach:
extract pure math into a new `*_core.py` module, pass external deps
as callable parameters, have the caller wrap the core function and
inject production dependencies. The caller stays in `omit` (it's
still subprocess-driven), the core module is NOT omitted, and
pytest-cov sees the math. Behavior stays identical because the
wrapper is a 1:1 passthrough.

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

### Picking up pt.8 / pt.9 (checklist for next session)

1. `git pull --ff-only origin main`
2. `cat CLAUDE.md` (this section first — it's the current plan)
3. `cat CHANGELOG.md` (skim last 3 rounds)
4. Confirm tests pass: `MASTER_ENCRYPTION_KEY=$(python3 -c 'print("a"*64)') pytest tests/ --deselect tests/test_dashboard_data.py::test_trading_session_is_computed_live_not_from_stale_json --deselect tests/test_auth.py::test_password_strength_rejects_weak --deselect tests/test_audit_round12_scheduler_latent.py::test_ruff_clean_on_real_bug_rules -q` — expect **1484 passing, 3 deselected**
5. Decide next step. Options:
   * **pt.8** (Vitest for dashboard JS, ~6000 LOC currently invisible
     to pytest-cov — adds JS coverage, pushes total past ~78%)
   * **pt.9** (deeper `cloud_scheduler.py` — still ~31%; pt.6 hit the
     HTTP surface but left 60+ scheduler fns behind)
   * Source-level refactors of other subprocess-driven files (e.g.
     `capital_check.py`) using the same `*_core.py` extract pattern.

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

### Post-61 pt.21 (consistency audit + constants single-source)
- `constants.STRATEGY_NAMES`, `CLOSED_STATUSES`, `ACTIVE_STATUSES`,
  `STRATEGY_FILE_PREFIXES` are the SINGLE source of truth. Every
  consumer MUST import from there. Adding a new strategy = add the
  name to the constants frozenset and every downstream reader
  (dashboard badge, scorecard bucket, orphan-scanner, wheel monitor)
  picks it up automatically.
- `error_recovery.migrate_legacy_short_sell_option_files` MUST run
  every error_recovery invocation BEFORE the orphan-scan loop — the
  migrated wheel file needs to be in place when the orphan scanner
  checks `strategy_symbol_map` for the underlying. Marking the
  legacy file `status: "migrated"` (not `closed`) is deliberate —
  both `_mark_auto_deployed` (server.py) and
  `list_strategy_files` (error_recovery.py) treat "migrated" the same
  as closed so the legacy file stops claiming the symbol.
- `audit_core.run_audit` is pure — no side effects, no file writes,
  no network. Takes an in-memory snapshot (positions, orders,
  strategy_files, journal, scorecard) and returns a findings list.
  Unit-test it in isolation; the `/api/audit` endpoint is just the
  HTTP glue.
- `audit_core._parse_strategy_filename` MUST try longest-prefix
  match against `STRATEGY_FILE_PREFIXES` before falling back to
  `stem.partition("_")`. A naive `rpartition("_")` splits
  `short_sell_SOXL` into `short_sell` / `SOXL` correctly, but would
  break on any multi-word strategy that has an underscore in the
  symbol. Longest-match is safer.

### Post-61 pt.18 (professional stepped trailing stop)
- `cloud_scheduler._compute_stepped_stop(entry, extreme, default_trail,
  is_short)` is the SINGLE source of truth for what the stop should
  be at any given profit level. Both `process_strategy_file` (longs)
  AND `process_short_strategy` (shorts) MUST call it — do not inline
  the tier table.
- Tier boundaries are `0%`, `+5%`, `+10%`, `+20%`. Changing these
  changes every open position's exit plan. Product-level decision,
  not a refactor.
- Tier 2 MUST return `entry` (not a trail-derived value) so the
  break-even lock is EXACT. Rounding a derived tier-2 value would
  leave a few cents below entry and defeat the no-loss guarantee.
- Tier 2 notification fires ONCE per position via
  `state["break_even_triggered"]` guard. Re-firing on every 60s
  monitor tick would spam the user's notification feed.
- Opt-out: `rules.stepped_trail=false` reverts to flat trail. This
  is intentional — some strategies may test alternative trail logic
  without globally disabling the tier system.
- `_compute_stepped_stop` MUST produce monotonically-increasing
  stops for longs (monotonically-decreasing for shorts) across all
  profit levels. The unit test
  `test_stops_monotonically_increase_with_profit_long` pins this —
  a tier-boundary edit that violates it gives back protection.

### Post-61 pt.17 (adaptive orphan stops + OCC option support)
- `error_recovery.create_orphan_strategy` and Check-2 "Missing
  Stop-Loss" MUST use `max(entry*1.10, current*1.05)` for shorts /
  `min(entry*0.90, current*0.95)` for longs. Without this, an
  already-underwater position gets an Alpaca-rejected stop.
- OCC option orphan symbols MUST route to `wheel_<UNDERLYING>.json`
  with an appropriate stage (`stage_1_put_active` for short put,
  `stage_2_call_active` for covered call), NOT to
  `short_sell_<OCC>.json`. The equity short-sell monitor doesn't
  understand contracts vs shares.
- Existing `wheel_<UNDERLYING>.json` files MUST NOT be overwritten
  by orphan adoption — the wheel monitor owns that state.
- `_occ_parse(sym)` is the canonical OCC parser for the orphan
  path. Returns `{underlying, expiration, right, strike}`. Return
  `None` on non-OCC input.

### Post-61 pt.15/16 (autonomous orphan adoption)
- `cloud_scheduler.run_orphan_adoption(user)` is the per-user entry
  point. Both the scheduled 10-min tick and the on-demand dashboard
  button MUST call it (not duplicate the subprocess invocation).
- `error_recovery.list_strategy_files` MUST filter the same
  closed-status set as `server._mark_auto_deployed`: `closed`,
  `stopped`, `cancelled`, `canceled`, `exited`, `filled_and_closed`.
  Case-insensitive. If the two lists drift, the dashboard will
  disagree with the adoption logic about what's MANUAL.

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
