"""Admitted network jobs wait for monitoring without hiding newer stops."""
from contextlib import contextmanager
import threading
import time
import unittest
from unittest.mock import patch

import test_control_jobs as fixture


class JobMonitorWaitTests(unittest.TestCase):
    setUp = fixture.ControlJobTests.setUp
    tearDown = fixture.ControlJobTests.tearDown
    request = fixture.ControlJobTests.request
    finish = fixture.ControlJobTests.finish

    @contextmanager
    def busy_monitor(self):
        acquired, release = threading.Event(), threading.Event()

        def hold_monitor_lock():
            with self.service.lock:
                acquired.set()
                release.wait(3)

        thread = threading.Thread(target=hold_monitor_lock)
        thread.start()
        try:
            self.assertTrue(acquired.wait(.5))
            yield release
        finally:
            release.set()
            thread.join(1)

    def wait_for_monitor(self):
        deadline = time.monotonic() + .75
        while time.monotonic() < deadline:
            job = self.runner.snapshot()
            if job.get('waiting_for') == 'account_monitor':
                self.assertEqual(job['status'], 'queued')
                return job
            time.sleep(.005)
        self.fail('Admitted job did not wait for the monitor.')

    def test_network_close_waits_and_runs_once_after_monitor_finishes(self):
        body = {'agent_id': 'openai', 'symbol': 'MSFT'}
        with self.busy_monitor() as release:
            started = time.monotonic()
            status, accepted = self.request('POST', '/api/close', body)
            self.assertEqual(status, 202)
            self.assertLess(time.monotonic() - started, .5)
            self.wait_for_monitor()
            self.assertEqual(self.service.calls, [])
            self.assertEqual(self.request('GET', '/api/state')[0], 200)
            status, duplicate = self.request('POST', '/api/close', body)
            self.assertEqual(status, 202)
            self.assertEqual(duplicate['job']['id'], accepted['job']['id'])
            self.assertEqual(self.request('POST', '/api/resume', {})[0], 409)
            self.service.release.set()
            release.set()
            job = self.finish()
        self.assertEqual(job['status'], 'complete')
        self.assertNotIn('waiting_for', job)
        self.assertEqual(self.service.calls, ['/api/close'])

    def test_scheduled_cycle_waits_instead_of_skipping_exit_checks(self):
        with self.busy_monitor() as release:
            self.runner.enqueue('autopilot')
            self.wait_for_monitor()
            self.assertEqual(self.service.calls, [])
            self.service.release.set()
            release.set()
            self.assertEqual(self.finish()['status'], 'complete')
        self.assertEqual(self.service.calls, ['/api/cycle'])

    def test_halt_cancels_waiting_job_before_monitor_releases_lock(self):
        with self.busy_monitor():
            self.request('POST', '/api/resume', {})
            self.wait_for_monitor()
            started = time.monotonic()
            self.assertEqual(self.request('POST', '/api/halt', {})[0], 200)
            job = self.finish()
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(job['status'], 'error')
            self.assertIn('canceled', job['error'])
            self.assertEqual(self.service.calls, [])
            self.assertEqual(self.service.experiment['status'], 'halted')

    def test_shutdown_cancels_waiting_job_before_monitor_releases_lock(self):
        with self.busy_monitor():
            self.runner.enqueue()
            self.wait_for_monitor()
            self.runner.close()
            job = self.finish()
            self.assertEqual(job['status'], 'error')
            self.assertIn('shutdown', job['error'])
            self.assertEqual(self.service.calls, [])

    def test_changed_experiment_cancels_waiting_job(self):
        with self.busy_monitor():
            self.runner.enqueue()
            self.wait_for_monitor()
            self.service.experiment['id'] = 'experiment-2'
            job = self.finish()
            self.assertEqual(job['status'], 'error')
            self.assertIn('experiment changed', job['error'])
            self.assertEqual(self.service.calls, [])

    def test_non_http_control_cancels_waiting_resume(self):
        self.service._control_generation = 0
        with self.busy_monitor():
            self.request('POST', '/api/resume', {})
            self.wait_for_monitor()
            self.service._control_generation += 1
            job = self.finish()
            self.assertEqual(job['status'], 'error')
            self.assertIn('newer control', job['error'])
            self.assertEqual(self.service.calls, [])

    def test_new_external_stop_file_cancels_waiting_resume(self):
        stop_stamp = [None]
        self.service._stop_version = lambda: stop_stamp[0]
        with self.busy_monitor():
            self.request('POST', '/api/resume', {})
            self.wait_for_monitor()
            stop_stamp[0] = (123, 40, 99999999)
            job = self.finish()
            self.assertEqual(job['status'], 'error')
            self.assertIn('newer control or stop', job['error'])
            self.assertEqual(self.service.calls, [])

    def test_wait_timeout_is_bounded_and_does_not_halt_automation(self):
        with self.busy_monitor(), patch.object(self.runner, 'MONITOR_WAIT_SECONDS', .1):
            self.runner.enqueue()
            job = self.finish()
            self.assertEqual(job['status'], 'error')
            self.assertIn('waiting limit', job['error'])
            self.assertEqual(self.service.calls, [])
            self.assertTrue(self.service.experiment['autopilot'])

    def test_resolution_is_network_job_and_requires_csrf(self):
        body = {'experiment_id': 'experiment-1', 'agent_id': 'openai',
                'order_id': 'order-1', 'client_order_id': 'client-1',
                'broker_confirmation': 'Support case ABC-123: not accepted.',
                'confirmed_not_accepted': True}
        with self.busy_monitor() as release:
            status, _ = self.request('POST', '/api/orders/resolve-unknown', body)
            self.assertEqual(status, 202)
            self.wait_for_monitor()
            release.set()
            self.assertEqual(self.finish()['status'], 'complete')
        self.assertEqual(self.service.calls, ['/api/orders/resolve-unknown'])
        original_token = self.token
        try:
            self.token = 'incorrect-token'
            self.assertEqual(self.request('POST', '/api/orders/resolve-unknown', body)[0], 403)
        finally:
            self.token = original_token
