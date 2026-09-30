"""Competition isolation and remote monitor regressions; no external networking."""
from __future__ import annotations
from contextlib import closing

from copy import deepcopy
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from arena.adapters import ApiError
from arena.service import Service
import test_service_autonomous as fixtures
import test_server as server_fixtures


class CompetitionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.services = []
        self.time = fixtures.START
        self.env = {"OPENAI_API_KEY": "test-private-openai", "ANTHROPIC_API_KEY": "test-private-claude"}
        for agent in ("OPENAI", "CLAUDE"):
            self.env[f"ALPACA_{agent}_KEY"] = f"test-private-{agent}-key"
            self.env[f"ALPACA_{agent}_SECRET"] = f"test-private-{agent}-secret"
        self.clock_patch = patch("arena.service.now", side_effect=lambda: self.time)
        self.clock_patch.start()
        self.network_patch = patch("socket.socket.connect", side_effect=AssertionError("External network prohibited in tests."))
        self.network_patch.start()
        self.brokers = {agent: fixtures.FakePaper(agent, lambda: self.time) for agent in ("openai", "claude")}
        self.svc = self.make_service()

    def tearDown(self):
        self.network_patch.stop()
        self.clock_patch.stop()
        for svc in self.services:
            svc.meta.close()
            svc.engine.close()
        self.tmp.cleanup()

    def make_service(self):
        svc = Service(self.tmp.name, environ=self.env)
        svc.broker = Mock(side_effect=lambda agent: self.brokers[agent["id"]])
        self.services.append(svc)
        return svc

    def configure(self, **changes):
        agents = [{"id": agent, "name": f"Configured {agent} name", "provider": provider,
                   "model": "test-model", "weight": 1, "input_price": 1, "output_price": 1}
                  for agent, provider in (("openai", "openai"), ("claude", "anthropic"))]
        self.svc.new_experiment({"mode": "paper", "agent_mode": "autonomous", "agents": agents,
                                 "total_capital": 500, "target": 10000, "loss_limit": 50,
                                 "position_cap_pct": 100, "exposure_cap_pct": 100,
                                 "monthly_model_budget": 10, "cycle_minutes": 60,
                                 "research_rounds": 3, "max_cycles_per_day": 4,
                                 "max_orders_per_cycle": 3, **changes})
        self.svc.engine.resume()

    def agent(self, agent_id="openai"):
        return next(a for a in self.svc.engine.state()["agents"] if a["id"] == agent_id)

    def researched_buy(self, provider, model_id, key, snapshot):
        records = [e for e in snapshot["evidence"] if e["kind"] == "quotes" and e["status"] == "ok"]
        if not records:
            return fixtures.response(research=[{"kind": "quotes", "query": "", "symbols": ["MSFT"], "lookback_days": 30}])
        return fixtures.response(orders=[fixtures.buy(evidence=records[0]["id"])])

    def test_paused_agent_remains_paused_after_restart_while_other_agent_trades(self):
        self.configure()
        self.svc.halt_agent("openai", "Pause only the first competitor.")
        self.svc = self.make_service()
        self.svc.engine.resume()
        with patch("arena.service.autonomous_turn", side_effect=self.researched_buy) as paid:
            self.svc.cycle()
        state = self.svc.state()
        self.assertTrue(state["agent_controls"]["openai"]["paused"])
        self.assertFalse(state["agent_controls"]["claude"]["paused"])
        self.assertEqual(state["experiment"]["status"], "ready")
        self.assertEqual({call.args[0] for call in paid.call_args_list}, {"anthropic"})
        self.brokers["openai"].submit_order.assert_not_called()
        self.assertEqual(self.brokers["claude"].submit_order.call_count, 1)
        self.assertEqual(self.agent()["positions"], {})
        self.assertIn("MSFT", self.agent("claude")["positions"])

    def test_one_broker_failure_does_not_stop_healthy_competitor(self):
        self.configure()
        self.brokers["openai"].account = Mock(side_effect=ApiError("Mock account unavailable"))
        with patch("arena.service.autonomous_turn", side_effect=self.researched_buy) as paid:
            self.svc.cycle()
        self.assertEqual(self.svc.engine.state()["experiment"]["status"], "ready")
        self.assertTrue(self.svc.state()["agent_controls"]["openai"]["paused"])
        self.assertEqual({call.args[0] for call in paid.call_args_list}, {"anthropic"})
        self.brokers["openai"].submit_order.assert_not_called()
        self.assertEqual(self.brokers["claude"].submit_order.call_count, 1)

    def test_provider_failure_does_not_prevent_other_competitors_plan(self):
        self.configure()

        def model(provider, *args):
            if provider == "openai":
                raise ApiError("Mock paid call timed out")
            return self.researched_buy(provider, *args)

        with patch("arena.service.autonomous_turn", side_effect=model) as paid:
            self.svc.cycle()
            self.svc.cycle()
        self.assertEqual(sum(call.args[0] == "openai" for call in paid.call_args_list), 1)
        self.assertEqual(self.svc.engine.state()["experiment"]["status"], "ready")
        self.brokers["openai"].submit_order.assert_not_called()
        self.assertEqual(self.brokers["claude"].submit_order.call_count, 1)

    def test_same_broker_account_is_not_bound_to_two_competitors(self):
        self.configure()
        self.brokers["claude"].account = Mock(return_value=self.brokers["openai"].account())
        try:
            self.svc.reconcile()
        except ValueError:
            pass
        state = self.svc.state()
        bound = [agent["account_id"] for agent in state["agents"] if agent.get("account_id")]
        self.assertEqual(len(bound), 1)
        self.assertEqual(len(bound), len(set(bound)))
        self.assertEqual(state["experiment"]["status"], "halted")
        self.brokers["openai"].submit_order.assert_not_called()
        self.brokers["claude"].submit_order.assert_not_called()

    def test_agent_resume_requires_fresh_reconciliation_of_only_its_account(self):
        self.configure()
        self.svc.halt_agent("openai", "Operator paused this competitor.")
        original_account = self.brokers["openai"].account
        self.brokers["openai"].account = Mock(side_effect=ApiError("Mock account unavailable"))
        self.brokers["claude"].account = Mock(wraps=self.brokers["claude"].account)
        with self.assertRaises(ValueError):
            self.svc.resume_agent("openai")
        self.assertTrue(self.svc.state()["agent_controls"]["openai"]["paused"])
        self.brokers["openai"].account = original_account
        self.svc.resume_agent("openai")
        controls = self.svc.state()["agent_controls"]["openai"]
        self.assertFalse(controls["paused"])
        self.assertTrue(controls["last_reconciled_at"])
        self.brokers["claude"].account.assert_not_called()

    def test_monitor_marks_portfolio_without_model_calls_or_exit_orders(self):
        self.configure(monthly_model_budget=0)
        self.svc.reconcile()
        plan = fixtures.response(orders=[fixtures.buy()], exits=[fixtures.exit_rule(stop=95)])
        self.svc._execute_plan(self.agent(), {"plan": plan}, {"cycle_id": "prior-plan"}, self.brokers["openai"])
        self.brokers["openai"].submit_order.reset_mock()
        self.brokers["openai"].prices["MSFT"] = 94
        with patch("arena.service.autonomous_turn") as paid:
            self.svc.monitor()
            self.svc.monitor()
            paid.assert_not_called()
        self.brokers["openai"].submit_order.assert_not_called()
        self.brokers["claude"].submit_order.assert_not_called()
        self.assertAlmostEqual(self.agent()["positions"]["MSFT"]["qty"], .3725)
        self.assertEqual(self.svc.engine.state()["market"]["prices"]["MSFT"], 94)
        self.assertEqual(self.svc.meta.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)

    def test_cached_state_and_monitor_do_not_wait_for_slow_cycle(self):
        self.configure()
        self.svc.state()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        failures = []

        def slow_cycle():
            try:
                with self.svc.lock:
                    entered.set()
                    release.wait(5)
            except BaseException as exc:
                failures.append(exc)

        def read_and_monitor():
            try:
                self.svc.public_state()
                self.svc.monitor()
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=slow_cycle)
        reader = threading.Thread(target=read_and_monitor)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            reader.start()
            self.assertTrue(finished.wait(.5), "Cached reads or monitor waited on the cycle lock.")
        finally:
            release.set()
            worker.join(2)
            if reader.ident is not None:
                reader.join(2)
        self.assertEqual(failures, [])
        copied = self.svc.public_state()
        copied["experiment"]["id"] = "caller-mutated-cache"
        self.assertNotEqual(self.svc.public_state()["experiment"]["id"], "caller-mutated-cache")

    def test_global_halt_during_paid_call_prevents_followup_calls_and_orders(self):
        self.configure()
        entered, release = threading.Event(), threading.Event()
        errors = []

        def blocked_model(*_args):
            entered.set()
            release.wait(5)
            return fixtures.response(research=[{"kind": "quotes", "query": "", "symbols": ["MSFT"], "lookback_days": 30}])

        def run():
            try:
                self.svc.cycle()
            except BaseException as exc:
                errors.append(exc)

        with patch("arena.service.autonomous_turn", side_effect=blocked_model) as paid:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                start = time.monotonic()
                self.svc.public_state()
                self.svc.halt("Operator stop during a pending response.")
                self.assertLess(time.monotonic() - start, 1, "State and halt waited for the model response.")
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(paid.call_count, 1)
        self.assertEqual(errors, [])
        self.assertEqual(self.svc.engine.state()["experiment"]["status"], "halted")
        for broker in self.brokers.values():
            broker.submit_order.assert_not_called()

    def test_quotes_refresh_during_paid_call_without_reconciling_or_trading(self):
        self.configure()
        self.svc.reconcile()
        plan = fixtures.response(orders=[fixtures.buy()], exits=[fixtures.exit_rule(stop=95)])
        self.svc._execute_plan(self.agent(), {"plan": plan}, {"cycle_id": "prior-plan"}, self.brokers["openai"])
        broker = self.brokers["openai"]
        broker.submit_order.reset_mock()
        broker.account = Mock(wraps=broker.account)
        entered, release = threading.Event(), threading.Event()
        errors = []

        def blocked_model(*_args):
            entered.set()
            release.wait(5)
            return fixtures.response()

        def run():
            try:
                self.svc.cycle()
            except BaseException as exc:
                errors.append(exc)

        with patch("arena.service.autonomous_turn", side_effect=blocked_model) as paid:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                before = self.svc.public_state()["agent_controls"]["openai"]["last_reconciled_at"]
                broker.account.reset_mock()
                self.time += dt.timedelta(seconds=30)
                broker.prices["MSFT"] = 94
                result = self.svc.monitor()
                self.assertTrue(result["quote_only"])
                self.assertIn("openai", result["refreshed"])
                state = self.svc.public_state()
                self.assertEqual(state["market"]["prices"]["MSFT"], 94)
                self.assertTrue(state["monitor"]["reconciliation_deferred"])
                self.assertEqual(state["agent_controls"]["openai"]["last_reconciled_at"], before)
                self.assertEqual(state["agent_controls"]["openai"]["quote_times"]["MSFT"], self.time.isoformat())
                self.assertEqual(paid.call_count, 1)
                broker.account.assert_not_called()
                broker.submit_order.assert_not_called()
                self.svc.halt("End the controlled slow model call.")
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_agent_halt_during_paid_call_preserves_other_agents_progress(self):
        self.configure()
        entered, release = threading.Event(), threading.Event()
        errors = []

        def blocked_model(provider, *args):
            if provider == "openai":
                entered.set()
                release.wait(5)
                return fixtures.response(research=[{"kind": "quotes", "query": "", "symbols": ["MSFT"], "lookback_days": 30}])
            return self.researched_buy(provider, *args)

        def run():
            try:
                self.svc.cycle()
            except BaseException as exc:
                errors.append(exc)

        with patch("arena.service.autonomous_turn", side_effect=blocked_model) as paid:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                before = time.monotonic()
                self.svc.halt_agent("openai", "Operator pauses only the pending competitor.")
                self.assertLess(time.monotonic() - before, 1)
            finally:
                release.set()
                worker.join(3)
            self.assertEqual(sum(call.args[0] == "openai" for call in paid.call_args_list), 1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.svc.state()["experiment"]["status"], "ready")
        self.brokers["openai"].submit_order.assert_not_called()
        self.assertEqual(self.brokers["claude"].submit_order.call_count, 1)

    def test_unknown_submission_is_not_retried_on_another_cycle(self):
        self.configure()
        broker = self.brokers["openai"]
        broker.submit_order.side_effect = ApiError("Mock lost acknowledgment")
        broker.order_by_client_id = Mock(side_effect=ApiError("Mock unresolved broker order"))
        with patch("arena.service.autonomous_turn", side_effect=self.researched_buy):
            self.svc.cycle()
            self.time += dt.timedelta(hours=1)
            self.svc.cycle()
        self.assertEqual(broker.submit_order.call_count, 1)
        pending = [order for order in self.svc.engine.pending_orders() if order["agent_id"] == "openai"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "unknown")
        self.assertGreater(self.agent()["reserved_cash"], 0)

    def test_legacy_experiment_migration_preserves_ids_allocations_and_strategy_mode(self):
        self.configure()
        original = self.svc.engine.state()
        payload = deepcopy(original)
        for key in ("agent_mode", "compound_profits", "research_rounds", "cycle_minutes",
                    "max_cycles_per_day", "max_orders_per_cycle"):
            payload["experiment"].pop(key, None)
        payload.pop("agent_controls", None)
        payload.pop("asset_registry", None)
        self.svc.meta.close()
        self.svc.engine.close()
        self.services.remove(self.svc)
        with closing(sqlite3.connect(Path(self.tmp.name) / "arena.sqlite3")) as db, db:
            db.execute("UPDATE arena_state SET payload=? WHERE id=1", (json.dumps(payload),))
        self.svc = self.make_service()
        restored = self.svc.state()
        self.assertEqual(restored["experiment"]["id"], original["experiment"]["id"])
        self.assertEqual(restored["experiment"]["agent_mode"], "reviewer")
        self.assertFalse(restored["experiment"]["compound_profits"])
        self.assertFalse(restored["experiment"]["autopilot"])
        self.assertEqual([(a["id"], a["name"], a["allocation"]) for a in restored["agents"]],
                         [(a["id"], a["name"], a["allocation"]) for a in original["agents"]])


