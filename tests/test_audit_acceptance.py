"""Independent failure/recovery acceptance tests; never use live APIs or funds.

These simulate specific operational failures. They do not establish multi-week
uptime, actual fill quality, or profitable strategy performance.
"""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from arena import adapters
from arena.core import AUDIT_LIMITS, Engine, EngineError
from arena.service import Service
import test_service_autonomous as fixtures


class PaperBook(fixtures.FakePaper):
    """Stable submission IDs and an append-only, independently queried book."""

    def orders(self, status="all", *, after=None, after_order_id=None):
        rows = deepcopy(self.book)
        if after_order_id:
            found = next((i for i, row in enumerate(rows) if row["id"] == after_order_id), None)
            if found is None:
                raise adapters.ApiError("Unknown mock cursor.")
            rows = rows[found + 1:]
        if status == "open":
            rows = [row for row in rows if row["status"] not in {"filled", "canceled", "rejected", "expired"}]
        elif status == "closed":
            rows = [row for row in rows if row["status"] in {"filled", "canceled", "rejected", "expired"}]
        return rows

    def order_by_client_id(self, client_id):
        for row in self.book:
            if row["client_order_id"] == client_id:
                return deepcopy(row)
        raise adapters.OrderNotFoundError("Mock order not found.", status_code=404)


class AuditAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.services = []
        self.time = fixtures.START
        self.env = {"OPENAI_API_KEY": "test-openai", "ANTHROPIC_API_KEY": "test-claude"}
        for name in ("OPENAI", "CLAUDE"):
            self.env[f"ALPACA_{name}_KEY"] = f"test-{name}"
            self.env[f"ALPACA_{name}_SECRET"] = f"test-secret-{name}"
        self.clock = patch("arena.service.now", side_effect=lambda: self.time)
        self.clock.start()
        self.core_clock = patch("arena.core._now", side_effect=lambda: self.time.isoformat())
        self.core_clock.start()
        self.network = patch("socket.socket.connect", side_effect=AssertionError("No external network in acceptance tests."))
        self.network.start()
        self.brokers = {name: PaperBook(name, lambda: self.time) for name in ("openai", "claude")}
        self.svc = self.make_service()
        self.config = {
            "mode": "paper", "agent_mode": "autonomous", "total_capital": 500,
            "target": 10000, "loss_limit": 50, "position_cap_pct": 100,
            "exposure_cap_pct": 100, "monthly_model_budget": 10,
            "research_rounds": 3, "cycle_minutes": 60, "max_cycles_per_day": 4,
            "max_orders_per_cycle": 3,
            "agents": [
                {"id": name, "name": name, "provider": provider, "model": "test-model",
                 "weight": 1, "input_price": 1, "output_price": 1}
                for name, provider in (("openai", "openai"), ("claude", "anthropic"))
            ],
        }
        self.svc.new_experiment(self.config)
        self.svc.resume()

    def tearDown(self):
        self.network.stop()
        self.core_clock.stop()
        self.clock.stop()
        for svc in self.services:
            svc.meta.close()
            svc.engine.close()
        self.tmp.cleanup()

    def make_service(self):
        svc = Service(self.tmp.name, environ=self.env)
        svc.broker = Mock(side_effect=lambda agent: self.brokers[agent["id"]])
        self.services.append(svc)
        return svc

    def agent(self, name="openai"):
        return next(a for a in self.svc.engine.state()["agents"] if a["id"] == name)

    def advance(self, seconds=301):
        self.time += dt.timedelta(seconds=seconds)

    def assert_no_submissions(self):
        for broker in self.brokers.values():
            broker.submit_order.assert_not_called()

    def test_definitive_rejection_releases_cash_and_allows_new_experiment(self):
        broker = self.brokers["openai"]
        broker.submit_order.side_effect = adapters.OrderRejectedError("Mock invalid order.", status_code=422)
        try:
            self.svc._submit(self.agent(), "MSFT", "buy", 100, notional=25)
        except ValueError:
            pass
        state = self.svc.engine.state()
        self.assertEqual(broker.submit_order.call_count, 1)
        self.assertEqual(len(state["orders"]), 1)
        self.assertEqual(state["orders"][0]["status"], "rejected")
        self.assertEqual(self.svc.engine.pending_orders(), [])
        self.assertEqual(self.agent()["reserved_cash"], 0)
        self.assertEqual(self.agent()["cash"], 250)
        self.svc.reconcile()
        self.svc.resume_agent("openai")
        old_id = state["experiment"]["id"]
        self.svc.new_experiment(self.config)
        self.assertNotEqual(self.svc.engine.state()["experiment"]["id"], old_id)

    def test_ambiguous_submission_404_stays_unknown_without_replay_then_recovers_confirmed_fill(self):
        broker = self.brokers["openai"]
        broker.submit_order.side_effect = adapters.SubmissionUncertainError("Mock lost response.")
        with self.assertRaises(ValueError):
            self.svc._submit(self.agent(), "MSFT", "buy", 100, notional=25)
        payload = deepcopy(broker.submit_order.call_args.args[0])
        client_id = payload["client_order_id"]
        for _ in range(3):
            self.advance()
            self.svc.monitor()
        self.assertEqual(broker.submit_order.call_count, 1)
        self.assertEqual(self.svc.engine.pending_orders()[0]["client_order_id"], client_id)
        self.assertTrue(self.svc.engine.state()["agent_controls"]["openai"]["paused"])
        with self.assertRaises((ValueError, EngineError)):
            self.svc.new_experiment(self.config)
        # The broker eventually discloses that the ORIGINAL request filled.
        broker._submit(payload)
        self.advance()
        self.svc.monitor()
        self.assertEqual(broker.submit_order.call_count, 1)
        self.assertEqual(self.svc.engine.pending_orders(), [])
        self.assertAlmostEqual(self.agent()["positions"]["MSFT"]["qty"], .25)
        self.assertFalse(self.svc.engine.state()["agent_controls"]["openai"]["paused"])

    def test_both_transient_account_failures_keep_automation_intent_and_recover(self):
        self.svc.engine.set_autopilot(True)
        original = {name: broker.account for name, broker in self.brokers.items()}
        for broker in self.brokers.values():
            broker.account = Mock(side_effect=adapters.TransientApiError("Mock connection lost."))
        self.svc.monitor()
        state = self.svc.engine.state()
        self.assertTrue(state["experiment"]["autopilot"])
        self.assertNotEqual(state["experiment"]["status"], "halted")
        self.assert_no_submissions()
        for name, broker in self.brokers.items():
            broker.account = original[name]
        self.advance()
        self.svc.monitor()
        state = self.svc.engine.state()
        self.assertTrue(state["experiment"]["autopilot"])
        self.assertEqual(state["experiment"]["status"], "ready")
        self.assertTrue(all(not c["paused"] for c in state["agent_controls"].values()))
        self.assert_no_submissions()

    def test_restart_preserves_intent_but_requires_successful_startup_reconciliation(self):
        self.svc.engine.set_autopilot(True)
        self.svc = self.make_service()
        state = self.svc.engine.state()
        self.assertEqual(state["experiment"]["status"], "recovering")
        self.assertTrue(state["experiment"]["autopilot"])
        original = {name: broker.account for name, broker in self.brokers.items()}
        for broker in self.brokers.values():
            broker.account = Mock(side_effect=adapters.TransientApiError("Mock startup outage."))
        with patch("arena.service.autonomous_turn") as paid:
            self.svc.cycle()
            paid.assert_not_called()
        self.assert_no_submissions()
        self.assertNotEqual(self.svc.engine.state()["experiment"]["status"], "ready")
        for name, broker in self.brokers.items():
            broker.account = original[name]
        self.advance()
        self.svc.monitor()
        state = self.svc.engine.state()
        self.assertEqual(state["experiment"]["status"], "ready")
        self.assertTrue(state["experiment"]["autopilot"])
        self.assert_no_submissions()

    def test_operator_pause_survives_transient_error_recovery_and_restart(self):
        reason = "Keep Astra paused until my review."
        self.svc.halt_agent("openai", reason)
        self.svc.engine.set_autopilot(True)
        original = self.brokers["openai"].account
        self.brokers["openai"].account = Mock(side_effect=adapters.TransientApiError("Mock network loss."))
        self.svc.monitor()
        self.brokers["openai"].account = original
        self.advance()
        self.svc.monitor()
        self.svc = self.make_service()
        self.svc.monitor()
        control = self.svc.engine.state()["agent_controls"]["openai"]
        self.assertTrue(control["paused"])
        self.assertEqual(control["reason"], reason)
        self.assert_no_submissions()

    def test_external_stop_survives_restart_and_read_only_monitor(self):
        self.svc.engine.set_autopilot(True)
        self.svc.halt("Operator emergency stop.")
        self.svc = self.make_service()
        with patch("arena.service.autonomous_turn") as paid:
            self.svc.monitor()
            self.svc.cycle()
            paid.assert_not_called()
        state = self.svc.engine.state()
        self.assertTrue(Path(self.tmp.name, "STOP").exists())
        self.assertEqual(state["experiment"]["status"], "halted")
        self.assertFalse(state["experiment"]["autopilot"])
        self.assert_no_submissions()

    def test_two_operator_pauses_survive_successful_startup_recovery(self):
        for name in self.brokers:
            self.svc.halt_agent(name, "Operator paused " + name)
        self.svc.engine.set_autopilot(True)
        self.svc = self.make_service()
        self.svc.monitor()
        state = self.svc.engine.state()
        self.assertEqual(state["experiment"]["status"], "ready")
        self.assertTrue(state["experiment"]["autopilot"])
        self.assertTrue(all(c["paused"] for c in state["agent_controls"].values()))
        with patch("arena.service.autonomous_turn") as paid:
            self.svc.cycle()
            paid.assert_not_called()
        self.assert_no_submissions()

    def test_inventory_integrity_failure_stays_paused_after_reads_recover(self):
        self.svc.engine.set_autopilot(True)
        self.brokers["openai"].holdings["MSFT"] = 1
        self.svc.monitor()
        state = self.svc.engine.state()
        self.assertTrue(state["agent_controls"]["openai"]["paused"])
        self.assertEqual(state["agent_controls"]["openai"]["pause_kind"], "integrity")
        self.assertFalse(state["agent_controls"]["claude"]["paused"])
        self.assertEqual(state["experiment"]["status"], "ready")
        # A later matching inventory cannot erase the need for operator review.
        self.brokers["openai"].holdings.clear()
        self.advance()
        self.svc.monitor()
        self.assertTrue(self.svc.engine.state()["agent_controls"]["openai"]["paused"])
        self.svc = self.make_service()
        self.svc.monitor()
        state = self.svc.engine.state()
        self.assertEqual(state["experiment"]["status"], "ready")
        self.assertTrue(state["agent_controls"]["openai"]["paused"])
        self.assertEqual(state["agent_controls"]["openai"]["pause_kind"], "integrity")
        self.assert_no_submissions()

    def test_model_cost_reduces_net_result_without_consuming_trading_loss_limit(self):
        self.svc.engine.charge_model("openai", 60, call_id="acceptance-fee")
        state = self.svc.engine.state()
        self.assertEqual(state["experiment"]["status"], "ready")
        self.assertEqual(state["experiment"]["equity"], 440)
        self.assertEqual(self.agent()["cash"], 250)
        self.assertEqual(self.agent()["model_cost"], 60)
        self.assertEqual(self.agent()["net_pnl"], -60)
        self.svc.engine.charge_model("openai", 60, call_id="acceptance-fee")
        self.assertEqual(self.agent()["model_cost"], 60)
        self.svc = self.make_service()
        self.svc.engine.charge_model("openai", 60, call_id="acceptance-fee")
        self.assertEqual(self.agent()["model_cost"], 60)

    def test_actual_trading_loss_halt_survives_restart_and_cannot_auto_recover(self):
        self.svc._submit(self.agent(), "MSFT", "buy", 100, notional=100)
        self.svc.engine.set_autopilot(True)
        self.svc.engine.charge_model("openai", 60, call_id="separate-model-cost")
        self.brokers["openai"].prices["MSFT"] = 40
        self.advance(1)
        self.svc.monitor()
        self.assertEqual(self.svc.engine.state()["experiment"]["status"], "halted")
        self.svc = self.make_service()
        self.svc.monitor()
        self.assertEqual(self.svc.engine.state()["experiment"]["status"], "halted")
        with self.assertRaises((ValueError, EngineError)):
            self.svc.resume()

    def test_missing_held_quote_preserves_mark_and_does_not_block_other_symbol_exit(self):
        broker = self.brokers["openai"]
        for symbol in ("MSFT", "NVDA"):
            self.svc._submit(self.agent(), symbol, "buy", 100, notional=25)
        self.svc._record_autonomy("openai", {"exits": {
            symbol: fixtures.exit_rule(symbol=symbol, stop=95) for symbol in ("MSFT", "NVDA")
        }})
        original = broker.quotes
        broker.quotes = lambda symbols: {s: row for s, row in original(symbols).items() if s != "MSFT"}
        broker.prices["NVDA"] = 94
        broker.submit_order.reset_mock()
        self.advance(1)
        self.svc.monitor()
        state = self.svc.engine.state()
        self.assertEqual(state["market"]["prices"]["MSFT"], 100)
        self.assertFalse(state["agent_controls"]["openai"]["paused"])
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(broker.submit_order.call_count, 1)
        self.assertEqual(broker.submit_order.call_args.args[0]["symbol"], "NVDA")
        self.assertEqual(broker.submit_order.call_args.args[0]["side"], "sell")
        self.assertIn("MSFT", self.agent()["positions"])

    def test_operator_stop_wins_a_late_startup_recovery_response(self):
        self.svc.engine.set_autopilot(True)
        self.svc = self.make_service()
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = self.brokers["openai"].account

        def slow_account():
            entered.set()
            if not release.wait(3):
                raise AssertionError("Acceptance test timed out.")
            return original()

        def monitor():
            try:
                self.svc.monitor()
            except BaseException as exc:
                errors.append(exc)

        self.brokers["openai"].account = slow_account
        worker = threading.Thread(target=monitor)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.svc.halt("Operator stop while startup network request was pending.")
        finally:
            release.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        state = self.svc.engine.state()
        self.assertEqual(state["experiment"]["status"], "halted")
        self.assertFalse(state["experiment"]["autopilot"])
        self.assert_no_submissions()


