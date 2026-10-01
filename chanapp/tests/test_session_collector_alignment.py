"""前端交易时段（engine/session.py）必须覆盖采集器盘中窗口：采集器还在取盘中数据时，
页面不能已显示「已收盘」并停止自动刷新。只调用不修改采集器。"""
import unittest
from datetime import datetime, timedelta

from chanapp.engine import session
from chanapp.engine.kline import collector


class SessionCoversCollectorIntraday(unittest.TestCase):
    def setUp(self):
        self.addCleanup(session.set_calendar_hook, session._calendar_hook)
        session.set_calendar_hook(lambda market, day: True)

    def test_every_intraday_second_is_inside_the_session(self):
        day = datetime(2026, 9, 21)                      # 周一
        for market in ("CN", "HK"):
            gaps = []
            t = day.replace(hour=9)
            while t < day.replace(hour=17):
                modes = collector.due_modes(t, market=market, is_trading_day=True, state={})
                if "INTRADAY" in modes and not session.is_session_open(t, market.lower()):
                    gaps.append(t.strftime("%H:%M:%S"))
                t += timedelta(seconds=30)
            with self.subTest(market=market):
                self.assertEqual(gaps, [], f"{market} 采集器盘中取数时前端判为已收盘")


if __name__ == "__main__":
    unittest.main()
