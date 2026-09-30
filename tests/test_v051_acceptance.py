"""Independent control handoff acceptance: newer stops must win over old jobs."""
import threading
import time
import unittest
from unittest.mock import patch

from server import CycleRunner
import test_audit_acceptance as acceptance


class StopHandoffAcceptanceTests(unittest.TestCase):
    setUp = acceptance.AuditAcceptanceTests.setUp
    tearDown = acceptance.AuditAcceptanceTests.tearDown
    make_service = acceptance.AuditAcceptanceTests.make_service
    agent = acceptance.AuditAcceptanceTests.agent
    advance = acceptance.AuditAcceptanceTests.advance

    def run_at_handoff(self, route, body, newer_stop):
        runner = CycleRunner(self.svc)
        original = self.svc.dispatch
        entered = threading.Event()

        def dispatch(*args, **kwargs):
            # The worker has validated its queue token and acquired the service
            # lock, but dispatch has not captured any new generation yet.
            newer_stop()
            entered.set()
            return original(*args, **kwargs)

        try:
            with patch.object(self.svc, 'dispatch', side_effect=dispatch):
                runner.enqueue(route=route, body=body)
                self.assertTrue(entered.wait(1), 'Worker did not reach dispatch.')
                deadline = time.monotonic() + 2
                while runner.snapshot()['status'] in runner.ACTIVE and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertNotIn(runner.snapshot()['status'], runner.ACTIVE)
                self.assertEqual(runner.snapshot()['status'], 'error')
        finally:
            runner.close()
            runner._thread.join(1)

    def test_new_global_stop_at_resume_handoff_is_not_cleared(self):
        self.run_at_handoff('/api/resume', {}, lambda: self.svc.halt('Newer global stop.'))
        state = self.svc.engine.snapshot()
        self.assertEqual(state['experiment']['status'], 'halted')
        self.assertTrue(self.svc.stop_file.exists())
        self.assertFalse(state['experiment']['autopilot'])

    def test_new_global_stop_at_autopilot_enable_handoff_wins(self):
        self.run_at_handoff('/api/autopilot', {'enabled': True}, lambda: self.svc.halt('Newer global stop.'))
        state = self.svc.engine.snapshot()
        self.assertEqual(state['experiment']['status'], 'halted')
        self.assertTrue(self.svc.stop_file.exists())
        self.assertFalse(state['experiment']['autopilot'])

    def test_new_agent_stop_at_agent_resume_handoff_wins(self):
        self.run_at_handoff('/api/agent/resume', {'agent_id': 'openai'},
                            lambda: self.svc.halt_agent('openai', 'Newer agent stop.'))
        state = self.svc.engine.snapshot()
        control = state['agent_controls']['openai']
        self.assertTrue(control['paused'])
        self.assertEqual(control['pause_kind'], 'operator')
        self.assertEqual(control['reason'], 'Newer agent stop.')

    def test_new_external_stop_file_at_resume_handoff_is_not_removed(self):
        self.run_at_handoff('/api/resume', {}, lambda: self.svc.stop_file.write_text('External emergency stop.'))
        self.assertTrue(self.svc.stop_file.exists())
        self.svc.check_stop()
        self.assertEqual(self.svc.engine.snapshot()['experiment']['status'], 'halted')


if __name__ == '__main__':
    unittest.main()
