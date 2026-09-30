"""Paper competition orchestration. Network calls never target a live trading host."""
from __future__ import annotations
import datetime as dt
from decimal import Decimal, ROUND_DOWN
from copy import deepcopy
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import threading
import uuid
from .core import Engine, EngineError, CRYPTO_QTY_EPS
from .adapters import AlpacaPaper, ApiError, baseline_signal, review_candidate, autonomous_turn, model_diagnostic, MAX_MODEL_OUTPUT_TOKENS, TransientApiError, AuthenticationApiError, OrderRejectedError, OrderNotFoundError, SubmissionUncertainError
from .operation_guard import OperationCanceled, ensure_current_operation, operation_canceled, accept_operation_stop_clear

UTC = dt.timezone.utc
SYMBOLS = ('SPY', 'QQQ', 'IWM')
VERSION = '0.6.1'

class QuoteUnavailable(ValueError):
    """An execution quote is unavailable; other research and read work may continue."""

class ExecutionDeferred(ValueError):
    """The current session cannot safely accept an order; retry on a later cycle."""

class UnresolvedOrder(ValueError):
    """An original submission still requires broker evidence or operator confirmation."""

def now():
    return dt.datetime.now(UTC)

def parse_time(value):
    return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))

def ny_time(value):
    # Avoid an external tzdata dependency on Windows; US rule valid since 2007.
    value = value.astimezone(UTC)
    year = value.year
    def sunday(month, nth):
        first = dt.date(year, month, 1)
        return 1 + (6 - first.weekday()) % 7 + 7 * (nth - 1)
    start = dt.datetime(year, 3, sunday(3, 2), 7, tzinfo=UTC)
    end = dt.datetime(year, 11, sunday(11, 1), 6, tzinfo=UTC)
    return value.astimezone(dt.timezone(dt.timedelta(hours=-4 if start <= value < end else -5)))

