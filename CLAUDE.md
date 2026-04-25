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
   — expect ~**3060+ passing, 3 deselected** after pt.76 (baseline grew from
   1802 at pt.32 through pt.46 +59, pt.47 +69, pt.48 +31, pt.49 +87, pt.50 +22,
   pt.51 +39, pt.52-57 polish, pt.58-69 batches +280, pt.70-76 +160 — incl.
   pt.74 +60 UI tests, pt.75 +10, pt.76 +4).
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

## Current session state (2026-04-25 — round 61 pt.10-76 SHIPPED)

**Latest merged batch (pt.65 → pt.76):** twelve accuracy + reliability +
UX PRs. The 8-item production-readiness sprint, the SOXL short-cover
bug (fixed three different ways: pt.50 → pt.53 → pt.69 → pt.75), and
the round of UX polish + auth-form cleanup.

**Pt.76 (PR #203) — signup form cleanup.** User-reported that the
Invite Code section showed the label twice (section header +
redundant `<label>` on the input) and the help text was a long
sentence about admin links and `SIGNUP_INVITE_CODE`. Fix:
  * Visible label = the section header. The `<label
    for="invite_code">` is preserved (pt.8 a11y contract requires
    the explicit association) but its inner text is wrapped in
    `<span class="pt76-sr-only">` so it doesn't render on screen.
  * Tightened help text to one sentence: "Only required if an
    admin sent you a single-use link."
  * Sentence-cased the title to match pt.74's section pattern.
  * Refreshed placeholder.
+4 source-pin tests in
`tests/test_round61_pt76_signup_invite_cleanup.py`.

**Pt.75 (PR #202) — nav DOM-order match + SOXL cancel-scan
reliability fix.** Two user-reported issues:
  1. **Nav tabs jumped around when clicked.** Header nav was in
     logical-grouping order, not page DOM order — clicking
     "Readiness" jumped DOWN past Positions/Analytics/Trades, then
     "Backtest" jumped BACK UP. Fixed by re-ordering the nav
     array to match actual rendered DOM: overview → picks →
     strategies → readiness → positions → analytics → trades →
     screener → [shorts] → [tax] → backtest → scheduler →
     heatmap → comparison → settings.
  2. **SOXL "insufficient qty" close kept failing despite pt.69's
     retry-with-backoff.** Root cause: cancel scan's URL had
     `?status=open&symbols=SOXL` and Alpaca's orders endpoint
     **silently excludes some "accepted"-status orders when
     filtered server-side by symbol**. Empty list → cancelled==0
     → pt.69's retry loop never fired. Fix: drop `?symbols=` URL
     filter; fetch all open orders + trust the existing client-
     side filter. Bumped limit from 50 → 200.
+10 source-pin tests in
`tests/test_round61_pt75_nav_order_cancel_scan.py`.

**Pt.74 (PR #201) — UI/UX "Pro" polish, 10-item batch.**
Comprehensive UI upgrade addressing every item in the
professional-feel audit. Additive — no DOM restructure; all new
behaviours opt-in via CSS classes / body state / localStorage:
  1. **Info hierarchy:** `.panel-tertiary` class + "· advanced"
     hint after section headers; scheduler + perf-attribution
     tagged.
  2. **Focus Mode toggle:** ◎ FOCUS pill in header, hides
     decorative panels; persisted in `localStorage["pt74_focusMode"]`.
  3. **Reduced motion:** `prefers-reduced-motion` honored on
     auth pages too; new `.pt74-soft-pulse` class for gentle 4s
     pulses.
  4. **Header cluster grouping:** CSS-only — focus pill = status
     anchor with right separator; force-deploy + voice = trading
     cluster; help + refresh = utilities cluster with left
     separator.
  5. **Skeleton loaders + freshness chips:** Analytics + Trades
     panels now render skeleton placeholders (instead of plain
     "Loading..." text) and track `_lastFetchedAt` /
     `_lastFetchError` for pt74RenderFreshnessChip.
  6. **Auth UX polish:** brand lockup + trust copy ("Paper-
     trading by default", "AES-256-GCM", "no auto-trades"),
     sentence-case headings, password Show/Hide toggle on every
     password/secret field.
  7. **Risk badge** at Kill Switch + Close Position action sites
     surfacing the paper/live mode.
  8. **Typography ramp** aligned across auth templates (SF Pro
     Display, 0.6px letter-spacing on micro-labels).
  9. **Sticky table headers** via `.pt74-sticky-table` (positions
     + orders).
  10. **High-stakes copy pass:** "EMERGENCY KILL SWITCH" →
      "Emergency kill switch", Close-modal subtitle now describes
      the action.
+23 vitest unit tests + 37 Python source-pin tests.

**Pt.72 (PR #199) — pre-trade quote abort + live-mode gate + per-
symbol cooldown.** Three production-readiness items:
  1. **Pre-trade quote-snapshot abort.** New
     `pre_trade_check.py` (pure module). Right before placing a
     deploy order, fetches a fresh `latestQuote`. Aborts if live
     spread > 0.5% or price drifted > 1% from screener-time price.
     Fail-open on any fetch error (microstructure insurance, not
     a hard prerequisite). Wired into `run_auto_deployer` just
     before the `smart_orders` / market-fallback POST.
  2. **Live-mode promotion gate.** New `live_mode_gate.py`
     (pure module). Paper validation ends ~May 15; right now
     flipping live is a manual eyeball click. New
     `check_live_mode_readiness(journal, scorecard,
     audit_findings)` AUTO-blocks the toggle until ≥30 closed
     trades + ≥45% win rate + ≥0.5 sharpe + ≤15% DD + 0 HIGH
     audit findings. Override with `override_readiness=true`.
     Wired into `handlers/auth_mixin.handle_toggle_live_mode`
     after the existing readiness ≥80 check.
  3. **Per-symbol 24h cooldown after stop-out.** New
     `symbol_cooldown.py` (pure module). Prevents re-deploying
     the same symbol the next morning if its 30-min screener
     score recovers. Hooks both ends:
       * `record_trade_close` calls `record_stop_out(state,
         symbol, exit_reason)` for `stop_hit` / `stop_loss` /
         `trailing_stop` / `bearish_news` / `dead_money` (NOT
         `target_hit` — that's a good close).
       * Auto-deployer pick loop calls `is_on_cooldown` and skips
         with `"cooldown_after_<reason>: Xh remaining"`.
     State persisted in `_last_runs["symbol_cooldown"]`. Default 24h.
+47 tests in
`tests/test_round61_pt72_quote_abort_live_gate_cooldown.py`.

**Pt.71 (PR #198) — news exits + ADV cap + drawdown taper.** Three
high-impact accuracy improvements:
  1. **Position-level news exit triggers.** New
     `news_exit_monitor.py` (pure module). `monitor_strategies` now
     sweeps open LONG positions for fresh bearish news (-10 close,
     -6 warn) using the same vocabulary as the pre-market scan
     (`news_scanner.score_news_article`). 10-min per-symbol
     cooldown via `_last_runs`. Sets a `news_exit_close_<user>_
     <sym>` flag the per-strategy close path picks up and market-
     sells with `exit_reason="bearish_news"`. Shorts skipped
     (they benefit from bearish news).
  2. **Liquidity-aware ADV cap.** New `adv_size_multiplier(price,
     qty, adv_dollar, cap_pct=5.0)` in `position_sizing.py`. Caps
     a position at 5% of 20-day average dollar volume so the bot
     doesn't become its own bad fill. Floor at 0.05× to avoid
     silently dropping a deploy.
  3. **Per-strategy drawdown taper.** New
     `compute_strategy_recent_drawdown` + `drawdown_size_multiplier`
     in `position_sizing.py`. 30-day per-strategy peak-to-trough
     drawdown lookup; linear taper from 1.0× at 0% DD to 0.5× at
     10% DD. Wired into `compute_full_size` between confluence
     and ADV cap.
+40 tests in `tests/test_round61_pt71_news_adv_drawdown.py`.

**Pt.69 (PR #196 — in flight) — retry-with-backoff after cancel.**
Fixes the user-reported `"insufficient qty available for order
(requested: 29, available: 0)"` on SOXL short close. Root cause:
Alpaca's order cancellation is async — the broker takes 250-1000ms
to release the reserved qty after the cancel ACK. Pt.53's immediate
retry hit the same error. Pt.69 polls DELETE up to 4 times with
exponential backoff (0.3s, 0.6s, 1.0s, 1.5s — total ~3.4s).
Different errors mid-loop surface immediately; same error after
exhaustion surfaces with a `"try again in a moment"` hint.
+7 source-pin tests in
`tests/test_round61_pt69_close_retry_backoff.py`.

**Pt.68 (PR #195) — data wiring + spread filter + 4h MTF gate.**
Three follow-ups closing accuracy gaps from earlier batches:
  1. **Sector ETF fetcher.** Pt.66's `apply_sector_momentum_filter`
     was imported but never fired in production because nothing
     produced `sector_returns`. New `fetch_sector_returns(fetch_bars_fn,
     lookback_days=20)` in `sector_momentum.py` builds
     `{sector_name: pct_return}` from XLE/XLF/XLK/XLV/XLY/XLP/XLI/
     XLB/XLU/XLRE/XLC daily bars over a 20-day window. Wired into
     `update_dashboard.py` to run before the filter.
  2. **VWAP retest data feed.** Pt.66 added `detect_vwap_retest`
     but the call site only passed `price+vwap`, never `prev_price`
     or `session_low` — so the retest pattern never fired. Pt.68
     extracts both from the bars already fetched in
     `cloud_scheduler.py` (min over today's 5-min bar lows for
     session_low, last 5-min bar close for prev_price) and passes
     them in. Cross-up retest patterns now ALLOW the entry instead
     of treating it as a chase. Zero extra API calls.
  3. **Bid-ask spread filter.** New pure module `spread_filter.py`:
     `compute_spread_pct`, `is_spread_tight`, `apply_spread_filter`.
     Rejects picks where `(ask - bid) / mid > 0.5%` — a "high-
     volume" small-cap with a 5% spread is a 5%-on-entry tax.
     Reads `latestQuote.bp/ap` from snapshots already fetched.
  4. **4h MTF breakout confirmation.** A 30-min screener can flag
     a "breakout" that on the 4-hour chart is just a mid-session
     blip getting rejected at 4h resistance. New
     `apply_mtf_breakout_confirmation` in `multi_timeframe.py`
     HARD-rejects Breakout picks at-or-below their 4h 20-bar high.
     New `fetch_intraday_bars` + `fetch_intraday_bars_for_symbols`
     in `update_dashboard.py` for the `4Hour` timeframe.
+42 tests in `tests/test_round61_pt68_data_wiring.py`.

**Pt.67 (PR #194) — mobile responsiveness + settled-funds coverage.**
Items 7-8 of the 8-item production-readiness sprint:
  1. **Analytics Hub mobile responsiveness.** New
     `@media (max-width: 600px)` block in `dashboard.html` targeting
     `#analyticsPanel` + `#tradesPanel`: equity row migrated from
     `2fr 1fr` → `auto-fit,minmax(280px,1fr)`, per-strategy grid
     forced single-column on phones, KPI grid floor 150px → 110px
     (3 cards/row vs 2-with-orphan), `factor-card` padding 14px →
     10px, equity SVG `max-height: 200px !important` to keep the
     chart from dominating the viewport.
  2. **Settled-funds coverage on user-initiated closes.** New
     `_record_close_to_settled_funds(handler, symbol, qty, price)`
     helper bridges the four `actions_mixin.py` close paths (RTH
     `DELETE /positions`, xh_close limit, MOO queue, retry-after-
     cancel, partial-sell) into the `settled_funds` ledger. Pt.51
     wired the auto-deployer's `record_trade_close` but the user
     "Close" button bypassed the ledger — a cash-account user could
     re-deploy proceeds same-day → Good Faith Violation. Skips short
     covers (qty<0) since buy-to-cover doesn't generate proceeds.
+16 tests in `tests/test_round61_pt67_mobile_settled_funds.py`.
Initial CI failure traced to fresh `auth` re-import without
`MASTER_ENCRYPTION_KEY` after a sibling http_harness test popped
auth from sys.modules; fixed with `monkeypatch.setenv` in the
two affected tests.

**Pt.66 (PR #193) — four-item accuracy refinement batch.**
Items 3-6 of the 8-item production-readiness sprint:
  1. **Sector-momentum filter.** New `sector_momentum.py` (pure
     module): blocks long deploys in sectors trending DOWN >10%
     MoM. Direction-aware — short_sell picks pass through.
  2. **VWAP retest detection.** Refines pt.61's gate. New
     `detect_vwap_retest(price, vwap, prev_price, session_low)`
     identifies the high-quality cross-up pattern (price spent the
     morning UNDER VWAP, just crossed above on volume). Returns
     `is_retest=True` and ALLOWS the entry even at offsets that
     would otherwise block.
  3. **Volume-confirmed gap penalty.** `apply_gap_penalty` now
     skips the 15% score demotion when `relative_volume >= 2.0×`
     — gap is institutional confirmation, not a thin pump. Tags
     `_gap_volume_confirmed`.
  4. **Post-event momentum lean-in.** New `post_event_momentum.py`:
     captures the 1-3 day post-FOMC/CPI/NFP/PCE drift edge that
     pt.48's "raise the threshold ON event day" missed. Lowers
     the effective score threshold by 1.05-1.20×.
+56 tests in `tests/test_round61_pt66_accuracy_refinements.py`.

**Pt.65 (PR #192) — wire pt.63 + pt.64 modules into runtime.**
Items 1-2 of the 8-item sprint:
  1. **Live-data divergence sweep** wired into `monitor_strategies`.
     Sweeps every open position once per RTH cycle, notifies on
     >2% bot-vs-live divergence. Dedupe via `_last_runs` keyed
     `divergence_alert_<user>_<symbol>`. Best-effort fail-open.
  2. **Risk-parity weights** surfaced in `build_analytics_view` via
     new `_safe_risk_parity_weights(journal)` helper. Read-only,
     dashboard can render σ-aware allocations without each call site
     re-implementing the math.
+15 tests in `tests/test_round61_pt65_wire_divergence_riskparity.py`.

**Pt.57 (PR #184) — production-readiness polish:**
four-in-one polish batch making the bot more
production-ready:
  1. **Cryptography-sandbox skip pattern.** `http_harness` fixture
     in `tests/conftest.py` now `pytest.skip`s with a clear reason
     when AESGCM construction raises `BaseException` (covers
     pyo3 PanicException from missing cffi backend). Local sandbox
     runs no longer drown in 259 ERROR rows — they all SKIP cleanly.
     CI keeps running them since cryptography installs from
     requirements.txt there.
  2. **Pipeline-backtest "simulate P&L" toggle.** Pt.49 + pt.56
     built the counterfactual-P&L feature in the library + endpoint
     but the dashboard had no way to opt in. Added a checkbox next
     to the "▶ Run" button; when checked, request includes
     `simulate_outcomes=true` and the panel renders a fifth row
     with trade count / win rate / total P&L / avg / expectancy.
     Endpoint fetches bars on-the-fly via `backtest_data.fetch_bars_for_symbols`.
     Timeout bumped 30s → 90s when simulating.
  3. **Score-to-outcome partial-data view.** Below the 30-trade
     full threshold the panel previously just said "insufficient
     sample". Pt.57 surfaces a status bar with three stages
     (INSUFFICIENT < 10, PRELIMINARY 10-29, TRUSTWORTHY ≥30) +
     fill % so users can see how close they are to having enough
     data. Legacy untracked count surfaced too.
  4. **Tier-aware risk e2e test.** Pt.38 added TIER_STRATEGY_PARAMS,
     pt.47 wired it through the resolver. Pt.57 adds 10 e2e tests
     locking in the claim "$500 cash account gets a tighter stop
     than a $100k margin account" — detects tier from account dict,
     resolves through `strategy_params.resolve_strategy_param`,
     asserts the tier value wins over the fallback. Plus 8 more
     source-pin + UI tests for the pipeline-backtest toggle and
     score-outcome partial view.
+18 tests in `tests/test_round61_pt57_polish.py`.

**Pt.51-56 landed (PRs #178/#179/#180/#181/#182/#183):** see
CHANGELOG.md for full notes. Highlights: Analytics Hub
score-health pill (pt.51), routing regression tests using lazy-
import pattern (pt.52/54), auto-cancel pending sell orders before
Close to fix the SOXL "insufficient qty" bug at market open
(pt.53), `alpaca_mock` conftest fixture v2 (pt.55), pipeline-
backtest end-to-end UI with picks_history.json snapshotting +
`/api/pipeline-backtest` endpoint + dashboard panel (pt.56).

**Pt.51 (legacy notes — superseded by pt.55):** test + UI polish for pt.49/pt.50 features.
  1. **Pt.50 routing regression tests.** 27 tests in
     `tests/test_round61_pt51_closed_market_routing.py` covering
     `_market_session` (RTH/premarket/afterhours/overnight/holiday/
     boundary), `_position_qty` (long/short/missing/error),
     `_latest_price` (success/error/data-endpoint pin), and
     `_build_xh_close_order` (long/short/zero-price/rounding).
     Fills the gap shipped in pt.50.
  2. **Score-health pill in Analytics Hub.** `build_analytics_view`
     already returns `score_health` from pt.49; pt.51 renders it
     as a status pill at the top of the Score-to-Outcome panel.
     Green when healthy, orange "warning" when one monotonic
     flag fails, red "degraded" when both fail.
  3. **`alpaca_mock` conftest fixture.** New `AlpacaMock` test
     double in `tests/conftest.py`. Patches every
     `cloud_scheduler` / `scheduler_api` Alpaca helper
     (`user_api_get`/`post`/`delete`/`patch`) so scheduler
     functions can be tested deterministically without network.
     12 harness tests in
     `tests/test_round61_pt51_scheduler_harness.py` covering
     `record_trade_open`/`close` (idempotence + journal write),
     `check_correlation_allowed` (sector cap), and the fixture
     itself. First step toward the 80% coverage goal — tested
     paths previously needed live Alpaca.
  4. **Ruff `noqa` warning fix.** Renamed the project-internal
     silent-except marker from `# noqa: silent-except` to
     `# allow-silent-except` so ruff stops emitting "Invalid
     `# noqa` directive" warnings (it interprets any `noqa:`
     comment as a ruff-rule list). Pt.22 ratchet test +
     scanner updated; four call sites in
     `handlers/actions_mixin.py` migrated.
+39 tests total (27 routing + 12 harness).

**Pt.50 landed (PR #177):** audit + dashboard polish round.
  1. **Weekend-aware scorecard freshness audit.**
     `audit_core.run_audit` no longer fires `STALE_SCORECARD` on
     normal weekends. Replaced the flat 48-hour threshold with
     `_trading_closes_between(start, end)` that counts expected
     daily_close runs (Mon-Fri excluding NYSE holidays from
     `_US_MARKET_HOLIDAYS` 2026/2027). Stale = ≥2 missed closes.
     Saturday morning audits no longer false-positive on the
     normal Friday→Monday gap.
  2. **Analytics + Trades load resilience.** Added 30s
     `AbortController` timeouts to `refreshAnalyticsPanel` and
     `refreshTradesPanel` so the panels surface a proper error
     instead of hanging on "Loading..." when the backend is
     mid-deploy. Added `_analyticsLastPayload` / `_tradesLastPayload`
     caches: `renderDashboard`'s 30s tick now re-renders the
     panels from cache after rebuilding the container, so the
     panels stop flashing back to "Loading..." between fetches.
  3. **In-app guide deep links.** Added `SECTION_GUIDES` entries
     for `analytics` (📊 Analytics Hub — pt.46/pt.47/pt.49 all
     covered), `trades` (Trades panel — pt.36/pt.43), plus four
     reference entries for features that don't have a dashboard
     section yet but power the bot: `position-sizing` (pt.49
     Kelly + correlation), `event-calendar` (pt.48), `walk-forward`
     (pt.47/pt.48), `pipeline-backtest` (pt.49). Clicking the (i)
     icon now opens the matching section, not the full README.
+22 tests in `tests/test_round61_pt50_audit_weekend_aware.py`
covering `_is_trading_day` (weekday/weekend/holiday/datetime/
invalid), `_trading_closes_between` (zero/single/weekend gap/
3-day holiday/Wed→Wed/naive+tz/invalid), and full-audit
integration tests with monkey-patched `now_et()`.

**Pt.49 landed (PR #176):** three-in-one position-sizing + active monitoring
+ pipeline-validation batch:
  1. **Fractional-Kelly + correlation-aware sizing.** New
     `position_sizing.py`. Functions:
     `kelly_fraction(win_rate, avg_win, avg_loss)` (half-Kelly,
     capped at 25%); `compute_strategy_edge(journal, strategy)`
     (per-strategy realised stats from the journal);
     `kelly_size_multiplier(edge)` (maps Kelly to a [0.5×, 2.0×]
     multiplier with 5% Kelly = 1.0×); `count_correlated_positions`
     + `correlation_size_multiplier` (each same-sector held
     position cuts size by 0.5×, floored at 0.25×);
     `compute_full_size(...)` end-to-end wrapper. Wired into
     auto-deployer (long + short paths) BEFORE drawdown_mult so
     all legacy caps still apply on top.
  2. **Score-degradation alerting.** New
     `analytics_core.check_score_degradation(journal, min_trades=30)`
     reads pt.47's score-outcome buckets and returns a
     {`degraded`, `warning`, `headline`, `detail`} dict.
     Degraded = both monotonic flags False over ≥30 trades.
     `cloud_scheduler.run_score_health_check` runs daily at
     4:35 PM ET, notifies the user once on transition INTO
     degraded (not every day) and once on recovery, persists
     state in `_last_runs` to prevent spam. Surfaced as
     `build_analytics_view["score_health"]` for the dashboard.
  3. **Pipeline-aware backtest.** New `pipeline_backtest.py`
     replays a historical picks sequence through every
     deploy-side gate (chase_block / volatility_block /
     already_held / below_50ma / above_50ma /
     breakout_unconfirmed / sector_cap / event_day /
     min_score). Reports `total_picks`, `would_deploy`,
     `blocked_by_reason`, `block_rate`, optional
     `counterfactual` P&L via `backtest_core._simulate_symbol`.
     Answers "are the gates blocking profitable picks?" — the
     question pt.45's chips surface visually but never
     quantified.
+34 tests in `tests/test_round61_pt49_kelly_sizing.py`
+15 tests in `tests/test_round61_pt49_score_health.py`
+38 tests in `tests/test_round61_pt49_pipeline_backtest.py`

**Pt.48 landed (PR #175):** three-in-one accuracy batch making the pt.47
infrastructure ACTIVE in production:
  1. **Walk-forward in self-learning loop.**
     `learn_backtest.run_self_learning` now accepts
     `validation_mode="walk_forward"`. Each variant evaluated on
     out-of-sample test slices via the pt.47
     `run_walk_forward_backtest` harness; `select_best_variant`
     gains `max_overfit_ratio` filter. `cloud_scheduler.run_weekly_learning`
     wires this on (mode="walk_forward", overfit cap 1.5).
     Variants must beat baseline on the OOS test slice AND have
     train/test ratio < 1.5 to be promoted. Closes the loop on
     pt.47's harness — without this wiring, the harness was
     decorative.
  2. **Realistic slippage + commission in self-learning.**
     `run_self_learning` accepts `slippage_bps` +
     `commission_per_trade`; production weekly hook now passes
     10 bps + $1 so simulated expectancy reflects real fill
     friction. Learned params no longer over-promise vs live.
  3. **Event-day gate.** New `event_calendar.py` (pure module)
     encodes 2026/2027 FOMC, CPI, NFP (first-Friday-rule), PCE
     (last-Friday-rule). `is_high_impact_event_day(date)` returns
     `(bool, label)`; `event_score_multiplier(label)` returns
     2.0/1.5/1.5/1.3 for FOMC/CPI/NFP/PCE. Auto-deployer raises
     long-side score gate (`50 × multiplier`) and short-side
     `min_short_score × multiplier` so the bot can't blunder
     into FOMC at 1pm with the same risk as a quiet Tuesday.
+31 tests in `tests/test_round61_pt48_active_self_learning.py`
covering walk-forward thread-through, overfit-ratio rejection,
backwards-compat zero-friction defaults, event calendar (FOMC/
CPI/NFP detection, ISO/datetime/string inputs, priority order),
and source-pin tests on the auto-deployer wiring.

**Pt.47 landed (PR #174):** five-in-one accuracy batch closing the deferred
wiring + adding the missing meta-validation:
  1. **Pt.44b — learned_params consumer.** New
     `strategy_params.py` resolver with precedence
     `learned > tier > fallback`. Wired into the auto-deployer's
     rules-construction (long + short paths) so the next-tick
     screener's `learned_params.json` (written by pt.44 weekly)
     actually gets read back at deploy time. Single-call helper
     `resolve_rules_dict(strategy=..., base_rules=..., tier_cfg=...,
     learned_params=...)` returns a copy with stop_loss_pct /
     profit_target_pct / max_hold_days resolved.
  2. **Pt.38b — TIER_STRATEGY_PARAMS consumer.** Same resolver
     (above) layers TIER_STRATEGY_PARAMS in BELOW learned_params
     but ABOVE the legacy hardcoded defaults. A $500 cash-micro
     account no longer takes the same 12% stops as a $100k
     margin-standard one — each tier+strategy combo gets its own
     value via `portfolio_calibration.get_strategy_param`.
  3. **Walk-forward backtest validation.** New
     `backtest_core.run_walk_forward_backtest` slides a (train_days,
     test_days) window forward by step_days; picks the best param
     variant on each train slice and evaluates on the immediately-
     following test slice. Reports per-fold + aggregate test-window
     metrics + an `overfit_ratio` (train_expectancy /
     test_expectancy). >1.5 ⇒ self-learning is overfitting.
  4. **Slippage + commission in backtest.** `_simulate_symbol`
     now accepts `slippage_bps` (applied to entry + exit prices,
     working against you on both sides) and `commission_per_trade`
     (subtracted from pnl per round-trip). Default 0 → bit-
     identical to pt.37 behaviour. Production should pass realistic
     values (e.g. 10 bps + $1) so backtest expectancy doesn't
     over-promise.
  5. **Score-to-outcome correlation panel.** New
     `analytics_core.compute_score_outcome` bins closed trades by
     their `_screener_score` (now embedded at deploy time) into
     5 quintile buckets and reports win-rate / expectancy per
     bucket + monotonic-winrate / monotonic-expectancy flags. New
     panel in the Analytics Hub renders the buckets + green/red
     pills. The meta-validation pt.46 was missing: did higher-
     scored picks actually win more often?
+22 tests in `tests/test_round61_pt47_strategy_params.py`
+28 tests in `tests/test_round61_pt47_walk_forward_slippage.py`
+19 tests in `tests/test_round61_pt47_score_outcome.py`

**Pt.46 landed (PR #173):** Analytics Hub — unified read-only dashboard
that consolidates KPIs, equity curve, drawdown, distributions,
top symbols, best/worst trades, exit-reason analysis, per-strategy
breakdown, and screener filter summary into a single tab. User
asked for "a really robust and good looking and highly functional
analytics dashboard… all the analytics are all over which are
fine for their area but I need a analytics hub". Three-piece
architecture mirroring pt.7/34/36/37:
  1. **`analytics_core.py`** — pure aggregator. Functions:
     `compute_headline_kpis`, `compute_equity_curve`,
     `compute_drawdown_curve`, `compute_pnl_by_period`,
     `compute_pnl_by_symbol`, `compute_pnl_by_exit_reason`,
     `compute_hold_time_distribution`, `compute_pnl_distribution`,
     `compute_best_worst_trades`, `compute_filter_summary`,
     `compute_strategy_breakdown`, end-to-end `build_analytics_view`.
     OCC option symbols resolve to underlying for symbol grouping.
     Validation tracking from 2026-04-15 paper-trading start.
  2. **`/api/analytics` endpoint** — `handle_analytics_view` in
     `handlers/actions_mixin.py`. POST-only, read-only, JSON
     out. Loads journal + scorecard + dashboard_data picks +
     live Alpaca account, builds full view via
     `build_analytics_view`. Auth-gated (401 if no session).
  3. **Analytics dashboard tab** in `templates/dashboard.html`:
     📊 Analytics nav button between Strategies and Positions.
     8 KPI cards (Total P&L, Win Rate, Expectancy, Avg Win,
     Max DD, Sharpe, Avg Hold, Validation). Equity Value chart
     (inline SVG with polyline + polygon fill — no Chart.js
     dep). P&L by Period card. Per-strategy breakdown grid.
     P&L Distribution + Hold Time Distribution histograms.
     Top Symbols by Impact + P&L by Exit Reason bars. Best/Worst
     trade lists. Screener filter summary. Auto-prefetches
     during init() so the tab is hot.
+39 unit tests in `test_round61_pt46_analytics_core.py`
+20 endpoint/dashboard pin tests in
`test_round61_pt46_analytics_endpoint.py` (4 cryptography-gated,
pass on CI).

**Pt.45 landed (PR #172):** filter-reason tags on screener picks.
User asked: "why are POET (458) / AMD (172) / INTC (155) not in the
Top 3 even though they outscore MRVL/TXN/NVDA?" Answer: they were
filtered by deploy-time gates (don't-chase, already-held, sector-cap,
trend filter, breakout-confirmation) but the screener table didn't
surface the WHY. Pt.45 bridges every filter into the unified
`filter_reasons` list and renders them as orange chips in the
screener table.
  * **`update_dashboard.py`**: builds `held_symbols` from
    `positions_list`, appends `already_held` to filter_reasons for
    matching symbols. Bridges pt.39 `_filtered_by_trend` →
    `below_50ma`/`above_50ma` and pt.40 `_breakout_unconfirmed` →
    `breakout_unconfirmed`. Existing chase_block + volatility_block
    tags retained unchanged.
  * **`templates/dashboard.html`**: screener table chip-render block
    consumes `p.filter_reasons` and emits orange chips with tooltip
    explaining the block. Each known reason has a short label
    (🚫 Held / ⤓ <50MA / ⤒ >50MA / ? 1-day / 🏃 chase / ⚡ vol);
    unknown codes fall back to the raw string.
+9 tests in `tests/test_round61_pt45_filter_reason_tags.py`.
