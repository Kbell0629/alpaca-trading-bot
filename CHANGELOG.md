# Changelog

All notable changes to this project are documented here. This file lives alongside the user-facing [README](README.md) so the guide stays clean and the release history is easy to audit.

Format: each entry is grouped by **round** (development cycle) and tagged with the **date** the work shipped. A "round" roughly corresponds to a focused batch of PRs — security sweeps, audit responses, UX polish, or feature additions. Every PR number cited below is merged to `main` and deployed.

The project is currently in **paper-trading validation** (started 2026-04-15, targeting ~30 days). Live-trading is code-complete but gated behind the validation window.

---

## 🆕 Round-61 pt.86 — live-mode dry-run / shadow mode (+18 tests)

**Date:** 2026-04-26

Paper validation ends ~May 15. After that the user can flip the
live-mode toggle (with the pt.72 promotion gate as a check), and
real money starts trading. Pt.86 adds an opt-in **shadow mode**
that runs the auto-deployer through every gate but RECORDS the
deploy intent into a per-user shadow log INSTEAD of POSTing the
order — letting the user "watch what live would do" without
committing capital.

**New pure module `shadow_mode.py`:**
* `is_shadow_mode_active(user, guardrails, env=os.environ)` —
  resolution order: per-user `guardrails.live_shadow_mode` →
  `LIVE_SHADOW_MODE` env var → False
* `record_shadow_event(user, action, **fields)` — appends a
  structured event (timestamp, action, symbol, strategy, qty,
  price, score, sector) to `users/<id>/shadow_log.json`. Capped
  at 500 entries (oldest-first prune)
* `get_shadow_log(user, limit=50)` — newest-first
* `summarize_shadow_log(events)` — counts by action / symbol /
  strategy for the dashboard

Wired into `cloud_scheduler.run_auto_deployer` just before the
order POST: when shadow mode is active, records the
`would_deploy` event and `continue`s the loop. Fail-safe: if the
shadow check raises, falls through to the real POST so we don't
accidentally suppress trading.

Pure-module discipline: no top-level imports of
`cloud_scheduler` / `auth` / `server`. Pinned by test.

+18 tests in `tests/test_round61_pt86_shadow_mode.py`.

---

## 🆕 Round-61 pt.84 — wire slippage_tracker into the fill path (+8 tests)

**Date:** 2026-04-26

Pt.80 shipped the slippage_tracker module + the Analytics Hub
`slippage_summary` key, but no call site populated the journal's
slippage fields, so the panel returned an empty aggregate. Pt.84
closes that loop.

**Changes:**
* Auto-deployer journal append now records
  `entry_expected_price` (= the screener-time price) on every
  new open entry so the close path has the "expected" anchor.
* `record_trade_close` accepts new `entry_filled_price` and
  `exit_filled_price` kwargs. When supplied alongside the
  stored expected price, computes signed slippage_bps via
  `slippage_tracker.compute_slippage_bps` and writes the four
  slippage fields back onto the closed entry.
