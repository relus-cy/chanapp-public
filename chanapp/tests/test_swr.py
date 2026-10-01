"""engine/swr 内核：三分支、防踩踏、退避策略矩阵、行为开关。"""
import threading
import time
import unittest
from unittest import mock

from chanapp.engine import cache_store, swr


def _key(name="k1"):
    return swr.make_key(name)


class TestBackoffPolicy(unittest.TestCase):
    def test_single_failure_no_cooldown(self):
        p = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.UNIFIED)
        p.record_failure(_key(), RuntimeError("x"))
        self.assertFalse(p.blocked(_key()))
        self.assertEqual(p.failure_count(_key()), 1)

    def test_consecutive_failures_cooldown_and_success_reset(self):
        p = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.UNIFIED)
        for _ in range(2):
            p.record_failure(_key(), RuntimeError("x"))
        self.assertTrue(p.blocked(_key()))
        p.record_success(_key())
        self.assertFalse(p.blocked(_key()))
        self.assertEqual(p.failure_count(_key()), 0)

    def test_global_scope_ignores_key(self):
        p = swr.BackoffPolicy(2, 30, scope=swr.Scope.GLOBAL, count_source=swr.CountSource.EMBEDDED)
        p.record_failure(None, RuntimeError("x"))
        self.assertEqual(p.failure_count(), 1)
        self.assertEqual(p.failure_count(_key("other")), 1)

    def test_per_key_failures_do_not_evict_other_keys(self):
        """按 key 计数：一个 key 的失败不清掉另一个 key 的连败与冷却。"""
        p = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.UNIFIED)
        for _ in range(2):
            p.record_failure(_key("a"), RuntimeError("x"))
        p.record_failure(_key("b"), RuntimeError("x"))
        self.assertTrue(p.blocked(_key("a")))
        self.assertEqual(p.failure_count(_key("b")), 1)

    def test_table_bounded(self):
        p = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.UNIFIED)
        for n in range(600):
            p.record_failure(_key(f"k{n}"), RuntimeError("x"))
        self.assertLessEqual(len(p._failures), 512)

    def test_never_count_exception(self):
        p = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.UNIFIED)
        p.record_failure(_key(), cache_store.ObsoletePublication("old"))
        self.assertEqual(p.failure_count(_key()), 0)


