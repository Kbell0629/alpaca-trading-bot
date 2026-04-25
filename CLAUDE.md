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

## Current session state (2026-04-25 — round 61 pt.10-51 SHIPPED)

**Pt.51 in flight:** test + UI polish for pt.49/pt.50 features.
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
