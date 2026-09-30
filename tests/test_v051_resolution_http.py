"""Real HTTP/ledger checks for operator recovery; only loopback transport allowed."""
import http.client
from http.server import ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from server import CycleRunner, make_handler
import test_audit_acceptance as acceptance
import test_v051_resolution as resolution_fixtures


class ResolutionHTTPAcceptanceTests(unittest.TestCase):
    make_service = acceptance.AuditAcceptanceTests.make_service
    agent = acceptance.AuditAcceptanceTests.agent
    advance = acceptance.AuditAcceptanceTests.advance
    unknown = resolution_fixtures.ResolutionTests.unknown
    assert_reserved = resolution_fixtures.ResolutionTests.assert_reserved

    def setUp(self):
        real_connect = socket.socket.connect
        acceptance.AuditAcceptanceTests.setUp(self)
        self.network.stop()

        def loopback_only(sock, address):
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
                raise AssertionError('HTTP acceptance prohibits external network access.')
            return real_connect(sock, address)

        self.network = patch('socket.socket.connect', new=loopback_only)
        self.network.start()
        self.unknown()
        self.runner = CycleRunner(self.svc)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.svc, self.runner))
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.port = self.http.server_port
        self.token = self.request('GET', '/api/session')[1]['token']

    def tearDown(self):
        self.runner.close()
        self.runner._thread.join(1)
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(1)
        acceptance.AuditAcceptanceTests.tearDown(self)

    def request(self, method, path, body=None, *, csrf=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        headers = {'Origin': f'http://127.0.0.1:{self.port}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            if csrf:
                headers['X-Arena-Token'] = self.token
        try:
            connection.request(method, path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def wait_job(self):
        deadline = time.monotonic() + 2
        while self.runner.snapshot()['status'] in self.runner.ACTIVE and time.monotonic() < deadline:
            time.sleep(.005)
        result = self.runner.snapshot()
        self.assertNotIn(result['status'], self.runner.ACTIVE)
        return result

    def test_resolution_missing_csrf_cannot_invoke_service_or_release_reservation(self):
        with patch.object(self.svc, 'resolve_unknown_order', wraps=self.svc.resolve_unknown_order) as resolve:
            status, body = self.request('POST', '/api/orders/resolve-unknown', self.args, csrf=False)
            self.assertEqual(status, 403, body)
            resolve.assert_not_called()
        self.assertEqual(self.runner.snapshot()['status'], 'idle')
        self.assert_reserved()

    def test_authenticated_resolution_queues_exact_body_and_completes_real_workflow(self):
        with patch.object(self.svc, 'resolve_unknown_order', wraps=self.svc.resolve_unknown_order) as resolve:
            status, body = self.request('POST', '/api/orders/resolve-unknown', self.args)
            self.assertEqual(status, 202, body)
            self.assertEqual(body['job']['action'], 'orders/resolve-unknown')
            job = self.wait_job()
            self.assertEqual(job['status'], 'complete', job)
            self.assertEqual(job['id'], body['job']['id'])
            resolve.assert_called_once_with(self.args)
        order = self.svc.engine.order_by_client_id(self.order['client_order_id'])
        self.assertEqual(order['status'], 'rejected')
        self.assertEqual(order['operator_resolution']['broker_confirmation'], self.args['broker_confirmation'])
        self.assertEqual(self.agent()['reserved_cash'], 0)
        self.assertEqual(self.brokers['openai'].submit_order.call_count, 1)
        self.svc.monitor()
        control = self.svc.engine.snapshot()['agent_controls']['openai']
        self.assertTrue(control['paused'])
        self.assertEqual(control['pause_kind'], 'operator')


if __name__ == '__main__':
    unittest.main()
