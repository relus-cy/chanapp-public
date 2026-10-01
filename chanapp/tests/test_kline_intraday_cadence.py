"""盘中增量节奏：个股与指数都是每 60 秒一轮（目标 2026-09-29 第三阶段统一 60 秒；此前个股 30 秒、指数 60 秒）。

失败模式：① 指数仍每轮都取；② 个股比 60 秒更密或更疏；③ 指数被一直跳过；④ 额度接近上限时间隔不随之加倍；
⑤ 收盘后 1 分钟的定格轮被节流跳过，指数右栏停在收盘集合竞价前的值直到定稿。
跨交易日重置不单测：节奏状态挂在（市场，日期）的调度状态上，新的一天天然从空开始。"""
import tempfile
import unittest
import unittest.mock
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from chanapp.engine.kline import collector, config
from chanapp.tests.test_kline_collector import Clock, FakeProvider, _enable_collector


class Live(FakeProvider):
    def __init__(self):
        super().__init__()
        self.at = {}

    def minute_live(self, code, fact_freq, *, now):
        self.calls.append(("live", code))
        self.at.setdefault(code, []).append(now)
        return []

    def preopen_ref(self, code, trade_date):
        return None


class IntradayCadenceTests(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        start = datetime(2026, 9, 28, 10, 0, 0)
        self.provider = Live()
        self.collector = collector.Collector(
            Path(tmp.name), providers={"mairui": self.provider}, clock=Clock(start.timestamp()),
            watchlist_fn=lambda: ["sh600036", "sz000002", "sh000001", "sz399006"])
        self.start = start

    def run_minutes(self, minutes, start=None):
        """调度线程每 5 秒一轮 tick，跑 minutes 分钟，返回各代码的 minute_live 次数。"""
        start = start or self.start
        for step in range(minutes * 12):
            self.collector.tick(start + timedelta(seconds=5 * step))
        return Counter(code for kind, code in self.provider.calls if kind == "live")

    def test_indexes_and_stocks_every_60s(self):
        calls = self.run_minutes(10)
        self.assertEqual(calls["sh600036"], 10)          # 10 分钟 10 轮
        self.assertEqual(calls["sz000002"], 10)
        self.assertEqual(calls["sh000001"], 10)          # 指数没有被饿死
        self.assertEqual(calls["sz399006"], 10)

    def test_index_interval_doubles_with_quota_slowdown(self):
        with unittest.mock.patch.object(config, "QUOTA_SLOWDOWN_RATIO", -1.0):   # 用量比恒高于阈值
            calls = self.run_minutes(10)
        self.assertEqual(calls["sh600036"], 5)           # 个股 120 秒
        self.assertEqual(calls["sh000001"], 5)           # 指数 120 秒

    def test_close_minute_is_not_throttled(self):
        # 轮次相位让指数在 15:00:05 取过一次：收盘后 1 分钟内的下一轮（15:00:35）仍要取，拿到含收盘竞价的末根
        for start, close in ((datetime(2026, 9, 28, 11, 28, 5), "11:30:30"),
                             (datetime(2026, 9, 28, 14, 58, 5), "15:00:30")):
            self.run_minutes(4, start=start)
            last = max(self.provider.at["sh000001"])
            self.assertGreaterEqual(last.time().isoformat(), close)
            self.assertEqual(max(self.provider.at["sh600036"]), last)      # 与个股同一轮定格


if __name__ == "__main__":
    unittest.main()
