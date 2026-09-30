"""Responsive HTTP control admission while network work is slow; no broker calls."""
from copy import deepcopy
import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from arena.adapters import ApiError
from server import ControlBusy, CycleRunner, make_handler


class SlowService:
    def __init__(self):
        self.lock = threading.RLock()
        self.env = {}
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        self.experiment = {'id': 'experiment-1', 'mode': 'paper', 'status': 'ready', 'autopilot': True}

    def public_state(self, **_kwargs):
        return {'experiment': deepcopy(self.experiment), 'agents': []}

    def cycle(self):
        with self.lock:
            self.calls.append('/api/cycle')
            self.entered.set()
            self.release.wait(3)
        return self.public_state()

    def halt(self, _reason=None):
        self.experiment.update(status='halted', autopilot=False)

    def dispatch(self, route, body):
        if route == '/api/halt':
            self.halt()
            return self.public_state()
        if route == '/api/autopilot' and body.get('enabled') is False:
            self.experiment['autopilot'] = False
            return self.public_state()
        with self.lock:
            self.calls.append(route)
            if route in ('/api/reconcile', '/api/close'):
                self.entered.set()
                self.release.wait(3)
            return self.public_state()


class ControlJobTests(unittest.TestCase):
    def setUp(self):
        self.service = SlowService()
        self.runner = CycleRunner(self.service)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.service, self.runner))
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.port = self.http.server_port
        self.token = self.request('GET', '/api/session')[1]['token']

    def tearDown(self):
        self.service.release.set()
        self.runner.close()
        self.runner._thread.join(1)
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(1)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=.75)
        try:
            headers = {'Origin': f'http://127.0.0.1:{self.port}'}
            if body is not None:
                headers.update({'Content-Type': 'application/json', 'X-Arena-Token': self.token})
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def finish(self):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            job = self.runner.snapshot()
            if job['status'] not in self.runner.ACTIVE:
                return job
            self.service.release.wait(.005) if not self.service.release.is_set() else time.sleep(.005)
        self.fail('Background job failed to complete.')

    def test_slow_reconcile_returns_202_and_duplicate_returns_same_job(self):
        started = time.monotonic()
        status, response = self.request('POST', '/api/reconcile', {})
        self.assertEqual(status, 202)
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(self.service.entered.wait(.5))
        status, duplicate = self.request('POST', '/api/reconcile', {})
        self.assertEqual(status, 202)
        self.assertEqual(duplicate['job']['id'], response['job']['id'])
        status, state = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(state['server_job']['status'], 'running')
        self.service.release.set()
        self.assertEqual(self.finish()['status'], 'complete')
        self.assertEqual(self.service.calls, ['/api/reconcile'])

    def test_conflicting_commands_never_execute_after_slow_cycle(self):
        self.request('POST', '/api/cycle', {})
        self.assertTrue(self.service.entered.wait(.5))
        for path, body in (('/api/config', {}), ('/api/new', {}), ('/api/close', {'agent_id': 'openai', 'symbol': 'MSFT'}),
                           ('/api/cancel', {'order_id': 'order-1'}), ('/api/resume', {})):
            with self.subTest(path=path):
                status, response = self.request('POST', path, body)
                self.assertEqual(status, 409)
                self.assertTrue(response['not_queued'])
        self.service.release.set()
        self.finish()
        self.assertEqual(self.service.calls, ['/api/cycle'])

    def test_halt_and_disable_autopilot_work_during_slow_cycle(self):
        self.request('POST', '/api/cycle', {})
        self.assertTrue(self.service.entered.wait(.5))
        for path, body in (('/api/autopilot', {'enabled': False}), ('/api/halt', {})):
            status, response = self.request('POST', path, body)
            self.assertEqual(status, 200)
            self.assertFalse(response['experiment']['autopilot'])
        self.assertEqual(response['experiment']['status'], 'halted')
        self.assertFalse(self.service.release.is_set())

    def test_queued_close_is_invalidated_by_newer_stop(self):
        worker_entered, release_worker = threading.Event(), threading.Event()
        original = self.runner._perform

        def delay(action, **kwargs):
            worker_entered.set()
            release_worker.wait(2)
            return original(action, **kwargs)

        with patch.object(self.runner, '_perform', side_effect=delay):
            try:
                status, _ = self.request('POST', '/api/close', {'agent_id': 'openai', 'symbol': 'MSFT'})
                self.assertEqual(status, 202)
                self.assertTrue(worker_entered.wait(.5))
                self.assertEqual(self.request('POST', '/api/halt', {})[0], 200)
            finally:
                release_worker.set()
            job = self.finish()
        self.assertEqual(job['status'], 'error')
        self.assertIn('canceled', job['error'])
        self.assertEqual(self.service.calls, [])

    def test_busy_monitor_does_not_defer_a_local_destructive_command(self):
        acquired, release = threading.Event(), threading.Event()

        def monitor():
            with self.service.lock:
                acquired.set()
                release.wait(2)

        thread = threading.Thread(target=monitor)
        thread.start()
        try:
            self.assertTrue(acquired.wait(.5))
            status, response = self.request('POST', '/api/new', {})
            self.assertEqual(status, 409)
            self.assertTrue(response['not_queued'])
        finally:
            release.set()
            thread.join(1)
        self.assertEqual(self.service.calls, [])

    def test_expected_network_error_does_not_disable_automation(self):
        with patch.object(self.service, 'cycle', side_effect=ApiError('Temporary read failure')):
            self.runner.enqueue()
            job = self.finish()
        self.assertEqual(job['status'], 'error')
        self.assertTrue(self.service.experiment['autopilot'])
        self.assertEqual(self.service.experiment['status'], 'ready')

    def test_completed_job_results_survive_later_operations_with_bounded_history(self):
        with patch.object(self.service, 'cycle', side_effect=ValueError('Failed account check')):
            failed = self.runner.enqueue()
            self.finish()
        self.runner.run_now('/api/config', {})
        status, response = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(response['server_job']['status'], 'complete')
        recorded = next(job for job in response['server_jobs'] if job['id'] == failed['id'])
        self.assertEqual(recorded['status'], 'error')
        self.assertIn('Failed account check', recorded['error'])
        for _ in range(35):
            self.runner.run_now('/api/config', {})
        self.assertEqual(len(self.runner.recent()), 32)

    def test_job_errors_are_redacted(self):
        self.service.env['OPENAI_API_KEY'] = 'fake-private-provider-key-12345'
        with patch.object(self.service, 'cycle', side_effect=ValueError('Rejected fake-private-provider-key-12345')):
            self.runner.enqueue()
            job = self.finish()
        self.assertEqual(job['status'], 'error')
        self.assertNotIn(self.service.env['OPENAI_API_KEY'], json.dumps(job))
        self.assertIn('[redacted]', job['error'])


if __name__ == '__main__':
    unittest.main()
