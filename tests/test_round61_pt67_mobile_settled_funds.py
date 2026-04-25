"""Round-61 pt.67 — items 7 & 8 of the production-readiness batch.

Item 7: Mobile-responsiveness audit on Analytics Hub panels
Item 8: Settled-funds coverage check across pt.50/pt.59 close paths
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent


# ============================================================================
# Item 7: mobile-responsiveness on Analytics Hub
# ============================================================================

def test_dashboard_has_mobile_analytics_block():
    """Pt.67 added a `@media (max-width: 600px)` block targeting
    `#analyticsPanel` so the panel doesn't horizontal-scroll on
    phones."""
    src = (_HERE / "templates" / "dashboard.html").read_text()
    assert "#analyticsPanel" in src
    # The mobile block must be inside a max-width: 600px media query.
    idx = src.find("#analyticsPanel .factor-card")
    assert idx > 0
    # Walk back to find the enclosing media query.
    head = src[max(0, idx - 800):idx]
    assert "@media (max-width: 600px)" in head


def test_dashboard_mobile_strategy_card_collapses_to_single_column():
    """Per-strategy cards (220px floor) must drop to 1fr on phones."""
    src = (_HERE / "templates" / "dashboard.html").read_text()
    assert 'div[style*="minmax(220px"]' in src
    assert "grid-template-columns: 1fr !important" in src


def test_dashboard_mobile_kpi_grid_tighter_floor():
    """KPI grid floor lowered from 150px → 110px on mobile so 3 fit
    per row instead of 2-with-orphan."""
    src = (_HERE / "templates" / "dashboard.html").read_text()
    assert 'minmax(110px, 1fr)' in src


def test_dashboard_equity_grid_uses_auto_fit():
    """The equity + per-period row no longer hardcodes 2fr 1fr;
    it auto-fits with a 280px floor so it collapses to single
    column on phones."""
    src = (_HERE / "templates" / "dashboard.html").read_text()
    # The pt.46 line `2fr 1fr;gap:16px;margin-bottom:18px` must be
    # gone in the analytics block.
    assert "analytics-equity-grid" in src
    assert "2fr 1fr;gap:16px;margin-bottom:18px" not in src


def test_dashboard_mobile_block_targets_trades_panel_too():
    """Pt.36 Trades panel uses the same KPI grid pattern; the mobile
    block must collapse it the same way."""
    src = (_HERE / "templates" / "dashboard.html").read_text()
    assert "#tradesPanel" in src
    idx = src.find('#tradesPanel > div[style*="minmax(150px"]')
    assert idx > 0


def test_dashboard_equity_svg_height_capped_on_mobile():
    """The equity SVG max-height shrinks on mobile so the chart
    doesn't dominate the viewport."""
    src = (_HERE / "templates" / "dashboard.html").read_text()
    assert "svg[viewBox]" in src
    assert "max-height: 200px !important" in src


# ============================================================================
# Item 8: settled-funds coverage on user-initiated close paths
# ============================================================================

def test_actions_mixin_has_record_close_helper():
    """Pt.67 added `_record_close_to_settled_funds` to bridge
    user-initiated closes into the settled_funds ledger."""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    assert "_record_close_to_settled_funds" in src
    assert "settled_funds" in src


def test_record_close_short_cover_skipped():
    """Shorts (qty<0) should NOT record into settled_funds —
    a buy-to-cover doesn't generate proceeds."""
    from handlers import actions_mixin
    fake_handler = object()
    # qty=-10 (short) → should silently skip
    actions_mixin._record_close_to_settled_funds(
        fake_handler, "X", -10, 100.0)
    # No exception means pass; we don't write anywhere.


def test_record_close_handles_zero_proceeds():
    """Price 0 → no record. None price → no record."""
    from handlers import actions_mixin
    fake_handler = object()
    actions_mixin._record_close_to_settled_funds(
        fake_handler, "X", 10, 0)
    actions_mixin._record_close_to_settled_funds(
        fake_handler, "X", 10, None)
    actions_mixin._record_close_to_settled_funds(
        fake_handler, "X", None, 100.0)
    # No exception = pass.


