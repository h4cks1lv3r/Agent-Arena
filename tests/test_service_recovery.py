"""Recovery cursor, retry cadence and control-race regressions, without network."""
from copy import deepcopy
import datetime as dt
import threading
import unittest
from unittest.mock import Mock, patch

from arena.adapters import AuthenticationApiError, OrderNotFoundError, OrderRejectedError, TransientApiError
from arena.service import UnresolvedOrder
import test_audit_acceptance as acceptance
import test_service_autonomous as fixtures


class RecoveryServiceTests(unittest.TestCase):
    setUp = acceptance.AuditAcceptanceTests.setUp
    tearDown = acceptance.AuditAcceptanceTests.tearDown
    make_service = acceptance.AuditAcceptanceTests.make_service
    agent = acceptance.AuditAcceptanceTests.agent
    advance = acceptance.AuditAcceptanceTests.advance

    def test_transient_read_backoff_is_capped_and_success_clears_it(self):
        broker = self.brokers['openai']
        original = broker.account
        broker.account = Mock(side_effect=TransientApiError('temporary', status_code=503))
        self.svc.engine.set_autopilot(True)
        for expected in (15, 30, 60, 120, 240, 300, 300):
            self.svc.monitor()
            control = self.svc.public_state()['agent_controls']['openai']
            due = dt.datetime.fromisoformat(control['recovery']['next_retry_at'])
            self.assertEqual((due - self.time).total_seconds(), expected)
            attempts = broker.account.call_count
            self.svc.monitor()
            self.assertEqual(broker.account.call_count, attempts)
            self.advance(expected)
        broker.account = original
        self.svc.monitor()
        control = self.svc.public_state()['agent_controls']['openai']
        self.assertFalse(control['paused'])
        self.assertEqual(control['recovery']['attempts'], 0)
        self.assertTrue(self.svc.engine.snapshot()['experiment']['autopilot'])

    def test_permission_failure_stays_paused_after_read_endpoint_recovers(self):
        broker = self.brokers['openai']
        original = broker.account
        broker.account = Mock(side_effect=AuthenticationApiError('permission denied', status_code=403))
        self.svc.monitor()
        self.assertEqual(self.svc.public_state()['agent_controls']['openai']['pause_kind'], 'integrity')
        broker.account = original
        self.advance()
        self.svc.monitor()
        self.assertTrue(self.svc.public_state()['agent_controls']['openai']['paused'])
        self.svc.resume_agent('openai')
        self.assertFalse(self.svc.public_state()['agent_controls']['openai']['paused'])

    def test_incremental_reconcile_does_not_request_old_history_again(self):
        broker = self.brokers['openai']
        self.svc._execute_plan(self.agent(), {'plan': fixtures.response(orders=[fixtures.buy()])}, {'cycle_id': 'cursor'}, broker)
        self.svc.reconcile()
        last_id = broker.book[-1]['id']
        broker.orders = Mock(wraps=broker.orders)
        with patch.object(self.svc.engine, 'state', side_effect=AssertionError('Full history must not be loaded.')):
            self.svc.monitor()
            self.svc.public_state()
        self.assertEqual(broker.orders.call_args_list[0].kwargs, {'status': 'all', 'after_order_id': last_id})
        self.assertEqual(broker.orders.call_args_list[1].kwargs, {'status': 'open'})

    def test_legacy_unknown_with_404_retains_reservation_and_client_id(self):
        order = self.svc.engine.reserve_order('openai', 'SPY', 'buy', 100, notional=25)
        self.svc.engine.unknown_order(order['id'], 'Pre-0.4 unknown outcome; no rejection evidence.', halt=False)
        self.brokers['openai'].order_by_client_id = Mock(side_effect=OrderNotFoundError('not found', status_code=404))
        self.svc.reconcile(force=True)
        control = self.svc.public_state()['agent_controls']['openai']
        self.assertEqual(control['pause_kind'], 'unknown_order')
        self.assertTrue(self.svc.engine.pending_orders()[0]['reserved'] > 0)
        self.assertEqual(self.svc.engine.pending_orders()[0]['client_order_id'], order['client_order_id'])
        self.advance()
        self.svc.monitor()
        self.brokers['openai'].submit_order.assert_not_called()
        self.assertEqual(self.brokers['openai'].order_by_client_id.call_count, 2)

    def test_late_explicit_resume_cannot_undo_new_operator_halt(self):
        self.svc.halt()
        entered, release = threading.Event(), threading.Event()
        original = self.brokers['openai'].account
        first = True
        failures = []
        def slow_account():
            nonlocal first
            if first:
                first = False
                entered.set()
                release.wait(3)
            return original()
        self.brokers['openai'].account = slow_account
        def resume():
            try:
                self.svc.resume()
            except ValueError as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=resume)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.svc.halt('A later operator stop wins.')
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(failures)
        self.assertTrue(self.svc.stop_file.exists())
        self.assertEqual(self.svc.engine.snapshot()['experiment']['status'], 'halted')

    def test_quote_only_observation_failure_does_not_pause_agent(self):
        broker = self.brokers['openai']
        self.svc._execute_plan(self.agent(), {'plan': fixtures.response(orders=[fixtures.buy()])}, {'cycle_id': 'hold'}, broker)
        broker.quotes = Mock(side_effect=TransientApiError('data feed temporarily unavailable', status_code=503))
        self.svc.monitor()
        state = self.svc.public_state()
        self.assertFalse(state['agent_controls']['openai']['paused'])
        self.assertIn('temporarily unavailable', state['agent_controls']['openai']['recovery']['quote_warning'])
        self.assertEqual(self.agent()['positions']['MSFT']['qty'], .3725)

    def test_definitive_auth_rejection_releases_cash_and_stops_future_calls(self):
        broker = self.brokers['openai']
        broker.submit_order.side_effect = OrderRejectedError('credential refused', status_code=401)
        before = self.agent()['available_cash']
        self.svc._execute_plan(self.agent(), {'plan': fixtures.response(orders=[fixtures.buy()])}, {'cycle_id': 'auth-reject'}, broker)
        self.assertFalse(self.svc.engine.pending_orders())
        self.assertEqual(self.agent()['available_cash'], before)
        control = self.svc.public_state()['agent_controls']['openai']
        self.assertTrue(control['paused'])
        self.assertEqual(control['pause_kind'], 'integrity')
        self.assertEqual(broker.submit_order.call_count, 1)

    def test_external_stop_created_during_resume_is_not_deleted(self):
        original = self.brokers['openai'].account
        def external_stop():
            self.svc.stop_file.write_text('Independent emergency stop', encoding='utf-8')
            return original()
        self.brokers['openai'].account = external_stop
        with self.assertRaisesRegex(ValueError, 'stop was requested'):
            self.svc.resume()
        self.assertTrue(self.svc.stop_file.exists())
        self.assertEqual(self.svc.public_state()['experiment']['status'], 'halted')

    def test_fill_between_order_and_position_reads_reconciles_without_manual_pause(self):
        broker = self.brokers['openai']
        local = self.svc.engine.reserve_order('openai', 'SPY', 'buy', 100, notional=25)
        remote = {'id': 'fill-race-1', 'client_order_id': local['client_order_id'], 'symbol': 'SPY',
                  'side': 'buy', 'status': 'new', 'filled_qty': '0', 'filled_avg_price': None}
        broker.book.append(remote)
        original = broker.positions
        first = True
        def fill_during_position_read():
            nonlocal first
            if first:
                first = False
                remote.update(status='filled', filled_qty='.25', filled_avg_price='100', filled_at=self.time.isoformat())
                broker.holdings['SPY'] = .25
            return original()
        broker.positions = fill_during_position_read
        result = self.svc.reconcile(force=True)
        self.assertIn('openai', result['reconciled'])
        self.assertFalse(self.svc.public_state()['agent_controls']['openai']['paused'])
        self.assertEqual(self.agent()['positions']['SPY']['qty'], .25)
        self.assertFalse(self.svc.engine.pending_orders())
        broker.submit_order.assert_not_called()

    def test_pending_inventory_mismatch_blocks_trades_then_becomes_integrity_if_order_ends(self):
        broker = self.brokers['openai']
        local = self.svc.engine.reserve_order('openai', 'SPY', 'buy', 100, notional=25)
        remote = {'id': 'mismatch-1', 'client_order_id': local['client_order_id'], 'symbol': 'SPY',
                  'side': 'buy', 'status': 'new', 'filled_qty': '0', 'filled_avg_price': None}
        broker.book.append(remote)
        broker.holdings['NVDA'] = 1  # Unexplained inventory must not be adopted.
        self.svc.reconcile(force=True)
        self.assertEqual(self.svc.public_state()['agent_controls']['openai']['pause_kind'], 'transient')
        self.assertEqual(self.agent()['positions'], {})
        remote['status'] = 'rejected'
        self.advance()
        self.svc.monitor()
        self.assertEqual(self.svc.public_state()['agent_controls']['openai']['pause_kind'], 'integrity')
        self.assertEqual(self.agent()['positions'], {})
        broker.submit_order.assert_not_called()
