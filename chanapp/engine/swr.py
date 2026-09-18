"""SWR（stale-while-revalidate）统一内核：过期回旧 + 后台刷新 + in-flight 防踩踏 + 退避。

四件套的单一所有者（三分支语义 = v1.3.1 契约不变；
设计：docs/superpowers/specs/2026-09-18-swr-merge-design.md）。
内核不摸文件、不碰发布逻辑：读写与落盘由消费方经 SwrIO 注入；
TTL、wire 字段映射、日志措辞均留在消费方。
"""
from __future__ import annotations

import importlib.util
import logging
import threading
import time
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from chanapp.engine import supply

if importlib.util.find_spec("chanapp.engine.cache_store") is not None:
    from chanapp.engine import cache_store
    _Obsolete = cache_store.ObsoletePublication
else:
    # 公开演示不含版本化缓存层：ObsoletePublication 永远不会被抛出，占位类仅保证类型引用有效。
    cache_store = None
    class _Obsolete(Exception):
        pass

log = logging.getLogger(__name__)

Identity = tuple  # (epoch, scheme, generation)


def make_key(identity: Identity, *domain: str) -> tuple:
    """统一工作键：(epoch, scheme, generation, *domain)。"""
    return (*identity, *domain)


@dataclass
class SwrIO:
    """消费方注入的数据面。read 只做存在性/合法性校验，TTL 判定由内核单点计算。"""
    read: Callable[[], "tuple[dict, float] | None"]   # (payload, written_at)；非法/缺失 → None
    sync_fetch: Callable[[], dict]                    # 抓取+落盘+superseded 回读；失败抛异常


class Scope(Enum):
    PER_KEY = "per-key"
    PER_REQUEST_KEY = "per-key"  # 语义别名：candidate 按请求键，实现同 PER_KEY
    GLOBAL = "global"


class CountSource(Enum):
    BACKGROUND_ONLY = "background"  # 仅后台线程失败计数（kline）
    UNIFIED = "unified"             # 冷路径与后台同语义计数（candidate）
    EMBEDDED = "embedded"           # 内核从不计数：计数嵌在消费方 fetch 内（baseline）


_GLOBAL_KEY = ("__global__",)


class BackoffPolicy:
    """连败退避状态机：after_failures 次连败冷却 seconds 秒（单次抖动不冷却）。

    PER_KEY/PER_REQUEST_KEY：状态按 key 存表，记录失败时清理异身份（key[:3] 不等）
    条目、表长 ≥512 全清；GLOBAL：单标量（key 忽略），无身份清理。
    ObsoletePublication 与 never_count 异常永不计数。"""

    def __init__(self, after_failures: int, seconds: float, *,
                 scope: Scope, count_source: CountSource,
                 never_count: tuple = ()):
        self.after_failures = after_failures
        self.seconds = seconds
        self.scope = scope
        self.count_source = count_source
        self.never_count = tuple(never_count) + (_Obsolete,)
        self._failures: dict = {}
        self._cool_until: dict = {}
        self._guard = threading.Lock()

    def _resolve(self, key):
        return _GLOBAL_KEY if self.scope is Scope.GLOBAL else key

    def blocked(self, key=None) -> bool:
        return time.monotonic() < self._cool_until.get(self._resolve(key), 0.0)

    def failure_count(self, key=None) -> int:  # 测试内省
        return self._failures.get(self._resolve(key), 0)

    def record_success(self, key) -> None:
        resolved = self._resolve(key)
        with self._guard:
            self._failures.pop(resolved, None)
            self._cool_until.pop(resolved, None)

    def record_failure(self, key, exc: BaseException | None = None) -> None:
        if exc is not None and isinstance(exc, self.never_count):
            return
        resolved = self._resolve(key)
        with self._guard:
            if self.scope is not Scope.GLOBAL:
                # Backoff is advisory; old identities cannot suppress current work.
                for old in list(self._failures):
                    if old[:3] != resolved[:3] or len(self._failures) >= 512:
                        self._failures.pop(old, None)
                        self._cool_until.pop(old, None)
            n = self._failures.get(resolved, 0) + 1
            self._failures[resolved] = n
            if n >= self.after_failures:
                self._cool_until[resolved] = time.monotonic() + self.seconds

    def reset(self) -> None:  # 测试辅助
        with self._guard:
            self._failures.clear()
            self._cool_until.clear()


