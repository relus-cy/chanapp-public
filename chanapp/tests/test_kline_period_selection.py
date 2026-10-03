"""分钟偏好采集回归（采集器替身，不访问外网）。

实现前失败方式：
- 取消所有分钟后首开、重拉、盘中仍取分钟。
- 后台仍规划、续传分钟缺口；恢复后原缺口或未规划历史不再推进。
- 日线请求期间取消，后续分钟请求仍发出。
- 港股前复权缓存绕过分钟偏好，或中途启用导致当前发布集合缺项。
- 只日线定稿被记完整完成，恢复分钟后不再定稿。
- 首开中途取消分钟，未做历史规划却写入完成标记。
- 恢复后短窗口仍被旧偏好的补取节流挡住。
- shared 更新回调时已有采集器仍沿用旧回调。
- 定稿日线请求途中取消分钟，已取回的日线不入库。
- 港股只日线期间建了日线缓存，重新勾选分钟后首开不补 m30 缓存（历史线程另有扩展路径补建）。
浏览器真实 API 验收由独立 E2E 覆盖；在途请求允许完成提交。
"""
from types import SimpleNamespace

from chanapp.engine import data, period_preferences
from chanapp.engine.kline import collector, facts, hk_vendor_qfq
from chanapp.tests.test_kline_viewing_tracking import Base, HkBase, A, X, FACT, at


class MinutePreferenceTests(Base):
    def make(self):
        c = super().make()
        self.enabled = False
        c.minute_enabled = lambda code: self.enabled
        return c

    def test_window_refetch_and_intraday_stop_and_resume(self):
        c = self.make()
        c.ensure_window(X, "m60", bars=10)
        c.refetch_window(X, "day", bars=10)
        c.intraday_tick([X], at("2026-09-25", 10))
        self.assertFalse(self.calls(X, FACT))
        self.assertFalse(self.closed_minutes(X))
        self.assertFalse(self.calls(X, "live"))
        self.enabled = True
        c.ensure_window(X, "m60", bars=10)
        self.assertTrue(self.calls(X, FACT))
        self.assertTrue(self.closed_minutes(X))

    def test_history_leaves_minute_gaps_pending_and_resumes(self):
        c = self.make()
        with c.writer() as conn:
            facts.record_gap(conn, A, FACT, "2026-09-24 09:30", "2026-09-24 15:00", "backfill")
        c._plan_history(A)
        c.drain_gaps(1, code=A)
        self.assertNotIn("minute", self.planned(A))
        self.assertFalse(self.calls(A, FACT))
        self.assertTrue(facts.open_gaps(self.conn, A, FACT))
        self.enabled = True
        c._plan_history(A)
        c.drain_gaps(1, code=A)
        self.assertIn("minute", self.planned(A))
        self.assertTrue(self.calls(A, FACT))

    def test_cancel_during_day_request_stops_remaining_minute_requests(self):
        c = self.make()
        self.enabled = True
        self.provider.hook = lambda call: setattr(self, "enabled", False) if call[0] == "day" else None
        c.ensure_window(X, "m60", bars=10)
        self.assertTrue(self.calls(X, "day"))
        self.assertFalse(self.calls(X, FACT))

    def test_cancel_during_window_does_not_mark_unplanned_minute_history_complete(self):
        c = self.make()
        c.backfill_day(A)
        self.enabled = True
        self.provider.hook = lambda call: setattr(self, "enabled", False) if call[0] == FACT else None
        c.ensure_window(A, "m60", bars=10)
        self.assertNotIn("minute", self.planned(A))
        self.enabled = True
        self.provider.hook = None
        c._plan_history(A)
        self.assertIn("minute", self.planned(A))
        self.assertTrue(facts.open_gaps(self.conn, A, FACT))

    def test_cancel_during_provider_initialization_does_not_send_minutes(self):
        c = self.make()
        self.enabled = True
        original = c.provider

        def initialize(name):
            provider = original(name)
            self.enabled = False
            return provider

        c.provider = initialize
        c.intraday_tick([X], at("2026-09-25", 10))
        self.assertFalse(self.calls(X, "live"))

    def test_finalize_resumes_minutes_after_day_only_completion(self):
        c = self.make()
        now = self.set_time(at("2026-09-25", 20))
        c.finalize_due([A], "2026-09-25", now, calendar_known=True)
        self.assertFalse(self.calls(A, FACT))
        self.enabled = True
        c.finalize_due([A], "2026-09-25", now, calendar_known=True)
        self.assertTrue(self.calls(A, FACT))

    def test_cancel_during_finalize_day_request_still_commits_day(self):
        c = self.make()
        self.enabled = True
        now = self.set_time(at("2026-09-25", 20))
        self.provider.hook = lambda call: setattr(self, "enabled", False) if call[0] == "day" else None
        c.finalize_due([A], "2026-09-25", now, calendar_known=True)
        self.assertFalse(self.calls(A, FACT))
        rows = facts.read_day_rows(self.conn, A, "2026-09-25", "2026-09-25")
        self.assertEqual([r["provenance"] for r in rows], ["final"])

    def test_preference_change_retries_short_window_immediately(self):
        self.addCleanup(data._window_attempts.clear)
        data._window_attempts.clear()
        paths = SimpleNamespace(period_prefs=self.dir / "periods.json")
        with period_preferences.activate(paths, {"CN": "m15", "HK": "m30"}) as prefs:
            self.assertTrue(data._claim_window(A, "m60"))
            self.assertFalse(data._claim_window(A, "m60"))
            prefs.save(["day"], prefs.snapshot()["revision"])
            prefs.save(["day", "m60"], prefs.snapshot()["revision"])
            self.assertTrue(data._claim_window(A, "m60"))
            self.assertFalse(data._claim_window(A, "m60"))

    def test_shared_updates_existing_collector_predicate(self):
        c = collector.shared(self.dir, minute_enabled=lambda code: False)
        self.addCleanup(collector._shared.pop, str(self.dir.resolve()), None)
        self.assertFalse(c.minute_enabled(A))
        self.assertIs(c, collector.shared(self.dir, minute_enabled=lambda code: True))
        self.assertTrue(c.minute_enabled(A))


class HkMinutePreferenceTests(HkBase):
    def test_day_window_does_not_build_minute_vendor_cache(self):
        c = self.make()
        c.minute_enabled = lambda code: False
        c.ensure_window("hk00700", "day", bars=10)
        self.assertTrue(self.fake.calls)
        self.assertFalse([call for call in self.fake.calls if "m30" in call])

    def test_reenable_during_vendor_day_request_finishes_then_next_round_builds_minutes(self):
        c = self.make()
        enabled = [False]
        c.minute_enabled = lambda code: enabled[0]
        original = self.fake.qfq_series

        def toggle(code, freq, *span):
            rows = original(code, freq, *span)
            enabled[0] = True
            return rows

        self.fake.qfq_series = toggle
        c.refresh_vendor_qfq("hk00700", at("2026-09-26", 20))
        c.refresh_vendor_qfq("hk00700", at("2026-09-26", 20))
        self.assertTrue([call for call in self.fake.calls if "m30" in call])

    def test_reenabled_minutes_build_missing_m30_cache_on_open(self):
        c = self.make()
        enabled = [False]
        c.minute_enabled = lambda code: enabled[0]
        c.ensure_window("hk00700", "day", bars=10)
        self.assertIsNone(hk_vendor_qfq.read(c.conn(), "hk00700", "m30")[1])
        enabled[0] = True
        c.ensure_window("hk00700", "m60", bars=10)
        self.assertIsNotNone(hk_vendor_qfq.read(c.conn(), "hk00700", "m30")[1])
