"""Independent upgrade check using the actual three released Engine versions.

Pass --references pointing to the extracted comparison base/ours/claude trees.
It never opens user databases or broker/model connections.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REFERENCES = None
sys.path.insert(0, str(ROOT))

def old_module(branch):
    source = REFERENCES / branch / 'Agent_Arena' / 'arena' / 'core.py'
    spec = importlib.util.spec_from_file_location('legacy_' + branch, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def full_legacy(engine, branch):
    state = engine.state()
    if branch == 'claude':
        state['decisions'] = engine.decisions()
        state['events'] = engine.archived_events(limit=100000) + state['events']
    return state

def populate(engine, branch, tag):
    engine.resume()
    for index in range(3):
        engine.record_decision('openai', {
            'action': 'research', 'reason': tag + str(index),
            'evidence': [{'id': tag + str(index), 'body': 'Full historical research evidence'}],
            'plan': {'orders': [], 'watchlist': ['SPY']},
        })
        engine.charge_model('openai', .001, call_id=tag + str(index))
        engine.mark({'SPY': 100 + index}, f'2026-09-17T14:0{index}:00+00:00')
        order = engine.reserve_order('openai', 'SPY', 'buy', 100, notional=10)
        engine.update_order(order['id'], {'status': 'rejected', 'filled_qty': '0'})
    if branch == 'claude':
        # Exercise the real archival transition in one transaction.
        # Identical text and time are still separate physical audit events.
        with patch.dict(engine._event.__globals__, {'_now': lambda: '2026-09-17T15:00:00+00:00'}), engine._write():
            for index in range(3100):
                engine._event(f'{tag} repeated audit event')
    return full_legacy(engine, branch)

def canonical(record):
    # v0.3/Claude events do not have IDs; migration adds stable identities.
    result = dict(record)
    for key in ('id',):
        if set(result) <= {'id', 'at', 'message', 'level'}:
            result.pop(key, None)
    return json.dumps(result, sort_keys=True, separators=(',', ':'))

def verify_rows(expected, actual, kind):
    from collections import Counter
    left = Counter(canonical(item) for item in expected)
    right = Counter(canonical(item) for item in actual)
    missing = left - right
    if missing:
        raise AssertionError(f'Migration omitted or changed {kind}: {list(missing)[:1]}')
    return {'original_rows': len(expected), 'exported_rows': len(actual),
            'all_original_records_preserved': True,
            'original_sha256': hashlib.sha256('\n'.join(sorted(left.elements())).encode()).hexdigest()}

def main():
    global REFERENCES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--references', required=True, type=Path, help='Directory containing base/, ours/, and claude/ source trees')
    parser.add_argument('--output', type=Path, default=Path('actual_release_migration_results.json'))
    args = parser.parse_args()
    REFERENCES = args.references.resolve()
    from arena.core import Engine
    results = {}
    with patch('socket.socket.connect', side_effect=AssertionError('No external connections')):
        for branch in ('base', 'ours', 'claude'):
            with tempfile.TemporaryDirectory(prefix='arena-upgrade-check-') as temporary:
                path = Path(temporary) / 'arena.sqlite3'
                legacy = old_module(branch).Engine(path)
                legacy.configure({'mode': 'paper'})
                archived = populate(legacy, branch, 'archived-')
                legacy.new_experiment({'mode': 'paper'})
                active = populate(legacy, branch, 'active-')
                pending = legacy.reserve_order('openai', 'SPY', 'buy', 100, notional=10)
                active = full_legacy(legacy, branch)
                legacy.close()
                before_db = sqlite3.connect(path)
                original_payload = before_db.execute('SELECT payload FROM arena_state WHERE id=1').fetchone()[0]
                before_db.close()
                migrated = Engine(path)
                try:
                    current = migrated.state()
                    result = {'storage_version': current['storage_version'],
                              'loss_limit_includes_model_costs': current['experiment']['loss_limit_includes_model_costs'],
                              'active': {}, 'archived': {}}
                    assert current['experiment']['loss_limit_includes_model_costs'] == (branch == 'base')
                    for kind in ('history', 'decisions', 'orders', 'events'):
                        result['active'][kind] = verify_rows(active[kind], current[kind], kind)
                    for before_agent in active['agents']:
                        after_agent = next(a for a in current['agents'] if a['id'] == before_agent['id'])
                        verify_rows(before_agent['model_charges'], after_agent['model_charges'], 'model_charges')
                    saved = json.loads(migrated._db.execute('SELECT payload FROM arena_archives WHERE id=?',
                                                           (archived['experiment']['id'],)).fetchone()[0])
                    for kind in ('history', 'decisions', 'orders', 'events'):
                        result['archived'][kind] = verify_rows(archived[kind], saved[kind], kind)
                    working = next(o for o in current['orders'] if o['id'] == pending['id'])
                    assert working['client_order_id'] == pending['client_order_id']
                    assert working['reserved'] == pending['reserved'] > 0
                    assert migrated.pending_orders()
                    result['pending_order_and_reservation_preserved'] = True
                    backups = list(Path(temporary).glob('arena.sqlite3.pre-v05-*.bak'))
                    assert len(backups) == 1
                    backup_db = sqlite3.connect(backups[0])
                    assert backup_db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                    assert backup_db.execute('SELECT payload FROM arena_state WHERE id=1').fetchone()[0] == original_payload
                    backup_db.close()
                    result['backup_files'] = [p.name for p in backups]
                    result['backup_preserves_original_payload_and_passes_integrity_check'] = True
                    results[branch] = result
                finally:
                    migrated.close()
    destination = args.output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))

if __name__ == '__main__':
    main()
