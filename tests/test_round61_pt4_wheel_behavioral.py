"""
Round-61 pt.4: BEHAVIORAL coverage for wheel_strategy helpers.

The pt.2 grep-pin tests pin state-machine invariants at the source level.
This file exercises the actual helper functions end-to-end:
  * log_history — history cap, timestamp, stage tagging
  * has_earnings_soon — earnings_warning flag + days_to_earnings check
  * score_contract — delta/DTE/premium/liquidity scoring
  * find_wheel_candidates — picks filter + sort by wheel_score
  * options_trading_allowed — account options approval level
  * cash_covered — cash sufficiency for CSP assignment
  * count_active_wheels — active wheel counter
  * _journal_wheel_close — journal write for exits

Complements the pt.2 grep-pins: together they catch both "math changed"
AND "guard removed."
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta


def _user(tmp):
    d = os.path.join(tmp, "user1")
    strategies = os.path.join(d, "strategies")
    os.makedirs(strategies, exist_ok=True)
    return {
        "id": 1, "username": "alice",
        "_data_dir": d, "_strategies_dir": strategies,
        "_api_key": "k", "_api_secret": "s",
        "_api_endpoint": "https://paper-api.alpaca.markets/v2",
        "_data_endpoint": "https://data.alpaca.markets/v2",
    }


# ========= log_history =========

class TestLogHistory:
    def test_log_history_appends_entry(self):
        import wheel_strategy as ws
        state = {"stage": "stage_1_searching", "history": []}
        ws.log_history(state, "tick", {"note": "hi"})
        assert len(state["history"]) == 1
        e = state["history"][0]
        assert e["event"] == "tick"
        assert e["stage"] == "stage_1_searching"
        assert e["detail"] == {"note": "hi"}
        assert "ts" in e

    def test_log_history_creates_list_if_missing(self):
        import wheel_strategy as ws
        state = {"stage": "stage_1_searching"}
        ws.log_history(state, "first_event")
        assert len(state["history"]) == 1

    def test_log_history_caps_at_max(self):
        import wheel_strategy as ws
        state = {"stage": "stage_1_searching", "history": []}
        for i in range(ws.HISTORY_MAX + 50):
            ws.log_history(state, "spam", {"i": i})
        assert len(state["history"]) == ws.HISTORY_MAX
        # Earliest entries should be dropped
        last_event = state["history"][-1]
        assert last_event["detail"]["i"] == ws.HISTORY_MAX + 49


# ========= has_earnings_soon =========

class TestHasEarningsSoon:
    def test_earnings_warning_flag_true(self):
        import wheel_strategy as ws
        assert ws.has_earnings_soon({"earnings_warning": True}) is True

    def test_within_days_window_true(self):
        import wheel_strategy as ws
        assert ws.has_earnings_soon(
            {"days_to_earnings": ws.EARNINGS_AVOID_DAYS}) is True
        assert ws.has_earnings_soon({"days_to_earnings": 0}) is True

    def test_outside_window_false(self):
        import wheel_strategy as ws
        assert ws.has_earnings_soon({
            "days_to_earnings": ws.EARNINGS_AVOID_DAYS + 1}) is False
        # Negative days (earnings already past)
        assert ws.has_earnings_soon({"days_to_earnings": -3}) is False

    def test_non_numeric_days_false(self):
        import wheel_strategy as ws
        assert ws.has_earnings_soon({"days_to_earnings": "soon"}) is False
        assert ws.has_earnings_soon({"days_to_earnings": None}) is False

    def test_empty_pick_false(self):
        import wheel_strategy as ws
        assert ws.has_earnings_soon({}) is False


# ========= find_wheel_candidates =========

class TestFindWheelCandidates:
    def test_empty_picks(self):
        import wheel_strategy as ws
        assert ws.find_wheel_candidates({}) == []
        assert ws.find_wheel_candidates({"picks": []}) == []

    def test_filters_non_wheel_strategy(self):
        import wheel_strategy as ws
        picks = {
            "picks": [
                {"symbol": "AAPL", "best_strategy": "Breakout",
                 "price": 30.0, "wheel_score": 80},
                {"symbol": "MSFT", "best_strategy": "Wheel Strategy",
                 "price": 30.0, "wheel_score": 70},
            ]
        }
        cands = ws.find_wheel_candidates(picks)
        assert len(cands) == 1
        assert cands[0]["symbol"] == "MSFT"

    def test_filters_price_out_of_range(self):
        import wheel_strategy as ws
        picks = {
            "picks": [
                {"symbol": "PENNY", "best_strategy": "Wheel Strategy",
                 "price": ws.MIN_STOCK_PRICE - 1, "wheel_score": 90},
                {"symbol": "EXPENSIVE", "best_strategy": "Wheel Strategy",
                 "price": ws.MAX_STOCK_PRICE + 1, "wheel_score": 90},
                {"symbol": "OK", "best_strategy": "Wheel Strategy",
                 "price": (ws.MIN_STOCK_PRICE + ws.MAX_STOCK_PRICE) / 2,
                 "wheel_score": 50},
            ]
        }
        cands = ws.find_wheel_candidates(picks)
        symbols = {c["symbol"] for c in cands}
        assert "PENNY" not in symbols
        assert "EXPENSIVE" not in symbols
        assert "OK" in symbols

    def test_filters_earnings_soon(self):
        import wheel_strategy as ws
        picks = {
            "picks": [
                {"symbol": "UPCOMING", "best_strategy": "Wheel Strategy",
                 "price": 25.0, "wheel_score": 90,
                 "days_to_earnings": 1},
                {"symbol": "CLEAR", "best_strategy": "Wheel Strategy",
                 "price": 25.0, "wheel_score": 70,
                 "days_to_earnings": 45},
            ]
        }
        cands = ws.find_wheel_candidates(picks)
        symbols = {c["symbol"] for c in cands}
        assert "UPCOMING" not in symbols
        assert "CLEAR" in symbols

    def test_sorts_by_wheel_score_desc(self):
        import wheel_strategy as ws
        picks = {
            "picks": [
                {"symbol": "LOW", "best_strategy": "Wheel Strategy",
                 "price": 25.0, "wheel_score": 30},
                {"symbol": "HIGH", "best_strategy": "Wheel Strategy",
                 "price": 25.0, "wheel_score": 80},
                {"symbol": "MID", "best_strategy": "Wheel Strategy",
                 "price": 25.0, "wheel_score": 50},
            ]
        }
        cands = ws.find_wheel_candidates(picks)
        assert [c["symbol"] for c in cands] == ["HIGH", "MID", "LOW"]

    def test_respects_max_candidates(self):
        import wheel_strategy as ws
        picks = {"picks": [
            {"symbol": f"S{i}", "best_strategy": "Wheel Strategy",
             "price": 25.0, "wheel_score": i}
            for i in range(30)
        ]}
        cands = ws.find_wheel_candidates(picks, max_candidates=5)
        assert len(cands) == 5

    def test_falls_back_to_best_score(self):
        import wheel_strategy as ws
        picks = {
            "picks": [
                {"symbol": "A", "best_strategy": "Wheel Strategy",
                 "price": 25.0, "best_score": 80},
                {"symbol": "B", "best_strategy": "Wheel Strategy",
                 "price": 25.0, "best_score": 40},
            ]
        }
        cands = ws.find_wheel_candidates(picks)
        assert cands[0]["symbol"] == "A"


# ========= options_trading_allowed =========

class TestOptionsTradingAllowed:
    def test_approval_level_2_allowed(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"options_approved_level": "2"})
        ok, reason = ws.options_trading_allowed({})
        assert ok is True

    def test_approval_level_3_allowed(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"options_approved_level": "3"})
        ok, _ = ws.options_trading_allowed({})
        assert ok is True

    def test_approval_level_1_blocked(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"options_approved_level": "1"})
        ok, reason = ws.options_trading_allowed({})
        assert ok is False
        assert "level" in reason.lower()

    def test_missing_level_treated_as_zero(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get", lambda u, p: {})
        ok, _ = ws.options_trading_allowed({})
        assert ok is False

    def test_api_error_blocked(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"error": "network"})
        ok, reason = ws.options_trading_allowed({})
        assert ok is False
        assert "failed" in reason.lower() or "error" in reason.lower()

    def test_bad_level_value_treated_as_zero(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"options_approved_level": "bogus"})
        ok, _ = ws.options_trading_allowed({})
        assert ok is False


# ========= cash_covered =========

class TestCashCovered:
    def test_sufficient_cash_allows(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"cash": "50000"})
        # Strike $100 × 100 shares = $10k, we have $50k
        ok, _ = ws.cash_covered({}, strike=100, qty=1)
        assert ok is True

    def test_insufficient_cash_blocks(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"cash": "5000"})
        ok, reason = ws.cash_covered({}, strike=100, qty=1)
        assert ok is False
        assert "cash" in reason.lower()

    def test_multiple_contracts_scale_requirement(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"cash": "15000"})
        # Strike $100 × 100 × qty 2 = $20k, we have $15k
        ok, _ = ws.cash_covered({}, strike=100, qty=2)
        assert ok is False

    def test_api_error_blocks(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"error": "down"})
        ok, reason = ws.cash_covered({}, strike=50)
        assert ok is False
        assert "failed" in reason.lower() or "error" in reason.lower()

    def test_bad_cash_value_treated_as_zero(self, monkeypatch):
        import wheel_strategy as ws
        monkeypatch.setattr(ws, "_api_get",
                             lambda u, p: {"cash": "n/a"})
        ok, _ = ws.cash_covered({}, strike=50)
        assert ok is False


# ========= score_contract =========

class TestScoreContract:
    def test_rejects_bad_keys(self):
        import wheel_strategy as ws
        assert ws.score_contract({}, {"bid": 1.0}, 100, 100, "put") is None

    def test_rejects_low_open_interest(self):
        import wheel_strategy as ws
        contract = {
            "strike_price": "100",
            "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
            "open_interest": ws.MIN_OPEN_INTEREST - 1,
        }
        assert ws.score_contract(contract, {"bid": 1.0}, 100, 100, "put") is None

    def test_rejects_zero_premium(self):
        import wheel_strategy as ws
        contract = {
            "strike_price": "100",
            "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
            "open_interest": 500,
        }
        assert ws.score_contract(contract, {"bid": 0}, 100, 100, "put") is None

    def test_rejects_thin_premium_pct(self):
        import wheel_strategy as ws
        contract = {
            "strike_price": "100",
            "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
            "open_interest": 500,
        }
        # premium 0.01 / strike 100 = 0.0001 — below MIN_PREMIUM_PCT
        assert ws.score_contract(contract, {"bid": 0.01}, 100, 100, "put") is None

    def test_valid_put_returns_score(self):
        import wheel_strategy as ws
        contract = {
            "strike_price": "95",
            "expiration_date": (date.today() + timedelta(days=ws.TARGET_DTE)).isoformat(),
            "open_interest": 2000,
            "implied_volatility": "0.30",
        }
        quote = {"bid": 1.50, "ask": 1.60}
        score = ws.score_contract(contract, quote, target_strike=95,
                                    current_price=100, opt_type="put")
        assert score is not None
        assert score > 0

    def test_valid_call_returns_score(self):
        import wheel_strategy as ws
        contract = {
            "strike_price": "105",
            "expiration_date": (date.today() + timedelta(days=ws.TARGET_DTE)).isoformat(),
            "open_interest": 2000,
            "implied_volatility": "0.30",
        }
        quote = {"bid": 1.00, "ask": 1.10}
        score = ws.score_contract(contract, quote, target_strike=105,
                                    current_price=100, opt_type="call")
        assert score is not None
        assert score > 0


# ========= count_active_wheels =========

class TestCountActiveWheels:
    def test_empty_strategies_dir(self, tmp_path):
        import wheel_strategy as ws
        user = _user(str(tmp_path))
        assert ws.count_active_wheels(user) == 0

    def test_counts_non_searching_stages(self, tmp_path):
        import wheel_strategy as ws
        user = _user(str(tmp_path))
        # Create wheel files at various stages
        sdir = user["_strategies_dir"]
        stages = {
            "wheel_AAPL.json": "stage_1_put_active",
            "wheel_MSFT.json": "stage_2_shares_owned",
            "wheel_NVDA.json": "stage_2_call_active",
            "wheel_GOOGL.json": "stage_1_searching",  # should NOT count
        }
        for fname, stage in stages.items():
            with open(os.path.join(sdir, fname), "w") as f:
                json.dump({"symbol": fname.split("_")[1].replace(".json", ""),
                            "strategy": "wheel",
                            "stage": stage}, f)
        # Active count = 3 (all but stage_1_searching)
        assert ws.count_active_wheels(user) == 3


# ========= _journal_wheel_close =========

class TestJournalWheelClose:
    def test_writes_trade_journal(self, tmp_path, monkeypatch):
        import wheel_strategy as ws
        user = _user(str(tmp_path))
        # record_trade_close is imported LOCALLY inside _journal_wheel_close
        # from cloud_scheduler (not trade_journal). Stub it there.
        calls = []

        def fake_record(user, symbol, strategy, exit_price, pnl,
                          exit_reason, qty=None, side=None):
            calls.append({
                "symbol": symbol, "strategy": strategy,
                "exit_price": exit_price, "pnl": pnl,
                "exit_reason": exit_reason,
            })

        monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
        for m in ("auth", "scheduler_api", "cloud_scheduler"):
            sys.modules.pop(m, None)
        import cloud_scheduler
        monkeypatch.setattr(cloud_scheduler, "record_trade_close", fake_record)

        contract_meta = {
            "contract_symbol": "AAPL260508P00150000",
            "type": "put",
            "strike": 150.0,
            "quantity": 1,
            "premium_received": 200.0,
        }
        ws._journal_wheel_close(user, contract_meta,
                                 exit_price=0.0, pnl=200.0,
                                 exit_reason="put expired worthless")
        assert calls, "record_trade_close must be invoked"
        c = calls[0]
        assert c["pnl"] == 200.0
        assert "expired" in c["exit_reason"]

    def test_never_raises_on_error(self, tmp_path, monkeypatch):
        """Journal write failure must not propagate — state machine
        correctness depends on it advancing regardless."""
        import wheel_strategy as ws
        user = _user(str(tmp_path))
        def boom(*a, **k):
            raise RuntimeError("journal disk full")

        monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "e" * 64)
        for m in ("auth", "scheduler_api", "cloud_scheduler"):
            sys.modules.pop(m, None)
        import cloud_scheduler
        monkeypatch.setattr(cloud_scheduler, "record_trade_close", boom)

        # Must not raise
        ws._journal_wheel_close(user, {
            "contract_symbol": "X", "type": "put",
            "strike": 100, "quantity": 1, "premium_received": 50.0,
        }, exit_price=0, pnl=50, exit_reason="test")


# ========= _load_json / _save_json atomic =========

class TestWheelJsonHelpers:
    def test_save_then_load_roundtrip(self, tmp_path):
        import wheel_strategy as ws
        path = str(tmp_path / "state.json")
        ws._save_json(path, {"a": 1, "nested": {"b": [1, 2, 3]}})
        loaded = ws._load_json(path)
        assert loaded == {"a": 1, "nested": {"b": [1, 2, 3]}}

    def test_load_missing_returns_none(self, tmp_path):
        import wheel_strategy as ws
        assert ws._load_json(str(tmp_path / "nope.json")) is None

    def test_load_malformed_returns_none(self, tmp_path):
        import wheel_strategy as ws
        path = str(tmp_path / "broken.json")
        with open(path, "w") as f:
            f.write("not json {")
        assert ws._load_json(path) is None

    def test_save_leaves_no_tmp_files(self, tmp_path):
        import wheel_strategy as ws
        path = str(tmp_path / "s.json")
        ws._save_json(path, {"x": 1})
        assert not any(n.endswith(".tmp") for n in os.listdir(tmp_path))


# ========= wheel_state_path / save_wheel_state =========

class TestWheelStatePath:
    def test_state_path_contains_symbol(self, tmp_path):
        import wheel_strategy as ws
        user = _user(str(tmp_path))
        path = ws.wheel_state_path(user, "AAPL")
        assert "AAPL" in path
        assert path.endswith(".json")
        assert user["_strategies_dir"] in path

    def test_list_wheel_files_empty(self, tmp_path):
        import wheel_strategy as ws
        user = _user(str(tmp_path))
        assert ws.list_wheel_files(user) == []

    def test_list_wheel_files_picks_up_wheels(self, tmp_path):
        import wheel_strategy as ws
        user = _user(str(tmp_path))
        sdir = user["_strategies_dir"]
        for sym in ("AAPL", "MSFT"):
            with open(os.path.join(sdir, f"wheel_{sym}.json"), "w") as f:
                json.dump({"symbol": sym, "strategy": "wheel",
                            "stage": "stage_1_searching"}, f)
        # Also a non-wheel file that should be ignored
        with open(os.path.join(sdir, "trailing_stop_NVDA.json"), "w") as f:
            json.dump({"symbol": "NVDA"}, f)
        files = ws.list_wheel_files(user)
        # Expect 2 entries, all wheel files
        assert len(files) == 2
        all_states = [s for _, s in files]
        symbols = {s.get("symbol") for s in all_states}
        assert symbols == {"AAPL", "MSFT"}

    def test_save_wheel_state_uses_symbol_filename(self, tmp_path):
        import wheel_strategy as ws
        user = _user(str(tmp_path))
        state = {"symbol": "AAPL", "stage": "stage_1_searching",
                  "history": []}
        ws.save_wheel_state(user, state)
        expected = os.path.join(user["_strategies_dir"], "wheel_AAPL.json")
        assert os.path.exists(expected)
        loaded = json.load(open(expected))
        assert loaded["symbol"] == "AAPL"
