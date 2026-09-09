"""结构计算缓存，按 (code, freq, data_version, calculation_id) 隔离完整数据窗口，LRU 上限 32 条。"""
from __future__ import annotations

import threading
from collections import OrderedDict

from chanapp.engine import data_identity, supply

_MAX = 32
_CACHE: OrderedDict = OrderedDict()
_LOCK = threading.Lock()


def dataset_version(dataset: dict) -> str:
    """Use the producer identity, or hash all bars plus their normalization context."""
    if dataset.get("data_version"):
        return dataset["data_version"]
    metadata = dataset.get("meta") or {}
    return data_identity.version(dataset["bars"], {
        "scheme": supply.current().scheme,
        **{key: dataset.get(key, metadata.get(key))
           for key in ("source", "fqf", "normalization")},
    })


def _identity(calculation_id: str | None) -> str:
    if calculation_id is None:
        from chanapp.engine.chanpy_profiles import profile_identity
        return profile_identity()["calculation_id"]
    return calculation_id


def get(code: str, freq: str, data_version: str, calculation_id: str | None = None) -> dict | None:
    key = (code, freq, data_version, _identity(calculation_id))
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            _CACHE.move_to_end(key)
        return entry


def put(code: str, freq: str, data_version: str,
        structure: dict, sig: dict, evidence: list, calculation_id: str | None = None) -> None:
    key = (code, freq, data_version, _identity(calculation_id))
    with _LOCK:
        _CACHE[key] = {"data_version": data_version, "structure": structure,
                       "sig": sig, "evidence": evidence}
        _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX:
            _CACHE.popitem(last=False)


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