def load_env(path):
    """Minimal literal .env parser. No shell expansion, executable content or logging."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if not key.replace('_', '').isalnum() or not key[0].isalpha():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)

class Service:
    def __init__(self, data_dir, environ=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.stop_file = self.data_dir / 'STOP'
        self.env = os.environ if environ is None else environ
        self.lock = threading.RLock()
        self.control_lock = threading.RLock()
        self.cache_lock = threading.RLock()
        self.meta_lock = threading.RLock()
        self._busy = threading.Event()
        self._monitor_busy = threading.Event()
        self._control_generation = 0
        self._cache = {'experiment': None, 'autonomy': {}, 'model_spend_month': 0.0}
        self._monitor = {'last_success': None, 'last_attempt': None, 'last_error': '', 'refresh_seconds': 15}
        self._snapshot_at = now().isoformat()
        self._quote_details = {}
        self.engine = Engine(self.data_dir / 'arena.sqlite3')
        self.meta = sqlite3.connect(self.data_dir / 'service.sqlite3', check_same_thread=False)
        self.meta.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.meta.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, experiment TEXT, agent TEXT, month TEXT, cost REAL, status TEXT)')
        self.meta.commit()
        # Claude v0.4 stored this preference in service metadata. Import once;
        # false must take effect before any startup reconciliation can resume.
        legacy_preference = self._get('auto_resume')
        legacy_pending = self.engine.snapshot()['experiment'].get('legacy_restart_preference_pending', False)
        if legacy_pending or (legacy_preference is not None and not self._get('auto_resume_imported_v05', False)):
            if legacy_preference is not None and type(legacy_preference) is not bool:
                raise ValueError('Legacy restart preference must be boolean; no automatic recovery was attempted.')
            self.engine.set_auto_resume(True if legacy_preference is None else bool(legacy_preference))
            self._put('auto_resume_imported_v05', True)
        # Paid requests interrupted during a crash retain their conservative cost reservation.
        state = self.engine.snapshot()
        for row in self.meta.execute("SELECT id,agent,cost FROM calls WHERE experiment=? AND status='reserved'", (state['experiment']['id'],)).fetchall():
            self.engine.charge_model(row[1], row[2], call_id=row[0])
            self.meta.execute("UPDATE calls SET status='uncertain' WHERE id=?", (row[0],))
        self.meta.commit()
        # Gate every active account before allowing independent startup recovery.
        # A missing key or unavailable peer cannot accidentally enter a cycle.
        if state['experiment']['status'] == 'recovering':
            for agent in state['agents']:
                if not state.get('agent_controls', {}).get(agent['id'], {}).get('paused'):
                    self.engine.halt_agent(agent['id'], 'Awaiting this account startup reconciliation.', kind='transient')
        self.asset_cache = {}
        self.check_stop()
        self._refresh_cache()

    def _get(self, key, default=None):
        with self.meta_lock:
            row = self.meta.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def _put(self, key, value):
        with self.meta_lock:
            self.meta.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))
            self.meta.commit()

    def _refresh_cache(self):
        state = self.engine.snapshot()
        month = now().strftime('%Y-%m')
        with self.meta_lock:
            spend = self.meta.execute('SELECT COALESCE(SUM(cost),0) FROM calls WHERE month=?', (month,)).fetchone()[0]
            autonomy = {a['id']: self._get(f"autonomy:{state['experiment']['id']}:{a['id']}", {}) for a in state['agents']}
            for agent in state['agents']:
                autonomy[agent['id']]['exit_retries'] = self._get(self._exit_retry_key(agent['id']), {})
                autonomy[agent['id']]['last_provider_model'] = self._get(f"provider_model:{state['experiment']['id']}:{agent['id']}")
        with self.cache_lock:
            self._cache = {'experiment': state['experiment']['id'], 'autonomy': autonomy, 'model_spend_month': round(spend, 6)}
            self._snapshot_at = now().isoformat()

    def _agent_paused(self, agent_id):
        return self.engine.snapshot().get('agent_controls', {}).get(agent_id, {}).get('paused', False)

    def _stopped(self, agent_id):
        return operation_canceled(self) or self.check_stop() or self.engine.snapshot()['experiment']['status'] != 'ready' or self._agent_paused(agent_id)

    def halt_agent(self, agent_id, reason='Agent paused by operator. Existing orders and positions remain.'):
        # This lock is never held across a broker or model request.
        with self.control_lock:
            self._control_generation += 1
            self.engine.halt_agent(agent_id, reason, kind='operator')
        return self.public_state()

    def resume_agent(self, agent_id):
        with self.lock:
            with self.control_lock:
                ensure_current_operation(self)
                generation = self._control_generation
            state = self.engine.snapshot()
            if not any(a['id'] == agent_id for a in state['agents']):
                raise ValueError('Unknown agent.')
            if state['experiment']['mode'] == 'paper':
                result = self.reconcile(agent_id=agent_id, force=True)
                if agent_id not in result['reconciled']:
                    raise ValueError(result['errors'].get(agent_id, 'Reconciliation has not completed.'))
            with self.control_lock:
                ensure_current_operation(self)
                if generation != self._control_generation or self.check_stop():
                    raise ValueError('A stop was requested during reconciliation; resume was canceled.')
                self.engine.resume_agent(agent_id, require_reconciled=state['experiment']['mode'] == 'paper')
        return self.public_state()

    def _retry_due(self, agent_id):
        control = self.engine.snapshot().get('agent_controls', {}).get(agent_id, {})
        retry = control.get('recovery', {}).get('next_retry_at')
        return not retry or parse_time(retry).astimezone(UTC) <= now()

    def _isolate_failure(self, agent, error):
        reason = f"{agent['name']}: {str(error)[:600]}"
        if isinstance(error, OperationCanceled):
            # The newer operator control is authoritative; cancellation does
            # not create a fresh account-integrity incident.
            return reason
        if isinstance(error, QuoteUnavailable):
            # Missing market data cannot prove an account or ledger failure.
            self.engine.note_recovery(agent['id'], {'quote_warning': reason})
            return reason
        if isinstance(error, ExecutionDeferred):
            # A normal session boundary or stale clock is not an account failure.
            previous = self.engine.snapshot().get('agent_controls', {}).get(agent['id'], {}).get('recovery', {}).get('execution_warning')
            self.engine.note_recovery(agent['id'], {'execution_warning': reason})
            if previous != reason:
                self.engine.event(reason + ' No order was sent; execution may be retried on a later eligible cycle.', 'warning')
            return reason
        kind = 'unknown_order' if isinstance(error, (UnresolvedOrder, SubmissionUncertainError)) else (
            'transient' if isinstance(error, TransientApiError) else 'integrity')
        with self.control_lock:
            control = self.engine.snapshot().get('agent_controls', {}).get(agent['id'], {})
            # A delayed network response must never replace an operator/integrity stop.
            protected = control.get('paused') and control.get('pause_kind', 'operator') not in ('transient', 'unknown_order')
            if not protected and (not control.get('paused') or control.get('reason') != reason or control.get('pause_kind') != kind):
                self.engine.halt_agent(agent['id'], reason, kind=kind)
            attempts = control.get('recovery', {}).get('attempts', 0) + 1
            delay = min(300, 15 * 2 ** min(attempts - 1, 5))
            self.engine.note_recovery(agent['id'], {'attempts': attempts, 'next_retry_at': (now() + dt.timedelta(seconds=delay)).isoformat(), 'last_error': reason})
            if 'different dedicated paper account' in str(error):
                self.engine.halt('Duplicate broker account detected. Both agents must use separate paper accounts.')
        return reason

    def _recovery_succeeded(self, agent_id):
        with self.control_lock:
            control = self.engine.snapshot().get('agent_controls', {}).get(agent_id, {})
            if control.get('paused') and control.get('pause_kind') in ('transient', 'unknown_order'):
                self.engine.resume_agent(agent_id, require_reconciled=True, automatic=True)
            self.engine.note_recovery(agent_id, {'attempts': 0, 'next_retry_at': None, 'last_error': ''})

    def check_stop(self):
        if self.stop_file.exists():
            state = self.engine.snapshot()
            if state['experiment']['status'] != 'halted':
                self.engine.halt('External STOP file is active. Resume explicitly to clear it.')
            return True
        return False

    def halt(self, reason='Operator stopped new entries. Existing orders and positions remain.'):
        with self.control_lock:
            self._control_generation += 1
            self.stop_file.write_text(reason, encoding='utf-8')
            self.engine.halt(reason)
            self.engine.set_autopilot(False)

    def _stop_version(self):
        try:
            stamp = self.stop_file.stat()
            return (stamp.st_ino, stamp.st_size, stamp.st_mtime_ns)
        except FileNotFoundError:
            return None

    def resume(self):
        with self.lock:
            with self.control_lock:
                ensure_current_operation(self)
                generation = self._control_generation
                stop_version = self._stop_version()
            if self.engine.snapshot()['experiment']['mode'] == 'paper':
                result = self.reconcile(force=True)
                if not result['reconciled']:
                    raise ValueError('; '.join(result['errors'].values()) or 'No account reconciled.')
            with self.control_lock:
                ensure_current_operation(self)
                if generation != self._control_generation or stop_version != self._stop_version():
                    raise ValueError('A stop was requested during reconciliation; resume was canceled.')
                self.engine.resume(allow_isolated=True)
                self.stop_file.unlink(missing_ok=True)
                accept_operation_stop_clear()

    def broker(self, agent):
        prefix = 'ALPACA_' + agent['id'].upper()
        key, secret = self.env.get(prefix + '_KEY'), self.env.get(prefix + '_SECRET')
        if not key or not secret:
            raise ValueError(f"{agent['name']}: paper credentials are not configured ({prefix}_KEY / _SECRET).")
        return AlpacaPaper(key, secret, feed=self.env.get('ALPACA_DATA_FEED', 'iex'), historical_feed=self.env.get('ALPACA_HISTORY_FEED', 'sip'))

    def public_state(self, *, compact=True):
        """Fresh local ledger + cached metadata, never waiting for network work."""
        self.check_stop()
        state = self.engine.snapshot() if compact else self.engine.state()
        with self.cache_lock:
            cache = deepcopy(self._cache)
            monitor = deepcopy(self._monitor)
            metadata_at = self._snapshot_at
            quote_details = deepcopy(self._quote_details)
        state['version'] = VERSION
        state['connections'] = {}
        for agent in state['agents']:
            prefix = 'ALPACA_' + agent['id'].upper()
            provider = agent['provider']
            state['connections'][agent['id']] = {
                'alpaca': bool(self.env.get(prefix + '_KEY') and self.env.get(prefix + '_SECRET')),
                'model': provider == 'rules' or (provider in ('openai', 'anthropic') and bool(self.env.get('OPENAI_API_KEY' if provider == 'openai' else 'ANTHROPIC_API_KEY'))),
                'env_prefix': prefix,
                'model_test_estimate': self._diagnostic_estimate(agent),
            }
            control = state.get('agent_controls', {}).get(agent['id'], {})
            held = set(agent.get('positions', {}))
            stamps = control.get('quote_times', {})
            missing = sorted(held - set(stamps))
            control['quote_details'] = {symbol: quote_details.get(agent['id'], {}).get(symbol, {}) for symbol in held}
            control['quote_missing'] = missing
            control['last_quote_at'] = min((stamps[symbol] for symbol in held), default=None) if not missing else None
            own_orders = [o for o in state['orders'] if o['agent_id'] == agent['id']]
            agent.setdefault('trade_count', sum(o['filled_qty'] > 0 for o in own_orders))
            agent['working_order_count'] = sum(o['status'] not in ('filled', 'canceled', 'expired', 'rejected') for o in own_orders)
            agent['target_equity'] = state['experiment']['target'] * agent['allocation'] / state['experiment']['total_capital']
        state['model_spend_month'] = cache['model_spend_month']
        state['autonomy'] = cache['autonomy'] if cache['experiment'] == state['experiment']['id'] else {}
        state['server_time'] = now().isoformat()
        state['snapshot_at'] = max(state.get('snapshot_at', ''), metadata_at)
        monitor['busy'] = self._busy.is_set() or self._monitor_busy.is_set()
        state['monitor'] = monitor
        state['limitations'] = ['Paper only; synthetic replay is not a backtest.', 'Prospective performance is inconclusive; taxes are not calculated.', 'One distinct clean Alpaca paper account per agent; no live broker endpoint.', 'Only USD crypto pairs and US equities are supported; options and forex orders are excluded.', 'Provider/model cost estimates are not a billing guarantee.', 'The combined experiment loss limit can stop both agents.', 'Pausing an agent stops its automatic exits too; working orders are not canceled.', 'Quotes and broker state can be stale during research or outages; inspect timestamps.']
        if compact:
            limits = {'history': 1000, 'decisions': 100, 'events': 200, 'closed_orders': 200}
            counts = {key: state.get('audit_counts', {}).get(key, len(state[key])) for key in ('history', 'decisions', 'events', 'orders')}
            if len(state['history']) > limits['history']:
                history = state['history']
                last = len(history) - 1
                state['history'] = [history[index * last // (limits['history'] - 1)] for index in range(limits['history'])]
            decision_fields = ('id', 'agent_id', 'at', 'action', 'symbol', 'reason', 'status')
            state['decisions'] = [{key: record[key] for key in decision_fields if key in record}
                                  for record in state['decisions'][-limits['decisions']:]]
            state['events'] = state['events'][-limits['events']:]
            terminal = {'filled', 'canceled', 'expired', 'rejected'}
            closed = [o for o in state['orders'] if o['status'] in terminal]
            keep_closed = {o['id'] for o in closed[-limits['closed_orders']:]}
            state['orders'] = [o for o in state['orders'] if o['status'] not in terminal or o['id'] in keep_closed]
            state['view_limits'] = {**limits, 'full_counts': counts, 'truncated': {key: len(state[key]) < counts[key] for key in counts}, 'full_audit_available': True}
        else:
            state['view_limits'] = {'compact': False, 'full_audit_available': True}
        return state

    def state(self):
        return self.public_state(compact=False)

    def new_experiment(self, config):
        with self.lock, self.control_lock:
            ensure_current_operation(self)
            state = self.engine.snapshot()
            if state['experiment']['mode'] == 'paper' and any(a.get('positions') for a in state['agents']):
                raise ValueError('Close and reconcile existing paper positions before starting another experiment.')
            self.engine.new_experiment(config)
            with self.cache_lock:
                self._quote_details = {}
            self._refresh_cache()
            if self.stop_file.exists():
                self.engine.halt('External STOP remains active. Resume explicitly to clear it.')

    def _reconcile_agent(self, agent, state):
        broker = self.broker(agent)
        pending_before = [order for order in self.engine.pending_orders() if order['agent_id'] == agent['id']]
        account = broker.account()
        if account.get('status') != 'ACTIVE' or account.get('trading_blocked') or account.get('account_blocked'):
            raise ValueError('Paper account is not active for trading.')
        bind_key = f"bound:{state['experiment']['id']}:{agent['id']}"
        bound = self._get(bind_key)
        terminal = {'filled', 'canceled', 'expired', 'rejected', 'replaced'}
        if not bound:
            broker_positions = broker.positions()
            broker_orders = broker.orders(status='all')
            if broker_positions or any(o.get('status') not in terminal for o in broker_orders):
                raise ValueError('Use a dedicated paper account with no positions or open orders.')
            self.engine.attach_account(agent['id'], account['id'])
            bound = {'at': now().isoformat(), 'account': account['id'],
                     'order_cursor': broker_orders[-1]['id'] if broker_orders else None}
            self._put(bind_key, bound)
            broker_orders = []
        else:
            self.engine.attach_account(agent['id'], account['id'])
            cursor = bound.get('order_cursor')
            recent = broker.orders(status='all', after_order_id=cursor) if cursor else broker.orders(status='all')
            # A creation-order cursor cannot show changes to an old working order.
            # Open orders and every pending local client ID cover that lifecycle.
            open_orders = broker.orders(status='open')
            broker_orders = list({item['id']: item for item in recent + open_orders}.values())
            next_cursor = recent[-1]['id'] if recent else cursor
        seen = set()
        prior_orders = set(bound.get('prior_orders', []))  # upgrade pre-0.4 bindings
        legacy_boundary = bound.get('orders_after') if not bound.get('order_cursor') else None
        if legacy_boundary:
            try:
                legacy_boundary = parse_time(legacy_boundary).astimezone(UTC)
            except (TypeError, ValueError, OverflowError):
                raise ValueError('Legacy broker binding boundary is invalid.') from None
        for item in broker_orders:
            client_id = item.get('client_order_id')
            known = self.engine.order_by_client_id(client_id) if client_id else None
            if known and known['agent_id'] == agent['id']:
                self.engine.update_order(known['id'], item)
                seen.add(client_id)
            elif item.get('id') not in prior_orders:
                # Claude bindings kept only a short initial order window. The
                # complete first scan can safely ignore older terminal records,
                # but never an open, local, or post-binding order.
                if legacy_boundary and item.get('status') in terminal:
                    try:
                        created = parse_time(item['submitted_at']).astimezone(UTC)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        raise ValueError('Legacy broker order has no valid submission timestamp.') from None
                    if created < legacy_boundary:
                        continue
                raise ValueError('Unrecognized paper-account order. Manual activity requires investigation.')
        for order in self.engine.pending_orders():
            if order['agent_id'] != agent['id'] or order['client_order_id'] in seen:
                continue
            # Read the ORIGINAL identifier. Neither a 404 nor a timeout proves
            # that a submission was rejected. Never create a replacement order.
            try:
                item = broker.order_by_client_id(order['client_order_id'])
                self.engine.update_order(order['id'], item)
            except OrderNotFoundError:
                self.engine.unknown_order(order['id'], 'Original client ID is not yet visible. Read-only recovery continues; no replacement order is sent.', halt=False)
                raise UnresolvedOrder('An order remains unconfirmed. A broker 404 alone cannot prove rejection, including for pre-0.4 unknown orders.') from None
            except (ApiError, ValueError) as exc:
                self.engine.unknown_order(order['id'], 'Order read failed; reservation retained until the original client ID is confirmed.', halt=False)
                raise UnresolvedOrder('An order is unresolved. Read-only recovery will retry; no replacement order has been sent.') from exc
        if any(o['agent_id'] == agent['id'] and o['symbol'].endswith('/USD') and o['filled_qty'] > 0
               for o in self.engine.orders(agent['id'])):
            # Broker fees can post after the trade date. Apply only explicit
            # activities, with exactly-once IDs journaled in the core ledger.
            for activity in broker.crypto_fees(bound['at']):
                self.engine.apply_crypto_fee(agent['id'], activity)
        def inventory():
            local = next(a for a in self.engine.snapshot()['agents'] if a['id'] == agent['id'])
            positions = broker.positions()
            if any(not math.isfinite(float(position['qty'])) for position in positions):
                raise ValueError('Broker inventory contains a nonfinite quantity.')
            remote_qty = {}
            for position in positions:
                symbol = str(position['symbol'])
                if '/' not in symbol and symbol.endswith('USD') and (position.get('asset_class') == 'crypto' or symbol[:-3] + '/USD' in local['positions']):
                    symbol = symbol[:-3] + '/USD'
                if abs(float(position['qty'])) > (CRYPTO_QTY_EPS if symbol.endswith('/USD') else 1e-9):
                    if symbol in remote_qty:
                        raise ValueError('Broker inventory contains duplicate symbols.')
                    remote_qty[symbol] = float(position['qty'])
            local_qty = {symbol: float(position['qty']) for symbol, position in local['positions'].items()
                         if abs(float(position['qty'])) > (CRYPTO_QTY_EPS if symbol.endswith('/USD') else 1e-8)}
            mismatch = set(remote_qty) != set(local_qty) or any(abs(remote_qty[symbol] - local_qty[symbol]) > (CRYPTO_QTY_EPS if symbol.endswith('/USD') else 1e-6) for symbol in local_qty if symbol in remote_qty)
            return local, mismatch

        fresh, mismatch = inventory()
        if mismatch and pending_before:
            # Alpaca does not expose an atomic orders+positions snapshot. A fill
            # can land between these reads. Refresh the original IDs once before
            # deciding whether this is an external ledger change.
            for order in pending_before:
                try:
                    item = broker.order_by_client_id(order['client_order_id'])
                except OrderNotFoundError:
                    current_order = self.engine.order_by_client_id(order['client_order_id'])
                    if current_order and current_order['status'] not in terminal:
                        self.engine.unknown_order(order['id'], 'Order disappeared during fill reconciliation; original ID remains reserved.', halt=False)
                        raise UnresolvedOrder('Order could not be confirmed during inventory reconciliation.') from None
                    raise TransientApiError('Broker lookup temporarily omitted a confirmed terminal order during inventory refresh.') from None
                self.engine.update_order(order['id'], item)
            fresh, mismatch = inventory()
        if mismatch:
            if any(order['agent_id'] == agent['id'] for order in self.engine.pending_orders()):
                raise TransientApiError('Order and position snapshots differ while an order is pending. Entries remain paused while read-only reconciliation retries.')
            raise ValueError('Paper inventory differs from the local ledger. Reconcile manually; entries are halted.')
        account = broker.account()
        broker_cash = float(account.get('cash', '0'))
        if not math.isfinite(broker_cash):
            raise ValueError('Broker cash balance is not finite.')
        if broker_cash + .02 < fresh['cash']:
            if any(order['agent_id'] == agent['id'] for order in self.engine.pending_orders()):
                raise TransientApiError('Broker cash changed while an order is pending. Read-only reconciliation will refresh fills before permitting entries.')
            raise ValueError('Broker cash is below the assigned virtual cash balance.')
        if broker_orders:
            bound['order_cursor'] = next_cursor
            bound.pop('prior_orders', None)
            bound.pop('orders_after', None)
            self._put(bind_key, bound)
        self.engine.note_reconciled(agent['id'], now().isoformat())
        self._recovery_succeeded(agent['id'])

    def _finish_startup_recovery(self, reconciled):
        state = self.engine.snapshot()
        if state['experiment']['status'] != 'recovering' or self.check_stop():
            return
        # Unverified accounts remain independently paused. A freshly verified
        # healthy account may run while an unavailable peer stays gated.
        key = f"startup:{state['experiment']['id']}"
        verified = getattr(self, '_startup_verified', {}).get(key, set()) | set(reconciled)
        self._startup_verified = {key: verified}
        active = [agent['id'] for agent in state['agents'] if not state['agent_controls'][agent['id']].get('paused')]
        if verified and all(agent_id in verified for agent_id in active):
            with self.control_lock:
                fresh = self.engine.snapshot()['experiment']
                if not self.check_stop() and fresh.get('auto_resume', True) and fresh.get('autopilot') and fresh['status'] == 'recovering':
                    # With no active accounts, the core deliberately requires
                    # all accounts to be checked before leaving recovery.
                    if active or len(verified) == len(state['agents']):
                        self.engine.recover_startup(allow_isolated=True)

    def reconcile(self, agent_id=None, force=False):
        """Update each ledger independently; recovery performs reads only."""
        with self.lock:
            state = self.engine.snapshot()
            if state['experiment']['mode'] != 'paper':
                raise ValueError('Reconciliation is available in Alpaca paper mode.')
            selected = [a for a in state['agents'] if agent_id is None or a['id'] == agent_id]
            if not selected:
                raise ValueError('Unknown agent.')
            result = {'reconciled': [], 'errors': {}, 'deferred': []}
            for agent in selected:
                prefix = 'ALPACA_' + agent['id'].upper()
                if not self.env.get(prefix + '_KEY') or not self.env.get(prefix + '_SECRET'):
                    if agent_id is not None:
                        result['errors'][agent['id']] = self._isolate_failure(agent, ValueError('Paper credentials are not configured.'))
                    continue
                if not force and not self._retry_due(agent['id']):
                    result['deferred'].append(agent['id'])
                    continue
                try:
                    self._reconcile_agent(agent, state)
                    result['reconciled'].append(agent['id'])
                except (ApiError, ValueError, KeyError, TypeError, OverflowError) as exc:
                    result['errors'][agent['id']] = self._isolate_failure(agent, exc)
            self._finish_startup_recovery(result['reconciled'])
            return result

    def _monitor_held_quotes(self, agent, experiment_id=None):
        captured = self.engine.snapshot()
        experiment_id = experiment_id or captured['experiment']['id']
        if captured['experiment']['id'] != experiment_id:
            return False
        current = next((a for a in captured['agents'] if a['id'] == agent['id']), None)
        if current is None:
            return False
        symbols = list(current['positions'])
        if not symbols:
            return True
        broker = self.broker(agent)
        quotes = broker.quotes(symbols)
        checked = []
        metadata = {}
        quote_times = {}
        for symbol in symbols:
            item = quotes.get(symbol)
            try:
                price = float(item['price'])
                stamp = parse_time(item['t']).astimezone(UTC)
                if not math.isfinite(price) or price <= 0 or stamp > now() + dt.timedelta(seconds=5):
                    continue
            except (TypeError, KeyError, ValueError, OverflowError):
                continue
            if symbol not in SYMBOLS and symbol not in captured['asset_registry']:
                metadata[symbol] = broker.asset(symbol)
            checked.append((symbol, price, stamp.isoformat()))
            quote_times[symbol] = stamp.isoformat()
        # Network work is finished. Check and apply under the same short engine
        # lock so a new experiment cannot inherit an old experiment's quotes.
        with self.engine.lock:
            if self.engine.snapshot()['experiment']['id'] != experiment_id:
                return False
            for symbol, asset in metadata.items():
                self.engine.register_asset(symbol, asset)
            for symbol, price, stamp in checked:
                self.engine.mark({symbol: price}, stamp, self._mark_source(quotes[symbol], broker))
            self.engine.note_quotes(agent['id'], quote_times)
            with self.cache_lock:
                self._quote_details[agent['id']] = {symbol: {key: quotes[symbol].get(key) for key in ('t', 'feed', 'price_source', 'price_quality', 'execution_eligible')} for symbol in quote_times}
            missing = [symbol for symbol in symbols if symbol not in quote_times]
            stale = [symbol for symbol, stamp in quote_times.items() if (now() - parse_time(stamp)).total_seconds() > 120]
            warning = ('Held quotes unavailable or stale: ' + ', '.join(sorted(set(missing + stale)))) if missing or stale else ''
            self.engine.note_recovery(agent['id'], {'quote_warning': warning})
        return True

    def _monitor_quotes_while_busy(self):
        # Never access metadata SQLite here: the active research worker owns it.
        state = self.engine.snapshot()
        result = {'skipped': True, 'quote_only': True, 'reason': 'Cycle busy; fill reconciliation deferred.', 'refreshed': [], 'errors': {}}
        if state['experiment']['mode'] != 'paper':
            return result
        for agent in state['agents']:
            prefix = 'ALPACA_' + agent['id'].upper()
            if not agent['positions'] or not self.env.get(prefix + '_KEY') or not self.env.get(prefix + '_SECRET'):
                continue
            try:
                if not self._monitor_held_quotes(agent, experiment_id=state['experiment']['id']):
                    result['experiment_changed'] = True
                    return result
                result['refreshed'].append(agent['id'])
            except (ApiError, ValueError, KeyError, TypeError, OverflowError) as exc:
                with self.engine.lock:
                    if self.engine.snapshot()['experiment']['id'] != state['experiment']['id']:
                        result['experiment_changed'] = True
                        return result
                    result['errors'][agent['id']] = str(exc)[:600]
                    self.engine.note_recovery(agent['id'], {'quote_warning': str(exc)[:600]})
        with self.cache_lock:
            self._monitor['last_attempt'] = now().isoformat()
            self._monitor['reconciliation_deferred'] = True
            self._monitor['last_error'] = '; '.join(result['errors'].values())[:1000]
            if result['refreshed']:
                self._monitor['last_quote_refresh'] = now().isoformat()
        return result

    def monitor(self):
        """Observe accounts and held quotes only. No model requests or orders."""
        if not self.lock.acquire(blocking=False):
            return self._monitor_quotes_while_busy()
        self._monitor_busy.set()
        errors = {}
        successes = []
        try:
            with self.cache_lock:
                self._monitor['last_attempt'] = now().isoformat()
                self._monitor['reconciliation_deferred'] = False
            state = self.engine.snapshot()
            if state['experiment']['mode'] != 'paper':
                return {'skipped': True, 'reason': 'Synthetic demo has no broker monitor.'}
            for agent in state['agents']:
                prefix = 'ALPACA_' + agent['id'].upper()
                if not self.env.get(prefix + '_KEY') or not self.env.get(prefix + '_SECRET') or not self._retry_due(agent['id']):
                    continue
                try:
                    self._reconcile_agent(agent, state)
                    successes.append(agent['id'])
                except (ApiError, ValueError, KeyError, TypeError, OverflowError) as exc:
                    errors[agent['id']] = self._isolate_failure(agent, exc)
                    continue
                try:
                    self._monitor_held_quotes(agent)
                except (ApiError, ValueError, KeyError, TypeError, OverflowError) as exc:
                    # Observation failures keep the last timestamped valuation.
                    # They do not cancel successful account reconciliation.
                    errors[agent['id']] = str(exc)[:600]
                    self.engine.note_recovery(agent['id'], {'quote_warning': str(exc)[:600]})
            self._finish_startup_recovery(successes)
            with self.cache_lock:
                if successes:
                    self._monitor['last_success'] = now().isoformat()
                self._monitor['last_error'] = '; '.join(errors.values())[:1000]
                self._snapshot_at = now().isoformat()
            return {'skipped': False, 'reconciled': successes, 'errors': errors}
        finally:
            self._monitor_busy.clear()
            self.lock.release()

    def set_auto_resume(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be true or false.')
        with self.control_lock:
            if enabled:
                ensure_current_operation(self)
            # An opt-out made during network recovery wins over its late result.
            self._control_generation += 1
            self.engine.set_auto_resume(enabled)
        self._refresh_cache()
        return self.public_state()

    def _diagnostic_estimate(self, agent):
        output_limit = 1024
        ip, op = float(agent.get('input_price', 0)), float(agent.get('output_price', 0))
        return {'reserved_usd': math.ceil(4000 * ip + output_limit * op) / 1_000_000,
                'input_token_bound': 4000, 'max_output_tokens': output_limit,
                'note': 'Conservative reservation using configured rates; provider billing can differ.'}

    def _paid_request(self, agent, request, *, input_bound, output_limit):
        """Reserve both budgets before the request; uncertain calls retain cost."""
        ensure_current_operation(self)
        if self._stopped(agent['id']):
            raise ValueError('Automation is stopped for this agent.')
        problem = self._model_availability(agent)
        if problem:
            raise ValueError(problem)
        state = self.engine.snapshot()
        exp = state['experiment']
        month = now().strftime('%Y-%m')
        ip, op = float(agent['input_price']), float(agent['output_price'])
        reserve = math.ceil(input_bound * ip + output_limit * op) / 1_000_000
        ai_weight = sum(a['weight'] for a in state['agents'] if a['provider'] in ('openai', 'anthropic'))
        allowance = exp['monthly_model_budget'] * agent['weight'] / ai_weight
        with self.control_lock, self.meta_lock:
            ensure_current_operation(self)
            total_used = self.meta.execute('SELECT COALESCE(SUM(cost),0) FROM calls WHERE month=?', (month,)).fetchone()[0]
            agent_used = self.meta.execute('SELECT COALESCE(SUM(cost),0) FROM calls WHERE month=? AND agent=?', (month, agent['id'])).fetchone()[0]
            if total_used + reserve > exp['monthly_model_budget'] or agent_used + reserve > allowance:
                raise ValueError('Agent or experiment monthly model budget prevents this request.')
            call_id = uuid.uuid4().hex
            self.meta.execute('INSERT INTO calls VALUES (?,?,?,?,?,?)', (call_id, exp['id'], agent['id'], month, reserve, 'reserved'))
            self.meta.commit()
        self._refresh_cache()
        if self._stopped(agent['id']):
            with self.meta_lock:
                self.meta.execute("UPDATE calls SET cost=0,status='canceled' WHERE id=?", (call_id,))
                self.meta.commit()
            self._refresh_cache()
            raise ValueError('Agent paused before provider request.')
        try:
            result = deepcopy(request())
            cost = (result['input_tokens'] * ip + result['output_tokens'] * op) / 1_000_000
            if not math.isfinite(cost) or cost < 0:
                raise ValueError('Invalid provider usage.')
        except Exception:
            # Persist the engine charge before settling metadata. Replay by call
            # ID is idempotent if a crash interrupts these two durable stores.
            self.engine.charge_model(agent['id'], reserve, call_id=call_id)
            with self.meta_lock:
                self.meta.execute("UPDATE calls SET status='uncertain' WHERE id=?", (call_id,))
                self.meta.commit()
            self._refresh_cache()
            raise ValueError('Provider call failed; no retry was made and its reserved cost was retained.') from None
        self.engine.charge_model(agent['id'], cost, call_id=call_id)
        with self.meta_lock:
            self.meta.execute("UPDATE calls SET cost=?,status='complete' WHERE id=?", (cost, call_id))
            self.meta.commit()
        if result.get('model'):
            self._put(f"provider_model:{exp['id']}:{agent['id']}", str(result['model'])[:200])
        result['estimated_cost'] = cost
        self._refresh_cache()
        return result

    def test_connections(self, paid_model=False):
        """Bounded operator diagnostics; never submit, cancel, or adopt orders."""
        if type(paid_model) is not bool:
            raise ValueError('paid_model must be true or false.')
        with self.lock:
            self._busy.set()
            try:
                state = self.engine.snapshot()
                if state['experiment']['mode'] != 'paper':
                    raise ValueError('Connection checks require an Alpaca paper experiment.')
                result = {'paid_model': paid_model, 'at': now().isoformat(), 'checks': []}
                for agent in state['agents']:
                    row = {'agent_id': agent['id'], 'name': agent['name'], 'estimate': self._diagnostic_estimate(agent)}
                    try:
                        broker = self.broker(agent)
                        account, clock = broker.account(), broker.clock()
                        if account.get('status') != 'ACTIVE' or account.get('trading_blocked') or account.get('account_blocked'):
                            raise ValueError('Account not available for paper trading.')
                        stamp = parse_time(clock['timestamp']).astimezone(UTC)
                        if abs((now() - stamp).total_seconds()) > 120:
                            raise ValueError('Broker clock is stale.')
                        row['broker'] = {'status': 'ok', 'message': 'Paper account and broker clock responded; no orders sent.',
                                         'market_open': bool(clock.get('is_open'))}
                    except Exception:
                        # Never echo upstream errors, credentials, or account IDs.
                        row['broker'] = {'status': 'error', 'message': 'Paper account or broker clock check failed. Check server credentials and broker status.'}
                    row['market_data'] = {'status': 'unavailable', 'message': 'Sample price was not available. Account connectivity does not establish data readiness.'}
                    if row['broker']['status'] == 'ok':
                        try:
                            quote = broker.quotes(['SPY']).get('SPY')
                            stamp = parse_time(quote['t']).astimezone(UTC)
                            age = (now() - stamp).total_seconds()
                            if not math.isfinite(float(quote['price'])) or float(quote['price']) <= 0 or age < -5:
                                raise ValueError('Invalid sample price.')
                            row['market_data'] = {'status': 'ok' if age <= 120 else 'stale',
                                                   'message': 'Sample valuation mark received; each order still needs its own fresh executable quote.',
                                                   'age_seconds': round(age, 3), 'feed': quote.get('feed', getattr(broker, 'feed', 'iex')),
                                                   'source': quote.get('price_source', 'quote'), 'execution_eligible': quote.get('execution_eligible', age <= 120)}
                        except Exception:
                            pass
                    row['broker']['sample_mark_available'] = row['market_data']['status'] in ('ok', 'stale')
                    if state['experiment'].get('asset_scope') == 'equities_crypto' and row['broker']['status'] == 'ok':
                        row['crypto_data'] = {'status': 'unavailable', 'message': 'BTC/USD paper data unavailable; no crypto order was sent.'}
                        try:
                            item = broker.quotes(['BTC/USD']).get('BTC/USD')
                            age = (now() - parse_time(item['t']).astimezone(UTC)).total_seconds()
                            row['crypto_data'] = {'status': 'ok' if item.get('execution_eligible') and -5 <= age <= 120 else 'stale',
                                                  'message': 'BTC/USD sample received; each crypto order still requires its own fresh quote.',
                                                  'age_seconds': round(age, 3), 'feed': item.get('feed', 'crypto_us'),
                                                  'execution_eligible': bool(item.get('execution_eligible'))}
                        except Exception:
                            pass
                    row['model'] = {'status': 'skipped', 'message': 'Paid model check was not requested.'}
                    if paid_model:
                        if self._stopped(agent['id']):
                            row['model'] = {'status': 'blocked', 'message': 'Experiment or agent is stopped. No paid call made.'}
                        elif agent['provider'] not in ('openai', 'anthropic'):
                            row['model'] = {'status': 'skipped', 'message': 'This agent has no paid model provider.'}
                        else:
                            try:
                                key = self.env.get('OPENAI_API_KEY' if agent['provider'] == 'openai' else 'ANTHROPIC_API_KEY')
                                reply = self._paid_request(agent, lambda: model_diagnostic(agent['provider'], agent['model'], key, max_output_tokens=1024), input_bound=4000, output_limit=1024)
                                row['model'] = {'status': 'ok', 'message': 'Structured model response accepted.',
                                                'model': agent['model'], 'actual_model': reply.get('model'), 'cost_usd': reply['estimated_cost']}
                            except ValueError as exc:
                                # _paid_request emits only fixed local messages.
                                row['model'] = {'status': 'blocked' if 'budget' in str(exc) or 'stopped' in str(exc) or 'paused' in str(exc) else 'error',
                                                'message': str(exc)}
                    result['checks'].append(row)
                return result
            finally:
                self._refresh_cache()
                self._busy.clear()

    def _review(self, agent, snapshot):
        if self._stopped(agent['id']):
            return {'approve': False, 'reason': 'Experiment or agent is stopped.', 'status': 'halted'}
        problem = self._model_availability(agent)
        if problem:
            return {'approve': False, 'reason': problem, 'status': 'budget_blocked' if 'budget' in problem or 'pricing' in problem else 'disconnected'}
        if len(json.dumps(snapshot).encode('utf-8')) > 12000:
            raise ValueError('Review snapshot exceeds token cost reservation size.')
        output_limit = self.engine.snapshot()['experiment'].get('output_token_limit', MAX_MODEL_OUTPUT_TOKENS)
        key = self.env['OPENAI_API_KEY' if agent['provider'] == 'openai' else 'ANTHROPIC_API_KEY']
        kwargs = {} if output_limit == MAX_MODEL_OUTPUT_TOKENS else {'max_output_tokens': output_limit}
        try:
            return self._paid_request(agent, lambda: review_candidate(agent['provider'], agent['model'], key, snapshot, **kwargs), input_bound=16000, output_limit=output_limit)
        except ValueError as exc:
            return {'approve': False, 'reason': str(exc), 'status': 'budget_blocked' if 'budget' in str(exc) else 'provider_error'}

    def _entry_capacity(self, agent_id, symbol, price):
        state = self.engine.snapshot()
        if any(o['agent_id'] == agent_id and o['symbol'] == symbol for o in self.engine.pending_orders()):
            return 0
        agent = next(a for a in state['agents'] if a['id'] == agent_id)
        exp = state['experiment']
        trading_equity = agent.get('trading_equity', agent['equity'])
        capital = trading_equity if exp.get('compound_profits', False) else min(agent['allocation'], trading_equity)
        existing = agent['positions'].get(symbol, {}).get('qty', 0) * price
        exposure = sum(p['qty'] * state['market']['prices'].get(s, p['avg_price']) for s, p in agent['positions'].items())
        exposure += sum(o['reserved'] / (1 + exp['fee_bps'] / 10000) for o in self.engine.pending_orders() if o['agent_id'] == agent_id and o['side'] == 'buy')
        return max(0, min(capital * exp['position_cap_pct'] / 100 - existing, capital * exp['exposure_cap_pct'] / 100 - exposure, agent.get('available_cash', agent['cash'])) * .98)

    @staticmethod
    def _mark_source(quote, broker, *, execution=False):
        source = 'quote_execution' if execution else str(quote.get('price_source', 'quote'))
        return 'alpaca_' + str(quote.get('feed') or getattr(broker, 'feed', 'iex')) + ':' + source

    def _exit_retry_key(self, agent_id):
        return f"exit_retries:{self.engine.snapshot()['experiment']['id']}:{agent_id}"

    def _exit_retry_blocked(self, agent_id, symbol):
        record = self._get(self._exit_retry_key(agent_id), {}).get(symbol, {})
        return bool(record.get('next_retry_at') and parse_time(record['next_retry_at']).astimezone(UTC) > now())

    def _note_exit_rejection(self, agent_id, symbol, error):
        key = self._exit_retry_key(agent_id)
        records = self._get(key, {})
        prior = records.get(symbol, {})
        message = str(error).lower()
        reason = 'inventory_or_funds' if any(term in message for term in ('insufficient', 'quantity', 'balance')) else 'order_validation'
        attempts = min(1000000, prior.get('attempts', 0) + 1) if prior.get('reason') == reason else 1
        base = 30 if reason == 'inventory_or_funds' else 300
        delay = min(900, base * 2 ** min(attempts - 1, 5))
        records[symbol] = {'attempts': attempts, 'reason': reason, 'http_status': error.status_code,
                           'last_rejected_at': now().isoformat(), 'next_retry_at': (now() + dt.timedelta(seconds=delay)).isoformat(),
                           'message': 'Broker rejected the automatic exit. Position remains open; read-only reconciliation continues.'}
        self._put(key, records)
        self._refresh_cache()

    def _clear_exit_retry(self, agent_id, symbol):
        key = self._exit_retry_key(agent_id)
        records = self._get(key, {})
        if symbol in records:
            del records[symbol]
            self._put(key, records)
            self._refresh_cache()

    @staticmethod
    def _check_execution_session(clock, side, *, crypto=False):
        checked_at = now()
        stamp = parse_time(clock['timestamp']).astimezone(UTC)
        if abs((checked_at - stamp).total_seconds()) > 120:
            raise ExecutionDeferred('Fresh broker clock is unavailable.')
        if crypto:
            return  # Alpaca spot crypto is available outside stock sessions.
        if not clock.get('is_open'):
            raise ExecutionDeferred('Fresh broker clock does not confirm an open stock market.')
        # Use the current time again after quote/asset reads, rather than the
        # earlier clock timestamp, so slow reads cannot cross an entry boundary.
        local = ny_time(checked_at)
        if side == 'buy' and not ((9, 45) <= (local.hour, local.minute) < (15, 45)):
            raise ExecutionDeferred('Outside the entry window.')
        if clock.get('next_close') and (parse_time(clock['next_close']).astimezone(UTC) - checked_at).total_seconds() < 90:
            raise ExecutionDeferred('Too close to the session close to submit a new paper order safely.')

    def _submit(self, agent, symbol, side, price, decision_id=None, *, notional=None, qty=None, automatic=False):
        ensure_current_operation(self)
        if side == 'sell' and automatic and self._exit_retry_blocked(agent['id'], symbol):
            return
        if self._agent_paused(agent['id']):
            raise ValueError('This agent is paused.')
        if (self.check_stop() or self.engine.snapshot()['experiment']['status'] != 'ready') and (side == 'buy' or automatic):
            raise ValueError('Automation is paused or halted.')
        broker = self.broker(agent)
        clock = broker.clock()
        crypto = symbol.endswith('/USD')
        self._check_execution_session(clock, side, crypto=crypto)
        quote = broker.quotes([symbol]).get(symbol)
        if not quote or quote.get('execution_eligible') is False:
            raise QuoteUnavailable(f'{symbol}: fresh execution quote unavailable.')
        execution_at = quote.get('execution_timestamp', quote['t'])
        age = (now() - parse_time(execution_at).astimezone(UTC)).total_seconds()
        price = float(quote.get('execution_price', quote['price']))
        if not math.isfinite(price) or price <= 0 or not -5 <= age <= 120 or quote.get('execution_eligible') is False:
            raise QuoteUnavailable('Fresh executable quote unavailable; order not submitted.')
        asset = broker.asset(symbol)
        if symbol in SYMBOLS:
            if not asset.get('tradable') or not asset.get('fractionable') or asset.get('asset_class', asset.get('class', 'us_equity')) != 'us_equity':
                raise ValueError(f'{symbol} is not an eligible equity for fractional paper orders.')
        else:
            self.engine.register_asset(symbol, asset)
        self.engine.mark({symbol: price}, execution_at, self._mark_source(quote, broker, execution=True))
        self.engine.note_quotes(agent['id'], {symbol: execution_at})
        if (now() - parse_time(execution_at).astimezone(UTC)).total_seconds() > 120:
            raise QuoteUnavailable('Quote became stale while checking asset eligibility.')
        self._check_execution_session(clock, side, crypto=crypto)
        state = self.engine.snapshot()
        exp = state['experiment']
        current = next(a for a in state['agents'] if a['id'] == agent['id'])
        if side == 'buy':
            # Small headroom for fee estimates and quote movement; no borrowing.
            amount = notional if notional is not None else math.floor(self._entry_capacity(agent['id'], symbol, price) * 100) / 100
            if amount < 1:
                return
            with self.control_lock:
                ensure_current_operation(self)
                order = self.engine.reserve_order(agent['id'], symbol, side, price, notional=amount, decision_id=decision_id)
        else:
            qty = float(Decimal(str(qty if qty is not None else current['positions'].get(symbol, {}).get('qty', 0))).quantize(Decimal('0.000000001'), rounding=ROUND_DOWN))
            if qty <= 0:
                return
            with self.control_lock:
                ensure_current_operation(self)
                order = self.engine.reserve_order(agent['id'], symbol, side, price, qty=qty, decision_id=decision_id)
        payload = {'symbol': symbol, 'side': side, 'type': 'market', 'time_in_force': 'gtc' if crypto else 'day', 'client_order_id': order['client_order_id']}
        payload['notional' if side == 'buy' else 'qty'] = format(Decimal(str(order['notional'] if side == 'buy' else order['qty'])), 'f')
        try:
            if self._agent_paused(agent['id']) or ((side == 'buy' or automatic) and (self.check_stop() or self.engine.snapshot()['experiment']['status'] != 'ready')):
                self.engine.update_order(order['id'], {'status': 'rejected', 'filled_qty': '0'})
                return
            self._check_execution_session(clock, side, crypto=crypto)
            ensure_current_operation(self)
            result = broker.submit_order(payload)
            self.engine.update_order(order['id'], result)
            self.engine.note_recovery(agent['id'], {'execution_warning': ''})
            if side == 'sell':
                if result.get('status') == 'rejected' and automatic:
                    self._note_exit_rejection(agent['id'], symbol, OrderRejectedError('Broker acknowledged a rejected order.', status_code=200))
                else:
                    self._clear_exit_retry(agent['id'], symbol)
            return order
        except (ExecutionDeferred, OperationCanceled):
            # This local gate runs before POST, so this reservation can safely
            # be released. A timeout after POST still follows the unknown path.
            self.engine.update_order(order['id'], {'status': 'rejected', 'filled_qty': '0'})
            raise
        except OrderRejectedError as exc:
            self.engine.update_order(order['id'], {'status': 'rejected', 'filled_qty': '0'})
            warning_key = f"rejection_warning:{state['experiment']['id']}:{agent['id']}:{symbol}:{side}"
            warning = f"{exc.status_code}:{str(exc)}"
            if self._get(warning_key) != warning:
                self.engine.event(f"{agent['id']}: {symbol} paper {side} rejected by broker (HTTP {exc.status_code}); reservation released.", 'warning')
                self._put(warning_key, warning)
            if side == 'sell' and automatic:
                self._note_exit_rejection(agent['id'], symbol, exc)
            # Alpaca documents POST /orders 403 as insufficient buying power or
            # shares. Definitive order refusals do not establish auth failure.
            # Read-endpoint 403 remains an AuthenticationApiError in the adapter.
            if exc.status_code == 401:
                self._isolate_failure(agent, AuthenticationApiError('Broker rejected the order credentials or permissions.', status_code=exc.status_code))
            return order
        except (ApiError, ValueError):
            self.engine.unknown_order(order['id'], 'Submission response unknown. Reconcile using original client order ID.', halt=False)
            error = UnresolvedOrder('Submission could not be confirmed. Read-only recovery uses its original client order ID; no replacement order is sent.')
            self._isolate_failure(agent, error)
            raise error from None

    def _reviewer_cycle(self, only_rules=False, reconciled=False, agent_id=None):
        with self.lock:
            state = self.engine.snapshot()
            if state['experiment']['mode'] != 'paper':
                raise ValueError('Create an Alpaca paper experiment first.')
            self.check_stop()
            if not reconciled:
                self.reconcile()
            state = self.engine.snapshot()
            connected = [a for a in state['agents'] if self.public_state()['connections'][a['id']]['alpaca'] and not self._agent_paused(a['id']) and (agent_id is None or a['id'] == agent_id) and (not only_rules or a['provider'] == 'rules')]
            if not connected:
                return
            if len(connected) > 1:
                for participant in connected:
                    try:
                        self._reviewer_cycle(only_rules=only_rules, reconciled=True, agent_id=participant['id'])
                    except (ApiError, ValueError, KeyError, TypeError) as exc:
                        self._isolate_failure(participant, exc)
                return
            broker = self.broker(connected[0])
            clock = broker.clock()
            if not clock.get('is_open'):
                self.engine.event('Market is closed; no strategy orders submitted.')
                return
            stamp = parse_time(clock['timestamp'])
            local = ny_time(stamp)
            if abs((now() - stamp.astimezone(UTC)).total_seconds()) > 120 or (local.hour, local.minute) < (9, 45) or (local.hour, local.minute) >= (15, 45):
                self.engine.event('Outside the 09:45–15:45 New York entry window or broker clock is stale.')
                return
            day = local.date().isoformat()
            quotes = broker.quotes(list(SYMBOLS))
            prices = {}
            for symbol in SYMBOLS:
                q = quotes.get(symbol)
                if not q or q.get('execution_eligible') is False:
                    raise QuoteUnavailable(f'{symbol}: fresh execution quote unavailable.')
                age = (now() - parse_time(q.get('execution_timestamp', q['t'])).astimezone(UTC)).total_seconds()
                price = float(q.get('execution_price', q['price']))
                if not math.isfinite(price) or price <= 0 or age < -5 or age > 120 or q.get('execution_eligible') is False:
                    raise QuoteUnavailable(f'{symbol}: missing or stale quote; no strategy orders submitted.')
                prices[symbol] = price
            for symbol, value in prices.items():
                self.engine.mark({symbol: value}, quotes[symbol].get('execution_timestamp', quotes[symbol]['t']), self._mark_source(quotes[symbol], broker, execution=True))
            self.engine.note_quotes(connected[0]['id'], {symbol: quotes[symbol].get('execution_timestamp', quotes[symbol]['t']) for symbol in prices})
            if self.engine.snapshot()['experiment']['status'] != 'ready':
                self.engine.event('Entries are paused; quotes and reconciliation updated.')
                return
            start = (local.date() - dt.timedelta(days=160)).isoformat()
            sessions = broker.calendar(start, day)
            completed = [str(session['date']) for session in sessions if str(session['date']) < day]
            if not completed:
                raise ValueError('Unable to identify the last completed trading session.')
            expected = max(completed)
            bars = broker.bars(list(SYMBOLS), start, day)
            bars = {s: [b for b in bars.get(s, []) if str(b['t'])[:10] < day] for s in SYMBOLS}
            if any(not bars[s] or str(bars[s][-1]['t'])[:10] != expected for s in SYMBOLS):
                raise ValueError('Daily bars are missing the last completed trading session. No decisions submitted.')
            signals = {s: baseline_signal([b for b in bars.get(s, []) if str(b['t'])[:10] < day]) for s in SYMBOLS}
            for agent in connected:
                claim = f"cycle:{state['experiment']['id']}:{agent['id']}:{day}"
                if self._get(claim):
                    continue
                # Persist BEFORE a paid request/order. After a crash the day's opportunity is skipped.
                self._put(claim, {'at': now().isoformat()})
                current = next(a for a in self.engine.snapshot()['agents'] if a['id'] == agent['id'])
                for symbol in SYMBOLS:
                    sig = signals[symbol]
                    if sig['action'] == 'sell' and current['positions'].get(symbol, {}).get('qty', 0) > 0:
                        decision = self.engine.record_decision(agent['id'], {'action': 'sell', 'symbol': symbol, 'reason': sig['reason'], 'status': 'rules_exit', 'signal': sig, 'recorded_before_order': True})
                        self._submit(agent, symbol, 'sell', prices[symbol], decision['id'], automatic=True)
                candidates = [s for s in SYMBOLS if signals[s]['action'] == 'buy']
                # Fixed shared ranking, independent of provider and account history.
                candidates.sort(key=lambda s: (signals[s]['close'] / signals[s]['sma50'], s), reverse=True)
                if not candidates:
                    self.engine.record_decision(agent['id'], {'action': 'hold', 'reason': 'No eligible trend candidate.', 'status': 'rules'})
                    continue
                symbol = candidates[0]
                capacity = self._entry_capacity(agent['id'], symbol, prices[symbol])
                if capacity < 1:
                    self.engine.record_decision(agent['id'], {'action': 'hold', 'symbol': symbol, 'reason': 'Insufficient allocation capacity or a working order exists.', 'status': 'risk_blocked'})
                    continue
                snapshot = {'at': stamp.isoformat(), 'symbol': symbol, 'price': prices[symbol], 'signal': signals[symbol], 'rule': 'Approve or reject this long-only candidate. No sizing or exit discretion.', 'experiment': 'prospective_paper'}
                if agent['provider'] == 'rules':
                    result = {'approve': True, 'reason': 'Fixed baseline trend rule.', 'status': 'rules'}
                elif agent['provider'] in ('openai', 'anthropic'):
                    result = self._review(agent, snapshot)
                else:
                    result = {'approve': False, 'reason': 'Manual agent execution is not implemented.', 'status': 'disconnected'}
                decision = self.engine.record_decision(agent['id'], {'action': 'buy' if result['approve'] else 'hold', 'symbol': symbol, 'reason': result['reason'], 'status': result.get('status', 'reviewed'), 'snapshot': snapshot, 'review': result, 'recorded_before_order': True})
                if result['approve']:
                    self._submit(agent, symbol, 'buy', prices[symbol], decision['id'], automatic=True)
            self.engine.event('Paper cycle completed. Daily claims prevent repeat entries and model calls.')

    def _autonomy_key(self, agent_id):
        return f"autonomy:{self.engine.snapshot()['experiment']['id']}:{agent_id}"

    def _model_availability(self, agent):
        exp = self.engine.snapshot()['experiment']
        key_name = 'OPENAI_API_KEY' if agent['provider'] == 'openai' else 'ANTHROPIC_API_KEY'
        if agent['provider'] not in ('openai', 'anthropic') or not self.env.get(key_name):
            return 'Model API key is missing.'
        if not agent.get('model'):
            return 'Choose an available model ID in experiment setup.'
        if exp['monthly_model_budget'] <= 0:
            return 'Paid research is disabled: monthly model budget is $0.'
        if any(not math.isfinite(float(agent[k])) or agent[k] <= 0 for k in ('input_price', 'output_price')):
            return 'Set positive model token pricing before paid research.'
        return None

    def _ask_autonomous(self, agent, snapshot):
        """Reserve real API cost before each bounded research turn; no retry."""
        if len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8')) > 40000:
            raise ValueError('Research context exceeds its paid-request size bound.')
        output_limit = self.engine.snapshot()['experiment'].get('output_token_limit', MAX_MODEL_OUTPUT_TOKENS)
        key = self.env.get('OPENAI_API_KEY' if agent['provider'] == 'openai' else 'ANTHROPIC_API_KEY')
        kwargs = {} if output_limit == MAX_MODEL_OUTPUT_TOKENS else {'max_output_tokens': output_limit}
        result = self._paid_request(agent, lambda: autonomous_turn(agent['provider'], agent['model'], key, snapshot, **kwargs), input_bound=45000, output_limit=output_limit)
        result.pop('estimated_cost', None)  # Autonomous response schema contains provider usage only.
        return result

    def _assets(self, agent, broker, day):
        cache_key = (agent['id'], day)
        if cache_key not in self.asset_cache:
            # Use broker metadata, never a model assertion, to define execution eligibility.
            self.asset_cache = {k: v for k, v in self.asset_cache.items() if k[1] == day}
            scope = self.engine.snapshot()['experiment'].get('asset_scope', 'equities')
            self.asset_cache[cache_key] = broker.assets(include_crypto=True) if scope == 'equities_crypto' else broker.assets()
        return self.asset_cache[cache_key]

    def _record_autonomy(self, agent_id, data):
        self._put(self._autonomy_key(agent_id), data)
        public = deepcopy(data)
        public['exit_retries'] = self._get(self._exit_retry_key(agent_id), {})
        public['last_provider_model'] = self._get(f"provider_model:{self.engine.snapshot()['experiment']['id']}:{agent_id}")
        with self.cache_lock:
            self._cache['experiment'] = self.engine.snapshot()['experiment']['id']
            self._cache['autonomy'][agent_id] = public
            self._snapshot_at = now().isoformat()

    def _fresh_prices(self, broker, symbols, agent_id=None, mark=False, strict=True):
        if not symbols:
            return {}
        result = broker.quotes(list(dict.fromkeys(symbols)))
        prices = {}
        missing = []
        for symbol in symbols:
            item = result.get(symbol)
            try:
                if not isinstance(item, dict):
                    raise ValueError('missing quote')
                price = float(item.get('execution_price', item['price']))
                age = (now() - parse_time(item.get('execution_timestamp', item['t'])).astimezone(UTC)).total_seconds()
                if not math.isfinite(price) or price <= 0 or not -5 <= age <= 120 or item.get('execution_eligible') is False:
                    raise ValueError('stale or valuation-only')
            except (KeyError, TypeError, ValueError, OverflowError):
                missing.append(symbol)
                continue
            prices[symbol] = price
        if mark:
            for symbol, price in prices.items():
                self.engine.mark({symbol: price}, result[symbol].get('execution_timestamp', result[symbol]['t']), self._mark_source(result[symbol], broker, execution=True))
            if agent_id is not None:
                self.engine.note_quotes(agent_id, {symbol: result[symbol].get('execution_timestamp', result[symbol]['t']) for symbol in prices})
                self.engine.note_recovery(agent_id, {'quote_warning': ('Fresh execution quotes unavailable: ' + ', '.join(missing)) if missing else ''})
        if missing and strict:
            raise QuoteUnavailable('Fresh execution quotes unavailable: ' + ', '.join(missing))
        return prices

    def _preflight_plan(self, agent, plan, broker):
        """Validate the full batch without orders; never assume unfilled sale proceeds."""
        from .adapters import validate_autonomous_response
        plan = validate_autonomous_response(plan)
        if plan['phase'] != 'plan':
            raise ValueError('Only a final researched plan can produce orders.')
        state = self.engine.snapshot()
        exp = state['experiment']
        if len(plan['orders']) > exp['max_orders_per_cycle']:
            raise ValueError('Plan exceeds the configured order count per cycle.')
        current = next(a for a in state['agents'] if a['id'] == agent['id'])
        order_symbols = [item['symbol'] for item in plan['orders']]
        if len(set(order_symbols)) != len(order_symbols):
            raise ValueError('A plan cannot send competing orders for the same symbol.')
        exit_symbols = [item['symbol'] for item in plan['exits']]
        if len(set(exit_symbols)) != len(exit_symbols):
            raise ValueError('An exit plan must name each symbol once.')
        known_positions = set(current['positions'])
        planned_buys = {o['symbol'] for o in plan['orders'] if o['side'] == 'buy'}
        if any(s not in known_positions | planned_buys for s in exit_symbols):
            raise ValueError('Exit conditions must belong to this agent\'s holding or planned buy.')
        symbols = list(dict.fromkeys(order_symbols + exit_symbols + list(current['positions'])))
        for symbol in symbols:
            self.engine.register_asset(symbol, broker.asset(symbol))
        prices = self._fresh_prices(broker, symbols, agent_id=agent['id'], mark=True, strict=False)
        # Equity holdings have no executable overnight quote. Keep their last
        # known marks for portfolio display and use a conservative value below.
        required = set(order_symbols) | ({s for s in current['positions'] if s.endswith('/USD')} if planned_buys else set())
        missing = required - set(prices)
        if missing:
            raise QuoteUnavailable('Plan needs fresh quotes for execution or exposure checks: ' + ', '.join(sorted(missing)))
        current = next(a for a in self.engine.snapshot()['agents'] if a['id'] == agent['id'])
        if self._stopped(agent['id']):
            raise ValueError('Plan execution is stopped for this agent.')
        pending = self.engine.pending_orders()
        cash = current['available_cash']
        trading_equity = current.get('trading_equity', current['equity'])
        basis = trading_equity if exp.get('compound_profits') else min(current['allocation'], trading_equity)
        exposure = sum(p['qty'] * (prices[s] if s in prices else 1.25 * max(state['market']['prices'].get(s, 0), p['avg_price'])) for s, p in current['positions'].items())
        exposure += sum(o['reserved'] / (1 + exp['fee_bps'] / 10000) for o in pending if o['agent_id'] == agent['id'] and o['side'] == 'buy')
        for order in plan['orders']:
            symbol = order['symbol']
            if any(o['agent_id'] == agent['id'] and o['symbol'] == symbol for o in pending):
                raise ValueError('An order for a proposed symbol is still unresolved.')
            if order['side'] == 'buy':
                amount = order['notional']
                if amount < 1 or Decimal(str(amount)) != Decimal(str(amount)).quantize(Decimal('.01')):
                    raise ValueError('Buy amounts must be at least $1 and use whole cents.')
                cash -= amount * (1 + exp['fee_bps'] / 10000)
                exposure += amount
                position_value = current['positions'].get(symbol, {}).get('qty', 0) * prices[symbol]
                if cash < -1e-8 or position_value + amount > basis * exp['position_cap_pct'] / 100 + 1e-8 or exposure > basis * exp['exposure_cap_pct'] / 100 + 1e-8:
                    raise ValueError('Plan exceeds this agent\'s cash, position or exposure limit. No sizing was changed silently.')
            else:
                if order['qty'] > current['positions'].get(symbol, {}).get('qty', 0) + 1e-9:
                    raise ValueError('Plan tries to sell more shares than this agent owns.')
        for rule in plan['exits']:
            price = prices.get(rule['symbol'])
            if rule['stop_loss'] and rule['take_profit'] and rule['stop_loss'] >= rule['take_profit']:
                raise ValueError('Stop-loss price must be below take-profit price.')
            if rule['symbol'] in planned_buys and ((rule['stop_loss'] and rule['stop_loss'] >= price) or (rule['take_profit'] and rule['take_profit'] <= price)):
                raise ValueError('A new buy has an exit threshold already crossed by the current quote.')
        return prices

    def _execute_plan(self, agent, run, memory, broker):
        plan = run['plan']
        prices = self._preflight_plan(agent, plan, broker)
        state = self.engine.snapshot()
        current = next(a for a in state['agents'] if a['id'] == agent['id'])
        exits = dict(memory.get('exits', {}))
        for rule in plan['exits']:
            symbol = rule['symbol']
            position = current['positions'].get(symbol, {})
            exits[symbol] = {**rule, 'opened_at': position.get('opened_at') or exits.get(symbol, {}).get('opened_at'), 'updated_at': now().isoformat()}
        # Durable before orders: subsequent fills always retain their intended exit conditions.
        memory.update(strategy=plan['strategy'], watchlist=plan['watchlist'], exits=exits, status='executing')
        self._record_autonomy(agent['id'], memory)
        for intent in plan['orders']:
            if self._stopped(agent['id']):
                break
            decision = self.engine.record_decision(agent['id'], {'action': intent['side'], 'symbol': intent['symbol'], 'reason': intent['reason'], 'status': 'autonomous_intent', 'strategy': plan['strategy'], 'intent': intent, 'cycle_id': memory['cycle_id'], 'recorded_before_order': True})
            self._submit(agent, intent['symbol'], intent['side'], prices[intent['symbol']], decision['id'], notional=intent['notional'] if intent['side'] == 'buy' else None, qty=intent['qty'] if intent['side'] == 'sell' else None, automatic=True)
        memory['status'] = 'stopped' if self._stopped(agent['id']) else 'planned'
        self._record_autonomy(agent['id'], memory)

    def _run_agent_exits(self, agent, broker):
        if self._stopped(agent['id']):
            return
        memory = self._get(self._autonomy_key(agent['id']), {})
        current = next(a for a in self.engine.snapshot()['agents'] if a['id'] == agent['id'])
        positions = current['positions']
        pending_buys = {o['symbol'] for o in self.engine.pending_orders() if o['agent_id'] == agent['id'] and o['side'] == 'buy'}
        retained_exits = {symbol: rule for symbol, rule in memory.get('exits', {}).items() if symbol in positions or symbol in pending_buys}
        if retained_exits != memory.get('exits', {}):
            memory['exits'] = retained_exits
            self._record_autonomy(agent['id'], memory)
        if not positions:
            return
        try:
            prices = self._fresh_prices(broker, list(positions), agent_id=agent['id'], mark=True, strict=False)
        except ApiError as exc:
            self.engine.note_recovery(agent['id'], {'quote_warning': str(exc)[:600]})
            return
        if self.engine.snapshot()['experiment']['status'] != 'ready':
            return
        pending_symbols = {o['symbol'] for o in self.engine.pending_orders() if o['agent_id'] == agent['id']}
        for symbol, rule in memory.get('exits', {}).items():
            if symbol not in positions or symbol in pending_symbols or symbol not in prices or self._exit_retry_blocked(agent['id'], symbol):
                continue
            position = positions[symbol]
            # Use position lifecycle, not the date when a model last revised its strategy.
            opened = position.get('opened_at') or rule.get('opened_at')
            if not opened:
                buy_orders = [o for o in self.engine.snapshot()['orders'] if o['agent_id'] == agent['id'] and o['symbol'] == symbol and o['side'] == 'buy' and o['filled_qty'] > 0]
                opened = min((o['created_at'] for o in buy_orders), default=now().isoformat())
            reason = None
            if rule.get('stop_loss', 0) > 0 and prices[symbol] <= rule['stop_loss']:
                reason = 'Agent-defined local stop-loss threshold reached.'
            elif rule.get('take_profit', 0) > 0 and prices[symbol] >= rule['take_profit']:
                reason = 'Agent-defined local take-profit threshold reached.'
            elif rule.get('max_hold_hours', 0) > 0 and (now() - parse_time(opened).astimezone(UTC)).total_seconds() >= rule['max_hold_hours'] * 3600:
                reason = 'Agent-defined maximum holding time reached.'
            if reason:
                decision = self.engine.record_decision(agent['id'], {'action': 'sell', 'symbol': symbol, 'reason': reason, 'status': 'agent_exit_trigger', 'exit_rule': rule, 'recorded_before_order': True})
                try:
                    self._submit(agent, symbol, 'sell', prices[symbol], decision['id'], qty=position['qty'], automatic=True)
                except ExecutionDeferred as exc:
                    self._isolate_failure(agent, exc)

    def _autonomous_cycle(self):
        from .autonomous import AutonomousResearch, run_agent_cycle
        self.check_stop()
        self.reconcile()
        state = self.engine.snapshot()
        connected = [a for a in state['agents'] if self.public_state()['connections'][a['id']]['alpaca'] and not self._agent_paused(a['id'])]
        for agent in connected:
            try:
                if agent['provider'] not in ('openai', 'anthropic') or self._agent_paused(agent['id']):
                    continue
                broker = self.broker(agent)
                clock = broker.clock()
                stamp = parse_time(clock['timestamp'])
                if abs((now() - stamp.astimezone(UTC)).total_seconds()) > 120:
                    raise ExecutionDeferred('Broker clock is stale.')
                self._run_agent_exits(agent, broker)
                marked = self.engine.snapshot()['experiment']
                if marked.get('equity', 0) >= marked['target'] and marked['target'] > marked['total_capital']:
                    self.halt('Experiment target recorded. Automation paused; positions and working orders remain.')
                    return
                if self.check_stop() or self.engine.snapshot()['experiment']['status'] != 'ready':
                    return
                local = ny_time(stamp)
                stock_window = bool(clock.get('is_open') and (9, 45) <= (local.hour, local.minute) < (15, 45)
                                    and (not clock.get('next_close') or (parse_time(clock['next_close']).astimezone(UTC) - now()).total_seconds() >= 90))
                memory = self._get(self._autonomy_key(agent['id']), {})
                if memory.get('next_due') and parse_time(memory['next_due']).astimezone(UTC) > now():
                    continue
                exp = self.engine.snapshot()['experiment']
                problem = self._model_availability(agent)
                if problem:
                    memory.update(status='disconnected', reason=problem)
                    self._record_autonomy(agent['id'], memory)
                    continue
                day = now().date().isoformat()  # A 24-hour UTC research quota.
                count_key = f"autonomy_count:{exp['id']}:{agent['id']}:{day}"
                count = self._get(count_key, 0)
                if count >= exp['max_cycles_per_day']:
                    continue
                self._put(count_key, count + 1)
                cycle_id = uuid.uuid4().hex
                previous_strategy = memory.get('strategy', {})
                memory.update(cycle_id=cycle_id, status='researching', last_cycle=now().isoformat(), next_due=(now() + dt.timedelta(minutes=exp['cycle_minutes'])).isoformat())
                self._record_autonomy(agent['id'], memory)
                current = next(a for a in self.engine.snapshot()['agents'] if a['id'] == agent['id'])
                own_decisions = [{k: d.get(k) for k in ('at', 'action', 'symbol', 'reason', 'status')} for d in self.engine.snapshot()['decisions'] if d['agent_id'] == agent['id']][-8:]
                snapshot = {
                    'at': stamp.isoformat(), 'mode': 'paper',
                    'goal': {'initial_capital': agent['allocation'], 'target_equity': exp['target'] * agent['allocation'] / exp['total_capital'], 'objective': 'Independently maximize own net equity growth as quickly as possible within the fixed risk and budget limits, toward the assigned target. Speed never overrides limits. No deadline or guaranteed return.'},
                    'portfolio': {k: current[k] for k in ('allocation', 'cash', 'available_cash', 'equity', 'positions', 'fees', 'model_cost', 'net_pnl')},
                    'working_orders': [{k: o[k] for k in ('symbol', 'side', 'notional', 'qty', 'status', 'reserved')} for o in self.engine.pending_orders() if o['agent_id'] == agent['id']],
                    'recent_fills': [{k: o[k] for k in ('symbol', 'side', 'filled_qty', 'filled_avg_price', 'estimated_fees', 'updated_at')} for o in self.engine.snapshot()['orders'] if o['agent_id'] == agent['id'] and o['filled_qty'] > 0][-10:],
                    'constraints': {k: exp[k] for k in ('position_cap_pct', 'exposure_cap_pct', 'loss_limit', 'fee_bps', 'cycle_minutes', 'max_cycles_per_day', 'max_orders_per_cycle', 'compound_profits')},
                    'market_availability': {'stock_entry_window_open': stock_window, 'crypto_trading_24_7': exp.get('asset_scope') == 'equities_crypto', 'asset_scope': exp.get('asset_scope', 'equities')},
                    'execution_rules': 'Long-only broker-eligible US equities/ETFs and, when enabled, spot crypto pairs quoted in USD. USD buys >=$1 in whole cents; sell only owned quantity. Stocks use fractional DAY market orders during the 09:45–15:45 New York entry window, subject to broker clock and closing buffer. Research stocks after hours and keep a next-session strategy/watchlist, but no stock order can be sent until a new fresh check in the next session. USD crypto uses GTC market orders 24/7. Do not assume unfilled sell proceeds fund buys. No leverage, derivatives, forex, options or non-USD crypto pairs. Stops/targets are local triggers while this server and automation run, not broker stop orders. One order per symbol per plan. No required signal or SMA strategy.',
                    'model_costs': {'input_usd_per_million_tokens': agent['input_price'], 'output_usd_per_million_tokens': agent['output_price'], 'monthly_agent_budget': exp['monthly_model_budget'] * agent['weight'] / sum(a['weight'] for a in state['agents'] if a['provider'] in ('openai', 'anthropic')), 'estimated_spent_this_month': self.meta.execute('SELECT COALESCE(SUM(cost),0) FROM calls WHERE month=? AND agent=?', (now().strftime('%Y-%m'), agent['id'])).fetchone()[0]},
                    'prior_strategy': previous_strategy, 'prior_watchlist': memory.get('watchlist', []), 'prior_exits': memory.get('exits', {}), 'deferred_equity_ideas': memory.get('deferred_equity_ideas', []), 'recent_decisions': own_decisions,
                }
                try:
                    assets = self._assets(agent, broker, day)
                    research = AutonomousResearch(broker, assets, stamp.isoformat())
                    run = run_agent_cycle(agent, snapshot, research, self._ask_autonomous, max_rounds=exp['research_rounds'], should_stop=lambda: self._stopped(agent['id']))
                    memory.update(status=run['status'], reason=run.get('reason', ''), evidence=run['evidence'], turns=run['turns'])
                    # Store the full source record and final plan BEFORE any execution request.
                    self.engine.record_decision(agent['id'], {'action': 'research', 'reason': run.get('reason', ''), 'status': run['status'], 'cycle_id': cycle_id, 'snapshot': snapshot, 'evidence': run['evidence'], 'turns': run['turns'], 'plan': run.get('plan'), 'recorded_before_order': True})
                    self._record_autonomy(agent['id'], memory)
                    if run.get('plan'):
                        plan = deepcopy(run['plan'])
                        deferred = [item for item in plan['orders'] if '/' not in item['symbol']] if not stock_window else []
                        if deferred:
                            memory['deferred_equity_ideas'] = deferred
                            plan['orders'] = [item for item in plan['orders'] if '/' in item['symbol']]
                            executable = {o['symbol'] for o in plan['orders']}
                            owned = set(current['positions'])
                            plan['exits'] = [item for item in plan['exits'] if item['symbol'] in executable | owned]
                        elif stock_window:
                            memory.pop('deferred_equity_ideas', None)
                        self._execute_plan(agent, {**run, 'plan': plan}, memory, broker)
                        if deferred:
                            memory['status'] = 'stock_ideas_deferred_until_new_session_review'
                            self._record_autonomy(agent['id'], memory)
                        delay = max(exp['cycle_minutes'], run['plan']['review_minutes'])
                        next_due = now() + dt.timedelta(minutes=delay)
                        if deferred and clock.get('next_open'):
                            # Schedule a fresh research turn in the next equity
                            # entry window, rather than sending a stale idea.
                            next_entry = parse_time(clock['next_open']).astimezone(UTC) + dt.timedelta(minutes=15)
                            if now() < next_entry < next_due:
                                next_due = next_entry
                        memory['next_due'] = next_due.isoformat()
                        self._record_autonomy(agent['id'], memory)
                except (ApiError, ValueError, KeyError, TypeError) as exc:
                    memory.update(status='blocked', reason=str(exc)[:500])
                    self._record_autonomy(agent['id'], memory)
                    self.engine.record_decision(agent['id'], {'action': 'hold', 'status': 'autonomy_blocked', 'reason': str(exc)[:500], 'cycle_id': cycle_id})
                    if self.engine.snapshot()['experiment']['status'] != 'ready':
                        return
            except (ApiError, ValueError, KeyError, TypeError, OverflowError) as exc:
                self._isolate_failure(agent, exc)
                continue
        # Retain the fixed rules competitor as a separate control, never as an AI constraint.
        self._reviewer_cycle(only_rules=True, reconciled=True)

    def cycle(self):
        with self.lock:
            self._busy.set()
            try:
                exp = self.engine.snapshot()['experiment']
                if exp['mode'] != 'paper':
                    raise ValueError('Create an Alpaca paper experiment first.')
                if exp.get('agent_mode', 'reviewer') == 'autonomous':
                    self._autonomous_cycle()
                    fresh = self.engine.snapshot()['experiment']
                    if fresh.get('equity', 0) >= fresh['target'] and fresh['target'] > fresh['total_capital']:
                        self.halt('Experiment target recorded. Automation paused; positions and working orders remain.')
                else:
                    self._reviewer_cycle()
            finally:
                self._refresh_cache()
                self._busy.clear()

    def cancel(self, order_id):
        with self.lock:
            ensure_current_operation(self)
            state = self.engine.snapshot()
            order = next((o for o in self.engine.pending_orders() if o['id'] == order_id), None)
            if not order:
                raise ValueError('No pending order with that identifier.')
            agent = next(a for a in state['agents'] if a['id'] == order['agent_id'])
            broker = self.broker(agent)
            item = broker.order_by_client_id(order['client_order_id'])
            self.engine.update_order(order_id, item)
            if item['status'] not in ('filled', 'canceled', 'expired', 'rejected'):
                ensure_current_operation(self)
                broker.cancel_order(item['id'])
                self.engine.event('Cancellation requested. Reservation remains until broker confirms a terminal state.')

    def close_position(self, agent_id, symbol):
        with self.lock:
            ensure_current_operation(self)
            if not isinstance(symbol, str) or not re.fullmatch(r'(?:[A-Z][A-Z0-9.\-]{0,14}|[A-Z0-9]{2,15}/USD)', symbol):
                raise ValueError('Unsupported symbol.')
            self.reconcile(agent_id=agent_id)
            agent = next((a for a in self.engine.snapshot()['agents'] if a['id'] == agent_id), None)
            if not agent:
                raise ValueError('Unknown agent.')
            broker = self.broker(agent)
            if not symbol.endswith('/USD') and not broker.clock().get('is_open'):
                raise ValueError('Market is closed; use broker controls to manage working orders.')
            q = broker.quotes([symbol])[symbol]
            if abs((now() - parse_time(q['t']).astimezone(UTC)).total_seconds()) > 120:
                raise ValueError('Quote is stale.')
            self._submit(agent, symbol, 'sell', float(q['price']))

    def resolve_unknown_order(self, payload):
        """Operator-only, broker-confirmed resolution; never an agent tool."""
        from .resolution import resolve_unknown_order
        required = {'experiment_id', 'agent_id', 'order_id', 'client_order_id',
                    'broker_confirmation', 'confirmed_not_accepted'}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError('Provide the exact order identity and broker confirmation fields.')
        ensure_current_operation(self)
        resolve_unknown_order(self, **payload)
        self._refresh_cache()
        return self.public_state()

    def dispatch(self, route, body):
        if route == '/api/auto-resume':
            return self.set_auto_resume(body.get('enabled'))
        if route == '/api/test-connections':
            return self.test_connections(paid_model=body.get('paid_model', False))
        if route == '/api/autopilot' and body.get('enabled') is False:
            with self.control_lock:
                self._control_generation += 1
                self.engine.set_autopilot(False)
            return self.public_state()
        if route == '/api/agent/halt':
            return self.halt_agent(body['agent_id'])
        if route == '/api/halt':
            # Bypass the network-cycle lock so UI stop takes effect immediately.
            self.halt()
            return self.public_state()
        with self.lock:
            ensure_current_operation(self)
            if route == '/api/config':
                with self.control_lock:
                    ensure_current_operation(self)
                    self.engine.configure(body)
            elif route == '/api/new':
                self.new_experiment(body)
            elif route == '/api/demo':
                self.check_stop()
                self.engine.demo_step(body.get('count', 1))
            elif route == '/api/halt':
                self.halt()
            elif route == '/api/resume':
                self.resume()
            elif route == '/api/cycle':
                self.cycle()
            elif route == '/api/reconcile':
                self.reconcile(agent_id=body.get('agent_id'))
            elif route == '/api/agent/resume':
                self.resume_agent(body['agent_id'])
            elif route == '/api/autopilot':
                if type(body.get('enabled')) is not bool:
                    raise ValueError('enabled must be true or false.')
                with self.control_lock:
                    ensure_current_operation(self)
                    generation = self._control_generation
                if body['enabled']:
                    if self.engine.snapshot()['experiment']['mode'] != 'paper':
                        raise ValueError('Autopilot is available only in Alpaca paper mode.')
                    self.resume()
                with self.control_lock:
                    ensure_current_operation(self)
                    if generation != self._control_generation:
                        raise ValueError('A stop was requested; automatic cycles remain disabled.')
                    self.engine.set_autopilot(body['enabled'])
            elif route == '/api/cancel':
                self.cancel(body['order_id'])
            elif route == '/api/close':
                self.close_position(body['agent_id'], body['symbol'])
            elif route == '/api/orders/resolve-unknown':
                return self.resolve_unknown_order(body)
            else:
                raise ValueError('Unknown API route.')
            self._refresh_cache()
            return self.public_state()