* Target-hit close path passes the real broker fill prices
  through (entry = position avg_entry_price; exit = close-
  order's filled_avg_price).

**Backwards-compat:** old call sites without the new kwargs still
work — slippage fields are simply omitted from those entries
(and the aggregator silently skips legacy entries).

Once trades close through the wired path, pt.80's
`slippage_summary` panel transitions from "preliminary, no data"
to a real verdict.

+8 tests in `tests/test_round61_pt84_wire_slippage_fields.py`.

---

## 🆕 Round-61 pt.82 — per-trade entry-rationale audit (+25 tests)

**Date:** 2026-04-25

Every closed trade has `exit_reason` but not `entry_rationale`.
Pt.82 records a structured rationale dict at deploy time so
post-mortems and the future meta-learning loop can correlate
WHICH entry conditions actually predict winners.

**New module `entry_rationale.py`:**
* `build_entry_rationale(pick, sizing_info, regime,
  sector_returns)` — 15-key dict (score, rs_score, sector,
  sector_strength, news_sentiment, vwap_offset_pct, atr_pct,
  regime, kelly_mult, correlation_mult, drawdown_mult, adv_mult,
  confluence_count, filter_reasons, headline)
* `format_rationale(rat)` — single-line headline like
  `"score=485 RS=+8 sector=Technology(strong) news=bullish
  vwap=+0.3% conf=4/5"`
* `aggregate_winners_vs_losers(journal)` — buckets closed trades
  by P&L sign and reports mean rationale fields per bucket +
  signed deltas

Wired into `cloud_scheduler.run_auto_deployer`'s journal append.
Best-effort — failures fall through to `None`; legacy pre-pt.82
journal entries silently skipped by the aggregator. Pure-module
discipline pinned by test (no top-level imports of
cloud_scheduler / auth / server).

+25 tests in `tests/test_round61_pt82_entry_rationale.py`.

---

## 🆕 Round-61 pt.81 — xh_close / overnight POST gets cancel+retry too (+7 tests)

**Date:** 2026-04-25

User-reported with screenshot at 8:34 PM ET on a Saturday: SOXL
"insufficient qty available" close kept failing despite pt.50 →
pt.53 → pt.69 → pt.75. Root cause: pt.50 routes after-hours and
overnight closes through a POST to `/orders` (xh_close limit or
MOO BUY-to-cover for shorts), NOT through DELETE `/positions`.
Pt.69's cancel-and-retry recovery only covered the DELETE branch
— the POST branch surfaced the bare error.

**Fix:** extend the same recovery to the POST branch:
1. POST → "insufficient qty"
2. `_cancel_pending_sell_orders` (drops the pending BUY-stop)
3. Retry POST up to 4 times with backoff (0.3 / 0.6 / 1.0 / 1.5s)
4. Success → fall through to settled-funds bridge + success
5. Exhausted → "try again in a moment"
6. Different error mid-retry → surface immediately

Now both paths have the same robust cancel-and-retry recovery.

+7 tests in `tests/test_round61_pt81_xh_close_cancel_retry.py`.

---

## 🆕 Round-61 pt.80 — realized vs expected slippage tracking (+30 tests)

**Date:** 2026-04-25

Pt.47's backtest harness assumes 10 bps of slippage on entry +
exit. Without measuring live fills we can't tell whether the
backtest expectancy matches reality. Pt.80 closes the loop.

**New pure module `slippage_tracker.py`:**
* `compute_slippage_bps(expected, filled, side)` — signed bps
  where POSITIVE = adverse. Buy: filled > expected → positive.
  Sell: filled < expected → positive (received less).
* `aggregate_realized_slippage(journal)` — walks closed trades
  with the four slippage fields populated, returns aggregate +
  per-strategy breakdown + dollar cost.
* `compare_to_assumption(agg, assumed_bps=10.0)` — verdict
  state: `ok` (≤ assumption) / `warn` (≤1.5×) / `alert` (>1.5×) /
  `preliminary` (<20 trades).
* `annotate_close_with_slippage(close_kwargs, ...)` — helper
  for record_trade_close callers. Writes 4 fields onto
  `extra` dict + computes signed bps.

**New trade-journal fields** (additive — legacy entries skipped
silently): `entry_expected_price`, `entry_filled_price`,
`entry_slippage_bps`; same trio for exit side.

`analytics_core.build_analytics_view` returns a new
`slippage_summary` key: `{aggregate, verdict}`.

+30 tests in `tests/test_round61_pt80_realized_slippage.py`.

---

## 🆕 Round-61 pt.79 — nav-tab order: Tax Harvest position fix (+4 tests)

**Date:** 2026-04-25

User-reported after pt.75: clicking "Tax Harvest" still scrolled
BACKWARDS up the page. Root cause: `taxHtml` is rendered INSIDE
the positions section (between the orders table and `</section>`)
so it lives BEFORE Analytics in DOM order — but pt.75's nav had
it AFTER Screener.

**Fix:** move the Tax Harvest nav button to between Positions and
Analytics. Short Sells stays where it was (its `shortHtml` is
rendered after the screener section close, before Backtest).

New nav order matches actual DOM:
```
overview → picks → strategies → readiness → positions →
[tax] → analytics → trades → screener → [shorts] →
backtest → scheduler → heatmap → comparison → settings
```

+4 source-pin tests including a strict full-order check.

---

## 🆕 Round-61 pt.78 — end-to-end close-flow integration test (+9 tests)

**Date:** 2026-04-25

The SOXL "insufficient qty available" close bug got fixed three
different ways (pt.50 → pt.53 → pt.69 → pt.75) and each round
found another corner. Pt.78 ships a single integration test that
exercises the full close path — POST `/api/close-position` →
cancel scan → retry-with-backoff → settled-funds ledger write —
with a self-contained Alpaca mock. If a future PR re-introduces
the `?symbols=` URL filter, removes the cancel-after-failure
step, or shortens the retry-backoff schedule below what Alpaca
needs, the test fails immediately.

9 scenarios: clean RTH close, full SOXL bug recovery, no-pending-
orders enrichment, all-retries-exhausted, different-error-mid-
retry, pt.75 URL pin, pre-market xh_close routing, short-cover
skipping settled-funds, long-close populating settled-funds.

Self-contained `_AlpacaStub` — no http_harness fixture, no
cryptography needed. <0.5s for all 9 tests.

+9 tests in `tests/test_round61_pt78_close_flow_integration.py`.

---

## 🆕 Round-61 pt.76 — signup form: dedupe Invite Code label + tighten help text (+4 tests)

**Date:** 2026-04-25

User-reported via screenshot: the Invite Code section showed the
label twice (section header + redundant `<label>` on the input)
and the help text was a long sentence about admin links and
`SIGNUP_INVITE_CODE`.

**Fix:**
* Visible label = the section header. The `<label
  for="invite_code">` is preserved for the pt.8 a11y contract,
  but its inner text is wrapped in a `<span class="pt76-sr-only">`
  so screen readers / click-to-focus still get the association
  while the visible UI stays clean.
* New `.pt76-sr-only` utility uses the standard sr-only pattern
  (1px clipped, `position: absolute`, `white-space: nowrap`).
* Tightened help text from "Required if an admin sent you a
  single-use invite (auto-filled from the URL) or if the
  deployment has SIGNUP_INVITE_CODE set." → "Only required if an
  admin sent you a single-use link."
* Sentence-cased the section title ("Invite Code" → "Invite
  code") to match pt.74's section pattern.
* Refreshed placeholder to "Leave blank if you don't have one".
* Auto-fill from `?invite=` URL param still works.

+4 source-pin tests in
`tests/test_round61_pt76_signup_invite_cleanup.py`. CI initially
failed because the pt.8 a11y test pinned the exact substring
`<label for="invite_code">`; fixed by keeping the opener and
hiding the inner text via the sr-only span.

---

## 🆕 Round-61 pt.75 — nav DOM-order match + SOXL cancel-scan reliability fix (+10 tests)

**Date:** 2026-04-25

Two user-reported issues fixed in one PR.

**1. Nav tabs jumped around when clicked.** Header nav was in
logical-grouping order, not page DOM order. Clicking "Readiness"
jumped DOWN past Positions/Analytics/Trades, then "Backtest"
jumped BACK UP past several sections. Re-ordered the nav array to
match actual rendered DOM:
```
overview → picks → strategies → readiness → positions →
analytics → trades → screener → [shorts] → [tax] →
backtest → scheduler → heatmap → comparison → settings
```
Conditional sections (shorts, tax) only show in nav when their
content actually rendered.

**2. SOXL "insufficient qty" close kept failing despite pt.69's
retry-with-backoff.** Toast showed the bare original error with
neither pt.69's `(cancelled X pending order(s))` enrichment NOR
the exhaustion message.

Root cause: cancel scan's URL had `?status=open&symbols=SOXL` and
**Alpaca's orders endpoint silently excludes some "accepted"-
status orders when filtered server-side by symbol**. Empty list
→ `cancelled == 0` → pt.69's retry loop never fired.

Fix: drop the `?symbols=` URL filter; fetch ALL open orders
(`?status=open&limit=200`) and trust the existing client-side
filter (`o.get("symbol") == symbol`). Slightly more bytes over
the wire but reliably catches every order — including the
"accepted"-status BUY-stops Alpaca was hiding.

+10 source-pin tests in
`tests/test_round61_pt75_nav_order_cancel_scan.py`.

---

## 🆕 Round-61 pt.74 — UI/UX "Pro" polish, 10-item batch (+60 tests)

**Date:** 2026-04-25

Comprehensive UI upgrade addressing every item in the
professional-feel audit. Additive — no DOM restructure; all new
behaviours opt-in via CSS classes / body state / localStorage.

| # | Item | Where |
|---|---|---|
| 1 | Info hierarchy: `.panel-tertiary` class + "· advanced" hint | dashboard.html |
| 2 | Focus Mode toggle (◎ FOCUS pill, persisted) | dashboard.html |
| 3 | Reduced-motion respected on auth pages too; new `.pt74-soft-pulse` class | all 3 templates |
| 4 | Header cluster grouping (status / trading / utilities) — CSS only | dashboard.html |
| 5 | Skeleton loaders + freshness chips for Analytics + Trades | dashboard.html |
| 6 | Auth polish: brand lockup, trust copy, password Show/Hide toggle | login.html + signup.html |
| 7 | Risk badge (paper/live) at Kill Switch + Close Position action sites | dashboard.html |
| 8 | Typography ramp aligned across auth templates | login.html + signup.html |
| 9 | Sticky table headers via `.pt74-sticky-table` (positions + orders) | dashboard.html |
| 10 | High-stakes copy pass: sentence-case headings, descriptive subtitles | dashboard.html |

**New helpers (window-exposed for testing + reuse):**
* `pt74RenderSkeleton({rows, cards})`
* `pt74FormatFreshness(ts, errorState)` → `{state, text}`
* `pt74RenderFreshnessChip(ts, errorState)` → full chip HTML
* `pt74RenderRiskBadge({live, detail, label})`
* `pt74WirePasswordToggles(rootEl)` — idempotent
* `toggleFocusMode()` — body class + localStorage

+23 vitest unit tests in `tests/js/pt74-uiux.test.js` (every
helper's branches, focus-mode persistence, password toggle
idempotence) + 37 Python source-pin tests in
`tests/test_round61_pt74_uiux_polish.py`. All 415 existing JS
tests still green (1 closePositionModal title test updated for
the sentence-case copy change).

---

## 🆕 Round-61 pt.72 — pre-trade quote abort + live-mode gate + per-symbol cooldown (+47 tests)

**Date:** 2026-04-25

Three production-readiness items + docs refresh covering pt.71.

* **Pre-trade quote-snapshot abort** (new `pre_trade_check.py`).
  Right before placing a deploy order, fetches a fresh
  `latestQuote`. Aborts if (a) live spread > 0.5% (microstructure
  shifted from screener time) or (b) price drifted > 1% from
  screener-time price (something broke in the last few seconds).
  Fail-open on any fetch error — this is microstructure insurance,
  not a hard prerequisite. Wired into `run_auto_deployer` just
  before `smart_orders` / market-fallback POST.
* **Live-mode promotion gate** (new `live_mode_gate.py`). Paper
  validation ends ~2026-05-15; right now flipping live is a manual
  eyeball click. New `check_live_mode_readiness(journal,
  scorecard, audit_findings)` returns `{ready, blockers,
  warnings, summary, metrics}` and AUTO-blocks the live toggle
  until: ≥30 closed trades, win rate ≥ 45%, sharpe ≥ 0.5, max
  drawdown ≤ 15%, 0 HIGH-severity audit findings. Wired into
  `handlers/auth_mixin.handle_toggle_live_mode` AFTER the existing
  readiness ≥ 80 check (defense-in-depth). Override with
  `override_readiness=true`.
* **Per-symbol 24h cooldown after stop-out** (new
  `symbol_cooldown.py`). Real-world example: SOXL stops out at
  -8% Tuesday afternoon. Wednesday morning the 30-min screener
  still has SOXL in the top 5. Bot deploys again. SOXL drops
  another 5%. Death by a thousand re-entries on a falling knife.
  Hooks both ends of the loop:
    * `record_trade_close` calls `symbol_cooldown.record_stop_out`
      for cooldown-triggering exit reasons (`stop_hit`,
      `stop_loss`, `trailing_stop`, `bearish_news`, `dead_money`
      — NOT `target_hit` since those are good closes)
    * Auto-deployer pick loop calls `is_on_cooldown` and skips
      with `"cooldown_after_<reason>: Xh remaining"`
  State persisted in `_last_runs["symbol_cooldown"]`. Default 24h.

Pure-module discipline: all three new modules avoid top-level
imports of `cloud_scheduler`, `auth`, `server`. Pinned by tests.

+47 tests in
`tests/test_round61_pt72_quote_abort_live_gate_cooldown.py`.

---

## 🆕 Round-61 pt.71 — news exits + ADV cap + drawdown taper (+40 tests)

**Date:** 2026-04-25

Three high-impact accuracy improvements building on the pt.65-69
sprint.

* **Position-level news exit triggers.** New pure module
  `news_exit_monitor.py`. `monitor_strategies` now sweeps open
  LONG positions for fresh bearish news (default -10 close, -6
  warn). Uses the same scoring vocabulary as the pre-market
  `news_scanner.score_news_article` so the pattern set is
  consistent. 10-min per-symbol cooldown via caller-managed
  state. Sets a `news_exit_close_<user>_<sym>` flag in
  `_last_runs`; per-strategy close path picks up the flag and
  market-sells with `exit_reason="bearish_news"`. Shorts skipped
  (bearish news benefits them). API:
  `aggregate_symbol_news_score`, `check_position_news`,
  `explain_close`.
* **Liquidity-aware ADV cap.** New `adv_size_multiplier(price,
  qty, adv_dollar, cap_pct=5.0)` in `position_sizing.py`. Caps
  positions at 5% of 20-day average dollar volume so the bot
  doesn't become its own bad fill on a thin name. Floor at 0.05×
  to avoid silently dropping a deploy. Wired into
  `compute_full_size` AFTER Kelly + correlation + confluence +
  drawdown. Auto-deployer call site computes `adv_dollar` from
  `pick.daily_volume × pick.price`.
* **Per-strategy drawdown taper.** New
  `compute_strategy_recent_drawdown(journal, strategy,
  lookback_days=30)` walks last 30 days of CLOSED trades for
  `strategy` and computes max peak-to-trough equity drawdown.
  New `drawdown_size_multiplier(drawdown_pct,
  halving_threshold_pct=5, floor=0.5)` linearly tapers from 1.0×
  at 0% DD to 0.5× at 10% DD (floored). Defaults: 5% DD →
  0.75×, 10% DD → 0.5×.

+40 tests in `tests/test_round61_pt71_news_adv_drawdown.py`.

---

## 🆕 Round-61 pt.69 — retry-with-backoff after cancel for SOXL-style closes (+7 tests)

**Date:** 2026-04-25

User-reported (with screenshot): closing a SOXL short returned
`422 "insufficient qty available for order (requested: 29,
available: 0)"` even though pt.53's
`_cancel_pending_sell_orders` had successfully cancelled the
pending BUY-stop cover order.

**Root cause:** Alpaca's order cancellation is asynchronous —
the broker returns a 200 cancel ACK immediately but takes
~250-1000ms to release the reserved qty / buying-power. Pt.53's
immediate retry hit the same error.

**Fix:** poll DELETE up to 4 times with exponential backoff
(0.3s, 0.6s, 1.0s, 1.5s — total ~3.4s) so the retry catches
the release window. If a DIFFERENT error surfaces mid-loop,
surface it immediately rather than burning the retry budget.
Surface a clear `"try again in a moment"` hint if all retries
exhaust with the same insufficient-qty error.

Also: success message updated from `"sell order(s)"` →
`"pending order(s)"` since pt.53's helper cancels both buy and
sell sides (BUY-to-cover for shorts).

+7 tests in `tests/test_round61_pt69_close_retry_backoff.py`.

---

## 🆕 Round-61 pt.68 — data wiring + spread filter + 4h MTF gate (+42 tests)

**Date:** 2026-04-25

Three follow-ups closing accuracy gaps from earlier batches:

* **1a — Sector ETF fetcher.** Pt.66 shipped
  `apply_sector_momentum_filter(picks, sector_returns)` but never
  produced `sector_returns`, so the filter was a no-op. New
  `fetch_sector_returns(fetch_bars_fn, lookback_days=20)` in
  `sector_momentum.py` builds `{sector_name: pct_return}` from
  XLE/XLF/XLK/XLV/XLY/XLP/XLI/XLB/XLU/XLRE/XLC daily bars over a
  20-day window. Wired into `update_dashboard.py`.
* **1b — VWAP retest data feed.** Pt.66 added
  `detect_vwap_retest` but the call site only passed `price+vwap`,
  never `prev_price` or `session_low`. Pt.68 extracts both from the
  bars already fetched in `cloud_scheduler.py` (min over today's
  5-min bar lows, last 5-min bar close) and passes them into
  `evaluate_vwap_gate`. Cross-up retest patterns now ALLOW the
  entry instead of treating it as a chase. Zero extra API calls.
* **2 — Bid-ask spread filter.** New pure module `spread_filter.py`:
  `compute_spread_pct`, `is_spread_tight`, `apply_spread_filter`.
  Rejects picks where `(ask - bid) / mid > 0.5%`. Reads
  `latestQuote.bp/ap` from snapshots already fetched. Bridges
  `_wide_spread` → `"wide_spread"` filter_reason.
* **3 — 4h MTF breakout confirmation.** New
  `apply_mtf_breakout_confirmation` in `multi_timeframe.py`
  HARD-rejects Breakout picks at-or-below their 4h 20-bar high.
  Cuts false-breakout rate from 30-min screener mid-session blips
  that get rejected at 4h resistance. New `fetch_intraday_bars` +
  `fetch_intraday_bars_for_symbols` for the `4Hour` timeframe.

+42 tests in `tests/test_round61_pt68_data_wiring.py`. Pt.61
VWAP-block source pins bumped 2500→5000 chars to accommodate the
inlined retest data extraction.

---

## 🆕 Round-61 pt.67 — Analytics Hub mobile responsiveness + settled-funds coverage on user-initiated closes (+16 tests)

**Date:** 2026-04-25

Items 7-8 of the 8-item production-readiness sprint:

**Item 7 — Analytics Hub mobile responsiveness.** Pt.46 shipped the
Analytics Hub with desktop-only inline grid styles. On 360-393px
phones the equity row used `2fr 1fr` (chart got crammed), per-
strategy cards used `auto-fit, minmax(220px, 1fr)` (slight
horizontal scroll), 8-card KPI grid at `minmax(150px, 1fr)` left
an orphan in row 3.

Fix: a `@media (max-width: 600px)` block targeting `#analyticsPanel`
+ `#tradesPanel`:
  * Equity row migrated to `auto-fit,minmax(280px,1fr)`
  * Per-strategy grid forced to single-column on phones
  * KPI grid floor 150px → 110px (3 cards/row instead of 2 + orphan)
  * `factor-card` padding 14px → 10px
  * Equity SVG `max-height: 200px !important`

**Item 8 — Settled-funds coverage on user-initiated closes.**
Pt.51 wired the auto-deployer's `record_trade_close` to update
`settled_funds`, but the four `actions_mixin.py` close paths
(RTH `DELETE /positions`, xh_close limit, MOO queue, insufficient-
qty retry, partial-sell) talked directly to Alpaca and never
touched the ledger. A cash-account user could click Close, re-
deploy proceeds same-day → Good Faith Violation.

Fix: new `_record_close_to_settled_funds(handler, symbol, qty,
price)` helper. Skips short covers (qty<0), zero/None proceeds,
missing user_id. Best-effort: never raises. Wired into all 4
success paths.

+16 tests in `tests/test_round61_pt67_mobile_settled_funds.py`.
Initial CI failure (`RuntimeError: MASTER_ENCRYPTION_KEY env var
is required`) traced to fresh `auth` re-import after a sibling
http_harness test popped auth from sys.modules. Fixed with
`monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "a"*64)` in the two
affected tests.

---

## 🆕 Round-61 pt.66 — four-item accuracy refinement batch (+56 tests)

**Date:** 2026-04-25

Items 3-6 of the 8-item production-readiness sprint:

* **Sector-momentum filter** (`sector_momentum.py`): blocks long
  deploys in sectors trending DOWN >10% MoM. Direction-aware —
  `short_sell` picks pass through (downtrend is good for shorts).
  API: `compute_pct_return`, `build_sector_returns`,
  `is_sector_in_downtrend`, `apply_sector_momentum_filter`.
* **VWAP retest pattern** (`vwap_gate.py`): new
  `detect_vwap_retest(price, vwap, prev_price, session_low)`
  identifies the high-quality cross-up pattern. `evaluate_vwap_gate`
  now returns `is_retest=True` and ALLOWS the entry even at offsets
  that would otherwise block, with reason `vwap_retest_cross_up`.
* **Volume-confirmed gap penalty** (`screener_core.py`):
  `apply_gap_penalty` now skips the 15% score demotion when
  `relative_volume >= 2.0×` — gap is institutional confirmation,
  not a thin pump. Tags `_gap_volume_confirmed`.
* **Post-event momentum lean-in** (`post_event_momentum.py`):
  captures the documented 1-3 day post-FOMC / CPI / NFP / PCE
  drift edge. Boosts: FOMC 1.20×/1.10×/1.05× for days 1-3, CPI
  1.15×/1.05× for days 1-2, NFP 1.10× for day 1, PCE 1.05× for
  day 1. Wired into `cloud_scheduler.run_auto_deployer` to lower
  the score threshold's effective bar.

+56 tests (44 unit + 6 wiring/source-pin + 6 from pt.66 internals).

---

## 🆕 Round-61 pt.65 — wire live_data_monitor + risk_parity into runtime (+15 tests)

**Date:** 2026-04-25

Items 1-2 of the 8-item production-readiness sprint. Both pt.63
(`live_data_monitor`) and pt.64 (`risk_parity`) shipped as
library-only modules with no runtime callers. Pt.65 wires them
into actual code paths.

* **Live-data divergence sweep.** `monitor_strategies` in
  `cloud_scheduler.py` now sweeps every open position once per
  cycle (RTH only), notifies on >2% bot-vs-live divergence.
  Best-effort fail-open. Dedupe via `_last_runs` keyed
  `divergence_alert_<user>_<symbol>`.
* **Risk-parity weights in analytics.** `build_analytics_view`
  (`analytics_core.py`) surfaces `compute_risk_parity_weights(journal)`
  as a new top-level key via `_safe_risk_parity_weights`. Read-
  only — wired so the dashboard and downstream consumers can
  render σ-aware allocations without each call site re-
  implementing the math.

+15 tests in `tests/test_round61_pt65_wire_divergence_riskparity.py`.

---

## 🆕 Round-61 pt.64 — daily backups + dead-money notification + risk-parity + tier-5 trailing (+24 tests)

**Date:** 2026-04-25

Four polish items completing the production-readiness sprint:

* **A. Daily backups already cover per-user state.** `backup.INCLUDES`
  has `users/` recursively — that captures `learned_params.json`,
  `picks_history.json`, journals, scorecards. Pt.64 adds 3 source-pin
  tests so a future refactor can't silently drop them.
* **B. Dead-money exit notification template.** New
  `notification_templates.dead_money_exit` returns rich subject + body
  with rationale ("the bot closed this even though it didn't hit a
  stop or target — the trade just stopped moving"). Wired into
  `cloud_scheduler.process_strategy_file`'s dead-money block (replaces
  the plain notify_user from pt.59).
* **C. Risk-parity weight allocator.** New `risk_parity.py` (pure
  module). `compute_risk_parity_weights(journal, *, strategies)`
  returns `{strategy: weight}` summing to 1.0, weighted inversely by
  σ(P&L). Read-only library for now; future PR can consume it.
* **D. Aggressive Tier-5 trailing-stop at +30% profit.** Adds a 5th
  tier to `_compute_stepped_stop` — at +30% profit the trail tightens
  from 4% (Tier 4) to 3% (Tier 5). Caps the typical 30% giveback that
  erased half of +30 winners over the following week.

+24 tests in `tests/test_round61_pt64_polish_batch.py`.

---

## 🆕 Round-61 pt.63 — live-data divergence monitor (+22 tests)

**Date:** 2026-04-25

New `live_data_monitor.py` pure module — detects when the bot's
last-seen price for a symbol diverges materially from Alpaca's
most-recent latest_trade. Severity tiers: `ok` (Δ < 2%), `warn`
(2–4%), `alert` (≥ 4%).

API: `compute_divergence_pct`, `classify_divergence`,
`check_position_divergence(positions, latest_trade_fn,
threshold_pct)`. +22 tests. Wiring into the monitor cycle ships in a
follow-up.

---

## 🆕 Round-61 pt.62 — scheduler coverage push round 2 (+31 tests)

**Date:** 2026-04-25

Second round of harness tests on `cloud_scheduler.py`. Targets
`_compute_stepped_stop` (12 tests covering the 4-tier trailing
math), `strategy_file_lock`, `user_strategies_dir`, `load_json` /
`save_json`, `_within_opening_bell_congestion`, `log()` ring-buffer,
`_flatten_all_user`, `now_et` tz-awareness, `_heartbeat_tick`.

+31 tests in `tests/test_round61_pt62_coverage_push_2.py`.

---

## 🆕 Round-61 pt.61 — wire VWAP gate + earnings calendar into auto-deployer (+18 tests)

**Date:** 2026-04-25

Pt.59 built `vwap_gate.py` and `earnings_calendar_static.py` as pure
modules but didn't wire either into the deploy path. Pt.61 plugs them
in:

1. **VWAP gate** runs after chase_block / volatility_block, ONLY for
   breakout picks. Fetches today's 5-min bars from Alpaca's data
   endpoint, computes VWAP via `indicators.vwap`, blocks the deploy
   if `price > VWAP × 1.005` (0.5% tolerance). Fails OPEN on
   data-fetch error.
2. **Earnings calendar** runs after the VWAP gate. Calls
   `earnings_calendar_static.next_earnings_date(symbol, max_days_ahead=3)`;
   if the static calendar reports earnings within 3 days, blocks the
   deploy. PEAD exempt.

+18 tests in `tests/test_round61_pt61_wire_vwap_earnings.py`.

---

## 🆕 Round-61 pt.51 — test polish + score-health UI + alpaca_mock harness (+39 tests)

**Date:** 2026-04-25

User asked: "fix everything we need to get this app/bot high
performing and accurate and reliable". Pt.51 closes the regression-
test gap from pt.50, surfaces the score_health data we computed but
never showed, lays the groundwork for the 80% coverage push, and
silences a ruff warning.

### 1. Pt.50 routing regression tests

Pt.50 shipped the closed-market order routing without tests.
Pt.51 adds 27 regression tests covering every branch in
`handlers/actions_mixin.py`:
* `_market_session(handler)` — RTH / premarket / afterhours /
  overnight classification across boundary times (4:00 AM, 9:30
  AM, 4:00 PM, 8:00 PM), weekend handling, fail-open on `/clock`
  probe error.
* `_position_qty(handler, symbol)` — long, short (negative),
  missing, error.
* `_latest_price(handler, symbol)` — success, no trade, error,
  + a pin that the helper hits the data endpoint (not the
  trading endpoint).
* `_build_xh_close_order(symbol, qty, side, price)` — long
  (-1%), short (+1%), zero/negative/invalid price → None,
  rounding to two decimals.

A pre-market regression on Close would now fail CI before it
reaches users.

### 2. Score-health pill in Analytics Hub

Pt.49 added `build_analytics_view["score_health"]` and a daily
4:35 PM check, but the dashboard didn't render the result. Pt.51
adds a status pill at the top of the Score-to-Outcome panel:

* **Green** "Score health: OK" — both monotonic flags pass.
* **Orange** "Score health: win rate not monotonic" — one flag
  failed.
* **Red** "⚠ Screener scoring appears uncorrelated to outcome" —
  both flags fail with ≥30 closed trades. Notification was already
  firing daily; now visible inline too.

### 3. `alpaca_mock` conftest fixture

New `AlpacaMock` test double in `tests/conftest.py` and matching
`alpaca_mock` pytest fixture. Patches every
`cloud_scheduler` / `scheduler_api` Alpaca helper
(`user_api_get`, `user_api_post`, `user_api_delete`,
`user_api_patch`) so scheduler functions can be invoked against
deterministic responses without network.

API:
```python
def test_deploy_skips_already_held(alpaca_mock):
    alpaca_mock.register("GET", "/positions", [
        {"symbol": "AAPL", "qty": "10", "market_value": "1500"}
    ])
    # ... call cloud_scheduler.run_auto_deployer(user) ...
    alpaca_mock.assert_called("GET", "/positions")
```

12 harness tests added in
`tests/test_round61_pt51_scheduler_harness.py` covering
`record_trade_open` / `record_trade_close` (idempotence + journal
write), `check_correlation_allowed` (sector cap blocks 3rd
position), and the fixture itself. First step toward the 80%
coverage target — tested paths previously needed live Alpaca.

### 4. Ruff `noqa` warning fix

The project-internal silent-except marker was
`# noqa: silent-except`, which ruff parsed as a malformed rule
suppression and warned on every run ("Invalid `# noqa` directive").
Pt.51 renames it to `# allow-silent-except` (no `noqa:` prefix)
across:
* `tests/test_round61_pt22_no_silent_except.py` — scanner
  + docs + sample-test snippets
* `handlers/actions_mixin.py` — four call sites in the pt.50
  routing helpers

Ratchet test still passes; ruff now silent.

### Tests

+27 in `tests/test_round61_pt51_closed_market_routing.py`
+12 in `tests/test_round61_pt51_scheduler_harness.py`
(plus pt.22 silent-except scanner + sample tests updated for
the marker rename).

### Deferred for follow-up

* Pipeline-backtest dashboard panel (data flow needs daily
  picks-history snapshotting first — currently no source for
  the harness to read).
* Close-path unification (RTH still uses DELETE
  `/positions/{symbol}` while extended hours uses POST
  `/orders`; keeping both for now since DELETE has built-in
  handling for options/short covers).

---

## 🆕 Round-61 pt.57 — production-readiness polish: cryptography skip + pipeline-backtest toggle + score-outcome partial view + tier e2e (+18 tests)

**Date:** 2026-04-25

User asked to "fix all of these and ensure we don't lose our spot
… get this app to be production ready and ready for users". Pt.57
is the first batch — four polish items that all needed shipping
to call the codebase production-ready.

### 1. Cryptography-sandbox skip pattern

The `http_harness` conftest fixture now `pytest.skip`s cleanly
when AESGCM construction raises (pyo3 PanicException from a
missing `_cffi_backend`). Local sandbox runs were producing
259 ERROR rows; they all SKIP cleanly now. CI keeps running them
since the wheel installs from requirements.txt fine. Catches
`BaseException` (not `Exception`) because PanicException doesn't
inherit from Exception.

### 2. Pipeline-backtest "simulate P&L" toggle

Pt.49 built the counterfactual P&L logic; pt.56 wired the
endpoint; pt.57 surfaces it. Checkbox next to the "▶ Run" button:
when checked, the request body includes `simulate_outcomes=true`
and the handler fetches bars via
`backtest_data.fetch_bars_for_symbols` (cap 30 symbols / 90 days)
before calling `pipeline_backtest.run_pipeline_backtest`. The
panel renders a counterfactual row with trade count / win rate /
total P&L / avg / expectancy. Frontend timeout bumped 30s → 90s
when the toggle is on.

### 3. Score-to-outcome partial-data view

Below the 30-trade full threshold, the Analytics Hub's
Score-to-Outcome panel previously just said "insufficient
sample". Pt.57 surfaces a colour-coded progress bar with three
stages:
* `INSUFFICIENT` (text-dim, < 10 tracked)
* `PRELIMINARY` (orange, 10–29 tracked)
* `TRUSTWORTHY` (green, ≥ 30 tracked)
plus the fill % toward the 30-trade threshold and a legacy-count
indicator (closed trades that don't have an embedded
`_screener_score` because they predate pt.47).

### 4. Tier-aware risk-cap end-to-end test

Pt.38 added `portfolio_calibration.TIER_STRATEGY_PARAMS`; pt.47
wired it through `strategy_params.resolve_strategy_param`. Pt.57
adds the e2e validation that was missing:

* `cash_micro` tier table has tighter stops than `margin_whale`
  for at least one strategy + never wider stops on any strategy.
* `pc.detect_tier` correctly returns `cash_micro` for a $500
  account and `margin_whale` for a $1M margin account.
* End-to-end: detect tier from account dict → resolve
  `stop_loss_pct` through the resolver → cash account always gets
  ≤ margin account's stop. Locks in the user-facing claim from
  pt.38.

### Tests

+18 in `tests/test_round61_pt57_polish.py`:
* 10 tier + resolver tests (`test_tier_*`,
  `test_resolver_picks_tier_value_over_fallback`,
  `test_resolve_rules_dict_round_trip_with_tier`,
  `test_e2e_500_cash_to_resolved_stop_is_tighter_than_100k_margin`)
* 4 pipeline-backtest UI tests (toggle presence, simulate flag
  threading, counterfactual rendering, endpoint handling)
* 3 score-outcome partial-view tests (stage labels, tracked-count
  binding, legacy count surfaced)
* 1 source-pin on the conftest crypto-skip pattern

---

## 🆕 Round-61 pt.56 — pipeline-backtest UI: picks history + endpoint + dashboard panel (+23 tests)

**Date:** 2026-04-25

Pt.49 built the `pipeline_backtest` module but had no data flow
or UI. Pt.56 closes the loop:

* `picks_history.py` — pure module that snapshots today's picks
  to per-user `picks_history.json` on every screener cycle.
  Append-only, atomic write via tempfile+rename, capped at 90
  days.
* `update_dashboard.py` hooks the snapshot after the main
  `dashboard_data.json` write.
* `/api/pipeline-backtest` POST endpoint returns total /
  would_deploy / blocked_by_reason / block_rate.
* Dashboard panel below the Analytics Hub with a "▶ Run" button
  + 3 KPI cards + blocked-by-reason histogram.

LOC ratchet on `server.py` bumped 3395 → 3400 for the new route.

+23 tests in `tests/test_round61_pt56_pipeline_backtest_ui.py`.

---

## 🆕 Round-61 pt.55 — alpaca_mock fixture v2 (+12 tests)

**Date:** 2026-04-25

Pt.51's first attempt at this same harness failed CI with exit
code 2. Pt.55 v2 uses the lazy-import pattern proven CI-stable
in pt.52: the fixture is a bare `_AlpacaMock` instance with no
module imports at fixture-creation time; tests do their own
`import cloud_scheduler as cs` inside the body and call
`monkeypatch.setattr(cs, "user_api_get", mock._do_get)`.

Coverage: register/match, call recording, default empty
response, last-registered wins, method isolation; plus
`record_trade_open`/`close` (idempotent), `check_correlation_allowed`
(sector cap blocks the 3rd same-sector position).

+12 tests in `tests/test_round61_pt55_alpaca_mock.py`.

---

## 🆕 Round-61 pt.54 — full routing-helper test coverage (+21 tests)

**Date:** 2026-04-25

Pt.52 established the lazy-import pattern is CI-stable. Pt.54
layered on the 21 tests that pt.51's monkeypatch-heavy file
couldn't ship.

Coverage:
* `_market_session` — RTH / premarket / afterhours / overnight
  classification across boundary times (4 AM, 9:30 AM, 4 PM,
  weekend), fail-open on `/clock` probe error.
* `_position_qty` — long, short (negative), missing, error.
* `_latest_price` — success, no trade, error, + a pin that the
  helper hits the data endpoint.
* `_market_is_closed` compatibility shim.

A pre-market regression on Close routing would now fail CI before
reaching users.

+21 tests in `tests/test_round61_pt54_routing_full.py`.

---

## 🆕 Round-61 pt.53 — auto-cancel pending sell orders before Close (+11 tests)

**Date:** 2026-04-25

User-reported: clicking Close on SOXL at 9:43 AM ET produced
"Close failed: insufficient qty available for order
(requested: 29, available: 0)" — even though the position was
29 shares with no recent sells.

Root cause: a pre-market MOO sell from pt.50 was still queued at
9:43 (Alpaca processes the open cross over several seconds).
Shares were earmarked for that queued order, so DELETE
`/positions` returned 422.

Fix: `handle_close_position` now traps "insufficient qty" /
"available: 0" errors, calls a new `_cancel_pending_sell_orders`
helper that lists open orders for the symbol via
`GET /orders?symbols=SOXL` and cancels each, then retries the
DELETE. If no pending orders are found, the error message points
to Open Orders so users know what to look for.

+11 tests in `tests/test_round61_pt53_cancel_before_close.py`.

---

## 🆕 Round-61 pt.52 — CI-stable minimal routing-test smoke (+7 tests)

**Date:** 2026-04-25

Diagnostic PR — pt.51 added 27 closed-market routing regression
tests but CI consistently failed with exit code 2 (collection
error) even though they passed locally. Pt.52 establishes the
CI-stable pattern: 7 minimal pure-function tests with lazy
`from handlers import actions_mixin` imports inside each test
body. Foundation for layering on the remaining 20 tests in
pt.54.

+7 tests in `tests/test_round61_pt52_routing_minimal.py`.

---

## 🆕 Round-61 pt.51 — score-health pill + ruff noqa fix

**Date:** 2026-04-25

Pt.51 ships the subset that's CI-stable:

1. Score-health pill in Analytics Hub — pt.49 added
   `build_analytics_view["score_health"]` but the dashboard never
   rendered it. Now a status pill at the top of the
   Score-to-Outcome panel — green/orange/red mapping to
   ok/warning/degraded states.

2. Ruff `noqa` warning fix — renamed the project-internal
   silent-except marker from `# noqa: silent-except` to
   `# allow-silent-except` so ruff stops emitting "Invalid
   `# noqa` directive" warnings.

Originally pt.51 also added an `alpaca_mock` conftest fixture +
27 closed-market routing regression tests, but both triggered an
exit-code-2 CI collection failure that didn't reproduce locally.
Reverted from this PR; routing tests came back in pt.52/pt.54
and the harness fixture in pt.55.

---

## 🆕 Round-61 pt.50 — audit weekend awareness + load resilience + guide deep-links (+22 tests)

**Date:** 2026-04-25

User-reported audit + dashboard issues:
  1. State Audit was flagging "MEDIUM · STALE_SCORECARD" every weekend
     because the flat 48h threshold trips on a normal Friday→Monday
     gap.
  2. Analytics Hub + Trades panels were stuck on "Loading..." (slow
     server / mid-deploy) and would also flash back to "Loading..."
     on every dashboard refresh tick.
  3. The in-app (i) info icons next to "📊 Analytics Hub" and "Trades"
     fell through to the full README instead of opening a focused
     section guide.

### 1. Weekend-aware scorecard freshness audit

Pt.21's audit fired any time `last_updated` was >48h old, false-
positive on every Monday morning. Pt.50 measures staleness in
TRADING DAYS, not wall-clock hours.

* New `audit_core._is_trading_day(d)` returns False for weekends
  AND for NYSE 2026/2027 holidays (encoded in
  `_US_MARKET_HOLIDAYS` — New Year, MLK, Presidents Day, Good
  Friday, Memorial Day, Juneteenth, Independence Day, Labor Day,
  Thanksgiving, Christmas).
* New `audit_core._trading_closes_between(start, end)` counts
  expected `daily_close` runs (16:05 ET on each trading day) that
  should have fired in the (start, end] window.
* Audit Check 7 now flags only when `missed_closes >= 2`. Friday
  4:05 PM → Monday 9 AM = 0 missed closes. Three-day Memorial
  Day weekend = 0 missed closes. Wed → Fri afternoon = 2 missed
  closes (Thu + Fri) → flag fires.
* Message format updated: `Scorecard last updated 64h ago (2
  expected daily_close runs missed). Trigger via Settings ->
  Force Daily Close.`

### 2. Analytics + Trades load resilience

Two related fixes for "panel won't load" reports:

* **30s timeout** via `AbortController` on both
  `refreshAnalyticsPanel` and `refreshTradesPanel`. A slow
  backend (Alpaca account fetch, mid-deploy) now surfaces a
  real error after 30 seconds instead of hanging on "Loading..."
  forever.
* **Cache last-good payload** in `_analyticsLastPayload` /
  `_tradesLastPayload`. The 30s `renderDashboard` tick rebuilds
  every section's HTML — without a cache, that wiped the
  Analytics + Trades panels back to the "Loading..." placeholder
  every cycle (the user-reported "loaded but goes back to
  loading" symptom). Post-paint hook now re-renders from cache
  so the panels stay populated between fetches.

### 3. In-app guide deep links

Added `SECTION_GUIDES` entries that the (i) buttons depend on:

* **`analytics`** — full Analytics Hub guide covering all 8 KPI
  cards, equity curve, P&L by Period, per-strategy breakdown,
  distributions, top symbols, exit reasons, best/worst trades,
  filter summary, and the pt.47 + pt.49 score-to-outcome panel.
* **`trades`** — Trades dashboard schema, filters, sort, and
  the pt.43 CSRF hotfix note.
* **`position-sizing`** — pt.49 Kelly + correlation explainer.
* **`event-calendar`** — pt.48 FOMC/CPI/NFP/PCE risk gate.
* **`walk-forward`** — pt.47/pt.48 walk-forward + slippage
  + commission methodology.
* **`pipeline-backtest`** — pt.49 shadow-deploy backtest API.

Clicking the (i) on Analytics Hub or Trades now opens a focused
modal with the matching section, NOT the full README at the top.

### Tests

+22 in `tests/test_round61_pt50_audit_weekend_aware.py`:
* `_is_trading_day`: weekday / weekend / NYSE holiday / datetime
  / invalid
* `_trading_closes_between`: zero/single/weekend-gap (the exact
  Fri-close → Mon-morning false positive) / 3-day holiday /
  Wed→Wed multi-day / tz-aware / invalid
* Full-audit integration with monkey-patched `now_et()`:
  silent after weekend, fires on 2 missed closes, silent for
  3-day holiday, message format includes missed-closes count.

Existing `pt.21` and `pt.22` audit tests updated to use 8-day
gaps so they always cross ≥5 trading-day closes regardless of
when the test runs.

---

## 🆕 Round-61 pt.49 — fractional-Kelly sizing + score-health alerting + pipeline-aware backtest (+87 tests)

**Date:** 2026-04-25

User asked: *"please implement all 3 of those now and dont deferr
anything"*. Pt.49 ships fractional-Kelly + correlation-aware position
sizing, daily score-health monitoring, and a pipeline-aware backtest
that replays historical picks through the full deploy-side gate
stack.

### 1. Fractional-Kelly + correlation-aware sizing

Replaces the flat sizing model where every strategy/symbol got the
same `recommended_shares × max_position_pct` treatment. Now the
size scales with the strategy's realised edge AND with how many
correlated positions are already on the book.

**`position_sizing.py`** (pure module):

* `kelly_fraction(win_rate, avg_win, avg_loss, *, fraction_of=0.5,
  max_fraction=0.25)` — half-Kelly by default, capped at 25%.
  Returns 0 on negative edge / invalid inputs.
* `compute_strategy_edge(journal, strategy, *, min_trades=10)` —
  pulls realised win-rate / avg_win / avg_loss for the strategy
  from the closed-trade journal. Reports `kelly_eligible=False`
  when sample is too small to trust.
* `kelly_size_multiplier(edge)` — maps Kelly to a multiplier in
  `[0.5×, 2.0×]` with `BASELINE_KELLY=0.05` mapping to 1.0×.
  Strong strategies scale up; weak strategies scale down; non-
  Kelly-eligible (insufficient sample) returns 1.0× so legacy
  flow stays in charge.
* `count_correlated_positions(symbol, existing, *, sector_map=...)`
  — same-sector tally; "Other" sector skipped to avoid over-
  discounting unrelated tickers; falls back to
  `constants.SECTOR_MAP` if no map passed.
* `correlation_size_multiplier(N)` — `0.5^N` floored at 0.25.
* `compute_full_size(*, base_qty, strategy, symbol, journal,
  existing_positions, sector_map=None, ...)` — end-to-end
  wrapper. Returns `{qty, base_qty, kelly_multiplier,
  correlation_multiplier, edge, correlated_count, rationale}`.

**Wired into `cloud_scheduler.run_auto_deployer`** at the
recommended-shares site BEFORE `drawdown_mult`. Long + short
paths both call `compute_full_size`. Adjusted qty + rationale
logged for audit. Best-effort — never blocks the deploy on a
sizing-module error.

### 2. Score-degradation alerting

Pt.47 added the score-to-outcome panel; pt.49 turns it into an
active monitor. Without alerting, scoring can silently degrade
and the bot keeps deploying picks the user trusts even though
the ranking has stopped meaning anything.

* `analytics_core.check_score_degradation(journal, *,
  min_trades=30, bucket_count=5)` returns
  `{degraded, warning, tracked_trades, total_closed, headline,
  detail, monotonic_winrate, monotonic_expectancy, ...}`.
  Degraded = both monotonic flags False AND tracked >= min.
  Warning = ONE flag False (soft signal). Below min_trades →
  `degraded=False, warning=False, headline="insufficient sample"`.
* `cloud_scheduler.run_score_health_check(user)` runs daily at
  4:35 PM ET. Notifies on transition INTO degraded state (not
  every day — `_last_runs[score_health_state_<uid>]` tracks last
  reported state). Notifies on recovery (degraded → ok) so the
  user knows it's fixed.
* `build_analytics_view` now includes `"score_health"` so the
  dashboard can surface the status pill alongside the bucket
  grid.

### 3. Pipeline-aware backtest

Pt.37's backtest tests STRATEGY in isolation: "if breakout fired,
would the trade have been profitable?". The production deploy
pipeline applies many filters AFTER the strategy signal —
chase_block, volatility_block, sector cap, trend filter, event-
day gate, min_score. A pick can pass the strategy and STILL not
deploy. Pt.49 quantifies that gap.

`pipeline_backtest.py` (pure module):

* `evaluate_gates(pick, *, held_symbols, sector_counts,
  sector_map, ..., event_label, event_multiplier)` runs a
  single pick through every gate. Returns `{deploy: bool,
  block_reasons: [...], sector: ...}`.
* `run_pipeline_backtest(picks_history, *, sector_map=None,
  initial_held=None, chase_block_pct=8.0,
  volatility_block_pct=25.0, max_per_sector=2, min_score=50,
  event_label_fn=None, simulate_outcomes=False,
  bars_by_symbol=None)` replays a sequence of historical picks
  day-by-day. Reports `{total_picks, would_deploy,
  blocked_by_reason, blocks_by_day, block_rate, deploys}` plus
  optional `counterfactual` P&L via
  `backtest_core._simulate_symbol`.
* Defaults to the production gate thresholds (pt.10-48) so a
  zero-arg call against a real picks history produces a
  realistic answer.

### Tests

+34 in `tests/test_round61_pt49_kelly_sizing.py`
(kelly_fraction edge cases, edge aggregation from journal,
multiplier mapping bounds, correlation count + multiplier,
end-to-end wrapper, source-pin tests on cloud_scheduler).
+15 in `tests/test_round61_pt49_score_health.py`
(degraded vs warning vs ok states, transition notification,
build_analytics_view exposure, source-pin tests on the
scheduler task).
+38 in `tests/test_round61_pt49_pipeline_backtest.py`
(per-gate isolation tests, end-to-end replay, sector cap
intra-day, event_label_fn injection, optional counterfactual).

---

## 🆕 Round-61 pt.48 — active walk-forward + slippage in self-learning + event-day gate (+31 tests)

**Date:** 2026-04-25

User asked: *"please implement 1, 2, 3 completely and test to ensure
fully functioning with no bugs"*. Pt.47 built the walk-forward
harness + slippage/commission parameters but the production
self-learning loop didn't use them — the harness was decorative.
Pt.48 wires them on AND adds news-event awareness so the bot
doesn't trade through Fed days like quiet Tuesdays.

### 1. Walk-forward INTO the self-learning loop

`learn_backtest.run_self_learning` now accepts
`validation_mode="walk_forward"` (default still `"in_sample"` for
backwards compat). When walk-forward:
* Each variant goes through the pt.47 `run_walk_forward_backtest`
  harness — base config sweeps the entire grid in one walk; per-
  variant calls confirm OOS metrics for each candidate.
* Variants compete on `aggregate_test_summary["expectancy"]` —
  the out-of-sample number, not the in-sample one.
* Each variant's `_overfit_ratio` (train_expectancy /
  test_expectancy from the harness) is stamped onto its summary;
  `select_best_variant` rejects any variant where the ratio
  exceeds `max_overfit_ratio` (default 1.5). Even if test
  expectancy beats baseline, a variant whose train run is >1.5×
  the test run is overfitting and gets rejected.
* `propose_adjustments` only applies the overfit filter when
  `validation_mode == "walk_forward"` — pt.44 in-sample callers
  see no behaviour change.

`cloud_scheduler.run_weekly_learning` wires this on:
```python
proposal = run_self_learning(
    bars_by_symbol=...,
    run_backtest_fn=_bc_run,
    current_defaults=DEFAULT_PARAMS,
    validation_mode="walk_forward",
    walk_forward_train_days=30,
    walk_forward_test_days=30,
    walk_forward_step_days=10,
    max_overfit_ratio=1.5,
    ...
)
```

Bar fetch window bumped from 60 to 120 days so walk-forward has
enough timeline to slide several folds.

### 2. Realistic slippage + commission in self-learning

`run_self_learning` accepts `slippage_bps` +
`commission_per_trade` parameters and threads them into every
backtest call (in-sample mode merges into each variant's params;
walk-forward mode passes via `base_params`). Both default to 0 so
existing pt.44 tests are bit-identical.

`run_weekly_learning` now passes `slippage_bps=10.0` +
`commission_per_trade=1.0` — production-realistic friction.
Without this, learned params were calibrated against perfect
fills and over-promised expectancy by 10–20% vs what live trading
would actually deliver.

### 3. Event-day gate (FOMC / CPI / NFP / PCE)

New `event_calendar.py` (pure, stdlib-only) encodes the 2026 +
2027 calendar of high-impact macro events:

* **FOMC** — hardcoded 2-day-meeting end dates from
  federalreserve.gov/monetarypolicy/fomccalendars.htm
* **CPI** — BLS release dates (~13th of each month)
* **NFP** — first Friday of each month (computed)
* **PCE** — last Friday of each month (computed)

API:
* `is_high_impact_event_day(date)` → `(bool, "FOMC"|"CPI"|"NFP"|"PCE"|None)`.
  Accepts `date`, `datetime`, or ISO string. FOMC > CPI > NFP >
  PCE on collisions.
* `next_high_impact_event(date)` → `(label, date)` for the
  next event ≥ the queried date (within 365d horizon).
* `event_score_multiplier(label)` → 2.0/1.5/1.5/1.3 for
  FOMC/CPI/NFP/PCE; 1.0 for non-event days.

`cloud_scheduler.run_auto_deployer` consults the calendar at the
top of each run:
* If today's a high-impact event day, log the elevation factor.
* **Long side**: every pick must clear `50 × multiplier`
  (skipped + logged otherwise — visible in the deploy log).
* **Short side**: `min_short_score` is multiplied by the
  multiplier so weak shorts don't fire on volatility days.

Result: on FOMC day a long pick needs `best_score >= 100` (vs
the implicit ~50 baseline); a short pick needs the elevated
short score. Bot can still take exceptional setups but won't
blunder into the 2pm rate decision with the same risk as a
quiet Tuesday.

### Tests

+31 in `tests/test_round61_pt48_active_self_learning.py`:
* run_self_learning backwards compat (default in-sample mode)
* walk_forward mode invokes the harness fn
* Invalid `validation_mode` raises ValueError
* `select_best_variant` overfit-ratio rejection (with + without
  filter, missing field passthrough)
* `propose_adjustments` only applies overfit filter in
  walk-forward mode
* Slippage/commission threaded into in-sample + walk-forward
  modes; zero-friction omits the keys
* Event calendar: FOMC/CPI/NFP detection, quiet day, ISO + datetime
  + invalid input, priority order, helper functions
* Source-pin tests on cloud_scheduler wiring (imports, calls,
  wiring of validation_mode/slippage/commission/120d window)

---

## 🆕 Round-61 pt.47 — accuracy batch: pt.44b/pt.38b wiring + walk-forward + slippage + score-to-outcome (+69 tests)

**Date:** 2026-04-25

User asked: *"what else do we need to do to make this app more accurate
... please implement 1, 2, 3, 4, 5 and be accurate and make sure you
test"*. Pt.47 ships all five gaps as one batch — closes the deferred
infrastructure-without-consumer items from pt.38 + pt.44, plus three
backtest/analytics accuracy upgrades.

### 1. Pt.44b — `learned_params.json` consumer

The pt.44 self-learning loop wrote `learned_params.json` every week,
but no consumer read it back. Pt.47 wires it in:

* New `strategy_params.py` (pure module). Function
  `resolve_strategy_param(strategy, param_name, fallback, tier_cfg,
  learned_params)` returns the effective per-strategy value following
  precedence `learned > tier > fallback`. Sister helper
  `resolve_rules_dict(strategy, base_rules, tier_cfg, learned_params)`
  returns a copy of `base_rules` with `stop_loss_pct`,
  `profit_target_pct`, `max_hold_days` resolved.
* Naming alias built in: learned_params uses the short
  `stop_pct`/`target_pct` names (see `learn_backtest.TUNABLE_PARAMS`),
  while TIER_STRATEGY_PARAMS + the deployer's `rules` dict use the
  long form. The resolver maps between them transparently.
* `cloud_scheduler.run_auto_deployer` loads the per-user
  `learned_params.json` once per run (best-effort; never blocks),
  passes it to `strategy_params.resolve_rules_dict` at the long-side
  rules-construction site, and to
  `strategy_params.resolve_strategy_param` at the short-side.

### 2. Pt.38b — `TIER_STRATEGY_PARAMS` consumer

The pt.38 tier-aware risk table was sitting unused since landing.
Pt.47 wires the same resolver above so tier values layer in BELOW
learned_params but ABOVE the legacy hardcoded defaults. Concrete
result: a $500 cash-micro account stops getting the same 12% stops
as a $100k margin-standard account.

### 3. Walk-forward backtest validation

Pt.37's backtest was in-sample. With pt.44 actively tuning params on
the same data, the "5% improvement" threshold was meaningless if the
tuning was overfitting. Pt.47 adds:

* `backtest_core.run_walk_forward_backtest(bars_by_symbol, strategy,
  *, train_days, test_days, step_days, param_grid, base_params,
  metric)` — slides a (train, test) window forward, picks the best
  param variant on each train slice and evaluates that exact variant
  on the immediately-following test slice.
* Returns per-fold detail + aggregate train/test expectancy + an
  `overfit_ratio` (train_expectancy / test_expectancy). Healthy
  strategy: ratio ≈ 1. Ratio > 1.5 ⇒ overfitting risk; the param
  tuner is memorising in-sample noise.
* Default param grid: ±20% sweep on stop_pct + target_pct around
  `DEFAULT_PARAMS[strategy]`.

### 4. Slippage + commission in backtest

Pt.37's simulator assumed perfect fills at the close price. Pt.47
adds realistic friction:

* `_simulate_symbol` now accepts `slippage_bps` (basis points; 1 bps
  = 0.01%) — applied to entry + exit prices, always working AGAINST
  you (long entry pays more, long exit receives less; short entry
  receives less, short exit pays more).
* `_simulate_symbol` now accepts `commission_per_trade` (dollar
  amount subtracted from pnl per round-trip).
* Both default to 0, so existing pt.37 behaviour is bit-identical.
  Production should pass realistic values (e.g. 10 bps + $1) so
  backtest expectancy doesn't over-promise vs live results.
* Trade records carry `slippage_bps` + `commission` for audit.

### 5. Score-to-outcome correlation panel

Pt.46 shipped the Analytics Hub but no meta-validation: did higher-
scored picks actually win more often? Pt.47 adds it:

* `cloud_scheduler` auto-deployer now embeds `_screener_score` into
  every journal-open entry (long + short paths). `record_trade_close`
  preserves the field (it only adds close-side fields).
* New `analytics_core.compute_score_outcome(journal, bucket_count=5)`
  bins closed trades by `_screener_score` into 5 quintile buckets and
  computes win_rate / total_pnl / expectancy per bucket. Reports
  `monotonic_winrate` + `monotonic_expectancy` flags — green if win
  rate / expectancy strictly non-decreasing low→high (the healthy
  pattern); red if random.
* New panel in the Analytics Hub renders the bucket grid + status
  pills. Surfaces "tracked vs untracked" so users know how many
  closed trades have a score embedded vs how many are pre-pt.47
  legacy.

### Tests

+22 in `tests/test_round61_pt47_strategy_params.py` (resolver
precedence, name aliasing, garbage-input safety, source pins).
+28 in `tests/test_round61_pt47_walk_forward_slippage.py`
(slippage helper, simulator-integrated, walk-forward folds, base
param passthrough, overfit-ratio).
+19 in `tests/test_round61_pt47_score_outcome.py` (bucketization,
monotonic detection, deployer source pins, dashboard panel pins).

---

## 🆕 Round-61 pt.46 — Analytics Hub (+59 tests)

**Date:** 2026-04-25

User said: *"can you make me a really robust and good looking and
highly functional analytics dashboard with everything I need to know
and make it user friendly but also useful please test it to make sure
its fully functional and is fully accurate im going to need this
right now all the analytics are all over which are fine for their
area but I need a analytics hub"*.

Pt.46 ships a single Analytics tab that consolidates every
performance metric the bot already had scattered across the
Performance Attribution panel, Today's Closes, Scorecard,
Trades dashboard (pt.36) and Backtest panel (pt.37). One tab,
one POST, every KPI on screen.

### What landed

Three-piece architecture mirroring pt.7/34/36/37:

1. **`analytics_core.py`** — pure aggregator. No I/O, no
   subprocess, no Alpaca calls. Every function takes already-
   loaded dicts and returns a JSON-safe dict:
   * `compute_headline_kpis(journal, scorecard, account, now)` —
     count totals, win rate, total/avg/best/worst realized P&L,
     unrealized P&L, expectancy, Sharpe, max drawdown, paper-
     validation days elapsed (anchored at 2026-04-15), avg
     hold days.
   * `compute_equity_curve(scorecard)` — sorted [{date, value}]
     from `daily_snapshots`.
   * `compute_drawdown_curve(equity_curve)` — running peak +
     drawdown_pct per day.
   * `compute_pnl_by_period(journal, now)` — today / 7d / 30d
     / 90d / all-time buckets.
   * `compute_pnl_by_symbol(journal, top_n=10)` — top |P&L|,
     OCC option symbols resolved to underlying.
   * `compute_pnl_by_exit_reason(journal)` — sorted worst-first.
   * `compute_hold_time_distribution(journal)` — six buckets
     (<1d / 1-3d / 3-7d / 7-14d / 14-30d / 30d+).
   * `compute_pnl_distribution(journal)` — six buckets
     (big_loss / loss / small_loss / small_win / win / big_win).
   * `compute_best_worst_trades(journal, top_n=5)` — `{best:[],
     worst:[]}` lists for the leaderboards.
   * `compute_filter_summary(picks)` — total/deployable/blocked
     plus per-reason counts (uses the pt.45 filter_reasons tags).
   * `compute_strategy_breakdown(journal)` — per-strategy
     `{count, wins, losses, win_rate, total_pnl, avg_pnl, best,
     worst}`.
   * `build_analytics_view(journal, scorecard, account, picks,
     now)` — end-to-end orchestrator.

2. **`/api/analytics` endpoint** — `handle_analytics_view` in
   `handlers/actions_mixin.py`. POST-only, read-only, JSON in
   /JSON out. Auth-gated (401 if no session). Loads
   `trade_journal.json`, `scorecard.json`, `dashboard_data.json`
   (for picks), and live Alpaca `/account` + `/positions` (best-
   effort — falls back to None if Alpaca errors). Calls
   `analytics_core.build_analytics_view`. Never writes a file,
   never places an order, never records a trade.

3. **Analytics dashboard tab** in `templates/dashboard.html`:
   * 📊 Analytics nav button between Strategies and Positions.
   * 8 headline KPI cards: Total P&L, Win Rate (with W/L count),
     Expectancy, Avg Win, Max DD, Sharpe (annualised), Avg Hold,
     Validation (days elapsed of paper-trading window).
   * Equity Value chart — inline SVG with `<polyline>` line +
     `<polygon>` fill area. No Chart.js dependency. Renders
     responsively to viewport width.
   * P&L by Period card (today / 7d / 30d / 90d / all-time).
   * Per-strategy breakdown grid (one card per strategy with
     count / win rate / total P&L / expectancy / best / worst).
   * P&L Distribution histogram (6 buckets).
   * Hold Time Distribution histogram (6 buckets).
   * Top Symbols by Impact bar (top 10 |P&L|).
   * P&L by Exit Reason bar (sorted worst-first).
   * Best Trades + Worst Trades leaderboards (top 5 each).
   * Screener Filter Summary (counts of how many picks were
     blocked by each filter — uses pt.45's tags).
   * Auto-prefetched during `init()` so the tab is hot on
     first click.

### Tests

* `tests/test_round61_pt46_analytics_core.py` — 39 unit tests
  covering every aggregator + edge cases (None inputs, empty
  journal, OCC underlying resolution, sort order, fail-open
  on bad data, validation-day anchoring).
* `tests/test_round61_pt46_analytics_endpoint.py` — 20 source-
  pin + http_harness tests for the route, handler, read-only
  invariant, auth requirement, dashboard nav tab, section
  anchor, refresh handler, endpoint URL, all 8 KPI labels,
  equity chart presence, distributions, top symbols/exit
  reasons, best/worst sections, filter summary. (4 use the
  cryptography-dependent `http_harness` — sandbox-skip,
  CI-pass.)

---

## 🆕 Round-61 pt.45 — filter-reason tags on screener picks (+9 tests)

**Date:** 2026-04-25

User-reported confusion: the Top 3 panel showed MRVL/TXN/NVDA when
the screener table had POET (458) / AMD (172) / INTC (155) above
them. Those higher-scored picks were filtered by deploy-time gates
but the screener table didn't surface the WHY.

### What landed

* **`update_dashboard.py`** — builds `held_symbols` from the user's
  open positions and appends `already_held` to `filter_reasons` for
  matching symbols. Bridges existing pt.39 `_filtered_by_trend` →
  `below_50ma`/`above_50ma` and pt.40 `_breakout_unconfirmed` →
  `breakout_unconfirmed`. Existing chase_block + volatility_block
  tags retained.
* **`templates/dashboard.html`** — screener table chip-render block
  consumes `p.filter_reasons` and emits orange chips with tooltips
  explaining the block:
  * `already_held` → 🚫 Held
  * `below_50ma` → ⤓ <50MA
  * `above_50ma` → ⤒ >50MA
  * `breakout_unconfirmed` → ? 1-day
  * `chase_block` → 🏃 + value
  * `volatility_block` → ⚡ + value

### Why it matters
Without these chips, the screener appeared to misrank picks. Pt.45
makes the deploy decision transparent.

### Tests
+9 in `tests/test_round61_pt45_filter_reason_tags.py`

---

## 🆕 Round-61 pt.44 — backtest-driven self-learning loop (+35 tests)

**Date:** 2026-04-25

User-requested closing of the feedback loop: every weekly run, the
bot runs the pt.37 backtest harness on each tradable strategy +
recent trade-journal symbols, then proposes parameter adjustments
based on what the simulation shows would have worked better.

### What landed

* **`learn_backtest.py`** — pure module: `build_param_variants`,
  `clamp_param_change` (±25% per cycle + absolute floor/ceiling),
  `select_best_variant` (≥5% improvement + ≥5 sim trades),
  `propose_adjustments`, `merge_into_learned_params` (12-cycle
  history cap), `run_self_learning` orchestrator.
* **Wired into `cloud_scheduler.run_weekly_learning`** after the
  existing `learn.py`. Best-effort. Reads journal symbols, fetches
  OHLCV via cached `backtest_data`, writes `learned_params.json`.

### Safety invariants
- Per-cycle change ≤ ±25%
- Absolute bounds: `stop_pct ∈ [5%, 20%]`, `target_pct ∈ [5%, 50%]`,
  `max_hold_days ∈ [3, 60]`
- Improvement threshold ≥5%, minimum sample ≥5 sim trades
- Tunable allowlist: stop_pct / target_pct / max_hold_days only

### Wiring (deferred to pt.44b)
Params are WRITTEN by the cycle but not yet READ back by the
screener. Pt.44b will route `learned_params.json` through
`screener_core` so the next-tick screener honours tuned values.

### Tests
+35 in `tests/test_round61_pt44_self_learning.py`

---

## 🆕 Round-61 pt.43 — Trades dashboard CSRF hotfix

**Date:** 2026-04-25

User reported: Trades section in dashboard shows "Trades unavailable
— getCookie is not defined". Pt.36's `refreshTradesPanel` and pt.37's
`runMultiStrategyBacktest` both passed `'X-CSRF-Token': getCookie(
'csrf_token')` in their fetch headers, but `getCookie` was never
defined at module scope AND the cookie name is `csrf` not
`csrf_token`.

### Fix
The dashboard already has a global fetch wrapper at the top of the
inline script that auto-injects `X-CSRF-Token` from the `csrf`
cookie via `readCsrfCookie()`. Removed the redundant + broken
manual header in both call sites.

### Touched files
- `templates/dashboard.html` (2 single-line fixes + comments)

---

## 🆕 Round-61 pt.42 — composite-regime weighting (+27 tests)

**Date:** 2026-04-25

User-prioritised screener accuracy improvement #4: extend pt.50's
tier system with a richer composite market regime that weights
per-strategy scores. Strong-bull boosts breakout, choppy boosts
mean-reversion + wheel, strong-bear boosts short.

### What landed

* **`screener_core.REGIME_WEIGHTS`** — table mapping five composite
  regimes (`strong_bull` / `weak_bull` / `choppy` / `weak_bear` /
  `strong_bear`) to per-strategy multipliers.
* **`screener_core.compute_composite_regime(spy_above_200ma,
  breadth_pct, vix)`** — pure classifier. Missing inputs → `choppy`.
* **`screener_core.apply_regime_weighting(picks, regime)`** —
  multiplies per-strategy scores, recomputes `best_score`, tags
  every pick. Re-sorts.
* Wired into `update_dashboard.py` after `apply_factor_scores`.
  SPY 200-MA from `spy_long_bars`, breadth from `breadth_data`,
  VIX from `market_info`. Zero extra API calls.

### Tests
+27 in `tests/test_round61_pt42_regime_weighting.py`

---

## 🆕 Round-61 pt.41 — per-strategy adaptive thresholds (+29 tests)

**Date:** 2026-04-25

User-prioritised screener accuracy improvement #3: self-correcting
feedback loop reading the trade journal. Cold strategies (<40% WR)
demoted ×0.70, hot strategies (>60% WR) boosted ×1.30.

### What landed

* `screener_core.compute_strategy_win_rates(journal, lookback=30)` —
  pure helper.
* `screener_core.get_threshold_multiplier(win_rate, sample_size)` —
  pure curve. <5 trades: neutral. ≤40% WR: 0.70. ≥60% WR: 1.30.
* `screener_core.apply_adaptive_thresholds(picks, win_rates)` —
  multiplies best_score, annotates, re-sorts.
* Wired into `update_dashboard.py` after trend filter.

### Tests
+29 in `tests/test_round61_pt41_adaptive_thresholds.py`

---

## 🆕 Round-61 pt.40 — multi-day breakout confirmation (+22 tests)

**Date:** 2026-04-25

User-prioritised screener accuracy improvement #2: single-day
breakouts have the well-known "Tuesday-fake-Wednesday-collapse"
failure mode. Academic momentum research consistently shows
two-bar confirmation lifts win rate ~10-15 points.

### What landed

* **`screener_core.apply_breakout_confirmation(picks, bars_map,
  lookback=20)`** — pure helper. Per Breakout-strategy pick,
  requires both today's close > today's 20-day high AND yesterday's
  close > yesterday's 20-day high. Unconfirmed picks get
  `_breakout_unconfirmed=True` and BOTH `best_score` AND
  `breakout_score` halved. Demoted, not eliminated.
* Wired into `update_dashboard.py` after factor scoring.

### Tests
+22 in `tests/test_round61_pt40_breakout_confirmation.py`

---

## 🆕 Round-61 pt.39 — trend filter: block longs below 50-MA, shorts above (+23 tests)

**Date:** 2026-04-25

User-prioritised highest-ROI screener accuracy improvement. Most
"fake breakouts" happen on stocks BELOW their 50-day MA — they look
exciting on the daily but are dead-cat-bouncing inside a downtrend.
Mirror problem on the short side.

### What landed

* **`screener_core.apply_trend_filter(picks, bars_map, period=50)`**
  — pure helper. Computes SMA(50) from bars_map closes, tags every
  pick with `sma_50` + `above_sma_50`, gates long strategies on
  `price > SMA(50)`, short strategies on `price < SMA(50)`. Filtered
  picks tagged with `_filtered_by_trend` (kept in list for dashboard
  visibility). Fail-open on missing data.
* Wired into `update_dashboard.py` after `apply_factor_scores`
  (zero extra API calls).

### Tests
+23 in `tests/test_round61_pt39_trend_filter.py`

---

## 🆕 Round-61 pt.38 — per-strategy tier-aware risk params (+17 tests)

**Date:** 2026-04-25

User-requested follow-up to round-50's tier system: "per-strategy
stop% / target% should also tier" — rationale being a $500 cash
account can't take the same 12% stops as a $100k margin account
without limiting itself to 4-5 losses before blowing out.

### What landed

* **`portfolio_calibration.TIER_STRATEGY_PARAMS`** — new data table
  keyed `{tier_name: {strategy: {param: value}}}`. Covers all six
  tiers (cash_micro / cash_small / cash_standard / margin_small /
  margin_standard / margin_whale) × every strategy enabled on
  each tier. Skips short_sell entries on cash tiers (the round-50
  invariant) and wheel entries on tiers where `wheel_enabled=False`.

* **`portfolio_calibration.get_strategy_param(tier_cfg, strategy,
  param_name, default)`** — pure helper. Returns the tier-specific
  value if present in the table, else the caller's default.
  Defensive: handles None / non-dict tier_cfg, missing `name` key,
  orphan strategy names, missing param names. Cleanly returns 0
  values from the table (avoids the classic `or default` bug).

### Design intent

| Tier             | Breakout stop | Breakout target | Hold |
|------------------|---------------|-----------------|------|
| Cash Micro       | 5%            | 20%             | 10d  |
| Cash Small       | 7%            | 25%             | 14d  |
| Cash Standard    | 10%           | 30%             | 21d  |
| Margin Small     | 7%            | 22%             | 14d  |
| Margin Standard  | 10%           | 30%             | 21d  |
| Margin Whale     | 12%           | 35%             | 30d  |

Smaller accounts: tighter stops, smaller targets, shorter holds.
Larger accounts: wider stops (avoid noise whipsaws), bigger
targets, longer holds.

### Tests
+17 in `tests/test_round61_pt38_per_strategy_tier.py` covering:
* Every tier × enabled-strategy combination is present in the
  table (no silent fallbacks for some strategies).
* Risk-budget invariants — micro stops ≤ small ≤ standard,
  targets grow with size, max-hold grows with size.
* Short_sell only on margin tiers, wheel only where
  `wheel_enabled=True`.
* `get_strategy_param` happy path + missing-tier / missing-strategy
  / missing-param fallback / non-dict input / 0-value-not-default.
* No orphan tier names in the params table; every TIER_DEFAULTS
  tier has a corresponding entry.

### Wiring (deferred to pt.38b)
Data + helper are in place; the auto-deployer + monitor consumers
still read stop/target from `guardrails.json` directly. Follow-up
PR will route them through `get_strategy_param` so each new
strategy file's rules dict gets tier-aware defaults at creation
time. Pt.38 is the infrastructure; pt.38b is the wiring.

### Touched files
- `portfolio_calibration.py` (TIER_STRATEGY_PARAMS + helper)
- `tests/test_round61_pt38_per_strategy_tier.py` (new)

---

## 🆕 Round-61 pt.37 — 30-day backtest harness (+84 tests)

**Date:** 2026-04-25

User-requested: "Pair the trades dashboard with a backtest harness
for the last 30 days." Closes the "is breakout 0/7 because of THIS
strategy or THIS market?" question with a real simulator.

### What landed

* **`backtest_core.py`** — pure simulation engine, no I/O:
  * Indicator helpers: `_highest_close`, `_lowest_close`,
    `_avg_volume`, `_rsi` (Wilder's, stdlib-only).
  * Per-strategy entry signals for `breakout`,
    `mean_reversion`, `short_sell` — the three strategies whose
    rules can be replayed from bars alone (wheel/pead need
    options/earnings data not in this layer).
  * Per-symbol simulator: stop / target / max_hold / window-end
    exits, both long and short. Stops checked at next bar's
    extreme (low for long, high for short).
  * `_summarize` matches the trades-dashboard schema so the
    backtest output renders with the same panels.
  * `run_backtest` + `run_multi_strategy_backtest` — end-to-end.
  * Pt.35 invariant: leveraged/inverse ETFs blocked from
    short-side sims (defers to `constants.is_leveraged_or_inverse_etf`).

* **`backtest_data.py`** — OHLCV fetcher + on-disk cache:
  * Cache layout: `$DATA_DIR/backtest_cache/<SYMBOL>.json` with
    `{symbol, fetched_at, bars}`. Atomic writes (tempfile+rename).
  * `CACHE_TTL_HOURS = 12` — fresh entries reused, stale ones
    refetched. Bypassable via `force_refresh=True`.
  * yfinance fetcher (`_yfinance_fetch`) is the production path;
    tests inject fakes via the `fetcher` parameter.
  * Graceful degrade: network failure returns stale cache rather
    than crashing.
  * `universe_from_journal` — pulls a sorted unique symbol list
    from the trade journal (resolves OCC option contracts to
    underlyings via the round-22 multi-contract pattern).
  * `universe_from_dashboard_data` — pulls from the screener's
    top picks (preserving order so highest-scored evaluate first).

* **`/api/backtest/run` POST endpoint** in
  `handlers/actions_mixin.handle_backtest_run`. Read-only;
  auth-gated. Body: `{symbols, strategies, days, params,
  force_refresh}`. Universe selection: explicit symbols >
  journal universe > dashboard picks. Returns per-strategy
  results + overall pooled summary + `symbols_evaluated` /
  `symbols_missing` for diagnostics.

* **Dashboard panel** in the existing Backtest section
  (`#section-backtest`): "30-Day Strategy Backtest" sub-panel
  with:
  * Day-window selector (14 / 30 / 60 / 90).
  * Run button → POST /api/backtest/run.
  * Top-line summary cards: Total Trades, Win Rate, Hypothetical
    P&L, Expectancy, Best, Worst.
  * Per-strategy comparison cards (color-coded by aggregate P&L
    sign).
  * Operator-facing explainer at the bottom: "A strategy that's
    NEGATIVE here is structurally losing money on the symbols
    you trade — disabling it via Settings → Auto-Deployer
    Strategies prevents real-money losses going forward."

### Tests
+40 in `tests/test_round61_pt37_backtest_core.py` covering every
indicator, signal, simulator branch (long target / long stop /
long window-end / long max-hold / short target / short cover-stop /
no-signal flat run), aggregation cases (empty / dust / mixed),
end-to-end `run_backtest` + `run_multi_strategy_backtest`, and the
pt.35 leveraged-ETF blocklist defer.

+34 in `tests/test_round61_pt37_backtest_data.py` covering cache
I/O round-trip + atomic-write safety, TTL freshness logic, fetcher
injection (cache-hit / stale-refetch / network-down / not-cached),
`force_refresh`, bulk fetch, and both universe builders.

+10 in `tests/test_round61_pt37_backtest_endpoint.py` covering the
route registration, auth gate, read-only invariant (no order
placement / no journal writes), universe fallback chain
(symbols > journal > picks), dashboard panel surface (Run handler,
window selector, summary cards, explainer block).

### Touched files
- `backtest_core.py` (new, 380 LOC pure simulation)
- `backtest_data.py` (new, 200 LOC fetcher + cache)
- `handlers/actions_mixin.py` (handle_backtest_run added)
- `server.py` (route /api/backtest/run)
- `templates/dashboard.html` (30-day backtest sub-panel + JS)
- `tests/test_round61_pt37_backtest_core.py` (new)
- `tests/test_round61_pt37_backtest_data.py` (new)
- `tests/test_round61_pt37_backtest_endpoint.py` (new)

---

## 🆕 Round-61 pt.36 — Trades dashboard + post-mortem panel (+83 tests)

**Date:** 2026-04-25

User-requested: "a place to see all my completed trades and the
results with the strategies and all the information possible so I
can see what is performing and what is not at a glance, filterable,
mobile-ready, with a per-trade post-mortem." Closes the visibility
gap between the existing Performance Attribution panel
(strategy-level totals only) and the trade-level detail that
informs strategy decisions.

### What landed

* **`trades_analysis_core.py`** — new pure-analysis module:
  * `enrich_trade(trade)` — adds derived fields: pnl_class
    (win/loss/flat/open), is_winner, is_open, hold_days,
    exit_reason_human, occ_underlying.
  * `filter_trades(trades, filters)` — supports status,
    strategy[], win_loss, symbol substring (matches OCC
    underlying too), exit_reason[], side (long/short),
    date_from / date_to, min_pnl / max_pnl.
  * `sort_trades(trades, sort_by, descending)` — stable sort with
    numeric/date/string awareness; missing values ALWAYS sort last
    regardless of direction (split-and-concat).
  * `compute_strategy_summary(trades)` — per-strategy aggregates:
    count, wins, losses, win_rate, total_pnl, avg_pnl,
    avg_win_pnl, avg_loss_pnl, expectancy, best_pnl, worst_pnl,
    avg_hold_days.
  * `compute_overall_summary(trades)` — top-line equivalents.
  * `build_trades_view(journal, filters, sort_by, descending)` —
    end-to-end glue.
  * `EXIT_REASON_LABELS` — pretty-print map for every exit-reason
    code the scheduler writes.

* **`/api/trades` POST endpoint** in
  `handlers/actions_mixin.handle_trades_view`. Read-only; auth
  required. Body: `{ filters, sort_by, descending }`. Returns the
  build_trades_view payload.

* **Trades dashboard tab** in `templates/dashboard.html`:
  * Nav-tab "Trades" between Positions and Screener.
  * Filter row: status select, win/loss select, symbol search
    input, strategy chips (toggle multiple), Reset button.
  * Top-line summary cards: Total Trades, Win Rate, Total P&L,
    Expectancy, Best, Worst — derived over the FILTERED set so
    they reflect the user's current view.
  * Per-strategy summary cards (one per strategy): total P&L,
    trade count, win rate, avg P&L, avg hold days. Borders
    color-coded green/red on aggregate P&L sign.
  * Sortable table (click any column header to flip direction):
    Date / Symbol / Strategy / Side / Qty / Entry / Exit / P&L $ /
    P&L % / Hold / Why exited.
  * Click any row to expand the **post-mortem detail**: entry
    context (symbol, qty, entry price, signal/reason text,
    deployer), exit context (price, why exited, hold duration,
    realized P&L). Option contracts show the OCC parse explicitly.
  * Mobile-friendly: table wrapper has `overflow-x:auto` +
    `min-width:680px`, filter chips wrap.

### Tests
+64 in `tests/test_round61_pt36_trades_analysis_core.py` covering
every helper / filter / sort / summary case.
+19 in `tests/test_round61_pt36_trades_endpoint.py` covering the
HTTP route registration, auth gate, payload shape, filter
plumbing, seeded-journal end-to-end, and dashboard JS surface
(nav tab, section anchor, refresh handler, filter controls,
post-mortem render, summary cards, sortable headers).

### Touched files
- `trades_analysis_core.py` (new, 380 LOC pure analysis)
- `handlers/actions_mixin.py` (handle_trades_view added)
- `server.py` (route /api/trades)
- `templates/dashboard.html` (Trades tab + section + render JS)
- `tests/test_round61_pt36_trades_analysis_core.py` (new)
- `tests/test_round61_pt36_trades_endpoint.py` (new)

---

## 🆕 Round-61 pt.35 — block leveraged + inverse ETFs from short-sell (+20 tests)

**Date:** 2026-04-25

User-reported SOXL incident (round-61 pt.30): the screener picked
SOXL (3x leveraged semis) as a short-sell candidate; the position
lost ~$500 in 9 paper-trading days from a normal-sized adverse move.
Pt.35 prevents this class of pick.

### Why leveraged/inverse ETFs are bad shorts

1. **Decay** — daily-reset products lose value to volatility drag
   even when the underlying is FLAT. Shorting SOXL "expecting
   nothing to happen" still loses money because the product itself
   rolls daily.
2. **Inverse logic error** — shorting an INVERSE ETF (SOXS, SQQQ,
   SDOW) is effectively going LONG the underlying. The screener's
   "bearish on SOXS" thesis is the SAME as "bullish on semis" but
   executed via a short. Always wrong on signal alignment.

### What landed

* **`constants.LEVERAGED_OR_INVERSE_ETFS`** — frozenset of all
  known 2x/3x leveraged longs, leveraged inverses, 1x inverses,
  single-stock leveraged ETFs (TSLL/NVDL/etc.), volatility products
  (VXX/UVXY/VIXY), and leveraged crypto ETFs.
* **`constants.is_leveraged_or_inverse_etf(symbol)`** —
  case-insensitive helper.
* **`short_strategy.identify_short_candidates`** — checks the
  blocklist at the TOP of the per-pick loop, short-circuiting
  before any score logic runs. Emits a one-line summary log when
  symbols are filtered (operator visibility).

### Tests
+20 in `tests/test_round61_pt35_block_leveraged_short.py` covering
the blocklist contents (SOXL/TQQQ/SQQQ/UVXY/single-stock pinned),
the helper (case insensitivity, empty input), filter behaviour
(SOXL/SOXS/VXX dropped, AMD/INTC pass), filter-runs-before-score,
log emission, source-pin (uses `constants` module not a private
copy).

### Touched files
- `constants.py` (new blocklist + helper)
- `short_strategy.py` (filter at top of loop)
- `tests/test_round61_pt35_block_leveraged_short.py` (new)

---

## 🆕 Round-61 pt.34 — capital_check core extraction (+47 tests)

**Date:** 2026-04-25

Mirrors the pt.7 pattern (screener_core.py + scorecard_core.py
extracted from update_dashboard.py + update_scorecard.py): pure math
moves out of the subprocess-driven entry point so pytest-cov can
actually see it.

### Why
`capital_check.py` is invoked exclusively as a subprocess from
`cloud_scheduler.run_auto_deployer`, so pytest-cov never sees its
line execution. Before pt.34 the capital-sustainability math —
including the security-critical `$1000/share fallback floor` from
round-12 — was untested at the unit level. Any regression in the
warning thresholds, sustainability score, or recommendation tiers
would have to be caught by the production scheduler running it,
which is too late.

### What landed

* **`capital_check_core.py`** — new module exporting:
  * `compute_reserved_by_orders` (moved from capital_check, signature
    unchanged) — full pricing-ladder coverage: explicit
    limit/stop/notional, live last-trade fallback, position avg
    entry fallback, `$1000/share` security floor.
  * `position_avg_cost_map` — symbol-uppercase normalization for
    the fallback ladder.
  * `compute_capital_metrics(account, positions, orders, guardrails,
    fetch_last, now_iso)` — pure version of `check_capital()`'s math:
    portfolio_value, total_position_value, reserved_by_orders,
    free_cash, pct_invested/reserved/free, max_positions,
    additional_trades_possible, can_trade, sustainability_score,
    warnings, recommendation. Schema matches the legacy output 1:1.
  * `_LAST_RESORT_PRICE_PER_SHARE` constant (1000) re-exported for
    callers that imported it directly.

* **`capital_check.py`** thinned: `check_capital()` is now a
  coordinator that fetches Alpaca + reads guardrails + delegates
  every line of math to `capital_check_core.compute_capital_metrics`.
  Saves the result via the round-10 per-user `CAPITAL_STATUS_PATH`
  override.

* **Backwards compat**: `_compute_reserved_by_orders` re-export +
  `_LAST_RESORT_PRICE_PER_SHARE` re-export so any caller that
  imported the underscore-prefix names from capital_check still
  works (round-15 audit-fix tests still pass unchanged).

### Tests
+47 in `tests/test_round61_pt34_capital_check_core.py` covering:
* Every pricing-ladder branch in `compute_reserved_by_orders`
  (explicit price, notional, live quote, avg entry, $1000 floor).
* Every Alpaca data shape edge (None orders, malformed qty,
  fetch_last raising, mixed BUY/SELL, multiple orders summed).
* `position_avg_cost_map` symbol normalization + missing fields.
* `compute_capital_metrics` full schema, error-passthrough,
  `portfolio_value=0` defensive math, `max_positions` defaults +
  guardrails override, `can_trade` true/false branches,
  `additional_trades_possible` capped by `max_positions - num_held`,
  divide-by-zero on `max_position_pct=0`.
* All five warning thresholds (HIGH EXPOSURE, MODERATE EXPOSURE,
  LOW CAPITAL, MAX POSITIONS REACHED, HEAVY ORDER BOOK).
* Sustainability score breakpoints (100 healthy, -1/pct above 50%
  invested, -20 at max positions, -30 on low cash, floor at 0).
* All three recommendation tiers (Healthy ≥80, Caution 50-79,
  Critical <50) plus free-cash $-format.
* `now_iso` passthrough so the timestamp field decouples from
  et_time.
* Backwards-compat alias from capital_check.py.

### Touched files
- `capital_check_core.py` (new, 222 LOC pure math)
- `capital_check.py` (thinned 130-line monolith → 60-line coordinator)
- `tests/test_round61_pt34_capital_check_core.py` (new)

---

## 🆕 Round-61 pt.33 — Vitest JS coverage threshold ratchet (lines 0% → 92% floor)

**Date:** 2026-04-25

Pt.22 set up the threshold infrastructure with V8 provider but the
floor stayed at 0% because `vm.runInThisContext` (the evaluation
primitive used by `tests/js/loadRenderCore.js` to load the extracted
render-core module) bypasses V8's coverage instrumentation. Pt.33
fixes this end-to-end so JS coverage is measured + ratcheted just
like Python coverage.

### What landed

1. **Provider switch:** `vitest.config.js` switched from `v8` →
   `istanbul`. Istanbul instruments at parse time (we drive the
   instrumenter manually); V8 only sees code that goes through
   Node's module loader, which `vm.runInThisContext` skips.
2. **Manual instrumentation in the loader:**
   `tests/js/loadRenderCore.js` now imports `createInstrumenter`
   from `istanbul-lib-instrument` (new devDependency), instruments
   the read source string, then `vm.runInThisContext`-evaluates the
   instrumented output. The instrumented code writes coverage
   probes to a global on every function call.
3. **Vitest's expected coverage global:** the default istanbul
   `coverageVariable` is `__coverage__` — but vitest's `takeCoverage()`
   reads from `globalThis.__VITEST_COVERAGE__` per its
   `COVERAGE_STORE_KEY` constant. Pt.33 configures the instrumenter
   with `coverageVariable: '__VITEST_COVERAGE__'` so vitest collects
   the probes as expected.
4. **Realistic thresholds:** measured locally at lines=96.09%,
   functions=100%, branches=86.85%, statements=93.12%. Floor set
   to lines=92, functions=95, branches=80, statements=90 with
   ~3-5% headroom for environment drift.

### Why it matters
The 392 JS tests already exercise the dashboard render core
thoroughly — but until pt.33 the measurement showed 0%, so any
regression that dropped a function from the test suite would go
unnoticed. Now CI fails any PR that drops below the ratcheted
floor, the same guarantee Python coverage has.

### Tests
+0 new tests — 392 existing tests now feed the threshold check.

### Touched files
- `vitest.config.js` (provider + thresholds)
- `tests/js/loadRenderCore.js` (instrumentation pipeline)
- `package.json` + `package-lock.json` (`istanbul-lib-instrument` dep)

---

## 🆕 Round-61 pt.32 — orchestrator coverage (+26 tests)

**Date:** 2026-04-24

Source-pin tests for the four scheduler orchestrators that drive
every live trade. The existing `test_round61_auto_deployer.py`
covered `run_auto_deployer` exhaustively; pt.32 fills the gaps for
the remaining three orchestrators so the coverage push from CLAUDE.md
checks out before the live-trading flip on ~2026-05-15.

### What landed

* **`run_daily_close` coverage** — per-user Alpaca cred + data path
  env vars passed to subprocess (round-9 isolation), update_scorecard.py
  + error_recovery.py both run with `timeout=60`, daily_starting_value
  reset under `strategy_file_lock` (round-57 RMW fix), legacy scorecard
  path fallback chain, push-only ntfy + rich email queueing.
* **`run_wheel_auto_deploy` coverage** — `_wheel_deploy_in_flight`
  dedup with mode-scoped key (round-46 paper+live separation),
  kill-switch gate, auto-deployer-disabled gate, per-strategy
  `wheel.enabled` toggle, `deploy_should_abort()` mid-loop check,
  `max_new_per_day` cap, screener-data freshness via
  `max_age_seconds=300`, rich `wheel_put_sold` email template.
* **`run_wheel_monitor` coverage** — kill-switch gate, multi-contract-
  aware iteration via `ws.list_wheel_files`, `advance_wheel_state`
  dispatch with per-event log+notify, stage-2 covered-call auto-pilot
  gated on `stage_2_shares_owned + not active_contract`, round-44
  wheel-open backfill at end-of-tick. Per-wheel try/except inside
  the loop so one bad file can't crash the whole monitor.
* **Cross-orchestrator invariants** — every orchestrator logs with
  `[username]` prefix, every orchestrator wraps its body in
  try/except so a single tick can't kill the scheduler thread.

### Tests
+26 in `tests/test_round61_pt32_orchestrator_coverage.py`

---

## 🆕 Round-61 pt.31 — partial-qty sell paths must shrink the protective stop FIRST (+13 tests)

**Date:** 2026-04-24

User-reported every Friday at 3:45 PM ET on INTC: 63 shares long,
trailing-stop reserved all 63, Friday risk reduction trim of 31
failed with HTTP 403 / `alpaca_code=40310000` "insufficient qty
available for order (requested: 31, available: 0)". Same bug class
as the SOXL pt.30 cover-stop loop, just on the long side and in a
different code path.

### Fix
New helper `_shrink_stop_before_partial_exit` PATCHes the stop's qty
to `remaining` (or cancels and lets the caller re-place if PATCH
fails). On Alpaca's replace-order semantics, PATCH returns a NEW
order id which the helper writes back to state.

Two paths fixed:
* **`check_profit_ladder`** — sells 25%/25%/25%/25% at +10/+20/+30/+50%
  profit; each rung's market sell would 403 because the trailing
  stop reserved the whole position.
* **`run_friday_risk_reduction`** — sells half of any +20% winner
  before weekend; same 403.

Already-correct paths (mean-reversion target, PEAD time/earnings,
universal pre-earnings, monthly rebalance) cancel-stop-FIRST
correctly and were left untouched — pinned via source assertions.

### Tests
+13 in `tests/test_round61_pt31_partial_sell_qty_reservation.py`

---

## 🆕 Round-61 pt.30 — position-drift guard + qty-available retry (SOXL loop fix) (+17 tests)

**Date:** 2026-04-24

User-reported SOXL stuck retrying a cover-stop every monitor tick
with HTTP 403 / `alpaca_code=40310000` "insufficient qty available
for order (requested: 29, available: 0)" despite the short being
live at -29 shares. Two-layer fix in one PR.

### 1. Position-drift guard
At the top of `process_short_strategy` and `process_strategy_file`,
query `/positions/{symbol}`. Act on POSITIVE evidence only:
  * 404-style error message → position gone. Mark strategy closed,
    journal-record, cancel lingering orders.
  * `qty == 0` → position flat. Same handling.
  * `abs(qty) < state-shares` → partial external close. Sync state
    to broker truth so the next placement uses real qty.

Critically **fails open** on transient errors (circuit breaker
open, rate limited, 5xx) — closing live strategies due to a brief
Alpaca outage would be catastrophic.

### 2. Qty-available retry
When cover-stop placement fails with a 40310000 / "insufficient qty"
/ "available: 0" error, query `/orders?status=open&symbols=SOXL`,
cancel any open BUY orders (they're competing for the same
short-cover qty), retry placement once. Diagnostic for the
production SOXL bug: a leftover `target_order_id` BUY limit at
$94.05 was reserving all 29 shares and blocking every cover-stop
attempt; the retry path flushes that and the cover-stop lands.

Symmetric handling on the long side via `process_strategy_file`.

### Tests
+17 in `tests/test_round61_pt30_position_drift.py`

---

## 🆕 Round-61 pt.22 — audit modal fix + 4 defensive hardening gaps + cloud_scheduler coverage (+31 tests)

User-reported after pt.21 deploy: clicked 🔍 Audit button, toast said
"Running state audit..." but no modal appeared. Plus the 4 hardening
gaps I had flagged as "proposed but not shipped" in the pt.21
close-out.

### What landed
1. **Audit modal visibility fix.** Rewrote `runStateAudit` to use
   the shared `.modal-overlay.active` pattern + `openModal()` helper.
   Pt.21's custom `modal-backdrop` class didn't exist in the
   stylesheet.
2. **Silent-except ratchet.**
   `tests/test_round61_pt22_no_silent_except.py` scans 9
   auth/trading files and fails CI on new `except Exception: pass`
   additions. Baseline frozen at current numbers; opt-out per line
   via `# noqa: silent-except`.
3. **Production snapshot regression test.**
   `tests/fixtures/production_snapshot_scrubbed.json` + matching
   test pin the exact audit findings expected from the known-bad
   state.
4. **Multi-contract wheel support.** When the default
   `wheel_<UNDERLYING>.json` already tracks a different contract,
   error_recovery creates `wheel_<UNDERLYING>__<YYMMDD><P|C><STRIKE8>.json`
   for the second contract. Dashboard + audit both handle the
   double-underscore filename pattern.
5. **JS coverage threshold.** `vitest.config.js` declares a
   `thresholds` block; CI runs `npx vitest run --coverage`. Floor
   starts at 0% pending switch to istanbul provider (vm-executed
   code isn't instrumented by V8).
6. **cloud_scheduler helper coverage.** 13 new tests covering
   `_fmt_money`/`_fmt_pct`/`_fmt_signed_money`, opening-bell window,
   `_compute_stepped_stop` (defensive re-pin), `_has_user_tag`,
   `_build_user_dict_for_mode`.

### Results
- Python tests: 1682 → **1713 (+31)**
- JS: 392 unchanged
- Ruff: clean

---

## 🆕 Round-61 pt.21 — consistency audit + constants source-of-truth + legacy file migration (+26 tests)

User requested a way to prevent the key-drift bug class that spawned
pt.16/pt.19/pt.20, plus fix two production data issues caught in the
JSON audit: DKNG and HIMS short-put positions were mis-routed through
the equity short-sell path (pre-pt.17 error_recovery) and never got
migrated. Five-part change:

### 1. Central constants module extensions

`constants.py` (already existed for SECTOR_MAP + HTTP timeouts) now
also exports:

- `STRATEGY_NAMES: frozenset` — every strategy the bot produces
- `CLOSED_STATUSES: frozenset` — closed/stopped/cancelled/canceled/
  exited/filled_and_closed
- `ACTIVE_STATUSES: frozenset` — active/awaiting_fill
- `STRATEGY_FILE_PREFIXES: tuple` — for filename parsing
- Helpers: `is_closed_status`, `is_active_status`, `is_known_strategy`

Existing consumers updated to import from constants:
- `server._mark_auto_deployed` — closed-status filter
- `error_recovery.list_strategy_files` — closed-status filter
- `scorecard_core.STRATEGY_BUCKETS` — derived from STRATEGY_NAMES
  directly (tuple of sorted names)

Adding a new strategy in the future is now a one-line change:
append to STRATEGY_NAMES and every consumer picks it up.

### 2. Legacy OCC file migration

`error_recovery.migrate_legacy_short_sell_option_files()` runs every
error_recovery invocation (before the orphan scan loop). Scans
STRATEGIES_DIR for `short_sell_<OCC>.json` files, parses the OCC
symbol via `_occ_parse`, and:
- Creates `wheel_<UNDERLYING>.json` in `stage_1_put_active` (short
  put) or `stage_2_call_active` (covered call) with active_contract
  populated from the OCC parse
- Preserves any existing wheel_<UNDERLYING>.json — the wheel monitor
  owns that state
- Marks the legacy file `status: "migrated"` so the dashboard
  labeller and the short-sell monitor both stop picking it up

Fixes user-reported DKNG260515P00021000 + HIMS260508P00027000
mis-routing from production audit.

### 3. `/api/audit` endpoint + `audit_core` module

New `audit_core.run_audit()` pure helper takes a state snapshot
(positions, orders, strategy_files, journal, scorecard) and returns
a severity-grouped findings list covering 7 check categories:

| # | Check | Severity |
|---|---|---|
| 1 | Orphan positions (position + no file + no journal) | HIGH |
| 2 | Legacy OCC mis-routing (short_sell_<OCC>.json still present) | MEDIUM |
| 3 | Ghost strategy files (active file + no position) | LOW |
| 4 | Missing stop orders on non-wheel positions | HIGH |
| 5 | Invalid stop prices (wrong side of current market) | HIGH |
| 6 | Unknown strategy names in journal | MEDIUM |
| 7 | Scorecard freshness (>48h stale) | MEDIUM |

`/api/audit` is the HTTP endpoint; `handle_state_audit` in
`handlers/actions_mixin.py` wires it up. Read-only, safe to call
on-demand. Auth-gated.

### 4. Dashboard 🔍 Audit button + modal

Added next to "🤖 Adopt MANUAL → AUTO" in the Positions section
header. Click opens a modal with findings grouped by severity, each
with category, symbol, and plain-English message. No need to grep
log files or read scheduler output.

### 5. `auto_deployer_config.strategies` widening

New migration `migrate_auto_deployer_strategies_round61_pt21`
(stamped `round61_pt21_auto_deployer_strategies_full`) extends the
historical `[trailing_stop, mean_reversion, breakout]` to include
all 7 strategies so wheel/pead/short_sell/copy_trading can also be
eligible for the unified auto-deployer loop. Idempotent via
`_migrations_applied`. Never removes user-added entries.

### Tests
- `tests/test_round61_pt21_audit_and_migration.py` (+26):
  - Constants: 5 tests on STRATEGY_NAMES + CLOSED_STATUSES + helpers
  - Migration: 4 tests on legacy-file retrofit (happy path, existing
    wheel preservation, equity-short skip, idempotency)
  - audit_core.run_audit: 9 behavioral tests across all 7 check
    categories + wheel exception
  - HTTP endpoint: 3 tests (auth gate, structured response, route)
  - Dashboard UI: 2 source pins (button + JS handler)
  - auto_deployer migration: 3 tests (widening, idempotency,
    missing-file no-op)

### Results
- Python tests: 1656 → **1682 (+26)**
- JS tests: 392 unchanged
- Ruff: clean, `node --check` clean

### Impact on deploy
1. On next Railway deploy, the per-user migration runs once and
   widens the strategies list.
2. On the next error_recovery tick (within 10 min during market
   hours), `migrate_legacy_short_sell_option_files` runs and
   converts DKNG + HIMS legacy files to proper wheel files. Next
   dashboard refresh shows both as AUTO + WHEEL.
3. User can click 🔍 Audit any time to verify state is consistent.

---

## 🆕 Round-61 pt.20 — strategy badge on shorts/wheels + de-spam orphan notifications (+8 tests)

User screenshot showed SOXL + DKNG with AUTO badge but no strategy
pill. Root cause: dashboard's `stratLabelMap` used key `'short'` but
backend writes `'short_sell'` (from filename). Also orphan-found
push notifications fired at warning severity every 10 min with the
same symbol list.

### Fixes
- `stratLabelMap` now keys on `'short_sell'` (primary), back-compat
  with `'short'`, aliases `'wheel_strategy'` + `'wheel_auto_deploy'`
  → WHEEL.
- Orphan notifications use `--type info` (not alert) + persist last
  notified set to `.orphan_notif_last.json`. Same set = no re-fire.

### Results
- Python: 1648 → **1656 (+8)**

---

## 🆕 Round-61 pt.19 — fix SOXL/HIMS adoption gaps + short_sell scorecard drop (+9 tests)

Three user-reported issues from pt.17/18 deploy:
1. SOXL adopted (AUTO badge) but no BUY stop. Monitor's initial
   cover-stop formula `entry * (1 + stop_pct)` placed stop BELOW
   current for underwater short → Alpaca rejected. Fix:
   `max(entry*(1+pct), current*1.05)` — same adaptive formula as
   error_recovery.
2. HIMS short put still MANUAL after "Adopt" click. Grace-period
   filter treated user's manual BUY stop as "in-progress entry"
   and skipped adoption. Fix: exempt shorts + OCC options.
3. `short_sell` missing from `scorecard_core.STRATEGY_BUCKETS` →
   every closed short trade silently dropped from performance
   attribution.

### Results
- Python: 1639 → **1648 (+9)**

---

## 🆕 Round-61 pt.18 — professional stepped trailing stop (+23 tests)

User feedback on pt.17 deploy: on CRDO (long, entry $189.15, current
$197.74), the flat 8% trailing stop sat at $181.72 — still a ~4%
loss from entry if triggered. The "flat trail" design gave back too
much gain on any winner that reversed before really running. User
requested: "would like this to be a professional system".

Replaced the flat `highest * (1 - trail)` formula with a tier-based
stop that mirrors how institutional trend-following systems (Turtle
Traders, Linda Raschke, CTA systems) manage risk as profit grows.

**Tiers (profit measured from entry):**

| Tier | Profit | Stop placement | Purpose |
|---|---|---|---|
| 1 | 0% to +5% | default trail (8% below highest) | Breathing room for the breakout test |
| 2 | +5% to +10% | **ENTRY (break-even lock)** | No-loss guarantee |
| 3 | +10% to +20% | 6% below highest | Lock in some gain |
| 4 | +20%+ | 4% below highest | Ride the monster tight |

**Single source of truth:** `cloud_scheduler._compute_stepped_stop(
entry, extreme_price, default_trail, is_short)`. Both
`process_strategy_file` (longs) and `process_short_strategy` (shorts)
call it — the tier table lives in one place so every code path
agrees.

**Short-side mirror:** same tiers, same boundaries, direction
inverted. Profit % = `(entry - lowest_seen) / entry`; stop is
`lowest * (1 + trail)` for Tier 1/3/4 and `entry` for Tier 2.

**Tier transitions:** `state["profit_tier"]` tracks the last fired
tier so transitions log + notify exactly once per position. Tier 2
entry additionally sets `state["break_even_triggered"]` for audit /
scorecard use. Notification copy on Tier 2:
`"<SYMBOL>: Tier 2 (BREAK-EVEN LOCKED — stop moved to entry)"`.

**Opt-out:** `rules.stepped_trail=false` reverts to flat trail for
backward compat / experimental strategies. Default `True`.

### Results
- Python tests: 1616 → **1639 (+23)**
- JS tests: 392 unchanged
- Ruff: clean

### Practical impact for existing positions
Next 60s monitor tick after Railway redeploys will re-evaluate
every open position against the tier table. No user action required.

---

## 🆕 Round-61 pt.17 — adaptive orphan stops + OCC option support (+14 tests)

User-reported on pt.15/pt.16 deploy:
1. SOXL short adopted by pt.15 but no protective stop ever placed.
   Root cause: cookie-cutter `stop = entry * 1.10 = $121.72` put the
   buy-stop BELOW current market ($129.31). Alpaca rejects those.
2. HIMS short put (OCC `HIMS260508P00027000`) got labeled MANUAL
   after clicking the Adopt button. Root cause: orphan loop routed
   OCC symbols to `short_sell_<OCC>.json`, but `monitor_strategies`
   short-sell path expects equity tickers + share quantities.

### Fixes
- **Adaptive stop formula** in `create_orphan_strategy` AND Check 2
  Missing Stop-Loss:
  - Short: `max(entry * 1.10, current * 1.05)` — always above current
  - Long: `min(entry * 0.90, current * 0.95)` — always below current
- New `_occ_parse(sym)` helper.
- OCC orphan branch routes short put → `wheel_<UNDERLYING>.json` in
  `stage_1_put_active`; covered call → `stage_2_call_active`; long
  option skipped. Never overwrites existing wheel files.

### Results
- Python tests: 1602 → **1616 (+14)**

---

## 🆕 Round-61 pt.16 — error_recovery skips closed strategy files (+6 tests)

Pt.15's "Adopt MANUAL → AUTO" button returned "No MANUAL positions
found" even though dashboard showed MANUAL. Root cause:
`error_recovery.list_strategy_files` returned ALL files while
`server._mark_auto_deployed` (round-61 #110) skips closed files.
Fix: match the same closed-status filter in both paths.

### Results
- Python tests: 1596 → **1602 (+6)**

---

## 🆕 Round-61 pt.15 — autonomous orphan-position adoption (+10 tests)

SOXL short labeled MANUAL despite being opened by the auto-deployer.
Root cause: `error_recovery.py` only ran once per day inside
`run_daily_close`. Between 4:05 PM ET closes, unmanaged positions
sat for up to 23.5 hours.

### Fix
- `cloud_scheduler.run_orphan_adoption(user)` — per-user wrapper.
- Scheduled every 10 min during market hours.
- On-demand `/api/adopt-orphans` endpoint + "🤖 Adopt MANUAL → AUTO"
  button in Positions header.

### Results
- Python tests: 1586 → **1596 (+10)**

---

## 🆕 Round-61 pt.14 — start.sh uses venv python (+4 tests)

Pt.13's red banner pinpointed: `ModuleNotFoundError: No module
named 'cryptography'` at boot even though `requirements.txt` pins
it. Root cause: `start.sh` ran bare `python3` which resolved to
Nix's system interpreter (no access to `/opt/venv/lib/.../
site-packages`).

### Fix
`start.sh` prefers `/opt/venv/bin/python` with loud WARNING fallback.
Added boot-time AESGCM smoke check for build-log visibility.

---

## 🆕 Round-61 pt.13 — crypto import diagnostics (+10 tests)

`MASTER_ENCRYPTION_KEY` unchanged but Save still failed. Root cause:
cryptography import silently failing (pyo3 `PanicException` is
`BaseException`, not `Exception`, so bare `except Exception`
swallowed it).

### Fixes
`except BaseException`, capture `_AESGCM_IMPORT_ERROR`, stderr
critical log, surfaced in `/api/data.api_errors` + dedicated red
dashboard banner. Pinned `cryptography<44.0.0` + added `cffi`
explicitly. Added `libffi` to `nixpacks.toml`.

---

## 🆕 Round-61 pt.12 — Alpaca key save/test diagnostics (+11 tests)

"Save failed" with no detail, even though Test Connection PASSED.
Three fixes:
1. `handle_save_alpaca_keys` differentiates HTTP 401/403/429/5xx
   with hint copy + wraps persist in try/except.
2. New `/api/test-saved-alpaca-keys` endpoint.
3. "Test Saved Keys (PAPER)" / "(LIVE)" buttons in Settings.

---

## 🆕 Round-61 pt.11 — "$0.00 + 100% drawdown" false-alarm fix (+4 JS tests)

Dashboard rendered `$0.00` + red "Approaching max drawdown limit!"
because `/account` error fell through to `parseFloat(... || 0)`.
- `buildGuardrailMeters` treats `portfolioValue <= 0` as "no data".
- Top-of-page orange banner when `d.api_errors.account` exists.

---

## 🆕 Round-61 pt.8 batch-7 — sanitize / preset / panels (+60)

Continues the pt.8 coverage push. JS tests 205 → **241** (+36). Test-only.

- `tests/js/sanitizeReadme.test.js` (16) — `_sanitizeReadmeHtml`
  XSS barrier. Allowed-tag preservation (h1-h6, p, strong, em, ul/li,
  code, pre, blockquote, tables), disallowed-tag unwrap (script,
  iframe, style, svg onload), attribute stripping (onclick, onerror,
  data-*), URI defang (javascript:, data:, vbscript:,
  case-insensitive), https:// passthrough, img data: defang.
- `tests/js/detectActivePreset.test.js` (8) — preset detection from
  guardrails + auto_deployer_config. Pins Round-20 migration
  (0.10 → 0.07 per-stock for Moderate), conservative/aggressive
  boundaries, custom fallback.
- `tests/js/buildTodaysClosesPanel.test.js` (10) — Round-34 panel.
  Empty-state hides panel entirely, one data-row per close,
  positive/negative P&L colour, net total sum, [orphan] marker
  placement, missing-field em-dash fallback (no "$NaN"),
  XSS escape on symbol/strategy/reason.
- `tests/js/renderPortfolioImpact.test.js` (2) — early-exit paths
  (missing target element, null dashboardData).

Results: JS 205 → 241. Python unchanged. Ruff + node checks clean.

---

## 🆕 Round-61 pt.8 batch-5 — sell-fraction, cancel-order, fmtSchedLast

Continues the pt.8 coverage push. JS tests 160 → **181** (+21) across
3 new files. Test-only PR.

- `tests/js/sellFractionModal.test.js` (7) — share-count math (floor
  + 1-share minimum clamp), pro-rata P&L on the sold fraction,
  25%/50% label, negative-P&L red styling.
- `tests/js/cancelOrderModal.test.js` (7) — market vs limit price
  rendering, singular vs plural "share"/"shares", buy-vs-sell copy
  distinction, Confirm button wiring.
- `tests/js/fmtSchedLast.test.js` (7) — nulls → "never", garbage
  passthrough, "just now" / "Xm ago" / "scheduled" branches,
  Round-11 scheduler-aware calendar-day math ("yesterday" vs
  "Nd ago" vs "17h ago" for same-calendar-day entries).

**Results:** 160 → 181 (+21), Python suite unchanged, ruff + node
checks clean.

---

## 🆕 Round-61 pt.8 batch-4 — close-position modal + modal lifecycle

Continues the pt.8 JS coverage push. JS tests 136 → **160** (+24)
across 2 new files. **Test-only PR** — no behavior changes.

**New JS tests:**
  * `tests/js/closePositionModal.test.js` (15) —
    `openClosePositionModal` wheel-aware OCC math + equity path.
    High-value target: getting the math wrong here leads users to
    think they're selling a covered call (capped-upside, safe) when
    they're actually in a naked call (unlimited loss). Pins short
    put breakeven (`strike - premium`), premium×100×contracts,
    cost-to-close math, max-profit = premium-collected,
    max-loss = (strike×100 - premium), assignment-note direction
    ("below" for puts / "above" for calls) + share count, short
    call "Unlimited (naked call)" label, long option breakeven +
    premium-paid-as-max-loss, equity shares×price breakdown +
    fmtPct formatting, multi-contract multiplier, Confirm button
    onclick wiring on both wheel + equity paths.
  * `tests/js/modal.test.js` (9) — `openModal` + `closeModal`
    lifecycle. Round-12 a11y fix pins: `.active` class toggle,
    `role=dialog` + `aria-modal=true` set on open, unknown-id
    no-ops, focus restoration to pre-open opener on close,
    detached-prev-focus safe-exit, stack-based multi-modal open/
    close round-trip.

**Results:**
  * JS tests: **136 → 160** (+24 across 2 new files).
  * Python suite unchanged.
  * Ruff clean, `node --check` clean.

---

## 🆕 Round-61 pt.8 batch-3 — toast / log / relative / scroll tests

Continues the pt.8 JS coverage push. JS tests 99 → **136** (+37)
across 4 new files. No behavior changes — test-only PR.

**New JS tests:**
  * `tests/js/toast.test.js` (16) — toast notification helper. Pins
    info/success/error/warning class branches, XSS escape on message
    + correlationId, ref-suffix conditional rendering, retry-button
    callback + DOM removal on click, retry-throws-swallowed safety.
    Covers `toastFromApiError` dispatch for null data, data.error,
    data.correlation_id surfacing, fallback text.
  * `tests/js/log.test.js` (8) — activity-log renderer. Pins
    newest-first ordering, type→class mapping, XSS escape on the
    message, the 20-entry visible-window cap, and the Round-57
    hash-skip quiet-tick (identical re-render does NOT touch
    innerHTML — anti-jitter).
  * `tests/js/fmtRelative.test.js` (8) — general "Xago" renderer
    used by the activity log + other non-scheduler callers. Pins
    "just now" / "Xm ago" / "Xh ago" / "Xd ago" bucket boundaries,
    unix-seconds numeric-input path, "future" return for negative
    deltas, "never" for null/empty, pass-through for garbage.
  * `tests/js/scrollToSection.test.js` (5) — nav-tab click handler.
    Pins Round-53 fix: `.active` class set on the right tab +
    cleared from prior active tab, `window._activeNavSection`
    persisted so renderDashboard can restore the highlight across
    the 10s auto-refresh tick. Missing target is a no-op (no throw).

**Loader additions:**
  * Exposes `toastFromApiError` and `fmtRelative` on the test API.

**Results:**
  * JS tests: **99 → 136** (+37 across 4 new files, all <500ms cold).
  * Python suite unchanged.
  * Ruff clean. `node --check` clean.

---

## 🆕 Round-61 pt.8 batch-2 — more JS helpers + AUTO/MANUAL mislabel fix

Continues the pt.8 coverage push from #122 (kickoff).

**New JS tests (+31, total now 99 passing across 7 files):**
  * `tests/js/atomicReplaceChildren.test.js` (9 tests) — pins every
    one of the 5 jitter-fix failure modes from CLAUDE.md. Null-guard
    no-ops, atomic `<template>` + `replaceChildren` swap, scroll
    preservation on `.sched-log-box`, data-attribute preservation,
    nested-structure preservation.
  * `tests/js/freshnessChip.test.js` (12 tests) — pins the post-60
    `data-label="..."` architectural invariant (mobile in-place patch
    relies on it), plus age-tier classes (`stale` at >2min,
    `very-stale` at >5min), rollover thresholds (Xs/Xm/Xh), clamp of
    negative ages to 0s, XSS escape on label, numeric unix-seconds
    input path.
  * `tests/js/focusables.test.js` (10 tests) — `_focusablesIn` under
    every modal-keyboard-accessibility branch. Enabled/disabled
    buttons, anchors with/without href, tabindex 0 vs -1, document
    order preservation (critical for Tab cycling), nested containers.

**Loader improvements:**
  * Loader now stubs `setTimeout` alongside `setInterval` so the
    dashboard's delayed `measureStickyHeader(…)` callback doesn't
    fire after the jsdom environment tears down. Silences the
    "document is not defined" async uncaught-exception noise that
    was tagging every batch with a spurious "1 error" summary.
  * Exposes `freshnessChip` + `_focusablesIn` on the test API.

**User-reported AUTO/MANUAL mislabel fix:**
User screenshot showed 5 positions all tagged MANUAL (CRDO long,
DKNG 5/15 $21 short put, HIMS 5/8 $27 short put, INTC long, SOXL
short) despite the bot having auto-deployed them. Root cause for
the two wheel puts: `wheel_strategy.py:791` writes
`deployer="wheel_auto_deploy"` to the trade journal, but
`server._mark_auto_deployed`'s allowlist only recognized
`("cloud_scheduler", "wheel_strategy", "error_recovery")`. Every
wheel-sold put therefore fell through the journal-fallback guard
and got the MANUAL label.

Fix: add `"wheel_auto_deploy"` to the tuple (with docstring calling
out which producer writes the value). Two new pins in
`tests/test_round61_auto_manual_journal_fallback.py`:
  * `test_wheel_auto_deploy_is_recognized_as_auto` — source-pin on
    both `server.py` (allowlist) and `wheel_strategy.py` (producer)
    so removing either side without the other fails CI.
  * `test_behavior_wheel_put_labeled_auto_via_journal` — end-to-end
    behavioral check: empty strats_dir + journal with
    `deployer="wheel_auto_deploy"` → position gets `_auto_deployed=
    True` with `_strategy="wheel"`.

(Equity positions like CRDO/INTC/SOXL use `deployer="cloud_scheduler"`
which was already in the allowlist — if those still show MANUAL it's
a stale-strategy-file + trimmed-journal edge case, not a label-bug.)

**Results:**
  * JS tests: 68 → 99 (+31 across 3 new files).
  * Python tests: 1484 → 1486 (+2 for the wheel_auto_deploy pins).
  * Ruff clean. `node --check` clean.
  * Dashboard redeploy surfaces the AUTO labels on next /api/data
    tick.

---

## 🆕 Round-61 pt.8 — Vitest + jsdom for dashboard JS (kickoff)

Stands up the JS test infrastructure that pt.5/6/7 couldn't reach.
`templates/dashboard.html` ships ~7000 LOC of inline JS that was
invisible to `pytest-cov`; the whole Python-only coverage push was
capped at ~78% because of it. This PR lands the harness + a starter
batch of tests and wires CI; subsequent PRs ratchet coverage up
toward 80% total.

**Infrastructure:**
  * `package.json` + `package-lock.json` (npm-managed) with
    `vitest`, `jsdom`, `@vitest/coverage-v8` as devDeps. The npm
    deps are dev-only — Python is still the only runtime.
  * `vitest.config.js` configures jsdom environment, scopes test
    discovery to `tests/js/**/*.test.js`, writes coverage to
    `coverage/js/` so it doesn't collide with `coverage/`.
  * `tests/js/loadDashboardJs.js` extracts the inline `<script>`
    block from `templates/dashboard.html` and runs it in
    `vm.runInThisContext` so its top-level `function` declarations
    attach to the jsdom global. Stubs the absent `Chart` /
    `marked` CDN libs, no-op-ifies `setInterval`, scaffolds the
    `#toastContainer` / `#app` / `#logPanel` DOM nodes the
    auto-init `init()` expects, and swallows the auto-`fetch()`
    via a 200/{} default. Tests can override stubs at load time
    via the `stubs` argument.
  * `.github/workflows/ci.yml` adds Node 20 setup + `npm ci` +
    `npm test` after the existing pytest step.

**Starter tests (68 passing across 4 files):**
  * `tests/js/esc.test.js` (18) — XSS escape helpers (`esc`,
    `jsStr`). Pin every dangerous-character path: `<`, `>`, `"`,
    `'`, `` ` ``, `\`. Includes combined-payload smoke tests.
  * `tests/js/format.test.js` (21) — money / pct / pnl-class /
    `fmtUpdatedET`. Pins `$NaN` / `+NaN%` regression guards (every
    helper already short-circuits via `isFinite()`), banker's
    rounding boundary, AM/PM ET extraction.
  * `tests/js/occ.test.js` (13) — OCC option-symbol parser
    (`_occParse`). Pins valid HIMS/AAPL/TQQQ/F symbols, lowercase
    rejection, truncation rejection, DTE clamp at 0 for past
    expirations + positive DTE for future expirations.
  * `tests/js/scheduler.test.js` (16) — `parseSchedTs` + 
    `latestForTask`. Pins bare-YYYY-MM-DD-with-task-fire-time
    behavior (auto_deployer 9:45, daily_close 16:05), exact-match
    vs prefix-match precedence, garbage-value skipping.

**Mobile fix piggybacked in this PR (user-reported regression):**
  * Admin → Users table on mobile was rendering each action button
    on its own line (Deactivate / Reset Password / Edit / Revoke
    Admin / Export / Delete = 6 stacked rows × 40px tap target =
    240px+ per user row). Wrapped actions in a flex-wrap container
    `.admin-actions`, added `admin-users-table` class so the
    existing CSS pin actually takes effect, hid Email + Logins
    columns at <=768px via `mobile-hide-md`, hid Role +
    Last-Login at <=380px via `mobile-hide-sm`. Also shortened
    long button labels ("Reset Password" → "Reset", "Revoke
    Admin" → "Revoke", "Make Admin" → "Promote") and switched
    Export/Delete to icon-only on mobile.

**Win-rate display fix (user-reported regression):**
  * User reported "Win Rate 0%" on the Paper-vs-Live comparison
    panel despite having portfolio gains. Root cause: the panel
    used `sc.win_rate_reliable !== false` which defaults to "true"
    when the API field is missing, then renders `(sc.win_rate_pct
    || 0) + '%'` → "0%" anchoring the user on a misleading
    number. Defensive fix: both the Paper-vs-Live panel and the
    Readiness card now compute reliability from
    `closed_trades >= 5 && win_rate_reliable !== false` so a
    missing/wrong API field can't override the sample-size guard.
    Falls back to "N=X, Need 5+ trades" when the sample is too
    small — matches the post-60 architectural invariant.

**Results so far:**
  * 68 JS tests passing (4 files), all in <500ms.
  * Mobile admin table usable on phones again.
  * Win-rate display matches the post-60 invariant under all API
    response shapes.
  * Python suite unchanged: 1484 passing.
  * Ruff clean, dashboard `node --check` clean.

**Still to do for pt.8 (follow-up PRs):**
  * Coverage push: the harness can drive ~50% of the dashboard JS
    pure-helper surface without touching DOM. Each follow-up PR
    targets 1-2 panels (renderDashboard core, openClosePositionModal
    OCC math, atomicReplaceChildren scroll preservation, etc.).
  * Add JS coverage threshold to CI (similar to Python's
    `--cov-fail-under`) once we have a stable floor.

---

## 🆕 Round-61 pt.7 follow-up — scorecard_core.py + behavioral tests

Completes the pt.7 coverage push started in #119:

**Behavioral tests for `screener_core.py`** (+62 tests) —
`tests/test_round61_pt7_screener_core.py` drives every branch of the
seven extracted functions: `pick_best_entry_strategy`,
`trading_day_fraction_elapsed`, `score_stocks` (breakout / wheel /
mean-reversion tiers, volatility soft-cap, copy + pead score-fn
injection + exception swallow, sector + sort), `apply_market_regime`,
`apply_sector_diversification`, `calc_position_size`, and
`compute_portfolio_pnl`. `screener_core.py` goes from 0% (it only
existed in the omit-listed caller) to **98% line + branch coverage**.

**Extract `scorecard_core.py` from `update_scorecard.py`** — same
pattern as #119: `update_scorecard.py` stays in the `omit` list (it's
still a subprocess entry point + dotenv loader + Alpaca HTTP client),
but the pure math moves to `scorecard_core.py` which is NOT omitted.

Functions extracted:
  * `_dec`, `_to_cents_float` — Decimal helpers (Phase-2 migration
    contract)
  * `normalize_strategy_name` — lowercase-underscore canonicalisation
    (Round-7 audit fix)
  * `count_trade_statuses`, `split_wins_losses`, `win_rate_pct`,
    `avg_pnl_pct`, `profit_factor`, `largest_win_loss`,
    `avg_holding_days`
  * `max_drawdown(snapshots, starting_capital, portfolio_value,
                    scorecard_peak)` — three-way peak reconciliation
  * `daily_returns_from_snapshots`, `sharpe_sortino` — annualised
    ratios with downside-deviation Sortino
  * `total_return_pct`, `build_strategy_breakdown`, `build_ab_testing`
  * `build_correlation_warning(positions, sector_map=..., annotate_fn=...)`
    — Round-58 OCC-option-to-underlying sector resolver (dependency
    injected so tests run without `position_sector`)
  * `compute_readiness` — 5-criterion scoring with configurable
    thresholds
  * `apply_snapshot_retention(snapshots, max_count=800)` — 2-year cap
  * `calculate_metrics(journal, scorecard, account, positions, *,
                          now_fn=None, sector_map=None, annotate_fn=None)` —
    orchestrator mirroring `update_scorecard.calculate_metrics`
  * `take_daily_snapshot(journal, account, positions, scorecard, *,
                            now_fn=None, max_snapshots=800)` — orchestrator
    mirroring `update_scorecard.take_daily_snapshot`

`update_scorecard.py` now holds two thin compat wrappers that inject
production dependencies (`now_et`, `constants.SECTOR_MAP`,
`position_sector.annotate_sector`); every existing call site still
works unchanged.

**Behavioral tests** (+85 tests) —
`tests/test_round61_pt7_scorecard_core.py` covers every branch:
Decimal helpers, status-bucketing, win/loss stats, profit-factor
no-losses path, max-drawdown three-way peak, Sharpe/Sortino with
zero-variance and only-positive-returns branches, strategy breakdown
with unknown-name drop, A/B testing with tie + winner-swap paths,
correlation-warning with injected annotator + fallback, readiness
scoring with custom criteria, snapshot retention. `scorecard_core.py`
lands at **99% line + branch coverage**.

Also: **Round-58 pin** (`tests/test_round58_json_audit_fixes.py::
test_correlation_warning_resolves_option_underlying`) updated to
grep BOTH `update_scorecard.py` (for the injected annotator import)
AND `scorecard_core.py` (for the `_underlying`/`_sector` grouping
logic) so a future refactor that removes the mechanism anywhere
fails loudly.

**Results:**
  * Total tests: **1392 → 1484** (+92, 3 deselected unchanged)
  * Total coverage: **51.04% → ~53%** (both modules now visible)
  * CI floor ratcheted: **45 → 48** (.github/workflows/ci.yml)
  * `screener_core.py` 98%, `scorecard_core.py` 99% — both well
    above the floor with room for future modifications

No behavior changes. Ruff clean. CI green locally.

**Still to do for pt.8/9** (future):
  * Vitest for dashboard JS (~6000 LOC) — caps Python-only coverage
    at ~75-80%
  * Deeper `cloud_scheduler.py` coverage (still ~31%; pt.6 moved
    the HTTP surface but not the scheduler internals)

---

## 🆕 Round-61 pt.7 kickoff — extract screener_core.py (PR #119)

`update_dashboard.py` (2608 lines) has been in the `omit` list since
the start because it's run as a subprocess, not import-tested. That
meant the pure scoring math inside it — ~190 lines of per-strategy
scoring, regime filters, position sizing — was invisible to
`pytest-cov` regardless of how many tests we wrote around it.

Pt.7 fixes that by extracting the pure-function logic into a new
`screener_core.py` module that is NOT in the omit list. Functions
pulled out:

  * `pick_best_entry_strategy(scores, entry_strategies)` — argmax
    over entry strategies (Trailing Stop excluded by design)
  * `trading_day_fraction_elapsed(now=None)` — trading-day time math
    that drives the volume_surge rescale. `now` param lets tests
    pin a deterministic value without monkey-patching the clock.
  * `score_stocks(snapshots, *, entry_strategies, sector_map,
                    min_price, min_volume, copy_trading_enabled,
                    pead_enabled, day_fraction=None,
                    pead_score_fn=None, copy_score_fn=None)` — the
    190-line heart of the screener. Per-strategy scoring (Breakout,
    Wheel, Mean Reversion, PEAD, Copy Trading, Trailing Stop),
    volatility soft-cap, data-quality filters, penny-stock + low-
    volume rejection. External deps (pead_strategy.score_symbol,
    capitol_trades.score_symbol) injected as callables so the core
    module has zero outbound imports.
  * `apply_market_regime(picks, regime)` — bias annotation
  * `apply_sector_diversification(picks, max_per_sector, top_n)` —
    max-per-sector + top-N selection
  * `calc_position_size(price, volatility, portfolio_value,
                          max_risk_pct)` — ATR-informed share count
    with 10%-of-portfolio notional cap
  * `compute_portfolio_pnl(positions, portfolio_value)` — per-
    strategy P&L aggregation

`update_dashboard.py` now imports from `screener_core` via thin
compat wrappers so every existing call site still works unchanged.

`tests/test_screener_guards.py` updated to concatenate both files in
its `_read_source()` helper so the regex-based guard tests resolve
patterns regardless of which file holds the definition.

**No behavior changes.** All 1392 pre-existing tests still pass.

**Still to do in pt.7** (follow-up PRs):
  * Behavioral tests for `screener_core.py` — pure functions, cheap
    to cover, should add ~3-5 percentage points
  * Extract `scorecard_core.py` from `update_scorecard.py` with the
    same pattern — another ~2-3 points
  * Ratchet CI floor 45 → ~50

---

## 🆕 Round-61 pt.6 follow-ups — deeper handler coverage (PR #117)

Pt.6 (#116) landed the harness + base coverage. This follow-up
exercises the happy-path branches those tests skipped via the
**admin-and-target pattern**: create admin1 → logout → create target
user → logout → re-auth as admin, so admin endpoints run against
real user IDs rather than only hitting auth gates.

`tests/test_round61_pt6_followup_admin_actions.py` — 57 tests:
  * admin set-active (activate / deactivate + last-admin protection)
  * admin reset-password (target reset + cross-admin block + short-pwd)
  * admin update-user / delete-user / set-admin / create-backup
  * invite lifecycle end-to-end (create → token returned → signup
    consumes the token via `/api/signup`)
  * user toggles: live-mode, track-record-public, scorecard-email
  * kill-switch activate → deactivate round-trip
  * auto-deployer, factor-bypass, force-auto-deploy, force-daily-close
  * forgot-password enumeration defense (same response existing vs
    bogus email — no user enumeration leak)
  * logout actually invalidates the server session (subsequent
    `/api/me` returns 401)
  * seeded journal tests for /api/perf-attribution, /api/trade-heatmap
  * /api/chart-bars, /api/readme, /api/compute-backtest
  * /api/admin/audit-log with `?limit`, /api/admin/export-user-data

**Coverage (server.py + handlers combined):**
  * `handlers/admin_mixin.py`: 33% → **74%** (+41)
  * `handlers/actions_mixin.py`: 38% → **56%** (+18)
  * `handlers/auth_mixin.py`: 44% → **49%** (+5)
  * `server.py`: 42% → **47%** (+5)
  * Full-suite total: 47.19% → **51.04%** (+3.85)
  * Tests: 1259 → **1337** (+78 across pt.6 follow-ups)

No source-code changes. Ruff clean.

---

## 🆕 Round-61 pt.6 — Mock WSGI harness for HTTP endpoints (PR #116)

`server.py` was 7% covered (1708 statements, 1555 uncovered) because
every endpoint required a real socket to test. `handlers/*.py` mixins
were near-0% for the same reason. Built a mock WSGI harness
(`tests/conftest.py::http_harness`) that subclasses `DashboardHandler`
without the socket-dependent `__init__`, auto-injects session + CSRF
cookies, and captures the response as a plain dict.

Four test files exercise the harness end-to-end:
  * `test_round61_pt6_harness_smoke.py` — 6 smoke tests
  * `test_round61_pt6_wsgi_endpoints.py` — 76 tests covering every
    major endpoint's auth gate and basic response shape
  * `test_round61_pt6_wsgi_authed_flows.py` — 82 tests exercising the
    authed code paths inside handlers (SSRF defense, ntfy allowlist,
    password change flow, settings update, email-status queue counting,
    tax report with seeded journal, switch-mode variants, admin flows,
    forgot/reset password enumeration defense)
  * `test_round61_pt6_strategy_mixin_flows.py` — 21 tests on the
    strategy lifecycle (deploy validation, kill-switch / loss-cooldown
    guardrails, pause/stop, apply-preset, toggle-short-selling, per-
    strategy deploy dispatch)

**Metrics:**
- Tests: 1077 → **~1280** (+200)
- Total coverage: 39.79% → **47.19%**
- `server.py`: 7% → **42%**
- `handlers/actions_mixin.py`: ~0% → 38%
- `handlers/admin_mixin.py`: ~0% → 33%
- `handlers/auth_mixin.py`: ~0% → 44%
- `handlers/strategy_mixin.py`: ~0% → 13%+ (pt.6 follow-ups push higher)
- CI floor: 36 → **45** (2% cushion)

No source code changes — pt.6 is purely test infrastructure + coverage.
Dashboard JS untouched. Ruff clean.

---

## 🆕 Round-61 post-shakedown — bug fixes from live traffic (PRs #107–#114)

After the round-61 coverage sprint (#102–#106), the user took the
deployed code through a real session and surfaced eight bugs over
~2 hours. All eight fixed and merged the same day (2026-04-24).

### Five iterative jitter fixes (#107, #108, #109, #111, #113)

User reported persistent scroll jitter that took five rounds of
fixes to resolve because the jitter came from five INDEPENDENT
failure modes. **All five remain necessary.**

| PR | Failure mode | Fix |
|---|---|---|
| #107 | Price ticks (`$192.40 → $192.41`) made `renderDashboard`'s normHash mismatch every 10s → full `#app.innerHTML` rewrite → jitter | Strip `$X.XX`, `±$X.XX`, `±X.X%` from the normalized hash |
| #108 | `refreshFactorHealth` rewrote every tick (embedded freshness chip "Xs ago"); sibling sections reflowed on any rewrite | CSS `contain: layout style` + `overflow-anchor: auto` on every refreshing section + normalized hash-skip in `refreshFactorHealth` |
| #109 | `#app.innerHTML = x` destroyed children before rebuilding → empty intermediate frame → document height collapsed → browser clamped scrollY → user saw "scroll jumps then comes back" | Atomic children swap via `<template>` + `replaceChildren`, plus `#app { min-height: 100vh; overflow-anchor: auto }` |
| #111 | Async-populated panels (`schedulerPanel`, `factorHealthPanel`, etc.) showed "Loading…" placeholders for 500ms-2s after any `#app` rewrite → height bounce → scroll shift | Snapshot each async panel's content before the swap; transplant cached HTML into the new template's matching placeholder |
| #113 | Individual panels still used `panel.innerHTML = html` directly (Recent Activity log added a new line every monitor tick → panel rewrite → jitter). Same empty-frame bug as #109, one level deeper. | New `atomicReplaceChildren(panelEl, newHtml)` helper applied to every panel renderer; preserves `.sched-log-box` `scrollTop` so internal log scroll position survives |

### Trade-display fixes (#110, #114)

| PR | Bug | Fix |
|---|---|---|
| #110 | SOXL close tagged `[orphan]` even though the bot's stop fired — root cause: `error_recovery.py` created a strategy file but didn't write a matching journal open entry, so `record_trade_close` couldn't find it and fell into the synthetic orphan branch | Append `auto_recovered=True` open entry alongside the strategy-file creation |
| #110 | Short SOXL position labeled "TRAILING STOP" because a stale `trailing_stop_SOXL.json` (status=closed) was claiming the symbol over the active `short_sell_SOXL.json` | `_mark_auto_deployed` skips strategy files whose status is closed/stopped/cancelled/canceled/exited/filled_and_closed; `short_sell` priority bumped to 2 |
| #114 | SOXL + HIMS option position labeled MANUAL when both were auto-deployed (no strategy file matched, even though the journal still recorded the deploy) | `_mark_auto_deployed` falls back to `trade_journal.json` when no strategy file matches; recognizes `deployer in (cloud_scheduler, wheel_strategy, error_recovery)`; walks newest→oldest with `setdefault`; option positions try both OCC and underlying for journal lookup |

### Email diagnostics (#112)

User flagged "some emails not coming through":
- **Short force-cover email**: was using `notify_type="info"` which `notify.py`'s `EMAIL_TYPES` set excludes. Changed to `"exit"` so the user actually gets notified when the bot forcibly covers a short after 14 days.
- **`/api/email-status` endpoint** + dashboard 📧 chip showing SMTP-enabled / queued / sent-today / failed-recent / dead-letter / last-sent / recipient. Color-coded (red OFF, orange NO ADDR / N STUCK, dim N queued, green N today). Click → details dialog. 60s poll.

### Metrics post-shakedown

- Tests passing: 1077 → ~1100 (+23 across the bug-fix PRs)
- Coverage: still ~40%
- `server.py` line-limit ratcheted 3100 → 3150 → 3250 → 3300 across three legitimate growths
- Dashboard JS `node --check` clean across all five jitter PRs

### CLAUDE.md updated

`Current session state` section + new `Scroll-jitter fix history (FIVE
rounds)` documented so the next agent doesn't undo any of the five
fix patterns.

---

## 🆕 Round-61 pt.5 — scheduler_api + yfinance_budget + Recent Activity jitter fix

Fourth PR in the round-61 coverage sprint. Lands behavioral tests for
the two most-used infra modules, fixes a user-flagged scroll jitter,
and ratchets the CI floor that was deferred in pt.4.

**+62 tests (scheduler_api + yfinance_budget):**

- `tests/test_round61_pt5_scheduler_api.py` (38 tests) — full
  behavioral coverage of the Alpaca HTTP layer: circuit breaker
  threshold/cooldown/paper-live isolation, rate limiter token
  bucket, auth-failure alert dedup per-day, GET retry on 429 with
  Retry-After, 5xx backoff, 4xx no-retry, CB-open fast-fail,
  endpoint routing (stocks/options/news → data endpoint; orders →
  api endpoint), POST/DELETE/PATCH 401/403 auth alerts. Coverage:
  41% → ~85%.
- `tests/test_round61_pt5_yfinance_budget.py` (24 tests) — rate-limit
  sliding window with timestamp pruning, circuit breaker lifecycle,
  `_call_with_retry` short-circuit when open, public wrappers with
  stubbed yfinance module (happy path, ImportError fallback,
  broken-ticker fallback, empty splits). Coverage: 61% → 92%.

**Recent Activity scroll-jitter fix.** User reported the scheduler
panel (which contains the "Recent Activity (last N)" section) caused
the page to scroll up/down on every 15s auto-refresh tick. Root
cause: the panel rebuilds its HTML every tick because `etTime` and
per-task "Last: Xm ago" strings always differ between renders, so
the existing `_lastHtml` hash-skip never fired → full innerHTML
swap → layout reflow → scroll jump.

Fix pattern (same as R60's `_lastAppNormHash` in `renderDashboard`):
`renderSchedulerPanel` now computes a *normalized* hash that strips
tick-varying substrings (Current ET, per-task "Last: Xm ago", log
timestamps). When the normalized hash matches the prior tick —
meaning state is unchanged, only timestamps advanced — the function
patches just the data-tagged elements via `textContent`/`innerHTML`
surgically instead of rewriting the whole panel. Zero reflow, zero
jitter, full data freshness. Falls back to a full rewrite if the
patch path throws, as a safety net.

**CI floor ratchet 32 → 36** (deferred from pt.4 to avoid triggering
the workflow-approval gate twice). Bundled into this PR so the user
clicks "Approve and run" once for the combined workflow edit +
jitter fix.

**Metrics:**
- Tests passing: 1015 → **1077** (+62)
- Coverage: 38.81% → **39.79%**
- Floor: 32% → **36%**

Ruff clean. `node --check` on dashboard JS clean.

---

## 🆕 Round-61 pt.4 — Behavioral coverage push (36% → 39%)

Third follow-on PR in the round-61 sprint. Pt.1-3 landed the grep-pin
invariants + first behavioral test for `monitor_strategies`; pt.4 adds
real behavioral coverage on modules that were under-tested:

**+118 tests across three files:**

- `tests/test_round61_pt4_helpers.py` (57 tests) — full behavioral
  coverage of `pdt_tracker.py`, `settled_funds.py`, `fractional.py`.
  PDT day-trade detection, buffer/bypass logic, settled-cash ledger
  math, business-day settlement calendar, fractional sizing with
  $1 minimum + whole-share fallback.
- `tests/test_round61_pt4_auto_deployer_behavioral.py` (17 tests) —
  `run_auto_deployer` early-exit paths with a full Alpaca + subprocess
  stub harness. Covers kill-switch short-circuit, config-disabled
  return, cooldown-after-loss + parse-failure-closed, calibration
  with small equity, daily_starting_value seeding, peak bump, capital
  check subprocess + CAPITAL_STATUS_PATH env injection, LIVE_MODE
  snapshot, correlation gate, circuit breaker.
- `tests/test_round61_pt4_wheel_behavioral.py` (44 tests) —
  `wheel_strategy.py` helpers: `log_history` + HISTORY_MAX cap,
  `has_earnings_soon` flag + days window, `find_wheel_candidates`
  filter + sort by wheel_score, `options_trading_allowed` approval
  level check, `cash_covered` sufficiency, `score_contract` delta /
  DTE / premium / liquidity math, `count_active_wheels`,
  `_journal_wheel_close` with fail-safe behavior, `_save_json`/
  `_load_json` atomic roundtrip.

**Coverage: 36.02% → 39%** (wheel_strategy 33% → 47%; helper modules
each now >80%).

**CI floor stays at 32 for now.** The 32 → 36 ratchet was pulled out
of this PR because editing `.github/workflows/ci.yml` triggers
GitHub's workflow-approval gate (which requires a human click before
CI runs). Pt.5 will bundle the floor bump with its workflow edit —
one approval for both. Tests from this PR still enforce the existing
32% floor locally and on CI.

🚨 **User-flagged for next round:** Recent Activity panel still causes
scroll jitter on refresh (desktop + mobile). R60 fixed the same family
for other panels but missed Recent Activity. Fix pattern known (extend
`_lastHtml !==` hash-skip to that renderer). Queued for pt.5 or
sibling round.

See CLAUDE.md "Current session state" section for the full roadmap to
50% (pt.5), 65% (pt.6 — mock WSGI harness for `server.py`), and 80%
(pt.7 — refactor `update_dashboard.py` + optional JS test stack).

---

## 🆕 Round-61 — Money-path test coverage (Option A)

User picked **Option A** from the test-coverage options discussion:
focus-fire tests on the four highest-risk money paths rather than a
full push to 80% coverage. Rationale: 50% coverage on the code that
can lose you money beats 80% on trivial glue.

### Targets

1. **`monitor_strategies`** — 60s loop that enforces kill-switch, daily-
   loss, max-drawdown, stop raises, profit takes, and exits. Heart of
   loss prevention.
2. **`check_profit_ladder`** — 25%-at-each-rung profit-take engine
   (+10/+20/+30/+50% levels). Quarterback of the exit flow.
3. **`run_auto_deployer`** — ~880-LOC deploy pipeline: tier gate,
   fractional routing, short-sell tier block, sector cap, correlation
   gate, skip-chop schedule, already-open dedup.
4. **Wheel state machine** — CSP open → assigned → CC open → CC
   assigned / expired / bought-back / rolled, plus OCC-symbol cost
   basis on assignment.

### Tests shipped

| File | Style | Tests |
|---|---|---|
| `test_round61_monitor_strategies.py` | grep-pin | 13 |
| `test_round61_profit_ladder.py` | behavioral (stubbed Alpaca) | 16 |
| `test_round61_auto_deployer.py` | grep-pin | 19 |
| `test_round61_wheel_state.py` | grep-pin | 23 |
| **Total** | | **71** |

PR split: **#102** (pt.1 — monitor_strategies + profit_ladder, 29 tests),
**#103** (pt.2 — auto_deployer + wheel_state, 42 tests), **#104** (pt.3
— docs, coverage ratchet, behavioral top-up).

### Coverage honesty

53 of the 71 tests are **grep-pin** — they assert on source patterns
(`strategy_file_lock(...)` present, `kill_switch` guard before
`/account` fetch, etc.). They catch refactor-renames, accidental guard
removals, and invariant drift. But they don't exercise code at runtime,
so `pytest-cov` doesn't count them as coverage.

Only the 16 profit_ladder tests + 2 daily-close edge tests are real
behavioral coverage. Actual `pytest-cov` delta is small; the value is
in the regression *detection*, not the coverage number.

Expected CI test count: 820 → **891 passing** after pt.2.

---

## 🆕 Round-60 — Post-live-rollout user feedback

Round-58 made the silent-skip LOUD; round-60 handles the LOUD output properly. Plus mobile polish.

### Fix 1a — Skip ETFs from earnings_exit

User got email alerts for `earnings_exit fetch failed: SOXL (emp...)` and `MSOS`, `IBIT`, etc. ETFs don't have earnings reports the way individual stocks do — the yfinance lookup legitimately returns empty. But round-58 treated that as a "silent fail" and emitted a Sentry breadcrumb.

Fix: `_KNOWN_ETFS` frozenset in `earnings_exit.py` containing 80+ popular ETFs (SOXL/SOXX/SMH, SPY/QQQ/IWM, XLK/XLF/XLV/…, IBIT/FBTC, MSOS/ARKK/JETS, TLT/HYG, GLD/SLV, etc.). `should_exit_for_earnings` short-circuits True on ETF match — no fetch, no breadcrumb.

### Fix 1b — Dedup Sentry breadcrumbs per (symbol, error) per ET day

Pre-market AH monitor runs every 5 min with 6 positions = 72 fetch attempts per hour. Before fix 1b, every failing symbol emitted a fresh Sentry breadcrumb every tick. User saw **60+ alerts in a single pre-market window**.

Fix: `_CAPTURED_TODAY: dict[(symbol, error), date]` tracks what we've already fired today. Same symbol + same error within 24h → silent skip. Different error for same symbol → fires again (real new failure). Midnight ET rolls the dedup set over automatically. Stale entries garbage-collected on each call.

### Fix 2 — Mobile jitter on auto-refresh

User reported: *"on mobile when I am on the sections in the screenshots and the page refreshes the screen scrolls up and down again to the section like it's refreshing."*

Root cause: every 10s tick, the "Updated HH:MM:SS" chip and the "Ns ago" freshness chips bake a different value into the generated HTML. Round-54's hash-skip compared raw strings — so every tick mismatched, triggered a full DOM replace, and the sync scroll-restore painted briefly at scroll=0 before catching up. On mobile that's visible as a jitter.

Fix: build a **normalised hash** that strips tick-only variations (`Updated [^<]*<`, `>Ns ago<`, `Last updated:` title attr) and compare that instead. Quiet ticks → hash matches → skip DOM replace entirely. In-place patch branch updates the timestamp + freshness chips via `textContent` / `outerHTML` without touching scroll. Real content changes (price move, new trade) still hash-mismatch correctly.

### Fix 3 — Position Correlation mobile horizontal scroll

User screenshot showed sector rows with dollar column truncated as `$23,5...`, `$3,6...`, `$1...` — the 380px-wide row didn't fit in the 375px mobile viewport and there was no scroll fallback.

Fix: wrap sector rows in `overflow-x:auto; -webkit-overflow-scrolling:touch` container with `min-width:420px` per row. User can swipe horizontally to see the dollar column; desktop users see no change (viewport accommodates the full row).

### Fix 4 — Dashboard reads `win_rate_pct_display` (round-58 plumbing)

Round-58 server-side plumbed `win_rate_pct_display: null` + `win_rate_reliable: false` + `win_rate_display_note` when `closed_trades < 5`. But the dashboard UI still rendered `sc.win_rate_pct||0` as `0%` in the Readiness panel AND the Paper-vs-Live comparison panel — defeating the round-58 fix.

Fix: both panels now branch on `sc.win_rate_reliable`. When false, they render `N=2` + "Need 5+ trades" (in orange) + the `win_rate_display_note` as a tooltip. When reliable, they render the normal percentage. User no longer anchors on an alarmist `0% win rate` from a 2-trade sample.

### Tests

13 new cases in `tests/test_round60_earnings_mobile_fixes.py`:
- ETF skip list (80+ tickers, case-insensitive, frozenset type)
- Sentry dedup fires once per (symbol, error) per day
- Different error for same symbol fires separately
- `no_future_unreported` stays quiet
- Normalised hash input strips timestamps + freshness chips + Last-updated title
- In-place patch branch present
- `freshnessChip` emits `data-label` for rerender
- Position correlation min-width + overflow-x
- Dashboard branches on `win_rate_reliable`
- `N=X` + "Need 5+" copy present
- `force_refresh` still works (operator bypass)

Plus 2 round-54 tests updated for the new `_lastAppNormHash` name.

**820 passing, 2 deselected**. Coverage 34.54%. Ruff clean. Dashboard JS `node --check` clean.

### Invariants to preserve post-round-60

- ETFs (SPY/QQQ/SOXL/IBIT/MSOS/XL*/etc.) MUST NOT hit yfinance via `should_exit_for_earnings`. Add new ETFs to `_KNOWN_ETFS` when they appear in positions — don't let them leak into the fetch path.
- Sentry breadcrumbs for `earnings_exit_fetch_failed` MUST dedup per `(symbol, error)` per ET calendar day. Removing the dedup returns the 60-alert-per-morning flood.
- `renderDashboard` MUST compare normalised hash (`_lastAppNormHash`), not raw string, so tick-varying timestamps don't trigger DOM replace.
- `freshnessChip` MUST emit `data-label="…"` when given a label; the in-place rerender needs it to regenerate.
- Dashboard panels that show win rate MUST branch on `sc.win_rate_reliable` to avoid anchoring on `0%` from tiny samples.

---

## 🆕 Round-59 — Final pre-live fixes

User asked for everything we can fix tonight. Three real items remained after rounds 56-58, all shipped in this round.

### Fix A — Form 4 XML parser → real `total_value_usd`

Round-58 changed `insider_data.total_value_usd` from misleading `0` to honest `null`. That removed the bug but the underlying feature wasn't built. Now it is.

* New `parse_form4_purchase_value(accession_with_doc)` fetches the primary doc XML from `https://www.sec.gov/Archives/edgar/data/...` and sums shares × price for transaction-code "P" (open-market purchase) only. Excludes A (grant), M (option exercise), G (gift), F (tax withholding), S (sale).
* `_form4_archive_url` parses `0001628280-26-023978:wk-form4_1775526679.xml` into the canonical SEC URL.
* Per-accession results cached **indefinitely** — Form 4s never change after submission. First screener run pays the cost; every subsequent one hits the cache.
* `_FORM4_XML_BUDGET_PER_CALL = 5` per `fetch_insider_buys` call so the screener doesn't spend 8 minutes hitting SEC at 1 req/sec for 50 picks × 10 filings each.
* Status enum: `parsed` (all filings parsed, ≥1 purchase), `partial` (budget exhausted), `no_purchase` (only sales/grants/exercises), `not_parsed` (no filings).
* Parse errors + bad accessions ARE cached (won't fix themselves). Network errors are NOT cached (transient — let next run retry).

Dashboard now shows real cluster-buy dollar value next to filer count instead of "—".

### Fix B — Migrations multi-process flock

Round-57 DB-agent flagged the migration race as MEDIUM theoretical (single Railway container = no concurrent boot). I'd skipped it then. Closing now in case Railway ever scales horizontally.

* New `_user_migration_lock(user_dir)` context wraps each user's migration cycle with `fcntl.flock(LOCK_EX)` on `<user_dir>/.migrations.lock`.
* POSIX-only — Windows degrades to a no-op (Linux containers always have fcntl).
* Defends against the race where two processes both load guardrails, both decide round-51 hasn't been applied, both apply, second clobbers first's `_round51_tier_adopted` field.
* Best-effort: if lock acquire fails (disk full, perms), yields anyway — better to migrate-twice than block boot.

### Fix C — Coverage floor ratcheted 25 → 30

Actual coverage measured at **34.36%** after rounds 54-58 added ~50 tests. Floor at 25% had 9 percentage points of cushion; we lock in most of that gain by raising to 30 (4% cushion). Future PRs that drop coverage will fail CI; future PRs that add tests should bump again.

### Tests

13 new in `tests/test_round59_final_fixes.py`:
- Form 4 archive URL construction (good + bad input)
- XML parser sums P transactions, ignores S/A/M/G/F
- `no_purchase` status when only sales/grants
- Cache hit on second call (no extra fetch)
- Parse-error caching (cached); fetch-error NOT caching (transient)
- Budget cap `_FORM4_XML_BUDGET_PER_CALL` enforced — `partial` status when exceeded
- `_user_migration_lock` serialises concurrent threads (real flock test)
- Lock degrades gracefully on missing dir
- `run_all_migrations` calls the lock per user
- CI coverage floor pinned to 30

Full suite: **807 passing, 2 deselected** (sandbox-only). Ruff clean. Coverage: 34.36% (well above 30 floor).

### Invariants to preserve post-round-59

- `parse_form4_purchase_value` MUST cache parse errors + bad accessions but NOT cache network errors.
- `_FORM4_XML_BUDGET_PER_CALL` (5) caps SEC fetches per `fetch_insider_buys`. Lowering it loses signal; raising it slows every screener run by 1s per extra fetch.
- Only transaction code "P" counts toward `total_value_usd`. Adding A/M/G/F would inflate the number with non-conviction events.
- `_user_migration_lock` MUST be acquired per user (not globally) so one user's slow round-51 fetch doesn't block other users' migrations.
- CI `--cov-fail-under` MUST never decrease. Rounds that add tests should ratchet it up.

---

## 🆕 Round-58 — Bugs surfaced by reviewing the live `/api/data`

User forwarded their live `/api/data` dump at 2026-04-22 7 PM ET for review. Audit surfaced 8 real issues — all fixed.

### Fix 1 — Correlation warning mis-buckets OCC options

Scorecard correlation guard at `update_scorecard.py:368` was calling `SECTOR_MAP.get(sym, "Other")` directly with the raw symbol. OCC option symbols like `HIMS260508P00027000` aren't in `SECTOR_MAP`, so they all fell into "Other" and triggered false "3+ positions in same sector" warnings. Fixed by routing through `position_sector.annotate_sector` which resolves OCC → underlying first. Also surfaces the underlying (e.g. `HIMS`) in the warning text instead of the 17-char OCC symbol nobody recognises.

### Fix 2 — SECTOR_MAP missing common picks

User's dump showed `CRDO`, `FSLY`, `MRVL`, `ALAB`, `LEVI`, `LUV`, `DAL`, `ALK`, `COF`, `KSS`, etc. tagged as "Other" — all well-known tickers with obvious sectors. Added 40+ tickers visible in the screener output to `constants.SECTOR_MAP`. Round-30 claimed 80+ additions but these popular names were missed.

### Fix 3 — Screener now mirrors the deployer's don't-chase + volatility gates

Top pick in the dump was MSOS with score 281 — but `daily_change=21.56%` and `volatility=25.23%`. Round-36's deploy-time gates correctly skip picks like this (`cloud_scheduler.py:2341`), but the screener still ranked them at the top. User saw "top pick" that would never deploy.

Fix: `update_dashboard.py` now annotates each enriched pick with `filter_reasons: []` + `will_deploy: bool`. Picks flagged with `chase_block` (>8% intraday for breakout/PEAD) or `volatility_block` (>20% vol) sort AFTER deployable picks so the "top 5" reflects what the deployer will actually pick up at 9:45 AM. Picks keep full enrichment so the operator can audit the screener's reasoning.

### Fix 4 — Picks include current positions

User's dump had CRDO, FSLY, SOXL, USAR, INTC, HIMS all in both `positions` and `picks`. The deploy path already skips already-held symbols, but the screener output didn't reflect that — confusing UX.

Fix: `get_dashboard_data` (server.py) annotates each pick with `already_held: true` when the caller is currently long (or short) the underlying. OCC options route via `_underlying` so the HIMS short put flags HIMS picks as already-held. Dashboard can dim already-held rows; we annotate rather than drop so the screener output remains auditable.

### Fix 5 — Scorecard displays alarmist 0% win rate on N=2

Scorecard shows `win_rate_pct: 0.0` on `total_trades: 9, closed: 2` — both closes happened to be losers, so 0%. Rendered on the dashboard as "0% win rate / Readiness 40/100" — reads catastrophic when the sample is this tiny.

Fix: `/api/data` now adds `win_rate_sample_size`, `win_rate_reliable`, `win_rate_pct_display` (null when N<5), and a `win_rate_display_note` ("Only 2 closed trades — not enough for a reliable win rate. Keep paper-trading."). Dashboard renders the note instead of the percentage when the sample is insufficient. Raw `win_rate_pct` stays in the payload for downstream analytics.

### Fix 6 — Insider data displays "$0 of insider buying"

Every pick's `insider_data.total_value_usd` was `0` even when `buy_count: 12, buyer_count: 6, has_cluster_buy: true`. SEC EDGAR's full-text search doesn't return transaction dollar amounts — those live in the Form 4 XML which we don't fetch. Displaying `0` next to a 12-filer cluster buy read as "$0 of insider buying" → misleading.

Fix: emit `total_value_usd: null` + `value_parse_status: "not_parsed"` so the UI can render "—" instead of a confidence-eroding "$0". Full Form 4 XML parse deferred (requires per-filing fetch + rate-limit respect); the cluster-buy boolean + filer count still drive the `insider_bonus` score correctly.

### Fix 7 — Un-enriched tail picks clutter `/api/data`

Screener enriches only the top 50 candidates but writes all ~431 passing picks to `dashboard_data.json`. Picks 51+ arrived with `momentum_5d: 0`, `momentum_20d: 0`, `relative_volume: 1.0`, no `technical` block, `recommended_shares: 0`. User saw them as "top picks" in the list.

Fix: `/api/data` filters picks that have none of: a `technical` block, non-zero momentum, or a positive `recommended_shares`. Everything that arrives with real screener signal gets `enriched: True` and renders normally; the default-valued tail is dropped from the response.

### Fix 8 — `earnings_exit` silently failed open on yfinance errors ⚠️ operational

User's INTC position should have auto-closed on 2026-04-22 per round-29 (earnings 2026-04-23, 1-day buffer). But INTC was still open at 7 PM ET. Root cause in `earnings_exit._fetch_next_earnings_from_yfinance`: every failure branch (`ImportError`, shape drift, network error, empty result) returned `None` → `should_exit_for_earnings` fail-opened silently → position held through earnings.

Fix: every failure branch now stamps `_LAST_FETCH_ERR[symbol]` with a distinct reason (`yfinance_not_installed`, `shape_drift:<ErrType>`, `network:<ErrType>`, `empty_result`, `no_future_unreported`). `should_exit_for_earnings` emits a Sentry `capture_message(event="earnings_exit_fetch_failed", …)` breadcrumb when the None return came from a real fetch failure — the legitimate "no upcoming earnings in the next 8 scheduled events" (`no_future_unreported`) stays quiet.

New `force_refresh(symbol)` operator tool busts the 4-hour cache and re-fetches immediately. Admin can now verify the earnings rule for a specific position in-session.

### Tests

13 new cases in `tests/test_round58_json_audit_fixes.py`:
- Correlation guard grep + integration test (HIMS put → Healthcare)
- SECTOR_MAP coverage for 14+ newly-added tickers
- Screener chase/vol/demotion gates
- `/api/data` `already_held` annotation
- Win-rate suppression on small sample
- Insider `total_value_usd: null` + `value_parse_status`
- Un-enriched pick filter
- `earnings_exit` LOUD path + error-type tracking + `force_refresh` tool

Full suite: **794 passing, 2 deselected** (sandbox-only deselects). Ruff clean. Dashboard JS `node --check` clean.

### Invariants to preserve post-round-58

- `update_scorecard` correlation guard MUST route through `position_sector.annotate_sector` so OCC option symbols resolve to underlying.
- `SECTOR_MAP` is the single source of truth. When the screener surfaces a new ticker, add it here — not duplicate in update_dashboard / update_scorecard.
- Screener-annotated `will_deploy=False` picks MUST sort after `will_deploy=True` picks in `top_candidates` so the "top 5" panel reflects deployability.
- `/api/data` MUST NOT emit picks with no real screener enrichment. The default-value tail is noise.
- `earnings_exit` fetch failures MUST emit `capture_message(event="earnings_exit_fetch_failed")`. The rule's silent fail-open cost us an earnings hold in round-58 — don't regress this.

---

## 🆕 Round-57 — Full tech-stack audit fixes

User asked for a pre-live sweep: "audit front to back the full tech stack to make sure there are no bugs or any logic issues or anything that should be corrected … fix all bugs and issues you're able to fix."

5 parallel Explore agents ran (security, DB/concurrency, trading logic, UI/mobile, tests/ops). 13 real bugs fixed + 1 user-flagged UX bug (desktop nav scroll). 3 false positives verified and documented (not re-audited). Zero items deferred.

### Concurrency — 4 unlocked `guardrails.json` RMW sites

The round-55 scheduler and round-54 HTTP handlers both mutate `guardrails.json` from multiple threads, but four sites were still doing unlocked read-modify-write. A concurrent handler POST (e.g. kill-switch toggle, calibration override) could race a scheduler tick and lose writes.

* `cloud_scheduler.py:1891` — stop-triggered `last_loss_time` write now inside `strategy_file_lock(gpath)`.
* `cloud_scheduler.py:2857` — `run_daily_close` `daily_starting_value` reset + `peak_portfolio_value` update now locked. Alpaca `/account` fetch moved **outside** the lock (100-500ms network call would block the monitor + handlers otherwise).
* `server.py` `/api/calibration/override` — RMW now under `strategy_file_lock`. Tier detection + `/account` fetch happen outside the lock first.
* `server.py` `/api/calibration/reset` — same pattern.

### Rate limiting — `/api/calibration/override`

Added per-user 3-second cooldown (`_CALIBRATION_OVERRIDE_LAST_WRITE` module dict). Scripted loops / fast double-clicks return HTTP 429 with `rate_limited:true` so the UI can differentiate from a validation error. Not a security fix (auth + CSRF are intact), just a disk-churn + flock-contention mitigation.

### Observability — failed trailing-stop raises surface to Sentry

Previously, a failed `PATCH /orders/{stop_id}` during a trailing-stop raise just logged a WARN and moved on. Operator debugging "why didn't my stop tighten at 4:30 PM?" had to scroll the activity log. Now fires `observability.capture_message` with event=`trailing_stop_raise_failed`, session (`AH` vs `market`), symbol, attempted new stop, and first 200 chars of the Alpaca response. Visible in the Sentry feed.

### UX / accessibility

* `<input type="range">` CSS — 32px container height, 22px custom thumb, `accent-color: var(--blue)`, webkit/moz styling for WCAG-AA contrast on the dark background. iOS users can actually drag the Calibration sliders now.
* Dashboard header — `⚡ AH TRAILING` status chip renders during pre-market (4:00-9:30 AM ET) and post-market (4:00-8:00 PM ET) when `guardrails.extended_hours_trailing ≠ false`. Users see the bot is actively tightening stops, not sleeping.
* Calibration hierarchy text — wrapped in `role="note" aria-live="polite" aria-atomic="true"` so screen readers announce it when the tab loads.
* **Desktop nav-tabs wrap at ≥1024px** — user flagged the horizontal scroll on a wide monitor. Tabs now flex-wrap onto multiple rows; scroll-chevron hint hidden. Mobile (<1024px) still scrolls horizontally.
* **Lower-page jitter fix** — user reported the screen still jumped around during auto-refresh when scrolled into the lower sections (Heatmap, Perf Attribution, Tax Report, Factor Health, Scheduler, Activity Log). Each enrichment panel now hash-skips its `innerHTML` write when the output hasn't changed — same pattern as round-54's `window._lastAppHtml`. Quiet ticks are now truly zero-repaint throughout the page.

### Data exposure

`/api/data` now returns `extended_hours_trailing` (default True) so the dashboard can render the AH chip. Defaults to True on file-missing / read-error paths so a corrupted guardrails doesn't silently render "AH OFF" when the monitor is actually running.

### Tests

16 new cases in `tests/test_round57_audit_fixes.py`:

* Lock-presence grep pins on all 4 guardrails RMW sites
* `/account` fetched outside the lock (ordering check)
* Rate-limit state + 429 response pins
* Sentry breadcrumb emission
* Daily-close `portfolio_value=None` / `=0` runtime edge cases
* `/api/data` exposes `extended_hours_trailing` with True-default fallbacks
* Dashboard `⚡ AH TRAILING` chip HTML
* Slider touch-target CSS (32px + accent-color + webkit thumb)
* Desktop nav-tabs wrap CSS (media query + flex-wrap + chevron hide)
* Lower-page enrichment panel hash-skip (>=6 `_lastHtml !==` guards)

**781 passing, 2 deselected** (sandbox-only deselects). Ruff clean. Dashboard JS `node --check` clean.

### False positives verified (don't re-audit)

* AH monitor vs. regular monitor race on strategy files — both paths already hold exclusive `strategy_file_lock(filepath)` (line 1240 + 1097). Impossible race.
* "Hash-skip jitter fix not implemented" — UI agent missed it. Actually implemented at `dashboard.html:6411` via `window._lastAppHtml` string-equality (no hash, no collision risk).
* "Save Overrides button does nothing if only a slider changes" — false reading. Per-key `onchange` handlers (`saveCalibrationOverride`) save each change individually; no bulk button exists by design.

### Invariants to preserve post-round-57

* Every `guardrails.json` RMW MUST hold `strategy_file_lock(gpath)`. Slow ops (Alpaca `/account` fetches, tier detection) MUST happen outside the lock.
* `/api/calibration/override` MUST rate-limit at 3s per user/mode.
* Failed trailing-stop raises MUST fire `observability.capture_message` with `event=trailing_stop_raise_failed` + `session` tag.
* `/api/data` MUST expose `extended_hours_trailing` (default True).
* `input[type=range]` CSS MUST keep 32px height + `accent-color` + custom webkit/moz thumb styling.
* `server.py` LOC cap now 3100 (was 3000 post-round-54). Bump with care.

---

## 🆕 Round-56 — Daily-close email option/short display

User forwarded a screenshot of their end-of-day email:
> `HIMS260508P00027000  +28.29%  +$58.00  (-1 sh)`

Three readability bugs, zero math bugs:
1. **"sh" label on an option contract** — OCC symbols (`HIMS260508P00027000`) are contracts, not shares.
2. **`{sym:<6}` column width** — OCC symbols are 17-18 chars. Fixed-width email clients truncated them visually.
3. **`(-1 sh)` for a short put** — negative magnitude reads like bad data; "short 1 contract" is clearer.

**Fix:** new `_display_label(sym, qty)` closure in `_build_daily_close_report` that:
* Detects OCC symbols via `error_recovery._is_occ_option_symbol`
* Parses OCC → `HIMS put 260508 $27` (underlying + right + expiry + strike)
* Labels contracts: `short 1 contract` / `5 contracts` (singular/plural aware)
* Prefixes shorts with "short" + absolute qty (instead of negative magnitude)
* Preserves equity output: `SOXL  +27.35%  +$2,723.76  (117 sh)` unchanged

**Before → After** for the HIMS row:
```
 • HIMS260508P00027000 +28.29%  +$58.00  (-1 sh)           ← old (truncated, wrong noun)
 • HIMS put 260508 $27    +28.29%  +$58.00  (short 1 contract)  ← new
```

Math (% + $ P&L + total unrealized + winners/losers sort) untouched — display-only.

**Tests:** 11 new cases in `tests/test_round56_daily_close_email.py` covering OCC labelling, short-prefix for equity and options, plural vs singular contract noun, strike + expiry render, long-symbol OCC edge case, and a grep-level regression guard that the `{sym:<6}` format string never returns to the positions block.

---

## 🆕 Round-55 — After-hours trailing-stop tightening

User: *"how do we get this bot to work in after hours too — right now I have some [stocks] I could have the stops raised and if they go back down before morning we are leaving money on the table."*

**Problem:** The bot was only tightening trailing stops during regular market hours (9:30 AM - 4:00 PM ET). If a position ran up $4 post-market then faded overnight, the stop stayed at the pre-pop level → gains lost.

**Fix:** Monitor runs in **stops-only mode** during pre-market (4:00-9:30 AM ET) + after-hours (4:00-8:00 PM ET).

**AH mode does:**
* 5-min cadence (regular hours still 60s — unchanged)
* Fetches latest trade (Alpaca returns extended-hours quotes)
* Updates `highest_price_seen` when AH beats prior high
* Runs trailing-stop raise (PATCH or cancel+replace) so stop is tighter before next open
* Stop stays `time_in_force: gtc` — triggers on next regular-hours price cross

**AH mode SKIPS (thin-book protection):**
* Daily-loss kill-switch, initial stop placement, profit-take ladder, mean-reversion target, PEAD 60-day, earnings-exit, short positions, wheel option closes

**Opt-out:** `extended_hours_trailing: false` in guardrails.json (default ON).

**Tests:** 10 new in `test_round55_after_hours_trailing.py`. Suite: 755 passed, 1 deselected (734 main + 11 round-54 + 10 round-55). Ruff clean.

**Operator impact:** once Railway deploys, post-market pops on your holdings will raise the trailing stop within 5 min. The stop fires at next market open if price crosses — locking in the new high instead of letting it fade overnight.

---

## 🆕 Round-54 — Calibration per-key overrides + desktop jitter fix

User asked: *"we were going to give the user the ability to adjust any of the auto calibration levers if they want with pop-ups and warnings as needed... make this user friendly but give the trader control as well. Also the desktop version is still jumping around when it refreshes makes it hard to use."*

**Calibration-override UI (new):**

* **POST `/api/calibration/override`** — writes one key at a time to `guardrails.json` with server-side validation:
  - Whitelist of editable keys: `max_positions`, `max_position_pct`, `min_stock_price`, `fractional_enabled`, `wheel_enabled`, `short_enabled`, `strategies_enabled`
  - Range checks: `max_position_pct` 0-50%, `max_positions` 1-50, `min_stock_price` 0-10000
  - **Alpaca-rule hard blocks**: `short_enabled=True` on a cash account returns `blocked_by_alpaca_rule=True` → UI shows a red `alert()` popup instead of saving
  - Audit log entry for every override

* **POST `/api/calibration/reset`** — reverts the tier-adopted keys back to calibrated defaults. Preserves user-customized risk keys (`daily_loss_limit_pct`, `earnings_exit_*`, `kill_switch_*`).

* **Settings → Calibration tab** got editable controls: sliders for `max_positions` / `max_position_pct` / `min_stock_price`, toggles for fractional / wheel / shorts, strategy pills, ↺ Reset to Tier Defaults button.

* **Client-side warnings** for risky overrides: `max_position_pct > 15%`, `max_positions > 12`, `short_enabled` going ON, `fractional_enabled` going OFF.

**How Templates + Calibration interact** (inline UI explainer):

```
Your manual edits  →  Preset click  →  Calibration defaults
   (most specific wins; each successive layer gets overridden)
```

**Jitter fix (desktop + mobile):**

Previous rounds (47, 48) tried scroll preservation + fewer cascading re-renders. User still reported jitter. Root cause: every 10-second tick wholesale-replaced ~30KB of DOM even when nothing meaningfully changed.

* **Hash-skip**: renderDashboard builds HTML into a variable, compares to `window._lastAppHtml`. If identical, skip the innerHTML assignment entirely → zero repaint, zero jitter on quiet ticks.

**Tests:** 11 new cases in `tests/test_round54_calibration_overrides.py`. Ruff clean. Node `--check` clean. server.py LOC cap raised 2850 → 3000 for the new endpoints.

---

## 🆕 Round-52 — Full tech-stack audit + fixes

User asked for a comprehensive audit after merging rounds 50 + 51. Five parallel Explore agents swept security, concurrency, trading-logic, UI, and ops/tests. Surfaced **11 real bugs** across all layers; also verified **8 false positives** (wheel options proceeds math, tier boundaries, tier-stash race, mode isolation, atomic writes, ledger pruning, XSS, JS syntax).

**Fixes shipped (all 11):**

1. **CRITICAL**: Short-sell tier gate missing (`cloud_scheduler.py:2611`). A cash-account user with `short_selling.enabled=true` in `auto_deployer_config.json` would trigger Alpaca shorts that then got rejected server-side (Alpaca rule: margin + ≥$2k equity). Fail-closed locally now: `TIER_CFG.short_enabled=False` → skip the block entirely.
2. **HIGH**: Unlocked read-modify-write in `fractional._save_cache`, `pdt_tracker.log_day_trade`, `settled_funds.record_sale`. Concurrent paper + live scheduler ticks on the same user could lose entries. Added fcntl.flock via `_file_lock(path)` helper in each module. Verified with 20-thread race tests that all entries land.
3. **HIGH**: Migration backup could orphan on main-write failure. If `migrate_guardrails_round51` wrote the backup successfully but the main `_save_json_atomic` raised (disk full, permission denied), the backup was left in place + the stamp wasn't written → next boot hit the "backup already exists" guard + skipped the fresh migration. Now: rollback the backup we created in this call if main write fails.
4. **HIGH**: Missing Sentry integration. New modules swallowed errors silently via `except Exception: pass`. Now route critical failure paths (`fractional.refresh_cache`, `fractional._save_cache`, `settled_funds.record_sale`) through `observability.capture_exception` so systematic failures surface in Sentry.
5. **HIGH**: Tests gap — added 5 new tests covering `/api/calibration` response shape, auth-gate position, migration malformed-guardrails handling, migration account-fetcher-raises handling.
6. **MEDIUM**: Fractional sub-$1 target couldn't fall back to whole-share (`fractional.size_position`). If tier said fractional ON + symbol fractionable but target was $0.50 (below Alpaca's $1 fractional minimum), we returned qty=0. Now: fall through to whole-share path — if 1 share at price ≤ $0.50 is affordable, buy it instead of rejecting.
7. **MEDIUM**: Tier log spam (`cloud_scheduler.py:1922`). Every `run_auto_deployer` tick was logging `Calibrated tier: 🌳 Cash Standard — equity $100,243 (strategies: ...)`. Now only logs on state change (first time for that user, or when tier changes tier). Per-user-per-mode state cache via `_last_runs`.
8. **MEDIUM**: README missing round-51 auto-migration docs. Added "Auto-migration (existing users)" section explaining the migration flow + revert path.
9. **LOW**: Removed `/tmp` fallback in `fractional._cache_path`, `settled_funds._ledger_path`. Silent fallback to `/tmp/fractionable_cache.json` could cause cross-user collisions if a user dict lacked `_data_dir` (programming bug). Now raises `ValueError` loudly so the caller gets immediate feedback.
10. **MINOR**: Recalibrate Now button had no debounce. Rapid double-click fired concurrent `/api/calibration` fetches. Added `_calibrationInFlight` flag + button `disabled` state + finally-block reset.
11. **MINOR**: Bare `except Exception` on best-effort logging paths (`pdt_tracker.log_day_trade`, etc.). Kept the broad except intentionally — these are best-effort audit paths that must never block trades. But the *right* fix was missing observability calls, which we added in #4.

**False positives verified** (audit agent reports read but traced against actual code):
- Options proceeds math: wheel closes use `side="buy"` which correctly SKIPS the settled-funds ledger. No 100× multiplier bug.
- Tier boundaries: verified no gaps or overlaps in `TIER_DEFAULTS`.
- `tier_cfg` stash race: scheduler is single-threaded per user.
- Mode isolation: all 3 new modules correctly scope to `user["_data_dir"]` which is mode-aware.
- Atomic writes: all use `tempfile + rename` correctly.
- Ledger pruning: `settles_on` cutoff is correct (not `sold_on`).
- XSS: `loadCalibration()` correctly `esc()`'s all user-controllable strings.
- JS syntax: `node --check` passes.

**Tests:** 16 new cases in `tests/test_round52_audit_fixes.py`. Includes 20-thread concurrent-write race tests for settled_funds + pdt_tracker — without the lock fix these would have lost entries. Full suite: **728 passed, 1 deselected** (was 712 + 16 new). Ruff clean. Node `--check` clean.

**Safety assessment:** all fixes are either additive (tests) or strictly fail-closed (short gate, locks, migration rollback). No behavior changes to working code paths. Can ship immediately.

---

## 🆕 Round-51 — Activate calibration for existing users + deep integration

**User ask:** *"can you just enable it for us now"*

Round-51 turns round-50's infrastructure into real trading behavior — existing users get calibrated defaults auto-adopted on first boot after deploy.

**What shipped:**

* **Auto-adoption migration** (`migrate_guardrails_round51`) — detects tier from Alpaca `/account`, merges tier defaults into `guardrails.json`, backs up the old file to `.pre-round51.backup`, stamps `_migrations_applied` for idempotency. Preserves user-customized risk keys (`daily_loss_limit_pct`, `earnings_exit_*`, kill-switch state). Runs at boot via `run_all_migrations`.
* **Settled-funds gate** in `run_auto_deployer` — cash accounts: blocks deploys that would exceed settled cash × 95% buffer (Good Faith Violation prevention). Margin: pass-through.
* **Fractional routing** in `run_auto_deployer` — when tier enables fractional + symbol is fractionable, uses `fractional.size_position()` and passes `fractional=True` to `smart_orders.place_smart_buy()` → market-only order per Alpaca's fractional-qty constraint.
* **PDT guard** in `check_profit_ladder` — for margin <$25k accounts, holds intraday profit-take exits overnight when `day_trades_remaining ≤ buffer`. Preserves emergency day-trade slot for kill-switch.
* **Sell-side ledger** in `record_trade_close` — every long-position sell records proceeds + T+1 settlement date. Next cash-account deploy respects the ledger.
* **Tier stashed on user dict** in `monitor_strategies` — all exit paths can read `user["_tier_cfg"]` to consult PDT / settled-funds rules.

**Operator impact:**

* **Kbell0629 + Jon (paper)**: first scheduler tick after deploy runs the migration. Activity log shows `migration round51_calibration_adopt: migrated`. Old guardrails backed up; defaults adopted.
* **Jon's future $500 live account**: Cash Micro tier detected on first live tick; fractional ON, 2 positions × 15%, 3 strategies. Works out of the box.
* **Revert path**: restore `guardrails.json.pre-round51.backup` if user dislikes the new defaults.

**Safety rails:**
* Migration "no_tier" outcome when Alpaca /account unavailable → stamp NOT written → retry next boot
* All calibration hooks fail OPEN (allow trade) on exception — never block money-making on advisory code
* User overrides always win; migration only fills tier-scoped keys (sizing, fractional, strategies)

**Tests:** 15 new cases in `tests/test_round51_activation.py`. Suite: **710 passed, 1 deselected** (was 697 + 13). Ruff clean.

---

## 🆕 Round-50 — Portfolio auto-calibration (any account size, Alpaca-rule aware)

**User ask:** *"allow this stock bot to calibrate everything under the hood based on how much money is available... a $500 cash trading account could still use this bot... if someone opened a $1M+ account they could also still use this bot... it wouldn't matter how much they have little or big."*

Round-50 makes the bot dynamic and Alpaca-rule-aware at any account size from $500 to $1M+. Detection reads Alpaca's `/v2/account` directly (not guesses from equity) and respects every Alpaca constraint: cash-account no-shorts, PDT rules, settled-funds, fractional eligibility.

**Four new modules:**

* **`portfolio_calibration.py`** — reads Alpaca's `multiplier`, `equity`, `pattern_day_trader`, `shorting_enabled`, `day_trades_remaining` and classifies the account into 6 tiers. Each tier has its own defaults.
* **`fractional.py`** — daily-cached list of fractionable symbols + sizing helper. A $500 account can hold a $25 slice of TSLA at $250/share.
* **`pdt_tracker.py`** — uses Alpaca's `day_trades_remaining` to respect the 3-in-5 rule on margin < $25k. Holds intraday exits overnight when ≤ 1 slot remains.
* **`settled_funds.py`** — T+1 ledger for cash accounts. Blocks deploys that would exhaust settled cash before recent sales settle (Good Faith Violation prevention).

**Six tiers:**

| Tier | Equity | Strategies | Positions | Max% | Fractional | Short | Wheel |
|---|---|---|---|---|---|---|---|
| 🌱 Cash Micro | $500-$2k | TS + Breakout + MeanRev | 2 | 15% | ON | ❌ | ❌ |
| 🌿 Cash Small | $2k-$25k | + PEAD + Copy | 5 | 10% | ON | ❌ | ❌ |
| 🌳 Cash Standard | $25k+ | + Wheel | 8 | 7% | Optional | ❌ | ✅ |
| 📘 Margin Small | $2k-$25k | + Short | 6 | 8% | ON | ✅ ETB, PDT | ❌ |
| 🏛️ Margin Standard | $25k-$500k | All 6 | 10 | 6% | Optional | ✅ | ✅ |
| 🐋 Margin Whale | $500k+ | All 6 + cap | 15 | 4% | Optional | ✅ | ✅ |

**Alpaca-rule enforcement:**

* **Cash accounts** → shorting BLOCKED (Alpaca rule). User overrides attempting `short_enabled=True` on cash silently rejected with `short_override_rejected=True` stamp.
* **Margin < $25k** → PDT rules ACTIVE. Bot tracks `day_trades_remaining`; intraday exits held overnight when ≤ buffer (default 1).
* **Cash accounts** → T+1 settled-funds active. Every sale recorded with `settles_on` date; deploys blocked if they'd exceed settled cash × 95% buffer.
* **Margin** → `min_stock_price: 3` (Alpaca's <$3 not-marginable rule).

**Fractional integration:**

* Micro/Small/Margin-Small default fractional ON — any liquid stock becomes affordable.
* `smart_orders.place_smart_buy(fractional=True)` routes direct to market (Alpaca's fractional-qty constraint).
* Screener price filter auto-relaxes when fractional is on.

**Settings UI:**

* New **🎛️ Calibration** tab shows detected tier, equity, settled cash, buying power, PDT status, day-trades-remaining, enabled/disabled strategies per Alpaca rules.
* Recalibrate Now button forces fresh `/account` fetch.
* User overrides in guardrails.json always win for risk-preference changes; Alpaca-rule violations blocked.

**Tests:** 41 new cases in `tests/test_round50_portfolio_calibration.py`:
  * Per-tier detection (6 tests)
  * Boundary cases (below $500, invalid input)
  * User override merge + Alpaca-rule rejection
  * Wheel affordability dynamic check
  * PDT allow/deny across cash + margin scenarios
  * Settled-funds record/query/expire
  * Fractional sizing (fractional symbol, whole-share, invalid)
  * Parametrized end-to-end for all 6 tiers

Suite: **697 passed, 1 deselected** under CI invocation (was 656 + 41 new). Ruff clean. Node `--check` clean.

**Operator impact:**
* Jon's $500 live-money account → auto-detects as 🌱 Cash Micro, fractional on, 2 positions × 15%, no shorts/wheel. Works out of the box.
* Your $100k paper → 🌳 Cash Standard. Full strategy set including wheel.
* Anyone at any size → saves keys, enables parallel, bot auto-tunes.

**Known limitations (future rounds can tighten):**
* Deep integration into `run_auto_deployer`'s per-pick loop is light-touch in round-50 — calibration fills missing guardrails defaults but doesn't yet force-disable strategies mid-deploy. Full enforcement lands in round-51 after beta testing.
* Fractional currently opt-in via smart_orders signature — round-51 will auto-route based on tier + symbol fractionability.

---

## 🆕 Round-44 — Auto-fix orphan wheels + kill the refresh jitter (2026-04-22)

Two user-requested UX fixes landed in one PR. Replaces the originally
drafted round-43 button approach with something fully automatic.

**1. Orphan wheel closes fix themselves now — no button.**

Round-43's first draft shipped a `/api/admin/backfill-wheel-opens`
endpoint + "🎡 Fix Orphan Wheel Closes" admin button. User feedback:
*"I don't want a button for the orphan wheels just fix it please."*
Agreed — this is plumbing, not a user decision.

Round-44 drops the button + endpoint and wires
`wheel_open_backfill.backfill_wheel_opens(user)` into the tail of
`run_wheel_monitor`. The backfill is idempotent + cheap (no Alpaca
calls, just reads local wheel files + journal), so it's safe to run
every monitor tick. Any new orphan close that lands in the journal
gets paired with its original sell-to-open entry price (recovered
from the wheel state `history[]`) within one wheel monitor cycle.

Clicking ⚡️ Force Deploy immediately triggers a tick — user's
CHWY `[orphan]` tag resolves without manually visiting the admin panel.

**2. Dashboard stops jumping around during auto-refresh.**

Root cause: `refreshData` fires every 30s, replaces large section
innerHTMLs, some sections' height changes (new positions, updated
rows). With viewport-level content shifted, the user's scroll
position now looks "different" — feels like the page is jumping.

Two-layer fix:

* **CSS `overflow-anchor: auto`** on `body` — modern browsers
  auto-compensate for above-viewport DOM height changes (Chrome,
  Firefox, Edge, Safari 18+). Free win for the common case.
* **JS scroll + focus preservation in `renderDashboard()`** —
  explicitly saves `window.scrollY` + `document.activeElement.id` +
  input selection range at the TOP of the render, then restores
  all three in a `requestAnimationFrame` after the browser paints.
  Only restores if scrollY drifted by more than 10px (so this
  doesn't fight `scrollToTop()` clicks or anchor scrolling).
  Selection range preservation means if you're mid-typing in an
  input when the 30s refresh fires, cursor stays in place + doesn't
  lose focus.

Net effect: the 30s auto-refresh becomes invisible to the user —
cards re-render in place, viewport stays exactly where it was,
in-progress typing isn't interrupted.

**Tests:** Round-44 is UX plumbing (no new pure-logic tests
needed); the 7 existing round-43 wheel_open_backfill tests still
pass. Full suite: **616 passing**. Ruff clean. Dashboard JS
`node --check` clean.

---

## 🆕 Round-46 — Round-45 dual-mode audit fixes + UX polish (2026-04-22)

User ask: *"I merging that now I would like you to audit all the
changes you just made because they are really important and make sure
you were perfect in execution."* Also: *"can you also take (round45)
off the actual app it doesn't look good"* and *"can we also make this
dashboard refresh on a faster rate? make it more real time?"*

Ran a direct code review + spawned a parallel audit Explore agent.
Four real bugs surfaced in round-45 (merged as PR #83). All four are
mode-contamination risks that could cause paper and live to
cross-pollute state. Fixed in this PR + three UX tweaks.

**Audit fixes (CRITICAL → HIGH severity):**

1. **`get_dashboard_data` / `_resolve_user_paths` were mode-unaware.**
   `/api/data` passed `user_id` but never told the dashboard loader
   which mode. When a session switched to live view, the loader
   silently read paper's `dashboard_data.json`, `overlay files`,
   `strategies/` — while the header's Alpaca account data correctly
   came from live. User would see live account equity paired with
   paper positions. Fixed by adding `mode="paper"` param that flows
   through: `/api/data` → `get_dashboard_data(..., mode=)` →
   `_resolve_user_paths(user_id, mode=)` → `auth.user_data_dir(id, mode=)`.

2. **`_wheel_deploy_in_flight` dedup shared between paper + live.**
   `run_wheel_auto_deploy()` used `uid = user.get("id")` as its
   in-flight dedup key. With `live_parallel_enabled=1`, the scheduler
   tick fires the wheel-deploy for paper then live on the same loop;
   whichever ran second would see its `uid` already in the set and
   skip. Fixed: `uid = f"{user['id']}:{_mode}"` for live (paper
   keeps plain `uid` for backward compat with the existing dedup
   pattern in the main scheduler loop).

3. **Alpaca auth-failure alert dedup was mode-blind.**
   `scheduler_api._alert_alpaca_auth_failure` used `_auth_alert_dates[uid]`
   with `uid = user.get("id")`. If paper creds expired first and
   fired the once-per-day alert, a subsequent live-creds-expired on
   the same day would be silenced — so users with live-parallel would
   miss real-money auth alerts. Scoped the dedup key by mode.

4. **Circuit-breaker + rate-limiter buckets shared between paper + live.**
   `scheduler_api._cb_key(user)` returned plain user_id. But paper and
   live hit DIFFERENT Alpaca backends (`paper-api.alpaca.markets` vs
   `api.alpaca.markets`), each with their own 200/min rate budget.
   Sharing the bucket meant a busy paper session could throttle live
   trades and a live CB trip would block paper. Fixed: paper keeps the
   plain-id key (backward compat with persisted in-memory state); live
   gets `"<id>:live"`.

**UX polish:**

5. **Removed `(round-45)` from the Parallel Mode info box.** User
   caught it on mobile and asked to take it off — it's dev-internal
   versioning that doesn't belong in user-facing copy.

6. **Dashboard refresh 60s → 10s.** User asked for a more real-time
   feel. `/api/data` makes ~3 Alpaca calls per refresh; at 10s cadence
   that's ~18 req/min, well under Alpaca's 200/min rate limit. The
   existing `_refreshInFlight` debounce prevents parallel refreshes
   from stacking. Token bucket serializes any rare overlap.

**Tests:** 7 new cases in `tests/test_round46_dual_mode_fixes.py`
pinning all four audit fixes (mode plumbing through
`_resolve_user_paths`, wheel-deploy dedup grep-level pin, auth-alert
dedup grep pin, `_cb_key` paper-vs-live distinctness + back-compat
with plain `user_id` for paper). Suite: **636 passing** (629 + 7).
Ruff clean. Node `--check` clean on dashboard JS.

**Thanks to the audit agent** — caught the wheel-deploy dedup bug I
missed. Zero false positives this time (unlike round-22's trading
agent). Solid run.

---

## 🆕 Round-48 — Cross-user privacy FIX + dashboard jitter (2026-04-22)

User reported TWO critical privacy issues + ongoing dashboard jitter:
*"I am getting emails for my friends trades and I still see him in
my log. Make sure there is 100% data security for users between users
and no risk of PII exposure externally or between users."*

**Root causes found:**

1. **`notify.py` had `EMAIL_RECIPIENT = "se2login@gmail.com"` hardcoded.**
   When `cloud_scheduler.notify_user(user, ...)` spawned the notify.py
   subprocess for godguruselfone's trade, notify.py ignored the user
   context and queued the email with the hardcoded recipient. The
   drainer then shipped it to Kbell0629's inbox. Result: Kbell0629 got
   every user's trade alerts, kill-switch pings, daily summaries.

2. **Shared `DATA_DIR/email_queue.json` file.** notify.py wrote to
   this shared path regardless of which user triggered it. Per-user
   queue isolation existed in the scheduler's `_queue_direct_email`
   but not in notify.py's `queue_email`.

3. **`email_sender.drain_all` drained the shared root queue.** Even
   after fixing #1 and #2, historical pre-round-48 entries in that
   shared file would ship to the hardcoded recipient on the next
   drain pass.

4. **`/api/scheduler-status` with `is_admin=True` returned unfiltered
   activity.** Round-39 filtered non-admins to their own activity but
   explicitly exempted admins. The bootstrap admin (user_id=1, aka
   Kbell0629) saw every user's screener/monitor/deploy events in
   their activity log — exactly what the user reported
   (`[godguruselfone] FSLY: Entry filled at ...` in Kbell0629's log).

**Privacy fixes shipped:**

* `notify.py:EMAIL_RECIPIENT` now reads from `NOTIFICATION_EMAIL` env
  var. Missing → `queue_email` refuses to enqueue (better to drop
  than misroute).
* `cloud_scheduler.notify_user` now sets `env["NOTIFICATION_EMAIL"]`
  AND `env["DATA_DIR"]` per-user before `subprocess.Popen(notify.py)`.
  No-email users → `NOTIFICATION_EMAIL` is popped from env so a
  stale parent-process value doesn't leak between users.
* `email_sender.drain_all` quarantines the shared root queue to
  `DATA_DIR/email_queue.json.pre-round48.dead` instead of draining
  it. Prevents historical cross-user backlog from flushing on next
  drain pass. Also added live-mode queue path (`users/<id>/live/`).
* `/api/scheduler-status` filters admins to their own activity by
  default. Admins who need the full view can pass `?all=1` explicitly
  (admin-panel drill-down future work — for now admins see their
  own trades only, privacy-by-default).

**Dashboard jitter fixes (user reported desktop also jumping + badge
flicker):**

The round-47 sync scroll restore helped but didn't eliminate jitter.
Root cause: every 10s auto-refresh triggered up to **3 wholesale
`renderDashboard()` calls** (initial + wheel-status callback +
news-alerts callback). Each wholesale `app.innerHTML = ...`
caused a repaint + scroll-anchor reset.

* Removed the cascading re-renders. Wheel-status and news-alerts
  fetches now store their data silently; next tick picks up the
  fresh values. 10s staleness on enrichment data is a fair trade
  for a smooth, jump-free dashboard.
* Throttled the `/api/scheduler-status` badge fetch to once per 30s
  (was firing every 10s inside renderDashboard). Also only touches
  the badge DOM when the displayed state actually changed — so
  unchanged ticks trigger zero repaint. Kills the "24/7 LIVE" pulse
  flicker the user called out.

**Tests:** 7 new cases in `tests/test_round48_privacy_fixes.py`
pinning all 4 privacy fixes (queue refuses without env,
honors env recipient, no hardcoded fallback, notify_user passes
per-user env, no-email user doesn't leak stale env, shared queue
quarantined, admin default-filtered). 1 existing test updated to
set `NOTIFICATION_EMAIL` in its setup (test_round14). Suite:
**647 passed, 1 deselected** (CI invocation). Ruff clean. Node
`--check` clean on dashboard JS.

---

## 🆕 Round-47 — Mobile dashboard auto-refresh jitter fix (2026-04-22)

Round-44 added scroll preservation in renderDashboard but used
`requestAnimationFrame` to restore scrollY AFTER the browser
paint. On mobile this caused a visible jump-to-top, then jump-back.
Round-47 restored scroll synchronously right after the wholesale
`app.innerHTML = ...` assignment so the browser bundles the
scrollTo into the same paint. Merged as PR.

---

## 🆕 Round-45 — Dual-mode paper + live parallel trading (2026-04-22)

**User ask:** *"when I switch to real money and I want to run in parallel
with paper the bot on both how do I switch back and forth between both
views paper and live real money… ship option 2 now."*

The existing round-11 live-trading path was single-mode-at-a-time — flip
Settings → Live and the whole bot pivots. Round-45 turns that into
dual-mode: paper and live run side-by-side, each with its own state
tree, and the dashboard has a one-click view toggle.

**Architecture (no migration required — fully backward compatible):**

* **State trees:** `users/<id>/...` remains paper (pre-round-45 behavior
  preserved exactly; no migration touches existing state files).
  `users/<id>/live/` is new — created lazily the first time a user
  enables parallel mode. Wheel state, strategies, trade journal,
  scorecard, guardrails — everything is fully isolated per mode.
* **Session mode:** new `sessions.mode` column (defaults `'paper'`).
  `validate_session` returns it so handlers know which tree to read.
  `set_session_mode(token, mode)` updates it. Legacy NULL rows are
  normalized to `'paper'`.
* **User flag:** new `users.live_parallel_enabled` column. When true
  AND the user has live keys saved, the scheduler expands the user
  into TWO entries per tick (paper + live), running every task on
  each mode independently.

**Endpoints:**

* `POST /api/switch-mode {mode: "paper"|"live"}` — change which tree
  the dashboard reads from. Rejects `'live'` if no live keys are
  configured. Requires a valid session.
* `POST /api/set-live-parallel {enabled: true|false}` — flip the
  scheduler-level parallel mode flag. Requires live keys.

**Dashboard:**

* Header "PAPER" badge is now a clickable mode toggle:
  - 📝 PAPER (orange) when viewing paper
  - 🔴 LIVE (red, glowing) when viewing live
  - Click cycles to the other. If live keys aren't configured, click
    opens Settings → Live Trading tab directly.
* Settings → 🔴 Live Trading tab gets a new "Parallel Mode" section
  with Enable / Disable buttons wired to `/api/set-live-parallel`.
* `/api/data` response includes `session_mode`, `has_live_keys`,
  `live_parallel_enabled` so the header renders with correct state.

**Scheduler (`cloud_scheduler.py`):**

* New helper `_build_user_dict_for_mode(user, mode)` — returns a user
  dict scoped to the requested mode (mode-aware data_dir, correct
  Alpaca keys + endpoint, `_mode` field).
* `get_all_users_for_scheduling()` now expands users into ONE entry
  (paper-only, default) or TWO (paper + live) based on flags.
* Dedup key `uid` includes the mode for live entries (`"1:live"`) so
  paper and live tasks don't stomp each other's daily-stamps /
  interval caches. Paper dedup keys remain unchanged for backward
  compat with existing `_last_runs` data.
* `notify_user` prefixes live-mode notifications with `[LIVE]` so
  ntfy / email recipients can tell real-money events from paper.

**Handler plumbing:**

* New `self.build_scoped_user_dict(mode=None)` on the base handler —
  defaults to the request's session_mode. Used everywhere handlers
  need to call into `cloud_scheduler` / `wheel_strategy`.
* `check_auth` honors session_mode when loading Alpaca creds + sets
  `self.session_mode` for downstream handlers.
* Falls back to paper if the session is 'live' but no live keys are
  saved (prevents a broken dashboard from a misconfigured session).

**Safety rails:**

* Default state: paper-only. Existing users see zero behavior change
  until they explicitly enable parallel mode.
* Saving live keys alone does NOT start live trading — user must
  flip "Enable Parallel Paper + Live" explicitly.
* Live entry in scheduler requires BOTH `live_parallel_enabled=1`
  AND live keys present.
* Session state tree fully isolated: a bug in paper strategy files
  can't contaminate live positions and vice versa.

**Operator workflow for going live:**

1. Save live keys on Settings → Alpaca API tab
2. Open Settings → 🔴 Live Trading → Parallel Mode section → click
   "Enable Parallel Paper + Live"
3. Scheduler picks up the flag on next tick — paper keeps running,
   live starts running alongside
4. Click the 📝 PAPER header badge to view live-tree state (or vice
   versa). Paper + live scorecards, positions, journals all separate.

**Tests:** 13 new cases in `tests/test_round45_dual_mode.py` —
`user_data_dir` mode isolation, session mode defaults, legacy NULL
normalization, credential mode override, scheduler expansion
invariants (paper-only default, both-when-enabled, skip-live-when-
missing-keys). Suite: **629 passing** (616 + 13). Ruff clean.
Node `--check` clean.

---

## 🆕 Round-42 — Wheel close journaling (2026-04-22)

**Motivating case:** CHWY short-put stopped out at $0.35 on Tuesday.
Alpaca's native stop order fired correctly + bought-to-close the put.
But the close never showed up in the dashboard's closed positions /
Today's Closes / scorecard — and the CHWY 260515P position just quietly
disappeared from the Positions table.

**Root cause:** `wheel_strategy.py` updated its own state file + audit
history on every exit path (assigned / expired / bought-to-close /
closed-externally) but **never called `record_trade_close`**. Asymmetric
with the round-33 fix that added `record_trade_open` to `open_short_put`.
Journal ended up with an orphan "open" entry that went stale.

**What shipped:**

* **`_journal_wheel_close(user, contract_meta, exit_price, pnl, reason)`**
  — new helper in `wheel_strategy.py` centralising the boilerplate.
  Uses the OCC contract symbol + `strategy="wheel"` + `side="buy"`
  (short-cover) so `record_trade_close`'s `pnl_pct` math lands in the
  short-cover branch (entry/exit - 1).
* **5 exit paths wired:**
  - `put_assigned` → pnl = premium kept, exit_price = 0
  - `put_expired_worthless` → pnl = premium kept, exit_price = 0
  - `call_assigned` → pnl = option premium (stock P&L separately in
    `total_realized_pnl`), exit_price = 0
  - `call_expired_worthless` → pnl = premium kept, exit_price = 0
  - `{type}_bought_to_close` (profit-target path) → pnl = net_premium,
    exit_price = close_price
* **NEW external-close detection** — the CHWY case. On each tick while
  `status == "active"` and pre-expiration, fetch Alpaca `/positions`.
  If the contract symbol is missing, an external event closed it (native
  stop fired, manual close via Alpaca web UI). Pulls the buy-to-close
  fill price from `/account/activities/FILL?symbol=<OCC>` when
  available. Logs `{type}_closed_externally` audit event, journals the
  close, resets the wheel stage, clears `active_contract`.
* **Gated to pre-expiration only** so it doesn't mis-journal an
  assignment (post-expiry, the option also disappears from positions
  but the dedicated assignment branch handles cost-basis + stage
  transition).

**Once Railway picks up this deploy**, CHWY's wheel file will trigger
the external-close detection on the next scheduler tick and the close
will land in the journal + Today's Closes panel + scorecard.

**Tests:** 6 new cases in `tests/test_round42_wheel_close_journaling.py`
— helper contract, 3 edge cases (missing symbol, swallowed errors,
grep-level exit-path pin), external-close detection fires, external-
close skips when position still open. Suite: **609 passing**
(603 baseline + 6 new). Ruff clean.

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

