"""v0.5 integration fixes; transport is prohibited by the shared fixture."""
import datetime as dt
import threading
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from arena.adapters import ApiError, OrderRejectedError, TransientApiError, SubmissionUncertainError
from arena.service import QuoteUnavailable, UnresolvedOrder
import test_audit_acceptance as acceptance
import test_service_autonomous as fixtures


class ServiceBlendTests(unittest.TestCase):
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

    def test_diagnostics_default_has_no_paid_calls_or_account_mutation(self):
        for broker in self.brokers.values():
            broker.prices['SPY'] = 100
        before = self.svc.engine.snapshot()
        with patch('arena.service.model_diagnostic') as model:
            result = self.svc.test_connections()
        model.assert_not_called()
        self.assertEqual([c['broker']['status'] for c in result['checks']], ['ok', 'ok'])
        self.assertTrue(all(c['model']['status'] == 'skipped' for c in result['checks']))
        self.assertEqual(before['orders'], self.svc.engine.snapshot()['orders'])
        self.assertEqual(result['checks'][0]['estimate']['reserved_usd'], .005024)
        for broker in self.brokers.values():
            broker.submit_order.assert_not_called()

    def test_diagnostics_cannot_exceed_remaining_agent_allowance(self):
        self.svc.meta.execute('INSERT INTO calls VALUES (?,?,?,?,?,?)', ('spent', 'past', 'openai', self.time.strftime('%Y-%m'), 5, 'complete'))
        self.svc.meta.commit()
        reply = {'input_tokens': 10, 'output_tokens': 4, 'model': 'actual-id'}
        with patch('arena.service.model_diagnostic', return_value=reply) as model:
            result = self.svc.test_connections(paid_model=True)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['checks'][0]['model']['status'], 'blocked')
        self.assertEqual(result['checks'][1]['model']['actual_model'], 'actual-id')
        self.assertEqual(result['checks'][1]['model']['cost_usd'], .000014)
        self.assertEqual(self.svc.public_state()['autonomy']['claude']['last_provider_model'], 'actual-id')

    def test_failed_paid_diagnostic_retains_reserved_cost_and_does_not_leak_error(self):
        with patch('arena.service.model_diagnostic', side_effect=ApiError('secret-do-not-leak')):
            result = self.svc.test_connections(paid_model=True)
        self.assertNotIn('secret-do-not-leak', str(result))
        rows = self.svc.meta.execute('SELECT cost,status FROM calls').fetchall()
        self.assertEqual(rows, [(.005024, 'uncertain'), (.005024, 'uncertain')])
        self.assertEqual(self.agent()['model_cost'], .005024)
        self.svc = self.make_service()
        self.assertEqual(self.agent()['model_cost'], .005024)

    def test_stop_during_first_diagnostic_blocks_second_request(self):
        def reply(*args, **kwargs):
            self.svc.halt('New stop during diagnostic')
            return {'input_tokens': 10, 'output_tokens': 4, 'model': 'actual-id'}
        with patch('arena.service.model_diagnostic', side_effect=reply) as model:
            result = self.svc.test_connections(paid_model=True)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['checks'][1]['model']['status'], 'blocked')
        self.assertTrue(self.svc.stop_file.exists())

    def test_output_cap_is_sent_and_used_in_reservation(self):
        self.svc.new_experiment({**self.config, 'output_token_limit': 16384})
        self.svc.resume()
        reservations = []
        def model(*args, **kwargs):
            reservations.append(self.svc.meta.execute('SELECT cost FROM calls WHERE status="reserved"').fetchone()[0])
            self.assertEqual(kwargs['max_output_tokens'], 16384)
            return fixtures.response()
        with patch('arena.service.autonomous_turn', side_effect=model):
            result = self.svc._ask_autonomous(self.agent(), {})
        self.assertEqual(reservations, [.061384])
        self.assertNotIn('estimated_cost', result)  # strict autonomous schema remains valid

    def test_healthy_account_recovers_while_deliberately_paused_peer_is_offline(self):
        self.svc.halt_agent('claude')
        self.svc.engine.set_autopilot(True)
        self.advance()
        self.brokers['claude'].account = Mock(side_effect=TransientApiError('offline'))
        self.svc = self.make_service()
        self.svc.monitor()
        state = self.svc.public_state()
        self.assertEqual(state['experiment']['status'], 'ready')
        self.assertFalse(state['agent_controls']['openai']['paused'])
        self.assertTrue(state['agent_controls']['claude']['paused'])
        self.assertEqual(state['agent_controls']['claude']['pause_kind'], 'operator')

    def test_unverified_peer_stays_gated_when_healthy_account_recovers(self):
        self.svc.engine.set_autopilot(True)
        self.advance()
        self.brokers['claude'].account = Mock(side_effect=TransientApiError('offline'))
        self.svc = self.make_service()
        self.svc.monitor()
        with patch('arena.service.autonomous_turn', return_value=fixtures.response()) as model:
            self.svc.cycle()
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[0], 'openai')
        self.assertTrue(self.svc.public_state()['agent_controls']['claude']['paused'])
        self.brokers['claude'].submit_order.assert_not_called()

    def test_disable_auto_resume_during_reconcile_wins(self):
        self.svc.engine.set_autopilot(True)
        self.advance()
        self.svc = self.make_service()
        original = self.brokers['openai'].account
        def disable():
            self.svc.set_auto_resume(False)
            return original()
        self.brokers['openai'].account = disable
        self.svc.monitor()
        state = self.svc.public_state()['experiment']
        self.assertFalse(state['auto_resume'])
        self.assertFalse(state['autopilot'])
        self.assertEqual(state['status'], 'paused')

    def test_repeated_restart_keeps_automatic_intent(self):
        self.svc.engine.set_autopilot(True)
        self.advance()
        self.svc = self.make_service()
        self.svc = self.make_service()
        self.assertEqual(self.svc.public_state()['experiment']['status'], 'recovering')
        self.svc.monitor()
        self.assertTrue(self.svc.public_state()['experiment']['autopilot'])
        self.assertEqual(self.svc.public_state()['experiment']['status'], 'ready')

    def test_claude_restart_preference_imported_once(self):
        self.svc.engine.set_autopilot(True)
        self.svc._put('auto_resume', False)
        self.advance()
        self.svc = self.make_service()
        self.assertFalse(self.svc.public_state()['experiment']['auto_resume'])
        self.svc.monitor()
        self.assertFalse(self.svc.public_state()['experiment']['autopilot'])
        self.svc.set_auto_resume(True)
        self.svc = self.make_service()
        self.assertTrue(self.svc.public_state()['experiment']['auto_resume'])

    def test_legacy_binding_accepts_only_old_terminal_orders_and_moves_cursor(self):
        key = f"bound:{self.svc.engine.snapshot()['experiment']['id']}:openai"
        self.svc._put(key, {'account': 'private-account-openai', 'at': self.time.isoformat(), 'orders_after': self.time.isoformat(), 'prior_orders': []})
        broker = self.brokers['openai']
        broker.book.append({'id': 'old', 'client_order_id': 'old-id', 'status': 'filled', 'submitted_at': (self.time-dt.timedelta(days=30)).isoformat()})
        result = self.svc.reconcile(agent_id='openai', force=True)
        self.assertIn('openai', result['reconciled'])
        self.assertEqual(self.svc._get(key)['order_cursor'], 'old')
        self.assertNotIn('orders_after', self.svc._get(key))
        broker.book.append({'id': 'new', 'client_order_id': 'foreign', 'status': 'filled', 'submitted_at': self.time.isoformat()})
        result = self.svc.reconcile(agent_id='openai', force=True)
        self.assertIn('openai', result['errors'])

    def test_legacy_binding_never_ignores_old_open_order(self):
        key = f"bound:{self.svc.engine.snapshot()['experiment']['id']}:openai"
        self.svc._put(key, {'account': 'private-account-openai', 'at': self.time.isoformat(), 'orders_after': self.time.isoformat(), 'prior_orders': []})
        self.brokers['openai'].book.append({'id': 'open', 'client_order_id': 'foreign', 'status': 'new', 'submitted_at': (self.time-dt.timedelta(days=30)).isoformat()})
        result = self.svc.reconcile(agent_id='openai', force=True)
        self.assertIn('openai', result['errors'])

    def test_exit_rejection_backoff_survives_restart_and_clears_after_success(self):
        broker = self.seed_exit()
        original = broker._submit
        broker.submit_order.side_effect = OrderRejectedError('insufficient quantity', status_code=422)
        self.svc._run_agent_exits(self.agent(), broker)
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(broker.submit_order.call_count, 1)
        record = self.svc.public_state()['autonomy']['openai']['exit_retries']['MSFT']
        self.assertEqual((dt.datetime.fromisoformat(record['next_retry_at']) - self.time).total_seconds(), 30)
        self.svc.engine.set_autopilot(True)
        self.svc = self.make_service()
        self.svc.monitor()
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(broker.submit_order.call_count, 1)
        self.advance(31)
        broker.submit_order.side_effect = original
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(broker.submit_order.call_count, 2)
        self.assertEqual(self.svc.public_state()['autonomy']['openai']['exit_retries'], {})

    def test_unknown_exit_has_no_retry_record_or_replacement(self):
        broker = self.seed_exit()
        broker.submit_order.side_effect = SubmissionUncertainError('timeout')
        with self.assertRaises(UnresolvedOrder):
            self.svc._run_agent_exits(self.agent(), broker)
        self.advance(1000)
        self.svc._run_agent_exits(self.agent(), broker)
        self.assertEqual(broker.submit_order.call_count, 1)
        self.assertEqual(self.svc.public_state()['autonomy']['openai']['exit_retries'], {})

    def test_valuation_trade_never_substitutes_for_execution_quote(self):
        broker = self.brokers['openai']
        broker.quotes = Mock(return_value={'MSFT': {'price': 150, 't': self.time.isoformat(), 'execution_price': 100,
                                                   'execution_timestamp': self.time.isoformat(), 'execution_eligible': True}})
        self.assertEqual(self.svc._fresh_prices(broker, ['MSFT']), {'MSFT': 100})
        old_asset = broker.asset
        def slow_asset(symbol):
            self.advance(121)
            return old_asset(symbol)
        broker.asset = slow_asset
        with self.assertRaises(QuoteUnavailable):
            self.svc._submit(self.agent(), 'MSFT', 'buy', 150, notional=25)
        broker.submit_order.assert_not_called()

    def test_trade_only_mark_can_value_a_holding_but_cannot_trade(self):
        broker = self.seed_exit()
        broker.quotes = Mock(return_value={'MSFT': {'price': 95, 't': self.time.isoformat(), 'execution_price': None,
                                                   'execution_timestamp': None, 'execution_eligible': False, 'price_source': 'trade', 'feed': 'iex'}})
        self.svc._monitor_held_quotes(self.agent())
        self.assertEqual(self.svc.engine.snapshot()['market']['prices']['MSFT'], 95)
        with self.assertRaises(QuoteUnavailable):
            self.svc._submit(self.agent(), 'MSFT', 'sell', 95, automatic=True)
        broker.submit_order.assert_not_called()

    def test_quote_summary_uses_oldest_current_holding_and_missing(self):
        broker = self.brokers['openai']
        self.svc._execute_plan(self.agent(), {'plan': fixtures.response(orders=[fixtures.buy(), fixtures.buy('NVDA')])}, {'cycle_id': 'holdings'}, broker)
        self.advance(300)
        self.svc.engine.note_quotes('openai', {'MSFT': self.time.isoformat(), 'SPY': (self.time+dt.timedelta(seconds=30)).isoformat()})
        control = self.svc.public_state()['agent_controls']['openai']
        self.assertEqual(control['last_quote_at'], (self.time-dt.timedelta(seconds=300)).isoformat())
        # Simulate an older imported holding without a saved timestamp.
        with self.svc.engine._write():
            self.svc.engine._state['agent_controls']['openai']['quote_times'].pop('NVDA')
        control = self.svc.public_state()['agent_controls']['openai']
        self.assertIsNone(control['last_quote_at'])
        self.assertEqual(control['quote_missing'], ['NVDA'])


if __name__ == '__main__':
    unittest.main()
