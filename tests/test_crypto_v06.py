"""Paper-only overnight execution, deferred stock ideas, and fee reconciliation."""
from copy import deepcopy
import datetime as dt
import tempfile
import unittest
from unittest.mock import Mock, patch

from arena.adapters import AlpacaPaper, ApiError
from arena.core import EngineError
from arena.service import Service
from test_service_autonomous import FakePaper, response, buy


NIGHT = dt.datetime(2026, 9, 20, 2, 0, tzinfo=dt.timezone.utc)  # Saturday night in New York


class CryptoPaper(FakePaper):
    def __init__(self, agent_id, clock):
        super().__init__(agent_id, clock)
        self.prices['BTC/USD'] = 100.0
        self.fees = []

    def clock(self):
        return {'is_open': False, 'timestamp': self.now().isoformat(),
                'next_open': '2026-09-21T13:30:00Z', 'next_close': (self.now() + dt.timedelta(days=2)).isoformat()}

    def asset(self, symbol):
        if symbol == 'BTC/USD':
            return {'symbol': symbol, 'status': 'active', 'name': 'Bitcoin USD',
                    'class': 'crypto', 'exchange': 'ALPACA', 'tradable': True, 'fractionable': True}
        return super().asset(symbol)

    def assets(self, include_crypto=False):
        return [self.asset(s) for s in self.prices if include_crypto or s != 'BTC/USD']

    def quotes(self, symbols):
        rows = super().quotes(symbols)
        for symbol in symbols:
            if symbol == 'BTC/USD':
                rows[symbol].update(feed='crypto_us', execution_eligible=True,
                                    execution_price=100.0, execution_timestamp=self.now().isoformat())
        return rows

    def positions(self):
        return [{'symbol': s.replace('/', '') if s == 'BTC/USD' else s,
                 'asset_class': 'crypto' if s == 'BTC/USD' else 'us_equity', 'qty': str(q)}
                for s, q in self.holdings.items() if q > 1e-9]

    def crypto_fees(self, after):
        return deepcopy(self.fees)

    def crypto_movers(self, symbols):
        return {'gainers': [{'symbol': 'BTC/USD', 'price': 100, 'percent_change': 1.5}],
                'losers': [], 'window': 'since_previous_completed_utc_day', 'feed': 'crypto_us'}


class CryptoBrokerTests(unittest.TestCase):
    @patch('arena.adapters._request')
    def test_crypto_uses_alpaca_us_data_and_gtc_paper_order(self, request):
        paper = AlpacaPaper('PAPER', 'SECRET')
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        request.return_value = {'BTC/USD': {'latestQuote': {'bp': 99, 'ap': 101, 't': stamp}}}
        self.assertTrue(paper.quotes(['BTC/USD'])['BTC/USD']['execution_eligible'])
        self.assertIn('/v1beta3/crypto/us/snapshots', request.call_args.args[1])
        self.assertNotIn('/v2/v1beta3/', request.call_args.args[1])
        request.return_value = {'bars': {'BTC/USD': [{'t': '2026-09-19T00:00:00Z', 'o': 99, 'h': 102,
                                                     'l': 98, 'c': 100, 'v': 1}]}}
        bars = paper.bars(['BTC/USD'], '2026-09-18', '2026-09-20')
        self.assertEqual(bars['BTC/USD'][0]['feed'], 'crypto_us')
        self.assertEqual(bars['BTC/USD'][0]['adjustment'], 'raw')
        self.assertIn('/v1beta3/crypto/us/bars', request.call_args.args[1])
        payload = {'symbol': 'BTC/USD', 'side': 'buy', 'notional': '25.00',
                   'type': 'market', 'time_in_force': 'gtc', 'client_order_id': 'arena-crypto'}
        request.return_value = {'id': 'paper-order', 'client_order_id': 'arena-crypto', 'status': 'accepted'}
        paper.submit_order(payload)
        self.assertEqual(request.call_args.args[1], 'https://paper-api.alpaca.markets/v2/orders')
        with self.assertRaises(ApiError):
            paper.submit_order({**payload, 'time_in_force': 'day'})


class OvernightPaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.time = NIGHT
        self.clock = patch('arena.service.now', side_effect=lambda: self.time)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('No real network in tests.'))
        self.clock.start()
        self.network.start()
        self.broker = CryptoPaper('openai', lambda: self.time)
        env = {'OPENAI_API_KEY': 'test-model-key', 'ALPACA_OPENAI_KEY': 'paper-key', 'ALPACA_OPENAI_SECRET': 'paper-secret'}
        self.service = Service(self.tmp.name, environ=env)
        self.service.broker = Mock(return_value=self.broker)
        self.service.new_experiment({'mode': 'paper', 'asset_scope': 'equities_crypto', 'agent_mode': 'autonomous',
                                     'monthly_model_budget': 10, 'total_capital': 250, 'loss_limit': 100,
                                     'position_cap_pct': 50, 'exposure_cap_pct': 75,
                                     'agents': [{'id': 'openai', 'name': 'Test', 'provider': 'openai', 'model': 'test-model',
                                                 'weight': 1, 'input_price': 1, 'output_price': 1}]})
        self.service.engine.resume()

    def tearDown(self):
        self.service.engine.close()
        self.service.meta.close()
        self.network.stop()
        self.clock.stop()
        self.tmp.cleanup()

    def test_weekend_crypto_fill_then_broker_fee_is_applied_once(self):
        def model(provider, model_id, key, snapshot, **kwargs):
            if snapshot['round'] == 1:
                return response(research=[{'kind': 'quotes', 'symbols': ['BTC/USD'], 'query': '', 'lookback_days': 30}])
            evidence = next(e['id'] for e in snapshot['evidence'] if e['kind'] == 'quotes')
            return {**response(orders=[buy('BTC/USD', 25, evidence)]), 'watchlist': ['BTC/USD']}

        with patch('arena.service.autonomous_turn', side_effect=model):
            self.service.cycle()
        payload = self.broker.submit_order.call_args.args[0]
        self.assertEqual((payload['symbol'], payload['time_in_force']), ('BTC/USD', 'gtc'))
        self.assertAlmostEqual(self.service.engine.snapshot()['agents'][0]['positions']['BTC/USD']['qty'], .25)
        self.broker.holdings['BTC/USD'] -= .000625
        self.broker.fees = [{'id': '20260921000000000::fee-1', 'activity_type': 'CFEE',
                             'symbol': 'BTCUSD', 'qty': '-0.000625', 'net_amount': '0'}]
        self.assertEqual(self.service.reconcile(force=True)['reconciled'], ['openai'])
        self.assertEqual(self.service.reconcile(force=True)['reconciled'], ['openai'])
        state = self.service.engine.snapshot()
        self.assertAlmostEqual(state['agents'][0]['positions']['BTC/USD']['qty'], .249375)
        self.assertEqual(len(state['crypto_fee_ids']), 1)

    def test_existing_stock_scope_does_not_silently_enable_crypto(self):
        config = self.service.engine._get_config()
        config['asset_scope'] = 'equities'
        self.service.new_experiment(config)
        with self.assertRaises(EngineError):
            self.service.engine.register_asset('BTC/USD', self.broker.asset('BTC/USD'))

    def seed_crypto_holding(self, qty):
        self.assertEqual(self.service.reconcile(force=True)['reconciled'], ['openai'])
        self.service.engine.register_asset('BTC/USD', self.broker.asset('BTC/USD'))
        order = self.service.engine.reserve_order('openai', 'BTC/USD', 'buy', 100, qty=qty)
        fill = {'id': 'seed-crypto-order', 'client_order_id': order['client_order_id'],
                'symbol': 'BTC/USD', 'side': 'buy', 'status': 'filled',
                'filled_qty': str(qty), 'filled_avg_price': '100'}
        self.service.engine.update_order(order['id'], fill)
        self.broker.book.append(fill)
        self.broker.holdings['BTC/USD'] = qty

    def test_manual_crypto_close_works_while_stock_market_is_closed(self):
        self.seed_crypto_holding(.25)
        self.assertFalse(self.broker.clock()['is_open'])
        self.service.close_position('openai', 'BTC/USD')
        payload = self.broker.submit_order.call_args.args[0]
        self.assertEqual((payload['symbol'], payload['side'], payload['time_in_force']),
                         ('BTC/USD', 'sell', 'gtc'))
        self.assertNotIn('BTC/USD', self.service.engine.snapshot()['agents'][0]['positions'])

    def test_small_crypto_holding_reconciles_without_false_inventory_mismatch(self):
        self.seed_crypto_holding(.000000005)
        result = self.service.reconcile(force=True)
        self.assertEqual(result['reconciled'], ['openai'])
        self.assertEqual(result['errors'], {})


    def test_weekend_stock_idea_is_saved_without_stock_order(self):
        self.time = dt.datetime(2026, 9, 21, 3, 0, tzinfo=dt.timezone.utc)  # Sunday night, before Monday open
        def model(provider, model_id, key, snapshot, **kwargs):
            self.assertFalse(snapshot['market_availability']['stock_entry_window_open'])
            evidence = next(e['id'] for e in snapshot['evidence'] if e['kind'] == 'search_assets')
            return response(orders=[buy('MSFT', 20, evidence)], minutes=1440)

        with patch('arena.service.autonomous_turn', side_effect=model):
            self.service.cycle()
        self.broker.submit_order.assert_not_called()
        memory = self.service.public_state()['autonomy']['openai']
        self.assertEqual(memory['deferred_equity_ideas'][0]['symbol'], 'MSFT')
        self.assertEqual(memory['strategy']['name'], 'Independent test thesis')
        self.assertEqual(memory['status'], 'stock_ideas_deferred_until_new_session_review')
        self.assertEqual(memory['next_due'], '2026-09-21T13:45:00+00:00')
        self.assertEqual(self.service.engine.snapshot()['experiment']['status'], 'ready')

    def test_crypto_buy_can_use_remaining_capacity_with_unquoted_stock_holding(self):
        self.assertEqual(self.service.reconcile(force=True)['reconciled'], ['openai'])
        self.service.engine.register_asset('MSFT', self.broker.asset('MSFT'))
        self.service.engine.mark({'MSFT': 100}, self.time.isoformat(), 'test_stock_mark')
        order = self.service.engine.reserve_order('openai', 'MSFT', 'buy', 100, notional=20)
        fill = {'id': 'old-stock-order', 'client_order_id': order['client_order_id'], 'symbol': 'MSFT',
                'side': 'buy', 'status': 'filled', 'filled_qty': '0.2', 'filled_avg_price': '100'}
        self.service.engine.update_order(order['id'], fill)
        self.broker.book.append(fill)
        self.broker.holdings['MSFT'] = .2
        original_quotes = self.broker.quotes
        self.broker.quotes = lambda symbols: {s: q for s, q in original_quotes(symbols).items() if s.endswith('/USD')}

        def model(provider, model_id, key, snapshot, **kwargs):
            if snapshot['round'] == 1:
                return response(research=[{'kind': 'quotes', 'symbols': ['BTC/USD'], 'query': '', 'lookback_days': 30}])
            evidence = next(e['id'] for e in snapshot['evidence'] if e['kind'] == 'quotes')
            return {**response(orders=[buy('BTC/USD', 25, evidence)]), 'watchlist': ['BTC/USD', 'MSFT']}

        with patch('arena.service.autonomous_turn', side_effect=model):
            self.service.cycle()
        self.assertEqual(self.broker.submit_order.call_args.args[0]['symbol'], 'BTC/USD')
        self.assertIn('MSFT', self.service.engine.snapshot()['agents'][0]['positions'])


if __name__ == '__main__':
    unittest.main()
