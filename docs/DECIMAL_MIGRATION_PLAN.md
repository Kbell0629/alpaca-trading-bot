# `float` → `Decimal` migration plan

## Why this exists

Python's `float` is IEEE 754 double. Most decimal fractions (share prices
ending in `.01`, `.05`, `.99`) can't be represented exactly — arithmetic
accumulates microscopic drift. For one trade it's invisible; compounded
across thousands of fills, partial closes, corporate actions, and cost
basis adjustments, the drift surfaces in the pennies column, sometimes
larger.

For this bot specifically the impact is:

- **Form 8949 cost basis / proceeds** must match Alpaca's 1099. Any
  accumulated drift is a tax-reporting discrepancy.
- **Wheel strategy** runs dozens of cycles per symbol. Cost-basis drift
  compounds across assignments + premiums.
- **P&L display** on the dashboard can show `$1,234.56000000000001` in
  exported CSVs when float formatting escapes.
- **Risk/sizing comparisons** can rarely produce off-by-epsilon rejections
  (`if cash >= order_cost:` where both are floats).

Doing the whole refactor at once is unsafe — it's ~200 `float(` sites
spanning the core trading path, and a single wrong boundary conversion
can cause a wrong-qty order to execute. This doc lays out a phased
migration with explicit rollout gates between phases.

## Principles

1. **Decimal is internal; float is at the edges.**
   - IN: every API boundary (Alpaca responses, JSON bodies, env vars)
     parses raw strings into `Decimal` as early as possible. NEVER
     convert `float(...) → Decimal(...)` — that pre-baked the precision
     error. Use `Decimal(str(x))` or `Decimal(x)` on the string itself.
   - OUT: JSON serialisation and HTML display format `Decimal` using
     `quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)` or equivalent.
   - Comparing `Decimal` and `float` in Python 3 raises `TypeError` on
     some ops. Every internal comparison must be Decimal↔Decimal.

2. **Currency rounding: banker's (`ROUND_HALF_EVEN`) at display time only.**
   Internal math is exact. Only the final write (JSON/HTML/CSV) rounds.

3. **One module at a time, fully tested, fully deployed, then the next.**
   Each phase ends with paper-trading for at least a week before
   advancing. If lifetime P&L in the scorecard changes by more than
   `$1` relative to the pre-phase baseline, PAUSE and investigate.

4. **Never quantize mid-computation.**
   E.g. computing average cost basis: sum (`qty * price`), sum qty, then
   divide — don't round `qty * price` to 2 dp mid-loop.

## Phase-by-phase plan

### Phase 1 — `tax_lots.py` (first PR)

**Risk**: LOW. `tax_lots.py` is a pure read-only computation. Its output
feeds `/api/tax-report` + `/api/tax-report.csv` — neither places an
order. A bug here is visible (wrong numbers in a report) but not
financially catastrophic.

**Scope**:

- Internal representation: `Decimal` everywhere inside `compute_tax_lots`.
- Entry points accept existing dict-shaped trades (which have `float`
  values from the journal); convert to `Decimal` at load using
  `Decimal(str(v))` to avoid double-precision contamination.
- Output `cost_basis`, `proceeds`, `gain_loss` fields serialise back to
  `float` in the return dict so downstream consumers (JSON) are
  unchanged.
- Quantize money fields to `Decimal("0.01")` before emitting.
- Preserve existing column names and field types — this is a
  behaviour-neutral change from the caller's POV.

**Tests** (new `tests/test_tax_lots_decimal.py`):

- Round-trip: known trades → expected cost basis (exact to the cent).
- Long chain: 52 wheel cycles on one symbol. Drift vs. naive float impl
  must be <= $0.01. Existing float impl drifts more than that.
- FIFO vs LIFO determinism: same trades, each method, stable outputs.
- Partial fills: opening 100 shares in 3 chunks, closing in 2 — basis
  math reconciles.
- Empty journal, single-side (all buys, no closes), cross-symbol
  independence — all regression-guard tests.
- Golden-master test: capture tax-lot output from current `main` for a
  fixture journal, assert new impl matches to the cent.

**Rollout gate**:

- Deploy to Railway paper env.
- Export `/api/tax-lots.csv` and compare line-by-line to the pre-deploy
  CSV. Accept if every row matches to the cent. Escalate if any row
  drifts by >$0.01.

### Phase 2 — `update_scorecard.py` (second PR)

**Risk**: LOW-MEDIUM. Like phase 1, read-only — computes metrics,
doesn't trade. But it's frequently called (on every scheduler tick)
and its outputs drive the dashboard + scorecard email.

**Scope**:

- Internal sum/avg/win-rate math in `calculate_metrics` uses `Decimal`.
- `strategy_breakdown` totals use `Decimal` then serialise to rounded
  `float` on output.
- `take_snapshot` leaves daily-snapshot fields as float (those are
  already rounded at write time and have no compounding drift).
- Keep existing JSON schema unchanged.

**Tests**:

