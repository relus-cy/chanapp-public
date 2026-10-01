"""状态栏的数据状态（目标 2026-09-29「报价、F10 与状态栏」，第三阶段）：视图在同一次读取里给出
coverage.data_status = {phase, day, at}，页面据此显示「交易中 · 实时抓取 / 已收盘 · 待定稿 / 已收盘 · 历史抓取」。

实现前列出的失败方式：
- 仅日线成功就把分钟图标成已定稿；
- 失败或空返回推进时间（时间应是当前展示数据最后成功接纳的时刻）；从未成功却编造时间；
- 休市或开盘前按今天判断，把上一交易日的待定稿说成已定稿（或反之）；
- 港股前复权图忽略供应商缓存：raw 已完整但缓存没追上时说已定稿；
- 状态不出自同一次读取（后台更新让旧图显示新状态）——由视图与 bars 同一读事务给出保证，本文件按读取结果断言。
"""
import json
import unittest
from datetime import datetime

from chanapp.engine.kline import facts, sessions, views
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests import test_kline_finalize_schedule as schedule
from chanapp.tests.test_kline_collector import DAY_VOL, FACT
from chanapp.tests.test_kline_finalize_schedule import CODE, DAY, SLOTS, Base, HKRawFake, at

PREV = "2026-09-25"                                   # DAY（周一）之前的交易日


def local_minute(iso):
    return datetime.fromisoformat(iso).astimezone().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M") if iso else None


class DataStatusTests(Base):
    def setUp(self):
        super().setUp()
        self.known_calendar()
        facts.commit_day_rows(self.conn, [RawDayRow(CODE, PREV, 10, 10, 10, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                                                    new_batch_id())],
                              market="CN", kind="stock", item="day_history", source="mairui", binding_gen=1,
                              today=PREV)

    def status(self, freq, when, code=CODE, adjust="raw"):
        return views.read_view(self.conn, code, freq, adjust=adjust, now=when).coverage["data_status"]

    def committed(self, dataset, code=CODE):
        return local_minute(facts.last_commit_at(self.conn, code, dataset))

    def test_in_session_is_live_with_last_acceptance_time(self):
        rows = [RawMinuteRow(CODE, DAY, f"{DAY} {t}", 10, 10, 10, 10, 1, "lot", 1, "forming", "traded",
                             new_batch_id()) for t in SLOTS if t <= "10:05"]   # 10:06 之前已开始的槽
        facts.commit_minute_rows(self.conn, rows, market="CN", kind="stock", item="minute_live", fact_freq=FACT,
                                 source="mairui", binding_gen=1, today=DAY)
        for freq in ("m30", "day"):
            self.assertEqual(self.status(freq, at(10, 6)), {"phase": "live", "day": DAY, "at": self.committed(FACT)})

    def test_day_final_alone_does_not_finalize_a_minute_chart(self):
        self.commit_today(slots=SLOTS[:-4])
        self.assertEqual(self.status("m30", at(17, 40))["phase"], "awaiting_final")
        self.assertEqual(self.status("m60", at(17, 40))["phase"], "awaiting_final")
        self.assertEqual(self.status("day", at(17, 40)), {"phase": "final", "day": DAY, "at": self.committed("day")})

    def test_complete_day_is_final_for_minute_chart(self):
        self.commit_today()
        self.assertEqual(self.status("m30", at(17, 40)), {"phase": "final", "day": DAY, "at": self.committed(FACT)})

    def test_after_close_without_final_day_is_awaiting(self):
        rows = [RawMinuteRow(CODE, DAY, f"{DAY} {t}", 10, 10, 10, 10, 1, "lot", 1, "forming", "traded",
                             new_batch_id()) for t in SLOTS]
        facts.commit_minute_rows(self.conn, rows, market="CN", kind="stock", item="minute_live", fact_freq=FACT,
                                 source="mairui", binding_gen=1, today=DAY)
        for freq in ("day", "week", "m30"):
            self.assertEqual(self.status(freq, at(15, 30))["phase"], "awaiting_final", freq)

    def test_never_accepted_minutes_show_no_time(self):
        self.assertEqual(self.status("m30", at(17, 40)), {"phase": "none", "day": DAY, "at": None})

    def test_before_open_and_holidays_judge_the_previous_trading_day(self):
        self.commit_today(slots=SLOTS[:-4])
        self.assertEqual(self.status("m30", at(9, 0, day=29)),
                         {"phase": "awaiting_final", "day": DAY, "at": self.committed(FACT)})
        self.assertEqual(self.status("day", at(9, 0, day=29))["phase"], "final")

    def conflict(self, slot):
        """已收盘槽来了不同值：旧行照常可读，另留一条未裁决的待核验（第三阶段复审阻断 1 的反例）。"""
        row = RawMinuteRow(CODE, DAY, f"{DAY} {slot}", 10, 11, 10, 10.5, 1, "lot", 1, "closed", "traded",
                           new_batch_id())
        r = facts.commit_minute_rows(self.conn, [row], market="CN", kind="stock", item="minute_history",
                                     fact_freq=FACT, source="mairui", binding_gen=1, today=DAY)
        self.assertEqual(r.pending_review, 1)

    def test_unresolved_review_on_a_complete_day_is_awaiting_not_final(self):
        self.commit_today()
        self.conflict(SLOTS[3])
        self.assertEqual(self.status("m30", at(17, 40))["phase"], "awaiting_final")
        self.assertEqual(self.status("day", at(17, 40))["phase"], "final")      # 日线本身无待核验
        c = self.make(schedule.FullFake())
        self.assertEqual(c._final_status(CODE, DAY), "pending_review")          # 与采集器同一判据

    def test_minute_day_mismatch_on_a_complete_day_is_awaiting_not_final(self):
        self.commit_today()
        with facts.write_txn(self.conn):
            self.conn.execute("INSERT INTO minute_day_checks(code, trade_date, fact_freq, status, reason, detail,"
                              " checked_at) VALUES (?,?,?,?,?,?,?)",
                              (CODE, DAY, FACT, "pending_review", "close_mismatch", json.dumps({"minute": {}}),
                               facts.now_iso()))
        self.assertEqual(self.status("m30", at(17, 40))["phase"], "awaiting_final")
        self.assertEqual(self.make(schedule.FullFake())._final_status(CODE, DAY), "reconcile_mismatch")

    def test_rejected_or_empty_commit_keeps_the_time(self):
        self.commit_today()
        before = self.status("m30", at(17, 40))
        bad = [RawMinuteRow(CODE, DAY, f"{DAY} 12:05", 10, 10, 10, 10, 1, "lot", 1, "closed", "traded",
                            new_batch_id())]                                  # 午休槽：准入拒收
        facts.commit_minute_rows(self.conn, bad, market="CN", kind="stock", item="minute_history", fact_freq=FACT,
                                 source="mairui", binding_gen=1, today=DAY)
        facts.commit_minute_rows(self.conn, [], market="CN", kind="stock", item="minute_history", fact_freq=FACT,
                                 source="mairui", binding_gen=1, today=DAY)
        self.assertEqual(self.status("m30", at(17, 41)), before)


