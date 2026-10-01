"""右栏参考前收守卫（计划 2026-09-29 决策 D6，失败模式 F23）。

港股与指数没有盘前参考价，参考前收取最近一个已收盘交易日的收盘：只有它与今天之间的每个交易日都有可读日线行
（停牌行也算）才可用，否则参考前收与涨跌置空；落后的代码不能用更早的收盘价算涨跌。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import calendar, facts, views
from chanapp.engine.kline.rows import CalendarRow, RawDayRow, RawMinuteRow
from chanapp.tests.test_kline_collector import FACT

TODAY = "2026-09-28"                                 # 周一
NOW = datetime(2026, 9, 28, 10, 0)
DAYS = ["2026-09-23", "2026-09-24", "2026-09-25", TODAY]


class QuoteGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)
        with facts.write_txn(self.conn):
            calendar.store_rows(self.conn, [CalendarRow(m, d, True) for m in ("CN", "HK") for d in DAYS], source="t")

    def day(self, code, d, close, *, sf=0):
        hk = code.startswith("hk")
        row = RawDayRow(code, d, close, close, close, close, 0 if sf else 100.0, "share" if hk else "lot",
                        None if hk else 1.0, "HKD" if hk else "CNY", close, sf, "final", "b")
        facts.commit_day_rows(self.conn, [row], market="HK" if hk else "CN", kind="index" if code.startswith("sh000")
                              else "stock", item="day_history", source="longbridge" if hk else "mairui",
                              binding_gen=1, today=TODAY)

    def live(self, code, price):
        hk = code.startswith("hk")
        slot, fact = (f"{TODAY} 10:00", "m30") if hk else (f"{TODAY} 09:45", FACT)
        facts.commit_minute_rows(self.conn, [RawMinuteRow(code, TODAY, slot, price, price, price, price, 10.0,
                                                          "share" if hk else "lot", None if hk else 1.0, "forming",
                                                          "traded", "b")],
                                 market="HK" if hk else "CN", kind="index" if code.startswith("sh000") else "stock",
                                 item="minute_live", fact_freq=fact, source="longbridge" if hk else "mairui",
                                 binding_gen=1, today=TODAY)
        return views.quote(self.conn, code, now=NOW)

    def test_lagging_index_has_no_reference_close(self):
        self.day("sh000001", "2026-09-23", 19.8)
        self.day("sh000001", "2026-09-24", 19.9)                      # 缺 09-25：最近收盘已不是上一交易日
        q = self.live("sh000001", 22.0)
        self.assertEqual((q["price"], q["price_label"], q["pc"], q["pct"]), (22.0, "最新", None, None))

    def test_lagging_hk_stock_has_no_reference_close(self):
        self.day("hk00700", "2026-09-24", 100.0)
        q = self.live("hk00700", 110.0)
        self.assertEqual((q["pc"], q["pct"]), (None, None))

    def test_suspended_previous_day_is_not_lagging(self):
        self.day("sh000001", "2026-09-24", 19.9)
        self.day("sh000001", "2026-09-25", 19.9, sf=1)                # 停牌行也算可读日线
        q = self.live("sh000001", 21.89)
        self.assertEqual((q["pc"], q["pct"]), (19.9, 10.0))

    def test_up_to_date_uses_previous_close(self):
        self.day("hk00700", "2026-09-24", 90.0)
        self.day("hk00700", "2026-09-25", 100.0)
        q = self.live("hk00700", 110.0)
        self.assertEqual((q["pc"], q["pct"]), (100.0, 10.0))


if __name__ == "__main__":
    unittest.main()
