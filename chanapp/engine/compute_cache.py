"""结构计算结果进程内缓存：/api/chart 算完写入，/api/analysis 同 code/freq/末bar 复用。

键 (code, freq)，值含 last_bar_dt/structure/sig/evidence；LRU 上限 32 条。
末 bar dt 不同即失效（结构随新 bar 变化，不允许复用）。单进程单 worker 前提。
"""
from __future__ import annotations

import threading
from collections import OrderedDict

_MAX = 32
_CACHE: OrderedDict = OrderedDict()
_LOCK = threading.Lock()


def get(code: str, freq: str, last_bar_dt: str) -> dict | None:
    with _LOCK:
        entry = _CACHE.get((code, freq))
        if entry is None or entry["last_bar_dt"] != last_bar_dt:
            return None
        _CACHE.move_to_end((code, freq))
        return entry


def put(code: str, freq: str, last_bar_dt: str,
        structure: dict, sig: dict, evidence: list) -> None:
    with _LOCK:
        _CACHE[(code, freq)] = {"last_bar_dt": last_bar_dt, "structure": structure,
                                "sig": sig, "evidence": evidence}
        _CACHE.move_to_end((code, freq))
        while len(_CACHE) > _MAX:
            _CACHE.popitem(last=False)


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
