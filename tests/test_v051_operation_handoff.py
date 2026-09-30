"""A stop at worker dispatch must still win inside real Service methods."""
import threading
import time
import unittest
from unittest.mock import patch

from arena.operation_guard import (OperationCanceled, admitted_operation,
                                   ensure_current_operation)
from server import CycleRunner
import test_audit_acceptance as fixture


class OperationHandoffTests(unittest.TestCase):
    make_service = fixture.AuditAcceptanceTests.make_service
    agent = fixture.AuditAcceptanceTests.agent

    def setUp(self):
        fixture.AuditAcceptanceTests.setUp(self)
        self.runner = CycleRunner(self.svc)

    def tearDown(self):
        self.runner.close()
        self.runner._thread.join(1)
        fixture.AuditAcceptanceTests.tearDown(self)

    def finish(self):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            job = self.runner.snapshot()
            if job['status'] not in self.runner.ACTIVE:
                return job
            time.sleep(.005)
        self.fail('Background operation did not finish.')

    def test_stop_in_dispatch_handoff_wins_over_resume_and_enable(self):
        original = self.svc.dispatch
        for route, body in [('/api/resume', {}),
                            ('/api/agent/resume', {'agent_id': 'openai'}),
                            ('/api/autopilot', {'enabled': True}),
                            ('/api/auto-resume', {'enabled': True})]:
            with self.subTest(route=route):
                self.svc.engine.set_auto_resume(False)

                def delayed_dispatch(action, payload):
                    self.svc.halt('New stop after runner validation.')
                    return original(action, payload)

                with patch.object(self.svc, 'dispatch', side_effect=delayed_dispatch):
                    with self.assertRaisesRegex(OperationCanceled, 'newer control or stop'):
                        self.runner.run_now(route, body)
                state = self.svc.engine.snapshot()['experiment']
                self.assertEqual(state['status'], 'halted')
                self.assertFalse(state['autopilot'])
                self.assertFalse(state['auto_resume'])
                self.assertTrue(self.svc.stop_file.exists())

    def test_external_stop_in_dispatch_handoff_is_not_cleared(self):
        original = self.svc.dispatch

        def delayed_dispatch(route, body):
            self.svc.stop_file.write_text('External stop after runner validation.', encoding='utf-8')
            return original(route, body)

        with patch.object(self.svc, 'dispatch', side_effect=delayed_dispatch):
            with self.assertRaisesRegex(OperationCanceled, 'newer control or stop'):
                self.runner.run_now('/api/resume', {})
        self.assertTrue(self.svc.stop_file.exists())
        self.assertEqual(self.svc.public_state()['experiment']['status'], 'halted')

    def test_background_handoff_stop_is_responsive_and_context_is_cleared(self):
        original = self.svc.dispatch
        entered, release = threading.Event(), threading.Event()

        def delayed_dispatch(route, body):
            entered.set()
            release.wait(2)
            return original(route, body)

        with patch.object(self.svc, 'dispatch', side_effect=delayed_dispatch):
            try:
                self.runner.enqueue(route='/api/autopilot', body={'enabled': True})
                self.assertTrue(entered.wait(.5))
                self.svc.halt('Stop from another thread during the dispatch handoff.')
                self.assertEqual(self.svc.public_state()['experiment']['status'], 'halted')
            finally:
                release.set()
            job = self.finish()
        self.assertEqual(job['status'], 'error')
        self.assertIn('canceled', job['error'])
        self.assertTrue(self.svc.stop_file.exists())
        # A new command can intentionally clear the stop; stale admission is
        # scoped to its worker call, not retained on the shared Service.
        self.runner.enqueue(route='/api/autopilot', body={'enabled': True})
        self.assertEqual(self.finish()['status'], 'complete')
        self.assertFalse(self.svc.stop_file.exists())
        self.assertTrue(self.svc.engine.snapshot()['experiment']['autopilot'])

    def test_new_explicit_enable_can_clear_the_stop_it_was_admitted_against(self):
        self.svc.halt('Existing stop, deliberately resumed by a newer command.')
        self.runner.run_now('/api/autopilot', {'enabled': True})
        self.assertFalse(self.svc.stop_file.exists())
        self.assertTrue(self.svc.engine.snapshot()['experiment']['autopilot'])

    def test_close_canceled_after_local_reservation_does_not_become_unknown(self):
        self.svc._submit(self.agent(), 'MSFT', 'buy', 100, notional=25)
        original = self.svc.engine.reserve_order

        def stop_after_reservation(*args, **kwargs):
            order = original(*args, **kwargs)
            self.svc.halt('Stop before reserved close was submitted.')
            return order

        with patch.object(self.svc.engine, 'reserve_order', side_effect=stop_after_reservation):
            with self.assertRaisesRegex(OperationCanceled, 'newer control or stop'):
                self.runner.run_now('/api/close', {'agent_id': 'openai', 'symbol': 'MSFT'})
        self.assertEqual(self.brokers['openai'].submit_order.call_count, 1)
        self.assertFalse(self.svc.engine.pending_orders())
        self.assertEqual(self.svc.engine.snapshot()['orders'][-1]['status'], 'rejected')
        self.assertIn('MSFT', self.agent()['positions'])

    def test_admission_context_is_not_shared_with_other_threads(self):
        guard = {'control_generation': self.svc._control_generation,
                 'stop_version': self.svc._stop_version()}
        experiment_id = self.svc.public_state()['experiment']['id']
        errors = []
        with admitted_operation(guard, experiment_id):
            self.svc.halt('Make the current thread admission stale.')
            with self.assertRaises(OperationCanceled):
                ensure_current_operation(self.svc)

            def independent_work():
                try:
                    ensure_current_operation(self.svc)
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=independent_work)
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        ensure_current_operation(self.svc)
