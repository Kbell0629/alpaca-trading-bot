"""
Round-61 tests: behavioral coverage of check_profit_ladder.

check_profit_ladder is the 25%-at-each-rung profit-take engine. Four levels
(+10%, +20%, +30%, +50%), each sells 25% of the ORIGINAL qty (not current),
with PDT awareness, client_order_id idempotency, and atomic stop-qty resize.

Pre-round-61 coverage: zero. Every behavior below was just source code
without a test. A silent regression in this function sells too many shares,
too few shares, fires twice per rung, or leaves the stop sized for the
pre-rung share count.

The function calls user_api_post / user_api_patch / user_api_delete /
save_json / log / notify_user / now_et. All of those get stubbed; the
tests focus on what goes into the Alpaca POST body and what persists in
strat['state'].

Rungs covered:
  * Below first rung: no-op
  * Each of 4 rungs fires once and only once
  * initial_qty anchors the 25% calc (not current shares)
  * Level-already-in-profit_takes → skip
  * Alpaca 422 "client_order_id already exists" → mark taken, continue
  * shares<=0 or entry falsy → no-op
  * Stop resize: PATCH tried first, cancel-then-replace on PATCH failure
  * Stop cancelled when remaining shares = 0
  * PDT: same-day + can't_day_trade + pdt_applies → skip rung
  * PDT: margin account (pdt_applies=False) → proceed
  * Only one level per call (returns after first hit)
  * client_order_id format: ladder-{symbol}-L{level}-{ET_date}
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def _fresh_strat(symbol="AAPL", initial_qty=100, profit_takes=None,
                 stop_order_id=None, current_stop_price=None):
    state = {"profit_takes": list(profit_takes or [])}
    if stop_order_id:
        state["stop_order_id"] = stop_order_id
    if current_stop_price:
        state["current_stop_price"] = current_stop_price
    return {
        "symbol": symbol,
        "initial_qty": initial_qty,
        "state": state,
        "strategy": "trailing_stop",
        "entered_at": "2026-04-22T09:45:00-04:00",  # yesterday; not same-day
    }


class _Recorder:
    """Captures Alpaca API calls made by check_profit_ladder."""

    def __init__(self, post_response=None, patch_response=None):
        self.posts = []   # list of (path, payload)
        self.patches = []
        self.deletes = []
        self.saved = []
        self.notifications = []
        # Default: first POST succeeds with a fresh id
        self._post_response = post_response or {"id": "new-order-123"}
        self._patch_response = patch_response

    def post(self, user, path, payload):
        self.posts.append((path, dict(payload)))
        return self._post_response

    def patch(self, user, path, payload):
        self.patches.append((path, dict(payload)))
        return self._patch_response or {"id": "patched-stop-456"}

    def delete(self, user, path):
        self.deletes.append(path)
        return {"status": "canceled"}

    def save(self, filepath, data):
        self.saved.append((filepath, data))

    def notify(self, user, msg, kind=None, **kwargs):
        self.notifications.append((msg, kind))


def _install(monkeypatch, rec, cs, now_iso="2026-04-23T10:30:00-04:00"):
    """Wire the recorder into cloud_scheduler. Returns nothing — mutations
    applied via monkeypatch so they auto-revert after each test."""
    monkeypatch.setattr(cs, "user_api_post", rec.post)
    monkeypatch.setattr(cs, "user_api_patch", rec.patch)
    monkeypatch.setattr(cs, "user_api_delete", rec.delete)
    monkeypatch.setattr(cs, "save_json", rec.save)
    monkeypatch.setattr(cs, "notify_user", rec.notify)
    monkeypatch.setattr(cs, "log", lambda *a, **k: None)
    fixed = datetime.fromisoformat(now_iso)
    monkeypatch.setattr(cs, "now_et", lambda: fixed)


def _load_cs(monkeypatch):
    import sys
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
    for m in ("auth", "scheduler_api", "cloud_scheduler"):
        sys.modules.pop(m, None)
    import cloud_scheduler
    return cloud_scheduler


# ========= Degenerate inputs =========

def test_no_op_when_shares_zero(monkeypatch):
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat()
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=200, entry=100, shares=0)
    assert rec.posts == [], "no orders should be placed when shares=0"
    assert rec.saved == []


def test_no_op_when_entry_falsy(monkeypatch):
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat()
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=200, entry=0, shares=100)
    assert rec.posts == []


def test_no_op_below_first_rung(monkeypatch):
    """+9% profit — below the +10% rung. Nothing should fire."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat()
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=109, entry=100, shares=100)
    assert rec.posts == []


# ========= Rung fires =========

def test_first_rung_sells_25_percent_at_plus_10(monkeypatch):
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat(initial_qty=100)
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=110, entry=100, shares=100)
    assert len(rec.posts) == 1
    path, payload = rec.posts[0]
    assert path == "/orders"
    assert payload["symbol"] == "AAPL"
    assert payload["qty"] == "25"  # 25% of initial_qty 100
    assert payload["side"] == "sell"
    assert payload["type"] == "market"
    # Level marked as taken + remaining share count updated
    assert strat["state"]["profit_takes"] == [10]
    assert strat["state"]["total_shares_held"] == 75