class RefreshBlocked(RuntimeError):
    """raise_when_blocked 下退避中进入同步路径时抛出（不计失败）。"""


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


def _refresh_in_background(key, ttl, io, policy, permit,
                           on_refreshed, log_failure, log_success):
    """后台刷新：与冷路径同一把 per-key 锁、同一份 sync_fetch；锁内双检提前
    return 未发请求，不计数。许可失效不触上游、不计数。"""
    try:
        with _lock_for(key):
            current = io.read()
            if current is not None and (time.time() - current[1]) <= ttl:
                return  # 等锁期间已被刷新（首冷/另一后台），无需重复抓取
            if not supply.permit_still_valid(permit):
                return
            io.sync_fetch()
        if policy is not None and policy.count_source is not CountSource.EMBEDDED:
            policy.record_success(key)
        if log_success is not None:
            log_success(key)
    except _Obsolete:
        return
    except Exception as exc:
        if policy is not None and policy.count_source is not CountSource.EMBEDDED:
            policy.record_failure(key, exc)
        (log_failure or _default_log_failure)(key, exc)
        return
    if on_refreshed is not None:
        try:
            current = io.read()
            if current is not None:
                on_refreshed(key, current[0])
        except Exception:
            log.warning("刷新后回调失败 %s", key, exc_info=True)


def _trigger(key, ttl, io, policy, should_trigger,
             on_refreshed, log_failure, log_success, domain):
    """过期缓存的后台异步刷新：同 key 同时只允许一个在飞（防踩踏）。

    已完成线程在下次触发时回收；连败冷却/自定义谓词抑制时不起线程。"""
    if should_trigger is not None:
        if not should_trigger(key):
            return
    elif policy is not None and policy.blocked(key):
        return
    with _inflight_guard:
        for old, thread in list(_inflight.items()):
            if not thread.is_alive():
                _inflight.pop(old, None)
        t = _inflight.get(key)
        if t is not None and t.is_alive():
            return
        captured = supply.current()
        permit = supply.capture_write_permit()

        def refresh():
            with supply.use(captured, permit):
                _refresh_in_background(key, ttl, io, policy, permit,
                                       on_refreshed, log_failure, log_success)

        t = threading.Thread(target=refresh,
                             name=f"swr-refresh-{domain or key[-1]}", daemon=True)
        _inflight[key] = t
        t.start()


def serve(key, ttl: float, io: SwrIO, *,
          policy: BackoffPolicy | None = None,
          hit_when: Callable[[dict, float], bool] | None = None,
          require_fresh: bool = False,
          serve_stale_on_cold_failure: bool = False,
          raise_when_blocked: bool = False,
          should_trigger: Callable[[tuple], bool] | None = None,
          on_refreshed: Callable[[tuple, dict], None] | None = None,
          log_failure: Callable[[tuple, BaseException], None] | None = None,
          log_success: Callable[[tuple], None] | None = None,
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
        _trigger(key, ttl, io, policy, should_trigger,
                 on_refreshed, log_failure, log_success, domain)
        return ServeResult(payload, written_at, True, "stale")
    with _lock_for(key):
        if not require_fresh:
            recheck = io.read()
            if recheck is not None and is_hit(recheck[0], recheck[1]):
                return ServeResult(recheck[0], recheck[1], False, "hit")
        if raise_when_blocked and policy is not None and policy.blocked(key):
            raise RefreshBlocked(f"swr 退避中（{domain or key}）")
        try:
            fresh_payload = io.sync_fetch()
        except _Obsolete:
            raise
        except Exception as exc:
            if policy is not None and policy.count_source is CountSource.UNIFIED:
                policy.record_failure(key, exc)
            if serve_stale_on_cold_failure and payload is not None and not require_fresh:
                return ServeResult(payload, written_at, age > ttl, "stale")
            raise
        if policy is not None and policy.count_source is CountSource.UNIFIED:
            policy.record_success(key)
    return ServeResult(fresh_payload, time.time(), False, "miss")
