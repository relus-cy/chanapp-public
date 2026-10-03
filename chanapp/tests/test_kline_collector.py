"""采集器：单写者、额度、熔断、缺口续传、不跨源回落（spec §5.1、§5.3、§9 D7）。"""
import tempfile
import contextlib
import unittest
import unittest.mock
from datetime import date, datetime, timedelta
from pathlib import Path

from chanapp.engine.kline import bindings, collector, facts, sessions
from chanapp.engine.kline.providers.raw import ProviderConnectionError, ProviderError
from chanapp.engine.kline.rows import FetchItem, RawDayRow, RawMinuteRow, new_batch_id

# A 股现行分钟事实粒度（2026-09-30 起 m15）：用例按它构造与断言，替身按所请求的粒度出网格
FACT = bindings.binding("CN", "stock", FetchItem.MINUTE_HISTORY).minute_fact_freq
DAY_VOL = len(sessions.slots("CN", FACT))      # 每槽 1 手：日线量等于分钟合计，核对一致
CHECKS = facts._checks_scope("sh600036", FACT)[0]   # 有意改写（第三阶段第二轮复审）：现行粒度的分钟日线核对表


class FakeProvider:
    name = "mairui"
    CONTRACT_VERSION = "fake"

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def day_history(self, code, start, end):
        self.calls.append(("day", code, start, end))
        if self.fail:
            raise ProviderError("down")
        return [RawDayRow(code, end, 10, 11, 9, 10, 1, "lot", 1, "CNY", 10, 0, "final", new_batch_id())]

    def minute_history(self, code, fact_freq, start, end, *, now):
        self.calls.append((fact_freq, code, start, end))
        if self.fail:
            raise ProviderError("down")
        day = end[:10]
        return [RawMinuteRow(code, day, f"{day} 15:00", 10, 10, 10, 10, 1, "lot", 1, "closed",
                             "traded", new_batch_id())]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _weekdays(start, end):
    day, last = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    while day <= last:
        if day.weekday() < 5:
            yield day.isoformat()
        day += timedelta(days=1)


