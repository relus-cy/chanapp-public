"""原始行契约与市场会话槽位（spec §5.2、§6.2）。"""
import unittest

from chanapp.engine.kline import rows, sessions


class RowsTests(unittest.TestCase):
    def test_market_and_kind(self):
        self.assertEqual(rows.market_of("sh600036"), "CN")
        self.assertEqual(rows.market_of("hk00700"), "HK")
        self.assertEqual(rows.kind_of("sh000001"), "index")
        self.assertEqual(rows.kind_of("sz399006"), "index")
        self.assertEqual(rows.kind_of("sz000001"), "stock")
        self.assertEqual(rows.kind_of("hk00700"), "stock")
        with self.assertRaises(ValueError):
            rows.market_of("us.AAPL")

    def test_volume_to_shares(self):
        self.assertEqual(rows.to_shares(4167.0, "lot"), 416700.0)
        self.assertEqual(rows.to_shares(10.0, "share"), 10.0)
        self.assertIsNone(rows.to_shares(None, "lot"))
        with self.assertRaises(KeyError):
            rows.to_shares(1.0, "hand")

    def test_minute_row_has_no_pc_field(self):
        # 分钟行的供应商 pc 是上一根收盘，不得进入因子（Review Focus 3）
        self.assertNotIn("pc", rows.RawMinuteRow.__dataclass_fields__)


class SessionTests(unittest.TestCase):
    def test_cn_grids(self):
        m5 = sessions.slots("CN", "m5")
        self.assertEqual(len(m5), 48)
        self.assertEqual((m5[0], m5[23], m5[24], m5[-1]), ("09:35", "11:30", "13:05", "15:00"))
        self.assertEqual(sessions.slots("CN", "m30"),
                         ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"))
        self.assertEqual(sessions.slots("CN", "m60"), ("10:30", "11:30", "14:00", "15:00"))
        self.assertEqual(len(sessions.slots("CN", "m15")), 16)

    def test_hk_grids_keep_short_morning_bucket(self):
        self.assertEqual(sessions.slots("HK", "m30"),
                         ("10:00", "10:30", "11:00", "11:30", "12:00",
                          "13:30", "14:00", "14:30", "15:00", "15:30", "16:00"))
        self.assertEqual(sessions.slots("HK", "m60"),
                         ("10:30", "11:30", "12:00", "14:00", "15:00", "16:00"))

    def test_bucket_end(self):
        self.assertEqual(sessions.bucket_end("CN", "m60", "09:35"), "10:30")
        self.assertEqual(sessions.bucket_end("CN", "m60", "11:30"), "11:30")
        self.assertEqual(sessions.bucket_end("CN", "m60", "13:05"), "14:00")
        self.assertEqual(sessions.bucket_end("CN", "m30", "11:25"), "11:30")
        self.assertEqual(sessions.bucket_end("HK", "m60", "12:00"), "12:00")
        self.assertEqual(sessions.bucket_end("HK", "m60", "13:30"), "14:00")
        self.assertIsNone(sessions.bucket_end("CN", "m60", "12:30"))   # 午休
        self.assertIsNone(sessions.bucket_end("CN", "m60", "09:30"))   # 开盘前（竞价归属待 P3）

    def test_week_start_is_stable_across_year_end(self):
        # 2024-12-30（周一）与 2025-01-03（周五）同一自然周，%Y-W%W 会切开
        self.assertEqual(sessions.week_start("2024-12-30"), "2024-12-30")
        self.assertEqual(sessions.week_start("2025-01-03"), "2024-12-30")
        self.assertEqual(sessions.week_start("2025-01-05"), "2024-12-30")
        self.assertEqual(sessions.week_start("2025-01-06"), "2025-01-06")