class HKVendorStatusTests(Base):
    HK = schedule.HKVendorOnlyTests.HK            # 只借搭建方法（导入测试类会让它的用例在本文件重跑）
    setup_hk = schedule.HKVendorOnlyTests.setup_hk
    committed = DataStatusTests.committed

    def test_raw_complete_but_vendor_behind_is_awaiting_until_cache_catches_up(self):
        fake = HKRawFake()
        c = self.setup_hk(fake)
        for freq in ("day", "m30"):
            self.assertEqual(views.read_view(self.conn, self.HK, freq, adjust="qfq", now=at(17, 0))
                             .coverage["data_status"]["phase"], "awaiting_final", freq)
        # 有意改写（第三阶段第三轮复审阻断 1）：当天的缓存只由定稿请求，这里按定稿的调用方式刷新
        c.refresh_vendor_qfq(self.HK, at(17, 0), closed_through=DAY, today_ok=True)
        for freq in ("day", "m30"):
            self.assertEqual(views.read_view(self.conn, self.HK, freq, adjust="qfq", now=at(17, 1))
                             .coverage["data_status"]["phase"], "final", freq)
        raw = views.read_view(self.conn, self.HK, "m30", adjust="raw", now=at(17, 1)).coverage["data_status"]
        self.assertEqual(raw["phase"], "final")

    def test_vendor_chart_time_is_the_cache_publish_time(self):
        """第三阶段复审阻断 1 后半：前复权图显示的是供应商缓存，时间取缓存发布时刻，不取 raw 最后接纳。"""
        c = self.setup_hk(HKRawFake())
        c.refresh_vendor_qfq(self.HK, at(17, 0), closed_through=DAY, today_ok=True)   # 有意改写：按定稿的调用方式
        published = "2026-09-28T17:00:00+08:00"
        with facts.write_txn(self.conn):
            self.conn.execute("UPDATE vendor_qfq_publish SET fetched_at=? WHERE code=?", (published, self.HK))
        for stale in (0, 1):                                 # 缓存停在旧版（stale）时 raw 尾巴不展示，时间也不跟 raw
            with facts.write_txn(self.conn):
                self.conn.execute("UPDATE vendor_qfq_publish SET stale=? WHERE code=?", (stale, self.HK))
            for freq in ("day", "m30"):
                ds = views.read_view(self.conn, self.HK, freq, adjust="qfq", now=at(17, 1)).coverage["data_status"]
                self.assertEqual(ds["at"], local_minute(published), (stale, freq))
        raw = views.read_view(self.conn, self.HK, "m30", adjust="raw", now=at(17, 1)).coverage["data_status"]
        self.assertEqual(raw["at"], self.committed("m30", self.HK))


if __name__ == "__main__":
    unittest.main()