class FullFake(FakeProvider):
    """每个工作日都有完整日线与所请求粒度的全部槽位（价格恒 10，分钟聚合与日线一致）。"""
    skip_days = ()

    def day_history(self, code, start, end):
        self.calls.append(("day", code, start, end))
        return [RawDayRow(code, d, 10, 10, 10, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final", new_batch_id())
                for d in _weekdays(start, end) if d not in self.skip_days]

    def minute_history(self, code, fact_freq, start, end, *, now):
        self.calls.append((fact_freq, code, start, end))
        return [RawMinuteRow(code, d, f"{d} {t}", 10, 10, 10, 10, 1, "lot", 1, "closed", "traded", new_batch_id())
                for d in _weekdays(start, end) if d not in self.skip_days
                for t in sessions.slots("CN", fact_freq) if start <= f"{d} {t}" <= end]


def _list_since(directory, codes, list_date):
    """登记上市日，把回填起点压到近几个月（测试不必取三年分钟）。"""
    conn = facts.open_facts(Path(directory) / facts.DB_NAME)
    with facts.write_txn(conn):
        for code in codes:
            conn.execute("INSERT OR REPLACE INTO instruments(code, name, list_date, delist_date, kind, source,"
                         " fetched_at) VALUES (?,?,?,?,?,?,?)",
                         (code, code, list_date, None, "stock", "test", facts.now_iso()))
    conn.close()


def _revised_minutes(directory, code, days, *, quarantine_day):
    """每个交易日先写 forming 再定稿成 closed（每槽两个 revision），并隔离 quarantine_day 的日线：
    minute_bars 累计行数是当前可读已收盘分钟的数倍。"""
    hk = code.startswith("hk")
    market, fact = ("HK", "m30") if hk else ("CN", FACT)
    price, volume, unit, factor = (100, 10, "share", None) if hk else (10, 1, "lot", 1)
    conn = facts.open_facts(Path(directory) / facts.DB_NAME)
    for d in days:
        for state, item in (("forming", "minute_live"), ("closed", "minute_history")):
            rows = [RawMinuteRow(code, d, f"{d} {t}", price, price, price, price, volume, unit, factor, state,
                                 "traded", new_batch_id()) for t in sessions.slots(market, fact)]
            facts.commit_minute_rows(conn, rows, market=market, kind="stock", item=item, fact_freq=fact,
                                     source="longbridge" if hk else "mairui", binding_gen=1, today=d)
    with facts.write_txn(conn):
        facts.quarantine(conn, code, "day", quarantine_day, "review_conflict")
    return conn


def _readable_closed(conn, code, fact):
    hidden = facts.quarantined_keys(conn, code, fact)
    days = facts.quarantined_keys(conn, code, "day")
    return sum(1 for r in facts.read_minute_rows(conn, code, fact)
               if r["state"] == "closed" and r["slot_end"] not in hidden and r["trade_date"] not in days)


def _enable_collector(case):
    """验收命令整体以 COLLECTOR_ENABLED=0 运行；调度与首取用例在这里显式打开开关。"""
    patcher = unittest.mock.patch.dict("os.environ", {"COLLECTOR_ENABLED": "1"})
    patcher.start()
    case.addCleanup(patcher.stop)


def _pin_mechanism_schedule(case):
    """定稿机制用例（台账、间隔、次数上限、暂缓、重启、追赶……）按 2026-09-30 之前的 A 股日程编写：首个定稿时点
    17:30、日历未知时 17:30/19:30/21:30、stale 截止 18:30。它们检验的是机制而非时点，一天内要走满 3 次尝试
    才需要三个时点，所以固定在这个日程上；生产日程（20:00 起、22:00 重试、21:00 截止）由 test_cn_* 用例单独覆盖。"""
    if getattr(case, "real_schedule", False):
        return
    for patcher in (unittest.mock.patch.dict(collector._FINALIZE_SLOTS, {"CN": ("17:30", "19:30", "21:30")}),
                    unittest.mock.patch.dict(collector.config.FINALIZE_DEADLINE, {"CN": "18:30"})):
        patcher.start()
        case.addCleanup(patcher.stop)



class BreakerTests(unittest.TestCase):
    def test_cooldown_expires_then_success_clears_failures(self):
        now = [1000.0]
        breaker = collector.Breaker(2, 300, lambda: now[0])
        breaker.record("mairui", False)
        breaker.record("mairui", False)
        self.assertFalse(breaker.allow("mairui"))
        now[0] += 301
        self.assertTrue(breaker.allow("mairui"))      # 冷却到期后放行重试
        breaker.record("mairui", False)
        self.assertTrue(breaker.allow("mairui"))      # 到期后计数从零重来，一次失败不再冷却
        breaker.record("mairui", True)
        breaker.record("mairui", False)
        self.assertTrue(breaker.allow("mairui"))      # 成功清零

class CollectorTests(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        _pin_mechanism_schedule(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = Clock(datetime(2026, 9, 26, 20, 0).timestamp())

    def make(self, provider, cold=None):
        providers = {"mairui": provider}
        if cold:
            providers["baostock"] = cold
        return collector.Collector(self.dir, providers=providers, clock=self.clock,
                                   watchlist_fn=lambda: ["sh600036"])

    def test_second_writer_is_locked_out(self):
        # 两个实例各自打开锁文件：flock 按打开的文件描述计，同进程内也互斥，等价于第二个进程
        c1, c2 = self.make(FakeProvider()), self.make(FakeProvider())
        with c1.writer():
            with self.assertRaises(collector.CollectorLocked):
                with c2.writer():
                    pass

    def test_budget_persists_and_keeps_reserve(self):
        conn_factory = lambda: facts.open_facts(self.dir / facts.DB_NAME)
        budget = collector.Budget(conn_factory, source="mairui", per_minute=100, per_day=10,
                                  reserve=0.2, clock=self.clock)
        self.assertEqual(sum(budget.take() for _ in range(10)), 8)
        self.assertTrue(budget.take(recovery=True))
        again = collector.Budget(conn_factory, source="mairui", per_minute=100, per_day=10,
                                 reserve=0.2, clock=self.clock)
        self.assertFalse(again.take())

    def test_failure_never_calls_cold_standby(self):
        cold = FakeProvider()
        c = self.make(FakeProvider(fail=True), cold=cold)
        c.backfill_day("sh600036")
        self.assertEqual(cold.calls, [])
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertTrue(facts.open_gaps(conn, "sh600036", "day"))

    def test_backfill_resumes_from_gaps_after_budget_exhaustion(self):
        _list_since(self.dir, ["sh600036"], "2026-05-01")
        provider = FullFake()
        c = self.make(provider)
        planned = c.plan_minute_backfill("sh600036")
        self.assertEqual(planned, 5)                        # 2026-05 … 2026-09 各一片
        first = c.drain_gaps(max_requests=2)
        self.assertEqual(first["done"], 2)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual(len(facts.open_gaps(conn, "sh600036", FACT)), planned - 2)
        c.drain_gaps(max_requests=100)
        self.assertEqual(facts.open_gaps(conn, "sh600036", FACT), [])
        fetched = [call[2][:7] for call in provider.calls if call[0] == FACT]
        self.assertEqual(len(fetched), len(set(fetched)))   # 同一个月不重复取

    def test_sparse_minutes_keep_gap_open(self):
        # 每月只返回一根分钟：槽位不全的日子不算覆盖，缺口保持打开并计一次尝试（原用例断言全部关闭是错的）
        _list_since(self.dir, ["sh600036"], "2026-07-01")
        c = self.make(FakeProvider())
        planned = c.plan_minute_backfill("sh600036")
        result = c.drain_gaps(max_requests=100)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        gaps = facts.open_gaps(conn, "sh600036", FACT)
        self.assertEqual(len(gaps), planned)
        self.assertEqual({g["attempts"] for g in gaps}, {1})
        self.assertEqual((result["done"], result["incomplete"]), (0, planned))

    def test_gap_resolves_when_only_quarantined_slot_is_missing(self):
        class MissingOne(FullFake):
            def minute_history(self, code, fact_freq, start, end, *, now):
                return [r for r in super().minute_history(code, fact_freq, start, end, now=now)
                        if r.slot_end != "2026-09-24 10:00"]

        c = self.make(MissingOne())
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", FACT, "2026-09-24 09:30", "2026-09-24 15:00", "backfill")
            facts.quarantine(conn, "sh600036", FACT, "2026-09-24 10:00", "proven_wrong")
        c.drain_gaps(max_requests=10)
        self.assertEqual(facts.open_gaps(conn, "sh600036", FACT), [])

    def test_all_rejected_rows_do_not_resolve_gap(self):
        class OffGrid(FakeProvider):
            def day_history(self, code, start, end):
                return [RawDayRow(code, "2026-09-24", 10, 9, 11, 10, 1, "lot", 1, "CNY", 10, 0, "final",
                                  new_batch_id())]                   # high < low：准入拒绝

            def minute_history(self, code, fact_freq, start, end, *, now):
                return [RawMinuteRow(code, "2026-09-24", "2026-09-24 12:00", 10, 10, 10, 10, 1, "lot", 1,
                                     "closed", "traded", new_batch_id())]   # 不在会话槽位上

        c = self.make(OffGrid())
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", FACT, "2026-09-24 09:30", "2026-09-24 15:00", "backfill")
            facts.record_gap(conn, "sh600036", "day", "2026-09-24", "2026-09-24", "finalize")
        c.drain_gaps(max_requests=10)
        gaps = facts.open_gaps(conn, "sh600036")
        self.assertEqual({(g["dataset"], g["attempts"]) for g in gaps}, {(FACT, 1), ("day", 1)})

    def test_drain_resolves_already_covered_gap_without_fetching(self):
        provider = FullFake()
        c = self.make(provider)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        facts.commit_day_rows(conn, provider.day_history("sh600036", "2026-09-24", "2026-09-24"), market="CN",
                              kind="stock", item="day_history", source="mairui", binding_gen=1, today="2026-09-26")
        provider.calls.clear()
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", "day", "2026-09-24", "2026-09-24", "write_failed")
        c.drain_gaps(max_requests=10)
        self.assertEqual((facts.open_gaps(conn, "sh600036"), provider.calls), ([], []))

    def test_drain_leaves_todays_gaps_to_finalize(self):
        # 今天的分钟只来自盘中与定稿（计划 A 决定 8）：BACKFILL 在 15:05 后、定稿前不得用历史接口取今天
        self.clock.t = datetime(2026, 9, 28, 15, 10).timestamp()
        provider = FullFake()
        c = self.make(provider)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", FACT, "2026-09-28 09:30", "2026-09-28 15:00", "write_failed")
        c.drain_gaps(max_requests=10)
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(facts.open_gaps(conn, "sh600036", FACT)), 1)

    def test_drained_minutes_reconcile_against_existing_final_day(self):
        provider = FullFake()
        c = self.make(provider)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        facts.commit_day_rows(conn, [RawDayRow("sh600036", d, 10, 11, 10, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                                               new_batch_id()) for d in ("2026-09-24", "2026-09-25")],
                              market="CN", kind="stock", item="day_history", source="mairui", binding_gen=1,
                              today="2026-09-26")
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", FACT, "2026-09-24 09:30", "2026-09-24 15:00", "backfill")
        c.drain_gaps(max_requests=10)
        checks = [(r["trade_date"], r["status"]) for r in conn.execute(f"SELECT * FROM {CHECKS}")]
        self.assertEqual(checks, [("2026-09-24", "pending_review")])   # 09-25 没写分钟，不核对

    def test_restart_after_one_day_downtime_discovers_missing_day(self):
        # 周二整天停机、周三晚上重启：定稿先写了周三，已规划标记不能挡住对周二的发现与续传。
        # 有意改写（目标 2026-09-29 第二阶段）：不在自选的上证指数只作系统依赖，只补日线，不再要求它的分钟
        _list_since(self.dir, ["sh600036", "sh000001"], "2026-09-01")
        provider = FullFake()
        c = self.make(provider)
        self.clock.t = datetime(2026, 9, 19, 20, 0).timestamp()
        c.tick(datetime(2026, 9, 19, 20, 0))
        c.history_tick(datetime(2026, 9, 19, 20, 0))          # 周六：首次规划并补齐（历史追赶线程）
        c.finalize_due(["sh000001", "sh600036"], "2026-09-21", datetime(2026, 9, 21, 17, 30), calendar_known=True)
        restarted = self.make(provider)
        self.clock.t = datetime(2026, 9, 23, 20, 0).timestamp()
        restarted.tick(datetime(2026, 9, 23, 20, 0))          # 调度：周三定稿
        restarted.history_tick(datetime(2026, 9, 23, 20, 0))  # 历史追赶：发现并补回周二
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        # 指数不在自选里：调度线程只为它定稿当日日线（系统依赖，第二阶段评审修复后）。定稿前先登记它停机期间缺的
        # 周二，否则周三 final 一写入，日历推导会把周二当成休市，个股周二也就不补了（修复中实测发现）
        for code, expected in (("sh600036", ["2026-09-21", "2026-09-22", "2026-09-23"]),
                               ("sh000001", ["2026-09-21", "2026-09-22", "2026-09-23"])):
            days = [r["trade_date"] for r in facts.read_day_rows(conn, code, "2026-09-21", "2026-09-23")
                    if r["provenance"] == "final"]
            self.assertEqual(days, expected, code)
        slots = facts.read_minute_rows(conn, "sh600036", FACT, "2026-09-22 00:00", "2026-09-22 23:59")
        self.assertEqual(len(slots), DAY_VOL)
        self.assertEqual(facts.open_gaps(conn, "sh600036"), [])

    def test_finalize_requires_minutes_actually_written(self):
        class NoMinutes(FullFake):
            def minute_history(self, code, fact_freq, start, end, *, now):
                return []

        c = self.make(NoMinutes())
        result = c.finalize_due(["sh600036"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)
        self.assertEqual((result["done"], result["failed"]), ([], ["sh600036"]))
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual({g["dataset"] for g in facts.open_gaps(conn, "sh600036")}, {"day", FACT})

    def finalize_gaps(self):
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        return {(g["dataset"], g["reason"]) for g in facts.open_gaps(conn, "sh600036")}

    def test_finalize_with_single_minute_slot_is_not_done(self):
        # 分钟只写入一个槽：按会话槽位判覆盖，不因「有一根」算成功
        class OneSlot(FullFake):
            def minute_history(self, code, fact_freq, start, end, *, now):
                return super().minute_history(code, fact_freq, start, end, now=now)[-1:]

        result = self.make(OneSlot()).finalize_due(["sh600036"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)
        self.assertEqual((result["done"], result["failed"]), ([], ["sh600036"]))
        self.assertEqual(self.finalize_gaps(), {("day", "finalize"), (FACT, "finalize")})

    def test_finalize_with_reconcile_mismatch_is_not_done(self):
        # 分钟齐全但与日线核对不一致（日线最高 11，分钟全 10）：不算成功，留核对证据
        class Mismatch(FullFake):
            def day_history(self, code, start, end):
                return [RawDayRow(code, end, 10, 11, 10, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final", new_batch_id())]

        result = self.make(Mismatch()).finalize_due(["sh600036"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)
        self.assertEqual(result["review"], ["sh600036"])
        self.assertEqual(self.finalize_gaps(), {("day", "finalize"), (FACT, "finalize")})
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual([r["status"] for r in conn.execute(f"SELECT status FROM {CHECKS}")], ["pending_review"])

    def test_finalize_with_slots_sent_to_review_is_not_done(self):
        # 混合批次：两个槽与已存 closed 值冲突（只差成交额）转待核验，其余照常写入；待核验的槽不算已定稿
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        old = [RawMinuteRow("sh600036", "2026-09-28", f"2026-09-28 {t}", 10, 10, 10, 10, 1, "lot", 2, "closed",
                            "traded", new_batch_id()) for t in sessions.slots("CN", FACT)[:2]]
        facts.commit_minute_rows(conn, old, market="CN", kind="stock", item="minute_history", fact_freq=FACT,
                                 source="mairui", binding_gen=1, today="2026-09-28")
        result = self.make(FullFake()).finalize_due(["sh600036"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)
        self.assertEqual(result["review"], ["sh600036"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_review").fetchone()[0], 2)
        self.assertEqual(self.finalize_gaps(), {("day", "finalize"), (FACT, "finalize")})

    def test_finalize_with_rejected_day_is_not_done(self):
        class BadDay(FullFake):
            def day_history(self, code, start, end):
                return [RawDayRow(code, end, 10, 9, 11, 10, 1, "lot", 1, "CNY", 10, 0, "final", new_batch_id())]

        c = self.make(BadDay())
        result = c.finalize_due(["sh600036"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)
        self.assertEqual(result["failed"], ["sh600036"])

    def test_finalize_suspended_day_needs_no_minutes(self):
        class Suspended(FullFake):
            def day_history(self, code, start, end):
                return [RawDayRow(code, end, 10, 10, 10, 10, 0, "lot", 0, "CNY", 10, 1, "final", new_batch_id())]

            def minute_history(self, code, fact_freq, start, end, *, now):
                return []

        c = self.make(Suspended())
        self.assertEqual(c.finalize_due(["sh600036"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)["done"], ["sh600036"])

    def test_finalize_write_failure_registers_gaps_and_continues(self):
        c = self.make(FullFake())
        real = facts.commit_minute_rows

        def flaky(conn, rows, **kw):
            if rows and rows[0].code == "sh600036":
                raise facts.FactsWriteError("disk full")
            return real(conn, rows, **kw)

        with unittest.mock.patch.object(facts, "commit_minute_rows", flaky):
            result = c.finalize_due(["sh600036", "sz000001"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)
        self.assertEqual((result["failed"], result["done"]), (["sh600036"], ["sz000001"]))
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual({(g["dataset"], g["reason"]) for g in facts.open_gaps(conn, "sh600036")},
                         {("day", "finalize"), (FACT, "finalize")})

    def test_backfill_write_failure_does_not_stop_other_codes(self):
        # 第一个标的日线回填写失败：登记缺口后，本市场余下标的的回填照常进行
        _list_since(self.dir, ["sh600036", "sz000001", "sh000001"], "2026-09-01")
        c = self.make(FullFake())
        c.watchlist_fn = lambda: ["sh600036", "sz000001"]
        real = facts.commit_day_rows

        def flaky(conn, rows, **kw):
            if rows and rows[0].code == "sh600036":
                raise facts.FactsWriteError("disk full")
            return real(conn, rows, **kw)

        with unittest.mock.patch.object(facts, "commit_day_rows", flaky):
            c.history_tick(datetime(2026, 9, 26, 20, 0))          # 周六：历史追赶（原 BACKFILL）
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual({(g["dataset"], g["reason"]) for g in facts.open_gaps(conn, "sh600036", "day")},
                         {("day", "write_failed")})
        self.assertTrue(facts.read_day_rows(conn, "sz000001"))
        self.assertIsNotNone(facts.setting(conn, "backfill_planned:sz000001"))
        self.assertIsNotNone(facts.setting(conn, collector.plan_key("sz000001", "minute")))

    def open_calendar(self, start, end):
        """已知开市日（供应商年表）：取数后的覆盖判定才会把缺席的工作日当成应有。"""
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            for d in _weekdays(start, end):
                conn.execute("INSERT OR REPLACE INTO calendar(market, date, is_open, sessions, source, fetched_at)"
                             " VALUES ('CN', ?, 1, '[]', 'test', ?)", (d, facts.now_iso()))
        return conn

    def test_first_backfill_with_rejected_row_registers_gap(self):
        class BadOne(FullFake):
            def day_history(self, code, start, end):
                rows = super().day_history(code, start, end)
                return [RawDayRow(code, r.trade_date, 10, 9, 11, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                                  new_batch_id()) if r.trade_date == "2026-09-15" else r for r in rows]

        _list_since(self.dir, ["sh600036"], "2026-09-01")
        c = self.make(BadOne())
        c.backfill_day("sh600036")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual([(g["start"], g["end"], g["reason"]) for g in facts.open_gaps(conn, "sh600036", "day")],
                         [("2026-09-15", "2026-09-15", "rejected")])
        self.assertIsNotNone(facts.setting(conn, "backfill_planned:sh600036"))

    def test_first_backfill_missing_known_trading_days_registers_gaps(self):
        # 上游漏掉已知开市日（日历已知）：按连续区段登记缺口，已规划标记照常写
        conn = self.open_calendar("2026-09-01", "2026-09-25")
        _list_since(self.dir, ["sh600036"], "2026-09-01")
        provider = FullFake()
        provider.skip_days = ("2026-09-01", "2026-09-02", "2026-09-10")
        self.make(provider).backfill_day("sh600036")
        self.assertEqual(sorted((g["start"], g["end"]) for g in facts.open_gaps(conn, "sh600036", "day")),
                         [("2026-09-01", "2026-09-02"), ("2026-09-10", "2026-09-10")])

    def test_listed_stock_gap_missing_range_start_stays_open(self):
        # 老股（上市日已知）补区间：上游漏掉区间开头，首根之前不能当成未上市
        conn = self.open_calendar("2026-09-01", "2026-09-10")
        _list_since(self.dir, ["sh600036"], "2000-01-04")
        provider = FullFake()
        provider.skip_days = ("2026-09-01", "2026-09-02")
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", "day", "2026-09-01", "2026-09-10", "backfill")
        self.make(provider).drain_gaps(max_requests=10)
        gaps = facts.open_gaps(conn, "sh600036", "day")
        self.assertEqual([(g["start"], g["attempts"]) for g in gaps], [("2026-09-01", 1)])

    def test_rejected_index_day_is_not_derived_as_closed(self):
        # 指数回填中被拒的日子登记缺口，日历推导不把它推成休市（留作未知）
        class BadIndex(FullFake):
            def day_history(self, code, start, end):
                rows = super().day_history(code, "2026-09-01", end)
                return [RawDayRow(code, r.trade_date, 10, 9, 11, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                                  new_batch_id()) if r.trade_date == "2026-09-16" else r for r in rows]

        from chanapp.engine.kline import calendar
        c = self.make(BadIndex())
        c.backfill_day("sh000001")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual([(g["start"], g["end"]) for g in facts.open_gaps(conn, "sh000001", "day")],
                         [("2026-09-16", "2026-09-16")])
        self.assertIsNone(calendar.is_trading_day(conn, "CN", "2026-09-16"))
        self.assertTrue(calendar.is_trading_day(conn, "CN", "2026-09-17"))

    def test_rejected_index_day_gap_not_closed_by_empty_refetch(self):
        # 被拒日单独登记 rejected 缺口，按严格口径：续传返回空不关闭，之后再推日历也不把它推成休市；
        # 续传取回合法行才关闭
        class BadIndex(FullFake):
            def day_history(self, code, start, end):
                rows = super().day_history(code, "2026-09-01", end)
                return [RawDayRow(code, r.trade_date, 10, 9, 11, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                                  new_batch_id()) if r.trade_date == "2026-09-16" else r for r in rows]

        class Empty(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return []

        from chanapp.engine.kline import calendar
        self.make(BadIndex()).backfill_day("sh000001")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual([(g["start"], g["end"], g["reason"]) for g in facts.open_gaps(conn, "sh000001", "day")],
                         [("2026-09-16", "2026-09-16", "rejected")])
        empty = Empty()
        self.make(empty).drain_gaps(max_requests=10)
        self.assertEqual(len(empty.calls), 1)
        gaps = facts.open_gaps(conn, "sh000001", "day")
        self.assertEqual([(g["start"], g["attempts"]) for g in gaps], [("2026-09-16", 1)])
        with facts.write_txn(conn):
            calendar.derive_cn_past(conn)
        self.assertIsNone(calendar.is_trading_day(conn, "CN", "2026-09-16"))
        self.make(FullFake()).drain_gaps(max_requests=10)
        self.assertEqual(facts.open_gaps(conn, "sh000001", "day"), [])
        self.assertTrue(calendar.is_trading_day(conn, "CN", "2026-09-16"))

    def test_day_rejected_during_gap_drain_is_tracked_strictly(self):
        # 首次整段回填失败 → 续传时某日被拒：该日单独记 rejected 缺口（严格口径），父缺口按其余日子关闭；
        # 再续传返回空不关闭它，之后推日历也不把它推成休市
        class Down(FullFake):
            def day_history(self, code, start, end):
                raise ProviderError("down")

        class BadIndex(FullFake):
            def day_history(self, code, start, end):
                rows = super().day_history(code, max(start, "2026-09-01"), end)
                return [RawDayRow(code, r.trade_date, 10, 9, 11, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                                  new_batch_id()) if r.trade_date == "2026-09-16" else r for r in rows]

        class Empty(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return []

        from chanapp.engine.kline import calendar
        self.make(Down()).backfill_day("sh000001")
        self.make(BadIndex()).drain_gaps(max_requests=10)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual([(g["start"], g["end"], g["reason"]) for g in facts.open_gaps(conn, "sh000001", "day")],
                         [("2026-09-16", "2026-09-16", "rejected")])
        self.make(Empty()).drain_gaps(max_requests=10)
        self.assertEqual([g["start"] for g in facts.open_gaps(conn, "sh000001", "day")], ["2026-09-16"])
        with facts.write_txn(conn):
            calendar.derive_cn_past(conn)
        self.assertIsNone(calendar.is_trading_day(conn, "CN", "2026-09-16"))

    def test_rejected_days_around_unknown_holiday_are_separate_gaps(self):
        # 两个被拒日之间夹着上游没返回、日历未知的工作日（假日）：各自登记单日缺口，不把假日并进必须覆盖的区段；
        # 两日补齐后缺口关闭，假日由指数连续性推为休市
        class BadIndex(FullFake):
            skip_days = ("2026-09-16",)

            def day_history(self, code, start, end):
                rows = super().day_history(code, max(start, "2026-09-01"), end)
                return [RawDayRow(code, r.trade_date, 10, 9, 11, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                                  new_batch_id()) if r.trade_date in ("2026-09-15", "2026-09-17") else r
                        for r in rows]

        class Holiday(FullFake):
            skip_days = ("2026-09-16",)

        from chanapp.engine.kline import calendar
        self.make(BadIndex()).backfill_day("sh000001")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual(sorted((g["start"], g["end"]) for g in facts.open_gaps(conn, "sh000001", "day")),
                         [("2026-09-15", "2026-09-15"), ("2026-09-17", "2026-09-17")])
        self.make(Holiday()).drain_gaps(max_requests=10)
        self.assertEqual(facts.open_gaps(conn, "sh000001", "day"), [])
        self.assertIs(calendar.is_trading_day(conn, "CN", "2026-09-16"), False)

    def test_single_day_backfill_gap_rejected_is_upgraded_not_closed(self):
        # 单日 backfill 缺口（如停机补洞发现的日子）续传取回被拒行：缺口原地升级为 rejected（严格口径），当轮不关闭，
        # 再续传返回空也不关闭
        class BadIndex(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return [RawDayRow(code, d, 10, 9, 11, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final", new_batch_id())
                        for d in _weekdays(start, end)]

        class Empty(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return []

        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh000001", "day", "2026-09-16", "2026-09-16", "backfill")
        self.make(BadIndex()).drain_gaps(max_requests=10)
        self.assertEqual([(g["start"], g["reason"]) for g in facts.open_gaps(conn, "sh000001", "day")],
                         [("2026-09-16", "rejected")])
        self.make(Empty()).drain_gaps(max_requests=10)
        self.assertEqual([g["start"] for g in facts.open_gaps(conn, "sh000001", "day")], ["2026-09-16"])

    def test_parent_gap_excludes_known_open_day_tracked_as_rejected(self):
        # 日历已知开市的日子在父缺口续传中被拒：由单日 rejected 缺口承接，父缺口按其余日子关闭，之后只取这一天
        class BadIndex(FullFake):
            def day_history(self, code, start, end):
                rows = super().day_history(code, start, end)
                return [RawDayRow(code, r.trade_date, 10, 9, 11, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                                  new_batch_id()) if r.trade_date == "2026-09-16" else r for r in rows]

        from chanapp.engine.kline import calendar
        from chanapp.engine.kline.rows import CalendarRow
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            calendar.store_rows(conn, [CalendarRow("CN", d, True) for d in _weekdays("2026-09-14", "2026-09-18")],
                                source="t")
            facts.record_gap(conn, "sh000001", "day", "2026-09-14", "2026-09-18", "backfill")
        self.make(BadIndex()).drain_gaps(max_requests=10)
        self.assertEqual([(g["start"], g["end"], g["reason"]) for g in facts.open_gaps(conn, "sh000001", "day")],
                         [("2026-09-16", "2026-09-16", "rejected")])
        again = BadIndex()
        self.make(again).drain_gaps(max_requests=10)
        self.assertEqual(again.calls, [("day", "sh000001", "2026-09-16", "2026-09-16")])

    def test_ensure_window_write_failure_registers_gap(self):
        # 有意改写（目标 2026-09-29 第二阶段）：原用非自选 sz000001；非自选的窗口首取不登记缺口（下次打开按覆盖重判），
        # 登记待补只属于跟踪代码，这里改用自选 sh600036
        # 再次有意改写（终审应修）：刚加入、未整段回填的自选首开只补窗口（不登记缺口），这里先标已回填，测已回填自选的首取
        _list_since(self.dir, ["sh600036", "sh000001"], "2026-08-01")
        c = self.make(FullFake())
        with facts.write_txn(c.conn()):
            facts.set_setting(c.conn(), collector.plan_key("sh600036", "backfill"), "2026-09-26")
        with unittest.mock.patch.object(facts, "commit_minute_rows",
                                        side_effect=facts.FactsWriteError("disk full")):
            c.ensure_window("sh600036", "m30")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        reasons = {(g["start"], g["reason"]) for g in facts.open_gaps(conn, "sh600036", FACT)}
        self.assertIn(("2026-09-01 09:30", "write_failed"), reasons)

    def test_m60_after_m30_window_fetches_enough_minutes(self):
        # A 股先开 30 分（取够 520 根 m30），再按 m60 补窗口：分钟事实继续取到 520 根 m60 所需（m15 事实为 520×4 根）
        _list_since(self.dir, ["sz000001", "sh000001"], "2025-06-01")
        c = self.make(FullFake())
        c.ensure_window("sz000001", "m30")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        count = lambda: len([r for r in facts.read_minute_rows(conn, "sz000001", FACT) if r["state"] == "closed"])
        per_hour = 60 // sessions.FREQ_MINUTES[FACT]
        self.assertLess(count(), 520 * per_hour)
        c.ensure_window("sz000001", "m60")
        self.assertGreaterEqual(count(), 520 * per_hour)

    def test_window_counts_readable_minutes_not_revisions(self):
        # minute_bars 累计 revision 数 ≥ 所需、当前可读已收盘分钟 < 所需：补窗口仍要继续取
        _list_since(self.dir, ["sz000001", "sh000001"], "2026-08-01")
        conn = _revised_minutes(self.dir, "sz000001", ["2026-09-24", "2026-09-25"], quarantine_day="2026-09-25")
        self.addCleanup(conn.close)
        need = 10 * 6                                         # 10 根 m30 = 60 根 m5
        total = conn.execute("SELECT COUNT(*) FROM minute_bars WHERE code='sz000001'").fetchone()[0]
        self.assertGreaterEqual(total, need)
        self.assertLess(_readable_closed(conn, "sz000001", FACT), need)
        provider = FullFake()
        self.make(provider).ensure_window("sz000001", "m30", bars=10)
        self.assertTrue([c for c in provider.calls if c[0] == FACT and c[1] == "sz000001"])
        self.assertGreaterEqual(_readable_closed(conn, "sz000001", FACT), need)

    def test_intraday_write_failure_registers_gap_for_today(self):
        class Live(FullFake):
            def minute_live(self, code, fact_freq, *, now):
                return [RawMinuteRow(code, "2026-09-28", "2026-09-28 10:00", 10, 10, 10, 10, 1, "lot", 1,
                                     "forming", "traded", new_batch_id())]

        c = self.make(Live())
        with unittest.mock.patch.object(facts, "commit_minute_rows",
                                        side_effect=facts.FactsWriteError("disk full")):
            c.intraday_tick(["sh600036"], datetime(2026, 9, 28, 10, 0, 5))
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        gaps = facts.open_gaps(conn, "sh600036", FACT)
        self.assertEqual([(g["start"][:10], g["reason"]) for g in gaps], [("2026-09-28", "write_failed")])

    def test_repeated_failures_suppress_further_calls(self):
        # 单标的退避（2 次）先于熔断（3 次）生效；两者都要求之后不再打上游
        c = self.make(FakeProvider(fail=True))
        for _ in range(3):
            c.backfill_day("sh600036")
        calls_before = len(c.providers["mairui"].calls)
        c.backfill_day("sh600036")
        self.assertEqual(len(c.providers["mairui"].calls), calls_before)

    def test_disabled_collector_never_touches_provider(self):
        provider = FakeProvider()
        c = self.make(provider)
        with unittest.mock.patch.dict("os.environ", {"COLLECTOR_ENABLED": "0"}):
            self.assertFalse(c.ensure_window("sz000001", "day"))
        self.assertEqual(provider.calls, [])

    def test_server_error_backs_off_code_but_not_source(self):
        class Flaky(FakeProvider):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                if code.startswith("sh6888"):
                    raise collector.ProviderServerError("HTTP 500")
                return super().day_history(code, start, end)

        c = self.make(Flaky())
        for code in ("sh688806", "sh688807", "sh688808", "sh688809"):   # 各 1 次：单标的退避不会先挡住
            Flaky.bad = code
            c.backfill_day(code)
        c.backfill_day("sh600036")
        self.assertIn(("day", "sh600036"), [(k, code) for k, code, *_ in c.providers["mairui"].calls])

    def test_upstream_fetch_runs_outside_writer_lock(self):
        seen = []

        class Probe(FakeProvider):
            def day_history(inner, code, start, end):
                seen.append(c._lock_held())
                return FakeProvider.day_history(inner, code, start, end)

        c = self.make(Probe())
        c.backfill_day("sh600036")
        self.assertEqual(seen, [False])

    def test_ensure_window_in_session_excludes_today(self):
        # 盘中首次打开非自选：历史接口不取今天，今日不出现 closed 分钟行（closed 只来自定稿）
        self.clock.t = datetime(2026, 9, 28, 10, 30).timestamp()
        provider = FakeProvider()
        c = self.make(provider)
        with unittest.mock.patch.dict("os.environ", {"COLLECTOR_ENABLED": "1"}):
            c.ensure_window("sz000001", "m30")
        ends = [call[3][:10] for call in provider.calls if call[0] == FACT]
        self.assertTrue(ends)
        self.assertTrue(all(end < "2026-09-28" for end in ends), ends)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        today = facts.read_minute_rows(conn, "sz000001", FACT, "2026-09-28 00:00", "2026-09-28 23:59")
        self.assertEqual([r for r in today if r["state"] == "closed"], [])

    def test_concurrent_first_fetch_requests_upstream_once(self):
        # spec D7 防重复请求：同一代码的并发首取只有一个在途，其余等它完成后直接读结果
        import threading
        entered, release = threading.Event(), threading.Event()

        class Slow(FullFake):
            def day_history(inner, code, start, end):
                if code == "sz000001":
                    entered.set()
                    release.wait(5)
                return FullFake.day_history(inner, code, start, end)

        _list_since(self.dir, ["sz000001", "sh000001"], "2026-09-01")
        provider = Slow()
        c = self.make(provider)
        results = []
        worker = lambda: results.append(c.ensure_window("sz000001", "day"))
        first = threading.Thread(target=worker)
        first.start()
        self.assertTrue(entered.wait(5))
        second = threading.Thread(target=worker)
        second.start()
        second.join(0.3)                                    # 第二个调用在等，不自己发请求
        release.set()
        first.join(5)
        second.join(5)
        full = [call for call in provider.calls if call[:2] == ("day", "sz000001")]
        self.assertEqual(len(full), 1)
        self.assertEqual(results, [True, True])

    def test_background_plan_and_first_open_do_not_both_backfill(self):
        import threading
        entered, release = threading.Event(), threading.Event()

        class Slow(FullFake):
            def day_history(inner, code, start, end):
                if code == "sz000001":
                    entered.set()
                    release.wait(5)
                return FullFake.day_history(inner, code, start, end)

        _list_since(self.dir, ["sz000001", "sh000001"], "2026-09-01")
        provider = Slow()
        # 后台规划只对自选（第二阶段评审修复：规划前复核资格）
        c = collector.Collector(self.dir, providers={"mairui": provider}, clock=self.clock,
                                watchlist_fn=lambda: ["sh600036", "sz000001"])
        background = threading.Thread(target=c.history_tick, args=(datetime.fromtimestamp(self.clock.t),))
        background.start()
        self.assertTrue(entered.wait(5))
        opener = threading.Thread(target=c.ensure_window, args=("sz000001", "day"))
        opener.start()
        opener.join(0.3)
        release.set()
        background.join(5)
        opener.join(5)
        self.assertEqual(len([call for call in provider.calls if call[:2] == ("day", "sz000001")]), 1)

    def test_viewing_window_expires(self):
        c = self.make(FakeProvider())
        c.touch_viewing("sz000001")
        now = datetime.fromtimestamp(self.clock.t)
        self.assertIn("sz000001", c.active_codes(now))
        self.clock.t += 601
        self.assertNotIn("sz000001", c.active_codes(datetime.fromtimestamp(self.clock.t)))

    def test_unverified_capability_does_not_trip_source(self):
        from chanapp.engine.kline.providers.raw import ProviderUnsupported

        class IndexLive(FakeProvider):
            def minute_live(self, code, fact_freq, *, now):
                self.calls.append(("live", code))
                if code.startswith(("sh000", "sz399")):
                    raise ProviderUnsupported("index latest unverified")
                return []

        c = self.make(IndexLive())
        now = datetime(2026, 9, 28, 10, 0)
        c.intraday_tick(["sh000001", "sh000688", "sz399006", "sh000300"], now)
        c.intraday_tick(["sh600036"], now)
        self.assertIn(("live", "sh600036"), c.providers["mairui"].calls)
        self.assertEqual(c._budget("mairui").used(), 1)          # 未验证能力不占额度

    def test_connection_errors_on_three_codes_cool_the_source(self):
        class Down(FakeProvider):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                raise ProviderConnectionError("timeout")

        c = self.make(Down())
        for code in ("sh600036", "sh600519", "sh601318"):
            c.backfill_day(code)
        c.backfill_day("sz000001")
        self.assertNotIn("sz000001", [code for _, code, *_ in c.providers["mairui"].calls])

    def test_http_client_errors_back_off_code_only(self):
        class Bad(FakeProvider):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                if code != "sz000001":
                    raise ProviderError("HTTP 404")
                return super().day_history(code, start, end)

        c = self.make(Bad())
        for code in ("sh600036", "sh600519", "sh601318"):
            c.backfill_day(code)
        c.backfill_day("sz000001")
        self.assertIn("sz000001", [code for _, code, *_ in c.providers["mairui"].calls])

    def test_preopen_commits_dated_reference(self):
        class Pre(FakeProvider):
            def preopen_ref(self, code, trade_date):
                return RawDayRow(code, trade_date, None, None, None, None, None, "lot", None, "CNY",
                                 16.77, 0, "preopen", new_batch_id())

        c = self.make(Pre())
        c.preopen(["sh600926"], datetime(2026, 9, 28, 9, 20))
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        row = facts.read_day_rows(conn, "sh600926")[0]
        self.assertEqual((row["trade_date"], row["provenance"], row["pc"]), ("2026-09-28", "preopen", 16.77))

    def test_preopen_retries_only_unconfirmed_codes(self):
        class Pre(FakeProvider):
            def preopen_ref(self, code, trade_date):
                self.calls.append(("pre", code))
                if code == "sh600926":                       # 停牌：参考价不带当日日期
                    return None
                return RawDayRow(code, trade_date, None, None, None, None, None, "lot", None, "CNY",
                                 10.0, 0, "preopen", new_batch_id())

        c = self.make(Pre())
        c.watchlist_fn = lambda: ["sh600036", "sh600926"]
        c.tick(datetime(2026, 9, 28, 9, 17))
        c.tick(datetime(2026, 9, 28, 9, 18, 5))
        calls = [code for kind, code, *_ in c.providers["mairui"].calls if kind == "pre"]
        self.assertEqual(calls, ["sh600036", "sh600926", "sh600926"])

    def test_rejected_preopen_is_not_confirmed(self):
        # 准入拒绝（参考前收缺失）的盘前行没有入库：不算确认，下一轮继续重试
        class NoPc(FakeProvider):
            def preopen_ref(self, code, trade_date):
                self.calls.append(("pre", code))
                return RawDayRow(code, trade_date, None, None, None, None, None, "lot", None, "CNY",
                                 None, 0, "preopen", new_batch_id())

        c = self.make(NoPc())
        self.assertEqual(c.preopen(["sh600926"], datetime(2026, 9, 28, 9, 20)), set())
        c.tick(datetime(2026, 9, 28, 9, 21))
        c.tick(datetime(2026, 9, 28, 9, 22, 5))
        calls = [code for kind, code, *_ in c.providers["mairui"].calls if kind == "pre"]
        self.assertEqual(calls.count("sh600036"), 2)

    def test_preopen_confirmation_read_from_facts_after_restart(self):
        # 进程在盘前窗口内重启：事实库里已有当日前收的标的不再请求
        class Pre(FakeProvider):
            def preopen_ref(self, code, trade_date):
                self.calls.append(("pre", code))
                return RawDayRow(code, trade_date, None, None, None, None, None, "lot", None, "CNY",
                                 10.0, 0, "preopen", new_batch_id())

        self.make(Pre()).tick(datetime(2026, 9, 28, 9, 17))
        fresh = self.make(Pre())
        fresh.tick(datetime(2026, 9, 28, 9, 18, 5))
        self.assertEqual([k for k, *_ in fresh.providers["mairui"].calls if k == "pre"], [])

    def test_intraday_fetches_preopen_for_unconfirmed_codes_with_throttle(self):
        # 服务盘中启动、盘前失败或盘中新加入：INTRADAY 为未确认的标的补取 F4，每代码 60 秒一次
        class Late(FakeProvider):
            ok = False

            def preopen_ref(self, code, trade_date):
                self.calls.append(("pre", code))
                pc = 10.0 if self.ok else None
                return RawDayRow(code, trade_date, None, None, None, None, None, "lot", None, "CNY",
                                 pc, 0, "preopen", new_batch_id())

            def minute_live(self, code, fact_freq, *, now):
                return []

        provider = Late()
        c = self.make(provider)
        c.watchlist_fn = lambda: ["sh600036", "sh000001", "hk00700"]
        pre = lambda: [code for kind, code, *_ in provider.calls if kind == "pre"]
        c.tick(datetime(2026, 9, 28, 10, 0, 0))
        self.assertEqual(pre(), ["sh600036"])                 # 指数与港股没有 F4
        c.tick(datetime(2026, 9, 28, 10, 0, 31))
        self.assertEqual(pre(), ["sh600036"])                 # 60 秒节流
        provider.ok = True
        c.tick(datetime(2026, 9, 28, 10, 1, 2))
        self.assertEqual(pre(), ["sh600036", "sh600036"])
        c.tick(datetime(2026, 9, 28, 10, 2, 30))
        self.assertEqual(pre(), ["sh600036", "sh600036"])     # 已确认：不再请求
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual(facts.read_day_rows(conn, "sh600036")[0]["pc"], 10.0)

    def test_finalize_closes_forming_and_reconciles(self):
        class Fin(FakeProvider):
            def minute_live(self, code, fact_freq, *, now):
                return [RawMinuteRow(code, "2026-09-28", "2026-09-28 15:00", 10, 10, 10, 10, 1, "lot", 1,
                                     "forming", "traded", new_batch_id())]

        c = self.make(Fin())
        c.intraday_tick(["sh600036"], datetime(2026, 9, 28, 14, 58))
        c.finalize_due(["sh600036"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        states = [r["state"] for r in facts.read_minute_rows(conn, "sh600036", FACT)]
        self.assertEqual(states, ["closed"])
        self.assertEqual(facts.read_day_rows(conn, "sh600036")[0]["provenance"], "final")

    def test_finalize_failure_registers_gap(self):
        c = self.make(FakeProvider(fail=True))
        c.finalize_due(["sh600036"], "2026-09-28", datetime(2026, 9, 28, 17, 30), calendar_known=True)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        gaps = facts.open_gaps(conn, "sh600036")
        self.assertEqual({(g["dataset"], g["reason"]) for g in gaps}, {("day", "finalize"), (FACT, "finalize")})

    def test_connection_outage_never_turns_gaps_into_known_gap(self):
        class Down(FakeProvider):
            def minute_history(self, code, fact_freq, start, end, *, now):
                raise ProviderConnectionError("timeout")

        c = self.make(Down())
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", FACT, "2026-08-01 09:30", "2026-08-31 15:00", "backfill")
        for _ in range(8):
            c.code_backoff.until.clear()
            c.source_breaker.until.clear()
            c.drain_gaps(max_requests=10)
        gap = conn.execute("SELECT attempts, reason FROM coverage_gaps").fetchone()
        self.assertEqual((gap["attempts"], gap["reason"]), (0, "backfill"))

    def test_gap_retries_capped_then_known_gap(self):
        c = self.make(FakeProvider(fail=True))
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", FACT, "2026-08-01 09:30", "2026-08-31 15:00", "backfill")
        for _ in range(8):
            c.code_backoff.until.clear()        # 只测重试上限，不测退避
            c.source_breaker.until.clear()
            c.drain_gaps(max_requests=10)
        gap = conn.execute("SELECT attempts, reason FROM coverage_gaps").fetchone()
        self.assertEqual((gap["attempts"], gap["reason"]), (5, "known_gap"))

    def test_minute_backfill_starts_at_first_day_bar_when_list_date_unknown(self):
        # 次新股：上市日未知（instrument 不可用），但日线回填已证明更早没有交易，分钟缺口不往前登记
        c = self.make(FakeProvider())
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        facts.commit_day_rows(conn, [RawDayRow("sh688806", d, 10, 11, 9, 10, 1, "lot", 1, "CNY", 10, 0, "final",
                                               new_batch_id()) for d in ("2026-07-21", "2026-09-25")],
                              market="CN", kind="stock", item="day_history", source="mairui", binding_gen=1,
                              today="2026-09-26")
        c.backfill_day("sh688806")                  # 日线整段回填完成后，首根日线才可信
        self.assertEqual(c.plan_minute_backfill("sh688806"), 3)
        self.assertEqual(min(g["start"] for g in facts.open_gaps(conn, "sh688806")), "2026-07-21 09:30")

    def test_single_finalized_row_does_not_truncate_minute_plan(self):
        c = self.make(FakeProvider())
        c.finalize_due(["sh600036"], "2026-09-25", datetime(2026, 9, 25, 17, 30), calendar_known=True)
        self.assertGreaterEqual(c.plan_minute_backfill("sh600036"), 36)

    def test_backfill_mode_plans_watchlist_history_once(self):
        # 自选股与上证指数由历史追赶（原 BACKFILL）补齐历史，不依赖首次打开；已规划过的不再整段重取
        provider = FakeProvider()
        c = self.make(provider)
        c.finalize_due(["sh600036"], "2026-09-25", datetime(2026, 9, 25, 17, 30), calendar_known=True)
        c.history_tick(datetime(2026, 9, 26, 20, 0))               # 周六
        full = [(code, start) for kind, code, start, _ in provider.calls if kind == "day" and start < "2017"]
        self.assertEqual(sorted(code for code, _ in full), ["sh000001", "sh600036"])
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertTrue(any(call[0] == FACT for call in provider.calls) or facts.open_gaps(conn, "sh600036", FACT))
        c.history_tick(datetime(2026, 9, 26, 20, 1))
        again = [call for call in provider.calls if call[0] == "day" and call[2] < "2017"]
        self.assertEqual(len(again), 2)

    def test_ensure_window_backfills_when_only_finalized_row_exists(self):
        # 有意改写（目标 2026-09-29 终审应修）：原要求首开同步整段回填。自选首开先服务窗口，只有定稿行时的整段回填
        # 交历史线程（仍不因为已有定稿行而跳过）
        provider = FakeProvider()
        c = self.make(provider)
        c.finalize_due(["sh600036"], "2026-09-25", datetime(2026, 9, 25, 17, 30), calendar_known=True)
        c.ensure_window("sh600036", "day")
        c.history_tick(datetime.fromtimestamp(self.clock.t))
        self.assertTrue([call for call in provider.calls
                         if call[:2] == ("day", "sh600036") and call[2] < "2017"])

    def test_in_session_history_drains_only_active_codes(self):
        # 有意改写（计划 2026-09-29 D5）：原 test_backfill_waits_while_any_market_in_session 固定「单线程调度，港股盘中不做 A 股回填」。历史追赶移到独立线程后，
        # 盘中照常为活跃代码（自选 sh600036）续传；全库续传等到没有市场开盘。
        # 再次有意改写（目标 2026-09-29 第二阶段）：全库续传只处理跟踪代码，非自选 sz000002 的遗留缺口保留但不再续传
        provider = FakeProvider()
        c = self.make(provider)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            for code in ("sh600036", "sz000002"):
                facts.record_gap(conn, code, FACT, "2026-08-01 09:30", "2026-08-31 15:00", "backfill")
            for code in ("sh600036", "sh000001"):
                for kind in ("backfill", "minute"):
                    facts.set_setting(conn, collector.plan_key(code, kind), "2026-09-28")
        self.clock.t = datetime(2026, 9, 28, 15, 10).timestamp()
        c.history_tick(datetime(2026, 9, 28, 15, 10))              # 周一：A 股已收盘，港股盘中
        m5 = lambda: {call[1] for call in provider.calls if call[0] == FACT}
        self.assertEqual(m5(), {"sh600036"})
        self.clock.t = datetime(2026, 9, 28, 16, 20).timestamp()
        c.history_tick(datetime(2026, 9, 28, 16, 20))
        self.assertEqual(m5(), {"sh600036"})
        self.assertTrue(facts.open_gaps(conn, "sz000002"))

    def test_drain_gaps_can_be_limited_to_one_market(self):
        provider = FakeProvider()
        c = self.make(provider)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", FACT, "2026-08-01 09:30", "2026-08-31 15:00", "backfill")
        self.assertEqual(c.drain_gaps(max_requests=10, market="HK")["done"], 0)
        self.assertEqual(provider.calls, [])

    def test_drained_finalize_gap_requires_rows(self):
        class Empty(FakeProvider):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return []

        c = self.make(Empty())
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", "day", "2026-09-25", "2026-09-25", "finalize")
        c.drain_gaps(max_requests=10)
        self.assertEqual(len(facts.open_gaps(conn, "sh600036", "day")), 1)

    def test_disabled_collector_tick_does_nothing(self):
        provider = FakeProvider()
        c = self.make(provider)
        with unittest.mock.patch.dict("os.environ", {"COLLECTOR_ENABLED": "0"}):
            self.assertEqual(c.tick(datetime(2026, 9, 26, 20, 0)), {})
        self.assertEqual(provider.calls, [])

    def test_status_reports_datasets_gaps_and_budget(self):
        c = self.make(FakeProvider())
        c.backfill_day("sh600036")
        status = c.status(["sh600036"], now=datetime(2026, 9, 26, 20, 0))
        day = next(d for d in status["datasets"] if d["dataset"] == "day")
        self.assertIsNotNone(day["last_commit_at"])
        self.assertEqual(status["budget"]["mairui"]["used"], 1)
        self.assertIn("probes", status)

    def test_status_marks_whether_stale_is_judged(self):
        # stale_judged 为 False 的时段采集器不判 stale（stale 恒 False）：盘中日线、开盘后到定稿截止之间
        c = self.make(FakeProvider())
        for now, expected in (
                (datetime(2026, 9, 28, 10, 30), {"day": False, FACT: True}),    # 盘中：只判分钟
                (datetime(2026, 9, 28, 16, 0), {"day": False, FACT: False}),    # 收盘后、定稿截止前：不判
                (datetime(2026, 9, 28, 19, 0), {"day": True, FACT: True}),      # 截止后：判当日未定稿
                (datetime(2026, 9, 26, 20, 0), {"day": True, FACT: True})):     # 非交易日：判上一交易日
            with self.subTest(now=now):
                rows = {d["dataset"]: d["stale_judged"]
                        for d in c.status(["sh600036"], now=now)["datasets"]}
                self.assertEqual(rows, expected)


class HKVendorFake:
    name = "longbridge"
    CONTRACT_VERSION = "fake"

    def __init__(self, qfq_close=50.0, fail_qfq=False, fail_freqs=(), bad_freqs=()):
        self.qfq_close, self.fail_qfq, self.calls = qfq_close, fail_qfq, []
        self.fail_freqs, self.bad_freqs = fail_freqs, bad_freqs

    # 定稿按槽位覆盖与分钟日线核对判成功：给齐当日全部 m30 槽位，日线与分钟聚合一致
    def day_history(self, code, start, end):
        n = len(sessions.slots("HK", "m30"))
        return [RawDayRow(code, end, 100, 100, 100, 100, 10 * n, "share", None, "HKD", 100, 0, "final",
                          new_batch_id())]

    def minute_history(self, code, fact_freq, start, end, *, now):
        day = end[:10]
        return [RawMinuteRow(code, day, f"{day} {t}", 100, 100, 100, 100, 10, "share", None, "closed",
                             "traded", new_batch_id()) for t in sessions.slots("HK", "m30")]

    def qfq_series(self, code, freq, start, end):
        self.calls.append((freq, start, end))
        if self.fail_qfq:
            raise ProviderError("down")
        c, day = self.qfq_close, end[:10]
        if freq == "day":
            return [{"trade_date": day, "open": c, "high": c, "low": c, "close": c,
                     "volume": 1000.0, "amount": None, "volume_unit": "share"}]
        return [{"trade_date": day, "slot_end": f"{day} 16:00", "open": c, "high": c, "low": c,
                 "close": c, "volume": 10.0, "amount": None, "volume_unit": "share"}]


class HKRangeFake(HKVendorFake):
    """按请求区间逐工作日返回（日线一根、m30 只给 16:00 槽），用于断言缓存覆盖范围。"""

    def day_history(self, code, start, end):
        return [RawDayRow(code, d, 100, 101, 99, 100, 1000, "share", None, "HKD", 100, 0, "final", new_batch_id())
                for d in _weekdays(start, end)]

    def minute_history(self, code, fact_freq, start, end, *, now):
        return [RawMinuteRow(code, d, f"{d} 16:00", 100, 100, 100, 100, 10, "share", None, "closed", "traded",
                             new_batch_id()) for d in _weekdays(start, end) if start <= f"{d} 16:00" <= end]

    def qfq_series(self, code, freq, start, end):
        self.calls.append((freq, start, end))
        if self.fail_qfq or freq in self.fail_freqs:
            raise ProviderError("down")
        c = self.qfq_close
        low = c + 1 if freq in self.bad_freqs else c          # low > high：整段校验不通过
        if freq == "day":
            return [{"trade_date": d, "open": c, "high": c, "low": low, "close": c, "volume": 1000.0,
                     "amount": None, "volume_unit": "share"} for d in _weekdays(start, end)]
        return [{"trade_date": d, "slot_end": f"{d} 16:00", "open": c, "high": c, "low": low, "close": c,
                 "volume": 10.0, "amount": None, "volume_unit": "share"}
                for d in _weekdays(start, end) if start <= f"{d} 16:00" <= end]


class HKFullFake(HKRangeFake):
    """每个工作日给齐港股全部 m30 槽位（分钟聚合与日线一致），缺口才能被判为已覆盖；
    since 模拟供应商前复权 m30 只有该日之后的历史。"""

    def __init__(self, *a, since="", **k):
        super().__init__(*a, **k)
        self.since = since

    def day_history(self, code, start, end):
        n = len(sessions.slots("HK", "m30"))
        return [RawDayRow(code, d, 100, 100, 100, 100, 10 * n, "share", None, "HKD", 100, 0, "final",
                          new_batch_id()) for d in _weekdays(start, end)]

    def minute_history(self, code, fact_freq, start, end, *, now):
        return [RawMinuteRow(code, d, f"{d} {t}", 100, 100, 100, 100, 10, "share", None, "closed", "traded",
                             new_batch_id()) for d in _weekdays(start, end)
                for t in sessions.slots("HK", "m30") if start <= f"{d} {t}" <= end]

    def qfq_series(self, code, freq, start, end):
        if freq == "day":
            return super().qfq_series(code, freq, start, end)
        self.calls.append((freq, start, end))
        c = self.qfq_close
        return [{"trade_date": d, "slot_end": f"{d} {t}", "open": c, "high": c, "low": c, "close": c,
                 "volume": 10.0, "amount": None, "volume_unit": "share"}
                for d in _weekdays(start, end) if d >= self.since
                for t in sessions.slots("HK", "m30") if start <= f"{d} {t}" <= end]


class HKVendorCollectorTests(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 16, 30)

    def setUp(self):
        _enable_collector(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def make(self, provider):
        return collector.Collector(self.dir, providers={"longbridge": provider},
                                   clock=Clock(self.NOW.timestamp()), watchlist_fn=lambda: ["hk00700"])

    def test_hk_finalize_publishes_vendor_cache(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        c = self.make(HKVendorFake())
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual([m[:2] for m in vq.cache_version(conn, "hk00700")], [["day", 1], ["m30", 1]])

    def test_vendor_refetch_failure_keeps_version_and_marks_stale(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        self.make(HKVendorFake()).finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        self.make(HKVendorFake(fail_qfq=True)).refresh_vendor_qfq("hk00700", self.NOW,
                                                                  closed_through="2026-09-25", today_ok=True)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual(vq.cache_version(conn, "hk00700"), [["day", 1, 0, 1], ["m30", 1, 0, 1]])

    def test_vendor_restatement_republishes(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        self.make(HKVendorFake()).finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        self.make(HKVendorFake(qfq_close=49.0)).refresh_vendor_qfq("hk00700", self.NOW,
                                                                   closed_through="2026-09-25", today_ok=True)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual(vq.read(conn, "hk00700", "day")[0][0]["close"], 49.0)

    def test_unfreeze_refetches_every_freq_in_full(self):
        from chanapp.engine.kline import bindings, hk_vendor_qfq as vq
        from chanapp.engine.kline.rows import FetchItem
        self.make(HKVendorFake()).finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        bindings.switch(conn, "HK", "stock", FetchItem.DAY_HISTORY, "yahoo", reason="test")
        self.make(HKVendorFake()).refresh_vendor_qfq("hk00700", self.NOW)
        bindings.switch(conn, "HK", "stock", FetchItem.DAY_HISTORY, "longbridge", reason="test")
        # 有意改写（第三阶段第四轮复审阻断 1）：冻结的缓存到了当天但没追平，覆盖当天的解冻重取归定稿，按定稿的调用方式
        self.make(HKVendorFake()).refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-25", today_ok=True)
        self.assertEqual(vq.cache_version(conn, "hk00700"), [["day", 2, 0, 0], ["m30", 2, 0, 0]])

    def test_ensure_window_builds_hk_vendor_cache(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        c = self.make(HKVendorFake())
        c.ensure_window("hk00700", "day")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual([m[0] for m in vq.cache_version(conn, "hk00700")], ["day", "m30"])

    def db(self):
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        return conn

    def raw_m30(self, conn, start, end):
        rows = HKRangeFake().minute_history("hk00700", "m30", f"{start} 09:30", f"{end} 16:00", now=self.NOW)
        facts.commit_minute_rows(conn, rows, market="HK", kind="stock", item="minute_history", fact_freq="m30",
                                 source="longbridge", binding_gen=1, today="2026-09-25")

    # 窗口：520 根 m30 ÷ 港股每日 11 根 ≈ 48 个交易日；长桥 m30 按 60 天分段，窗口两段内取完
    WINDOW_DAYS = -(-520 // 11)

    def m30_days(self, conn):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        return sorted({b["trade_date"] for b in vq.read(conn, "hk00700", "m30")[0]})

    def test_first_build_without_minute_facts_covers_window_only(self):
        # 非自选首开日线时还没有分钟事实：m30 缓存只建默认窗口（同步请求可控），更早的历史交给后台
        fake = HKRangeFake()
        self.make(fake).ensure_window("hk00700", "day")
        days = self.m30_days(self.db())
        self.assertEqual(days[-1], "2026-09-24")
        self.assertTrue(self.WINDOW_DAYS <= len(days) <= self.WINDOW_DAYS + 10, len(days))
        (lo, hi), = [(a, b) for f, a, b in fake.calls if f == "m30"]
        self.assertLessEqual((date.fromisoformat(hi[:10]) - date.fromisoformat(lo[:10])).days + 1, 2 * 60)

    def test_day_first_and_m30_first_build_same_m30_start(self):
        # 先开日线（没有 raw m30）与先开 30 分（已按月取回约三个月 raw，早于窗口）首建出的 m30 缓存起点一致
        from chanapp.engine.kline import hk_vendor_qfq as vq
        self.make(HKFullFake()).ensure_window("hk00700", "day")
        day_first = vq.read(self.db(), "hk00700", "m30")[0][0]["dt"]
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        c = collector.Collector(Path(other.name), providers={"longbridge": HKFullFake()},
                                clock=Clock(self.NOW.timestamp()), watchlist_fn=lambda: [])
        c.ensure_window("hk00700", "m30")
        conn = facts.open_facts(Path(other.name) / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual(vq.read(conn, "hk00700", "m30")[0][0]["dt"], day_first)

    def test_m60_first_open_builds_window_for_requested_period(self):
        # 先开 60 分：默认窗口 520 根 m60 = 1040 根 m30，缓存窗口按所请求周期取够
        self.make(HKFullFake()).ensure_window("hk00700", "m60")
        self.assertGreaterEqual(len(self.m30_days(self.db())), -(-1040 // 11))

    def test_m60_after_day_open_widens_cache_to_requested_window(self):
        # 先开日线只建了 520 根 m30 的窗口；之后门面按 m60 补窗口：缓存要覆盖 1040 根 m30 所需区间
        c = self.make(HKFullFake())
        c.ensure_window("hk00700", "day")
        self.assertLess(len(self.m30_days(self.db())), -(-1040 // 11))
        c.ensure_window("hk00700", "m60")
        self.assertGreaterEqual(len(self.m30_days(self.db())), -(-1040 // 11))

    def cache_ends(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        conn = self.db()
        return {freq: vq.read(conn, "hk00700", freq)[0][-1]["trade_date"] for freq in ("day", "m30")}

    def test_widen_after_todays_finalize_keeps_todays_bars(self):
        # 当天定稿已把缓存发布到今天；之后按 m60 补窗口整段重取，截止日不得退回昨天。
        # 第三阶段第五轮复审阻断 1：补窗口不是定稿，请求只到昨天（当天只归定稿与手动重拉），当天尾部沿用已发布版本
        fake = HKFullFake()
        c = self.make(fake)
        c.ensure_window("hk00700", "day")
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        self.assertEqual(self.cache_ends(), {"day": "2026-09-25", "m30": "2026-09-25"})
        fake.calls.clear()
        c.ensure_window("hk00700", "m60")
        self.assertGreaterEqual(len(self.m30_days(self.db())), -(-1040 // 11))     # 确实走了补窗口
        self.assertEqual(self.cache_ends(), {"day": "2026-09-25", "m30": "2026-09-25"})
        self.assertTrue(fake.calls)
        self.assertEqual([x for x in fake.calls if x[2][:10] >= "2026-09-25"], [])

    def test_backfill_extension_after_todays_finalize_keeps_todays_bars(self):
        # BACKFILL 扩展（raw 起点早于缓存）整段重取同样不撤掉当天已定稿的尾部
        # （第五轮复审阻断 1：扩展请求只到昨天）
        from chanapp.engine.kline import hk_vendor_qfq as vq
        fake = HKFullFake()
        c = self.make(fake)
        c.ensure_window("hk00700", "day")
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        conn = self.db()
        self.raw_m30(conn, "2026-06-01", "2026-06-05")
        fake.calls.clear()
        self.assertTrue(c._extend_vendor("hk00700", self.NOW))
        self.assertEqual(vq.read(conn, "hk00700", "m30")[0][0]["trade_date"], "2026-06-01")   # 确实整段扩展
        self.assertEqual(self.cache_ends(), {"day": "2026-09-25", "m30": "2026-09-25"})
        self.assertTrue(fake.calls)
        self.assertEqual([x for x in fake.calls if x[2][:10] >= "2026-09-25"], [])

    def test_widen_deferred_before_any_request_is_retried_on_next_open(self):
        # 第三阶段第五轮复审应修 4：补窗口因额度一个请求都没发出——不算「这个起点已试过」，下次打开照常补
        from unittest import mock
        c = self.make(HKFullFake())
        c.ensure_window("hk00700", "day")
        real = c._call

        def call(code, source, fn, **kw):
            if kw.get("capability") == "qfq_series":
                raise collector._BudgetExhausted()
            return real(code, source, fn, **kw)
        with mock.patch.object(c, "_call", side_effect=call):
            c.ensure_window("hk00700", "m60")
        self.assertLess(len(self.m30_days(self.db())), -(-1040 // 11))
        c.ensure_window("hk00700", "m60")
        self.assertGreaterEqual(len(self.m30_days(self.db())), -(-1040 // 11))

    def test_failed_extension_after_todays_finalize_leaves_todays_cache_servable(self):
        # 第三阶段第五轮复审阻断 1：扩展只取到昨天，失败不说明已发布的当天尾部落后——不标 stale
        # （当天定稿已完成，不会再有自动请求替它清掉 stale，当天就一直降级）
        from chanapp.engine.kline import hk_vendor_qfq as vq
        fake = HKFullFake()
        c = self.make(fake)
        c.ensure_window("hk00700", "day")
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        conn = self.db()
        self.raw_m30(conn, "2026-06-01", "2026-06-05")
        fake.fail_qfq = True
        self.assertTrue(c._extend_vendor("hk00700", self.NOW))
        self.assertEqual({f: vq.read(conn, "hk00700", f)[1]["stale"] for f in vq.FREQS}, {"day": False, "m30": False})
        self.assertEqual(self.cache_ends(), {"day": "2026-09-25", "m30": "2026-09-25"})

    def check_history_refresh_keeps_health(self, degrade):
        # 第三阶段第六轮复审阻断：只取历史区间的刷新保留当天尾部时，不能顺带把没重取的尾部认证为健康
        from chanapp.engine.kline import hk_vendor_qfq as vq
        c = self.make(HKFullFake())
        c.ensure_window("hk00700", "day")
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        conn = self.db()
        with c.writer() as wconn:
            degrade(wconn)
        before = {f: (vq.read(conn, "hk00700", f)[1]["stale"], vq.read(conn, "hk00700", f)[1]["frozen"])
                  for f in vq.FREQS}
        c.ensure_window("hk00700", "m60")
        after = {f: (vq.read(conn, "hk00700", f)[1]["stale"], vq.read(conn, "hk00700", f)[1]["frozen"])
                 for f in vq.FREQS}
        self.assertEqual(after, before)
        self.assertEqual(self.cache_ends(), {"day": "2026-09-25", "m30": "2026-09-25"})

    def test_history_refresh_keeps_stale_of_the_kept_tail(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        self.check_history_refresh_keeps_health(lambda conn: vq.mark_stale(conn, "hk00700"))

    def test_history_refresh_does_not_unfreeze_the_kept_tail(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        self.check_history_refresh_keeps_health(lambda conn: vq.freeze(conn, "hk00700"))

    def test_restatement_found_then_refetch_failed_marks_stale_even_with_todays_tail(self):
        # 第三阶段第六轮复审阻断：比对已发现历史重述、整段重取又失败——旧历史已被否定，保留当天尾部也要标 stale
        from chanapp.engine.kline import hk_vendor_qfq as vq

        class RestateThenFail(HKFullFake):
            def qfq_series(self, code, freq, start, end):
                if self.armed and len(self.calls) >= 2:           # 近 30 天比对两次之后的整段重取失败
                    self.calls.append((freq, start, end))
                    raise ProviderError("down")
                return super().qfq_series(code, freq, start, end)
        fake = RestateThenFail()
        fake.armed = False
        c = self.make(fake)
        c.ensure_window("hk00700", "day")
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        fake.calls.clear()
        fake.armed, fake.qfq_close = True, 45.0                 # 供应商重述历史价
        c.refresh_vendor_qfq("hk00700", self.NOW)
        self.assertGreater(len(fake.calls), 2)                   # 确实发现重述并尝试整段重取
        self.assertEqual({f: vq.read(self.db(), "hk00700", f)[1]["stale"] for f in vq.FREQS},
                         {"day": True, "m30": True})

    def check_partial_restatement_marks_stale(self, refresh):
        # 第三阶段第七轮复审阻断：日线已返回重述（旧历史被否定），随后 m30 请求失败——保留当天尾部也要标 stale
        from chanapp.engine.kline import hk_vendor_qfq as vq

        class DayRestatedM30Fails(HKFullFake):
            def qfq_series(self, code, freq, start, end):
                if self.armed and freq == "m30":
                    self.calls.append((freq, start, end))
                    raise ProviderError("down")
                return super().qfq_series(code, freq, start, end)
        fake = DayRestatedM30Fails()
        fake.armed = False
        c = self.make(fake)
        c.ensure_window("hk00700", "day")
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        fake.armed, fake.qfq_close = True, 45.0
        refresh(c)
        self.assertEqual({f: vq.read(self.db(), "hk00700", f)[1]["stale"] for f in vq.FREQS},
                         {"day": True, "m30": True})

    def test_restated_day_then_failed_m30_marks_stale(self):
        self.check_partial_restatement_marks_stale(lambda c: c.refresh_vendor_qfq("hk00700", self.NOW))

    def test_restated_day_then_failed_m30_marks_stale_on_widen(self):
        self.check_partial_restatement_marks_stale(
            lambda c: c.refresh_vendor_qfq("hk00700", self.NOW, window=5000, widen=True))

    def check_no_overlap_failure_keeps_health(self, refresh):
        # 第三阶段第八轮复审阻断：候选与缓存没有重叠只说明要整段重取，不是重述证据——之后失败不能降级健康缓存
        from chanapp.engine.kline import hk_vendor_qfq as vq

        class OnlyTodayThenM30Fails(HKFullFake):
            def qfq_series(self, code, freq, start, end):
                if self.armed and freq == "m30":
                    self.calls.append((freq, start, end))
                    raise ProviderError("down")
                if not self.armed:
                    start = end[:10] if freq == "day" else f"{end[:10]} 09:30"   # 定稿只发布当天
                return super().qfq_series(code, freq, start, end)
        fake = OnlyTodayThenM30Fails()
        fake.armed = False
        c = self.make(fake)
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        self.assertEqual(vq.read(self.db(), "hk00700", "day")[0][0]["trade_date"], "2026-09-25")
        fake.armed = True
        refresh(c)
        self.assertEqual({f: vq.read(self.db(), "hk00700", f)[1]["stale"] for f in vq.FREQS},
                         {"day": False, "m30": False})

    def test_no_overlap_failure_keeps_health(self):
        self.check_no_overlap_failure_keeps_health(lambda c: c.refresh_vendor_qfq("hk00700", self.NOW))

    def test_no_overlap_failure_keeps_health_on_widen(self):
        self.check_no_overlap_failure_keeps_health(
            lambda c: c.refresh_vendor_qfq("hk00700", self.NOW, window=5000, widen=True))

    def test_restated_day_then_untracked_marks_stale(self):
        # 第三阶段第八轮复审阻断：日线已证实重述后被移出自选，停止后续请求与发布，但旧历史已被否定，照标 stale
        from chanapp.engine.kline import hk_vendor_qfq as vq
        fake = HKFullFake()
        c = self.make(fake)
        c.ensure_window("hk00700", "day")
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        fake.qfq_close = 45.0
        tripped = []
        real = fake.qfq_series

        def qfq(code, freq, start, end):
            tripped.append(freq)
            return real(code, freq, start, end)
        fake.qfq_series = qfq
        out = c.refresh_vendor_qfq("hk00700", self.NOW, eligible=lambda code: not tripped)
        self.assertEqual(out, {"untracked": True})
        self.assertEqual({f: vq.read(self.db(), "hk00700", f)[1]["stale"] for f in vq.FREQS},
                         {"day": True, "m30": True})

    def restated_then(self, m30_hook):
        """定稿后供应商重述历史（日线先返回重述价 45），m30 请求时执行 m30_hook。"""
        fake = HKFullFake()
        c = self.make(fake)
        c.ensure_window("hk00700", "day")
        c.finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        fake.qfq_close = 45.0
        real = fake.qfq_series

        def qfq(code, freq, start, end):
            if freq == "m30":
                m30_hook()
            return real(code, freq, start, end)
        fake.qfq_series = qfq
        return c

    def served_old_as_healthy(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        conn = self.db()
        day, meta = vq.read(conn, "hk00700", "day")
        return not meta["stale"] and day[0]["close"] == 50.0

    def test_restated_day_then_interrupted_m30_marks_stale(self):
        # 第三阶段第九轮复审阻断：已证实重述后请求被中断（非 Exception）——异常退出也要结算证据，原异常照抛
        def interrupt():
            raise KeyboardInterrupt()
        c = self.restated_then(interrupt)
        with self.assertRaises(KeyboardInterrupt):
            c.refresh_vendor_qfq("hk00700", self.NOW, window=5000, widen=True)
        self.assertFalse(self.served_old_as_healthy())

    def test_restated_then_writer_busy_is_settled_once_the_lock_frees(self):
        # 第三阶段第九轮复审阻断：已证实重述后发布时写者锁被另一个进程占用（标 stale 也写不进）——证据留待锁释放后
        # 补落盘（按取数时的版本，期间有新版本发布就作废），不能丢
        other = collector.Collector(self.dir, providers={}, clock=Clock(self.NOW.timestamp()), watchlist_fn=lambda: [])
        held, grabbed = contextlib.ExitStack(), []
        self.addCleanup(held.close)

        def grab():
            if not grabbed:
                grabbed.append(held.enter_context(other.writer()))
        c = self.restated_then(grab)
        with self.assertRaises(collector.CollectorLocked):
            c.refresh_vendor_qfq("hk00700", self.NOW, window=5000, widen=True)
        held.close()
        c.history_tick(self.NOW)
        self.assertFalse(self.served_old_as_healthy())

    def test_stale_flush_keeps_newer_pending_evidence(self):
        # 第三阶段第十轮复审应修 1：旧的补落盘任务（版本已被取代，不标）不能删掉期间登记的新版本证据
        from unittest import mock
        from chanapp.engine.kline import hk_vendor_qfq as vq
        c = self.make(HKFullFake())
        old, new = {"day": 2, "m30": 2}, {"day": 3, "m30": 3}
        c._vendor_stale_pending["hk00700"] = old

        def interleave(conn, code, versions):
            if versions == old:                                  # 旧任务写库期间，另一线程登记了新证据
                c._vendor_stale_pending[code] = new
            return False
        with mock.patch.object(vq, "mark_stale_if", side_effect=interleave):
            c._flush_vendor_stale()
        self.assertEqual(c._vendor_stale_pending.get("hk00700"), new)

    def test_failed_refresh_does_not_degrade_a_version_published_meanwhile(self):
        # 第三阶段第十轮复审应修 2：取数期间另一个实例发布了新版本，本次失败只否定取数时的旧版本
        from chanapp.engine.kline import hk_vendor_qfq as vq
        other_fake = HKFullFake(qfq_close=45.0)
        other = collector.Collector(self.dir, providers={"longbridge": other_fake}, clock=Clock(self.NOW.timestamp()),
                                    watchlist_fn=lambda: ["hk00700"])

        def publish_then_fail():
            other.refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-25", force=True)
            raise ProviderError("down")
        c = self.restated_then(publish_then_fail)
        c.refresh_vendor_qfq("hk00700", self.NOW, window=5000, widen=True)
        day, meta = vq.read(self.db(), "hk00700", "day")
        self.assertEqual(day[0]["close"], 45.0)
        self.assertFalse(meta["stale"])

    def test_raw_m30_window_counts_readable_minutes_not_revisions(self):
        _list_since(self.dir, ["hk00700"], "2026-06-01")
        conn = _revised_minutes(self.dir, "hk00700", ["2026-09-23", "2026-09-24"], quarantine_day="2026-09-24")
        self.addCleanup(conn.close)
        need = 10 * 2                                         # 10 根 m60 = 20 根 m30
        total = conn.execute("SELECT COUNT(*) FROM minute_bars WHERE code='hk00700'").fetchone()[0]
        self.assertGreaterEqual(total, need)
        self.assertLess(_readable_closed(conn, "hk00700", "m30"), need)
        self.make(HKFullFake()).ensure_window("hk00700", "m60", bars=10)
        self.assertGreaterEqual(_readable_closed(conn, "hk00700", "m30"), need)

    def test_m60_widen_without_raw_minutes_uses_window_start(self):
        # raw 分钟取数失败（没有 raw 起点可跟）时，也按所请求周期的窗口整段重取
        class NoRawMinutes(HKFullFake):
            def minute_history(self, code, fact_freq, start, end, *, now):
                raise ProviderError("down")

        c = self.make(NoRawMinutes())
        c.ensure_window("hk00700", "day")
        c.code_backoff.until.clear()
        c.ensure_window("hk00700", "m60")
        self.assertGreaterEqual(len(self.m30_days(self.db())), -(-1040 // 11))

    def test_restatement_refetch_does_not_shrink_cache(self):
        # 首开日线只建窗口；之后只有最近几天的 raw m30（定稿写入），重述触发整段重取时不能缩到 raw 起点
        from chanapp.engine.kline import hk_vendor_qfq as vq
        self.make(HKRangeFake()).ensure_window("hk00700", "day")
        conn = self.db()
        start = vq.read(conn, "hk00700", "m30")[0][0]["dt"]
        self.raw_m30(conn, "2026-09-21", "2026-09-24")
        self.make(HKRangeFake(qfq_close=49.0)).refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")
        bars, meta = vq.read(conn, "hk00700", "m30")
        self.assertEqual((bars[0]["dt"], bars[0]["close"], meta["version"]), (start, 49.0, 2))

    def test_todays_forming_minutes_do_not_set_cache_start(self):
        # 盘中已写入今天的 forming m30：缓存只收已收盘 bar，起点不能跟到今天（否则候选为空、缓存建不起来）
        from chanapp.engine.kline import hk_vendor_qfq as vq
        conn = self.db()
        facts.commit_minute_rows(conn, [RawMinuteRow("hk00700", "2026-09-25", "2026-09-25 10:00", 100, 100, 100, 100,
                                                     10, "share", None, "forming", "traded", new_batch_id())],
                                 market="HK", kind="stock", item="minute_live", fact_freq="m30",
                                 source="longbridge", binding_gen=1, today="2026-09-25")
        self.make(HKRangeFake()).ensure_window("hk00700", "day")
        days = self.m30_days(conn)
        self.assertEqual(days[-1], "2026-09-24")
        self.assertGreaterEqual(len(days), self.WINDOW_DAYS)

    def test_raw_extending_backward_refetches_cache_in_full(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        conn = self.db()
        self.raw_m30(conn, "2026-09-01", "2026-09-24")
        c = self.make(HKRangeFake())
        c.refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")
        self.assertEqual(vq.read(conn, "hk00700", "m30")[0][0]["dt"], "2026-09-01 16:00")   # 跟随 raw 起点
        self.raw_m30(conn, "2026-08-03", "2026-08-31")                                      # 回填向前扩展
        c.refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")
        bars, meta = vq.read(conn, "hk00700", "m30")
        self.assertEqual((bars[0]["dt"], bars[-1]["dt"], meta["version"]),
                         ("2026-08-03 16:00", "2026-09-24 16:00", 2))

    SUNDAY = datetime(2026, 9, 27, 7, 0)          # 非交易日：历史追赶做全库续传与缓存扩展

    def backfill_setup(self, fake):
        """自选港股首开日线（只建窗口），上市日压到 6 月让分钟计划只有几个月；返回采集器。"""
        _list_since(self.dir, ["hk00700"], "2026-06-01")
        c = collector.Collector(self.dir, providers={"longbridge": fake}, clock=Clock(self.SUNDAY.timestamp()),
                                watchlist_fn=lambda: ["hk00700"])
        c.ensure_window("hk00700", "day")
        return c

    def test_backfill_extends_cache_after_minute_gaps_drained(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        c = self.backfill_setup(HKFullFake())
        conn = self.db()
        start = vq.read(conn, "hk00700", "m30")[0][0]["dt"]
        self.assertGreater(start, "2026-07")                                   # 首建只有窗口
        with facts.write_txn(conn):                                             # known_gap 不阻止扩展
            facts.record_gap(conn, "hk00700", "m30", "2026-06-01 09:30", "2026-06-01 16:00", "known_gap")
        c.history_tick(self.SUNDAY)
        self.assertEqual([g["reason"] for g in facts.open_gaps(conn, "hk00700", "m30")], ["known_gap"])
        bars, meta = vq.read(conn, "hk00700", "m30")
        self.assertEqual((bars[0]["dt"], bars[-1]["dt"], meta["version"]),
                         ("2026-06-01 10:00", "2026-09-25 16:00", 2))

    def test_backfill_does_not_extend_while_minute_gaps_open(self):
        # 单槽假源：分钟缺口取回后仍不完整、未排空；raw 起点已早于缓存，也不整段重取
        from chanapp.engine.kline import hk_vendor_qfq as vq
        fake = HKRangeFake()
        c = self.backfill_setup(fake)
        conn = self.db()
        start = vq.read(conn, "hk00700", "m30")[0][0]["dt"]
        before = len(fake.calls)
        c.history_tick(self.SUNDAY)
        self.assertTrue([g for g in facts.open_gaps(conn, "hk00700", "m30") if g["reason"] != "known_gap"])
        self.assertLess(c._raw_first("hk00700", "m30"), start[:10])
        self.assertEqual(vq.read(conn, "hk00700", "m30")[1]["version"], 1)
        self.assertEqual(len(fake.calls), before)

    def test_backfill_extends_once_when_vendor_history_is_shorter(self):
        # 供应商 m30 历史比 raw 短：重取后缓存起点仍晚于 raw，之后每 5 秒一轮的 BACKFILL 不得反复整段重取
        from chanapp.engine.kline import hk_vendor_qfq as vq
        fake = HKFullFake(since="2026-07-01")
        c = self.backfill_setup(fake)
        c.history_tick(self.SUNDAY)
        conn = self.db()
        self.assertEqual(vq.read(conn, "hk00700", "m30")[1]["version"], 2)
        calls = len(fake.calls)
        c.history_tick(self.SUNDAY)
        self.assertEqual(len(fake.calls), calls)

    def test_one_period_failing_publishes_neither(self):
        # day 重述、m30 取数失败：两个周期都不发布，否则 bundle 会读到两个复权基准
        from chanapp.engine.kline import hk_vendor_qfq as vq
        conn = self.db()
        self.raw_m30(conn, "2026-09-01", "2026-09-24")
        self.make(HKRangeFake()).refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")
        self.make(HKRangeFake(qfq_close=49.0, fail_freqs=("m30",))).refresh_vendor_qfq(
            "hk00700", self.NOW, closed_through="2026-09-24")
        self.assertEqual(vq.cache_version(conn, "hk00700"), [["day", 1, 0, 1], ["m30", 1, 0, 1]])
        self.assertEqual({b["close"] for b in vq.read(conn, "hk00700", "day")[0]}, {50.0})

    def test_invalid_candidate_publishes_neither(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        conn = self.db()
        self.raw_m30(conn, "2026-09-01", "2026-09-24")
        self.make(HKRangeFake()).refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")
        self.make(HKRangeFake(qfq_close=49.0, bad_freqs=("m30",))).refresh_vendor_qfq(
            "hk00700", self.NOW, closed_through="2026-09-24")
        self.assertEqual(vq.cache_version(conn, "hk00700"), [["day", 1, 0, 1], ["m30", 1, 0, 1]])

    def test_switch_back_stays_frozen_until_both_periods_refetched(self):
        from chanapp.engine.kline import bindings, hk_vendor_qfq as vq
        from chanapp.engine.kline.rows import FetchItem
        conn = self.db()
        self.raw_m30(conn, "2026-09-01", "2026-09-24")
        self.make(HKRangeFake()).refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")
        bindings.switch(conn, "HK", "stock", FetchItem.DAY_HISTORY, "yahoo", reason="drill")
        self.assertEqual([m[2] for m in vq.cache_version(conn, "hk00700")], [1, 1])      # 切到冷备即冻结
        bindings.switch(conn, "HK", "stock", FetchItem.DAY_HISTORY, "longbridge", reason="drill")
        self.assertEqual([m[2] for m in vq.cache_version(conn, "hk00700")], [1, 1])      # 切回不解冻
        self.make(HKRangeFake(fail_freqs=("m30",))).refresh_vendor_qfq("hk00700", self.NOW,
                                                                       closed_through="2026-09-24")
        self.assertEqual([m[:3] for m in vq.cache_version(conn, "hk00700")], [["day", 1, 1], ["m30", 1, 1]])
        self.make(HKRangeFake()).refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")
        self.assertEqual(vq.cache_version(conn, "hk00700"), [["day", 2, 0, 0], ["m30", 2, 0, 0]])

    def test_binding_change_during_fetch_rejects_publish(self):
        from chanapp.engine.kline import bindings, hk_vendor_qfq as vq
        from chanapp.engine.kline.rows import FetchItem
        conn = self.db()
        self.raw_m30(conn, "2026-09-01", "2026-09-24")
        self.make(HKRangeFake()).refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")

        class Switching(HKRangeFake):
            def qfq_series(inner, code, freq, start, end):
                if freq == "m30":                     # 取数期间有人推进了绑定代次
                    bindings.switch(conn, "HK", "stock", FetchItem.DAY_HISTORY, "longbridge", reason="drill")
                return HKRangeFake.qfq_series(inner, code, freq, start, end)

        self.make(Switching(qfq_close=49.0)).refresh_vendor_qfq("hk00700", self.NOW, closed_through="2026-09-24")
        self.assertEqual([m[1] for m in vq.cache_version(conn, "hk00700")], [1, 1])

    def test_late_first_build_does_not_overwrite_newer_publish(self):
        # 首建取数期间（同一绑定代次）定稿先发布了更新版本：首建的迟到候选被丢弃，缓存保持新版本且不标 stale
        from chanapp.engine.kline import hk_vendor_qfq as vq
        other = self.db()
        newer = {"day": [{"trade_date": "2026-09-24", "open": 51.0, "high": 51.0, "low": 51.0, "close": 51.0,
                          "volume": 1000.0, "amount": None, "volume_unit": "share"}],
                 "m30": [{"trade_date": "2026-09-24", "slot_end": "2026-09-24 16:00", "open": 51.0, "high": 51.0,
                          "low": 51.0, "close": 51.0, "volume": 10.0, "amount": None, "volume_unit": "share"}]}

        class Interleaved(HKRangeFake):
            def qfq_series(inner, code, freq, start, end):
                if freq == "m30" and not vq.cache_version(other, code):
                    vq.publish_set(other, code, newer, closed_through="2026-09-24", full=True)
                return HKRangeFake.qfq_series(inner, code, freq, start, end)

        out = self.make(Interleaved()).refresh_vendor_qfq("hk00700", self.NOW)
        self.assertEqual(out, {"superseded": True})
        self.assertEqual(vq.cache_version(other, "hk00700"), [["day", 1, 0, 0], ["m30", 1, 0, 0]])
        self.assertEqual([b["close"] for b in vq.read(other, "hk00700", "m30")[0]], [51.0])

    def test_finalize_refresh_waits_for_first_build_in_flight(self):
        # 定稿与扩展的刷新也走代码单飞锁：首建在途时，另一路刷新等它完成，不同时打供应商
        import threading
        entered, release = threading.Event(), threading.Event()

        class Slow(HKRangeFake):
            def qfq_series(inner, code, freq, start, end):
                if not entered.is_set():
                    entered.set()
                    release.wait(5)
                return HKRangeFake.qfq_series(inner, code, freq, start, end)

        fake = Slow()
        c = self.make(fake)
        first = threading.Thread(target=c.ensure_window, args=("hk00700", "day"))
        first.start()
        self.assertTrue(entered.wait(5))
        calls = len(fake.calls)
        second = threading.Thread(target=c.refresh_vendor_qfq, args=("hk00700", self.NOW),
                                  kwargs={"closed_through": "2026-09-24"})
        second.start()
        second.join(0.3)
        self.assertEqual(len(fake.calls), calls)
        release.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())

    def test_cold_binding_freezes_cache_without_calling_vendor(self):
        from chanapp.engine.kline import bindings, hk_vendor_qfq as vq
        from chanapp.engine.kline.rows import FetchItem
        self.make(HKVendorFake()).finalize_due(["hk00700"], "2026-09-25", self.NOW, calendar_known=True)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        bindings.switch(conn, "HK", "stock", FetchItem.DAY_HISTORY, "yahoo", reason="test")
        lb = HKVendorFake(qfq_close=49.0)
        c = self.make(lb)
        c.refresh_vendor_qfq("hk00700", self.NOW)
        self.assertEqual(lb.calls, [])
        self.assertEqual([m[2] for m in vq.cache_version(conn, "hk00700")], [1, 1])


class ScheduleTests(unittest.TestCase):
    def modes(self, hhmm, *, market="CN", trading=True, **state):
        h, m, *sec = (int(x) for x in hhmm.split(":"))
        now = datetime(2026, 9, 28, h, m, sec[0] if sec else 0)
        return collector.due_modes(now, market=market, is_trading_day=trading, state=state)

    def test_preopen_window(self):
        self.assertNotIn("PREOPEN", self.modes("09:15:59"))
        self.assertIn("PREOPEN", self.modes("09:16"))
        self.assertNotIn("PREOPEN", self.modes("09:20", preopen_done=True))
        last = datetime(2026, 9, 28, 9, 19, 30)
        self.assertNotIn("PREOPEN", self.modes("09:20", last_preopen=last))      # 每分钟至多一轮
        self.assertIn("PREOPEN", self.modes("09:20:30", last_preopen=last))

    def test_intraday_windows_and_interval(self):
        self.assertNotIn("INTRADAY", self.modes("11:31:30"))
        self.assertIn("INTRADAY", self.modes("15:00:30"))
        # 有意改写（目标 2026-09-29 第三阶段）：正常盘中统一 60 秒一轮（原 30 秒），额度接近上限时加倍
        last = datetime(2026, 9, 28, 10, 0, 0)
        self.assertNotIn("INTRADAY", self.modes("10:00:31", last_intraday=last))
        self.assertNotIn("INTRADAY", self.modes("10:00:59", last_intraday=last))
        self.assertIn("INTRADAY", self.modes("10:01:00", last_intraday=last))
        self.assertNotIn("INTRADAY", self.modes("10:01:30", last_intraday=last, quota_ratio=0.85))
        self.assertIn("INTRADAY", self.modes("10:02:00", last_intraday=last, quota_ratio=0.85))

    def test_closing_round_after_each_session_end_is_not_throttled(self):
        # 收盘定格：刚结束的 bar 在槽边界后约 20–30 秒内还会被改；上一轮早于收盘后 30 秒时，定格窗口内再取一轮
        at = lambda hms: datetime(2026, 9, 28, *(int(x) for x in hms.split(":")))
        for last, now, market in (("15:00:05", "15:00:35", "CN"), ("14:59:50", "15:00:30", "CN"),
                                  ("11:30:10", "11:30:40", "CN"), ("16:10:05", "16:10:35", "HK")):
            self.assertIn("INTRADAY", self.modes(now, market=market, last_intraday=at(last)), (last, now))
        self.assertNotIn("INTRADAY", self.modes("15:00:50", last_intraday=at("15:00:31")))   # 定格轮只一次

    def test_closing_round_missed_by_a_long_round_is_made_up_once(self):
        # 第三阶段复审阻断 4：15:00:29 开始的一轮拖过 15:01:00，定格窗口里没有新一轮；窗口外补一次，有截止，只一次
        at = lambda hms: datetime(2026, 9, 28, *(int(x) for x in hms.split(":")))
        for last, now, market in (("15:00:29", "15:01:06", "CN"), ("11:30:20", "11:32:00", "CN"),
                                  ("16:10:25", "16:11:20", "HK"), ("12:00:10", "12:01:30", "HK")):
            self.assertIn("INTRADAY", self.modes(now, market=market, last_intraday=at(last)), (last, now, market))
        self.assertNotIn("INTRADAY", self.modes("15:01:10", last_intraday=at("15:01:06")))   # 补过了
        self.assertNotIn("INTRADAY", self.modes("15:04:30", last_intraday=at("15:00:29")))   # 过了补轮截止
        self.assertNotIn("INTRADAY", self.modes("15:01:06"))                                  # 今天没有过盘中轮
        # 补轮里的指数同样不受指数节流
        state = {"index_live_at": {"sh000001": at("15:00:29")}}
        self.assertEqual(collector.Collector._intraday_codes(state, ["sh000001"], at("15:01:06")), ["sh000001"])

    def test_finalize_due_from_first_slot_until_day_end(self):
        # 有意改写（计划 2026-09-29 D1）：原用例固定按市场的锁存 finalized/finalize_tried。现在首个定稿时点起
        # 每轮都给 FINALIZE，逐代码的完成判据（事实库）与重试节流在 tick 里判，旧 state 键不再起作用。
        # 有意改写（所有者 2026-09-30）：A 股首个定稿时点 17:30 → 20:00
        self.assertNotIn("FINALIZE", self.modes("19:59"))
        self.assertIn("FINALIZE", self.modes("20:00"))
        self.assertIn("FINALIZE", self.modes("20:01", finalize_tried={"20:00"}))
        self.assertIn("FINALIZE", self.modes("23:59", finalized=True))
        self.assertNotIn("FINALIZE", self.modes("16:29", market="HK"))
        self.assertNotIn("FINALIZE", self.modes("20:00", trading=False))

    def test_cn_first_finalize_slot_is_20_00_and_hk_stays_16_30(self):
        # 所有者 2026-09-30：实测当日日线 17:00 已有、17:30 又短暂返回空，A 股首个定稿时点改 20:00；港股不变
        self.assertNotIn("FINALIZE", self.modes("19:59"))
        self.assertIn("FINALIZE", self.modes("20:00"))
        self.assertNotIn("FINALIZE", self.modes("16:29", market="HK"))
        self.assertIn("FINALIZE", self.modes("16:30", market="HK"))

    def test_hk_windows(self):
        self.assertIn("INTRADAY", self.modes("16:10", market="HK"))
        self.assertIn("FINALIZE", self.modes("16:30", market="HK"))

    def test_non_trading_day(self):
        self.assertEqual(set(self.modes("10:00", trading=False)) - {"KEEPALIVE"}, {"BACKFILL", "CALENDAR"})

    def test_backfill_after_close_on_trading_day(self):
        self.assertNotIn("BACKFILL", self.modes("14:00"))
        self.assertIn("BACKFILL", self.modes("15:06"))

    def test_disabled_collector_does_not_start(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.dict("os.environ", {"COLLECTOR_ENABLED": "0"}):
            self.assertFalse(collector.Collector(tmp).start())

    def test_tick_exports_selfcheck_calendar(self):
        _enable_collector(self)
        with tempfile.TemporaryDirectory() as tmp:
            c = collector.Collector(tmp, providers={"mairui": FakeProvider()},
                                    clock=Clock(datetime(2026, 9, 26, 20, 0).timestamp()),
                                    watchlist_fn=lambda: [])
            c.tick(datetime(2026, 9, 26, 20, 0))
            self.assertTrue((Path(tmp) / "selfcheck_calendar.json").exists())


class TodayFinalizeCoverageTests(unittest.TestCase):
    """当日定稿的覆盖与重试（backlog「非自选收盘后首开缺当日」「定稿无剩余时点不重试」）。

    判据按结果写、不绑定实现：交易日首个定稿时点之后才进入覆盖集合的代码，或当晚定稿失败的代码，
    当日结束前应有可读的当日 final 日线，不必等下一个交易日 15:05 之后的 BACKFILL。"""
    DAY = "2026-09-28"                                  # 周一

    def setUp(self):
        _enable_collector(self)
        _pin_mechanism_schedule(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = Clock(datetime(2026, 9, 28, 17, 0).timestamp())
        _list_since(self.dir, ["sh600036", "sz000001", "sz000002", "sh000001"], "2026-09-21")

    def make(self, provider, watch):
        return collector.Collector(self.dir, providers={"mairui": provider}, clock=self.clock,
                                   watchlist_fn=lambda: list(watch))

    def at(self, c, hh, mm, ss=0):
        now = datetime(2026, 9, 28, hh, mm, ss)
        self.clock.t = now.timestamp()
        c.tick(now)

    def final_today(self, code):
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        try:
            return any(r["trade_date"] == self.DAY and r["provenance"] == "final"
                       for r in facts.read_day_rows(conn, code))
        finally:
            conn.close()

    def test_code_added_to_watchlist_after_finalize_is_finalized_same_evening(self):
        watch = ["sh600036"]
        c = self.make(FullFake(), watch)
        self.at(c, 17, 30)                              # 首个定稿时点：当时的自选全部定稿成功
        self.assertTrue(self.final_today("sh600036"))
        watch.append("sz000001")                        # 定稿成功之后才加入自选
        for hh, mm in ((17, 45), (18, 30), (20, 0), (21, 0), (23, 0)):
            self.at(c, hh, mm)
        self.assertTrue(self.final_today("sz000001"))

    def test_finalize_failing_after_last_slot_is_retried_same_evening(self):
        # 09-28 sz300209：部署晚于最后定稿时点，21:17 唯一一次定稿失败后当晚不再重试（靠重启服务绕过）
        class FirstTodayFails(FullFake):
            failed = False

            def day_history(self, code, start, end):
                if start == end == TodayFinalizeCoverageTests.DAY and not self.failed:
                    self.failed = True
                    self.calls.append(("day", code, start, end))
                    raise ProviderError("503")
                return super().day_history(code, start, end)

        c = self.make(FirstTodayFails(), ["sh600036"])
        self.at(c, 21, 15)                              # 服务在 20:00 之后启动：补跑一次定稿，失败
        self.assertFalse(self.final_today("sh600036"))
        for hh, mm in ((21, 45), (22, 30), (23, 30)):
            self.at(c, hh, mm)
        self.assertTrue(self.final_today("sh600036"))

    def test_first_open_after_finalize_has_today_after_read_catch_up(self):
        # S4 验收：收盘定稿之后才首开的 sz000002 只显示到上一交易日并标 stale。
        # 有意改写（目标 2026-09-29 第二阶段）：原 test_first_open_after_finalize_has_today_by_next_tick 固定「查看中的
        # 非自选由下一轮调度定稿」；现在调度线程只为自选定稿，搜索查看在读取路径的限时追赶里补当日
        c = self.make(FullFake(), ["sh600036"])
        self.at(c, 17, 30)
        self.clock.t = datetime(2026, 9, 28, 21, 0).timestamp()
        c.ensure_window("sz000002", "day")              # 门面首开：同步建窗口
        c.touch_viewing("sz000002")
        self.at(c, 21, 0, 10)                           # 调度线程不为非自选定稿
        self.assertFalse(self.final_today("sz000002"))
        now = datetime(2026, 9, 28, 21, 0, 20)
        self.assertTrue(c.lagging("sz000002", now))
        c.catch_up("sz000002", now)                     # 门面读取时的追赶
        self.assertTrue(self.final_today("sz000002"))