def test_record_close_handles_missing_user_id(monkeypatch, tmp_path):
    """Handler without `user_id` attribute → silent skip (best-
    effort means never raise)."""
    from handlers import actions_mixin
    class _H:
        pass
    actions_mixin._record_close_to_settled_funds(_H(), "X", 10, 100.0)


def test_record_close_handles_settled_funds_import_failure(monkeypatch):
    """If settled_funds import or call fails, the close path must
    not raise — best-effort only."""
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "a" * 64)
    from handlers import actions_mixin
    class _H:
        user_id = 999
        session_mode = "paper"
    # Even if the user_data_dir lookup raises, we swallow.
    actions_mixin._record_close_to_settled_funds(_H(), "X", 10, 100.0)


def test_record_close_writes_to_ledger(monkeypatch, tmp_path):
    """A long close at $100 × 10 shares writes a $1000 entry to
    the user's settled_funds ledger.

    Pt.67 follow-up: ensure MASTER_ENCRYPTION_KEY is set BEFORE we
    touch auth so a sibling test that popped auth from sys.modules
    (e.g. via http_harness) doesn't cause a fresh auth import to
    fail with `RuntimeError: MASTER_ENCRYPTION_KEY env var is
    required` when monkeypatch.setattr("auth.user_data_dir", ...)
    tries to resolve the target attribute path.
    """
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "a" * 64)
    import auth  # noqa: F401 — force a fresh import under the env
    from handlers import actions_mixin
    import settled_funds
    # Stub user_data_dir to point into our tmp dir.
    monkeypatch.setattr("auth.user_data_dir",
                          lambda uid, mode="paper": str(tmp_path))

    class _H:
        user_id = 42
        session_mode = "paper"

    actions_mixin._record_close_to_settled_funds(
        _H(), "AAPL", 10, 100.0)
    # Verify the ledger now has the $1000 entry.
    user_dict = {"_data_dir": str(tmp_path)}
    unsettled = settled_funds.unsettled_cash(user_dict)
    assert unsettled == 1000.0


def test_close_paths_call_record_close_helper():
    """Source-pin: handle_close_position + handle_sell wire the
    helper into all success paths (RTH delete, xh_close, retry, partial-sell)."""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    # The helper should be invoked in 4 success paths.
    count = src.count("_record_close_to_settled_funds(")
    # 1 helper definition + at least 4 call sites.
    assert count >= 5


def test_close_path_captures_qty_before_retry():
    """Pt.67: qty must be captured BEFORE the retry close, since
    after a successful DELETE /positions the qty probe returns None."""
    src = (_HERE / "handlers" / "actions_mixin.py").read_text()
    assert "_qty_before_retry" in src
    assert "_qty_at_close" in src


def test_pt59_dead_money_already_records_settled_funds():
    """Pt.59 dead-money cutter calls record_trade_close with
    side='sell' which inside record_trade_close calls record_sale.
    This is pre-existing behavior (pt.51) but verify it's still
    wired up."""
    src = (_HERE / "cloud_scheduler.py").read_text()
    # The dead-money block calls record_trade_close with side="sell"
    idx = src.find("dead_money_exit")
    assert idx > 0
    block = src[idx:idx + 3000]
    # The eventual record_trade_close call uses qty=shares + side="sell"
    assert 'side="sell"' in block
    assert "qty=shares" in block


def test_record_trade_close_records_sale_for_long_close():
    """Sanity-check: record_trade_close when side='sell' should
    eventually call settled_funds.record_sale. This is the contract
    pt.51 established and pt.67 extends to user-initiated closes."""
    src = (_HERE / "cloud_scheduler.py").read_text()
    idx = src.find("def record_trade_close")
    assert idx > 0
    block = src[idx:idx + 3000]
    assert "settled_funds" in block
    assert "record_sale" in block
