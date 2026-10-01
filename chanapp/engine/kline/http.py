"""HTTP 抓取小工具：urllib + 每调用方限速（显示层 display_feed 与 feeds/baseline_backup 共用）。

K 线 raw provider 各自管理传输，不经过本模块。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

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


def fetch_json(url: str, *, timeout: float = 30, min_interval: float = 0.3,
               headers: dict | None = None) -> dict:
    throttle(min_interval)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
