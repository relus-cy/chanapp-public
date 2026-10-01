"""历史追赶线程（计划 2026-09-29 决策 D5，失败模式 F18–F21、F24）。

线程逻辑经可同步调用的 history_tick(now) 测试，不靠真实 sleep；阻塞点用 threading.Event 控制。
判据按结果写：事实库里的 final 日线与已收盘分钟、缺口状态与尝试次数、请求记录。"""
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import collector, config, facts, sessions
from chanapp.engine.kline.providers.raw import ProviderError
from chanapp.engine.kline.rows import RawMinuteRow, new_batch_id
from chanapp.tests.test_kline_collector import (DAY_VOL, FACT, Clock, FullFake, _enable_collector, _list_since,
                                                _pin_mechanism_schedule, _weekdays)

DAY = "2026-09-28"                                   # 周一
A, B, X, INDEX = "sh600036", "sz000001", "sz000002", "sh000001"
SLOTS = sessions.slots("CN", FACT)          # 现行分钟事实网格


def at(hh, mm, ss=0, day=28):
    return datetime(2026, 9, day, hh, mm, ss)


class Base(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        _pin_mechanism_schedule(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = Clock(at(10, 0).timestamp())
        _list_since(self.dir, [A, B, X, INDEX], "2026-09-01")
        self.conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(self.conn.close)
        with facts.write_txn(self.conn):
            for d in _weekdays("2026-09-01", "2026-10-30"):
                self.conn.execute("INSERT OR REPLACE INTO calendar(market, date, is_open, sessions, source,"
                                  " fetched_at) VALUES ('CN', ?, 1, '[]', 'test', ?)", (d, facts.now_iso()))

    def make(self, provider, watch=(A, B)):
        watch = list(watch)
        return collector.Collector(self.dir, providers={"mairui": provider}, clock=self.clock,
                                   watchlist_fn=lambda: list(watch))

    def seed(self, code, through, *, skip_day=(), skip_minute=(), watermark=None):
        """09-01..through 的交易日事实（skip_* 中的日子不写），历史规划已完成，发现水位推进到 watermark（缺省 through）。"""
        days = list(_weekdays("2026-09-01", through))
        fake = FullFake()
        facts.commit_day_rows(self.conn, [r for r in fake.day_history(code, days[0], through)
                                          if r.trade_date not in skip_day],
                              market="CN", kind="index" if code == INDEX else "stock", item="day_history",
                              source="mairui", binding_gen=1, today=through)
        facts.commit_minute_rows(self.conn, [r for r in fake.minute_history(code, FACT, f"{days[0]} 09:30",
                                                                            f"{through} 15:00", now=None)
                                             if r.trade_date not in skip_minute],
                                 market="CN", kind="index" if code == INDEX else "stock", item="minute_history",
                                 fact_freq=FACT, source="mairui", binding_gen=1, today=through)
        with facts.write_txn(self.conn):
            for kind in ("backfill", "minute"):
                facts.set_setting(self.conn, collector.plan_key(code, kind), "2026-09-01")
            for dataset in ("day", FACT):
                facts.set_setting(self.conn, f"discovered:{dataset}:{code}", watermark or through)

    def history(self, c, when, rounds=1):
        self.clock.t = when.timestamp()
        for _ in range(rounds):
            c.history_tick(when)

    def finals(self, code, lo="2026-09-21", hi=DAY):
        return [r["trade_date"] for r in facts.read_day_rows(self.conn, code, lo, hi) if r["provenance"] == "final"]

    def closed_slots(self, code, day):
        return len([r for r in facts.read_minute_rows(self.conn, code, FACT, f"{day} 00:00", f"{day} 23:59")
                    if r["state"] == "closed"])

    def open_gaps(self, code):
        return [g for g in facts.open_gaps(self.conn, code) if g["reason"] != "known_gap"]


class Overreach(FullFake):
    """行为不端的上游：历史接口无视 end，一直返回到今天（日线标 final、分钟标 closed）；
    gate 给出时 A 的历史日线请求在 gate 放开前阻塞（可控阻塞点）；minute_live 为 B 与 A 返回当日 forming。"""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.gate, self.entered, self.live, self.last_close = clock, None, threading.Event(), [], {}

    def day_history(self, code, start, end):
        if code == A and self.gate is not None and start < DAY:
            self.entered.set()
            self.gate.wait(10)
        return super().day_history(code, start, max(end, DAY))

    def minute_history(self, code, fact_freq, start, end, *, now):
        return super().minute_history(code, fact_freq, start, f"{DAY} 15:00", now=now)

    def minute_live(self, code, fact_freq, *, now):
        self.live.append((code, now.strftime("%H:%M:%S")))
        close = self.last_close[code] = 10 + len(self.live) / 100
        return [RawMinuteRow(code, DAY, f"{DAY} 10:15", 10, close, 10, close, 1, "lot", 1, "forming", "traded",
                             new_batch_id())]


class DecisiveTwoCodeTests(Base):
    """决定性验证（F19、F20）：A 只加自选、从不打开，停机缺了 09-23..09-25，历史请求卡在阻塞点；
    B 的盘中增量照常推进；放开后 A 补齐中间交易日，且任何代码都不产生当日的历史 closed 行。"""

    def run_tick(self, c, when):
        self.clock.t = when.timestamp()
        t = threading.Thread(target=c.tick, args=(when,), daemon=True)
        t.start()
        t.join(10)
        return not t.is_alive()

    def test_blocked_history_does_not_stall_intraday_and_fills_after_release(self):
        self.seed(INDEX, "2026-09-25")
        self.seed(B, "2026-09-25")
        self.seed(A, "2026-09-22")                                   # 停机：09-23 起未发现、未补
        provider = Overreach(self.clock)
        provider.gate = threading.Event()
        c = self.make(provider)
        self.clock.t = at(10, 0).timestamp()
        worker = threading.Thread(target=c.history_tick, args=(at(10, 0),), daemon=True)
        worker.start()
        self.addCleanup(provider.gate.set)
        self.assertTrue(provider.entered.wait(10), "A 的历史请求没有发出")
        for when in (at(10, 0, 1), at(10, 1, 2), at(10, 2, 3)):           # 盘中 60 秒一轮（目标 2026-09-29 第三阶段）
            self.assertTrue(self.run_tick(c, when), f"{when:%H:%M:%S} 盘中轮次被历史请求阻塞")
        live_b = [t for code, t in provider.live if code == B]
        self.assertEqual(live_b, ["10:00:01", "10:01:02", "10:02:03"])
        forming = facts.read_minute_rows(self.conn, B, FACT, f"{DAY} 00:00", f"{DAY} 23:59")
        self.assertEqual([(r["state"], r["close"]) for r in forming], [("forming", provider.last_close[B])])
        self.assertTrue(worker.is_alive())                            # A 仍卡在阻塞点
        provider.gate.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.history(c, at(10, 2), rounds=5)
        self.assertEqual(self.finals(A), ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"])
        for d in ("2026-09-23", "2026-09-24", "2026-09-25"):
            self.assertEqual(self.closed_slots(A, d), len(SLOTS), d)
        self.assertEqual(self.open_gaps(A), [])
        for code in (A, B, INDEX):                                    # 决定 8：今天没有历史 closed 行
            self.assertEqual(self.closed_slots(code, DAY), 0, code)
            self.assertNotIn(DAY, self.finals(code), code)


class HistoryCoverageTests(Base):
    """F18：不只比较最新 final 日期，也看发现水位与未解决缺口；大缺口分片不误关整段。盘中进行。"""

    def test_latest_day_caught_up_but_middle_days_missing(self):
        self.seed(INDEX, "2026-09-25")
        self.seed(A, "2026-09-25", skip_day=("2026-09-23", "2026-09-24"),
                  skip_minute=("2026-09-23", "2026-09-24"), watermark="2026-09-22")
        c = self.make(FullFake(), watch=(A,))
        self.history(c, at(10, 0), rounds=3)
        self.assertEqual(self.finals(A), ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"])
        self.assertEqual(self.closed_slots(A, "2026-09-24"), len(SLOTS))

    def test_day_complete_but_minutes_missing(self):
        self.seed(INDEX, "2026-09-25")
        self.seed(A, "2026-09-25", skip_minute=("2026-09-24", "2026-09-25"))
        with facts.write_txn(self.conn):
            facts.set_setting(self.conn, f"discovered:{FACT}:{A}", "2026-09-23")
        provider = FullFake()
        c = self.make(provider, watch=(A,))
        self.history(c, at(10, 0), rounds=3)
        self.assertEqual([call[0] for call in provider.calls], [FACT])
        self.assertEqual((self.closed_slots(A, "2026-09-24"), self.closed_slots(A, "2026-09-25")), (len(SLOTS), len(SLOTS)))

    def test_watermark_advanced_but_gap_unresolved(self):
        self.seed(INDEX, "2026-09-25")
        self.seed(A, "2026-09-25", skip_day=("2026-09-23",), skip_minute=("2026-09-23",))
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, A, "day", "2026-09-23", "2026-09-23", "backfill")
            facts.record_gap(self.conn, A, FACT, "2026-09-23 09:30", "2026-09-23 15:00", "backfill")
        c = self.make(FullFake(), watch=(A,))
        self.history(c, at(10, 0), rounds=2)
        self.assertIn("2026-09-23", self.finals(A))
        self.assertEqual(self.open_gaps(A), [])

    def test_partial_return_does_not_close_large_gap(self):
        self.seed(INDEX, "2026-09-25")
        self.seed(A, "2026-09-25", skip_minute=tuple(_weekdays("2026-09-01", "2026-09-25")))
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, A, FACT, "2026-09-01 09:30", "2026-09-25 15:00", "backfill")
        provider = FullFake()
        provider.skip_days = ("2026-09-10",)                          # 上游这一段只缺一天
        c = self.make(provider, watch=(A,))
        self.history(c, at(10, 0))
        gaps = self.open_gaps(A)
        self.assertEqual([(g["start"], g["end"], g["attempts"]) for g in gaps],
                         [("2026-09-01 09:30", "2026-09-25 15:00", 1)])
        self.assertEqual(self.closed_slots(A, "2026-09-11"), len(SLOTS))  # 取回的部分照常可读


class ActiveOnlyInSessionTests(Base):
    """F21：盘中只处理跟踪代码（自选，A 股另加上证指数）；全库缺口只在没有市场开盘时续传，且只续传跟踪代码。"""

    def test_in_session_drains_tracked_codes_and_library_drain_skips_untracked(self):
        # 有意改写（目标 2026-09-29 第二阶段）：原 test_in_session_drains_only_active_codes_then_whole_library_after_close
        # 固定「收盘后全库续传处理非自选 X」；现在非自选的遗留缺口保留但不再续传
        self.seed(INDEX, "2026-09-25")
        for code in (A, X):
            self.seed(code, "2026-09-25", skip_minute=("2026-09-24",))
            with facts.write_txn(self.conn):
                facts.record_gap(self.conn, code, FACT, "2026-09-24 09:30", "2026-09-24 15:00", "backfill")
        provider = FullFake()
        c = self.make(provider, watch=(A,))
        self.history(c, at(10, 0))
        self.assertEqual({call[1] for call in provider.calls}, {A})
        self.history(c, at(15, 10))                                   # A 股收盘、港股仍在盘中
        self.assertEqual({call[1] for call in provider.calls}, {A})
        self.history(c, at(16, 20))
        self.assertEqual({call[1] for call in provider.calls}, {A})
        self.assertTrue(self.open_gaps(X))

    def test_index_first_in_cn_round(self):
        for code in (INDEX, A):
            self.seed(code, "2026-09-25", skip_minute=("2026-09-24",))
            with facts.write_txn(self.conn):
                facts.record_gap(self.conn, code, FACT, "2026-09-24 09:30", "2026-09-24 15:00", "backfill")
        provider = FullFake()
        c = self.make(provider, watch=(A, INDEX))   # 指数分钟缺口只在它被自选时续传（V17）；排序仍是指数在前
        self.history(c, at(10, 0))
        self.assertEqual([call[1] for call in provider.calls], [INDEX, A])


class SameGapTests(Base):
    """F24：请求路径追赶与历史线程的 drain_gaps 同时续传同一条缺口：至多一个在途，失败只计一次。"""

    def test_concurrent_drains_fetch_a_gap_once(self):
        self.seed(INDEX, "2026-09-25")
        self.seed(A, "2026-09-25", skip_day=("2026-09-23",))
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, A, "day", "2026-09-23", "2026-09-23", "backfill")
        gate, entered = threading.Event(), threading.Event()

        class Flaky(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                if len(self.calls) == 1:
                    entered.set()
                    gate.wait(10)
                raise ProviderError("503")

        provider = Flaky()
        c = self.make(provider, watch=(A,))
        self.clock.t = at(20, 0).timestamp()
        first = threading.Thread(target=c.drain_gaps, args=(4,), kwargs={"code": A, "since": "2026-09-01"},
                                 daemon=True)
        first.start()
        self.addCleanup(gate.set)
        self.assertTrue(entered.wait(10))
        c.drain_gaps(20, market="CN")                                 # 调度侧同时排空全库
        gate.set()
        first.join(10)
        self.assertEqual(len(provider.calls), 1)
        gap = facts.open_gaps(self.conn, A, "day")[0]
        self.assertEqual((gap["attempts"], gap["reason"]), (1, "backfill"))


class ThreadWiringTests(Base):
    """BACKFILL 的工作从调度 tick 移到独立守护线程：tick 不再发历史请求；start 起历史线程循环调用 history_tick。"""

    def test_tick_no_longer_does_history_work(self):
        provider = FullFake()
        c = self.make(provider, watch=(A,))
        self.clock.t = datetime(2026, 9, 26, 20, 0).timestamp()        # 周六：原来 tick 会跑 BACKFILL
        c.tick(datetime(2026, 9, 26, 20, 0))
        self.assertEqual(provider.calls, [])
        c.history_tick(datetime(2026, 9, 26, 20, 0))
        self.assertTrue([call for call in provider.calls if call[1] == A])

    def test_start_runs_history_loop_in_its_own_thread(self):
        c = self.make(FullFake(), watch=())
        called = threading.Event()
        names = []

        def fake_history_tick(now):
            names.append(threading.current_thread().name)
            called.set()

        with unittest.mock.patch.object(config, "HISTORY_INTERVAL_S", 0.01), \
                unittest.mock.patch.object(c, "history_tick", fake_history_tick):
            self.assertTrue(c.start())
            try:
                self.assertTrue(called.wait(5))
            finally:
                c.stop()
        self.assertEqual(names[0], "kline-history")
        self.assertFalse(any(t.name == "kline-history" and t.is_alive() for t in threading.enumerate()))

    def test_disabled_collector_history_tick_does_nothing(self):
        provider = FullFake()
        c = self.make(provider, watch=(A,))
        with unittest.mock.patch.dict("os.environ", {"COLLECTOR_ENABLED": "0"}):
            self.assertEqual(c.history_tick(datetime(2026, 9, 26, 20, 0)), {})
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
