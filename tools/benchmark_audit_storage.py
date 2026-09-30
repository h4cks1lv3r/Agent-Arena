#!/usr/bin/env python3
"""Reproduce the bounded-storage benchmark using invented audit history.

Run from any directory:
  python tools/benchmark_audit_storage.py
  python tools/benchmark_audit_storage.py --large --output docs/validation_storage.json

No broker, model API, external package, or network access is used. Temporary
databases are removed. --large adds 120,000 history rows and 8,000 research
decisions; this is a data-volume test, NOT a multi-week trading/uptime test.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arena.core import Engine

START = datetime(2026, 9, 17, 14, tzinfo=timezone.utc)


def legacy_payload(history_count, decision_count, order_count=650):
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
        {"at": (START + timedelta(seconds=i)).isoformat(), "equity": 500,
         "agents": {"openai": 250, "claude": 250}}
        for i in range(history_count)
    ]
    payload["decisions"] = [
        {"id": f"decision-{i}", "agent_id": "openai", "at": START.isoformat(),
         "action": "research", "status": "plan", "reason": f"record-{i}",
         "evidence": [{"id": f"source-{i}", "body": "preserve-full-evidence-" + "x" * 1000}]}
        for i in range(decision_count)
    ]
    payload["orders"] = [
        {**template, "id": f"order-{i}", "client_order_id": f"arena-legacy-{i}"}
        for i in range(order_count)
    ]
    payload["events"] = [
        {"at": START.isoformat(), "message": f"old-event-{i}", "level": "info"}
        for i in range(350)
    ]
    payload["agents"][0]["model_charges"] = [
        {"at": START.isoformat(), "cost": .01, "call_id": f"legacy-call-{i}"}
        for i in range(150)
    ]
    payload["agents"][0]["model_cost"] = 1.5
    return payload


def run_case(history_count, decision_count, samples):
    payload = legacy_payload(history_count, decision_count)
    encoded = json.dumps(payload)
    with tempfile.TemporaryDirectory(prefix="arena-storage-benchmark-") as folder:
        path = Path(folder) / "arena.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE arena_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        db.execute("INSERT INTO arena_state VALUES(1,?)", (encoded,))
        db.commit()
        db.close()
        started = time.perf_counter()
        engine = Engine(path)
        migration_seconds = time.perf_counter() - started
        try:
            tick_times, snapshot_times = [], []
            for i in range(samples):
                stamp = (START + timedelta(days=7, minutes=i)).isoformat()
                started = time.perf_counter()
                engine.mark({"SPY": 100 + i / 100}, stamp)
                tick_times.append(time.perf_counter() - started)
                started = time.perf_counter()
                engine.snapshot()
                snapshot_times.append(time.perf_counter() - started)
            active_bytes = engine._db.execute("SELECT length(payload) FROM arena_state WHERE id=1").fetchone()[0]
            full = engine.state()
            expected = {"history": history_count + samples, "decisions": decision_count,
                        "orders": 650, "events": 350}
            for key, count in expected.items():
                if len(full[key]) != count:
                    raise AssertionError(f"Lost {key} records during migration or updates.")
            if full["decisions"][0]["evidence"] != payload["decisions"][0]["evidence"]:
                raise AssertionError("Old full evidence was changed.")
            return {
                "history_rows": history_count, "decisions": decision_count,
                "terminal_orders": 650, "events": 350, "model_charges": 150,
                "samples": samples, "legacy_blob_bytes": len(encoded.encode("utf-8")),
                "active_state_bytes": active_bytes,
                "migration_seconds": round(migration_seconds, 3),
                "mark_median_ms": round(statistics.median(tick_times) * 1000, 3),
                "mark_max_ms": round(max(tick_times) * 1000, 3),
                "snapshot_median_ms": round(statistics.median(snapshot_times) * 1000, 3),
                "snapshot_max_ms": round(max(snapshot_times) * 1000, 3),
                "audit_export_counts_preserved": True, "old_full_evidence_preserved": True,
            }
        finally:
            engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--large", action="store_true", help="Also test 120,000 history rows and 8,000 decisions.")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, help="Optionally save the JSON report at this path.")
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000:
        parser.error("--samples must be between 1 and 1000")
    cases = [(1600, 320)] + ([(120000, 8000)] if args.large else [])
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Synthetic audit-volume benchmark; not multi-week operation, market execution, or profitability evidence.",
        "environment": {
            "python": sys.version, "platform": platform.platform(),
            "machine": platform.machine(), "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count(), "sqlite": sqlite3.sqlite_version,
            "temporary_storage": "Current environment temporary directory; filesystem and host workload affect timings.",
        },
        "method": "Migrate a legacy JSON database, perform sequential price marks and bounded snapshots, then verify full export counts and old evidence.",
        "cases": [run_case(histories, decisions, args.samples) for histories, decisions in cases],
    }
    report = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
