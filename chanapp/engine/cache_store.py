"""Atomic display-cache publication: newest data wins, damaged files are quarantined."""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
from pathlib import Path
import time
import uuid

from chanapp.engine import atomic_file

log = logging.getLogger(__name__)


class CachePublicationError(RuntimeError):
    pass


def valid_timestamp(stamp):
    return type(stamp) in (float, int) and math.isfinite(stamp) and 0 <= stamp <= time.time() + 60


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    if not atomic_file.replace(path, data, sync_dir=True):
        # Replacement already committed. Do not claim zero publication.
        log.warning('cache publication committed; directory durability unconfirmed')


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
            return 'superseded'
        atomic_json(path, payload)
    return 'published'
