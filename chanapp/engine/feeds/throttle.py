"""显示层请求限速（display_feed、feeds/baseline_backup 与 search 共用）。

K 线 raw provider 各自管理传输，不经过本模块。
"""
from __future__ import annotations

import threading
import time

_locks: dict[float, threading.Lock] = {}
_last_ts: dict[float, float] = {}


def throttle(min_interval: float) -> None:
    """按调用方各自的 min_interval 限速（线程安全）。"""
    lock = _locks.setdefault(min_interval, threading.Lock())
    with lock:
        wait = min_interval - (time.monotonic() - _last_ts.get(min_interval, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_ts[min_interval] = time.monotonic()
