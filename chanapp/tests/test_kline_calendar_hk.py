"""港股交易日历接线：采集器每日刷新后，港股假日与半日市对今天和未来生效；
从定稿日线推导过去开市日时不抹掉供应商给的半日市会话。只调用采集器，不修改。"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from chanapp.engine.kline import calendar, collector, facts
from chanapp.engine.kline.providers import longbridge
from chanapp.engine.kline.rows import CalendarRow, RawDayRow
from chanapp.tests.test_kline_provider_longbridge import FakeCalendarClient

TODAY = date(2026, 9, 29)
HALF_SLOTS = {f"2026-12-24 {t}" for t in ("10:00", "10:30", "11:00", "11:30", "12:00")}


def hk_day(day):
    return RawDayRow("hk00700", day, 1, 1, 1, 1, 1, "share", 1, "HKD", 1, 0, "final", "b")


class HkCalendarRefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        now = datetime(2026, 9, 29, 8, 31)
        provider = longbridge.LongbridgeProvider(client=FakeCalendarClient(TODAY), today=lambda: TODAY)
        self.c = collector.Collector(Path(tmp.name), providers={"longbridge": provider},
                                     clock=lambda: now.timestamp())
        self.now = now

    def test_refresh_marks_hk_holiday_closed_for_today_and_future(self):
        self.c.refresh_calendar("HK", self.now)
        conn = self.c.conn()
        self.assertFalse(calendar.is_trading_day(conn, "HK", "2026-10-01"))
        self.assertTrue(calendar.is_trading_day(conn, "HK", "2026-10-02"))
        self.assertFalse(self.c._trading("HK", datetime(2026, 10, 1, 10, 0)), "港股假日采集器不应按交易日运行")
        self.assertEqual(calendar.required_slots(conn, "HK", "m30", "2026-12-24"), HALF_SLOTS)

    def test_past_derivation_keeps_vendor_half_day_sessions(self):
        conn = self.c.conn()
        with facts.write_txn(conn):
            calendar.store_rows(conn, [CalendarRow("HK", "2026-12-24", True, (("09:30", "12:00"),))],
                                source="longbridge")
        facts.commit_day_rows(conn, [hk_day("2026-12-24")], market="HK", kind="stock", item="day_history",
                              source="longbridge", binding_gen=1, today="2026-12-28")
        with facts.write_txn(conn):
            calendar.derive_hk_past(conn)
        self.assertTrue(calendar.is_trading_day(conn, "HK", "2026-12-24"))
        self.assertEqual(calendar.required_slots(conn, "HK", "m30", "2026-12-24"), HALF_SLOTS,
                         "日线推导不得把半日市会话覆盖成全日")

    def test_vendor_half_day_sessions_apply_to_already_derived_past_days(self):
        # 首次刷新时过去的港股开市日已由日线推出（index_day），供应商的半日市会话仍要落上去
        conn = self.c.conn()
        facts.commit_day_rows(conn, [hk_day("2025-12-24")], market="HK", kind="stock", item="day_history",
                              source="longbridge", binding_gen=1, today="2025-12-29")
        with facts.write_txn(conn):
            calendar.derive_hk_past(conn)
            calendar.store_rows(conn, [CalendarRow("HK", "2025-12-24", True, (("09:30", "12:00"),)),
                                       CalendarRow("HK", "2025-12-23", True)], source="longbridge")
        self.assertEqual(len(calendar.required_slots(conn, "HK", "m30", "2025-12-24")), 5)
        row = conn.execute("SELECT source FROM calendar WHERE market='HK' AND date='2025-12-24'").fetchone()
        self.assertEqual(row["source"], "index_day", "开市事实仍以日线为准")


if __name__ == "__main__":
    unittest.main()
