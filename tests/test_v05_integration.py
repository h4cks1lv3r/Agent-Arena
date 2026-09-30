"""Cross-feature acceptance under deterministic faults, with network prohibited.

The clock advances across selected synthetic sessions. These are bounded state
machine checks, not a multi-week uptime test, a market simulation, or evidence
that an investment strategy makes money.
"""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import unittest
from unittest.mock import Mock, patch

from arena import adapters
from arena.service import QuoteUnavailable
import test_audit_acceptance as acceptance
import test_service_autonomous as fixtures


class CombinedAcceptanceTests(unittest.TestCase):
    setUp = acceptance.AuditAcceptanceTests.setUp
    tearDown = acceptance.AuditAcceptanceTests.tearDown
    make_service = acceptance.AuditAcceptanceTests.make_service
    agent = acceptance.AuditAcceptanceTests.agent
    advance = acceptance.AuditAcceptanceTests.advance

    def restart(self):
        """Close both SQLite handles; no previous process remains available."""
        old = self.svc
        old.meta.close()
        old.engine.close()
        self.services.remove(old)
        self.svc = self.make_service()

    def test_120_fault_cycles_keep_original_orders_cash_and_account_isolation(self):
        self.config.update(cycle_minutes=15, max_cycles_per_day=24)
        self.svc.new_experiment(self.config)
        self.svc.resume()
        self.svc.engine.set_autopilot(True)
        exp_id = self.svc.engine.snapshot()['experiment']['id']
        broker = self.brokers['openai']
        self.svc._submit(self.agent(), 'MSFT', 'buy', 100, notional=25)
        broker.submit_order.side_effect = adapters.SubmissionUncertainError('Response lost after send.')
        with self.assertRaises(ValueError):
            self.svc._submit(self.agent(), 'NVDA', 'buy', 100, notional=20)
        original_payload = deepcopy(broker.submit_order.call_args.args[0])
        client_id = original_payload['client_order_id']
        local_id = self.svc.engine.pending_orders()[0]['id']
        reserved = self.agent()['reserved_cash']
        broker.submit_order.side_effect = broker._submit
        account_reads = {name: book.account for name, book in self.brokers.items()}
        quote_reads = {name: book.quotes for name, book in self.brokers.items()}
        offline = set()
        paid_counts = {'openai': 0, 'claude': 0}
        stale_checks = 0
        restarts = 0

        def model(provider, _model, _key, _snapshot, **_kwargs):
            name = 'openai' if provider == 'openai' else 'claude'
            self.assertNotIn(name, offline, 'A failed account reached a paid request.')
            self.assertFalse(self.svc._stopped(name))
            self.assertFalse(any(order['agent_id'] == name and order['status'] == 'unknown'
                                 for order in self.svc.engine.pending_orders()))
            paid_counts[name] += 1
            return fixtures.response()

        with patch('arena.service.autonomous_turn', side_effect=model):
            for tick in range(120):
                with self.subTest(tick=tick):
                    # Twenty quarter-hour checks on each of six Thursdays.
                    self.time = fixtures.START + dt.timedelta(days=7 * (tick // 20), minutes=15 * (tick % 20))
                    offline.clear()
                    if tick % 9 in (2, 3):
                        offline.add('openai')
                    if tick % 13 == 5:
                        offline.add('claude')
                    if tick % 17 == 7:
                        offline.update(self.brokers)
                    for name, book in self.brokers.items():
                        book.account = (Mock(side_effect=adapters.TransientApiError('Injected account outage.'))
                                        if name in offline else account_reads[name])
                        book.quotes = quote_reads[name]
                    stale = tick % 7 == 4
                    if stale:
                        def stale_quotes(symbols):
                            rows = quote_reads['openai'](symbols)
                            if 'MSFT' in rows:
                                rows['MSFT'].update(t=(self.time - dt.timedelta(minutes=19)).isoformat(),
                                                    source='trade', feed='delayed_sip', execution_eligible=False)
                            return rows
                        broker.quotes = stale_quotes
                    if tick and tick % 11 == 0:
                        self.restart()
                        restarts += 1
                        if tick % 22 == 0:
                            # Reboot again before the first startup check.
                            self.restart()
                            restarts += 1
                    if tick == 36:
                        # The broker discloses that the one ORIGINAL request
                        # filled. This is a book update, not a new submission.
                        broker._submit(original_payload)
                    before = dict(paid_counts)
                    self.svc.monitor()
                    self.svc.cycle()
                    state = self.svc.engine.state()
                    self.assertEqual(state['experiment']['id'], exp_id)
                    self.assertTrue(state['experiment']['autopilot'])
                    self.assertNotEqual(state['experiment']['status'], 'halted')
                    for name in offline:
                        self.assertEqual(paid_counts[name], before[name])
                    if tick < 36:
                        self.assertEqual(paid_counts['openai'], 0)
                        pending = self.svc.engine.pending_orders()
                        self.assertEqual(len(pending), 1)
                        self.assertEqual((pending[0]['id'], pending[0]['client_order_id']), (local_id, client_id))
                        self.assertAlmostEqual(self.agent()['reserved_cash'], reserved)
                    if stale and not self.svc._stopped('openai'):
                        with self.assertRaises(QuoteUnavailable):
                            self.svc._submit(self.agent(), 'MSFT', 'buy', 100, notional=1)
                        stale_checks += 1
                    self.assertEqual(broker.submit_order.call_count, 2, 'Uncertain submission was replayed.')
                    self.brokers['claude'].submit_order.assert_not_called()
                    self.assertEqual(len(state['orders']), 2)
                    self.assertEqual(len({order['client_order_id'] for order in state['orders']}), 2)
                    for name in self.brokers:
                        agent = self.agent(name)
                        position_value = sum(item['qty'] * 100 for item in agent['positions'].values())
                        self.assertAlmostEqual(agent['cash'] + position_value + agent['fees'], 250, places=6)

        for name, book in self.brokers.items():
            book.account, book.quotes = account_reads[name], quote_reads[name]
        self.advance(301)
        self.svc.monitor()
        self.assertEqual(self.svc.engine.pending_orders(), [])
        self.assertAlmostEqual(self.agent()['positions']['MSFT']['qty'], .25)
        self.assertAlmostEqual(self.agent()['positions']['NVDA']['qty'], .20)
        self.assertEqual(self.svc.engine.order_by_client_id(client_id)['id'], local_id)
        self.assertEqual(self.agent()['reserved_cash'], 0)
        self.assertGreater(paid_counts['openai'], 10)
        self.assertGreater(paid_counts['claude'], 10)
        self.assertGreater(stale_checks, 5)
        self.assertEqual(restarts, 15)

    def test_stop_during_diagnostic_settles_one_call_and_blocks_peer_across_restart(self):
        for book in self.brokers.values():
            book.prices['SPY'] = 100
        self.svc.engine.set_autopilot(True)
        observed = []

        def diagnostic(_provider, _model, _key, **_kwargs):
            row = self.svc.meta.execute('SELECT id,cost,status FROM calls').fetchone()
            observed.append(row)
            self.assertEqual(row[2], 'reserved')
            self.assertGreater(row[1], 0)
            self.svc.halt('Newer operator stop while diagnostic was in flight.')
            return {'input_tokens': 100, 'output_tokens': 25, 'model': 'observed-test-model'}

        with patch('arena.service.model_diagnostic', side_effect=diagnostic) as paid:
            result = self.svc.test_connections(paid_model=True)
        self.assertEqual(paid.call_count, 1)
        self.assertEqual([row['model']['status'] for row in result['checks']], ['ok', 'blocked'])
        self.assertEqual(len(observed), 1)
        call_id = observed[0][0]
        amount = self.agent()['model_cost']
        self.assertAlmostEqual(amount, .000125)
        for _ in range(2):
            self.restart()
            with patch('arena.service.model_diagnostic') as paid:
                self.svc.monitor()
                self.svc.test_connections(paid_model=True)
                paid.assert_not_called()
            state = self.svc.engine.snapshot()
            self.assertEqual(state['experiment']['status'], 'halted')
            self.assertFalse(state['experiment']['autopilot'])
            self.assertTrue(self.svc.stop_file.exists())
            self.assertEqual(self.agent()['model_cost'], amount)
            self.assertEqual(self.agent()['cash'], 250)
            self.assertEqual(self.svc.meta.execute('SELECT status FROM calls WHERE id=?', (call_id,)).fetchone()[0], 'complete')
        for book in self.brokers.values():
            book.submit_order.assert_not_called()

    def test_interrupted_diagnostic_reservation_is_charged_once_and_limits_future_calls(self):
        self.config['monthly_model_budget'] = .011
        self.svc.new_experiment(self.config)
        self.svc.resume()
        self.svc.engine.set_autopilot(True)
        for book in self.brokers.values():
            book.prices['SPY'] = 100
        with patch('arena.service.model_diagnostic', side_effect=KeyboardInterrupt('Simulated process interruption.')) as paid:
            with self.assertRaises(KeyboardInterrupt):
                self.svc.test_connections(paid_model=True)
        self.assertEqual(paid.call_count, 1)
        row = self.svc.meta.execute('SELECT id,cost,status FROM calls').fetchone()
        self.assertEqual(row[2], 'reserved')
        for _ in range(2):
            self.restart()
            self.svc.monitor()
            self.assertAlmostEqual(self.agent()['model_cost'], row[1])
            self.assertEqual(self.agent()['cash'], 250)
        self.assertEqual(self.svc.engine.snapshot()['experiment']['status'], 'ready')
        with patch('arena.service.model_diagnostic', return_value={'input_tokens': 100, 'output_tokens': 25, 'model': 'observed-test-model'}) as paid:
            result = self.svc.test_connections(paid_model=True)
        # The first agent cannot spend its peer's budget after an uncertain call.
        self.assertEqual(result['checks'][0]['model']['status'], 'blocked')
        self.assertIn('budget', result['checks'][0]['model']['message'])
        self.assertEqual(paid.call_count, 1)
        self.assertEqual(paid.call_args.args[0], 'anthropic')
        self.assertEqual(self.svc.meta.execute('SELECT status FROM calls WHERE id=?', (row[0],)).fetchone()[0], 'uncertain')
        self.assertAlmostEqual(self.agent()['model_cost'], row[1])
        for book in self.brokers.values():
            book.submit_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
