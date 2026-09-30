#!/usr/bin/env python3
"""Independent mocked audit scenarios, runnable against either release tree.

No broker, model, or socket calls. This records outcomes rather than presuming
that the review's conclusions apply to a particular release.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock, patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sys.path[:0] = [str(args.source.resolve()), str((args.source / "tests").resolve())]
    from arena import adapters
    from server import CycleRunner
    import test_audit_acceptance as acceptance
    import test_service_autonomous as fixtures
    from test_control_jobs import SlowService
    from test_v05_service import ServiceBlendTests

    outcomes = {"source": str(args.source.resolve()), "all_transports": "mocked"}

    def run_fixture(fn):
        fixture = ServiceBlendTests("runTest")
        fixture.setUp()
        try:
            return fn(fixture)
        finally:
            fixture.tearDown()

    def refused_403(f):
        broker = f.brokers["openai"]
        broker.submit_order.side_effect = adapters.OrderRejectedError("Broker refused insufficient buying power.", status_code=403)
        f.svc._submit(f.agent(), "MSFT", "buy", 100, notional=25)
        for _ in range(5):
            f.advance()
            f.svc.monitor()
        state = f.svc.engine.snapshot()
        control = state["agent_controls"]["openai"]
        return {"submissions": broker.submit_order.call_count,
                "status": state["orders"][0]["status"],
                "reserved_cash": f.agent()["reserved_cash"],
                "paused_after_five_healthy_monitors": control["paused"],
                "pause_kind": control.get("pause_kind")}

    outcomes["order_403"] = run_fixture(refused_403)

    def exit_near_close(f):
        broker = f.seed_exit()
        f.time = f.time.replace(hour=19, minute=59, second=0)
        with patch("arena.service.autonomous_turn", return_value=fixtures.response()):
            f.svc.cycle()
        first = f.svc.engine.snapshot()["agent_controls"]["openai"]
        for _ in range(5):
            f.advance(1)
            f.svc.monitor()
        state = f.svc.engine.snapshot()
        f.time = f.time + dt.timedelta(days=1)
        f.time = f.time.replace(hour=14, minute=0, second=0)
        with patch("arena.service.autonomous_turn", return_value=fixtures.response()):
            f.svc.cycle()
        return {"paused_at_close": first["paused"], "pause_kind": first.get("pause_kind"),
                "pause_reason": first.get("reason"),
                "paused_after_five_healthy_monitors": state["agent_controls"]["openai"]["paused"],
                "position_held_before_next_session": "MSFT" in state["agents"][0]["positions"],
                "position_held_after_next_session": "MSFT" in f.agent()["positions"],
                "submissions_after_next_session": broker.submit_order.call_count}

    outcomes["near_close_exit"] = run_fixture(exit_near_close)

    def market_closes_between_reads(f):
        broker = f.seed_exit()
        original = broker.clock
        calls = [0]

        def flip():
            calls[0] += 1
            value = original()
            if calls[0] >= 2:
                value["is_open"] = False
            return value

        broker.clock = flip
        with patch("arena.service.autonomous_turn", return_value=fixtures.response()):
            f.svc.cycle()
        state = f.svc.engine.snapshot()
        return {"clock_reads": calls[0], "paused": state["agent_controls"]["openai"]["paused"],
                "pause_kind": state["agent_controls"]["openai"].get("pause_kind"),
                "submissions": broker.submit_order.call_count,
                "position_held": "MSFT" in f.agent()["positions"]}

    outcomes["market_close_clock_race"] = run_fixture(market_closes_between_reads)

    service = SlowService()
    service.release.set()
    runner = CycleRunner(service)
    acquired, release = threading.Event(), threading.Event()

    def monitor():
        with service.lock:
            acquired.set()
            release.wait(5)

    thread = threading.Thread(target=monitor)
    thread.start()
    try:
        if not acquired.wait(1):
            raise AssertionError("Mock monitor never acquired its lock.")
        runner.enqueue("scheduled")
        # Give the worker time to encounter a lock held by account monitoring.
        time.sleep(.15)
        during = runner.snapshot()
        release.set()
        thread.join(1)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and runner.snapshot()["status"] in runner.ACTIVE:
            time.sleep(.01)
        outcomes["monitor_lock_collision"] = {"while_monitor_busy": during["status"],
                "final_status": runner.snapshot()["status"], "error": runner.snapshot().get("error"),
                "executed_routes": service.calls}
    finally:
        release.set()
        thread.join(1)
        runner.close()
        runner._thread.join(1)

    def separate_restart(f):
        f.svc.engine.set_autopilot(True)
        f.advance()
        f.brokers["claude"].account = Mock(side_effect=adapters.AuthenticationApiError("Mock permanently invalid credentials.", status_code=401))
        f.svc = f.make_service()
        f.svc.monitor()
        with patch("arena.service.autonomous_turn", return_value=fixtures.response()) as model:
            f.svc.cycle()
        state = f.svc.engine.snapshot()
        return {"experiment_status": state["experiment"]["status"],
                "openai_paused": state["agent_controls"]["openai"]["paused"],
                "claude_paused": state["agent_controls"]["claude"]["paused"],
                "claude_pause_kind": state["agent_controls"]["claude"].get("pause_kind"),
                "model_providers": [call.args[0] for call in model.call_args_list]}

    outcomes["restart_permanent_peer_failure"] = run_fixture(separate_restart)
    encoded = json.dumps(outcomes, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
