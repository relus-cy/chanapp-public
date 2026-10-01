"""F5 交易日历（spec §5.1 F5、执行计划 A 决定 5）。"""
import json
import tempfile
import unittest
from pathlib import Path

from chanapp.engine.kline import calendar, facts
from chanapp.engine.kline.rows import CalendarRow, RawDayRow


def index_day(date):
    return RawDayRow("sh000001", date, 1, 1, 1, 1, 1, "lot", 1, "CNY", 1, 0, "final", "b")


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = facts.open_facts(Path(self.tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)

    def test_past_from_index_future_from_vendor(self):
        facts.commit_day_rows(self.conn, [index_day("2024-09-27"), index_day("2024-09-30")],
                              market="CN", kind="index", item="day_history", source="mairui",
                              binding_gen=1, today="2026-09-28")
        with facts.write_txn(self.conn):
            calendar.derive_cn_past(self.conn)
            calendar.store_rows(self.conn, [CalendarRow("CN", "2026-10-09", True),
                                            CalendarRow("CN", "2026-10-01", False)], source="mairui")
        self.assertEqual(calendar.trading_days(self.conn, "CN", "2024-09-27", "2024-10-01"),
                         ["2024-09-27", "2024-09-30"])
        self.assertTrue(calendar.is_trading_day(self.conn, "CN", "2026-10-09"))
        self.assertFalse(calendar.is_trading_day(self.conn, "CN", "2026-10-01"))
        self.assertIsNone(calendar.is_trading_day(self.conn, "CN", "2031-01-02"))

    def test_past_closed_weekdays_derived_between_index_days_except_open_gaps(self):
        # 指数 09-27（五）、09-30（一）、10-08（二）有 final；10-01..10-07 的工作日由相邻指数日证明休市，
        # 但指数自身在 10-01..10-03 登记了未解决缺口时，这段只能算未知
        facts.commit_day_rows(self.conn, [index_day(d) for d in ("2024-09-27", "2024-09-30", "2024-10-08")],
                              market="CN", kind="index", item="day_history", source="mairui",
                              binding_gen=1, today="2026-09-28")
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, "sh000001", "day", "2024-10-01", "2024-10-03", "backfill")
            calendar.derive_cn_past(self.conn)
        self.assertIsNone(calendar.is_trading_day(self.conn, "CN", "2024-10-02"))
        self.assertFalse(calendar.is_trading_day(self.conn, "CN", "2024-10-04"))
        self.assertFalse(calendar.is_trading_day(self.conn, "CN", "2024-10-07"))
        self.assertTrue(calendar.is_trading_day(self.conn, "CN", "2024-10-08"))
        self.assertEqual(calendar.closed_days(self.conn, "CN", "2024-09-27", "2024-10-08"),
                         ["2024-10-04", "2024-10-07"])

    def test_export_matches_selfcheck_shape(self):
        with facts.write_txn(self.conn):
            calendar.store_rows(self.conn, [CalendarRow("CN", "2026-10-01", False),
                                            CalendarRow("CN", "2026-10-09", True),
                                            CalendarRow("HK", "2026-10-01", False)], source="t")
        out = Path(self.tmp.name) / "cal.json"
        calendar.export_selfcheck(self.conn, out, year=2026)
        data = json.loads(out.read_text())
        self.assertEqual(data["year"], 2026)
        self.assertIn("10-01", data["cn"]["closed"])
        self.assertNotIn("10-09", data["cn"]["closed"])
        self.assertIn("10-01", data["hk"]["closed"])
        self.assertEqual(data["cn"]["sessions"], [["09:30", "11:30"], ["13:00", "15:00"]])
        self.assertEqual(data["hk"]["half_days"], [])
