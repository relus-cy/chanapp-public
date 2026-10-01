"""pytdx raw 冷备：13:00→11:30、占位过滤、回绕报错、不复权、个股日线不在范围（spec §5.2、§8，D11a）。

录制 fixture 驱动，不联网、不导入 pytdx。
"""
import copy
import json
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline.providers import pytdx_raw, raw

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw" / "pytdx"


def _rows(name):
    return json.loads((FIX / name).read_text())["rows"]


class FakeTdx:
    """模拟 pytdx 分页：offset 0 为最新一页；页内按时间升序（与 pytdx 一致）。"""

    def __init__(self, rows, page=None):
        self.rows, self.page, self.calls = rows, page, []

    def __call__(self, kind, category, market, num, offset, count):
        self.calls.append((kind, category, market, num, offset, count))
        size = self.page or count
        end = len(self.rows) - offset
        if end <= 0:
            return []
        return copy.deepcopy(self.rows[max(0, end - min(size, count)):end])


class PytdxRawTests(unittest.TestCase):
    NOW = datetime(2026, 9, 15, 10, 0)

    def history(self, rows, code="sz300209", start="2026-09-14 09:30", end="2026-09-14 15:00", now=None):
        q = FakeTdx(rows)
        out = pytdx_raw.PytdxRawProvider(query=q).minute_history(code, "m5", start, end, now=now or self.NOW)
        return out, q

    def test_1300_label_normalised_to_1130_and_unique(self):
        rows, _ = self.history(_rows("m5-handwritten.json"))
        labels = [r.slot_end[11:] for r in rows]
        self.assertEqual(labels, ["09:35", "11:25", "11:30", "13:05"])
        self.assertEqual(len({r.slot_end for r in rows}), len(rows))

    def test_both_1130_and_1300_on_same_day_is_error(self):
        data = _rows("m5-handwritten.json")
        extra = dict(data[2], datetime="2026-09-14 11:30")
        with self.assertRaises(raw.ProviderError):
            self.history(data[:2] + [extra] + data[2:])

    def test_placeholder_rows_dropped(self):
        rows, _ = self.history(_rows("m5-handwritten.json"))
        self.assertNotIn("2026-09-14 13:10", {r.slot_end for r in rows})
        self.assertTrue(all(r.volume >= 1 for r in rows))

    def test_volume_wraparound_raises(self):
        data = _rows("m5-handwritten.json")
        data[1]["vol"] = float(2 ** 32)
        with self.assertRaises(raw.ProviderError):
            self.history(data)

    def test_recorded_index_m5_is_raw_share_closed(self):
        rows, q = self.history(_rows("m5-sh000001-20260811.json"), code="sh000001",
                               start="2026-08-11 09:30", end="2026-08-11 15:00")
        self.assertEqual(q.calls[0][:4], ("index", 0, 1, "000001"))
        self.assertEqual(len(rows), 48)
        self.assertEqual(rows[23].slot_end, "2026-08-11 11:30")
        self.assertEqual({(r.volume_unit, r.state, r.trade_state) for r in rows}, {("share", "closed", "traded")})

    def test_stock_m5_uses_security_bars(self):
        rows, q = self.history(_rows("m5-sz300209-20260811.json"), code="sz300209",
                               start="2026-08-11 09:30", end="2026-08-11 15:00")
        self.assertEqual(q.calls[0][:4], ("stock", 0, 0, "300209"))
        self.assertEqual(len(rows), 48)

    def test_history_filters_to_requested_range(self):
        rows, _ = self.history(_rows("m5-sh000001-20260811.json"), code="sh000001",
                               start="2026-08-11 13:00", end="2026-08-11 14:00")
        self.assertEqual((rows[0].slot_end, rows[-1].slot_end), ("2026-08-11 13:05", "2026-08-11 14:00"))

    def test_history_pages_back_until_start(self):
        data = _rows("m5-sh000001-20260811.json")
        q = FakeTdx(data, page=10)
        rows = pytdx_raw.PytdxRawProvider(query=q, page=10).minute_history(
            "sh000001", "m5", "2026-08-11 09:30", "2026-08-11 15:00", now=self.NOW)
        self.assertEqual(len(rows), 48)
        self.assertEqual([c[4] for c in q.calls], [0, 10, 20, 30, 40])   # 第 5 页不足一页即见底
        q2 = FakeTdx(data, page=10)
        rows = pytdx_raw.PytdxRawProvider(query=q2, page=10).minute_history(
            "sh000001", "m5", "2026-08-11 14:00", "2026-08-11 15:00", now=self.NOW)
        self.assertEqual([c[4] for c in q2.calls], [0, 10])   # 已翻过起点即停
        self.assertEqual((len(rows), rows[0].slot_end), (13, "2026-08-11 14:00"))   # 范围两端闭区间，同麦蕊

    def test_minute_live_keeps_today_as_forming(self):
        data = _rows("m5-sh000001-20260811.json")
        q = FakeTdx(data)
        rows = pytdx_raw.PytdxRawProvider(query=q).minute_live("sh000001", "m5", now=datetime(2026, 8, 11, 15, 5))
        self.assertEqual(len(rows), 48)
        self.assertEqual({r.state for r in rows}, {"forming"})
        none = pytdx_raw.PytdxRawProvider(query=FakeTdx(data)).minute_live(
            "sh000001", "m5", now=datetime(2026, 8, 12, 9, 40))
        self.assertEqual(none, [])

    def test_index_day_is_lot_with_previous_close(self):
        q = FakeTdx(_rows("day-sh000001-202609.json"))
        rows = pytdx_raw.PytdxRawProvider(query=q).day_history("sh000001", "2026-09-02", "2026-09-11")
        self.assertEqual(q.calls[0][:4], ("index", 9, 1, "000001"))
        self.assertEqual(rows[0].trade_date, "2026-09-02")
        self.assertIsNotNone(rows[0].pc)
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur.pc, prev.close)
        self.assertEqual({(r.volume_unit, r.currency, r.sf, r.provenance) for r in rows},
                         {("lot", "CNY", 0, "final")})

    def test_stock_day_is_out_of_scope(self):
        q = FakeTdx(_rows("day-sh000001-202609.json"))
        with self.assertRaises(raw.ProviderUnsupported):
            pytdx_raw.PytdxRawProvider(query=q).day_history("sz300209", "2026-09-01", "2026-09-11")
        self.assertEqual(q.calls, [])

    def test_only_m5_minutes(self):
        with self.assertRaises(raw.ProviderUnsupported):
            self.history_freq("m30")

    def history_freq(self, freq):
        return pytdx_raw.PytdxRawProvider(query=FakeTdx([])).minute_history(
            "sh000001", freq, "2026-08-11 09:30", "2026-08-11 15:00", now=self.NOW)

    def test_host_breaker_opens_after_three_failures(self):
        state = pytdx_raw._HostState()
        for _ in range(pytdx_raw.HOST_MAX_FAILURES):
            self.assertTrue(state.allow())
            state.record_failure()
        self.assertFalse(state.allow())
        state.cool_until = 0
        self.assertTrue(state.allow())

    def test_unsorted_page_sorted_and_day_placeholder_filtered(self):
        # 迁自旧 test_kline_providers：输出按时间升序；日线盘前占位（denormal vol）同样过滤
        data = _rows("day-sh000001-202609.json")
        placeholder = dict(data[-1], datetime="2026-09-14 15:00", vol=5.877471754111438e-39)
        q = FakeTdx(list(reversed(data)) + [placeholder])
        rows = pytdx_raw.PytdxRawProvider(query=q).day_history("sh000001", "2026-09-01", "2026-09-30")
        self.assertEqual([r.trade_date for r in rows], sorted(r.trade_date for r in rows))
        self.assertNotIn("2026-09-14", {r.trade_date for r in rows})
        rows, _ = self.history(list(reversed(_rows("m5-handwritten.json"))))
        self.assertEqual([r.slot_end[11:] for r in rows], ["09:35", "11:25", "11:30", "13:05"])

    def test_injected_query_connection_failure_is_connection_error(self):
        def down(*args):
            raise TimeoutError("timed out")
        with self.assertRaises(raw.ProviderConnectionError):
            pytdx_raw.PytdxRawProvider(query=down).day_history("sh000001", "2026-09-01", "2026-09-11")

    def test_host_pool_all_hosts_unreachable_is_connection_error(self):
        pool = pytdx_raw._HostPool()

        def refuse():
            raise ConnectionRefusedError("refused")
        pool._ensure_conn = refuse
        with self.assertRaises(raw.ProviderConnectionError):
            pool.query("index", 9, 1, "000001", 0, 10)

    def test_host_pool_empty_data_everywhere_is_not_connection_error(self):
        pool = pytdx_raw._HostPool()

        class Api:
            def get_index_bars(self, *args):
                return []
        pool._ensure_conn = lambda: setattr(pool, "_api", Api())
        with self.assertRaises(raw.ProviderError) as cm:
            pool.query("index", 9, 1, "000001", 0, 10)
        self.assertNotIsInstance(cm.exception, raw.ProviderConnectionError)

    def test_market_num_rejects_hk(self):
        with self.assertRaises(raw.ProviderError):
            pytdx_raw._market_num("hk00700")

    def test_latest_trading_day_for_host_freshness_gate(self):
        f = pytdx_raw._latest_trading_day
        self.assertEqual(f(datetime(2026, 9, 14, 9, 29)), "2026-09-11")   # 周一开盘前 → 上周五
        self.assertEqual(f(datetime(2026, 9, 14, 9, 30)), "2026-09-14")
        self.assertEqual(f(datetime(2026, 9, 13, 12, 0)), "2026-09-11")   # 周日 → 周五

    def test_category_mapping(self):
        self.assertEqual(pytdx_raw.CATEGORY, {"m5": 0, "m15": 1, "m30": 2, "m60": 3, "day": 9})
        self.assertEqual(pytdx_raw._market_num("sh000001"), (1, "000001"))
        self.assertEqual(pytdx_raw._market_num("sz300209"), (0, "300209"))


if __name__ == "__main__":
    unittest.main()
