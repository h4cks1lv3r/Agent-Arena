"""Carry operator admission identity through synchronous worker dispatch."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar


class OperationCanceled(ValueError):
    """A newer stop or experiment invalidated an admitted operation."""


_admission = ContextVar('arena_operation_admission', default=None)


@contextmanager
def admitted_operation(guard, experiment_id):
    # ContextVar prevents the monitor and immediate-stop HTTP threads from
    # inheriting a worker's admission. Never put this guard on shared Service.
    token = _admission.set({**guard, 'experiment_id': experiment_id})
    try:
        yield
    finally:
        _admission.reset(token)


def ensure_current_operation(service):
    guard = _admission.get()
    if guard is None:
        return
    # Callers committing a control mutation also hold this RLock around their
    # mutation. No admission/control lock is retained over a network request.
    with getattr(service, 'control_lock', nullcontext()):
        if (getattr(service, '_control_generation', None) != guard['control_generation'] or
                getattr(service, '_stop_version', lambda: None)() != guard['stop_version']):
            raise OperationCanceled('This operation was canceled by a newer control or stop. It did not continue.')
        if service.public_state()['experiment']['id'] != guard['experiment_id']:
            raise OperationCanceled('The experiment changed. This operation did not continue.')


def operation_canceled(service):
    try:
        ensure_current_operation(service)
        return False
    except OperationCanceled:
        return True


def accept_operation_stop_clear():
    """After this operation intentionally removed its captured STOP, expect none."""
    guard = _admission.get()
    if guard is not None:
        # Do not snapshot current state: a newly-created external STOP must
        # still invalidate this operation, not become its accepted baseline.
        _admission.set({**guard, 'stop_version': None})