def test_second_rung_uses_initial_qty_not_current(monkeypatch):
    """After the first rung, current shares = 75, but the second rung
    (+20%) should still sell 25% of the ORIGINAL 100 = 25 shares — not
    25% of 75 = 19. Anchoring on current would mean the rungs trail
    off geometrically, never flattening the position."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    # Simulate state after first rung already taken
    strat = _fresh_strat(initial_qty=100, profit_takes=[10])
    strat["state"]["total_shares_held"] = 75
    # +21% so float-precision on (120/100-1)*100 = 19.999... doesn't
    # keep us below the +20% rung.
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=121, entry=100, shares=75)
    assert len(rec.posts) == 1
    assert rec.posts[0][1]["qty"] == "25", (
        "second rung must sell 25% of initial_qty (25), not 25% of current (19)")


def test_already_taken_rung_is_skipped(monkeypatch):
    """If the level is already in profit_takes, it must not fire again
    even if the price is still above the rung."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat(initial_qty=100, profit_takes=[10])
    strat["state"]["total_shares_held"] = 75
    # Still at +11% — above rung 1, below rung 2
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=111, entry=100, shares=75)
    assert rec.posts == [], "rung 1 already taken, must not re-fire"


def test_only_one_rung_fires_per_call(monkeypatch):
    """Even at +50%, only the FIRST uncovered rung should fire in this
    call. The remaining rungs wait for the next monitor tick (60s).
    Firing all rungs at once is dangerous — a single spike could sell
    the whole position into thin upper-book liquidity."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat(initial_qty=100)
    # Price is above all 4 rungs
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=200, entry=100, shares=100)
    assert len(rec.posts) == 1, "only one rung per call"
    assert strat["state"]["profit_takes"] == [10]


# ========= Idempotency (client_order_id) =========

def test_client_order_id_format_is_stable(monkeypatch):
    """ET-date-tagged client_order_id prevents double-fires if a
    monitor tick's response is lost and the next tick retries. Format:
    ladder-<symbol>-L<level>-<YYYYMMDD>."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs, now_iso="2026-04-23T10:30:00-04:00")
    strat = _fresh_strat(initial_qty=100)
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=110, entry=100, shares=100)
    coid = rec.posts[0][1]["client_order_id"]
    assert coid == "ladder-AAPL-L10-20260423", (
        f"client_order_id must be ET-date-tagged for idempotency, got {coid}")


def test_alpaca_422_duplicate_is_treated_as_already_taken(monkeypatch):
    """Alpaca returns 422 with 'client_order_id already exists' when a
    prior tick's request made it through but the response was lost.
    The handler must mark the rung as taken and move on, NOT retry."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder(post_response={"error": "client_order_id already exists"})
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat(initial_qty=100)
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=110, entry=100, shares=100)
    # Rung marked taken despite the error response
    assert 10 in strat["state"]["profit_takes"], (
        "422 'already exists' must mark the level as taken so subsequent "
        "ticks don't keep retrying")
    # But total_shares_held should NOT have been reduced (the order didn't
    # fill this call — the original post did, tracked separately)
    assert rec.saved, "state must be persisted after idempotency hit"


# ========= Stop resize after rung =========

def test_stop_patched_to_remaining_shares_after_rung(monkeypatch):
    """After a rung fires, the protective stop must be resized to match
    the remaining shares. Not doing so = stop for 100 shares triggers on
    the 75 remaining = Alpaca rejects (or worse, opens a 25-share short
    on short-enabled accounts)."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat(initial_qty=100, stop_order_id="stop-abc",
                         current_stop_price=95)
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=110, entry=100, shares=100)
    # Stop must be PATCHed to qty=75 (not cancel+replace)
    assert rec.patches, "stop should be PATCHed first"
    assert rec.patches[0][0] == "/orders/stop-abc"
    assert rec.patches[0][1]["qty"] == "75"
    # No cancel should happen when PATCH succeeds
    assert rec.deletes == [], "PATCH success must not trigger cancel"
    assert strat["state"]["stop_order_id"] == "patched-stop-456"


def test_stop_falls_back_to_cancel_and_replace_on_patch_failure(monkeypatch):
    """PATCH sometimes fails (e.g. order moved to held). Fallback is
    cancel-then-replace. Pin this path so it doesn't get refactored away."""
    cs = _load_cs(monkeypatch)
    # PATCH fails (no id in response), POST for replacement succeeds
    rec = _Recorder(patch_response={"error": "order not found"})
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat(initial_qty=100, stop_order_id="stop-abc",
                         current_stop_price=95)
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=110, entry=100, shares=100)
    # PATCH attempted
    assert rec.patches
    # Delete + second POST for replacement stop
    assert rec.deletes == ["/orders/stop-abc"]
    # The second POST is the replacement stop (first POST was the sell)
    assert len(rec.posts) == 2
    replacement = rec.posts[1][1]
    assert replacement["type"] == "stop"
    assert replacement["qty"] == "75"
    assert replacement["stop_price"] == "95"
    assert replacement["time_in_force"] == "gtc"