class RemoteMonitorIntegrationTests(unittest.TestCase):
    """Exercise actual HTTP handlers behind a simulated HTTPS reverse proxy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir()
        self.port = server_fixtures.free_port()
        self.origin = "https://arena.example.invalid"
        self.host = "arena.example.invalid"
        self.access_token = "test-remote-access-token-6c205b30223140a39c33"
        self.release = self.data_dir / "test-cycle-release"
        self.started = self.data_dir / "test-cycle-starts"
        env = os.environ.copy()
        env.update(ARENA_ACCESS_TOKEN=self.access_token, PYTHONDONTWRITEBYTECODE="1")
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env[name] = ""
        for agent in ("RULES", "OPENAI", "CLAUDE"):
            for suffix in ("KEY", "SECRET"):
                env[f"ALPACA_{agent}_{suffix}"] = ""

        # Retain real HTTP/auth/queue logic. Only the expensive cycle dependency
        # is replaced, allowing us to control a slow call without external I/O.
        injection = f"""
from arena.service import Service
from pathlib import Path
import time
def delayed_test_cycle(self):
    with self.lock:
        with Path({str(self.started)!r}).open('a') as marker:
            marker.write('cycle\\n')
        deadline = time.monotonic() + 8
        while not Path({str(self.release)!r}).exists() and time.monotonic() < deadline:
            time.sleep(.02)
