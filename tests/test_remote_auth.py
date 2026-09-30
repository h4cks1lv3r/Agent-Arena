"""Private-token lifecycle and bounded background jobs; no external APIs."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from arena.remote_auth import (AuthFailure, COOKIE_NAME, RemoteAuth, SESSION_SECONDS,
                               initialize_access, load_access_secret, validate_public_origin)
from server import CycleRunner


class RemoteAuthTests(unittest.TestCase):
    def test_public_origin_rejects_non_origins_and_credentials(self):
        self.assertEqual(validate_public_origin("https://ARENA.example:443"), "https://arena.example")
        self.assertEqual(validate_public_origin("https://arena.example:8443"), "https://arena.example:8443")
        self.assertEqual(validate_public_origin("https://[::1]"), "https://[::1]")
        for origin in ("http://arena.example", "https://arena.example/", "https://arena.example?",
                       "https://arena.example#", "https://arena.example?q=1", "https://arena.example/#x",
                       "https://user:secret@arena.example", "https://arena.example:0", "https://arena.example:65536",
                       "https://arena.example:", "https://arena_example", "https://arena.example\n", "//arena.example"):
            with self.subTest(origin=origin), self.assertRaises(ValueError) as caught:
                validate_public_origin(origin)
            self.assertNotIn("user:secret", str(caught.exception))

    def test_access_file_created_private_once_and_environment_takes_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            token = initialize_access(temporary)
            path = Path(temporary) / "access-token"
            self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
            self.assertEqual(path.read_text().strip(), token)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_access_secret(temporary, {}), token)
            override = "environment-access-secret-" + "z" * 32
            self.assertEqual(load_access_secret(temporary, {"ARENA_ACCESS_TOKEN": override}), override)
            with self.assertRaises(ValueError) as caught:
                initialize_access(temporary)
            self.assertNotIn(token, str(caught.exception))
            self.assertEqual(path.read_text().strip(), token)
            # An explicitly empty environment value must not silently use the file.
            with self.assertRaises(ValueError):
                load_access_secret(temporary, {"ARENA_ACCESS_TOKEN": ""})

    def test_missing_weak_or_publicly_readable_secrets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                load_access_secret(temporary, {})
            for token in ("too-short", "x" * 31, "x" * 4097, "x" * 32 + "\n"):
                with self.subTest(length=len(token)), self.assertRaises(ValueError) as caught:
                    load_access_secret(temporary, {"ARENA_ACCESS_TOKEN": token})
                self.assertNotIn(token, str(caught.exception))
            initialize_access(temporary)
            if os.name != "nt":
                (Path(temporary) / "access-token").chmod(0o644)
                with self.assertRaises(ValueError):
                    load_access_secret(temporary, {})

    def test_cookie_and_csrf_are_separate_expire_and_revoke_independently(self):
        clock = [100.0]
        secret = "private-token-" + "q" * 32
        auth = RemoteAuth(secret, clock=lambda: clock[0])
        first_cookie, first = auth.login(secret, "127.0.0.1")
        second_cookie, second = auth.login(secret, "127.0.0.1")
        header = f"{COOKIE_NAME}={first_cookie}"
        self.assertEqual(len({secret, first_cookie, first.csrf, second_cookie, second.csrf}), 5)
        self.assertEqual(auth.session(header), first)
        self.assertTrue(auth.csrf_valid(first, first.csrf))
        self.assertFalse(auth.csrf_valid(first, second.csrf))
        self.assertFalse(auth.csrf_valid(first, secret))
        self.assertIsNone(auth.session(header + "; " + header))
        self.assertIsNone(RemoteAuth(secret).session(header))  # restart revokes all sessions
        auth.logout(header)
        self.assertIsNone(auth.session(header))
        self.assertEqual(auth.session(f"{COOKIE_NAME}={second_cookie}"), second)
        clock[0] += SESSION_SECONDS
        self.assertIsNone(auth.session(f"{COOKIE_NAME}={second_cookie}"))

    def test_failed_login_is_rate_limited_then_recovers_without_secret_disclosure(self):
        clock = [100.0]
        secret = "private-token-" + "q" * 32
        auth = RemoteAuth(secret, clock=lambda: clock[0])
        for _ in range(5):
            with self.assertRaises(AuthFailure) as caught:
                auth.login("wrong-token", "127.0.0.1")
            self.assertEqual(caught.exception.status, 401)
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn("wrong-token", str(caught.exception))
        with self.assertRaises(AuthFailure) as caught:
            auth.login(secret, "127.0.0.1")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 60)
        clock[0] += 60
        cookie, session = auth.login(secret, "127.0.0.1")
        self.assertEqual(auth.session(f"{COOKIE_NAME}={cookie}"), session)

    def test_session_and_peer_storage_are_bounded(self):
        secret = "private-token-" + "q" * 32
        auth = RemoteAuth(secret, max_sessions=2, max_peers=2)
        old_cookie, _ = auth.login(secret, "127.0.0.1")
        auth.login(secret, "127.0.0.1")
        latest_cookie, latest = auth.login(secret, "127.0.0.1")
        self.assertIsNone(auth.session(f"{COOKIE_NAME}={old_cookie}"))
        self.assertEqual(auth.session(f"{COOKIE_NAME}={latest_cookie}"), latest)
        for index in range(10):
            with self.assertRaises(AuthFailure):
                auth.login("wrong-token", f"peer-{index}")
        self.assertLessEqual(len(auth._sessions), 2)
        self.assertLessEqual(len(auth._peers), 2)


class CycleRunnerTests(unittest.TestCase):
    def runner(self, cycle, env=None):
        service = SimpleNamespace(cycle=cycle, env=env or {}, engine=Mock(), halt=Mock(),
                                  lock=threading.RLock(), public_state=lambda: {"experiment": {"id": "test-experiment"}})
        runner = CycleRunner(service)
        self.addCleanup(runner.close)
        return runner, service

    def wait_finished(self, runner):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = runner.snapshot()
            if job["status"] in ("complete", "error"):
                return job
            threading.Event().wait(.005)
        self.fail("Background cycle did not finish promptly.")

    def test_duplicate_requests_share_one_running_cycle_and_snapshot_stays_responsive(self):
        started, release = threading.Event(), threading.Event()
        lock = threading.Lock()

        def cycle():
            with lock:
                started.set()
                if not release.wait(2):
                    raise AssertionError("Test did not release held cycle.")

        cycle_mock = Mock(side_effect=cycle)
        runner, service = self.runner(cycle_mock)
        self.addCleanup(release.set)
        queued = runner.enqueue()
        self.assertTrue(started.wait(1))
        before = time.monotonic()
        duplicate = runner.enqueue("autopilot")
        status = runner.snapshot()
        self.assertLess(time.monotonic() - before, .5)
        self.assertEqual(queued["id"], duplicate["id"])
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["source"], "manual")
        self.assertIn("queued_at", status)
        self.assertIn("started_at", status)
        release.set()
        self.assertEqual(self.wait_finished(runner)["status"], "complete")
        cycle_mock.assert_called_once()
        service.engine.halt.assert_not_called()

    def test_expected_error_records_failure_redacts_credentials_and_preserves_recovery(self):
        secret = "test-private-provider-secret-0123456789"
        cycle = Mock(side_effect=ValueError("Provider failure: " + secret))
        runner, service = self.runner(cycle, {"OPENAI_API_KEY": secret})
        runner.enqueue()
        job = self.wait_finished(runner)
        self.assertEqual(job["status"], "error")
        self.assertIn("finished_at", job)
        self.assertNotIn(secret, repr(job))
        self.assertIn("[redacted]", job["error"])
        service.halt.assert_not_called()
        service.engine.halt.assert_not_called()
        service.engine.set_autopilot.assert_not_called()
        cycle.assert_called_once()

    def test_unexpected_defect_halts_automation(self):
        runner, service = self.runner(Mock(side_effect=RuntimeError("unexpected secret detail")))
        runner.enqueue()
        job = self.wait_finished(runner)
        self.assertEqual(job["status"], "error")
        self.assertNotIn("secret detail", job["error"])
        service.halt.assert_called_once()

    def test_closed_runner_rejects_new_work(self):
        runner, _ = self.runner(Mock())
        runner.close()
        with self.assertRaises(ValueError):
            runner.enqueue()


if __name__ == "__main__":
    unittest.main()
