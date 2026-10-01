"""显示层 secid 映射（迁自 test_em_data.py 的 secid 用例）。"""
import unittest

from chanapp.engine.feeds.secid import secid


class SecidTests(unittest.TestCase):
    def test_markets(self):
        self.assertEqual(secid("sh000001"), "1.000001")
        self.assertEqual(secid("sz399006"), "0.399006")
        self.assertEqual(secid("hk00700"), "116.00700")

    def test_unknown_prefix_rejected(self):
        with self.assertRaises(ValueError):
            secid("bj430047")


if __name__ == "__main__":
    unittest.main()
