"""交易时段判断（engine/session.py）与 /api/session，离线可跑。"""
import unittest
from datetime import datetime
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.api import main
from chanapp.engine import session
from chanapp.engine.session import is_session_open


def _use_hook(test, fn):
    """临时替换交易日历钩子，结束后恢复原值（K 线门面导入时注册过真钩子，不能清成 None）。"""
    test.addCleanup(session.set_calendar_hook, session._calendar_hook)
    session.set_calendar_hook(fn)


class TestSessionHours(unittest.TestCase):
    def test_cn_session(self):
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 10, 30), "cn"))   # 周五盘中
        self.assertFalse(is_session_open(datetime(2026, 8, 22, 10, 30), "cn"))  # 周六
        self.assertFalse(is_session_open(datetime(2026, 8, 21, 15, 30), "cn"))  # 收盘后

    def test_hk_session(self):
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 12, 0), "hk"))
        self.assertFalse(is_session_open(datetime(2026, 8, 21, 16, 30), "hk"))  # 港股收盘后
        self.assertFalse(is_session_open(datetime(2026, 8, 23, 12, 0), "hk"))   # 周日

    def test_session_edges(self):
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 9, 25), "cn"))    # 边界含
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 15, 5), "cn"))
        self.assertFalse(is_session_open(datetime(2026, 8, 21, 9, 24), "cn"))
        self.assertFalse(is_session_open(datetime(2026, 8, 21, 15, 6), "cn"))
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 9, 30), "hk"))
        # 港股收盘端覆盖采集器盘中窗口（到 16:11）并留缓冲
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 16, 10, 30), "hk"))
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 16, 15), "hk"))
        self.assertFalse(is_session_open(datetime(2026, 8, 21, 16, 16), "hk"))

    def test_calendar_hook_closes_holiday(self):
        # 钩子判定休市：工作日也不在交易时段
        _use_hook(self, lambda market, day: False)
        self.assertFalse(is_session_open(datetime(2026, 10, 1, 10, 30), "cn"))

    def test_calendar_hook_failure_or_gap_falls_back_to_weekdays(self):
        def broken(market, day):
            raise RuntimeError("calendar unavailable")

        _use_hook(self, broken)
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 10, 30), "cn"))
        session.set_calendar_hook(lambda market, day: None)      # 未覆盖该日
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 10, 30), "cn"))
        self.assertFalse(is_session_open(datetime(2026, 8, 22, 10, 30), "cn"))

    def test_unknown_market(self):
        with self.assertRaises(ValueError):
            is_session_open(datetime(2026, 8, 21, 10, 30), "us")


class TestSessionApi(unittest.TestCase):
    """GET /api/session：前端自动刷新与「交易中」状态的唯一口径，按东八区现在时刻计算。"""

    def setUp(self):
        self.client = TestClient(main.app)

    def _get(self, now):
        with mock.patch.object(main, "_session_now", return_value=now):
            return self.client.get("/api/session")

    def test_open_markets_on_a_trading_day(self):
        _use_hook(self, lambda market, day: None)
        response = self._get(datetime(2026, 8, 21, 10, 30))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"checked_at": "2026-08-21T10:30:00+08:00",
                                           "markets": {"cn": {"open": True}, "hk": {"open": True}}})

    def test_holiday_from_calendar_is_closed(self):
        _use_hook(self, lambda market, day: market != "cn")
        body = self._get(datetime(2026, 10, 1, 10, 30)).json()
        self.assertEqual(body["markets"], {"cn": {"open": False}, "hk": {"open": True}})

    def test_hk_open_through_collector_closing_minute(self):
        _use_hook(self, lambda market, day: None)
        body = self._get(datetime(2026, 8, 21, 16, 10, 30)).json()
        self.assertEqual(body["markets"], {"cn": {"open": False}, "hk": {"open": True}})


if __name__ == "__main__":
    unittest.main()
