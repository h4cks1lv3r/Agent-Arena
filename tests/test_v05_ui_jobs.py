"""Local HTTP checks for bounded diagnostic jobs and independent stop controls."""
from copy import deepcopy
import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from server import CycleRunner, make_handler


class DiagnosticService:
    def __init__(self):
        self.lock = threading.RLock()
        self.entered, self.release = threading.Event(), threading.Event()
        self.env = {'OPENAI_API_KEY': 'fake-private-key-diagnostics'}
        self.calls = []
        self.experiment = {'id': 'experiment-1', 'mode': 'paper', 'status': 'ready',
                           'autopilot': True, 'auto_resume': True}

    def public_state(self, **_kwargs):
        return {'experiment': deepcopy(self.experiment), 'agents': []}

    def halt(self, _reason=None):
        self.experiment.update(status='halted', autopilot=False)

    def dispatch(self, route, body):
        if route == '/api/halt':
            self.halt()
            return self.public_state()
        if route == '/api/auto-resume':
            self.experiment['auto_resume'] = body['enabled']
            return self.public_state()
        if route == '/api/test-connections':
            self.calls.append((route, deepcopy(body)))
            self.entered.set()
            self.release.wait(3)
            return {'paid_model': body.get('paid_model', False), 'at': '2026-09-25T18:00:00Z',
                    'checks': [{'agent_id': 'agent', 'name': 'Agent',
                                'broker': {'status': 'ok', 'secret': 'never expose'},
                                'model': {'status': 'skipped', 'message': self.env['OPENAI_API_KEY']},
                                'estimate': {'reserved_usd': .025, 'max_output_tokens': 2048}}],
                    'credentials': 'never export this extra root key'}
        return self.public_state()


class V05UiJobTests(unittest.TestCase):
    def setUp(self):
        self.service = DiagnosticService()
        self.runner = CycleRunner(self.service)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.service, self.runner))
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.token = self.request('GET', '/api/session')[1]['token']

    def tearDown(self):
        self.service.release.set()
        self.runner.close()
        self.runner._thread.join(1)
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(1)

    def request(self, method, path, body=None, *, token=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=.75)
        headers = {'Origin': f'http://127.0.0.1:{self.http.server_port}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            if token:
                headers['X-Arena-Token'] = self.token
        try:
            conn.request(method, path, None if body is None else json.dumps(body), headers)
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
            time.sleep(.005)
        self.fail('Diagnostic operation did not finish.')

    def test_diagnostics_are_async_and_preserve_bounded_redacted_result(self):
        start = time.monotonic()
        status, accepted = self.request('POST', '/api/test-connections', {'paid_model': False})
        self.assertEqual(status, 202)
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(self.service.entered.wait(.5))
        self.assertEqual(self.request('GET', '/api/state')[0], 200)
        status, duplicate = self.request('POST', '/api/test-connections', {'paid_model': False})
        self.assertEqual(status, 202)
        self.assertEqual(accepted['job']['id'], duplicate['job']['id'])
        self.service.release.set()
        job = self.finish()
        self.assertEqual(job['status'], 'complete')
        self.assertFalse(job['result']['paid_model'])
        self.assertEqual(job['result']['checks'][0]['estimate']['reserved_usd'], .025)
        self.assertEqual(job['result']['checks'][0]['model']['message'], '[redacted]')
        self.assertNotIn('secret', job['result']['checks'][0]['broker'])
        self.assertNotIn('credentials', job['result'])
        self.assertNotIn(self.service.env['OPENAI_API_KEY'], json.dumps(job))
        self.runner.run_now('/api/config', {})
        _, state = self.request('GET', '/api/state')
        previous = next(item for item in state['server_jobs'] if item['id'] == accepted['job']['id'])
        self.assertEqual(previous['result'], job['result'])
        self.assertNotIn('result', state['server_job'])
        self.assertEqual(len(self.service.calls), 1)

    def test_restart_opt_out_and_halt_remain_available_during_diagnostics(self):
        self.request('POST', '/api/test-connections', {'paid_model': True})
        self.assertTrue(self.service.entered.wait(.5))
        status, state = self.request('POST', '/api/auto-resume', {'enabled': False})
        self.assertEqual(status, 200)
        self.assertFalse(state['experiment']['auto_resume'])
        self.assertFalse(self.service.release.is_set())
        status, state = self.request('POST', '/api/halt', {})
        self.assertEqual(status, 200)
        self.assertEqual(state['experiment']['status'], 'halted')

    def test_newer_stop_cancels_queued_paid_diagnostic(self):
        entered, release = threading.Event(), threading.Event()
        original = self.runner._perform

        def delayed(action, **kwargs):
            entered.set()
            release.wait(2)
            return original(action, **kwargs)

        with patch.object(self.runner, '_perform', side_effect=delayed):
            try:
                status, _ = self.request('POST', '/api/test-connections', {'paid_model': True})
                self.assertEqual(status, 202)
                self.assertTrue(entered.wait(.5))
                self.request('POST', '/api/halt', {})
            finally:
                release.set()
            job = self.finish()
        self.assertEqual(job['status'], 'error')
        self.assertIn('canceled', job['error'])
        self.assertEqual(self.service.calls, [])

    def test_diagnostics_and_restart_preference_require_csrf(self):
        for path, body in [('/api/test-connections', {'paid_model': True}),
                           ('/api/auto-resume', {'enabled': False})]:
            with self.subTest(path=path):
                self.assertEqual(self.request('POST', path, body, token=False)[0], 403)
        self.assertTrue(self.service.experiment['auto_resume'])
        self.assertEqual(self.service.calls, [])


if __name__ == '__main__':
    unittest.main()
