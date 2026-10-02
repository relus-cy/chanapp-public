"""显示层请求的市场标识映射。"""
import unittest

from chanapp.engine.display_feed import _secid as secid


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
