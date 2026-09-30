from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from arena.core import Engine, EngineError


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "arena.sqlite3"
        self.engine = Engine(self.path)
        # Preserve the original three-agent regression scenarios explicitly.
        self.engine.configure({"agents": self.legacy_agents()})

    @staticmethod
    def legacy_agents():
        return [
            {"id": "rules", "name": "Rules baseline", "provider": "rules"},
            {"id": "openai", "name": "OpenAI strategist", "provider": "openai"},
            {"id": "claude", "name": "Claude strategist", "provider": "anthropic"},
        ]

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def agent(self, agent_id="rules"):
        return next(a for a in self.engine.state()["agents"] if a["id"] == agent_id)

    def ready(self, **config):
        self.engine.configure(config)
        self.engine.resume()

    def buy(self, symbol="SPY", amount=40, price=10, agent_id="rules"):
        return self.engine.reserve_order(agent_id, symbol, "buy", price, notional=amount)

    def fill(self, order, qty=None, price=None, status="filled"):
        return self.engine.update_order(order["id"], {"id": "broker-" + order["id"], "status": status,
                                                     "filled_qty": order["qty"] if qty is None else qty,
                                                     "filled_avg_price": order["price"] if price is None else price})

    def test_allocation_is_cent_exact_and_isolated(self):
        self.ready()
        state = self.engine.state()
        self.assertEqual([round(a["allocation"] * 100) for a in state["agents"]], [16667, 16667, 16666])
        untouched = self.agent("openai")
        self.fill(self.buy())
        self.assertEqual(self.agent("openai"), untouched)
        self.assertAlmostEqual(self.agent()["cash"], 126.666)
        self.assertAlmostEqual(self.agent()["fees"], .004)
        state["agents"][0]["cash"] = -1
        self.assertGreater(self.agent()["cash"], 0)

    def test_cumulative_fills_are_idempotent_and_regressions_ignored(self):
        self.ready()
        order = self.buy()
        self.fill(order, 1, 10, "partially_filled")
        self.assertAlmostEqual(self.agent()["cash"], 156.669)
        self.fill(order, 1, 10, "partially_filled")
        self.fill(order, .5, 10, "new")
        self.assertAlmostEqual(self.agent()["positions"]["SPY"]["qty"], 1)
        self.fill(order, 4, 10, "filled")
        self.fill(order, 4, 10, "filled")
        self.assertAlmostEqual(self.agent()["cash"], 126.666)
        self.assertAlmostEqual(self.agent()["positions"]["SPY"]["qty"], 4)
        self.assertAlmostEqual(self.agent()["fees"], .004)
        self.assertEqual(self.engine.pending_orders(), [])

    def test_cumulative_average_applies_only_increment(self):
        self.ready()
        order = self.engine.reserve_order("rules", "SPY", "buy", 10, qty=4)
        self.fill(order, 1, 9, "partially_filled")
        self.fill(order, 4, 9.75)
        self.assertAlmostEqual(self.agent()["cash"], 166.67 - 39 - .0039)
        self.assertAlmostEqual(self.agent()["positions"]["SPY"]["avg_price"], 9.75)

    def test_pending_cancel_holds_cash_and_blocks_same_symbol(self):
        self.ready()
        order = self.buy()
        self.fill(order, 1, 10, "pending_cancel")
        self.assertAlmostEqual(self.agent()["reserved_cash"], 30.003)
        with self.assertRaises(EngineError):
            self.buy()
        self.fill(order, 1, 10, "canceled")
        self.assertAlmostEqual(self.agent()["reserved_cash"], 0)
        self.assertEqual(len(self.engine.pending_orders()), 0)
        self.assertAlmostEqual(self.agent()["positions"]["SPY"]["qty"], 1)

    def test_pending_orders_count_toward_total_exposure(self):
        self.ready()
        self.buy("SPY", 40)
        self.buy("QQQ", 40)
        with self.assertRaisesRegex(EngineError, "exposure cap"):
            self.buy("IWM", 30)
        self.assertEqual(len(self.engine.pending_orders()), 2)

    def test_fee_reservation_prevents_overspending(self):
        self.ready(position_cap_pct=100, exposure_cap_pct=100, fee_bps=100)
        with self.assertRaisesRegex(EngineError, "unreserved cash"):
            self.buy(amount=166)
        self.buy(amount=160)
        self.assertAlmostEqual(self.agent()["reserved_cash"], 161.6)

    def test_restart_disables_autopilot_and_preserves_unknown_halt(self):
        self.ready(mode="paper")
        self.engine.set_autopilot(True)
        order = self.buy()
        self.engine.unknown_order(order["id"], "Response was lost.")
        self.engine.close()
        self.engine = Engine(self.path)
        state = self.engine.state()
        self.assertFalse(state["experiment"]["autopilot"])
        self.assertEqual(state["experiment"]["status"], "halted")
        self.assertIn("unknown", state["experiment"]["halt_reason"])
        self.assertGreater(self.agent()["reserved_cash"], 0)
        with self.assertRaises(EngineError):
            self.engine.resume()
        self.fill(order, 0, 0, "rejected")
        self.engine.resume()
        self.assertEqual(self.engine.state()["experiment"]["status"], "ready")

    def test_restart_recovers_autopilot_and_halts_manual_unresolved(self):
        self.ready(mode="paper")
        self.engine.set_autopilot(True)
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.engine.state()["experiment"]["status"], "recovering")
        self.assertTrue(self.engine.state()["experiment"]["autopilot"])
        with self.assertRaises(EngineError):
            self.engine.recover_startup()
        for agent in self.engine.snapshot()["agents"]:
            self.engine.note_reconciled(agent["id"])
        self.engine.recover_startup()
        self.engine.set_autopilot(False)
        self.buy()
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.engine.state()["experiment"]["status"], "halted")

    def test_halt_prevents_entries_but_permits_exits(self):
        self.ready()
        self.fill(self.buy())
        self.engine.halt("Operator stop")
        with self.assertRaises(EngineError):
            self.buy("QQQ")
        order = self.engine.reserve_order("rules", "SPY", "sell", 10, qty=4)
        self.fill(order)
        self.assertEqual(self.agent()["positions"], {})
        self.assertAlmostEqual(self.agent()["cash"], 166.662)
        self.assertEqual(self.engine.state()["experiment"]["status"], "halted")

    def test_model_cost_reduces_net_results_not_trading_loss_budget(self):
        self.ready(mode="paper", loss_limit=1)
        self.engine.set_autopilot(True)
        self.engine.charge_model("openai", 1, call_id="loss")
        exp = self.engine.snapshot()["experiment"]
        self.assertEqual(exp["status"], "ready")
        self.assertTrue(exp["autopilot"])
        self.assertEqual(exp["net_pnl"], -1)
        self.assertEqual(exp["trading_pnl"], 0)
        self.engine.resume()

    def test_idempotent_model_charge_survives_restart(self):
        self.ready()
        cash = self.agent("openai")["cash"]
        self.engine.charge_model("openai", .125, call_id="unique-call")
        self.engine.close()
        self.engine = Engine(self.path)
        self.engine.charge_model("openai", .125, call_id="unique-call")
        a = self.agent("openai")
        self.assertEqual(a["cash"], cash)
        self.assertEqual(a["model_cost"], .125)
        self.assertEqual(len(a["model_charges"]), 1)
        self.assertAlmostEqual(a["equity"], cash - .125)
        self.engine.charge_model("openai", .2, call_id="unique-call")
        self.assertEqual(self.agent("openai")["model_cost"], .125)

    def test_quote_marks_trigger_loss_halt_and_drawdown(self):
        self.ready(loss_limit=10)
        self.fill(self.buy())
        self.engine.mark({"SPY": 20}, "2025-01-02T21:00:00Z")
        self.assertGreater(self.agent()["equity"], self.agent()["allocation"])
        self.engine.mark({"SPY": 5}, "2025-01-03T21:00:00Z")
        self.assertGreater(self.agent()["max_drawdown_pct"], 0)
        self.assertEqual(self.engine.state()["experiment"]["status"], "halted")

    def test_all_nonfinite_financial_values_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf"), True):
            with self.subTest(value=value):
                with self.assertRaises(EngineError):
                    self.engine.configure({"total_capital": value})
                with self.assertRaises(EngineError):
                    self.engine.charge_model("openai", value)
                with self.assertRaises(EngineError):
                    self.engine.mark({"SPY": value}, "2025-01-02")
        self.ready()
        for value in (float("nan"), float("inf"), -1, 0, True):
            with self.assertRaises(EngineError):
                self.buy(price=value)
        self.assertEqual(self.engine.pending_orders(), [])

    def test_account_binding_unique_and_immutable(self):
        self.engine.attach_account("rules", "A")
        self.engine.attach_account("rules", "A")
        with self.assertRaises(EngineError):
            self.engine.attach_account("rules", "B")
        with self.assertRaises(EngineError):
            self.engine.attach_account("openai", "A")
        self.engine.attach_account("openai", "B")

    def test_new_experiment_archives_full_state_and_rejects_pending(self):
        self.ready()
        old_id = self.engine.state()["experiment"]["id"]
        order = self.buy()
        with self.assertRaises(EngineError):
            self.engine.new_experiment({})
        self.fill(order, 0, 0, "rejected")
        self.engine.new_experiment({"mode": "paper"})
        state = self.engine.state()
        self.assertEqual(state["experiment"]["status"], "paused")
        self.assertEqual(state["archives"][0]["id"], old_id)
        self.assertNotEqual(state["experiment"]["id"], old_id)
        with closing(sqlite3.connect(self.path)) as db, db:
            saved = json.loads(db.execute("SELECT payload FROM arena_archives WHERE id=?", (old_id,)).fetchone()[0])
        self.assertEqual(len(saved["orders"]), 1)

    def test_concurrent_reservation_does_not_double_spend(self):
        self.ready()
        outcomes = []
        def reserve():
            try:
                outcomes.append(self.buy())
            except EngineError:
                outcomes.append(None)
        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        self.assertEqual(len(self.engine.pending_orders()), 1)

    def test_broker_truth_overfill_recorded_and_halted(self):
        self.ready()
        order = self.engine.reserve_order("rules", "SPY", "buy", 10, qty=4)
        self.fill(order, 5, 10)
        self.assertEqual(self.agent()["positions"]["SPY"]["qty"], 5)
        self.assertEqual(self.engine.state()["experiment"]["status"], "halted")

    def test_terminal_status_not_regressed_by_stale_acknowledgement(self):
        self.ready()
        order = self.buy()
        self.fill(order)
        self.fill(order, 4, 10, "new")
        self.assertEqual(self.engine.state()["orders"][0]["status"], "filled")
        self.assertEqual(self.engine.pending_orders(), [])

    def test_secrets_and_nonfinite_decisions_rejected(self):
        with self.assertRaises(EngineError):
            self.engine.record_decision("rules", {"nested": {"api_key": "never-save"}})
        with self.assertRaises(EngineError):
            self.engine.record_decision("rules", {"score": float("nan")})
        self.assertEqual(self.engine.state()["decisions"], [])

    def test_demo_is_synthetic_and_reviewers_never_trade(self):
        self.ready()
        self.engine.demo_step(35)
        state = self.engine.state()
        self.assertEqual(state["experiment"]["step"], 35)
        self.assertEqual(state["market"]["source"], "synthetic_demo")
        self.assertGreater(len(state["orders"]), 0)
        self.assertTrue(all(order["agent_id"] == "rules" for order in state["orders"]))
        self.assertTrue(all(d["status"] == "disconnected" for d in state["decisions"] if d["agent_id"] != "rules"))
        self.assertEqual(self.agent("openai")["net_pnl"], 0)
        self.assertEqual(self.agent("claude")["model_cost"], 0)
        self.assertEqual(self.engine.pending_orders(), [])


    @staticmethod
    def asset(symbol="AAPL", **updates):
        metadata = {"id": "broker-asset-id", "symbol": symbol, "name": "Example company",
                    "status": "active", "tradable": True, "fractionable": True,
                    "class": "us_equity", "exchange": "NASDAQ"}
        metadata.update(updates)
        return metadata

    def test_new_experiment_autonomy_defaults_and_legacy_form_merging(self):
        exp = self.engine.state()["experiment"]
        self.assertEqual(exp["agent_mode"], "autonomous")
        self.assertTrue(exp["compound_profits"])
        self.assertEqual(exp["research_rounds"], 3)
        self.engine.configure({"total_capital": 600})
        self.assertEqual(self.engine.state()["experiment"]["cycle_minutes"], 60)
        self.assertEqual(self.agent("openai")["name"], "OpenAI strategist")
        self.assertEqual(self.agent("claude")["name"], "Claude strategist")

    def test_autonomy_config_rejects_invalid_limits_atomically(self):
        initial = self.engine.state()
        for field, values in {
            "agent_mode": ["independent", 1, None],
            "compound_profits": ["true", 1, None],
            "research_rounds": [0, 6, 3.0, True],
            "cycle_minutes": [14, 1441, 60.5, "60"],
            "max_cycles_per_day": [0, 25, float("nan")],
            "max_orders_per_cycle": [0, 11, float("inf")],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(EngineError):
                        self.engine.configure({field: value})
        self.assertEqual(self.engine.state(), initial)

    def test_old_database_migrates_without_enabling_autonomy_or_compounding(self):
        self.ready(fee_bps=0)
        order = self.buy()
        self.fill(order)
        self.engine.close()
        new_keys = ("agent_mode", "research_rounds", "cycle_minutes", "max_cycles_per_day",
                    "max_orders_per_cycle", "compound_profits")
        with closing(sqlite3.connect(self.path)) as db, db:
            original = json.loads(db.execute("SELECT payload FROM arena_state WHERE id=1").fetchone()[0])
            for key in new_keys:
                original["experiment"].pop(key, None)
            original.pop("asset_registry", None)
            original["agents"][1]["name"] = "My existing reviewer"
            original["agents"][0]["positions"]["SPY"].pop("opened_at", None)
            db.execute("UPDATE arena_state SET payload=? WHERE id=1", (json.dumps(original),))
        self.engine = Engine(self.path)
        state = self.engine.state()
        self.assertEqual(state["experiment"]["agent_mode"], "reviewer")
        self.assertFalse(state["experiment"]["compound_profits"])
        self.assertEqual(self.agent("openai")["name"], "My existing reviewer")
        self.assertEqual(self.agent()["cash"], original["agents"][0]["cash"])
        self.assertEqual(self.agent()["positions"]["SPY"]["qty"], 4)
        self.assertEqual(self.agent()["positions"]["SPY"]["opened_at"], order["created_at"])
        self.assertEqual(state["orders"], original["orders"])
        self.assertEqual(state["asset_registry"], {})

    def test_registered_asset_can_be_marked_and_traded_and_persists(self):
        self.ready()
        with self.assertRaises(EngineError):
            self.engine.mark({"AAPL": 10}, "2025-01-01T21:00:00Z")
        with self.assertRaises(EngineError):
            self.buy("AAPL")
        entry = self.engine.register_asset("AAPL", self.asset())
        self.assertEqual(entry["asset_class"], "us_equity")
        self.engine.mark({"AAPL": 10}, "2025-01-01T21:00:00Z")
        self.fill(self.buy("AAPL"))
        self.assertAlmostEqual(self.agent()["positions"]["AAPL"]["qty"], 4)
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.engine.state()["asset_registry"]["AAPL"], entry)
        self.engine.new_experiment({})
        self.assertEqual(self.engine.state()["asset_registry"], {})

    def test_registry_rejects_fake_ineligible_and_mismatched_instruments(self):
        for updates in ({"status": "inactive"}, {"tradable": False}, {"tradable": "true"},
                        {"fractionable": 1}, {"fractionable": False}, {"class": "crypto"},
                        {"asset_class": "crypto"}, {"exchange": "OTC"}, {"exchange": "FAKE"},
                        {"symbol": "MSFT"}):
            with self.subTest(updates=updates):
                with self.assertRaises(EngineError):
                    self.engine.register_asset("AAPL", self.asset(**updates))
        for symbol in ("../AAPL", "BTC/USD", "AAPL;rm", "aapl", "A..B", ""):
            with self.assertRaises(EngineError):
                self.engine.register_asset(symbol, self.asset(symbol=symbol))
        self.assertEqual(self.engine.state()["asset_registry"], {})
        entry = self.engine.register_asset("BRK.B", self.asset(symbol="BRK.B", exchange="NYSE", api_key="must-not-persist"))
        self.assertNotIn("api_key", entry)

    def test_compounding_uses_current_equity_and_legacy_caps_initial_equity(self):
        for compound in (False, True):
            with self.subTest(compound=compound):
                self.engine.new_experiment({"agents": self.legacy_agents(), "compound_profits": compound, "fee_bps": 0})
                self.engine.resume()
                self.fill(self.buy())
                self.engine.mark({"SPY": 20}, "2025-01-02T21:00:00Z")
                sell = self.engine.reserve_order("rules", "SPY", "sell", 20, qty=4)
                self.fill(sell)
                self.assertAlmostEqual(self.agent()["equity"], 206.67)
                if compound:
                    self.buy("QQQ", amount=60)
                else:
                    with self.assertRaisesRegex(EngineError, "position cap"):
                        self.buy("QQQ", amount=60)

    def test_compounding_never_borrows_or_transfers_between_agents(self):
        self.ready(compound_profits=True, fee_bps=0, position_cap_pct=100, exposure_cap_pct=100)
        self.fill(self.buy(amount=100))
        self.engine.mark({"SPY": 30}, "2025-01-02T21:00:00Z")
        self.assertAlmostEqual(self.agent()["equity"], 366.67)
        with self.assertRaisesRegex(EngineError, "unreserved cash"):
            self.buy("QQQ", amount=100)
        with self.assertRaisesRegex(EngineError, "unreserved cash"):
            self.buy("QQQ", amount=200, agent_id="openai")
        self.assertAlmostEqual(self.agent("openai")["equity"], 166.67)

    def test_capital_basis_shrinks_with_losses_even_when_compounding(self):
        self.ready(compound_profits=True, fee_bps=0)
        self.fill(self.buy())
        self.engine.mark({"SPY": 5}, "2025-01-02T21:00:00Z")
        self.assertAlmostEqual(self.agent()["equity"], 146.67)
        with self.assertRaisesRegex(EngineError, "position cap"):
            self.buy("QQQ", amount=45)
        self.buy("QQQ", amount=40)

    def test_first_fill_time_survives_topups_partial_sells_and_restart(self):
        self.ready(fee_bps=0)
        order = self.buy(amount=20)
        self.engine.update_order(order["id"], {"status": "filled", "filled_qty": 2,
                                               "filled_avg_price": 10, "filled_at": "2025-01-02T15:00:00Z"})
        opened = self.agent()["positions"]["SPY"]["opened_at"]
        self.assertEqual(opened, "2025-01-02T15:00:00+00:00")
        topup = self.buy(amount=20)
        self.engine.update_order(topup["id"], {"status": "filled", "filled_qty": 2,
                                               "filled_avg_price": 10, "filled_at": "2025-01-03T15:00:00Z"})
        sell = self.engine.reserve_order("rules", "SPY", "sell", 10, qty=1)
        self.fill(sell)
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.agent()["positions"]["SPY"]["opened_at"], opened)
        self.assertEqual(self.agent()["positions"]["SPY"]["qty"], 3)

    def test_invalid_fill_timestamp_uses_valid_update_timestamp(self):
        self.ready()
        order = self.buy()
        self.engine.update_order(order["id"], {"status": "filled", "filled_qty": 4,
                                               "filled_avg_price": 10, "filled_at": "invalid",
                                               "updated_at": "2025-01-02T15:00:00Z"})
        self.assertEqual(self.agent()["positions"]["SPY"]["opened_at"], "2025-01-02T15:00:00+00:00")


    def test_v03_fresh_defaults_are_two_separate_strategists(self):
        engine = Engine(":memory:")
        try:
            agents = engine.state()["agents"]
            self.assertEqual([(a["id"], a["name"], a["allocation"]) for a in agents], [("openai", "Astra", 250), ("claude", "Claude", 250)])
            self.assertEqual([(a["model"], a["input_price"], a["output_price"]) for a in agents], [("gpt-6-astra", 10, 50), ("claude-opus-5-5", 4, 20)])
        finally:
            engine.close()

    def test_agent_pause_preserves_other_agent_and_survives_restart(self):
        self.ready()
        self.engine.halt_agent("openai", "Investigate this account")
        with self.assertRaisesRegex(EngineError, "agent is paused"):
            self.buy(agent_id="openai")
        self.fill(self.buy(agent_id="claude"))
        self.assertFalse(self.engine.state()["agent_controls"]["claude"]["paused"])
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertTrue(self.engine.state()["agent_controls"]["openai"]["paused"])
        self.assertEqual(len(self.engine.state()["agents"]), 3)
        with self.assertRaises(EngineError):
            self.engine.resume_agent("openai", require_reconciled=True)
        self.engine.note_reconciled("openai", "2026-09-18T14:00:00Z")
        self.engine.resume_agent("openai", require_reconciled=True)
        self.assertFalse(self.engine.state()["agent_controls"]["openai"]["paused"])

    def test_isolated_unknown_order_does_not_block_healthy_agent(self):
        self.ready()
        order = self.buy(agent_id="openai")
        self.engine.unknown_order(order["id"], "Lost acknowledgment", halt=False)
        self.assertEqual(self.engine.state()["experiment"]["status"], "ready")
        self.assertTrue(self.engine.state()["agent_controls"]["openai"]["paused"])
        self.buy(agent_id="claude")
        with self.assertRaises(EngineError):
            self.engine.resume_agent("openai")

    def test_stale_quote_cannot_overwrite_later_mark(self):
        self.engine.mark({"SPY": 20}, "2026-09-18T14:00:00Z")
        self.engine.mark({"SPY": 10, "QQQ": 12}, "2026-09-18T13:00:00Z")
        market = self.engine.state()["market"]
        self.assertEqual(market["prices"]["SPY"], 20)
        self.assertEqual(market["prices"]["QQQ"], 12)
        self.engine.note_quotes("openai", {"SPY": "2026-09-18T14:00:00Z"})
        self.assertNotIn("last_quote_at", self.engine.state()["agent_controls"]["claude"])


    def test_paused_unknown_order_does_not_block_healthy_autopilot_resume(self):
        self.ready(mode="paper")
        order = self.buy(agent_id="openai")
        self.engine.unknown_order(order["id"], "Unresolved submission", halt=False)
        self.engine.halt("Restart review")
        self.engine.resume(allow_isolated=True)
        self.engine.set_autopilot(True)
        self.assertTrue(self.engine.state()["experiment"]["autopilot"])
        self.assertTrue(self.engine.state()["agent_controls"]["openai"]["paused"])
        self.buy(agent_id="claude")


if __name__ == "__main__":
    unittest.main()
