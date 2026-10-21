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

    def test_past_hk_half_days_before_vendor_calendar_get_morning_session(self):
        # 回归（2026-10-21）：供应商年表只回答最近一年，更早的半日市只有日线推出的开市行、没有会话，
        # 覆盖判定按全日要 11 槽，含半日市的月份永远不达标、转 known_gap，页面把这些日子标成不完整
        conn = self.c.conn()
        days = ("2024-02-09", "2024-12-23", "2024-12-24", "2024-12-31", "2025-01-28", "2025-12-24", "2025-12-31")
        facts.commit_day_rows(conn, [hk_day(d) for d in days], market="HK", kind="stock", item="day_history",
                              source="longbridge", binding_gen=1, today="2026-09-28")
        self.c.refresh_calendar("HK", self.now)          # 今年与去年的年表 + 日线推导 + 一年以前按规则
        morning = lambda d: {f"{d} {t}" for t in ("10:00", "10:30", "11:00", "11:30", "12:00")}   # noqa: E731
        for d in ("2024-02-09", "2024-12-24", "2024-12-31", "2025-01-28"):     # 一年以前：按港交所规则
            self.assertEqual(calendar.required_slots(conn, "HK", "m30", d), morning(d), d)
        for d in ("2025-12-24", "2025-12-31"):                                   # 一年以内：去年年表给出
            self.assertEqual(calendar.required_slots(conn, "HK", "m30", d), morning(d), d)
        self.assertEqual(len(calendar.required_slots(conn, "HK", "m30", "2024-12-23")), 11)

    def test_startup_rearm_recognizes_recent_half_day_before_any_calendar_fetch(self):
        # 回归（2026-10-21）：启动重开 known_gap 只推导、不取年表；一年以内的去年半日市（年表还没取到）若不靠规则，
        # 重开的 2025-12 月份在当天日历刷新前就按 11 槽判定，再白耗 5 次尝试转回 known_gap
        conn = self.c.conn()
        facts.commit_day_rows(conn, [hk_day(d) for d in ("2025-12-23", "2025-12-24", "2025-12-31")], market="HK",
                              kind="stock", item="day_history", source="longbridge", binding_gen=1,
                              today="2026-09-28")
        self.c.rearm_known_gaps()
        for d in ("2025-12-24", "2025-12-31"):
            self.assertEqual(calendar.required_slots(conn, "HK", "m30", d),
                             {f"{d} {t}" for t in ("10:00", "10:30", "11:00", "11:30", "12:00")}, d)
        self.assertEqual(len(calendar.required_slots(conn, "HK", "m30", "2025-12-23")), 11)

    def test_scheduled_half_day_rule_skips_weekend_eves(self):
        self.assertEqual([d for d in ("2022-12-24", "2023-12-29", "2024-12-24", "2027-02-05", "2028-12-31")
                          if calendar.hk_scheduled_half_day(d)], ["2024-12-24", "2027-02-05"])


if __name__ == "__main__":
    unittest.main()
