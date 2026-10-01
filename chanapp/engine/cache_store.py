"""Atomic display-cache publication: newest data wins, damaged files are quarantined."""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishResult:
    status: str  # 'published' | 'superseded'
    reason: str = ''

    def __bool__(self):
        return self.status == 'published'


class CachePublicationError(RuntimeError):
    pass


class ObsoletePublication(CachePublicationError):
    """A finished old task must not retry or count a refresh failure."""


def valid_timestamp(stamp):
    return type(stamp) in (float, int) and math.isfinite(stamp) and 0 <= stamp <= time.time() + 60


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Replacement already committed. Do not claim zero publication.
            log.warning('cache publication committed; directory durability unconfirmed')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _quarantine(path):
    if path.exists():
        os.replace(path, path.with_name('corrupt-' + uuid.uuid4().hex + '.json'))
        log.warning('cache file quarantined; rebuilding verified data')
        diagnostics = sorted(path.parent.glob('corrupt-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in diagnostics[2:]:
            old.unlink(missing_ok=True)


def publish_json(path, payload):
    """Atomically replace ``path`` unless the file on disk already holds newer data."""
    path = Path(path)
    if not valid_timestamp(payload.get('ts')):
        raise ValueError('invalid display timestamp')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            old = json.loads(path.read_text())
            if not isinstance(old, dict) or not valid_timestamp(old.get('ts')):
                raise ValueError()
        except FileNotFoundError:
            old = {}
        except (ValueError, TypeError):
            _quarantine(path)
            old = {}
        if old and old['ts'] > payload['ts']:
            return PublishResult('superseded', 'newer_data')
        atomic_json(path, payload)
    return PublishResult('published')


def require_published(result):
    """Return the publication status ('published' or 'superseded')."""
    if result.status not in ('published', 'superseded'):
        raise CachePublicationError(f'unexpected cache publication status: {result.status}')
    return result.status
