"""Real local HTTP/CLI tests. Temporary databases and fake credentials only."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"

# Execute the actual CLI in a child process, with an audit guard that forbids
# external DNS and connections even if a regression unexpectedly invokes an API.
BOOTSTRAP = """
import runpy, sys
def loopback_only(event, args):
    if event not in ('socket.connect', 'socket.getaddrinfo'):
        return
    address = args[1] if event == 'socket.connect' else args[0]
    host = address[0] if isinstance(address, tuple) else address
    if host not in ('127.0.0.1', '::1', 'localhost'):
        print('TEST BLOCKED NONLOOPBACK NETWORK', file=sys.stderr, flush=True)
        raise RuntimeError('Integration tests prohibit external network access.')
sys.addaudithook(loopback_only)
script = sys.argv.pop(1)
sys.argv[0] = script
runpy.run_path(script, run_name='__main__')
"""


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.children = []
        self.process = None
        self.env = os.environ.copy()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Override credentials inherited from the developer's shell or .env.
        names = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
        names += [f"ALPACA_{agent}_{part}" for agent in ("RULES", "OPENAI", "CLAUDE")
                  for part in ("KEY", "SECRET")]
        self.fake_credentials = {name: f"test-only-never-send-{name}-6b709d" for name in names}
        self.env.update(self.fake_credentials)
        self.addCleanup(self.cleanup)
        self.start_server()

    def cleanup(self):
        output = ""
        for process in self.children:
            if process.poll() is None:
                process.terminate()
            try:
                text, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                text, _ = process.communicate(timeout=5)
            output += text or ""
        self.tmp.cleanup()
        self.assertNotIn("TEST BLOCKED NONLOOPBACK NETWORK", output,
                         "A server integration test attempted external network access.")

    def spawn(self, *arguments):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", BOOTSTRAP, str(SERVER),
             "--data-dir", str(self.data_dir), "--no-browser", *arguments],
            cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        self.children.append(process)
        return process

    def start_server(self):
        self.port = free_port()
        self.process = self.spawn("--port", str(self.port))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output, _ = self.process.communicate(timeout=1)
                self.fail(f"Server exited during startup: {output}")
            try:
                status, _, body = self.request("GET", "/api/session")
                if status == 200:
                    self.token = json.loads(body)["token"]
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.02)
        self.fail("Local HTTP server did not start within 10 seconds.")

    def stop_server(self):
        self.process.terminate()
        self.process.wait(timeout=5)

    def request(self, method, path, body=None, headers=None):
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            conn.request(method, path, body=encoded, headers=request_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def post(self, path, body=None, *, expected=200):
        status, _, raw = self.request("POST", path, {} if body is None else body,
                                     {"X-Arena-Token": self.token,
                                      "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, expected, raw.decode("utf-8"))
        return json.loads(raw)

    def state(self):
        status, _, raw = self.request("GET", "/api/state")
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def test_session_resume_and_synthetic_demo_end_to_end(self):
        status, headers, raw = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(json.loads(raw)["token"]), 32)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        initial = self.state()
        self.assertEqual(initial["experiment"]["mode"], "demo")
        self.assertEqual(initial["experiment"]["status"], "paused")
        self.post("/api/config", {"agents": [
            {"id": "rules", "name": "Demo rules", "provider": "rules", "weight": 1}
        ]})
        self.post("/api/demo", {"count": 1}, expected=400)
        ready = self.post("/api/resume")
        self.assertEqual(ready["experiment"]["status"], "ready")
        result = self.post("/api/demo", {"count": 35})
        self.assertEqual(result["experiment"]["step"], 35)
        self.assertEqual(result["market"]["source"], "synthetic_demo")
        self.assertTrue(result["orders"], "Demo must exercise the order and fill path.")
        self.assertTrue(all(o["agent_id"] == "rules" for o in result["orders"]))
        self.assertTrue(all(o["status"] == "filled" for o in result["orders"]))
        self.assertTrue(all(a["model_cost"] == 0 for a in result["agents"]))
        self.assertEqual(result["model_spend_month"], 0)
        self.assertEqual(self.state()["orders"], result["orders"])

    def test_bad_host_or_origin_cannot_read_session_or_mutate_state(self):
        for bad_headers in (
            {"Host": "attacker.invalid"},
            {"Host": f"127.0.0.1.attacker.invalid:{self.port}"},
            {"Origin": "https://attacker.invalid"},
            {"Origin": "null"},
            {"Origin": f"https://127.0.0.1:{self.port}"},
        ):
            with self.subTest(headers=bad_headers):
                for path in ("/api/session", "/api/state", "/api/export"):
                    status, _, raw = self.request("GET", path, headers=bad_headers)
                    self.assertEqual(status, 403, raw)
                    self.assertNotIn(self.token.encode(), raw)
                status, _, raw = self.request("POST", "/api/resume", {},
                                             {**bad_headers, "X-Arena-Token": self.token})
                self.assertEqual(status, 403, raw)
        self.assertEqual(self.state()["experiment"]["status"], "paused")

    def test_missing_or_wrong_csrf_token_cannot_resume_or_halt(self):
        for token in (None, "", "not-the-session-token"):
            with self.subTest(token=token):
                headers = {"Origin": f"http://127.0.0.1:{self.port}"}
                if token is not None:
                    headers["X-Arena-Token"] = token
                for path in ("/api/resume", "/api/halt"):
                    status, _, raw = self.request("POST", path, {}, headers)
                    self.assertEqual(status, 403, raw)
        self.assertEqual(self.state()["experiment"]["status"], "paused")
        self.assertFalse((self.data_dir / "STOP").exists())

    def test_export_contains_results_but_no_credentials_or_session_token(self):
        self.post("/api/resume")
        self.post("/api/demo", {"count": 2})
        status, headers, raw = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        exported = json.loads(raw)
        self.assertEqual(exported["experiment"]["step"], 2)
        self.assertTrue(exported["decisions"])
        # Configured status may be exported, but credential values may not.
        self.assertTrue(all(c["alpaca"] for c in exported["connections"].values()))
        for secret in (*self.fake_credentials.values(), self.token):
            self.assertNotIn(secret.encode(), raw)

    def test_dashboard_halt_survives_restart_until_explicit_resume(self):
        self.post("/api/resume")
        before = self.post("/api/demo", {"count": 3})
        halted = self.post("/api/halt")
        self.assertEqual(halted["experiment"]["status"], "halted")
        self.assertTrue((self.data_dir / "STOP").exists())
        old_token = self.token
        self.stop_server()
        self.start_server()
        restored = self.state()
        self.assertNotEqual(self.token, old_token)
        self.assertEqual(restored["experiment"]["id"], before["experiment"]["id"])
        self.assertEqual(restored["experiment"]["step"], 3)
        self.assertEqual(restored["orders"], before["orders"])
        self.assertEqual(restored["experiment"]["status"], "halted")
        self.assertFalse(restored["experiment"]["autopilot"])
        status, _, _ = self.request("POST", "/api/resume", {}, {"X-Arena-Token": old_token})
        self.assertEqual(status, 403)
        self.post("/api/demo", {"count": 1}, expected=400)
        self.assertEqual(self.state()["experiment"]["step"], 3)
        resumed = self.post("/api/resume")
        self.assertEqual(resumed["experiment"]["status"], "ready")
        self.assertFalse((self.data_dir / "STOP").exists())
        self.assertEqual(self.post("/api/demo", {"count": 1})["experiment"]["step"], 4)

    def test_external_halt_works_with_running_and_stopped_server(self):
        self.post("/api/resume")
        for running in (True, False):
            with self.subTest(server_running=running):
                if not running:
                    self.post("/api/resume")
                    self.stop_server()
                command = self.spawn("--halt")
                output, _ = command.communicate(timeout=5)
                self.assertEqual(command.returncode, 0, output)
                self.assertTrue((self.data_dir / "STOP").exists())
                if not running:
                    self.start_server()
                result = self.state()
                self.assertEqual(result["experiment"]["status"], "halted")
                self.assertFalse(result["experiment"]["autopilot"])
                self.post("/api/demo", {"count": 1}, expected=400)
                self.assertEqual(self.state()["experiment"]["step"], 0)

    def test_second_process_cannot_share_data_directory(self):
        self.post("/api/resume")
        before = self.post("/api/demo", {"count": 1})
        # A distinct port proves refusal is the data lock, not address contention.
        other = self.spawn("--port", str(free_port()))
        output, _ = other.communicate(timeout=5)
        self.assertEqual(other.returncode, 1, output)
        self.assertIn("already using this data directory", output)
        after = self.state()
        self.assertEqual(after["experiment"], before["experiment"])
        self.assertEqual(after["orders"], before["orders"])
        self.assertEqual(self.post("/api/demo", {"count": 1})["experiment"]["step"], 2)


if __name__ == "__main__":
    unittest.main()
