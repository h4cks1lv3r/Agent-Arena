"""Autonomous orchestration regressions with real ledgers and mocked transports."""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from arena.adapters import ApiError
from arena.service import Service


START = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.timezone.utc)


def response(*, orders=None, exits=None, research=None, minutes=15):
    return {
        "phase": "research" if research else "plan",
        "strategy": {"name": "Independent test thesis", "thesis": "Use researched company evidence.",
                     "invalidation": "Exit if the recorded price threshold fails.", "lessons": "No prior observations."},
        "research": research or [], "orders": orders or [], "exits": exits or [],
        "watchlist": ["MSFT"], "review_minutes": minutes,
        "reason": "A bounded test plan, unrelated to the baseline indicators.",
        "input_tokens": 100, "output_tokens": 25, "model": "test-model",
    }


def buy(symbol="MSFT", amount=37.25, evidence="evidence_fixture"):
    return {"symbol": symbol, "side": "buy", "notional": amount, "qty": 0,
            "reason": "Allocate the model-chosen amount.", "evidence_ids": [evidence]}


def sell(symbol="MSFT", qty=1, evidence="evidence_fixture"):
    return {"symbol": symbol, "side": "sell", "notional": 0, "qty": qty,
            "reason": "Reduce this agent's own holding.", "evidence_ids": [evidence]}


def exit_rule(symbol="MSFT", stop=95, target=0, hours=0):
    return {"symbol": symbol, "stop_loss": stop, "take_profit": target,
            "max_hold_hours": hours, "reason": "A stored local exit condition."}


class FakePaper:
    """A broker book whose accepted orders fill immediately at its quoted price."""

    def __init__(self, agent_id, clock):
        self.agent_id = agent_id
        self.now = clock
        self.prices = {"MSFT": 100.0, "NVDA": 100.0}
        self.holdings = {}
        self.book = []
        self.on_submit = None
        self.submit_order = Mock(side_effect=self._submit)
        self.bars = Mock(side_effect=AssertionError("Autonomous plan must not require SMA bars."))
        self.calendar = Mock(side_effect=AssertionError("Autonomous plan must not run the baseline."))

    def account(self):
        return {"id": f"private-account-{self.agent_id}", "status": "ACTIVE", "cash": "100000"}

    def clock(self):
        return {"is_open": True, "timestamp": self.now().isoformat(),
                "next_close": self.now().replace(hour=20, minute=0).isoformat()}

    def asset(self, symbol):
        if symbol not in self.prices:
            raise ApiError("Unknown mock asset.")
        return {"symbol": symbol, "name": "Example " + symbol, "status": "active",
                "class": "us_equity", "exchange": "NASDAQ", "tradable": True, "fractionable": True}

    def assets(self):
        return [self.asset(symbol) for symbol in self.prices]

    def quotes(self, symbols):
        return {symbol: {"price": self.prices[symbol], "bid": self.prices[symbol] - .01,
                         "ask": self.prices[symbol] + .01, "t": self.now().isoformat()}
                for symbol in symbols}

    def news(self, symbols=None, limit=10):
        return [{"id": "news-test-1", "headline": "A test company report", "summary": "Mock market data.",
                 "source": "Mock source", "url": "https://example.invalid/not-fetched",
                 "created_at": self.now().isoformat(), "updated_at": self.now().isoformat(), "symbols": ["MSFT"]}]

    def movers(self):
        return {"gainers": [{"symbol": "MSFT", "price": 100, "percent_change": 2}], "losers": []}

    def positions(self):
        return [{"symbol": symbol, "qty": str(qty)} for symbol, qty in self.holdings.items() if qty > 1e-8]

    def orders(self, status="all", *, after_order_id=None):
        orders = self.book
        if after_order_id is not None:
            index = next(i for i, item in enumerate(orders) if item["id"] == after_order_id)
            orders = orders[index + 1:]
        if status == "open":
            orders = [item for item in orders if item["status"] not in ("filled", "canceled", "expired", "rejected", "replaced")]
        return deepcopy(orders)

    def order_by_client_id(self, client_id):
        return deepcopy(next(item for item in self.book if item["client_order_id"] == client_id))

    def _submit(self, payload):
        if self.on_submit:
            self.on_submit(deepcopy(payload))
        symbol = payload["symbol"]
        qty = float(payload["notional"]) / self.prices[symbol] if payload["side"] == "buy" else float(payload["qty"])
        self.holdings[symbol] = self.holdings.get(symbol, 0) + (qty if payload["side"] == "buy" else -qty)
        order = {**payload, "id": f"mock-{self.agent_id}-{len(self.book)}", "status": "filled",
                 "filled_qty": str(qty), "filled_avg_price": str(self.prices[symbol]),
                 "filled_at": self.now().isoformat()}
        self.book.append(order)
        return deepcopy(order)


class AutonomousServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.services = []
        self.time = START
        self.env = {"OPENAI_API_KEY": "private-model-openai", "ANTHROPIC_API_KEY": "private-model-claude",
                    "ALPACA_OPENAI_KEY": "private-broker-key", "ALPACA_OPENAI_SECRET": "private-broker-secret"}
        self.clock_patch = patch("arena.service.now", side_effect=lambda: self.time)
        self.clock_patch.start()
        self.network_patch = patch("socket.socket.connect", side_effect=AssertionError("Tests prohibit real network access."))
        self.network_patch.start()
        self.brokers = {agent: FakePaper(agent, lambda: self.time) for agent in ("openai", "claude")}
        self.svc = self.make_service()

    def tearDown(self):
        self.network_patch.stop()
        self.clock_patch.stop()
        for svc in self.services:
            svc.meta.close()
            svc.engine.close()
        self.tmp.cleanup()

    def make_service(self):
        service = Service(self.tmp.name, environ=self.env)
        service.broker = Mock(side_effect=lambda agent: self.brokers[agent["id"]])
        self.services.append(service)
        return service

    def configure(self, **config):
        agents = [{"id": "openai", "name": "First strategist", "provider": "openai", "model": "test-model",
                   "weight": 1, "input_price": 1, "output_price": 1},
                  {"id": "claude", "name": "Second strategist", "provider": "anthropic", "model": "test-model",
                   "weight": 3, "input_price": 1, "output_price": 1}]
        self.svc.new_experiment({"mode": "paper", "agent_mode": "autonomous", "total_capital": 500,
                                 "target": 10000, "loss_limit": 50, "position_cap_pct": 100,
                                 "exposure_cap_pct": 100, "monthly_model_budget": 10,
                                 "research_rounds": 3, "cycle_minutes": 60, "max_cycles_per_day": 4,
                                 "max_orders_per_cycle": 3, "agents": agents, **config})
        self.svc.engine.resume()

    def agent(self, agent_id="openai"):
        return next(a for a in self.svc.engine.state()["agents"] if a["id"] == agent_id)

    def memory(self, agent_id="openai"):
        return self.svc.state()["autonomy"][agent_id]

    def seed_previous_plan(self, *, amount=37.25, rule=None):
        self.svc.reconcile()
        plan = response(orders=[buy(amount=amount)], exits=[rule or exit_rule()])
        self.svc._execute_plan(self.agent(), {"plan": plan}, {"cycle_id": "fixture-prior-cycle"}, self.brokers["openai"])
        return plan

    def test_researched_nonbaseline_buy_keeps_model_size_and_journals_before_order(self):
        self.configure()
        self.svc._record_autonomy("claude", {"strategy": {"thesis": "other-agent-private-strategy"}})
        self.svc.engine.record_decision("claude", {"action": "hold", "reason": "other-agent-private-decision"})
        inputs = []
        before_submit = []
        claims_before_calls = []

        def model(provider, model_id, key, snapshot):
            inputs.append(deepcopy(snapshot))
            exp = self.svc.engine.state()["experiment"]
            claims_before_calls.append((self.svc._get(f"autonomy_count:{exp['id']}:openai:2026-09-17"), self.memory().get("next_due")))
            evidence = [record for record in snapshot["evidence"] if record["kind"] == "quotes" and record["status"] == "ok"]
            if not evidence:
                return response(research=[{"kind": "quotes", "query": "", "symbols": ["MSFT"], "lookback_days": 30}])
            return response(orders=[buy(evidence=evidence[0]["id"])], exits=[exit_rule()])

        def observe_submission(payload):
            state = self.svc.engine.state()
            before_submit.append((deepcopy(state["decisions"]), deepcopy(self.memory()), payload))

        self.brokers["openai"].on_submit = observe_submission
        with patch("arena.service.autonomous_turn", side_effect=model) as paid, \
             patch("arena.service.baseline_signal", side_effect=AssertionError("SMA must not gate independent plans.")):
            self.svc.cycle()
        self.assertEqual(paid.call_count, 2, self.memory())
        self.assertEqual(self.brokers["openai"].submit_order.call_count, 1, self.memory())
        payload = self.brokers["openai"].submit_order.call_args.args[0]
        self.assertEqual(payload["symbol"], "MSFT")
        self.assertEqual(float(payload["notional"]), 37.25)
        self.assertAlmostEqual(self.agent()["positions"]["MSFT"]["qty"], .3725)
        self.brokers["openai"].bars.assert_not_called()
        self.brokers["openai"].calendar.assert_not_called()
        self.assertTrue(all(count == 1 and due for count, due in claims_before_calls))
        decisions, memory, _ = before_submit[0]
        research = next(d for d in decisions if d["agent_id"] == "openai" and d["action"] == "research")
        self.assertEqual(research["plan"]["orders"][0]["notional"], 37.25)
        self.assertEqual(len(research["turns"]), 2)
        self.assertTrue(research["evidence"])
        self.assertTrue(research["recorded_before_order"])
        intent = next(d for d in decisions if d.get("status") == "autonomous_intent")
        self.assertTrue(intent["recorded_before_order"])
        self.assertEqual(memory["exits"]["MSFT"]["stop_loss"], 95)
        self.assertEqual(memory["strategy"]["name"], "Independent test thesis")
        encoded = json.dumps(inputs)
        for secret in (*self.env.values(), "private-account-openai", "private-account-claude",
                       "other-agent-private-strategy", "other-agent-private-decision"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(inputs[0]["portfolio"]["allocation"], 125)
        self.assertEqual(inputs[0]["goal"]["target_equity"], 2500)

    def test_zero_budget_blocks_provider_and_orders(self):
        self.configure(monthly_model_budget=0)
        with patch("arena.service.autonomous_turn") as paid:
            self.svc.cycle()
            with self.assertRaisesRegex(ValueError, "budget"):
                self.svc._ask_autonomous(self.agent(), {})
            paid.assert_not_called()
        self.assertIn("$0", self.memory()["reason"])
        self.assertEqual(self.svc.meta.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)
        self.brokers["openai"].submit_order.assert_not_called()

    def test_weighted_model_budget_blocks_one_agent_without_spending_other_share(self):
        self.configure(monthly_model_budget=.24)
        completion = {**response(), "input_tokens": 40000, "output_tokens": 1000}
        with patch("arena.service.autonomous_turn", return_value=completion) as paid:
            self.svc._ask_autonomous(self.agent(), {})
            with self.assertRaisesRegex(ValueError, "budget"):
                self.svc._ask_autonomous(self.agent(), {})
            self.svc._ask_autonomous(self.agent("claude"), {})
        self.assertEqual(paid.call_count, 2)
        self.assertAlmostEqual(self.agent()["model_cost"], .041)
        self.assertAlmostEqual(self.agent("claude")["model_cost"], .041)
        self.assertAlmostEqual(self.svc.state()["model_spend_month"], .082)

    def test_failed_paid_turn_keeps_reservation_and_is_not_retried_same_cycle(self):
        self.configure()
        with patch("arena.service.autonomous_turn", side_effect=ApiError("Mock provider timeout")) as paid:
            self.svc.cycle()
            self.svc.cycle()
        self.assertEqual(paid.call_count, 1)
        rows = self.svc.meta.execute("SELECT cost,status FROM calls").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "uncertain")
        self.assertAlmostEqual(rows[0][0], .053192)
        self.assertAlmostEqual(self.agent()["model_cost"], .053192)
        self.brokers["openai"].submit_order.assert_not_called()

    def test_full_plan_preflight_prevents_partial_execution_on_overspend(self):
        self.configure()
        plan = response(orders=[buy(amount=10), buy("NVDA", 120)])
        with self.assertRaisesRegex(ValueError, "cash|exposure|position"):
            self.svc._execute_plan(self.agent(), {"plan": plan}, {"cycle_id": "too-large"}, self.brokers["openai"])
        self.brokers["openai"].submit_order.assert_not_called()
        self.assertEqual(self.svc.engine.pending_orders(), [])
        self.assertEqual(self.svc.engine.state()["orders"], [])
        self.assertEqual(self.agent()["cash"], 125)

    def test_preflight_cannot_sell_another_agents_position(self):
        self.configure()
        self.svc.engine.register_asset("MSFT", self.brokers["claude"].asset("MSFT"))
        order = self.svc.engine.reserve_order("claude", "MSFT", "buy", 100, qty=1)
        self.svc.engine.update_order(order["id"], {"id": "other-agent-fill", "status": "filled",
                                                "filled_qty": "1", "filled_avg_price": "100"})
        with self.assertRaisesRegex(ValueError, "more shares than this agent owns"):
            self.svc._preflight_plan(self.agent(), response(orders=[sell(qty=.5)]), self.brokers["openai"])
        self.brokers["openai"].submit_order.assert_not_called()
        self.assertEqual(self.agent()["positions"], {})
        self.assertEqual(self.agent("claude")["positions"]["MSFT"]["qty"], 1)

    def test_preflight_does_not_spend_proceeds_of_unfilled_sell(self):
        self.configure(monthly_model_budget=0)
        self.seed_previous_plan(amount=100)
        calls = self.brokers["openai"].submit_order.call_count
        with self.assertRaisesRegex(ValueError, "cash|exposure"):
            self.svc._execute_plan(self.agent(), {"plan": response(orders=[sell(qty=1), buy("NVDA", 50)])},
                                   {"cycle_id": "unfilled-funding"}, self.brokers["openai"])
        self.assertEqual(self.brokers["openai"].submit_order.call_count, calls)
        self.assertEqual(self.agent()["positions"]["MSFT"]["qty"], 1)

    def test_current_broker_ineligibility_rejects_whole_plan_before_first_order(self):
        self.configure()
        broker = self.brokers["openai"]
        original_asset = broker.asset

        def asset(symbol):
            metadata = original_asset(symbol)
            if symbol == "NVDA":
                metadata["tradable"] = False
            return metadata

        plan = response(orders=[buy(amount=10), buy("NVDA", 10)])
        with patch.object(broker, "asset", side_effect=asset), self.assertRaises(ValueError):
            self.svc._execute_plan(self.agent(), {"plan": plan}, {"cycle_id": "eligibility-changed"}, broker)
        broker.submit_order.assert_not_called()
        self.assertEqual(self.svc.engine.state()["orders"], [])

    def test_due_time_and_daily_limit_persist_through_restart(self):
        self.configure(max_cycles_per_day=2, cycle_minutes=60)
        with patch("arena.service.autonomous_turn", return_value=response()) as paid:
            self.svc.cycle()
            due = self.memory()["next_due"]
            self.assertEqual(dt.datetime.fromisoformat(due), START + dt.timedelta(hours=1))
            self.svc.cycle()
            self.svc = self.make_service()
            self.svc.engine.resume()
            self.svc.cycle()
            self.assertEqual(paid.call_count, 1)
            self.assertEqual(self.memory()["next_due"], due)
            self.time = START + dt.timedelta(hours=1)
            self.svc.cycle()
            self.assertEqual(paid.call_count, 2)
            self.time += dt.timedelta(hours=1)
            self.svc.cycle()
            self.assertEqual(paid.call_count, 2, "Daily cap must survive advancing beyond next_due.")
            self.time = START + dt.timedelta(days=1)
            self.svc.cycle()
            self.assertEqual(paid.call_count, 3, "A new New York trading day gets a fresh daily allowance.")

    def test_stored_local_exit_runs_with_zero_budget_without_model_call(self):
        self.configure(monthly_model_budget=0)
        self.seed_previous_plan()
        memory = self.memory()
        memory["next_due"] = (START + dt.timedelta(days=1)).isoformat()
        self.svc._record_autonomy("openai", memory)
        self.svc = self.make_service()
        self.svc.resume()
        self.brokers["openai"].prices["MSFT"] = 94
        with patch("arena.service.autonomous_turn") as paid:
            self.svc.cycle()
            self.svc.cycle()
            paid.assert_not_called()
        orders = self.brokers["openai"].submit_order.call_args_list
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[-1].args[0]["side"], "sell")
        self.assertAlmostEqual(float(orders[-1].args[0]["qty"]), .3725)
        self.assertEqual(self.agent()["positions"], {})
        decisions = self.svc.engine.state()["decisions"]
        self.assertTrue(any(d.get("status") == "agent_exit_trigger" for d in decisions))

    def test_user_halt_blocks_automatic_exits_and_entries(self):
        self.configure(monthly_model_budget=0)
        self.seed_previous_plan()
        self.brokers["openai"].prices["MSFT"] = 94
        self.svc.halt("Operator stopped all automatic order activity.")
        before = self.brokers["openai"].submit_order.call_count
        with patch("arena.service.autonomous_turn") as paid:
            self.svc.cycle()
            for side in ("buy", "sell"):
                with self.subTest(side=side), self.assertRaisesRegex(ValueError, "paused or halted"):
                    self.svc._submit(self.agent(), "MSFT", side, 94, automatic=True,
                                     notional=1 if side == "buy" else None, qty=.1 if side == "sell" else None)
            paid.assert_not_called()
        self.assertEqual(self.brokers["openai"].submit_order.call_count, before)
        self.assertAlmostEqual(self.agent()["positions"]["MSFT"]["qty"], .3725)
        self.assertEqual(self.svc.engine.state()["experiment"]["status"], "halted")

    def test_flat_position_clears_exit_rule_before_unrelated_future_purchase(self):
        self.configure(monthly_model_budget=0)
        self.seed_previous_plan()
        broker = self.brokers["openai"]
        self.svc._submit(self.agent(), "MSFT", "sell", 100, qty=.3725, automatic=True)
        self.assertEqual(self.agent()["positions"], {})
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertNotIn("MSFT", self.memory()["exits"])

        # A future purchase below the old stop must not inherit that old rule.
        broker.prices["MSFT"] = 94
        self.svc._submit(self.agent(), "MSFT", "buy", 94, notional=10, automatic=True)
        submissions = broker.submit_order.call_count
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(broker.submit_order.call_count, submissions)
        self.assertAlmostEqual(self.agent()["positions"]["MSFT"]["qty"], 10 / 94)

    def test_pending_buy_preserves_exit_rule_until_fill_or_terminal_rejection(self):
        self.configure(monthly_model_budget=0)
        broker = self.brokers["openai"]
        self.svc.engine.register_asset("NVDA", broker.asset("NVDA"))
        rule = exit_rule("NVDA")
        self.svc._record_autonomy("openai", {"exits": {"NVDA": rule}})
        order = self.svc.engine.reserve_order("openai", "NVDA", "buy", 100, notional=10)
        self.assertEqual(self.agent()["positions"], {})
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(self.memory()["exits"]["NVDA"], rule)
        broker.submit_order.assert_not_called()

        self.svc.engine.update_order(order["id"], {"status": "rejected", "filled_qty": "0"})
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertNotIn("NVDA", self.memory()["exits"])


if __name__ == "__main__":
    unittest.main()
