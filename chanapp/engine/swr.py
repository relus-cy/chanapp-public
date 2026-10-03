"""SWR（stale-while-revalidate）统一内核：过期回旧 + 后台刷新 + in-flight 防踩踏 + 退避。

四件套的单一所有者（三分支语义 = v1.3.1 契约不变）。
内核不摸文件、不碰发布逻辑：读写与落盘由消费方经 SwrIO 注入；
TTL、wire 字段映射、日志措辞均留在消费方。
"""
from __future__ import annotations

import logging
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)


def make_key(*domain: str) -> tuple:
    """统一工作键：消费方给出的缓存域（如文件名）组成的元组。"""
    return tuple(domain)


@dataclass
class SwrIO:
    """消费方注入的数据面。read 只做存在性/合法性校验，TTL 判定由内核单点计算。"""
    read: Callable[[], "tuple[dict, float] | None"]   # (payload, written_at)；非法/缺失 → None
    sync_fetch: Callable[[], dict]                    # 抓取+落盘+superseded 回读；失败抛异常


class BackoffPolicy:
    """连败退避状态机：after_failures 次连败冷却 seconds 秒（单次抖动不冷却）。"""

    def __init__(self, after_failures: int, seconds: float):
        self.after_failures = after_failures
        self.seconds = seconds
        self._failures = 0
        self._cool_until = 0.0
        self._guard = threading.Lock()

    def blocked(self) -> bool:
        return time.monotonic() < self._cool_until

    def failure_count(self) -> int:  # 测试内省
        return self._failures

    def record_success(self) -> None:
        with self._guard:
            self._failures = 0
            self._cool_until = 0.0

    def record_failure(self) -> None:
        with self._guard:
            self._failures += 1
            if self._failures >= self.after_failures:
                self._cool_until = time.monotonic() + self.seconds

    def reset(self) -> None:  # 测试辅助
        with self._guard:
            self._failures = 0
            self._cool_until = 0.0


@dataclass
class ServeResult:
    payload: dict
    written_at: float
    stale: bool   # age > ttl（miss 恒 False）
    cache: str    # "hit" | "stale" | "miss"（锁内双检命中也报 "hit"）


_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()
_inflight: dict = {}
_inflight_guard = threading.Lock()


def _lock_for(key) -> threading.Lock:
    """per-key 串行锁：异 key 互不阻塞；同 key 串行，配合锁内双检防重复取数。"""
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _default_log_failure(key, exc):
    log.warning("后台刷新失败 %s", key, exc_info=True)


def _refresh_in_background(key, ttl, io, log_failure):
    """后台刷新：与冷路径同一把 per-key 锁、同一份 sync_fetch；锁内双检提前
    return 未发请求。"""
    try:
        with _lock_for(key):
            current = io.read()
            if current is not None and (time.time() - current[1]) <= ttl:
                return  # 等锁期间已被刷新（首冷/另一后台），无需重复抓取
            io.sync_fetch()
    except Exception as exc:
        (log_failure or _default_log_failure)(key, exc)


def _trigger(key, ttl, io, should_trigger, log_failure, domain):
    """过期缓存的后台异步刷新：同 key 同时只允许一个在飞（防踩踏）。

    已完成线程在下次触发时回收；自定义谓词抑制时不起线程。"""
    if should_trigger is not None and not should_trigger(key):
        return
    with _inflight_guard:
        for old, thread in list(_inflight.items()):
            if not thread.is_alive():
                _inflight.pop(old, None)
        t = _inflight.get(key)
        if t is not None and t.is_alive():
            return
        def refresh():
            _refresh_in_background(key, ttl, io, log_failure)

        t = threading.Thread(target=refresh,
                             name=f"swr-refresh-{domain or key[-1]}", daemon=True)
        _inflight[key] = t
        t.start()


def serve(key, ttl: float, io: SwrIO, *,
          hit_when: Callable[[dict, float], bool] | None = None,
          require_fresh: bool = False,
          serve_stale_on_cold_failure: bool = False,
          should_trigger: Callable[[tuple], bool] | None = None,
          log_failure: Callable[[tuple, BaseException], None] | None = None,
          domain: str = "") -> ServeResult:
    """三分支：TTL 内命中直返；过期回旧 + 触发后台刷新；仅无缓存同步抓取（首冷）。

    hit_when：自定义命中判定（默认 age<=ttl）；回旧分支只看 age>ttl，与 hit_when 无关。
    require_fresh：跳过回旧与锁内双检（新鲜缓存仍命中），过期时强制同步抓取。
    """
    def is_hit(payload, written_at):
        if hit_when is not None:
            return hit_when(payload, written_at)
        return (time.time() - written_at) <= ttl

    current = io.read()
    payload, written_at, age = None, 0.0, 0.0
    if current is not None:
        payload, written_at = current
        age = max(0.0, time.time() - written_at)
        if is_hit(payload, written_at):
            return ServeResult(payload, written_at, False, "hit")
    if payload is not None and not require_fresh and age > ttl:
        _trigger(key, ttl, io, should_trigger, log_failure, domain)
        return ServeResult(payload, written_at, True, "stale")
    with _lock_for(key):
        if not require_fresh:
            recheck = io.read()
            if recheck is not None and is_hit(recheck[0], recheck[1]):
                return ServeResult(recheck[0], recheck[1], False, "hit")
        try:
            fresh_payload = io.sync_fetch()
        except Exception:
            if serve_stale_on_cold_failure and payload is not None and not require_fresh:
                return ServeResult(payload, written_at, age > ttl, "stale")
            raise
    return ServeResult(fresh_payload, time.time(), False, "miss")
