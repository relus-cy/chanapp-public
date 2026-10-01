"""首条完整链路（计划 A 的 A1 退出证据 1）：录制 fixture → 麦蕊 provider → 准入 → 事实库 → 视图。

不联网、不用凭据：清空 MAIRUI_LICENCE，并让任何 socket 连接直接失败。
对照：
- 300209 前复权 vs BaoStock adjustflag=2 独立参照（2024-12-20 重整除权，因子 4.79/6.29）；
- 600036 在 2026-09-29 的原生 m15（现行分钟事实）聚合 m30 vs 麦蕊原生 m30；
- 600036 在 2024-09-27 的 m5 聚合 m30 vs 麦蕊原生 m30（P2 复核，含零成交占位；m5 只为回退保留，按事实层直接聚合）。
"""
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from chanapp.engine.kline import calendar, facts, periods, views
from chanapp.engine.kline.providers import mairui, raw
from chanapp.engine.kline.rows import RawDayRow, new_batch_id

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw"
TODAY = "2026-09-28"
NOW = datetime(2026, 9, 28, 20, 0)
ROUTES = {
    "/hsindex/history/000001.SH/d/{L}": "idx-day-000001-2024q4.json",
    "/hsstock/history/300209.SZ/d/n/{L}": "day-300209-2024q4.json",
    "/hsstock/history/600036.SH/5/n/{L}": "m5-600036-20240927.json",
    "/hsstock/history/688806.SH/5/n/{L}": "m5-688806-500.json",
    "/hsindex/latest/000001.SH/5/{L}": "latest-idx-000001-lt2.json",
    "/hsstock/history/600036.SH/15/n/{L}": "m15-600036-20260929.json",
    "/hsindex/latest/000001.SH/15/{L}": "latest-idx-000001-m15-lt2.json",
}


def _load(name):
    return json.loads((FIX / "mairui" / name).read_text())


def recorded_transport(path, params, timeout):
    rec = _load(ROUTES[path])
    return rec["status"], rec["body"]


def _no_network(*args, **kwargs):
    raise AssertionError("链路测试不得联网")


class ChainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAIRUI_LICENCE", None)
        net = mock.patch.object(socket.socket, "connect", _no_network)
        net.start()
        self.addCleanup(net.stop)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)
        self.provider = mairui.MairuiProvider(transport=recorded_transport, licence="")

    def ingest_day(self, code, kind, start, end):
        rows = self.provider.day_history(code, start, end)
        return facts.commit_day_rows(self.conn, rows, market="CN", kind=kind, item="day_history",
                                     source="mairui", binding_gen=1, today=TODAY,
                                     start=start, end=end)

    def ingest_calendar(self):
        self.ingest_day("sh000001", "index", "2024-09-01", "2025-01-31")
        with facts.write_txn(self.conn):
            calendar.derive_cn_past(self.conn)

    def test_300209_qfq_matches_independent_reference(self):
        self.ingest_calendar()
        result = self.ingest_day("sz300209", "stock", "2024-10-01", "2025-01-31")
        self.assertEqual(result.rejected, [])
        qfq = {b["dt"]: b for b in views.read_view(self.conn, "sz300209", "day", adjust="qfq",
                                                    limit=2000, now=NOW).bars}
        raw_view = {b["dt"]: b for b in views.read_view(self.conn, "sz300209", "day", adjust="raw",
                                                         limit=2000, now=NOW).bars}
        ref = json.loads((FIX / "reference" / "bs-300209-d-adj2-2024q4.json").read_text())
        ref_raw = json.loads((FIX / "reference" / "bs-300209-d-adj3-2024q4.json").read_text())
        close, status = ref["fields"].index("close"), ref["fields"].index("tradestatus")
        traded = [r for r in ref["rows"] if r[status] == "1"]
        self.assertEqual(sorted(qfq), sorted(r[0] for r in traded))       # 停牌日不出 bar
        for r in traded:
            self.assertAlmostEqual(qfq[r[0]]["close"], float(r[close]), delta=0.01, msg=r[0])
        self.assertAlmostEqual(qfq["2024-12-18"]["close"] / raw_view["2024-12-18"]["close"],
                               4.79 / 6.29, places=9)
        # 除权日 pc 与第二来源一致（麦蕊 pc 对 BaoStock 不复权 preclose）
        pre = ref_raw["fields"].index("preclose")
        bs_pc = {r[0]: float(r[pre]) for r in ref_raw["rows"]}
        day_rows = {r["trade_date"]: r for r in facts.read_day_rows(self.conn, "sz300209")}
        self.assertAlmostEqual(day_rows["2024-12-20"]["pc"], bs_pc["2024-12-20"])

    def test_600036_m30_from_m15_matches_vendor_native(self):
        now = datetime(2026, 9, 29, 22, 0)
        rows = self.provider.minute_history("sh600036", "m15", "2026-09-29 09:30", "2026-09-29 15:00", now=now)
        result = facts.commit_minute_rows(self.conn, rows, market="CN", kind="stock",
                                          item="minute_history", fact_freq="m15", source="mairui",
                                          binding_gen=1, today="2026-09-29")
        self.assertEqual((result.inserted, result.rejected), (16, []))
        view = views.read_view(self.conn, "sh600036", "m30", adjust="raw", now=now)
        native = _load("m30-600036-20260929.json")["body"]
        self.assertEqual([(b["dt"], b["open"], b["high"], b["low"], b["close"], b["volume"])
                          for b in view.bars],
                         [(r["t"][:16], r["o"], r["h"], r["l"], r["c"], r["v"] * 100) for r in native])

    def test_600036_m30_from_m5_matches_vendor_native(self):
        self.ingest_calendar()
        rows = self.provider.minute_history("sh600036", "m5", "2024-09-27 09:30", "2024-09-27 15:00",
                                            now=NOW)
        result = facts.commit_minute_rows(self.conn, rows, market="CN", kind="stock",
                                          item="minute_history", fact_freq="m5", source="mairui",
                                          binding_gen=1, today=TODAY)
        self.assertEqual((result.inserted, result.rejected), (48, []))
        bars = periods.aggregate_minutes(facts.read_minute_rows(self.conn, "sh600036", "m5"), market="CN",
                                         fact_freq="m5", target_freq="m30")
        native = _load("m30-600036-20240927.json")["body"]
        self.assertEqual([(b["dt"], b["open"], b["high"], b["low"], b["close"], b["volume"])
                          for b in bars],
                         [(r["t"][:16], r["o"], r["h"], r["l"], r["c"], r["v"] * 100) for r in native])

    def test_upstream_500_writes_nothing(self):
        with self.assertRaises(raw.ProviderServerError):
            self.provider.minute_history("sh688806", "m5", "2026-08-01 09:30", "2026-08-31 15:00",
                                         now=NOW)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0], 0)


    def test_index_intraday_quote_is_live_after_latest(self):
        # 指数盘中（P3 指数路径）：latest → 准入 → 事实库 → 右栏行情。m15 录制于 09-29 收盘后（lt=2 回当日末两根），
        # 按 14:59 的时钟回放；盘中录制待 09-30 取证后替换。
        # 对照：盘中没有指数分钟提交时，行情停在「昨收」并判 stale（切换后首个交易日开盘前的线上代码即如此）
        now, today = datetime(2026, 9, 29, 14, 59), "2026-09-29"
        prev = RawDayRow("sh000001", "2026-09-28", 3878.41, 3878.41, 3806.67, 3823.62, 1.0, "lot", 1.0, "CNY",
                         3888.37, 0, "final", new_batch_id())      # 09-28 日线录制值
        facts.commit_day_rows(self.conn, [prev], market="CN", kind="index", item="day_history",
                              source="mairui", binding_gen=1, today=today)
        before = views.quote(self.conn, "sh000001", now=now)
        self.assertEqual((before["price_label"], before["stale"]), ("昨收", True))
        rows = self.provider.minute_live("sh000001", "m15", now=now)
        result = facts.commit_minute_rows(self.conn, rows, market="CN", kind="index", item="minute_live",
                                          fact_freq="m15", source="mairui", binding_gen=1, today=today)
        self.assertEqual((result.inserted, result.rejected), (2, []))
        q = views.quote(self.conn, "sh000001", now=now)
        self.assertEqual((q["price_label"], q["price"], q["pc"], q["pct"], q["stale"]),
                         ("最新", 3830.45, 3823.62, 0.18, False))


if __name__ == "__main__":
    unittest.main()
