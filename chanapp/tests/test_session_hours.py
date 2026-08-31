"""Task 5: 交易时段判断（engine/session.py），离线可跑。"""
import unittest
from datetime import datetime

from chanapp.engine.session import is_session_open


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
        self.assertTrue(is_session_open(datetime(2026, 8, 21, 16, 5), "hk"))

    def test_unknown_market(self):
        with self.assertRaises(ValueError):
            is_session_open(datetime(2026, 8, 21, 10, 30), "us")


if __name__ == "__main__":
    unittest.main()
