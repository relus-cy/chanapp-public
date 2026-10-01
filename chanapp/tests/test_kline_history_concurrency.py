"""历史追赶线程带来的并发面（计划 2026-09-29 决策 D5，失败模式 F22）。

懒初始化「先查后建」与计数「读改写」在两个线程下的竞态：构造函数或读取里放一道 Barrier(2)，
没有加锁时两个线程必然同时进入；加锁后第二个线程进不来，Barrier 超时即放行（不靠运气复现）。"""
import os
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock
from collections import Counter

from chanapp.engine.kline import collector
from chanapp.engine.kline.providers import longbridge

WAIT_S = 0.5


def both(fn):
    """两个线程同时调用 fn，返回两个结果。"""
    out, errors = [None, None], []

    def run(i):
        try:
            out[i] = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    if errors:
        raise errors[0]
    return out


def rendezvous(barrier):
    try:
        barrier.wait(WAIT_S)
    except threading.BrokenBarrierError:
        pass


class SlowProvider:
    built = 0
    barrier = None

    def __init__(self):
        type(self).built += 1
        rendezvous(type(self).barrier)


class LazyInitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def test_provider_is_created_once(self):
        SlowProvider.built, SlowProvider.barrier = 0, threading.Barrier(2)
        c = collector.Collector(self.dir)                              # 未注入 provider：按工厂懒加载
        with unittest.mock.patch.dict(collector._FACTORIES, {"slow": (__name__, "SlowProvider")}):
            a, b = both(lambda: c.provider("slow"))
        self.assertEqual(SlowProvider.built, 1)
        self.assertIs(a, b)

    def test_budget_is_created_once(self):
        built, barrier = [], threading.Barrier(2)

        class SlowBudget(collector.Budget):
            def __init__(self, *args, **kw):
                built.append(1)
                rendezvous(barrier)
                super().__init__(*args, **kw)

        c = collector.Collector(self.dir)
        with unittest.mock.patch.object(collector, "Budget", SlowBudget):
            a, b = both(lambda: c._budget("mairui"))
        self.assertEqual(len(built), 1)
        self.assertIs(a, b)


class BudgetConnectionTests(unittest.TestCase):
    def test_budget_opens_its_connection_once(self):
        # 调度线程读用量（ratio）与另一线程扣额度（take）同时首次访问：只开一条连接，读写都在额度锁内
        from chanapp.engine.kline import facts
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        opened, barrier = [], threading.Barrier(2)

        def factory():
            opened.append(1)
            rendezvous(barrier)
            conn = facts.open_facts(os.path.join(tmp.name, facts.DB_NAME))
            self.addCleanup(conn.close)
            return conn

        budget = collector.Budget(factory, source="mairui", per_minute=100, per_day=100, reserve=0.1)
        both(budget.used)
        self.assertEqual(len(opened), 1)


class BreakerCountTests(unittest.TestCase):
    def test_concurrent_failures_are_all_counted(self):
        barrier = threading.Barrier(2)

        class Racy(Counter):
            def __missing__(self, key):
                rendezvous(barrier)                                     # 读到旧值后让另一个线程也读
                return 0

        breaker = collector.Breaker(10, 300, lambda: 0.0)
        breaker.failures = Racy()
        both(lambda: breaker.record("mairui", False))
        self.assertEqual(breaker.failures["mairui"], 2)


class LongbridgeContextTests(unittest.TestCase):
    def test_sdk_context_is_created_once(self):
        built, barrier = [], threading.Barrier(2)

        class QuoteContext:
            def __init__(self, config):
                built.append(config)
                rendezvous(barrier)

        class Config:
            @staticmethod
            def from_apikey(*creds, **kw):
                return creds

        openapi = types.ModuleType("longbridge.openapi")
        openapi.Config, openapi.QuoteContext = Config, QuoteContext
        package = types.ModuleType("longbridge")
        package.openapi = openapi
        env = {name: "x" for name in longbridge._CRED_NAMES}
        with unittest.mock.patch.dict(sys.modules, {"longbridge": package, "longbridge.openapi": openapi}), \
                unittest.mock.patch.dict(os.environ, env):
            client = longbridge.SdkClient()
            a, b = both(client._context)
        self.assertEqual(len(built), 1)
        self.assertIs(a, b)


if __name__ == "__main__":
    unittest.main()
