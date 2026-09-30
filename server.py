#!/usr/bin/env python3
"""Loopback dashboard; optional authenticated access through an HTTPS proxy."""
from __future__ import annotations
import argparse
from copy import deepcopy
import datetime as dt
from email.utils import formatdate
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arena.remote_auth import (AuthFailure, COOKIE_NAME, RemoteAuth, SESSION_SECONDS,
                               initialize_access, load_access_secret, validate_public_origin)
from arena.operation_guard import admitted_operation

ROOT = Path(__file__).resolve().parent


def _stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_error(exc, service):
    from arena.adapters import ApiError
    if not isinstance(exc, (ValueError, KeyError, TypeError, ApiError)):
        return 'Unexpected application error. Review state before resuming.'
    text = str(exc).strip()[:1000] or 'The request could not be completed.'
    for name, value in getattr(service, 'env', {}).items():
        if re.search(r'(?i)key|secret|token|password', str(name)) and isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, '[redacted]')
    if re.search(r'(?i)https?://|\bbearer\s+\S+|\bsk-[A-Za-z0-9_-]{4,}', text):
        return 'The request could not be completed. Sensitive error details were withheld.'
    return text[:500]


class ControlBusy(ValueError):
    """The command was not admitted and must never run later."""


class CycleRunner:
    """One admitted operation; background work can wait for account monitoring."""
    ACTIVE = ('queued', 'running')
    MONITOR_WAIT_SECONDS = 300
    LOCK_POLL_SECONDS = .1

    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=1)
        self._closed = threading.Event()
        self._generation = 0
        self._job = {'id': None, 'status': 'idle'}
        self._recent = []
        self._request = None
        self._thread = threading.Thread(target=self._work, name='arena-operation', daemon=True)
        self._thread.start()

    def snapshot(self):
        with self._lock:
            return deepcopy(self._job)

    def recent(self):
        with self._lock:
            return deepcopy(self._recent)

    def invalidate_pending(self):
        # A stop issued after admission invalidates queued resume/close/cycle
        # requests. In-flight calls also check the service's operator generation.
        with self._lock:
            self._generation += 1

    def _claim(self, route, body, source):
        experiment = self.service.public_state()['experiment']['id']
        guard = {'control_generation': getattr(self.service, '_control_generation', None),
                 'stop_version': getattr(self.service, '_stop_version', lambda: None)()}
        with self._lock:
            if self._closed.is_set():
                raise ControlBusy('The server is stopping; no command was queued.')
            if self._job['status'] in self.ACTIVE:
                if self._request == (route, body) and self._job.get('generation') == self._generation:
                    return deepcopy(self._job), None
                raise ControlBusy('Another operation is running. This command was not queued. Wait for its result and try again.')
            self._request = (route, deepcopy(body))
            self._job = {'id': uuid.uuid4().hex, 'status': 'queued', 'source': source,
                         'kind': 'cycle' if route == '/api/cycle' else 'control',
                         'action': route.removeprefix('/api/'), 'queued_at': _stamp(),
                         'experiment_id': experiment, 'generation': self._generation}
            action = (deepcopy(self._job), route, deepcopy(body), guard)
            return deepcopy(self._job), action

    def enqueue(self, source='manual', *, route='/api/cycle', body=None):
        job, action = self._claim(route, {} if body is None else body, source)
        if action:
            self._queue.put_nowait(action)
        return job

    def _validate_pending(self, job, guard):
        with self._lock:
            if self._closed.is_set() or job['generation'] != self._generation:
                raise ControlBusy('This command was canceled by a newer stop or shutdown. It did not run.')
        # Stops issued outside HTTP (including an external STOP file) must also
        # cancel a waiting resume, which would otherwise clear that newer stop.
        if (getattr(self.service, '_control_generation', None) != guard['control_generation'] or
                getattr(self.service, '_stop_version', lambda: None)() != guard['stop_version']):
            raise ControlBusy('This command was canceled by a newer control or stop. It did not run.')
        if self.service.public_state()['experiment']['id'] != job['experiment_id']:
            raise ControlBusy('The experiment changed. This command did not run.')

    def _perform(self, action, *, wait_for_monitor=False):
        job, route, body, guard = action
        deadline = time.monotonic() + self.MONITOR_WAIT_SECONDS
        while True:
            self._validate_pending(job, guard)
            if wait_for_monitor:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlBusy('Account work did not finish within the waiting limit. This command did not run; review the monitor and retry.')
                acquired = self.service.lock.acquire(timeout=min(self.LOCK_POLL_SECONDS, remaining))
            else:
                acquired = self.service.lock.acquire(blocking=False)
            if acquired:
                break
            if not wait_for_monitor:
                raise ControlBusy('Account work is in progress. This command did not run. Wait for its result and try again.')
            # Admission remains bounded to this one job. Waiting happens only in
            # the worker, so state requests and emergency stops stay responsive.
            with self._lock:
                self._job['waiting_for'] = 'account_monitor'
                self._job.setdefault('waiting_since', _stamp())
        try:
            self._validate_pending(job, guard)
            with self._lock:
                if self._closed.is_set() or job['generation'] != self._generation:
                    raise ControlBusy('This command was canceled by a newer stop or shutdown. It did not run.')
                self._job.pop('waiting_for', None)
                self._job.update(status='running', started_at=_stamp())
            with admitted_operation(guard, job['experiment_id']):
                return self.service.cycle() if route == '/api/cycle' else self.service.dispatch(route, body)
        finally:
            self.service.lock.release()

    def _finish(self, error=None, result=None):
        with self._lock:
            self._job.pop('waiting_for', None)
            self._job.update(status='error' if error else 'complete', finished_at=_stamp())
            if error:
                self._job['error'] = error
            elif self._job.get('action') == 'test-connections' and isinstance(result, dict):
                # Diagnostics need a result after their asynchronous request ends.
                # Retain only their bounded report, never full account snapshots.
                def clean(value, depth=0):
                    if depth > 5:
                        return None
                    if isinstance(value, dict):
                        return {str(key)[:80]: clean(item, depth + 1) for key, item in list(value.items())[:30]
                                if not re.search(r'(?i)secret|password|authorization|api_key', str(key))}
                    if isinstance(value, list):
                        return [clean(item, depth + 1) for item in value[:12]]
                    if isinstance(value, str):
                        return _safe_error(ValueError(value), self.service) if value else ''
                    return value if value is None or isinstance(value, (bool, int, float)) else None
                self._job['result'] = clean({key: result[key] for key in ('checks', 'paid_model', 'at') if key in result})
            self._recent.append(deepcopy(self._job))
            self._recent = self._recent[-32:]

    def run_now(self, route, body):
        _job, action = self._claim(route, body, 'manual')
        if action is None:
            raise ControlBusy('This command is already running. Check its result before trying again.')
        try:
            result = self._perform(action)
        except Exception as exc:
            self._finish(_safe_error(exc, self.service))
            raise
        self._finish(result=result)
        return result

    def _work(self):
        from arena.adapters import ApiError
        while not self._closed.is_set():
            try:
                action = self._queue.get(timeout=.25)
            except queue.Empty:
                continue
            error = None
            result = None
            try:
                result = self._perform(action, wait_for_monitor=True)
            except Exception as exc:
                error = _safe_error(exc, self.service)
                # Service classifies broker/provider failures and preserves
                # recovery intent. Only an unexpected defect stops automation.
                if not isinstance(exc, (ValueError, KeyError, TypeError, ApiError)):
                    try:
                        self.service.halt('Unexpected operation error. Review state before resuming.')
                    except Exception:
                        pass
            finally:
                self._finish(error, result)
                self._queue.task_done()

    def close(self):
        self._closed.set()
        self.invalidate_pending()


