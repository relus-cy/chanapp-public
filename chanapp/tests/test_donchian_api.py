"""唐奇安通道经 /api/chart 下发（真实门面与事实库，采集器关闭）：两组参数、与 K 线逐根对齐、
盘中当日 bar 上的信号为 provisional、不复权个股不给。计算口径本身见 test_donchian.py。"""
import unittest
from datetime import date, timedelta

from fastapi.testclient import TestClient

from chanapp.engine import chart_payload
from chanapp.engine.kline import calendar, facts
from chanapp.engine.kline.rows import CalendarRow, RawDayRow, new_batch_id
from chanapp.tests.test_kline_facade import CODE, KW, FacadeBase, weekdays


class DonchianChartApiTests(FacadeBase):
    def setUp(self):
        super().setUp()
        self.days = weekdays(60, date.today() - timedelta(days=1))      # 历史日线连到昨天，前复权链不断
        with self.writer() as conn:
            calendar.store_rows(conn, [CalendarRow("CN", d, True) for d in self.days], source="t")
        self.pin_view_now("10:30")                         # 盘中：当日日线由形成中的分钟聚合
        self.commit_days(self.days)                        # 历史日线最高 11（close=10 × 1.1）
        prev_close = 10.0 + (len(self.days) - 1) % 5 * 0.1               # 与 commit_days 末日同值
        with self.writer() as conn:                        # 当日参考前收已确认（等于昨收，无除权），前复权才服务今天
            facts.commit_day_rows(conn, [RawDayRow(CODE, self.today, 12.0, 12.3, 11.9, 12.1, 1000.0, "lot", 1.0,
                                                   "CNY", prev_close, 0, "live", new_batch_id())],
                                  item="preopen_ref", today=self.today, **KW)
        self.commit_minutes(self.today, state="forming", base=12.0)    # 当日盘中已越过 20 日高点
        from chanapp.api.main import app
        self.client = TestClient(app)

    def chart(self, **params):
        r = self.client.get("/api/chart", params={"code": CODE, "freq": "day", **params})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_two_param_sets_aligned_with_kline(self):
        body = self.chart()
        dc, kline = body["donchian"], body["kline"]
        self.assertEqual(set(dc), {"20/10", "55/20"})
        rows = dc["20/10"]["rows"]
        self.assertEqual([r["time"] for r in rows], [k["time"] for k in kline])
        self.assertEqual(rows[-1]["entry"], max(k["high"] for k in kline[-21:-1]))
        self.assertEqual(rows[-1]["exit"], min(k["low"] for k in kline[-11:-1]))
        self.assertNotIn("forming", kline[-1])             # K 线行字段不变

    def test_signal_on_forming_bar_is_provisional(self):
        body = self.chart()
        today = body["kline"][-1]["time"]
        close_mode = body["donchian"]["20/10"]["modes"]["close"]
        last = close_mode["signals"][-1]
        self.assertEqual((last["time"], last["side"], last["kind"], last["status"]),
                         (today, "buy", "close", "provisional"))
        self.assertTrue(close_mode["state"]["holding"])
        self.assertTrue(close_mode["state"]["provisional"])
        for key in ("20/10", "55/20"):                     # 只有当日（形成中）bar 上的信号是 provisional
            for mode in ("close", "touch"):
                for sig in body["donchian"][key]["modes"][mode]["signals"]:
                    self.assertEqual(sig["status"], "provisional" if sig["time"] == today else "confirmed")

    def test_raw_stock_has_no_donchian(self):
        self.assertIsNone(self.chart(adjust="raw")["donchian"])


class DonchianForTests(unittest.TestCase):
    BARS = [{"dt": f"2026-01-{i + 1:02d}", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1}
            for i in range(25)]

    def test_index_served_raw_still_gets_donchian(self):
        self.assertIsNotNone(chart_payload.donchian_for("sh000001", {"adjust": "raw", "bars": self.BARS}))
        self.assertIsNone(chart_payload.donchian_for("sz300209", {"adjust": "raw", "bars": self.BARS}))
        self.assertIsNotNone(chart_payload.donchian_for("hk06166", {"adjust": "qfq", "bars": self.BARS}))


if __name__ == "__main__":
    unittest.main()
