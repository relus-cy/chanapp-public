"""Service lifetime ownership and cross-process publication fencing."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

_owner = None
_publisher = None
_owner_epoch = None
_faulted = False
_local_lock = threading.RLock()
_held = threading.local()


def directory():
    if _owner is not None:
        return _owner[1]
    explicit = os.environ.get('SUPPLY_RUNTIME_DIR')
    return Path(explicit) if explicit else None


@contextmanager
def short_lock(root):
    with _local_lock:
        key = str(root.resolve())
        held = getattr(_held, 'roots', set())
        if key in held:
            yield
            return
        try:
            handle = (root / 'publication.lock').open('a')
        except OSError as exc:
            from .supply import StateUnavailable
            raise StateUnavailable('runtime_unavailable') from exc
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _held.roots = held | {key}
            try:
                yield
            finally:
                _held.roots = held
                fcntl.flock(handle, fcntl.LOCK_UN)


def read(root):
    from .supply import StateUnavailable, Snapshot
    if _faulted and _owner is not None and root == _owner[1]:
        raise StateUnavailable('runtime_recovery_required')
    try:
        # Lifetime ownership and publication authority are distinct: a faulty
        # service retains its instance lock while revoking cross-process writes.
        for filename in ('lifecycle.lock', 'publisher.lock'):
            with (root / filename).open('a') as owner_check:
                try:
                    fcntl.flock(owner_check, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(owner_check, fcntl.LOCK_UN)
                    raise ValueError('authority absent')
        data = json.loads((root / 'runtime.json').read_text())
        if not isinstance(data['epoch'], str) or not data['epoch']:
            raise ValueError()
        Snapshot(**data['active'])
        if data['active']['epoch'] != data['epoch'] or data.get('fault'):
            raise ValueError()
        if not isinstance(data['state_path'], str) or type(data['owner_pid']) is not int:
            raise ValueError()
        uuid.UUID(data['epoch'])
        preparing = data['preparing']
        if preparing is not None:
            proposed = Snapshot(**preparing['snapshot'])
            if (not isinstance(preparing['token'], str) or not preparing['token'] or
                    type(preparing['start_generation']) is not int or
                    preparing['start_generation'] != data['active']['generation'] or
                    proposed.generation != preparing['start_generation'] + 1 or
                    proposed.epoch != data['epoch']):
                raise ValueError()
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateUnavailable('runtime_unavailable') from exc


def write(root, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=root, prefix='.runtime-', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / 'runtime.json')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def start_runtime(state_path=None, runtime_dir=None):
    from . import supply
    global _owner, _publisher, _owner_epoch
    if _owner is not None:
        raise supply.StateUnavailable('runtime_already_running')
    root = Path(runtime_dir or os.environ.get('SUPPLY_RUNTIME_DIR') or
                Path(__file__).resolve().parents[1] / '.runtime')
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / 'lifecycle.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise supply.StateUnavailable('runtime_already_running') from exc
    publisher = (root / 'publisher.lock').open('a')
    try:
        fcntl.flock(publisher, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # An authority probe can briefly hold the publisher lock between our two
        # acquisitions; report a clean "already running" instead of a raw OSError.
        publisher.close()
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
        raise supply.StateUnavailable('runtime_already_running') from None
    _publisher = publisher
    manager = supply.Manager(Path(state_path) if state_path is not None else supply._manager().path)
    _owner = (handle, root, manager)
    manager.runtime_dir = root
    try:
        with short_lock(root):
            # Persist the first clean baseline so disappearance is detectable.
            initialized = manager.path.with_name(manager.path.name + '.initialized')
            state = manager._read(require_exists=initialized.exists())
            if not manager.path.exists():
                manager.path.parent.mkdir(parents=True, exist_ok=True)
                manager._persist(state)
            initialized.touch(exist_ok=True)
            state = supply.Snapshot(state.scheme, state.generation, str(uuid.uuid4()))
            _owner_epoch = state.epoch
            write(root, {'epoch': state.epoch, 'owner_pid': os.getpid(),
                         'state_path': str(manager.path.resolve()), 'active': asdict(state),
                         'preparing': None})
        return state
    except supply.StateUnavailable as exc:
        if isinstance(exc.__cause__, OSError) and not isinstance(exc.__cause__, FileNotFoundError):
            _release_locks()
            raise exc.__cause__
        # Diagnostic-only startup keeps the instance occupied; no auxiliary
        # file mutation is necessary to revoke publishers.
        fence_owner()
        raise
    except BaseException:
        _release_locks()
        raise


def fence_owner():
    """Revoke publication authority without relinquishing service ownership."""
    global _faulted
    _faulted = True
    if _publisher is not None:
        fcntl.flock(_publisher, fcntl.LOCK_UN)


def _release_locks():
    global _owner, _publisher, _owner_epoch, _faulted
    if _publisher is not None:
        fcntl.flock(_publisher, fcntl.LOCK_UN)
        _publisher.close()
    if _owner is not None:
        fcntl.flock(_owner[0], fcntl.LOCK_UN)
        _owner[0].close()
    _owner = _publisher = _owner_epoch = None
    _faulted = False


def stop_runtime():
    if _owner is None:
        return
    _, root, manager = _owner
    try:
        with short_lock(root):
            path = root / 'runtime.json'
            try:
                descriptor = json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                descriptor = None
            # A stale owner's cleanup must never unlink replacement authority.
            if (isinstance(descriptor, dict) and _owner_epoch is not None and
                    descriptor.get('epoch') == _owner_epoch and
                    descriptor.get('owner_pid') == os.getpid() and
                    descriptor.get('state_path') == str(manager.path.resolve())):
                path.unlink(missing_ok=True)
    finally:
        _release_locks()


def active(data):
    from .supply import Manager, Snapshot, StateUnavailable
    state = Manager(Path(data['state_path']))._read(require_exists=True)
    recorded = Snapshot(**data['active'])
    if (state.scheme, state.generation) != (recorded.scheme, recorded.generation):
        raise StateUnavailable('state_runtime_mismatch')
    return recorded


def _check_permit(data, permit):
    from .supply import PublicationRejected
    state = active(data)
    valid = permit.snapshot == state and permit.purpose == 'active'
    if permit.purpose == 'baseline_import':
        valid = (permit.snapshot.epoch == state.epoch and
                 permit.snapshot.generation == state.generation and
                 permit.snapshot.scheme == 'baseline')
    if permit.purpose == 'prepare':
        preparing = data['preparing']
        valid = bool(preparing and permit.token and preparing['token'] == permit.token and
                     preparing['snapshot'] == asdict(permit.snapshot) and
                     preparing['start_generation'] == state.generation)
    if not valid:
        raise PublicationRejected('obsolete_context')


@contextmanager
def guard_publication(permit):
    from .supply import PublicationRejected
    root = directory()
    if root is None:
        if permit.snapshot.epoch:
            raise PublicationRejected('obsolete_context')
        yield
        return
    # read()/active()/short_lock() already convert missing runtime files into
    # StateUnavailable; an exception escaping the yield body belongs to the
    # caller and must not be relabeled as a runtime fault.
    with short_lock(root):
        _check_permit(read(root), permit)
        yield


def permit_still_valid(permit):
    """Cheap pre-flight before spending an upstream request. False only when the
    permit is positively obsolete; unconfirmable authority lets the caller proceed
    and rely on the publication fence."""
    from .supply import PublicationRejected, StateUnavailable
    root = directory()
    if root is None:
        return not permit.snapshot.epoch
    try:
        with short_lock(root):
            _check_permit(read(root), permit)
        return True
    except PublicationRejected:
        return False
    except (StateUnavailable, OSError):
        return True