def make_handler(service, runner, *, public_origin=None, auth=None):
    local_token = secrets.token_urlsafe(32)
    public_host = urllib.parse.urlsplit(public_origin).netloc if public_origin else None

    class Handler(BaseHTTPRequestHandler):
        server_version = 'AgentArena/0.6.1'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_args):
            # URLs, cookies and submitted access secrets never enter logs.
            pass

        def safe_origin(self, *, mutation=False):
            hosts = self.headers.get_all('Host', [])
            origins = self.headers.get_all('Origin', [])
            if len(hosts) != 1 or len(origins) > 1 or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                return False
            if public_origin:
                if hosts[0] != public_host:
                    return False
                return origins == [public_origin] if mutation else not origins or origins == [public_origin]
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            return hosts[0] in allowed and (not origins or origins[0] in {f'http://{h}' for h in allowed})

        def send(self, data, status=200, mime='application/json; charset=utf-8', attachment=False, headers=None):
            if not isinstance(data, bytes):
                data = json.dumps(data, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if public_origin:
                self.send_header('Strict-Transport-Security', 'max-age=31536000')
            if attachment:
                self.send_header('Content-Disposition', 'attachment; filename="agent-arena-snapshot.json"')
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != 'HEAD':
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass

        def request_path(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith('/'):
                raise ValueError('Invalid request target.')
            return parsed.path

        def session(self):
            cookies = self.headers.get_all('Cookie', [])
            return auth.session(cookies[0]) if auth and len(cookies) == 1 else None

        def require_auth(self, *, csrf=False):
            if public_origin:
                session = self.session()
                if session is None:
                    self.send({'error': 'Sign in to access Agent Arena.', 'login_required': True, 'remote': True}, 401)
                    return None
                values = self.headers.get_all('X-Arena-Token', [])
                if csrf and (len(values) != 1 or not auth.csrf_valid(session, values[0])):
                    self.send({'error': 'A valid session request token is required. Refresh the page.'}, 403)
                    return None
                return session
            if csrf:
                values = self.headers.get_all('X-Arena-Token', [])
                if len(values) != 1 or not secrets.compare_digest(values[0].encode(), local_token.encode()):
                    self.send({'error': 'Local session token required. Refresh the page.'}, 403)
                    return None
            return True

        def public_state(self, compact=True):
            state = service.public_state(compact=compact)
            state['server_job'] = runner.snapshot()
            state['server_jobs'] = runner.recent()
            return state

        def read_json(self):
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or self.headers.get('Transfer-Encoding') is not None:
                raise ValueError('A single bounded Content-Length is required.')
            try:
                length = int(lengths[0])
            except ValueError:
                raise ValueError('Request body size is invalid.') from None
            if not 0 < length <= 65536:
                raise ValueError('Request body must be 1–65,536 bytes.')
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('Request Content-Type must be application/json.')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Request body was incomplete.')

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('Duplicate request fields are not allowed.')
                    result[key] = value
                return result

            try:
                body = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                raise ValueError('Request body must be valid, finite JSON with unique fields.') from None
            if not isinstance(body, dict):
                raise ValueError('Request body must be a JSON object.')
            return body

        def do_GET(self):
            if not self.safe_origin():
                return self.send({'error': 'Request host or origin is not allowed.'}, 403)
            try:
                path = self.request_path()
                session = None
                if path.startswith('/api/'):
                    session = self.require_auth()
                    if session is None:
                        return
                if path == '/api/session':
                    return self.send({'token': session.csrf if public_origin else local_token, 'remote': bool(public_origin)})
                if path in ('/api/state', '/api/export'):
                    return self.send(self.public_state(compact=not path.endswith('export')), attachment=path.endswith('export'))
                files = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'),
                         '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if path in ('/static/app.js', '/static/style.css'):
                    path = path[len('/static'):]
                if path not in files:
                    return self.send({'error': 'Not found.'}, 404)
                name, mime = files[path]
                return self.send((ROOT / 'arena' / 'static' / name).read_bytes(), mime=mime)
            except (ValueError, KeyError, TypeError):
                return self.send({'error': 'The requested state or resource is unavailable.'}, 400)
            except Exception:
                return self.send({'error': 'The server could not provide the requested resource.'}, 500)

        do_HEAD = do_GET

        def do_POST(self):
            if not self.safe_origin(mutation=True):
                return self.send({'error': 'Request host or origin is not allowed.'}, 403)
            try:
                path = self.request_path()
                if path == '/api/login' and public_origin:
                    body = self.read_json()
                    if set(body) != {'access_token'}:
                        raise ValueError('Sign-in requires only an access_token field.')
                    cookie, session = auth.login(body['access_token'], self.client_address[0])
                    header = f'{COOKIE_NAME}={cookie}; Path=/; Max-Age={SESSION_SECONDS}; Expires={formatdate(session.expires, usegmt=True)}; Secure; HttpOnly; SameSite=Strict'
                    return self.send({'token': session.csrf, 'remote': True}, headers={'Set-Cookie': header})
                if self.require_auth(csrf=True) is None:
                    return
                body = self.read_json()
                if path == '/api/logout' and public_origin:
                    auth.logout(self.headers.get('Cookie', ''))
                    return self.send({'logged_out': True, 'remote': True}, headers={'Set-Cookie': f'{COOKIE_NAME}=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure; HttpOnly; SameSite=Strict'})
                if path == '/api/cycle':
                    if service.public_state()['experiment']['mode'] != 'paper':
                        raise ValueError('Create an Alpaca paper experiment first.')
                    job = runner.enqueue('manual')
                    return self.send({'state': self.public_state(), 'job': job}, 202)
                immediate = path in ('/api/halt', '/api/agent/halt') or (path in ('/api/autopilot', '/api/auto-resume') and body.get('enabled') is False)
                if immediate:
                    runner.invalidate_pending()
                    return self.send(service.dispatch(path, body))
                paper = service.public_state()['experiment']['mode'] == 'paper'
                network_controls = ('/api/resume', '/api/agent/resume', '/api/reconcile',
                                    '/api/autopilot', '/api/cancel', '/api/close', '/api/orders/resolve-unknown')
                if path == '/api/test-connections' or (paper and path in network_controls):
                    job = runner.enqueue(route=path, body=body)
                    return self.send({'state': self.public_state(), 'job': job}, 202)
                # Fast local commands also use nonblocking admission. A request
                # rejected during research cannot surprise the user minutes later.
                return self.send(runner.run_now(path, body))
            except ControlBusy as exc:
                return self.send({'error': str(exc), 'not_queued': True, 'job': runner.snapshot()}, 409)
            except AuthFailure as exc:
                headers = {'Retry-After': str(exc.retry_after)} if exc.retry_after is not None else None
                return self.send({'error': str(exc), 'login_required': True, 'remote': True}, exc.status, headers=headers)
            except (TimeoutError, OSError):
                return self.send({'error': 'The request did not complete within the allowed time.'}, 408)
            except (ValueError, KeyError, TypeError) as exc:
                return self.send({'error': _safe_error(exc, service)}, 400)
            except Exception:
                service.engine.halt('Unexpected application error. Restart and reconcile before resuming.')
                return self.send({'error': 'Unexpected application error; automation halted. Review state and restart.'}, 500)

        def reject_method(self):
            return self.send({'error': 'Method not allowed.'}, 405, headers={'Allow': 'GET, HEAD, POST'})

        do_OPTIONS = do_PUT = do_PATCH = do_DELETE = do_TRACE = do_CONNECT = reject_method

    return Handler


def main():
    parser = argparse.ArgumentParser(description='Agent Arena — paper only; optional authenticated HTTPS proxy access')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--public-origin', help='Exact external HTTPS origin served by a trusted reverse proxy; server still binds loopback.')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--halt', action='store_true', help='Persist a stop even if the server is not running.')
    action.add_argument('--init-access', action='store_true', help='Create a private remote access token, print it once, and exit.')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('Port must be between 0 and 65535.')
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.halt:
        (args.data_dir / 'STOP').write_text('External operator stop. Existing broker orders/positions remain.', encoding='utf-8')
        print('Persistent stop created. Use Alpaca directly to cancel working orders or close positions.')
        return
    if args.init_access:
        try:
            token = initialize_access(args.data_dir)
        except (ValueError, OSError) as exc:
            parser.error(str(exc) if isinstance(exc, ValueError) else 'The private access-token file could not be created.')
        print('Remote access token (shown once; keep it private):\n' + token)
        return
    from arena.service import Service, load_env
    load_env(ROOT / '.env')
    try:
        public_origin = validate_public_origin(args.public_origin) if args.public_origin else None
        auth = RemoteAuth(load_access_secret(args.data_dir)) if public_origin else None
    except ValueError as exc:
        parser.error(str(exc))
    lock_handle = (args.data_dir / 'server.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock_handle.seek(0)
            lock_handle.write(b'0')
            lock_handle.flush()
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('Another Agent Arena process is already using this data directory.', file=sys.stderr)
        sys.exit(1)
    service = Service(args.data_dir)
    runner = CycleRunner(service)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(service, runner, public_origin=public_origin, auth=auth))
    server.daemon_threads = True
    stop = threading.Event()

    def scheduler():
        while not stop.wait(60):
            try:
                experiment = service.engine.snapshot()['experiment']
                if not service.check_stop() and experiment.get('autopilot') and experiment['status'] == 'ready':
                    runner.enqueue('autopilot')
            except ControlBusy:
                pass  # An admitted operator command has priority over the next scheduled cycle.
            except Exception:
                service.engine.event('Automatic cycle scheduling failed; no order retry was made.', 'error')

    def monitor():
        while not stop.wait(15):
            try:
                service.monitor()
            except Exception:
                service.engine.event('Read-only quote monitoring failed; last successful values remain timestamped.', 'warning')

    threading.Thread(target=scheduler, name='arena-scheduler', daemon=True).start()
    threading.Thread(target=monitor, name='arena-monitor', daemon=True).start()
    url = public_origin or f'http://127.0.0.1:{server.server_port}'
    print(f'Agent Arena v0.6.0 — PAPER ONLY\nOpen {url}\nListening on loopback 127.0.0.1:{server.server_port}.\nKeep this process running. Ctrl+C stops the app; broker orders remain.\nRestart recovery follows your auto-resume preference and verifies accounts before trading. Operator stops remain in effect.')
    if public_origin:
        print('Remote sign-in is required. HTTPS must be provided by your configured reverse proxy.')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nApp stopped. Working paper orders and positions remain at the broker.')
    finally:
        stop.set()
        runner.close()
        server.server_close()
        lock_handle.close()


if __name__ == '__main__':
    main()