class TestServe(unittest.TestCase):
    def setUp(self):
        for t in list(swr._inflight.values()):
            t.join(5)

    def _io(self, payload=None, ts=None, fetch=None):
        state = {"payload": payload, "ts": ts if ts is not None else time.time()}
        def read():
            return (state["payload"], state["ts"]) if state["payload"] is not None else None
        def sync_fetch():
            fresh = fetch() if fetch else {"data": "fresh"}
            state["payload"], state["ts"] = fresh, time.time()
            return fresh
        return swr.SwrIO(read=read, sync_fetch=sync_fetch), state

    def test_hit_fresh_no_refresh(self):
        io, _ = self._io(payload={"data": 1})
        with mock.patch.object(threading, "Thread") as th:
            r = swr.serve(_key(), 60, io)
        self.assertEqual((r.cache, r.stale), ("hit", False))
        th.assert_not_called()

    def test_stale_serves_old_and_background_lands(self):
        io, state = self._io(payload={"data": "old"}, ts=time.time() - 120)
        r = swr.serve(_key(), 60, io, domain="k1")
        self.assertEqual((r.cache, r.stale, r.payload["data"]), ("stale", True, "old"))
        swr._inflight[_key()].join(5)
        self.assertEqual(state["payload"]["data"], "fresh")

    def test_inflight_dedup(self):
        io, _ = self._io(payload={"data": "old"}, ts=time.time() - 120)
        gate = threading.Event()
        orig_fetch = io.sync_fetch
        io = swr.SwrIO(read=io.read, sync_fetch=lambda: (gate.wait(5), orig_fetch())[1])
        swr.serve(_key(), 60, io, domain="k1")
        swr.serve(_key(), 60, io, domain="k1")
        alive = [t for t in swr._inflight.values() if t.is_alive()]
        self.assertEqual(len(alive), 1)
        gate.set()
        for t in alive:
            t.join(5)

    def test_cold_sync_fetch(self):
        io, _ = self._io(payload=None)
        r = swr.serve(_key(), 60, io)
        self.assertEqual((r.cache, r.stale, r.payload["data"]), ("miss", False, "fresh"))

    def test_cold_double_check_returns_fresh(self):
        io, state = self._io(payload=None)
        reads = []
        def read():
            reads.append(1)
            if len(reads) == 1:
                return None  # 初次检查：无缓存
            return ({"data": "concurrent"}, time.time())  # 锁内双检：另一线程已刷新
        calls = []
        io = swr.SwrIO(read=read,
                       sync_fetch=lambda: (calls.append(1), {"data": "fresh"})[1])
        r = swr.serve(_key(), 60, io)
        self.assertEqual((r.cache, r.payload["data"]), ("hit", "concurrent"))
        self.assertEqual(calls, [])

    def test_require_fresh_skips_stale_serve_and_double_check(self):
        io, _ = self._io(payload={"data": "old"}, ts=time.time() - 120)
        calls = []
        orig = io.sync_fetch
        io = swr.SwrIO(read=io.read, sync_fetch=lambda: (calls.append(1), orig())[1])
        r = swr.serve(_key(), 60, io, require_fresh=True)
        self.assertEqual((r.cache, r.payload["data"]), ("miss", "fresh"))
        self.assertEqual(len(calls), 1)

    def test_hit_when_customized(self):
        io, _ = self._io(payload={"data": {"a": 1}, "requested": ["a"]})
        r = swr.serve(_key(), 60, io,
                      hit_when=lambda p, ts: set(["a", "b"]).issubset(p["requested"]))
        self.assertEqual(r.cache, "miss")  # 新鲜但覆盖不全 → 进同步路径

    def test_serve_stale_on_cold_failure(self):
        io, _ = self._io(payload={"data": "old"}, ts=time.time() - 120,
                         fetch=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # fetch 抛错但 ts 已过期 → stale 分支先命中；构造 fresh-but-incomplete 场景：
        io2, _ = self._io(payload={"data": "old"}, ts=time.time(),
                          fetch=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        r = swr.serve(_key(), 60, io2, serve_stale_on_cold_failure=True,
                      hit_when=lambda p, ts: False)
        self.assertEqual((r.cache, r.stale), ("stale", False))  # 年龄新鲜 → stale=False

    def test_obsolete_reraised_even_with_stale_fallback(self):
        """spec §3：ObsoletePublication 冷路径原样抛，不被 serve_stale_on_cold_failure 兜底。"""
        io, _ = self._io(payload={"data": "old"}, ts=time.time(),
                         fetch=lambda: (_ for _ in ()).throw(cache_store.ObsoletePublication("old")))
        with self.assertRaises(cache_store.ObsoletePublication):
            swr.serve(_key(), 60, io, serve_stale_on_cold_failure=True,
                      hit_when=lambda p, ts: False)

    def test_cold_failure_propagates_without_fallback(self):
        io, _ = self._io(payload=None, fetch=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with self.assertRaises(RuntimeError):
            swr.serve(_key(), 60, io)

    def test_raise_when_blocked_not_counted(self):
        p = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.UNIFIED)
        for _ in range(2):
            p.record_failure(_key(), RuntimeError("x"))
        io, _ = self._io(payload=None)
        with self.assertRaises(swr.RefreshBlocked):
            swr.serve(_key(), 60, io, policy=p, raise_when_blocked=True)
        self.assertEqual(p.failure_count(_key()), 2)  # 退避中不计数

    def test_unified_counts_cold_failure_background_only_does_not(self):
        io, _ = self._io(payload=None, fetch=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        pu = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.UNIFIED)
        with self.assertRaises(RuntimeError):
            swr.serve(_key("u"), 60, io, policy=pu)
        self.assertEqual(pu.failure_count(_key("u")), 1)
        pb = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.BACKGROUND_ONLY)
        with self.assertRaises(RuntimeError):
            swr.serve(_key("b"), 60, io, policy=pb)
        self.assertEqual(pb.failure_count(_key("b")), 0)

    def test_background_failure_counted_and_obsolete_silent(self):
        p = swr.BackoffPolicy(2, 300, scope=swr.Scope.PER_KEY, count_source=swr.CountSource.BACKGROUND_ONLY)
        io, _ = self._io(payload={"data": "old"}, ts=time.time() - 120,
                         fetch=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        swr.serve(_key(), 60, io, policy=p, domain="k1")
        swr._inflight[_key()].join(5)
        self.assertEqual(p.failure_count(_key()), 1)
        io2, _ = self._io(payload={"data": "old"}, ts=time.time() - 120,
                          fetch=lambda: (_ for _ in ()).throw(cache_store.ObsoletePublication("old")))
        swr.serve(_key("obs"), 60, io2, policy=p, domain="obs")
        swr._inflight[_key("obs")].join(5)
        self.assertEqual(p.failure_count(_key("obs")), 0)

    def test_on_refreshed_called_with_reread_payload(self):
        seen = []
        io, _ = self._io(payload={"data": "old"}, ts=time.time() - 120)
        swr.serve(_key(), 60, io, on_refreshed=lambda key, payload: seen.append(payload), domain="k1")
        swr._inflight[_key()].join(5)
        self.assertEqual(seen, [{"data": "fresh"}])

    def test_trigger_suppressed_by_should_trigger(self):
        io, _ = self._io(payload={"data": "old"}, ts=time.time() - 120)
        with mock.patch.object(threading, "Thread") as th:
            r = swr.serve(_key(), 60, io, should_trigger=lambda key: False)
        self.assertEqual(r.cache, "stale")
        th.assert_not_called()


class TestLocks(unittest.TestCase):
    def test_unused_per_key_locks_are_released(self):
        """per-key 锁弱引用持有：按代码增长的 f10 键在无人使用后回收，不随标的数无界增长。"""
        import gc
        held = swr._lock_for(_key("held"))
        for n in range(100):
            swr._lock_for(_key(f"f10_{n}"))
        gc.collect()
        self.assertIs(swr._lock_for(_key("held")), held)
        self.assertNotIn(_key("f10_0"), swr._locks)


class TestImportGuard(unittest.TestCase):
    def test_obsolete_binds_real_cache_store(self):
        """完整树（cache_store 存在）：_Obsolete 必须绑定真实异常，守卫不降级。"""
        self.assertIs(swr._Obsolete, cache_store.ObsoletePublication)
        self.assertIsNotNone(swr.cache_store)


if __name__ == "__main__":
    unittest.main()
