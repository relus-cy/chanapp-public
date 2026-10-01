"""历史追赶与盘中、定稿的隔离（主会话 2026-09-29 对阶段 3 风险的裁定）。

- 历史来源的请求（历史追赶的规划与续传、请求路径追赶的续传）按 code#history 记单标的退避：历史连败不让同一代码的
  盘中与定稿被跳过；连接类失败仍按源冷却（源挂了对谁都挂了）；
- 历史追赶每轮活跃代码续传有全局请求上限（HISTORY_ROUND_REQUESTS），上证指数在前、从新到旧；
- status 的 enabled 要求调度线程与历史线程都存活。"""
import unittest
import unittest.mock
from datetime import datetime

from chanapp.engine.kline import collector, config, facts, sessions
from chanapp.engine.kline.providers.raw import ProviderError
from chanapp.engine.kline.rows import RawMinuteRow, new_batch_id
from chanapp.tests.test_kline_collector import DAY_VOL, FACT, FullFake, _list_since
from chanapp.tests.test_kline_history_thread import DAY, INDEX, A, Base, at

SLOTS = sessions.slots("CN", FACT)          # 现行分钟事实网格


class HistoryFails(FullFake):
    """A 的历史日线一律 503（非连接类）；当日日线与盘中照常。"""

    def __init__(self):
        super().__init__()
        self.live = []

    def day_history(self, code, start, end):
        if code == A and start < DAY:
            self.calls.append(("day", code, start, end))
            raise ProviderError("503")
        return super().day_history(code, start, end)

    def minute_live(self, code, fact_freq, *, now):
        self.live.append((code, now.strftime("%H:%M:%S")))
        return [RawMinuteRow(code, DAY, f"{DAY} 10:15", 10, 10, 10, 10, 1, "lot", 1, "forming", "traded",
                             new_batch_id())]


class BackoffBulkheadTests(Base):
    def setUp(self):
        super().setUp()
        self.seed(INDEX, "2026-09-25")
        self.seed(A, "2026-09-25", skip_day=("2026-09-23",))
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, A, "day", "2026-09-23", "2026-09-23", "backfill")
        self.provider = HistoryFails()
        self.c = self.make(self.provider, watch=(A,))

    def history_failures(self):
        return [call for call in self.provider.calls if call[0] == "day" and call[1] == A and call[2] < DAY]

    def test_history_failure_streak_does_not_skip_intraday(self):
        for s in (0, 10, 20):
            self.history(self.c, at(10, 0, s))
        self.assertEqual(len(self.history_failures()), config.CODE_BACKOFF_AFTER)   # 历史侧自己照样退避
        self.clock.t = at(10, 0, 30).timestamp()
        self.c.tick(at(10, 0, 30))
        self.assertIn((A, "10:00:30"), self.provider.live)

    def test_catch_up_failure_streak_does_not_skip_intraday(self):
        for s in (0, 10, 20):
            self.clock.t = at(10, 0, s).timestamp()
            self.c.catch_up(A, at(10, 0, s))
        self.assertEqual(len(self.history_failures()), config.CODE_BACKOFF_AFTER)
        self.clock.t = at(10, 0, 30).timestamp()
        self.c.tick(at(10, 0, 30))
        self.assertIn((A, "10:00:30"), self.provider.live)

    def test_history_failure_streak_does_not_defer_finalize(self):
        for when in (at(17, 28), at(17, 28, 30), at(17, 29)):     # 共用退避时会一直退到 17:34
            self.history(self.c, when)
        self.assertEqual(len(self.history_failures()), config.CODE_BACKOFF_AFTER)
        self.clock.t = at(17, 30).timestamp()
        self.c.tick(at(17, 30))
        self.assertTrue(any(r["trade_date"] == DAY and r["provenance"] == "final"
                            for r in facts.read_day_rows(self.conn, A)))

    def test_finalize_failures_still_back_off_the_plain_code(self):
        # 定稿仍用原键：定稿连败退避的是该代码本身（盘中、定稿），与历史键互不影响
        self.c.code_backoff.record(A, False)
        self.c.code_backoff.record(A, False)
        self.assertFalse(self.c.code_backoff.allow(A))
        self.assertTrue(self.c.code_backoff.allow(f"{A}#history"))
        self.history(self.c, at(10, 0))
        self.assertEqual(len(self.history_failures()), 1)                # 历史请求照发