- Golden-master: replay a fixture journal through old + new, diff the
  scorecard outputs. Expected: totals match to the cent; per-strategy
  breakdowns match to the cent.
- Long-horizon: synthetic 5-year trade stream, compare lifetime PnL
  between float + Decimal paths.

**Rollout gate**:

- 7-day paper deploy, compare scorecard.json before/after the swap for
  the same user. No strategy-breakdown row should differ by more than
  $0.01.

### Phase 3 — `risk_sizing.py`, `portfolio_risk.py` (third PR)

**Risk**: MEDIUM. These feed position-sizing decisions. A subtle bug
could cause over- or under-sized orders.

**Scope**:

- ATR calculation, volatility scaling, position-size output — all
  `Decimal`-internal.
- Output is a share-count integer which is unaffected, but the
  intermediate math gets rounded at known boundaries (volatility
  multiplier quantized to 3dp, stop price to cent).

**Tests**:

- Known-fixture ATR: five bar sets with expected ATR to 4 decimal
  places. Assert exact match post-migration.
- Position-sizing boundary: exercise the rounding edge where
  old/new impls would return different integer qty. Document any
  differences explicitly.

**Rollout gate**:

- Run the screener + sizer through a paper day. Compare sized-qty
  output to pre-phase baseline. Differences MUST be zero for the same
  input bars — if not, there's a deeper issue than rounding.

### Phase 4 — `wheel_strategy.py` (fourth PR)

**Risk**: HIGH. Wheel cycles chain cost basis across many legs
(put-sell → assignment → call-sell → assignment or expiration → …).
Drift compounds. This is also where actual orders are placed.

**Scope**:

- Cost basis, premium collection, net P&L fields migrate to Decimal.
- Strike price + premium comparisons (`premium > min_premium`)
  Decimal↔Decimal.
- Order-placement path accepts the existing float-typed Alpaca
  response but converts defensively.

**Tests**:

- Synthetic 52-cycle wheel: expected cost basis after 52 cycles vs
  naive float. Decimal impl matches exact analytical value; float
  drifts by >$0.50 typically.
- Edge cases: assignment at strike (zero premium loss), expiry at max
  profit, early close at 50%.
- Live paper integration: run one real cycle on a $5-$10 stock for a
  week; compare every journal entry's pnl field to hand-computed.

**Rollout gate**:

- Two weeks paper. No wheel cycle's recorded PnL may differ by >$0.01
  from the same cycle run under the old code on the same inputs.

### Phase 5 — `smart_orders.py`, fills reconciliation (fifth PR)

**Risk**: HIGHEST. This is the order-placement path. A wrong qty here
= real money lost.

**Scope**:

- Position-size math in `compute_order_qty` Decimal-internal.
- Quantize-to-int conversion explicit and tested.
- Reconciliation paths (fill confirmations, partial-fill aggregation)
  Decimal.

**Tests**:

- Fuzz test: random cash balances, random target percentages, assert
  old float path and new Decimal path produce IDENTICAL qty outputs.
  Any diff pauses the migration.
- Specific edge cases: $0 cash, exactly one share affordable, very
  high price (SPX), fractional-share boundary.

**Rollout gate**:

- One week paper with the fuzzer running on every tick.
- Switchover to live only after zero-diff confirmation.

## Out of scope (won't migrate)

- Display formatting (HTML, CSV) — these quantize from Decimal at
  emit time; internal type stays float for serialization boundary.
- `float(` inside parsing of Alpaca API responses (`float(acct.get("cash"))`)
  remains the parse boundary — but each consumer wraps it in
  `Decimal(str(float(...)))` before compounding.
- Statistical computations in `update_scorecard` (Sharpe, Sortino, max
  drawdown) — those are unitless ratios, drift is immaterial.

## Known gotchas

1. `Decimal("1.0") == 1.0` is `True`, but `Decimal("1.0") < 1.1` raises
   `TypeError`. Any mixed comparison in the existing code becomes a
   latent bug waiting for the wrong type to be passed in.
2. `sum(decimals)` needs a `Decimal("0")` start value — otherwise the
   default int `0` is fine but `sum(decimals, 0.0)` raises.
3. JSON: `json.dumps(Decimal("1.0"))` raises unless you pass a custom
   encoder. Always convert at the serialisation boundary:
   `float(x.quantize(Decimal("0.01")))`.
4. `Decimal(str(float_x))` vs `Decimal(float_x)`: the first gives you
   the human-readable form (e.g. `Decimal("0.1")`), the second gives
   you the exact binary (`Decimal("0.1000000000000000055511...")`).
   Always use `Decimal(str(...))` when crossing from float.

## Rollback plan (applies to every phase)

- Each phase ships as an isolated PR with its own feature branch.
- If the rollout gate detects drift beyond tolerance, revert the PR
  via `git revert` → PR → merge. No state changes are irreversible
  (the journal format is unchanged; archived trades are untouched).
- Keep the float-only versions of critical functions in git history
  under their original commit SHAs — easy to cherry-pick back.
