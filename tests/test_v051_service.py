"""v0.5.1 session-boundary and order-refusal regressions; no external transport."""
import datetime as dt
import unittest
from unittest.mock import Mock, patch

from arena.adapters import AuthenticationApiError, OrderRejectedError
from arena.service import ExecutionDeferred
from arena.operation_guard import OperationCanceled, admitted_operation
import test_audit_acceptance as acceptance
import test_service_autonomous as fixtures


class SessionAndRejectionTests(unittest.TestCase):
    setUp = acceptance.AuditAcceptanceTests.setUp
    tearDown = acceptance.AuditAcceptanceTests.tearDown
    make_service = acceptance.AuditAcceptanceTests.make_service
    agent = acceptance.AuditAcceptanceTests.agent
    advance = acceptance.AuditAcceptanceTests.advance

    def seed_exit(self):
        broker = self.brokers['openai']
        self.svc._execute_plan(self.agent(), {'plan': fixtures.response(orders=[fixtures.buy()], exits=[fixtures.exit_rule()])}, {'cycle_id': 'initial'}, broker)
        broker.prices['MSFT'] = 90
        broker.submit_order.reset_mock()
        return broker

    def assert_operable(self):
        self.assertFalse(self.svc.public_state()['agent_controls']['openai']['paused'])
        self.assertFalse(self.svc.engine.pending_orders())

    def admitted(self):
        return admitted_operation({'control_generation': self.svc._control_generation,
                                   'stop_version': self.svc._stop_version()},
                                  self.svc.engine.snapshot()['experiment']['id'])

    def test_autopilot_off_after_reservation_cancels_unsent_job_order(self):
        broker = self.brokers['openai']
        reserve = self.svc.engine.reserve_order
        def turn_off(*args, **kwargs):
            order = reserve(*args, **kwargs)
            self.svc.dispatch('/api/autopilot', {'enabled': False})
            return order
        with self.admitted(), patch.object(self.svc.engine, 'reserve_order', side_effect=turn_off):
            with self.assertRaises(OperationCanceled):
                self.svc._submit(self.agent(), 'MSFT', 'buy', 100, notional=25, automatic=True)
        broker.submit_order.assert_not_called()
        self.assert_operable()
        self.assertEqual(self.agent()['available_cash'], 250)
        self.assertEqual(self.svc.engine.snapshot()['orders'][-1]['status'], 'rejected')

    def test_autopilot_off_before_paid_request_releases_unsent_cost_reservation(self):
        refresh = self.svc._refresh_cache
        def turn_off():
            self.svc.dispatch('/api/autopilot', {'enabled': False})
            refresh()
        request = Mock(side_effect=AssertionError('No paid call after cancellation.'))
        with self.admitted(), patch.object(self.svc, '_refresh_cache', side_effect=turn_off):
            with self.assertRaisesRegex(ValueError, 'paused before provider'):
                self.svc._paid_request(self.agent(), request, input_bound=4000, output_limit=1024)
        request.assert_not_called()
        self.assertEqual(self.svc.meta.execute('SELECT cost,status FROM calls').fetchall(), [(0.0, 'canceled')])
        self.assertEqual(self.agent()['model_cost'], 0)
        self.assert_operable()

    def test_autopilot_off_during_cancel_lookup_keeps_order_pending_without_cancel_post(self):
        broker = self.brokers['openai']
        order = self.svc.engine.reserve_order('openai', 'SPY', 'buy', 100, notional=25)
        remote = {'id': 'working-id', 'client_order_id': order['client_order_id'], 'symbol': 'SPY',
                  'side': 'buy', 'status': 'new', 'filled_qty': '0', 'filled_avg_price': None}
        def lookup(client_id):
            self.svc.dispatch('/api/autopilot', {'enabled': False})
            return remote
        broker.order_by_client_id = Mock(side_effect=lookup)
        broker.cancel_order = Mock()
        with self.admitted():
            with self.assertRaises(OperationCanceled):
                self.svc.cancel(order['id'])
        broker.cancel_order.assert_not_called()
        self.assertEqual(self.svc.engine.pending_orders()[0]['status'], 'new')
        self.assertGreater(self.agent()['reserved_cash'], 0)
        self.assertFalse(self.svc.public_state()['agent_controls']['openai']['paused'])

    def test_unknown_resolution_route_requires_exact_operator_payload(self):
        with patch('arena.resolution.resolve_unknown_order') as resolve:
            for payload in ({}, {'confirmed_not_accepted': True}, {'unexpected': 'field'}):
                with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, 'exact order identity'):
                    self.svc.dispatch('/api/orders/resolve-unknown', payload)
        resolve.assert_not_called()

    def test_last_90_seconds_defers_exit_and_next_session_retries_without_resume(self):
        broker = self.seed_exit()
        self.time = self.time.replace(hour=19, minute=59, second=0)
        with patch('arena.service.autonomous_turn', return_value=fixtures.response()) as model:
            for _ in range(5):
                self.svc.cycle()
                self.svc.reconcile(force=True)
        self.assertEqual(model.call_count, 2)  # Planning continues near the stock close; the exit still waits.
        broker.submit_order.assert_not_called()
        self.assert_operable()
        self.assertIn('MSFT', self.agent()['positions'])
        recovery = self.svc.public_state()['agent_controls']['openai']['recovery']
        self.assertIn('session close', recovery['execution_warning'])
        warnings = [e for e in self.svc.engine.state()['events'] if 'No order was sent; execution may be retried' in e.get('message', '')]
        self.assertEqual(len(warnings), 1)
        self.time = (self.time + dt.timedelta(days=1)).replace(hour=14, minute=0)
        with patch('arena.service.autonomous_turn', return_value=fixtures.response()):
            self.svc.cycle()
        self.assertEqual(broker.submit_order.call_count, 1)
        self.assertEqual(broker.submit_order.call_args.args[0]['side'], 'sell')
        self.assertNotIn('MSFT', self.agent()['positions'])
        self.assert_operable()
        self.assertEqual(self.svc.public_state()['agent_controls']['openai']['recovery']['execution_warning'], '')

    def test_market_closes_between_outer_and_submission_clock_reads(self):
        broker = self.seed_exit()
        self.time = self.time.replace(hour=19, minute=58)
        first = broker.clock()
        broker.clock = Mock(side_effect=[first, {**first, 'is_open': False}])
        self.svc.cycle()
        self.assertEqual(broker.clock.call_count, 2)
        broker.submit_order.assert_not_called()
        self.assert_operable()
        self.assertIn('MSFT', self.agent()['positions'])
        self.assertIn('MSFT', self.svc.public_state()['autonomy']['openai']['exits'])

    def test_slow_asset_read_enters_close_buffer_without_post_or_pause(self):
        broker = self.seed_exit()
        self.time = self.time.replace(hour=19, minute=58, second=0)
        original = broker.asset
        def cross_boundary(symbol):
            self.advance(31)
            return original(symbol)
        broker.asset = cross_boundary
        self.svc._run_agent_exits(self.agent(), broker)
        broker.submit_order.assert_not_called()
        self.assert_operable()
        self.assertIn('MSFT', self.agent()['positions'])

    def test_boundary_after_reservation_releases_unsent_local_order(self):
        broker = self.seed_exit()
        self.time = self.time.replace(hour=19, minute=58, second=0)
        reserve = self.svc.engine.reserve_order
        def cross_boundary(*args, **kwargs):
            order = reserve(*args, **kwargs)
            self.advance(31)
            return order
        with patch.object(self.svc.engine, 'reserve_order', side_effect=cross_boundary):
            self.svc._run_agent_exits(self.agent(), broker)
        broker.submit_order.assert_not_called()
        self.assert_operable()
        self.assertIn('MSFT', self.agent()['positions'])
        self.assertEqual(self.svc.engine.snapshot()['orders'][-1]['status'], 'rejected')

    def test_slow_asset_read_cannot_cross_entry_cutoff(self):
        broker = self.brokers['openai']
        self.time = self.time.replace(hour=19, minute=44, second=59)
        original = broker.asset
        def cross_boundary(symbol):
            self.advance(2)
            return original(symbol)
        broker.asset = cross_boundary
        with self.assertRaisesRegex(ExecutionDeferred, 'entry window'):
            self.svc._submit(self.agent(), 'MSFT', 'buy', 100, notional=25)
        broker.submit_order.assert_not_called()
        self.assert_operable()
        self.assertEqual(self.agent()['reserved_cash'], 0)

    def test_stale_cycle_clock_is_recoverable_without_operator_resume(self):
        broker = self.brokers['openai']
        original = broker.clock
        clock = original()
        clock['timestamp'] = (self.time - dt.timedelta(seconds=121)).isoformat()
        broker.clock = Mock(return_value=clock)
        with patch('arena.service.autonomous_turn', return_value=fixtures.response()) as model:
            self.svc.cycle()
        self.assert_operable()
        self.assertEqual(model.call_count, 1)  # Healthy peer still researches.
        self.assertEqual(model.call_args.args[0], 'anthropic')
        broker.submit_order.assert_not_called()
        broker.clock = original
        with patch('arena.service.autonomous_turn', return_value=fixtures.response()) as model:
            self.svc.cycle()
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[0], 'openai')
        self.assert_operable()

    def test_403_buy_rejection_releases_cash_without_pausing(self):
        broker = self.brokers['openai']
        before = self.agent()['available_cash']
        broker.submit_order.side_effect = OrderRejectedError('insufficient buying power', status_code=403)
        self.svc._submit(self.agent(), 'MSFT', 'buy', 100, notional=25, automatic=True)
        self.assert_operable()
        self.assertEqual(self.agent()['available_cash'], before)
        self.assertEqual(self.svc.engine.snapshot()['orders'][-1]['status'], 'rejected')
        broker.submit_order.side_effect = broker._submit
        self.svc._submit(self.agent(), 'MSFT', 'buy', 100, notional=25, automatic=True)
        self.assertEqual(broker.submit_order.call_count, 2)
        self.assertEqual(self.agent()['positions']['MSFT']['qty'], .25)

    def test_403_exit_rejection_retries_after_backoff_without_pausing(self):
        broker = self.seed_exit()
        broker.submit_order.side_effect = OrderRejectedError('insufficient shares', status_code=403)
        self.svc._run_agent_exits(self.agent(), broker)
        self.assert_operable()
        retry = self.svc.public_state()['autonomy']['openai']['exit_retries']['MSFT']
        self.assertEqual(retry['http_status'], 403)
        self.assertEqual(retry['reason'], 'inventory_or_funds')
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(broker.submit_order.call_count, 1)
        self.advance(31)
        broker.submit_order.side_effect = broker._submit
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(broker.submit_order.call_count, 2)
        self.assertNotIn('MSFT', self.agent()['positions'])
        self.assert_operable()

    def test_401_submission_and_403_account_read_still_require_operator_resume(self):
        broker = self.brokers['openai']
        broker.submit_order.side_effect = OrderRejectedError('unauthorized', status_code=401)
        self.svc._submit(self.agent(), 'MSFT', 'buy', 100, notional=25)
        self.assertFalse(self.svc.engine.pending_orders())
        self.assertEqual(self.svc.public_state()['agent_controls']['openai']['pause_kind'], 'integrity')
        self.svc.reconcile(force=True)
        self.assertTrue(self.svc.public_state()['agent_controls']['openai']['paused'])
        other = self.brokers['claude']
        original = other.account
        other.account = Mock(side_effect=AuthenticationApiError('forbidden', status_code=403))
        self.svc.monitor()
        self.assertEqual(self.svc.public_state()['agent_controls']['claude']['pause_kind'], 'integrity')
        other.account = original
        self.svc.reconcile(force=True)
        self.assertTrue(self.svc.public_state()['agent_controls']['claude']['paused'])


if __name__ == '__main__':
    unittest.main()
