from contextlib import closing
"""Shared v0.5 storage, policy, and full-period chart acceptance checks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from arena.core import AUDIT_LIMITS, Engine, EngineError


def payload():
    engine = Engine(":memory:")
    state = engine.state()
    engine.close()
    for field in ("storage_version", "audit_counts", "migration"):
        state.pop(field, None)
    for field in ("auto_resume", "loss_limit_includes_model_costs", "output_token_limit"):
        state["experiment"].pop(field, None)
    return state


def legacy(path, state, claude=False, archive=None):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE arena_state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")
        db.execute("CREATE TABLE arena_archives(id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        db.execute("INSERT INTO arena_state VALUES(1,?)", (json.dumps(state),))
        if archive:
            db.execute("INSERT INTO arena_archives VALUES(?,?)", (archive["experiment"]["id"], json.dumps(archive)))
        if claude:
            db.execute("CREATE TABLE arena_decisions(id TEXT PRIMARY KEY,experiment TEXT NOT NULL,agent_id TEXT NOT NULL,at TEXT NOT NULL,payload TEXT NOT NULL)")
            db.execute("CREATE TABLE arena_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,experiment TEXT NOT NULL,at TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL)")


class V05CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "arena.sqlite3"
        self.engine = None

    def tearDown(self):
        if self.engine:
            self.engine.close()
        self.temp.cleanup()

    def open(self):
        self.engine = Engine(self.path)
        return self.engine

    def test_legacy_policy_and_full_evidence_preserved_with_checked_backup(self):
        state = payload()
        state["agents"][0]["model_cost"] = 2
        state["experiment"]["loss_limit"] = 1
        state["decisions"] = [{"id": "d1", "agent_id": "openai", "action": "hold", "at": "2026-01-01", "evidence": ["unaltered"]}]
        legacy(self.path, state)
        engine = self.open()
        full = engine.state()
        self.assertTrue(full["experiment"]["loss_limit_includes_model_costs"])
        self.assertEqual(full["experiment"]["status"], "halted")
        self.assertEqual(full["decisions"], state["decisions"])
        report = engine.migration_report
        self.assertEqual(report["to_version"], 3)
        self.assertEqual(report["verified"][state["experiment"]["id"]]["decisions"]["count"], 1)
        backup = Path(report["backup"])
        self.assertTrue(backup.exists())
        with closing(sqlite3.connect(backup)) as db, db:
            self.assertEqual(json.loads(db.execute("SELECT payload FROM arena_state").fetchone()[0]), state)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        engine.close()
        self.engine = None
        self.open()
        self.assertEqual(len(list(self.path.parent.glob("*.bak"))), 1)

    def test_ours_v2_preserves_trading_only_policy_and_indexed_evidence(self):
        engine = self.open()
        original = engine.record_decision("openai", {"reason": "hold", "evidence": ["full source"]})
        engine.close()
        self.engine = None
        with closing(sqlite3.connect(self.path)) as db, db:
            state = json.loads(db.execute("SELECT payload FROM arena_state").fetchone()[0])
            state["storage_version"] = 2
            state["experiment"].pop("loss_limit_includes_model_costs")
            db.execute("UPDATE arena_state SET payload=?", (json.dumps(state),))
            db.execute("DROP TABLE arena_chart")
        engine = self.open()
        self.assertFalse(engine.state()["experiment"]["loss_limit_includes_model_costs"])
        self.assertEqual(engine.state()["decisions"], [original])

    def test_claude_active_and_archive_evidence_events_and_multiplicity(self):
        state, archive = payload(), payload()
        state["experiment"]["loss_limit_includes_model_costs"] = False
        event = {"at": "2026-01-01T00:00:00+00:00", "level": "info", "message": "repeated event"}
        state["events"] = [deepcopy(event)]
        archive["events"] = [deepcopy(event)]
        for i, item in enumerate((state, archive)):
            item["decisions"] = [{"id": f"d{i}", "agent_id": "openai", "at": event["at"], "action": "hold", "detail": True}]
        legacy(self.path, state, claude=True, archive=archive)
        with closing(sqlite3.connect(self.path)) as db, db:
            for item in (state, archive):
                exp = item["experiment"]["id"]
                decision = {k: v for k, v in item["decisions"][0].items() if k != "detail"}
                decision["evidence"] = [{"text": "required private research history"}]
                db.execute("INSERT INTO arena_decisions VALUES(?,?,?,?,?)", (decision["id"], exp, "openai", decision["at"], json.dumps(decision)))
                db.executemany("INSERT INTO arena_events(experiment,at,level,message) VALUES(?,?,?,?)", [(exp, event["at"], event["level"], event["message"])] * 3001)
        engine = self.open()
        full = engine.state()
        saved = json.loads(engine._db.execute("SELECT payload FROM arena_archives WHERE id=?", (archive["experiment"]["id"],)).fetchone()[0])
        for item in (full, saved):
            self.assertEqual(len(item["events"]), 3002)
            self.assertEqual(item["decisions"][0]["evidence"][0]["text"], "required private research history")
        self.assertFalse(full["experiment"]["loss_limit_includes_model_costs"])
        self.assertTrue(full["experiment"]["legacy_restart_preference_pending"])
        engine.set_auto_resume(False)
        self.assertNotIn("legacy_restart_preference_pending", engine.snapshot()["experiment"])
        self.assertEqual(engine._db.execute("SELECT count(*) FROM arena_events").fetchone()[0], 6002)

    def test_failed_verification_rolls_back_all_imports_retains_backup(self):
        state = payload()
        state["decisions"] = [{"id": "same", "agent_id": "openai", "at": str(i), "action": "hold"} for i in range(2)]
        legacy(self.path, state)
        with self.assertRaisesRegex(EngineError, "verification failed"):
            self.open()
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(json.loads(db.execute("SELECT payload FROM arena_state").fetchone()[0]), state)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='arena_records'").fetchone())
        self.assertEqual(len(list(self.path.parent.glob("*.bak"))), 1)

    def test_write_failure_rolls_back_migration_and_retains_source(self):
        state = payload()
        legacy(self.path, state)
        with patch.object(Engine, "_save_state", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.open()
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(json.loads(db.execute("SELECT payload FROM arena_state").fetchone()[0]), state)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='arena_chart'").fetchone())

    def test_unknown_schema_or_version_refused_before_database_mutation(self):
        state = payload()
        state["storage_version"] = 99
        legacy(self.path, state)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EngineError, "Unknown storage"):
            self.open()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)
        self.assertFalse(list(self.path.parent.glob("*.bak")))
        with closing(sqlite3.connect(self.path)) as db, db:
            state.pop("storage_version")
            db.execute("UPDATE arena_state SET payload=?", (json.dumps(state),))
            db.execute("ALTER TABLE arena_state ADD COLUMN custom TEXT")
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EngineError, "Unknown arena_state layout"):
            self.open()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)

    def test_chart_keeps_middle_extremes_all_agents_and_raw_audit(self):
        engine = self.open()
        day = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(5000):
            rows.append({"at": (day + timedelta(minutes=i)).isoformat(), "equity": 600 if i == 500 else (400 if i == 1500 else 500),
                         "agents": {"openai": 400 if i == 700 else 250, "claude": 100 if i == 900 else 250}})
        with engine._write():
            engine._state["history"].extend(rows)
        sample = engine.snapshot()["history"]
        self.assertEqual(len(sample), AUDIT_LIMITS["history"])
        for i in (500, 700, 900, 1500, 4999):
            self.assertIn(rows[i], sample)
        self.assertEqual(len(engine.state()["history"]), 5001)
        reads = []
        engine._db.set_trace_callback(reads.append)
        engine.snapshot()
        engine._db.set_trace_callback(None)
        self.assertEqual(reads, [])
        engine.close()
        self.engine = None
        self.assertEqual(self.open().snapshot()["history"], sample)

    def test_chart_updates_revised_extreme_and_configure_clears_prior_chart(self):
        engine = self.open()
        engine.mark({"SPY": 100}, "2026-01-01T00:00:00+00:00")
        row = {"at": "old-point", "equity": 1000000, "agents": {"openai": 1000000, "claude": 250}}
        with engine._write():
            engine._state["history"].append(row)
        with engine._write():
            engine._state["history"][-1] = {**row, "equity": 500, "agents": {"openai": 250, "claude": 250}}
        self.assertNotIn(1000000, [item["equity"] for item in engine.snapshot()["history"]])
        engine.configure({"total_capital": 1000})
        self.assertEqual(len(engine.snapshot()["history"]), 1)
        self.assertEqual(engine.snapshot()["history"][0]["equity"], 1000)

    def test_persistent_restart_preference_blocks_late_recovery(self):
        engine = self.open()
        engine.configure({"mode": "paper"})
        engine.resume()
        engine.set_autopilot(True)
        engine.close()
        self.engine = None
        engine = self.open()
        self.assertEqual(engine.snapshot()["experiment"]["status"], "recovering")
        engine.close()
        self.engine = None
        engine = self.open()
        self.assertTrue(engine.snapshot()["experiment"]["autopilot"])
        for agent in engine.snapshot()["agents"]:
            engine.note_reconciled(agent["id"])
        engine.set_auto_resume(False)
        with self.assertRaises(EngineError):
            engine.recover_startup()
        engine.close()
        self.engine = None
        engine = self.open()
        self.assertFalse(engine.snapshot()["experiment"]["auto_resume"])
        self.assertFalse(engine.snapshot()["experiment"]["autopilot"])

    def test_output_cap_and_loss_policy_config_validation(self):
        engine = self.open()
        for value in (True, 1023, 16385, 8192.5):
            with self.assertRaises(EngineError):
                engine.configure({"output_token_limit": value})
        for field in ("auto_resume", "loss_limit_includes_model_costs"):
            with self.assertRaises(EngineError):
                engine.configure({field: "true"})
        engine.configure({"output_token_limit": 16384, "loss_limit_includes_model_costs": True, "loss_limit": 1})
        engine.resume()
        engine.charge_model("openai", 2)
        self.assertEqual(engine.snapshot()["experiment"]["status"], "halted")
        with self.assertRaises(EngineError):
            engine.resume()

    def test_disconnected_demo_decisions_are_holds(self):
        engine = self.open()
        engine.resume()
        engine.demo_step(5)
        self.assertTrue(engine.state()["decisions"])
        self.assertTrue(all(item["action"] == "hold" for item in engine.state()["decisions"]))
        self.assertFalse(engine.state()["orders"])


if __name__ == "__main__":
    unittest.main()