class AuditStorageAcceptanceTests(unittest.TestCase):
    """Migration preserves old evidence while normal work stays bounded."""

    def legacy_payload(self, history_count=1600, decision_count=320, order_count=650):
        engine = Engine(":memory:")
        try:
            engine.resume()
            order = engine.reserve_order("openai", "SPY", "buy", 100, notional=10)
            engine.update_order(order["id"], {"status": "rejected", "filled_qty": "0"})
            payload = engine.state()
            template = payload["orders"][0]
        finally:
            engine.close()
        for key in ("storage_version", "audit_counts", "view_limits"):
            payload.pop(key, None)
        for agent in payload["agents"]:
            agent.pop("trade_count", None)
        payload["experiment"].update(status="paused", autopilot=False)
        payload["history"] = [
            {"at": (fixtures.START + dt.timedelta(seconds=i)).isoformat(), "equity": 500,
             "agents": {"openai": 250, "claude": 250}}
            for i in range(history_count)
        ]
        payload["decisions"] = [
            {"id": f"decision-{i}", "agent_id": "openai", "at": fixtures.START.isoformat(),
             "action": "research", "status": "plan", "reason": f"record-{i}",
             "evidence": [{"id": f"source-{i}", "body": "preserve-full-evidence-" + "x" * 1000}]}
            for i in range(decision_count)
        ]
        payload["orders"] = [
            {**template, "id": f"order-{i}", "client_order_id": f"arena-legacy-{i}"}
            for i in range(order_count)
        ]
        payload["events"] = [
            {"at": fixtures.START.isoformat(), "message": f"old-event-{i}", "level": "info"}
            for i in range(350)
        ]
        payload["agents"][0]["model_charges"] = [
            {"at": fixtures.START.isoformat(), "cost": .01, "call_id": f"legacy-call-{i}"}
            for i in range(150)
        ]
        payload["agents"][0]["model_cost"] = 1.5
        return payload

    @staticmethod
    def write_legacy(path, payload):
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE arena_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            db.execute("INSERT INTO arena_state VALUES(1,?)", (json.dumps(payload),))
            db.commit()
        finally:
            db.close()

    def test_legacy_blob_migration_keeps_all_audit_rows_and_old_cost_ids(self):
        payload = self.legacy_payload()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "arena.sqlite3"
            self.write_legacy(path, payload)
            engine = Engine(path)
            try:
                full, compact = engine.state(), engine.snapshot()
                for key in ("history", "decisions", "orders", "events"):
                    self.assertEqual(len(full[key]), len(payload[key]), key)
                    self.assertEqual(compact["audit_counts"][key], len(payload[key]), key)
                self.assertEqual(full["decisions"][0]["evidence"], payload["decisions"][0]["evidence"])
                self.assertEqual(len(compact["history"]), AUDIT_LIMITS["history"])
                self.assertEqual(len(compact["decisions"]), AUDIT_LIMITS["decisions"])
                self.assertNotIn("evidence", compact["decisions"][0])
                self.assertEqual(len(compact["orders"]), AUDIT_LIMITS["closed_orders"])
                self.assertEqual(len(full["agents"][0]["model_charges"]), 150)
                self.assertEqual(len(compact["agents"][0]["model_charges"]), AUDIT_LIMITS["model_charges"])
                engine.charge_model("openai", 100, call_id="legacy-call-0")
                self.assertAlmostEqual(engine.snapshot()["agents"][0]["model_cost"], 1.5)
                self.assertEqual(engine.order_by_client_id("arena-legacy-0")["id"], "order-0")
            finally:
                engine.close()
            engine = Engine(path)
            try:
                self.assertEqual(len(engine.state()["decisions"]), 320)
                self.assertEqual(engine.state()["decisions"][0]["evidence"], payload["decisions"][0]["evidence"])
                engine.charge_model("openai", 100, call_id="legacy-call-0")
                self.assertAlmostEqual(engine.snapshot()["agents"][0]["model_cost"], 1.5)
            finally:
                engine.close()

    def test_price_tick_writes_only_changed_audit_record_not_full_history(self):
        payload = self.legacy_payload()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "arena.sqlite3"
            self.write_legacy(path, payload)
            engine = Engine(path)
            statements = []
            try:
                engine._db.set_trace_callback(statements.append)
                engine.mark({"SPY": 123}, (fixtures.START + dt.timedelta(days=1)).isoformat())
                engine._db.set_trace_callback(None)
                writes = [q for q in statements if q.startswith("INSERT INTO arena_records")]
                self.assertEqual(len(writes), 1, "A quote update rewrote unrelated audit records.")
                full = engine.state()
                self.assertEqual(len(full["history"]), len(payload["history"]) + 1)
                self.assertEqual(full["decisions"][0]["evidence"], payload["decisions"][0]["evidence"])
                active_bytes = engine._db.execute("SELECT length(payload) FROM arena_state WHERE id=1").fetchone()[0]
                self.assertLess(active_bytes, len(json.dumps(payload)) / 2)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
