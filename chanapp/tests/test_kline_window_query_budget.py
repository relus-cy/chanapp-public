"""搜索查看的请求路径查询预算：窗口已覆盖时，一次图表请求在事实库上执行的 SQL 语句数有字面上限，且不随窗口天数线性增长。

经 FastAPI 路由走用户路径，采集器是真实实现（事实库在临时目录），只把数据源换成记录请求的假源、时钟钉住，
后台线程不启动。语句数在事实库连接工厂上用 sqlite3 的 trace 回调计数（请求线程、追赶线程与只读连接都算），
并按打开连接的模块分开记采集器自己的连接（窗口缺口与追赶判断在这些连接上执行）。
"""
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from fastapi.testclient import TestClient

from chanapp.engine import data as engine_data
from chanapp.engine.kline import collector, config, facts
from chanapp.tests import cache_support
from chanapp.tests.test_kline_collector import Clock, HKFullFake, _enable_collector, _list_since
from chanapp.tests.test_kline_viewing_tracking import A, INDEX, X, Recording, at

# 窗口已覆盖的一次 m60 图表请求：修复后实测共 322 条，其中采集器连接 37 条（逐日判断覆盖时共 2893 条、采集器
# 连接 2608 条）；预算约为实测的 1.5 倍
STATEMENT_BUDGET = 480
COLLECTOR_STATEMENT_BUDGET = 55
# 窗口天数翻倍时采集器连接上语句数的增量上限：修复后实测 0（逐日判断时 2548）
GROWTH_BOUND = 10


class WindowQueryBudget(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        cache_support.isolate_cache_dir(self, self.dir)
        cache_support.set_env(self, "ANALYSIS_CACHE_DIR", str(self.dir / "analysis"))
        watchlist = self.dir / "watchlist.json"
        watchlist.write_text(json.dumps([{"code": A, "name": "招商银行"}], ensure_ascii=False))
        cache_support.set_env(self, "WATCHLIST_PATH", str(watchlist))
        cache_support.set_env(self, "VIEW_LOG_PATH", str(self.dir / "views.sqlite"))
        _list_since(self.dir, [A, X, INDEX], "2010-01-04")
        self.statements, self.counting, self.count_lock = {}, False, threading.Lock()
        real_open = facts.open_facts

        def counting_open(path):
            owner = "collector" if sys._getframe(1).f_globals.get("__name__") == collector.__name__ else "reader"
            conn = real_open(path)
            conn.set_trace_callback(lambda _sql: self._trace(owner))
            return conn

        patcher = unittest.mock.patch.object(facts, "open_facts", counting_open)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock(at("2026-09-26", 20).timestamp())          # 周六晚：不在盘中、不定稿
        self.provider = Recording()
        from chanapp.api.main import _read_watchlist_raw
        worker = collector.Collector(self.dir, providers={"mairui": self.provider, "longbridge": HKFullFake()},
                                     clock=self.clock,
                                     watchlist_fn=lambda: [w["code"] for w in _read_watchlist_raw()])
        collector._shared.clear()
        collector._shared[str(self.dir.resolve())] = worker
        self.addCleanup(collector._shared.clear)
        engine_data._window_attempts.clear()
        self.addCleanup(engine_data._window_attempts.clear)
        from chanapp.api.main import app
        self.api = TestClient(app)                                     # 不进 with：不跑 lifespan，不起后台线程

    def _trace(self, owner):
        if self.counting:
            with self.count_lock:
                self.statements[owner] = self.statements.get(owner, 0) + 1

    def chart(self, code, freq):
        engine_data._window_attempts.clear()                          # 去掉门面节流，每次都走完整的读取与追赶判断
        self.clock.t += config.CATCHUP_THROTTLE_S + 1                 # 越过追赶节流（仍是周六晚）：每次都判断是否落后
        r = self.api.get(f"/api/chart?code={code}&freq={freq}")
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.json()

    def measured_chart(self, code, freq):
        """窗口已覆盖后的一次图表请求：(响应, 数据源请求数, {连接归属: 事实库语句数})。"""
        calls = len(self.provider.calls)
        self.statements, self.counting = {}, True
        try:
            body = self.chart(code, freq)
        finally:
            self.counting = False
        return body, len(self.provider.calls) - calls, dict(self.statements)

    def warm(self, code):
        """首开并补齐分析窗口（日线与分钟），直到再打开不再请求数据源。"""
        self.chart(code, "day")
        for _ in range(3):
            calls = len(self.provider.calls)
            self.chart(code, "m60")
            if len(self.provider.calls) == calls:
                return
        self.fail("分析窗口三次打开后仍在请求数据源")

    def test_covered_window_view_stays_within_statement_budget(self):
        self.warm(X)
        body, requests, statements = self.measured_chart(X, "m60")
        self.assertEqual(requests, 0)
        self.assertEqual(body["meta"]["bars"], 520)
        self.assertEqual(len(body["kline"]), 520)
        self.assertEqual(body["kline"][-1]["time"], "2026-09-25 15:00")
        self.assertLessEqual(sum(statements.values()), STATEMENT_BUDGET, statements)
        self.assertLessEqual(statements["collector"], COLLECTOR_STATEMENT_BUDGET, statements)

    def test_statement_count_does_not_grow_with_window_days(self):
        """分析窗口翻倍（日线约 2 年变 4 年、分钟约半年变一年）：采集器连接上的语句数只多一个小常数，不随天数线性增长。
        窗口长度只能经采集器参数 DEFAULT_WINDOW 改变（请求的 limit 不影响追赶判断的窗口），这里临时翻倍它；它同时
        翻倍图表读取的根数，读取侧（只读连接）的语句数不在本条的对象内。"""
        self.warm(X)
        _, requests, base = self.measured_chart(X, "m60")
        self.assertEqual(requests, 0)
        with unittest.mock.patch.object(config, "DEFAULT_WINDOW", config.DEFAULT_WINDOW * 2):
            calls = len(self.provider.calls)
            self.warm(X)
            self.assertTrue([c for c in self.provider.calls[calls:] if c[1] == X], "窗口翻倍后应补取更早的数据")
            body, requests, doubled = self.measured_chart(X, "m60")
        self.assertEqual(requests, 0)
        self.assertEqual(body["meta"]["bars"], 1040)
        self.assertLessEqual(doubled["collector"] - base["collector"], GROWTH_BOUND, (base, doubled))


if __name__ == "__main__":
    unittest.main()
