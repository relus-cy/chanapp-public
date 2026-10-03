"""只日线实例的采集与视图回归：所有上游均用内存替身，无网络。"""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chanapp.engine.kline import bindings, collector, facts, views
from chanapp.engine.kline.rows import FetchItem, RawDayRow, new_batch_id


class DayProvider:
    def __init__(self):
        self.minute_requests = []
        self.qfq_requests = []

    def day_history(self, code, start, end):
        return [RawDayRow(code, end, 10, 10, 10, 10, 100, "share", 1, "CNY", 10, 0,
                          "final", new_batch_id())]

    def minute_live(self, code, freq, *, now):
        self.minute_requests.append((code, freq))
        return []

    def qfq_series(self, code, freq, start, end):
        self.qfq_requests.append(freq)
        return [{"trade_date": end[:10], "slot_end": end, "open": 10, "high": 10,
                 "low": 10, "close": 10, "volume": 100, "volume_unit": "share"}]


class DailyOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = bindings.BINDINGS
        self.daily = tuple(replace(b, minute_fact_freq=None) for b in self.original)
        self.enterContext(patch.object(bindings, "BINDINGS", self.daily))
        self.enterContext(patch.object(bindings, "_INDEX", {(b.market, b.kind, b.item): b for b in self.daily}))
        self.enterContext(patch.dict("os.environ", {"COLLECTOR_ENABLED": "1"}))
        self.now = datetime(2026, 9, 25, 10, 0)
        self.provider = DayProvider()
        self.worker = collector.Collector(Path(self.tmp.name),
            providers={b.primary: self.provider for b in self.daily},
            clock=lambda: self.now.timestamp(), watchlist_fn=lambda: ["sh600036", "hk00700"])
        self.addCleanup(lambda: self.worker.conn().close())

    def seed_day(self, code):
        market = "HK" if code.startswith("hk") else "CN"
        source, gen = bindings.active(self.worker.conn(), market, "stock", FetchItem.DAY_HISTORY)
        rows = self.provider.day_history(code, "2026-09-24", "2026-09-24")
        facts.commit_day_rows(self.worker.conn(), rows, market=market, kind="stock", item="day_history",
                              source=source, binding_gen=gen, today="2026-09-25")

    def test_disabled_minutes_return_unsupported_and_keep_day_readable(self):
        self.seed_day("sh600036")
        view = views.read_view(self.worker.conn(), "sh600036", "m30", adjust="raw", now=self.now)
        self.assertEqual(view.bars, [])
        self.assertIn("unsupported", [n["code"] for n in view.notices])
        self.assertTrue(views.read_view(self.worker.conn(), "sh600036", "day", adjust="raw", now=self.now).bars)

    def test_intraday_and_manual_refetch_do_not_request_disabled_minutes(self):
        self.seed_day("sh600036")
        self.worker.intraday_tick(["sh600036"], self.now)
        self.assertEqual(self.provider.minute_requests, [])
        self.worker.refetch_window("sh600036", "day", bars=1)
        self.assertEqual(self.provider.minute_requests, [])

    def test_tracked_window_and_status_only_include_day(self):
        for code in ("sh600036", "sh000001"):
            self.worker.backfill_day(code)
        self.assertTrue(self.worker.ensure_window("sh600036", "m30", bars=1))
        status = self.worker.status(["sh600036"], now=self.now)
        self.assertEqual([d["dataset"] for d in status["datasets"]], ["day"])
        self.assertEqual(status["datasets"][0]["stale_judged"], False)     # 盘中日线不判 stale

    def test_disabling_then_restoring_minutes_advances_generation_once(self):
        code = "sh600036"
        conn = self.worker.conn()
        item = FetchItem.MINUTE_HISTORY
        before = bindings.active(conn, "CN", "stock", item)[1]
        self.worker.sync_minute_fact_freq()
        disabled = bindings.active(conn, "CN", "stock", item)[1]
        self.assertEqual(disabled, before + 1)
        self.worker.sync_minute_fact_freq()
        self.assertEqual(bindings.active(conn, "CN", "stock", item)[1], disabled)
        with patch.object(bindings, "BINDINGS", self.original), patch.object(bindings, "_INDEX",
                {(b.market, b.kind, b.item): b for b in self.original}):
            self.worker.sync_minute_fact_freq()
            self.assertEqual(bindings.active(conn, "CN", "stock", item)[1], disabled + 1)

    def test_hk_qfq_daily_instance_publishes_day_without_minute_request(self):
        self.seed_day("hk00700")
        result = self.worker.refresh_vendor_qfq("hk00700", self.now)
        self.assertEqual(self.provider.qfq_requests, ["day"])
        self.assertEqual(set(result), {"day"})
        view = views.read_view(self.worker.conn(), "hk00700", "day", now=self.now)
        self.assertTrue(view.bars)
        minute = views.read_view(self.worker.conn(), "hk00700", "m30", now=self.now)
        self.assertEqual(minute.bars, [])
        self.assertIn("unsupported", [n["code"] for n in minute.notices])

    def test_daily_history_planning_and_finalize_never_create_minute_work(self):
        self.assertEqual(self.worker.plan_minute_backfill("sh600036"), 0)
        self.worker._plan_history("sh600036")
        result = self.worker.finalize_due(["sh600036"], "2026-09-25", datetime(2026, 9, 25, 21, 0),
                                          calendar_known=True)
        self.assertEqual(result, {"done": ["sh600036"], "failed": [], "review": [], "deferred": []})
        self.assertEqual(self.provider.minute_requests, [])
        self.assertTrue(all(g["dataset"] == "day" for g in facts.open_gaps(self.worker.conn(), "sh600036")))
