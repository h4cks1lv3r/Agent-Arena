"""Service safety regressions; every broker/provider interaction is mocked."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from arena.adapters import ApiError
from arena.service import Service


NOW = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.timezone.utc)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "OPENAI_API_KEY": "mock-provider-key",
            "ALPACA_OPENAI_KEY": "mock-paper-key",
            "ALPACA_OPENAI_SECRET": "mock-paper-secret",
        }
        self.services = []
        self.clock_patch = patch("arena.service.now", return_value=NOW)
        self.clock_patch.start()
        self.svc = self.make_service()

    def make_service(self):
        svc = Service(self.tmp.name, environ=self.env)
        self.services.append(svc)
        return svc

    def tearDown(self):
        self.clock_patch.stop()
        # Both state stores must be closed before TemporaryDirectory cleanup on Windows.
        for svc in self.services:
            for owner in (svc, svc.engine):
                for value in vars(owner).values():
                    if isinstance(value, sqlite3.Connection):
                        value.close()
        self.tmp.cleanup()

    def configure_paper(self, budget=1):
        self.svc.new_experiment({
            "mode": "paper", "agent_mode": "reviewer", "compound_profits": False, "total_capital": 500, "target": 10000,
            "loss_limit": 50, "position_cap_pct": 30, "exposure_cap_pct": 60,
            "slippage_bps": 5, "fee_bps": 1, "monthly_model_budget": budget,
            "agents": [{"id": "openai", "name": "AI reviewer", "provider": "openai",
                        "model": "test-model", "weight": 1, "input_price": 1,
                        "output_price": 1}],
        })
        self.svc.engine.resume()
        return self.svc.engine.state()["agents"][0]

    def fake_broker(self):
        broker = Mock()
        broker.feed = "iex"
        broker.account.return_value = {"id": "paper-account-one", "status": "ACTIVE", "cash": "100000"}
        broker.positions.return_value = []
        broker.clock.return_value = {"is_open": True, "timestamp": NOW.isoformat()}
        broker.asset.return_value = {"tradable": True, "fractionable": True}
        broker.quotes.side_effect = lambda symbols: {
            s: {"price": 180.0, "bid": 179.99, "ask": 180.01, "t": NOW.isoformat()}
            for s in symbols
        }
        dates = [NOW.date() - dt.timedelta(days=i) for i in range(1, 120)]
        dates = sorted(d for d in dates if d.weekday() < 5)[-60:]
        broker.calendar.return_value = [{"date": d.isoformat()} for d in dates]
        bars = [{"t": d.isoformat() + "T04:00:00Z", "o": 120 + i,
                 "h": 121 + i, "l": 119 + i, "c": 120 + i, "v": 1000000}
                for i, d in enumerate(dates)]
        broker.bars.return_value = {s: bars for s in ("SPY", "QQQ", "IWM")}
        orders = []
        broker.orders.side_effect = lambda status="all", **kwargs: list(orders)

        def submit(payload):
            order = {**payload, "id": "mock-order-" + str(len(orders)),
                     "status": "new", "filled_qty": "0", "filled_avg_price": None}
            orders.append(order)
            return order

        broker.submit_order.side_effect = submit
        broker.order_by_client_id.side_effect = lambda cid: next(o for o in orders if o["client_order_id"] == cid)
        return broker

    def test_zero_budget_never_calls_provider_even_if_price_rounds_to_zero(self):
        agent = self.configure_paper(budget=0)
        with patch("arena.service.review_candidate") as paid:
            for price in (1.0, 1e-9):
                with self.subTest(price=price):
                    result = self.svc._review({**agent, "input_price": price, "output_price": price}, {"symbol": "SPY"})
                    self.assertFalse(result["approve"])
                    self.assertEqual(result["status"], "budget_blocked")
            paid.assert_not_called()
        self.assertEqual(self.svc.meta.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)

    def test_restart_does_not_charge_reserved_provider_call_twice(self):
        agent = self.configure_paper()
        experiment = self.svc.engine.state()["experiment"]["id"]
        call_id = "interrupted-after-engine-charge"
        self.svc.meta.execute("INSERT INTO calls VALUES (?,?,?,?,?,?)",
                              (call_id, experiment, agent["id"], "2026-09", .10, "reserved"))
        self.svc.meta.commit()
        # Simulate process death after engine accounting but before the service commit.
        self.svc.engine.charge_model(agent["id"], .02, call_id=call_id)
        restarted = self.make_service()
        self.assertAlmostEqual(restarted.engine.state()["agents"][0]["model_cost"], .02)
        self.assertEqual(restarted.meta.execute("SELECT status FROM calls WHERE id=?", (call_id,)).fetchone()[0], "uncertain")
        restarted_again = self.make_service()
        self.assertAlmostEqual(restarted_again.engine.state()["agents"][0]["model_cost"], .02)

    def test_daily_claim_prevents_second_paid_review_and_submission(self):
        self.configure_paper()
        broker = self.fake_broker()
        review = {"approve": True, "reason": "Mock approval", "input_tokens": 100,
                  "output_tokens": 20, "model": "test-model"}
        with patch.object(self.svc, "broker", return_value=broker), patch("arena.service.review_candidate", return_value=review) as paid:
            self.svc.cycle()
            self.svc.cycle()
        self.assertEqual(paid.call_count, 1)
        self.assertEqual(broker.submit_order.call_count, 1)

    def test_unknown_submission_is_reconciled_without_retry(self):
        self.configure_paper()
        broker = self.fake_broker()
        broker.submit_order.side_effect = ApiError("Mock lost acknowledgment")
        broker.order_by_client_id.side_effect = ApiError("Mock unresolved order")
        review = {"approve": True, "reason": "Mock approval", "input_tokens": 100,
                  "output_tokens": 20, "model": "test-model"}
        with patch.object(self.svc, "broker", return_value=broker), patch("arena.service.review_candidate", return_value=review):
            with self.assertRaises(ValueError):
                self.svc.cycle()
            self.assertEqual(self.svc.engine.state()["experiment"]["status"], "ready")
            self.assertTrue(self.svc.engine.state()["agent_controls"]["openai"]["paused"])
            self.svc.cycle()  # Deferred read-only recovery does not fail the scheduler.
        self.assertEqual(broker.submit_order.call_count, 1)
        self.assertTrue(self.svc.engine.pending_orders())

    def test_submit_rechecks_quote_and_session_before_any_order(self):
        agent = self.configure_paper()
        broker = self.fake_broker()
        self.svc.engine.mark({"SPY": 180, "QQQ": 180, "IWM": 180}, NOW.isoformat())
        with patch.object(self.svc, "broker", return_value=broker):
            broker.quotes.side_effect = lambda symbols: {
                s: {"price": 180, "bid": 179.99, "ask": 180.01,
                    "t": (NOW - dt.timedelta(minutes=10)).isoformat()} for s in symbols
            }
            with self.assertRaises(ValueError):
                self.svc._submit(agent, "SPY", "buy", 180)
            broker.clock.return_value = {"is_open": False, "timestamp": NOW.isoformat()}
            with self.assertRaises(ValueError):
                self.svc._submit(agent, "SPY", "buy", 180)
        broker.submit_order.assert_not_called()
        self.assertEqual(self.svc.engine.pending_orders(), [])

    def test_stale_daily_bars_block_paid_reviews_and_orders(self):
        self.configure_paper()
        broker = self.fake_broker()
        # Quotes are fresh, but the newest completed session is absent from bars.
        broker.bars.return_value = {symbol: rows[:-1] for symbol, rows in broker.bars.return_value.items()}
        with patch.object(self.svc, "broker", return_value=broker), patch("arena.service.review_candidate") as paid:
            with self.assertRaisesRegex(ValueError, "last completed"):
                self.svc.cycle()
            paid.assert_not_called()
        broker.submit_order.assert_not_called()
        self.assertEqual(self.svc.engine.pending_orders(), [])

    def test_halt_takes_effect_while_network_cycle_lock_is_held(self):
        self.configure_paper()
        completed = threading.Event()
        errors = []

        def stop():
            try:
                self.svc.dispatch("/api/halt", {})
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=stop, daemon=True)
        try:
            with self.svc.lock:
                thread.start()
                self.assertTrue(completed.wait(1), "Stop waited for the network-cycle lock")
                self.assertTrue(self.svc.stop_file.exists())
        finally:
            thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(self.svc.engine.state()["experiment"]["status"], "halted")

    def test_loss_halt_blocks_further_paid_reviews_without_stop_file(self):
        agent = self.configure_paper()
        self.svc.engine.halt("Mock loss limit reached")
        self.assertFalse(self.svc.stop_file.exists())
        with patch("arena.service.review_candidate") as paid:
            result = self.svc._review(agent, {"symbol": "SPY"})
            self.assertFalse(result["approve"])
            paid.assert_not_called()

    def test_fractional_sell_quantity_uses_fixed_point_and_rounds_down(self):
        agent = self.configure_paper()
        broker = self.fake_broker()
        with patch.object(self.svc, "broker", return_value=broker):
            for symbol, held in (("SPY", .123456789123), ("QQQ", .000000123456789)):
                with self.subTest(symbol=symbol):
                    entry = self.svc.engine.reserve_order(agent["id"], symbol, "buy", 180, qty=held)
                    self.svc.engine.update_order(entry["id"], {
                        "id": "mock-filled-" + symbol, "status": "filled",
                        "filled_qty": str(held), "filled_avg_price": "180",
                    })
                    self.svc._submit(agent, symbol, "sell", 180)
                    payload = broker.submit_order.call_args.args[0]
                    quantity = payload["qty"]
                    self.assertIsInstance(quantity, str)
                    self.assertNotIn("e", quantity.lower())
                    self.assertLessEqual(len(quantity.partition(".")[2]), 9)
                    self.assertGreater(Decimal(quantity), 0)
                    self.assertLessEqual(Decimal(quantity), Decimal(str(held)))
                    self.assertEqual(payload["side"], "sell")
        self.assertEqual(broker.submit_order.call_count, 2)


    def test_mobile_snapshot_bounds_history_without_deleting_audit(self):
        self.configure_paper()
        with self.svc.engine._write():
            state = self.svc.engine._state
            state["history"].extend({"at": str(i), "equity": 500 + i, "agents": {"openai": 500 + i}} for i in range(1500))
            state["decisions"] = [{"id": str(i), "agent_id": "openai", "reason": "audit record", "action": "hold"} for i in range(150)]
            state["events"] = [{"at": str(i), "message": "event", "level": "info"} for i in range(250)]
        compact = self.svc.public_state()
        full = self.svc.public_state(compact=False)
        self.assertLessEqual(len(compact["history"]), 1000)
        self.assertEqual(compact["history"][0], full["history"][0])
        self.assertEqual(compact["history"][-1], full["history"][-1])
        self.assertEqual(len(compact["decisions"]), 100)
        self.assertEqual(len(compact["events"]), 200)
        self.assertTrue(compact["view_limits"]["truncated"]["history"])
        self.assertEqual(len(full["history"]), 1501)
        self.assertEqual(len(self.svc.engine.state()["decisions"]), 150)

    def test_public_snapshot_reads_do_not_query_service_database(self):
        self.configure_paper()
        self.svc._record_autonomy("openai", {"strategy": {"name": "Cached strategy"}})
        statements = []
        self.svc.meta.set_trace_callback(statements.append)
        try:
            snapshot = self.svc.public_state()
        finally:
            self.svc.meta.set_trace_callback(None)
        self.assertEqual(statements, [])
        self.assertEqual(snapshot["autonomy"]["openai"]["strategy"]["name"], "Cached strategy")
        self.assertTrue(snapshot["snapshot_at"])
        self.assertTrue(snapshot["server_time"])
        self.assertEqual(snapshot["monitor"]["refresh_seconds"], 15)


    def test_busy_quote_monitor_cannot_mutate_a_replacement_experiment(self):
        agent = self.configure_paper()
        order = self.svc.engine.reserve_order("openai", "SPY", "buy", 10, qty=1)
        self.svc.engine.update_order(order["id"], {"status": "filled", "filled_qty": 1, "filled_avg_price": 10})
        old_id = self.svc.engine.state()["experiment"]["id"]
        broker = self.fake_broker()
        def change_experiment(symbols):
            self.svc.engine.new_experiment({"mode": "paper"})
            return {symbol: {"price": 1, "t": NOW.isoformat()} for symbol in symbols}
        broker.quotes.side_effect = change_experiment
        with patch.object(self.svc, "broker", return_value=broker):
            result = self.svc._monitor_quotes_while_busy()
        fresh = self.svc.engine.state()
        self.assertNotEqual(fresh["experiment"]["id"], old_id)
        self.assertTrue(result["experiment_changed"])
        self.assertEqual(fresh["market"]["prices"], {})
        self.assertEqual(fresh["experiment"]["status"], "paused")
        self.assertFalse(any(c["paused"] for c in fresh["agent_controls"].values()))


    def test_compact_decisions_omit_research_payload_but_export_keeps_it(self):
        self.configure_paper()
        decision = self.svc.engine.record_decision("openai", {
            "action": "research", "symbol": "SPY", "reason": "Inspect current prices", "status": "planned",
            "snapshot": {"portfolio": {"cash": 500}},
            "evidence": [{"id": "evidence_example", "data": {"price": 180}}],
            "turns": [{"response": {"phase": "plan"}}],
            "plan": {"orders": [], "reason": "Hold"},
        })
        compact = self.svc.public_state()["decisions"][-1]
        full = self.svc.public_state(compact=False)["decisions"][-1]
        self.assertEqual(set(compact), {"id", "agent_id", "at", "action", "symbol", "reason", "status"})
        self.assertEqual(compact["id"], decision["id"])
        self.assertEqual(compact["reason"], "Inspect current prices")
        for key in ("snapshot", "evidence", "turns", "plan"):
            self.assertNotIn(key, compact)
            self.assertEqual(full[key], decision[key])
        self.assertEqual(self.svc.engine.state()["decisions"][-1], decision)


if __name__ == "__main__":
    unittest.main()
