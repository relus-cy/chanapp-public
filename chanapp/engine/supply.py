"""Persistent supply selection and immutable request context.

Atomic replace is the commit boundary. Earlier failures leave state unchanged.
If directory sync fails after replacement, switch returns the committed state
with durability_warning='directory_sync_failed'; crash durability is unconfirmed.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import fcntl
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Callable, Iterator


SCHEMES = frozenset({'baseline', 'primary_candidate'})


@dataclass(frozen=True)
class Snapshot:
    scheme: str
    generation: int
    epoch: str = ""

    def __post_init__(self):
        if not isinstance(self.epoch, str):
            raise ValueError('Invalid supply epoch')
        if self.scheme not in SCHEMES:
            raise ValueError('Invalid supply scheme')
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError('Invalid supply generation')


@dataclass(frozen=True)
class WritePermit:
    snapshot: Snapshot
    purpose: str
    token: str | None = None


class StateUnavailable(ValueError):
    def __init__(self, reason='state_unavailable'):
        self.reason = reason
        super().__init__(reason)


class PublicationRejected(RuntimeError):
    def __init__(self, reason='obsolete_context'):
        self.reason = reason
        super().__init__(reason)


class ConflictError(RuntimeError):
    """The caller's generation no longer matches the committed state."""


class Manager:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._switch_lock = threading.Lock()
        self.runtime_dir = None
        self._seen_state = False

    def _read(self, require_exists=False) -> Snapshot:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            if require_exists or self._seen_state:
                raise StateUnavailable('state_missing') from exc
            return Snapshot('baseline', 0)
        except (OSError, ValueError) as exc:
            raise StateUnavailable('state_invalid') from exc
        self._seen_state = True
        try:
            if not isinstance(data, dict):
                raise ValueError()
            if type(data.get('schema_version', 1)) is not int or data.get('schema_version', 1) != 1:
                raise StateUnavailable('state_schema_unsupported')
            return Snapshot(data['scheme'], data['generation'])
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, StateUnavailable):
                raise
            raise StateUnavailable('state_invalid') from exc

    def snapshot(self) -> Snapshot:
        if self.runtime_dir is not None:
            from . import supply_runtime as runtime
            with runtime.short_lock(self.runtime_dir):
                return runtime.active(runtime.read(self.runtime_dir))
        return self._read()

    def switch(self, target: str, expected_generation: int,
               prepare: Callable[[Snapshot], dict], expected_epoch: str | None = None) -> dict:
        if target not in SCHEMES:
            raise ValueError('Invalid supply scheme')
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError('Invalid supply generation')
        if self.runtime_dir is not None:
            return self._runtime_switch(target, expected_generation, prepare, expected_epoch)
        with self._switch_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Never replace/unlink this lock file: every process must lock the
            # same inode even as the separate state file is replaced.
            with self.path.with_name(self.path.name + '.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    before = self.snapshot()
                    if before.generation != expected_generation:
                        raise ConflictError('Supply generation changed')
                    if target == before.scheme:
                        return {**_response(before), 'prepared': {}}
                    proposed = Snapshot(target, before.generation + 1)
                    prepared = prepare(proposed)
                    if not isinstance(prepared, dict):
                        raise ValueError('Invalid supply preparation result')
                    warning = self._persist(proposed)
                    result = {**_response(proposed), 'prepared': prepared}
                    if warning:
                        result['durability_warning'] = warning
                    return result
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _runtime_switch(self, target, expected_generation, prepare, expected_epoch):
        from . import supply_runtime as runtime
        import uuid
        root = self.runtime_dir
        with self._switch_lock:
            with runtime.short_lock(root):
                descriptor = runtime.read(root)
                before = runtime.active(descriptor)
                if before.generation != expected_generation or before.epoch != expected_epoch:
                    raise ConflictError('Supply identity changed')
                if target == before.scheme:
                    return {**asdict(before), 'prepared': {}}
                proposed = Snapshot(target, before.generation + 1, before.epoch)
                permit = WritePermit(proposed, 'prepare', str(uuid.uuid4()))
                descriptor['preparing'] = {'snapshot': asdict(proposed), 'token': permit.token,
                                           'start_generation': before.generation}
                runtime.write(root, descriptor)
            committed = False
            try:
                with use(proposed, permit):
                    prepared = prepare(proposed)
                if not isinstance(prepared, dict):
                    raise ValueError('Invalid supply preparation result')
                with runtime.short_lock(root):
                    descriptor = runtime.read(root)
                    if runtime.active(descriptor) != before:
                        raise ConflictError('Supply identity changed')
                    warning = self._persist(proposed)
                    committed = True
                    descriptor.update(active=asdict(proposed), preparing=None)
                    try:
                        runtime.write(root, descriptor)
                    except OSError:
                        # The committed disk state is authoritative; mismatch fences all writes.
                        warning = 'runtime_recovery_required'
                result = {**asdict(proposed), 'prepared': prepared}
                if warning:
                    result['durability_warning'] = warning
                return result
            finally:
                if not committed:
                    try:
                        with runtime.short_lock(root):
                            descriptor = runtime.read(root)
                            descriptor['preparing'] = None
                            runtime.write(root, descriptor)
                    except (OSError, StateUnavailable) as exc:
                        # Removing the descriptor revokes every token if update failed.
                        try:
                            (root / 'runtime.json').unlink(missing_ok=True)
                        except OSError:
                            runtime.fence_owner()
                        finally:
                            raise StateUnavailable('runtime_recovery_required') from exc

    def _persist(self, state: Snapshot) -> str | None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self.path.parent,
                                             prefix='.state-', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({'schema_version': 1, 'scheme': state.scheme,
                           'generation': state.generation}, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None  # Committed; never report subsequent sync as rollback.
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                return 'directory_sync_failed'
            return None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _state_path() -> Path:
    explicit = os.environ.get('SUPPLY_STATE_PATH')
    if explicit:
        return Path(explicit)
    root = Path(os.environ.get('CHANAPP_CACHE_DIR') or
                Path(__file__).resolve().parents[1] / '.cache')
    return root / 'supply' / 'state.json'


# An explicit manager override is useful to embedded applications and tests.
_default_manager: Manager | None = None


@lru_cache(maxsize=16)
def _manager_for_path(path: Path) -> Manager:
    return Manager(path)


def _manager() -> Manager:
    from . import supply_runtime as runtime
    if runtime._owner is not None:
        return runtime._owner[2]
    return _default_manager if _default_manager is not None else _manager_for_path(_state_path())


_permit_context: ContextVar[WritePermit | None] = ContextVar('supply_permit', default=None)

_context: ContextVar[Snapshot | None] = ContextVar('supply_snapshot', default=None)


def snapshot() -> Snapshot:
    return _manager().snapshot()


def current() -> Snapshot:
    bound = _context.get()
    return bound if bound is not None else snapshot()


@contextmanager
def use(state: Snapshot, permit: WritePermit | None = None) -> Iterator[Snapshot]:
    if permit is None:
        bound = _permit_context.get()
        permit = bound if bound is not None and bound.snapshot == state else None
    elif permit.snapshot != state:
        raise ValueError('Permit snapshot mismatch')
    permit_token = _permit_context.set(permit)
    token = _context.set(state)
    try:
        yield state
    finally:
        _context.reset(token)
        _permit_context.reset(permit_token)


def switch(target: str, expected_generation: int,
           prepare: Callable[[Snapshot], dict], expected_epoch: str | None = None) -> dict:
    return _manager().switch(target, expected_generation, prepare, expected_epoch)


def _response(state):
    result = asdict(state)
    if not state.epoch:
        result.pop('epoch')
    return result


def capture_write_permit():
    bound = _permit_context.get()
    return bound if bound is not None else WritePermit(current(), 'active')


def baseline_import_permit():
    from . import supply_runtime as runtime
    root = runtime.directory()
    if root is None:
        state = snapshot()
    else:
        with runtime.short_lock(root):
            state = runtime.active(runtime.read(root))
    return WritePermit(Snapshot('baseline', state.generation, state.epoch), 'baseline_import')


from .supply_runtime import start_runtime, stop_runtime, guard_publication, permit_still_valid