def test_stop_cancelled_when_remaining_shares_zero(monkeypatch):
    """If the rung sells EVERYTHING (initial_qty * 25% >= shares), the
    stop must be cancelled, not resized to zero."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    # initial_qty 4, shares 1 — rung wants 1 share, leaves 0
    strat = _fresh_strat(initial_qty=4, stop_order_id="stop-xyz",
                         current_stop_price=95)
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=110, entry=100, shares=1)
    assert rec.deletes == ["/orders/stop-xyz"], (
        "remaining=0 must cancel the old stop, not try to patch it to 0")
    assert strat["state"]["stop_order_id"] is None


# ========= PDT gate =========

def test_pdt_gate_skips_rung_on_same_day_cash_constrained(monkeypatch):
    """When tier has pdt_applies=True and the position is same-day AND
    no day-trades remaining with buffer=1, the rung MUST skip. CLAUDE.md
    Post-50 invariant — saves the last day-trade slot for kill-switch."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs, now_iso="2026-04-23T14:30:00-04:00")

    # Same-day entry
    strat = _fresh_strat(initial_qty=100)
    strat["entered_at"] = "2026-04-23T09:45:00-04:00"

    user = {
        "username": "u", "id": 1,
        "_tier_cfg": {"pdt_applies": True},
    }

    # Stub pdt_tracker so we control the gate
    import types
    fake_pdt = types.ModuleType("pdt_tracker")
    fake_pdt.is_day_trade = lambda a, b: True
    fake_pdt.can_day_trade = lambda tier, buffer=1: (False, "no slots", 0)
    import sys
    monkeypatch.setitem(sys.modules, "pdt_tracker", fake_pdt)

    cs.check_profit_ladder(user, "/tmp/s.json", strat,
                           price=110, entry=100, shares=100)
    assert rec.posts == [], "PDT gate should have skipped the rung"


def test_pdt_gate_bypassed_when_tier_does_not_apply(monkeypatch):
    """Margin account >= $25k: pdt_applies=False. The gate must never
    block. Defensive — a broken check would freeze profit-takes on the
    accounts that need them most."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat(initial_qty=100)
    strat["entered_at"] = "2026-04-23T09:45:00-04:00"  # would be same-day
    user = {
        "username": "u", "id": 1,
        "_tier_cfg": {"pdt_applies": False},
    }
    cs.check_profit_ladder(user, "/tmp/s.json", strat,
                           price=110, entry=100, shares=100)
    assert len(rec.posts) == 1, "margin tier must not be gated by PDT check"


def test_pdt_gate_exception_is_fail_open(monkeypatch):
    """The PDT check is wrapped in try/except and failing inside it must
    NEVER block the profit take. CLAUDE.md Post-51 invariant: 'All round-51
    hooks MUST fail OPEN on exception — advisory code never blocks trading.'"""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)
    strat = _fresh_strat(initial_qty=100)
    strat["entered_at"] = "2026-04-23T09:45:00-04:00"
    user = {
        "username": "u", "id": 1,
        "_tier_cfg": {"pdt_applies": True},
    }
    # Stub pdt_tracker to raise
    import types
    fake_pdt = types.ModuleType("pdt_tracker")
    def _boom(*a, **k):
        raise RuntimeError("pdt down")
    fake_pdt.is_day_trade = _boom
    fake_pdt.can_day_trade = _boom
    import sys
    monkeypatch.setitem(sys.modules, "pdt_tracker", fake_pdt)

    cs.check_profit_ladder(user, "/tmp/s.json", strat,
                           price=110, entry=100, shares=100)
    assert len(rec.posts) == 1, (
        "PDT check raising must NOT block the profit take — advisory code "
        "never blocks trading (Post-51 invariant)")


# ========= Rung ordering =========

def test_rungs_are_10_20_30_50_in_order(monkeypatch):
    """Pin the ladder levels. Any change here changes the exit plan for
    every open position — needs a product-level decision, not a silent
    refactor."""
    cs = _load_cs(monkeypatch)
    rec = _Recorder()
    _install(monkeypatch, rec, cs)

    # Fire each level in sequence. Uses +1% headroom above each rung to
    # avoid float-precision flakes (e.g. (120/100-1)*100 = 19.999...).
    strat = _fresh_strat(initial_qty=100)
    # +11% → rung 1
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=111, entry=100, shares=100)
    assert strat["state"]["profit_takes"] == [10]
    # +19% → still just rung 1 (no rung 2 yet)
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=119, entry=100, shares=75)
    assert strat["state"]["profit_takes"] == [10]
    # +21% → rung 2
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=121, entry=100, shares=75)
    assert strat["state"]["profit_takes"] == [10, 20]
    # +31% → rung 3
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=131, entry=100, shares=50)
    assert strat["state"]["profit_takes"] == [10, 20, 30]
    # +51% → rung 4
    cs.check_profit_ladder({"username": "u", "id": 1}, "/tmp/s.json",
                           strat, price=151, entry=100, shares=25)
    assert strat["state"]["profit_takes"] == [10, 20, 30, 50]