Service.cycle = delayed_test_cycle
"""
        bootstrap = server_fixtures.BOOTSTRAP.replace("script = sys.argv.pop(1)", injection + "\nscript = sys.argv.pop(1)")
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", bootstrap, str(server_fixtures.SERVER),
             "--data-dir", str(self.data_dir), "--port", str(self.port), "--no-browser",
             "--public-origin", self.origin],
            cwd=server_fixtures.ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.addCleanup(self.cleanup)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output, _ = self.process.communicate(timeout=1)
                self.fail(f"Remote server exited on startup: {output}")
            try:
                status, _, _ = self.request("GET", "/api/session")
                if status == 401:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.02)
        self.fail("Remote server did not become ready within 10 seconds.")

    def cleanup(self):
        self.release.touch()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            output, _ = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            output, _ = self.process.communicate(timeout=5)
        self.tmp.cleanup()
        self.assertNotIn("TEST BLOCKED NONLOOPBACK NETWORK", output or "")
        self.assertNotIn(self.access_token, output or "", "Server logged its remote access token.")

    def request(self, method, path, body=None, headers=None):
        outgoing = {"Host": self.host, **(headers or {})}
        if body is not None:
            body = json.dumps(body).encode("utf-8")
            outgoing.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            conn.request(method, path, body=body, headers=outgoing)
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()

    def login(self):
        status, headers, raw = self.request("POST", "/api/login", {"access_token": self.access_token},
                                            {"Origin": self.origin})
        self.assertEqual(status, 200, raw)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return cookie, json.loads(raw)["token"], headers, raw

    def test_remote_state_session_and_export_require_authentication(self):
        for path in ("/api/session", "/api/state", "/api/export"):
            with self.subTest(path=path):
                status, _, raw = self.request("GET", path)
                self.assertEqual(status, 401, raw)
                self.assertNotIn(self.access_token.encode(), raw)
                self.assertNotIn(b'"experiment"', raw)
                self.assertNotIn(b'"token":', raw)
        status, _, raw = self.request("POST", "/api/login", {"access_token": "incorrect-access-token"},
                                      {"Origin": self.origin})
        self.assertIn(status, (401, 403), raw)

    def test_login_cookie_is_protected_and_export_has_no_authentication_secrets(self):
        cookie, csrf, headers, raw = self.login()
        attributes = headers["Set-Cookie"].lower()
        self.assertTrue(cookie.startswith("__Host-arena_session="))
        for expected in ("secure", "httponly", "samesite=strict", "path=/"):
            self.assertIn(expected, attributes)
        self.assertNotIn("domain=", attributes)
        self.assertNotIn(self.access_token.encode(), raw)
        for path in ("/api/state", "/api/export"):
            status, response_headers, raw = self.request("GET", path, headers={"Cookie": cookie})
            self.assertEqual(status, 200, raw)
            self.assertEqual(response_headers["Cache-Control"], "no-store")
            for secret in (self.access_token, csrf, cookie.split("=", 1)[1]):
                self.assertNotIn(secret.encode(), raw)

    def test_remote_login_requires_exact_origin_and_ignores_forwarded_host(self):
        cases = [{}, {"Origin": "null"}, {"Origin": "http://arena.example.invalid"},
                 {"Origin": "https://attacker.invalid"},
                 {"Host": "attacker.invalid", "Origin": self.origin, "X-Forwarded-Host": self.host}]
        for headers in cases:
            with self.subTest(headers=headers):
                status, _, raw = self.request("POST", "/api/login", {"access_token": self.access_token}, headers)
                self.assertEqual(status, 403, raw)

    def test_authenticated_mutation_requires_its_own_csrf_and_same_origin(self):
        cookie, csrf, _, _ = self.login()
        other_cookie, other_csrf, _, _ = self.login()
        self.assertNotEqual(csrf, other_csrf)
        rejected_headers = [
            {"Cookie": cookie, "Origin": self.origin},
            {"Cookie": cookie, "Origin": self.origin, "X-Arena-Token": "incorrect"},
            {"Cookie": other_cookie, "Origin": self.origin, "X-Arena-Token": csrf},
            {"Cookie": cookie, "Origin": "https://attacker.invalid", "X-Arena-Token": csrf},
        ]
        for headers in rejected_headers:
            status, _, raw = self.request("POST", "/api/halt", {}, headers)
            self.assertEqual(status, 403, raw)
        self.assertFalse((self.data_dir / "STOP").exists())
        status, _, raw = self.request("POST", "/api/halt", {},
                                     {"Cookie": cookie, "Origin": self.origin, "X-Arena-Token": csrf})
        self.assertEqual(status, 200, raw)
        self.assertTrue((self.data_dir / "STOP").exists())

    def test_logout_invalidates_only_that_authenticated_session(self):
        cookie, csrf, _, _ = self.login()
        other_cookie, _, _, _ = self.login()
        status, _, raw = self.request("POST", "/api/logout", {},
                                     {"Cookie": cookie, "Origin": self.origin, "X-Arena-Token": csrf})
        self.assertEqual(status, 200, raw)
        status, _, _ = self.request("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/api/state", headers={"Cookie": other_cookie})
        self.assertEqual(status, 200)

    def test_repeated_cycle_request_uses_one_job_and_state_halt_stay_responsive(self):
        cookie, csrf, _, _ = self.login()
        headers = {"Cookie": cookie, "Origin": self.origin, "X-Arena-Token": csrf}
        status, _, raw = self.request("POST", "/api/new", {"mode": "paper"}, headers)
        self.assertEqual(status, 200, raw)
        status, _, raw = self.request("POST", "/api/cycle", {}, headers)
        self.assertEqual(status, 202, raw)
        first = json.loads(raw)["job"]
        deadline = time.monotonic() + 2
        while not self.started.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.started.exists(), "Background cycle did not start.")
        status, _, raw = self.request("POST", "/api/cycle", {}, headers)
        self.assertEqual(status, 202, raw)
        self.assertEqual(json.loads(raw)["job"]["id"], first["id"])
        before = time.monotonic()
        status, _, raw = self.request("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(status, 200, raw)
        status, _, raw = self.request("POST", "/api/halt", {}, headers)
        self.assertEqual(status, 200, raw)
        self.assertLess(time.monotonic() - before, 1, "State or halt waited on the active cycle.")
        self.assertTrue((self.data_dir / "STOP").exists())
        self.assertEqual(self.started.read_text().splitlines(), ["cycle"])
        self.release.touch()


if __name__ == "__main__":
    unittest.main()
