from contextlib import closing
"""Regression cases for durable audit storage and explicit recovery boundaries."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from arena.core import AUDIT_LIMITS, Engine, EngineError


class IncrementalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "arena.db"
        self.engine = Engine(self.path)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_v03_blob_migration_preserves_full_evidence_and_bounded_state(self):
        original = self.engine.state()
        original.pop("storage_version", None)
        original.pop("audit_counts", None)
        evidence = {"source": "original full source " * 500, "request": {"at": "2026-01-01"}}
        original["decisions"] = [{"id": str(i), "agent_id": "openai", "at": str(i), "action": "hold", "reason": "wait", "status": "recorded", "evidence": evidence} for i in range(120)]
        original["history"] = [{"at": f"2026-01-01T00:{i // 60:02}:{i % 60:02}+00:00", "equity": 500, "agents": {"openai": 250, "claude": 250}} for i in range(700)]
        original["events"] = [{"at": str(i), "message": "legacy event", "level": "info"} for i in range(240)]
        original["agents"][0]["model_charges"] = [{"at": str(i), "cost": .01, "call_id": f"call-{i}"} for i in range(130)]
        original["agents"][0]["model_cost"] = 1.3
        self.engine.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE arena_records")
            db.execute("UPDATE arena_state SET payload=? WHERE id=1", (json.dumps(original),))
        self.engine = Engine(self.path)
        full = self.engine.state()
        snap = self.engine.snapshot()
        self.assertEqual(full["decisions"], original["decisions"])
        self.assertEqual(full["history"], original["history"])
        self.assertEqual(len(full["events"]), 240)
        self.assertEqual(len(full["agents"][0]["model_charges"]), 130)
        self.assertEqual(len(snap["history"]), AUDIT_LIMITS["history"])
        self.assertEqual(snap["audit_counts"]["decisions"], 120)
        self.assertNotIn("evidence", snap["decisions"][-1])
        self.engine.mark({"SPY": 100}, "2026-01-02T00:00:00+00:00")
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.engine.state()["decisions"], original["decisions"])
        self.assertEqual(len(self.engine.state()["history"]), 701)
        # An old charge, evicted from the active window, remains idempotent.
        self.engine.charge_model("openai", 10, call_id="call-0")
        self.assertEqual(self.engine.snapshot()["agents"][0]["model_cost"], 1.3)

    def test_long_history_writes_do_not_rewrite_audit_rows(self):
        with self.engine._write():
            self.engine._state["history"].extend({"at": f"old-{i}", "equity": 500, "agents": {"openai": 250, "claude": 250}} for i in range(5000))
        writes = []
        self.engine._db.set_trace_callback(lambda sql: writes.append(sql) if sql.startswith("INSERT INTO arena_records") else None)
        self.engine.mark({"SPY": 100}, "2026-01-01T00:00:00+00:00")
        self.engine._db.set_trace_callback(None)
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(self.engine.state()["history"]), 5002)
        self.assertEqual(len(self.engine.snapshot()["history"]), 512)
        payload_size = self.engine._db.execute("SELECT length(payload) FROM arena_state WHERE id=1").fetchone()[0]
        self.assertLess(payload_size, 100000)
        reads = []
        self.engine._db.set_trace_callback(reads.append)
        self.engine.snapshot()
        self.engine._db.set_trace_callback(None)
        self.assertEqual(reads, [])

    def test_old_terminal_orders_remain_lookupable_and_fills_idempotent(self):
        self.engine.configure({"fee_bps": 0})
        self.engine.resume()
        old = self.engine.reserve_order("openai", "SPY", "buy", 10, notional=10)
        self.engine.update_order(old["id"], {"id": "broker-first", "status": "filled", "filled_qty": 1, "filled_avg_price": 10})
        with self.engine._write():
            for i in range(210):
                item = dict(old, id=f"later-{i}", client_order_id=f"client-{i}", status="rejected", filled_qty=0, reserved=0)
                self.engine._state["orders"].append(item)
        self.assertNotIn(old["id"], [o["id"] for o in self.engine.snapshot()["orders"]])
        self.assertTrue(self.engine.has_broker_order("broker-first"))
        self.assertEqual(self.engine.order_by_client_id(old["client_order_id"])["status"], "filled")
        self.engine.update_order(old["id"], {"status": "filled", "filled_qty": 1, "filled_avg_price": 10})
        agent = self.engine.snapshot()["agents"][0]
        self.assertEqual(agent["positions"]["SPY"]["qty"], 1)
        self.assertEqual(agent["trade_count"], 1)
        self.assertEqual(len(self.engine.orders("openai")), 211)

    def test_transaction_failure_rolls_back_audit_and_active_ledger(self):
        old = self.engine.state()
        original_save = self.engine._save_state
        def fail():
            raise RuntimeError("disk write failure")
        self.engine._save_state = fail
        with self.assertRaises(RuntimeError):
            self.engine.record_decision("openai", {"action": "hold", "reason": "will roll back"})
        self.engine._save_state = original_save
        self.assertEqual(self.engine.state(), old)

    def test_full_archive_keeps_evidence_after_active_window_eviction(self):
        for i in range(105):
            self.engine.record_decision("openai", {"reason": "hold", "evidence": "original-source", "n": i})
        old_id = self.engine.snapshot()["experiment"]["id"]
        self.engine.new_experiment({})
        archived = json.loads(self.engine._db.execute("SELECT payload FROM arena_archives WHERE id=?", (old_id,)).fetchone()[0])
        self.assertEqual(len(archived["decisions"]), 105)
        self.assertEqual(archived["decisions"][0]["evidence"], "original-source")
        self.assertEqual(self.engine.snapshot()["audit_counts"]["decisions"], 0)

    def test_automatic_recovery_never_clears_operator_or_integrity_stop(self):
        for kind in ("operator", "integrity"):
            self.engine.halt_agent("openai", "stop", kind=kind)
            self.engine.halt_agent("openai", "temporary failure", kind="transient")
            self.engine.note_reconciled("openai")
            with self.assertRaises(EngineError):
                self.engine.resume_agent("openai", automatic=True)
            self.assertEqual(self.engine.snapshot()["agent_controls"]["openai"]["pause_kind"], kind)
            self.engine.resume_agent("openai")
        self.engine.halt_agent("openai", "network unavailable", kind="transient")
        self.engine.note_reconciled("openai")
        self.engine.resume_agent("openai", automatic=True)
        self.assertFalse(self.engine.snapshot()["agent_controls"]["openai"]["paused"])

    def test_model_charges_do_not_change_trading_caps_or_loss_stop(self):
        self.engine.configure({"loss_limit": 1, "fee_bps": 0})
        self.engine.resume()
        self.engine.charge_model("openai", 60, "provider-only")
        self.assertEqual(self.engine.snapshot()["experiment"]["status"], "ready")
        # Thirty percent of $250 trading equity, even after model fees.
        self.engine.reserve_order("openai", "SPY", "buy", 10, notional=75)
        exp = self.engine.snapshot()["experiment"]
        self.assertEqual(exp["trading_pnl"], 0)
        self.assertEqual(exp["net_pnl"], -60)

    def test_restart_with_both_agents_paused_finishes_checks_preserving_stops(self):
        self.engine.configure({"mode": "paper"})
        self.engine.resume()
        self.engine.set_autopilot(True)
        for agent in self.engine.snapshot()["agents"]:
            self.engine.halt_agent(agent["id"])
        self.engine.close()
        self.engine = Engine(self.path)
        with self.assertRaises(EngineError):
            self.engine.recover_startup()
        for agent in self.engine.snapshot()["agents"]:
            self.engine.note_reconciled(agent["id"])
        self.engine.recover_startup()
        state = self.engine.snapshot()
        self.assertEqual(state["experiment"]["status"], "ready")
        self.assertTrue(state["experiment"]["autopilot"])
        self.assertTrue(all(c["paused"] and c["pause_kind"] == "operator" for c in state["agent_controls"].values()))

    def test_operator_halt_survives_auto_restart_without_recovery(self):
        self.engine.configure({"mode": "paper"})
        self.engine.resume()
        self.engine.set_autopilot(True)
        self.engine.halt("Operator emergency stop")
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.engine.snapshot()["experiment"]["status"], "halted")
        with self.assertRaises(EngineError):
            self.engine.recover_startup()
        self.assertFalse(self.engine.snapshot()["experiment"]["autopilot"])


if __name__ == "__main__":
    unittest.main()