class RoundCapTests(Base):
    CODES = ["sh600036", "sz000001", "sz000002", "sh600000", "sz000858"]

    def test_active_code_drain_has_a_global_round_cap_index_first(self):
        _list_since(self.dir, self.CODES, "2026-09-01")
        for code in [INDEX, *self.CODES]:
            self.seed(code, "2026-09-25", skip_minute=("2026-09-23", "2026-09-24"))
            with facts.write_txn(self.conn):
                for d in ("2026-09-23", "2026-09-24"):
                    facts.record_gap(self.conn, code, FACT, f"{d} 09:30", f"{d} 15:00", "backfill")
        provider = FullFake()
        # 指数在自选里才续传它的分钟缺口（不在自选时只有日线系统职责，见 test_kline_viewing_tracking V17）
        c = self.make(provider, watch=[INDEX, *self.CODES])
        with unittest.mock.patch.object(config, "HISTORY_ROUND_REQUESTS", 8, create=True):
            self.history(c, at(10, 0))
        self.assertEqual(len(provider.calls), 8)
        # 跨代码按缺口结束点从新到旧；结束点相同时上证指数在前，其余按代码
        self.assertEqual([call[1:3] for call in provider.calls],
                         [(INDEX, "2026-09-24 09:30"), ("sh600000", "2026-09-24 09:30"),
                          ("sh600036", "2026-09-24 09:30"), ("sz000001", "2026-09-24 09:30"),
                          ("sz000002", "2026-09-24 09:30"), ("sz000858", "2026-09-24 09:30"),
                          (INDEX, "2026-09-23 09:30"), ("sh600000", "2026-09-23 09:30")])

    def test_recent_gap_is_not_starved_by_backlogs_sorted_first(self):
        # 四个代码各有三年分钟积压（按代码排在前面），另一个代码只缺昨天：昨天的缺口第一轮就取
        backlog = ["sh600000", "sh600036", "sh600519", "sh601318"]
        _list_since(self.dir, backlog + ["sz000001"], "2026-09-01")
        self.seed(INDEX, "2026-09-25")
        for code in backlog:
            self.seed(code, "2026-09-25")
            with facts.write_txn(self.conn):
                for y, m in [(y, m) for y in (2023, 2024, 2025, 2026) for m in range(1, 13)
                             if (2023, 9) <= (y, m) <= (2026, 8)]:
                    facts.record_gap(self.conn, code, FACT, f"{y}-{m:02d}-01 09:30", f"{y}-{m:02d}-28 15:00",
                                     "backfill")
        self.seed("sz000001", "2026-09-25", skip_minute=("2026-09-25",))
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, "sz000001", FACT, "2026-09-25 09:30", "2026-09-25 15:00", "backfill")
        provider = FullFake()
        c = self.make(provider, watch=backlog + ["sz000001"])
        self.history(c, at(10, 0))
        self.assertEqual(provider.calls[0][1:3], ("sz000001", "2026-09-25 09:30"))
        self.assertEqual(len(provider.calls), config.HISTORY_ROUND_REQUESTS)
        self.assertEqual(self.open_gaps("sz000001"), [])


class PostFetchFailureTests(Base):
    """取回之后的处理（核对、被拒日登记等）持续抛非 provider 异常：请求已发出，照样占每代码与全轮上限，
    并按一次失败尝试计入该缺口，满 MAX_GAP_ATTEMPTS 转 known_gap，不在每轮无限重取。"""

    def setUp(self):
        super().setUp()
        self.seed(INDEX, "2026-09-25")
        self.seed(A, "2026-09-25", skip_minute=("2026-09-22", "2026-09-23", "2026-09-24"))
        with facts.write_txn(self.conn):
            for d in ("2026-09-22", "2026-09-23", "2026-09-24"):
                facts.record_gap(self.conn, A, FACT, f"{d} 09:30", f"{d} 15:00", "backfill")
        patcher = unittest.mock.patch.object(collector.Collector, "_reconcile",
                                             side_effect=RuntimeError("reconcile broke"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def attempts(self):
        return [(g["start"][:10], g["attempts"], g["reason"]) for g in facts.open_gaps(self.conn, A)]

    def test_history_round_counts_the_request_and_the_attempt(self):
        provider = FullFake()
        provider.skip_days = ("2026-09-22", "2026-09-23", "2026-09-24")   # 上游给不全：缺口不会被下一轮零请求关闭
        c = self.make(provider, watch=(A,))
        self.history(c, at(10, 0))
        self.assertEqual(len(provider.calls), config.HISTORY_CODE_REQUESTS)
        self.assertEqual(self.attempts(), [("2026-09-24", 1, "backfill"), ("2026-09-23", 1, "backfill"),
                                           ("2026-09-22", 0, "backfill")])
        for s in range(1, 12):
            self.history(c, at(10, 0, s))
        self.assertEqual(len(provider.calls), 3 * collector.MAX_GAP_ATTEMPTS)    # 转 known_gap 后不再取
        self.assertEqual({reason for *_, reason in self.attempts()}, {"known_gap"})

    def test_drain_gaps_keeps_going_and_counts_failures(self):
        c = self.make(FullFake(), watch=(A,))
        self.clock.t = at(20, 0).timestamp()
        res = c.drain_gaps(10, code=A)
        self.assertEqual((res["failed"], res["done"]), (3, 0))
        self.assertEqual([a for _, a, _ in self.attempts()], [1, 1, 1])
        self.assertTrue(all("RuntimeError" in g["last_error"] for g in facts.open_gaps(self.conn, A)))


class StartGuardTests(Base):
    def test_start_does_not_spawn_a_second_history_thread(self):
        c = self.make(FullFake(), watch=())
        survivor = unittest.mock.Mock(is_alive=lambda: True)
        c._thread, c._history_thread = None, survivor          # 调度线程已死、历史线程仍在
        with unittest.mock.patch.object(config, "HISTORY_INTERVAL_S", 3600):
            self.assertTrue(c.start())
            try:
                self.assertIs(c._history_thread, survivor)
                self.assertTrue(c._thread.is_alive())
                self.assertFalse(c.start())                     # 两个都活着：不再起
            finally:
                c._history_thread = None
                c.stop()


class StatusTests(Base):
    def test_enabled_requires_both_threads_alive(self):
        c = self.make(FullFake(), watch=())
        alive = unittest.mock.Mock(is_alive=lambda: True)
        dead = unittest.mock.Mock(is_alive=lambda: False)
        now = datetime(2026, 9, 28, 20, 0)
        c._thread, c._history_thread = alive, None
        self.assertFalse(c.status([], now=now)["enabled"])
        c._history_thread = dead
        self.assertFalse(c.status([], now=now)["enabled"])
        c._history_thread = alive
        self.assertTrue(c.status([], now=now)["enabled"])


if __name__ == "__main__":
    unittest.main()
