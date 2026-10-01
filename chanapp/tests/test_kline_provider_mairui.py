"""麦蕊 provider：路径、参数、行映射、范围校验、危险入口、脱敏（spec §5.1、§5.2）。"""
import json
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline.providers import mairui, raw

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw" / "mairui"
LICENCE = "SECRET-LICENCE-123"


def fixture(name):
    rec = json.loads((FIX / name).read_text())
    return rec["status"], rec["body"]


class Transport:
    def __init__(self, response):
        self.response, self.calls = response, []

    def __call__(self, path, params, timeout):
        self.calls.append((path, params))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class MairuiTests(unittest.TestCase):
    def provider(self, response):
        t = Transport(response)
        return mairui.MairuiProvider(transport=t, licence=LICENCE), t

    def test_day_history_maps_raw_rows(self):
        p, t = self.provider(fixture("day-300209-2024q4.json"))
        rows = p.day_history("sz300209", "2024-10-01", "2025-01-31")
        self.assertEqual(t.calls[0], ("/hsstock/history/300209.SZ/d/n/{L}",
                                      {"st": "20241001", "et": "20250131"}))
        self.assertEqual({r.volume_unit for r in rows}, {"lot"})
        self.assertTrue(all(isinstance(r.sf, int) and r.provenance == "final" for r in rows))

    def test_index_uses_index_endpoint(self):
        p, t = self.provider(fixture("idx-day-000001-2024q4.json"))
        p.day_history("sh000001", "2024-09-01", "2025-01-31")
        self.assertEqual(t.calls[0][0], "/hsindex/history/000001.SH/d/{L}")

    def test_minute_history_iso_params_and_closed_state(self):
        p, t = self.provider(fixture("m5-600036-20240927.json"))
        rows = p.minute_history("sh600036", "m5", "2024-09-27 09:30", "2024-09-27 15:00",
                                now=datetime(2026, 9, 28, 10))
        self.assertEqual(t.calls[0][1], {"st": "2024-09-27 09:30:00", "et": "2024-09-27 15:00:00"})
        self.assertEqual(len(rows), 48)
        self.assertEqual({r.state for r in rows}, {"closed"})
        self.assertEqual(rows[23].slot_end, "2024-09-27 11:30")

    def test_minute_rows_drop_vendor_pc(self):
        p, _ = self.provider(fixture("m5-600036-20240927.json"))
        row = p.minute_history("sh600036", "m5", "2024-09-27 09:30", "2024-09-27 15:00",
                               now=datetime(2026, 9, 28, 10))[0]
        self.assertFalse(hasattr(row, "pc"))

    def test_out_of_range_rows_raise(self):
        p, _ = self.provider(fixture("day-300209-2024q4.json"))
        with self.assertRaises(raw.ProviderRangeError):
            p.day_history("sz300209", "2024-12-01", "2024-12-10")

    def test_server_error_for_new_listing(self):
        p, _ = self.provider(fixture("m5-688806-500.json"))
        with self.assertRaises(raw.ProviderServerError):
            p.minute_history("sh688806", "m5", "2026-08-01 09:30", "2026-08-31 15:00",
                             now=datetime(2026, 9, 28, 10))

    def test_minute_live_drops_rows_not_today(self):
        p, t = self.provider(fixture("latest-600036-lt2.json"))
        rows = p.minute_live("sh600036", "m5", now=datetime(2026, 9, 28, 9, 58))
        self.assertEqual(t.calls[0][1], {"lt": 2})
        # 09:40 已过去仍记 forming：盘中值只能由定稿的 history 行转为 closed
        self.assertEqual([(r.slot_end, r.state) for r in rows], [("2026-09-28 09:40", "forming")])

    def test_minute_live_empty_is_not_error(self):
        p, _ = self.provider((200, []))
        self.assertEqual(p.minute_live("sh600036", "m5", now=datetime(2026, 10, 1, 10)), [])

    def test_index_minute_live_uses_index_latest_and_drops_prior_day(self):
        # 09-28 V1 录制（P3 指数路径）：指数 latest 与个股同样推进；首行是上一交易日的停牌占位（sf=1），按当日过滤
        p, t = self.provider(fixture("latest-idx-000001-lt2.json"))
        rows = p.minute_live("sh000001", "m5", now=datetime(2026, 9, 28, 9, 31))
        self.assertEqual(t.calls[0], ("/hsindex/latest/000001.SH/5/{L}", {"lt": 2}))
        self.assertEqual([(r.slot_end, r.state, r.trade_state, r.close) for r in rows],
                         [("2026-09-28 09:35", "forming", "traded", 3878.41)])

    def test_preopen_ref_only_when_dated_today(self):
        p, _ = self.provider(fixture("real-600036.json"))
        self.assertIsNone(p.preopen_ref("sh600036", "2026-09-28"))
        row = p.preopen_ref("sh600036", "2026-09-24")
        self.assertEqual((row.provenance, row.pc, row.close), ("preopen", 40.6, None))

    def test_instrument_list_date(self):
        p, t = self.provider(fixture("instr-600036.json"))
        row = p.instrument("sh600036")
        self.assertEqual(t.calls[0][0], "/hsstock/instrument/600036.SH/{L}")
        self.assertEqual(row.list_date, "2002-04-09")   # 录制值 od 为 ISO 形式

    def test_calendar_marks_weekdays_closed_when_absent(self):
        p, _ = self.provider(fixture("cal-2026.json"))
        rows = {r.date: r.is_open for r in p.calendar(2026)}
        self.assertTrue(rows["2026-01-05"])
        self.assertFalse(rows["2026-01-01"])

    def test_truncated_or_empty_calendar_leaves_absent_weekdays_unknown(self):
        # 计划 A 决定 5：年表不完整时缺席工作日视为未知，不能写成休市而停掉这些日子的采集
        status, body = fixture("cal-2026.json")
        p, _ = self.provider((status, [d for d in body if d < "20260701"]))
        rows = {r.date: r.is_open for r in p.calendar(2026)}
        self.assertTrue(rows["2026-01-05"])
        self.assertNotIn("2026-01-01", rows)
        self.assertNotIn("2026-09-28", rows)
        p, _ = self.provider((status, []))
        self.assertEqual(p.calendar(2026), [])

    def test_errors_are_redacted(self):
        p, _ = self.provider(ConnectionError(f"GET https://x/{LICENCE} failed"))
        with self.assertRaises(raw.ProviderError) as ctx:
            p.day_history("sh600036", "2026-09-01", "2026-09-02")
        self.assertNotIn(LICENCE, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_transport_failure_is_connection_class_but_http_4xx_is_not(self):
        # 连接类失败冷却整个源；4xx 是标的级（如代码不存在），只退避该标的
        p, _ = self.provider(ConnectionError("timeout"))
        with self.assertRaises(raw.ProviderConnectionError):
            p.day_history("sh600036", "2026-09-01", "2026-09-02")
        p, _ = self.provider((404, {"msg": "no such code"}))
        with self.assertRaises(raw.ProviderError) as ctx:
            p.day_history("sh600036", "2026-09-01", "2026-09-02")
        self.assertNotIsInstance(ctx.exception, raw.ProviderConnectionError)

    def test_missing_licence_is_source_level(self):
        p = mairui.MairuiProvider(licence="")
        with self.assertRaises(raw.ProviderConnectionError):
            p.day_history("sh600036", "2026-09-01", "2026-09-02")

    def test_native_adjusted_paths_are_unreachable(self):
        source = Path(mairui.__file__).read_text()
        self.assertNotRegex(source, r"/(f|fr)/\{L\}")
