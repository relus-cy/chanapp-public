"""当日定稿按（代码，交易日）调度（计划 2026-09-29 决策 D1–D3，失败模式 F4–F15、F17）。

判据按结果写：请求次数与时刻、事实库里的定稿行、缺口与 last_error、待核验记录。
F1–F3 在 test_kline_collector.TodayFinalizeCoverageTests。"""
import logging
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import bindings, collector, facts, sessions, views
from chanapp.engine.kline import hk_vendor_qfq as vq
from chanapp.engine.kline.providers.raw import ProviderError, ProviderServerError
from chanapp.engine.kline.rows import FetchItem, RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests.test_kline_collector import (DAY_VOL, FACT, Clock, FullFake, HKRangeFake, HKVendorFake,
                                                _enable_collector, _list_since, _pin_mechanism_schedule, _weekdays)

DAY = "2026-09-28"                                   # 周一
CODE = "sh600036"
SLOTS = sessions.slots("CN", FACT)          # 现行分钟事实网格
CHECKS = facts._checks_scope(CODE, FACT)[0]    # 有意改写（第三阶段第二轮复审）：现行粒度的分钟日线核对所在的表


def at(hh, mm, ss=0, day=28):
    return datetime(2026, 9, day, hh, mm, ss)


def today_day_calls(provider, code=CODE):
    return [call for call in provider.calls if call[0] == "day" and call[1] == code and call[2] == call[3] == DAY]


def today_calls(provider, code=CODE):
    return [call for call in provider.calls if call[1] == code and DAY in (call[2][:10], call[3][:10])]


def _mute_system_index(provider):
    """不在自选的上证指数由调度线程只定稿当日日线（系统依赖，目标 2026-09-29 第二阶段评审修复，行为见
    test_kline_viewing_tracking.SystemIndexRoleTests）。本文件只测单个代码的定稿：指数当日日线请求在这里返回空、
    不记账，既不进 calls/times，也不因写入指数 final 行把当日推成已知交易日（日历未知与假日用例依赖这一点）。"""
    inner = provider.day_history

    def day_history(code, start, end):
        if code == "sh000001" and start == end == DAY:
            return []
        return inner(code, start, end)
    provider.day_history = day_history


class Timed(FullFake):
    """记录每次请求时的时钟（hh:mm）；fail_today 时当日日线请求抛 fail_with。"""

    def __init__(self, clock, *, fail_today=False, fail_with=None):
        super().__init__()
        self.clock, self.fail_today = clock, fail_today
        self.fail_with = fail_with or ProviderError("503")
        self.times = []

    def day_history(self, code, start, end):
        if start == end == DAY:
            self.times.append((code, datetime.fromtimestamp(self.clock.t).strftime("%H:%M")))
            if self.fail_today:
                self.calls.append(("day", code, start, end))
                raise self.fail_with
        return super().day_history(code, start, end)


class Base(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        _pin_mechanism_schedule(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = Clock(at(17, 0).timestamp())
        _list_since(self.dir, [CODE, "sz000001", "sh000001"], "2026-09-21")
        self.conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(self.conn.close)
        self.quiet_history([CODE, "sz000001", "sh000001", "hk00700"])

    def quiet_history(self, codes):
        """历史规划视为已完成（首次规划日即今天）：BACKFILL 不发请求，只测定稿。"""
        with facts.write_txn(self.conn):
            for code in codes:
                for kind in ("backfill", "minute"):
                    facts.set_setting(self.conn, collector.plan_key(code, kind), DAY)

    def known_calendar(self, market="CN", closed=()):
        with facts.write_txn(self.conn):
            for d in _weekdays("2026-09-01", "2026-10-30"):
                self.conn.execute("INSERT OR REPLACE INTO calendar(market, date, is_open, sessions, source,"
                                  " fetched_at) VALUES (?,?,?,'[]','test',?)",
                                  (market, d, int(d not in closed), facts.now_iso()))

    def make(self, provider, watch=(CODE,), source="mairui"):
        watch = list(watch)
        _mute_system_index(provider)
        return collector.Collector(self.dir, providers={source: provider}, clock=self.clock,
                                   watchlist_fn=lambda: list(watch))

    def tick(self, c, when):
        self.clock.t = when.timestamp()
        c.tick(when)

    def ticks(self, c, start, end, step_s=60):
        t = start.timestamp()
        while t <= end.timestamp():
            self.tick(c, datetime.fromtimestamp(t))
            t += step_s

    def final_today(self, code=CODE):
        return any(r["trade_date"] == DAY and r["provenance"] == "final" for r in facts.read_day_rows(self.conn, code))

    def gaps(self, code=CODE):
        return {(g["dataset"], g["reason"]) for g in facts.open_gaps(self.conn, code)}

    def last_errors(self, code=CODE):
        return [g["last_error"] for g in facts.open_gaps(self.conn, code)]

    def commit_today(self, code=CODE, *, slots=SLOTS, forming=(), day_row=True, sf=0, twice=False):
        """直接写入当日事实（与 FullFake 同值，分钟聚合与日线一致）；twice 时每槽先 forming 再 closed。"""
        if day_row:
            vol = 0 if sf else DAY_VOL
            facts.commit_day_rows(self.conn, [RawDayRow(code, DAY, 10, 10, 10, 10, vol, "lot", 1, "CNY", 10, sf,
                                                        "final", new_batch_id())],
                                  market="CN", kind="stock", item="day_history", source="mairui", binding_gen=1,
                                  today=DAY)
        states = (("forming", "minute_live"), ("closed", "minute_history")) if twice else (("closed", "minute_history"),)
        for state, item in states:
            rows = [RawMinuteRow(code, DAY, f"{DAY} {t}", 10, 10, 10, 10, 1, "lot", 1, state, "traded", new_batch_id())
                    for t in slots if t not in forming]
            facts.commit_minute_rows(self.conn, rows, market="CN", kind="stock", item=item, fact_freq=FACT,
                                     source="mairui", binding_gen=1, today=DAY)
        if forming:
            rows = [RawMinuteRow(code, DAY, f"{DAY} {t}", 10, 10, 10, 10, 1, "lot", 1, "forming", "traded",
                                 new_batch_id()) for t in forming]
            facts.commit_minute_rows(self.conn, rows, market="CN", kind="stock", item="minute_live", fact_freq=FACT,
                                     source="mairui", binding_gen=1, today=DAY)

    def pending_count(self, code=CODE):
        return self.conn.execute("SELECT COUNT(*) FROM pending_review WHERE code=? AND verdict IS NULL",
                                 (code,)).fetchone()[0]


class RestartTests(Base):
    def test_restart_with_complete_facts_makes_no_request_and_closes_leftover_gaps(self):
        # F4（09-28 21:44）：重启清空内存后，库内当日已完整的代码不再取数，遗留的定稿与同范围写失败缺口零请求关闭
        self.tick(self.make(FullFake()), at(17, 30))
        self.assertTrue(self.final_today())
        lo, hi = f"{DAY} 09:30", f"{DAY} 15:00"
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, CODE, "day", DAY, DAY, "finalize")
            facts.record_gap(self.conn, CODE, FACT, lo, hi, "finalize")
            self.conn.execute("INSERT INTO coverage_gaps(code, dataset, start, end, reason, created_at)"
                              " VALUES (?,?,?,?,?,?)", (CODE, FACT, lo, hi, "write_failed", facts.now_iso()))
        provider = FullFake()
        self.tick(self.make(provider), at(21, 44))
        self.assertEqual(provider.calls, [])
        self.assertEqual(facts.open_gaps(self.conn, CODE), [])


class ReviewTests(Base):
    def test_unresolved_conflict_is_terminal_without_refetch(self):
        # F5：当日有未裁决冲突时不算完成，当日不取数、不再插冲突记录，last_error 写明待核验
        self.known_calendar()
        self.commit_today()
        with facts.write_txn(self.conn):
            facts._pending(self.conn, CODE, FACT, f"{DAY} 10:00", {"close": 11}, "closed_conflict", "b1")
        provider = FullFake()
        c = self.make(provider)
        for when in (at(17, 30), at(17, 40), at(18, 30), at(20, 0), at(22, 0)):
            self.tick(c, when)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.pending_count(), 1)
        self.assertTrue(self.gaps() >= {("day", "finalize")}, self.gaps())
        self.assertTrue(all(e and "pending_review" in e for e in self.last_errors()), self.last_errors())

    def test_reconcile_mismatch_is_terminal_for_the_day(self):
        class Mismatch(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return [RawDayRow(code, end, 10, 11, 10, 10, DAY_VOL, "lot", 1, "CNY", 10, 0, "final", new_batch_id())]

        self.known_calendar()
        provider = Mismatch()
        c = self.make(provider)
        for when in (at(17, 30), at(17, 40), at(18, 30), at(20, 0), at(22, 0)):
            self.tick(c, when)
        self.assertEqual(len(today_day_calls(provider)), 1)
        self.assertEqual([r["status"] for r in self.conn.execute(f"SELECT status FROM {CHECKS}")], ["pending_review"])
        self.assertTrue(all(e and "reconcile_mismatch" in e for e in self.last_errors()), self.last_errors())

    def test_conflicts_from_first_attempt_are_not_reinserted(self):
        old = [RawMinuteRow(CODE, DAY, f"{DAY} {t}", 10, 10, 10, 10, 1, "lot", 2, "closed", "traded", new_batch_id())
               for t in SLOTS[:2]]
        facts.commit_minute_rows(self.conn, old, market="CN", kind="stock", item="minute_history", fact_freq=FACT,
                                 source="mairui", binding_gen=1, today=DAY)
        self.known_calendar()
        c = self.make(FullFake())
        for when in (at(17, 30), at(17, 40), at(18, 30), at(20, 0), at(22, 0)):
            self.tick(c, when)
        self.assertEqual(self.pending_count(), 2)


class SlotTests(Base):
    """F6：完成判据按槽位集合与既有隔离规则（新实例 21:00 一轮：判完成则零请求）。"""

    def run_restart(self):
        provider = FullFake()
        self.tick(self.make(provider), at(21, 0))
        return provider

    def test_revised_slots_count_once(self):
        self.commit_today(twice=True)
        self.assertEqual(self.run_restart().calls, [])

    def test_missing_slot_is_incomplete(self):
        self.commit_today(slots=SLOTS[:-1])
        self.assertTrue([c for c in self.run_restart().calls if c[0] == FACT])

    def test_forming_slot_is_incomplete(self):
        self.commit_today(forming=(SLOTS[-1],))
        self.assertTrue([c for c in self.run_restart().calls if c[0] == FACT])

    def test_quarantined_missing_slot_is_not_refetched(self):
        self.commit_today(slots=SLOTS[:-1])
        with facts.write_txn(self.conn):
            facts.quarantine(self.conn, CODE, FACT, f"{DAY} {SLOTS[-1]}", "review_conflict")
        self.assertEqual(self.run_restart().calls, [])

    def test_quarantined_slot_is_a_terminal_review_not_completion(self):
        # 终审应修（目标 2026-09-29 第四阶段）：隔离槽是已证实错误的数据，不重取也不能认证完成——与图表状态栏
        # 同一判据（图表仍是待定稿），定稿记待核验终态，不记 done
        self.commit_today(slots=SLOTS[:-1])
        with facts.write_txn(self.conn):
            facts.quarantine(self.conn, CODE, FACT, f"{DAY} {SLOTS[-1]}", "review_conflict")
        self.known_calendar()
        provider = FullFake()
        c = self.make(provider)
        out = c.finalize_due([CODE], DAY, at(17, 30), calendar_known=True)
        self.assertEqual(out["review"], [CODE], out)
        self.assertFalse(c._final_record(CODE, DAY)["done"])
        # 同库图表口径一致：分钟图不报已定稿（与待核验等终态同样显示「待定稿」），后续时点也不再请求
        status = views.read_view(self.conn, CODE, "m30", adjust="raw", now=at(17, 31)).coverage["data_status"]
        self.assertEqual((status["phase"], status["day"]), ("awaiting_final", DAY))
        # 终态本身（零请求可能只是再次判出隔离）：内存与持久台账都记终态，之后的时点与重启都不再消耗次数
        ledger = facts.setting(self.conn, f"finalize_ledger:{DAY}:{CODE}")
        self.assertTrue(ledger["terminal"], ledger)
        self.assertTrue(c._final_record(CODE, DAY)["terminal"])
        calls = len(provider.calls)
        again = c.finalize_due([CODE], DAY, at(19, 30), calendar_known=True)
        self.assertEqual(len(provider.calls), calls)
        self.assertFalse(again["review"] or again["done"], again)
        self.assertEqual(facts.setting(self.conn, f"finalize_ledger:{DAY}:{CODE}")["attempts"], ledger["attempts"])
        restarted = self.make(provider)
        self.assertFalse(restarted.finalize_due([CODE], DAY, at(21, 30), calendar_known=True)["review"])
        self.assertEqual(len(provider.calls), calls)


class FailureReasonTests(Base):
    def test_last_error_names_stage_and_class_and_warns_once_per_reason(self):
        # F7：失败原因可查（阶段 + 脱敏类别），同一原因不重复告警；暂缓不告警
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True,
                         fail_with=ProviderServerError("HTTP 503 licence=SECRET"))
        c = self.make(provider)
        with self.assertLogs(collector.log, level="INFO") as logs:
            for when in (at(17, 30), at(19, 30)):
                self.tick(c, when)
            c.source_breaker.until["mairui"] = at(23, 59).timestamp()      # 冷却：暂缓，不告警
            for when in (at(21, 30), at(22, 0), at(23, 30)):
                self.tick(c, when)
        self.assertEqual(len(provider.times), 2)
        errors = self.last_errors()
        self.assertTrue(errors and all(e and e.startswith("finalize/fetch:ProviderServerError") for e in errors), errors)
        self.assertFalse(any("SECRET" in e for e in errors))
        warns = [r for r in logs.records if r.levelno >= logging.WARNING and CODE in r.getMessage()
                 and "定稿" in r.getMessage()]
        self.assertEqual(len(warns), 1, [r.getMessage() for r in warns])


class GapCloseTests(Base):
    def test_complete_day_closes_gaps_without_requests_under_cooldown_and_quota(self):
        # F8：已覆盖的当日缺口不联网即关闭，冷却与额度耗尽时也照样执行
        self.known_calendar()
        self.commit_today()
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, CODE, "day", DAY, DAY, "finalize")
            facts.record_gap(self.conn, CODE, FACT, f"{DAY} 09:30", f"{DAY} 15:00", "write_failed")
            self.conn.execute("INSERT INTO quota_usage(source, day, count) VALUES ('mairui', ?, 10000)", (DAY,))
        provider = FullFake()
        c = self.make(provider)
        c.source_breaker.until["mairui"] = at(23, 59).timestamp()
        self.tick(c, at(18, 0))
        self.assertEqual(provider.calls, [])
        self.assertEqual(facts.open_gaps(self.conn, CODE), [])


class RetryScheduleTests(Base):
    # 失败重试间隔、次数上限、日历未知时点与跨重启见 test_kline_finalize_ledger
    def test_no_today_day_request_before_first_slot(self):
        # F10：首个定稿时点之前（15:00–17:30）不取今天的日线（麦蕊把返回的日线一律标 final）
        self.known_calendar()
        provider = Timed(self.clock)
        c = self.make(provider, watch=(CODE, "sz000001"))
        self.ticks(c, at(15, 1), at(17, 29), step_s=300)
        self.assertEqual(provider.times, [])
        self.assertFalse(self.final_today())


class CalendarAndDateTests(Base):
    def test_task_fixes_target_day_across_midnight(self):
        # F11：任务开始时固定目标日；跨过午夜才返回也提交给原日期；午夜之后不按前一天重试
        self.known_calendar()
        clock = self.clock

        class Slow(FullFake):
            def day_history(inner, code, start, end):
                if start == end == DAY:
                    clock.t = at(0, 0, 10, day=29).timestamp()
                return FullFake.day_history(inner, code, start, end)

        provider = Slow()
        c = self.make(provider)
        self.tick(c, at(23, 59, 50))
        self.assertTrue(self.final_today())
        self.assertEqual(self.gaps(), set())
        before = len(provider.calls)
        self.tick(c, at(0, 5, day=29))
        self.assertEqual([call for call in provider.calls[before:] if DAY in call[2] + call[3]], [])

    def test_known_holiday_has_no_finalize(self):
        self.known_calendar(closed=(DAY,))
        provider = FullFake()
        c = self.make(provider)
        for when in (at(17, 30), at(20, 0), at(22, 0)):
            self.tick(c, when)
        self.assertEqual(today_calls(provider), [])


class HKRawFake(HKVendorFake):
    """供应商按请求区间逐工作日返回、m30 给齐全部槽位（真实契约，与 raw 同一归一标签）：只回末日或缺槽的序列会被
    发布前的覆盖检查拒绝。"""

    def qfq_series(self, code, freq, start, end):
        if freq == "day":
            return HKRangeFake.qfq_series(self, code, freq, start, end)
        self.calls.append((freq, start, end))
        if self.fail_qfq or freq in self.fail_freqs:
            raise ProviderError("down")
        c = self.qfq_close
        return [{"trade_date": d, "slot_end": f"{d} {t}", "open": c, "high": c, "low": c, "close": c,
                 "volume": 10.0, "amount": None, "volume_unit": "share"}
                for d in _weekdays(start, end) for t in sessions.slots("HK", "m30") if start <= f"{d} {t}" <= end]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.raw = []

    def day_history(self, code, start, end):
        self.raw.append(("day", start, end))
        return super().day_history(code, start, end)

    def minute_history(self, code, fact_freq, start, end, *, now):
        self.raw.append(("m30", start, end))
        return super().minute_history(code, fact_freq, start, end, now=now)


class HKVendorOnlyTests(Base):
    HK = "hk00700"

    def setup_hk(self, fake):
        """原始事实当日已完整，供应商缓存只到上一交易日。"""
        c = self.make(fake, watch=(self.HK,), source="longbridge")
        self.clock.t = at(16, 0).timestamp()
        c.refresh_vendor_qfq(self.HK, at(16, 0), closed_through="2026-09-25")
        fake.calls.clear()
        n = len(sessions.slots("HK", "m30"))
        facts.commit_day_rows(self.conn, [RawDayRow(self.HK, DAY, 100, 100, 100, 100, 10 * n, "share", None, "HKD",
                                                    100, 0, "final", new_batch_id())],
                              market="HK", kind="stock", item="day_history", source="longbridge", binding_gen=1,
                              today=DAY)
        rows = [RawMinuteRow(self.HK, DAY, f"{DAY} {t}", 100, 100, 100, 100, 10, "share", None, "closed", "traded",
                             new_batch_id()) for t in sessions.slots("HK", "m30")]
        facts.commit_minute_rows(self.conn, rows, market="HK", kind="stock", item="minute_history", fact_freq="m30",
                                 source="longbridge", binding_gen=1, today=DAY)
        return c

    def test_raw_complete_refreshes_only_vendor_cache(self):
        # F12：原始事实已完成只刷新供应商缓存，不重取 raw，也不登记 raw 缺口
        fake = HKRawFake()
        c = self.setup_hk(fake)
        self.tick(c, at(16, 30))
        self.assertEqual(fake.raw, [])
        self.assertTrue(fake.calls)
        bars, meta = vq.read(self.conn, self.HK, "day")
        self.assertEqual((bars[-1]["trade_date"], meta["stale"]), (DAY, False))
        self.assertEqual(facts.open_gaps(self.conn, self.HK), [])

    def test_vendor_failure_retries_cache_only(self):
        fake = HKRawFake()
        c = self.setup_hk(fake)
        fake.fail_qfq = True
        self.tick(c, at(16, 30))
        first = len(fake.calls)
        self.assertTrue(first)
        self.assertTrue(vq.read(self.conn, self.HK, "day")[1]["stale"])
        self.tick(c, at(18, 30))                         # 隔 2 小时（港股第二个定稿时点）再试缓存
        self.assertGreater(len(fake.calls), first)
        self.assertEqual(fake.raw, [])
        self.assertEqual(facts.open_gaps(self.conn, self.HK), [])


    def test_throttled_vendor_retry_does_no_checks_or_writes(self):
        # 审查发现：节流中的代码每 5 秒一轮不得重跑核对（reconcile_day 会写 day_checks、soft_flags）
        fake = HKRawFake()
        c = self.setup_hk(fake)
        fake.fail_qfq = True
        self.tick(c, at(16, 30))
        with unittest.mock.patch.object(facts, "reconcile_day", wraps=facts.reconcile_day) as spy:
            self.ticks(c, at(16, 30, 5), at(16, 34, 55), step_s=5)
        self.assertEqual(spy.call_count, 0)


class ContentionTests(Base):
    def run_tick(self, c, when):
        worker = threading.Thread(target=self.tick, args=(c, when), daemon=True)
        worker.start()
        worker.join(5)
        return not worker.is_alive()

    def test_code_lock_busy_defers_without_request(self):
        # F13：同一代码的首取在途（单飞锁被占）时，调度线程本轮跳过该代码，不阻塞、不计失败
        self.known_calendar()
        provider = FullFake()
        c = self.make(provider)
        held, release = threading.Event(), threading.Event()

        def hold():
            with c._single_flight(CODE):
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(held.wait(5))
        try:
            self.assertTrue(self.run_tick(c, at(17, 30)), "调度线程被单飞锁阻塞")
            self.assertEqual(today_calls(provider), [])
        finally:
            release.set()
            holder.join(5)
        self.assertTrue(self.run_tick(c, at(17, 30, 5)))
        self.assertTrue(self.final_today())
        self.assertEqual(self.gaps(), set())

    def test_writer_locked_defers_without_request_or_gap_attempt(self):
        self.known_calendar()
        provider = FullFake()
        c = self.make(provider)
        other = collector.Collector(self.dir, providers={}, clock=self.clock)
        holding = other.writer()
        holding.__enter__()
        try:
            self.tick(c, at(17, 30))
            self.assertEqual(today_calls(provider), [])
        finally:
            holding.__exit__(None, None, None)
        self.tick(c, at(17, 30, 5))
        self.assertTrue(self.final_today())
        self.assertEqual(self.gaps(), set())


class SuspensionTests(Base):
    def test_suspended_final_needs_no_minutes(self):
        # F14：可读 final 且 sf=1 即豁免分钟，完成后不再请求
        class Suspended(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return [RawDayRow(code, end, 10, 10, 10, 10, 0, "lot", 0, "CNY", 10, 1, "final", new_batch_id())]

            def minute_history(self, code, fact_freq, start, end, *, now):
                self.calls.append((fact_freq, code, start, end))
                return []

        self.known_calendar()
        provider = Suspended()
        c = self.make(provider)
        self.tick(c, at(17, 30))
        self.assertTrue(self.final_today())
        n = len(provider.calls)
        for when in (at(17, 40), at(18, 30), at(20, 0)):
            self.tick(c, when)
        self.assertEqual(len(provider.calls), n)
        self.assertEqual(self.gaps(), set())

    def test_empty_return_is_not_suspension(self):
        class Empty(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return []

            def minute_history(self, code, fact_freq, start, end, *, now):
                self.calls.append((fact_freq, code, start, end))
                return []

        self.known_calendar()
        provider = Empty()
        c = self.make(provider)
        self.tick(c, at(17, 30))
        self.assertFalse(self.final_today())
        self.assertIn(("day", "finalize"), self.gaps())
        self.assertTrue(all(e and e.startswith("finalize/check:missing_final") for e in self.last_errors()),
                        self.last_errors())
        self.tick(c, at(19, 30))
        self.assertEqual(len(today_day_calls(provider)), 2)

    def test_suspended_day_with_traded_minutes_is_not_done(self):
        class Contradiction(FullFake):
            def day_history(self, code, start, end):
                self.calls.append(("day", code, start, end))
                return [RawDayRow(code, end, 10, 10, 10, 10, 0, "lot", 0, "CNY", 10, 1, "final", new_batch_id())]

        self.known_calendar()
        provider = Contradiction()
        c = self.make(provider)
        for when in (at(17, 30), at(17, 40), at(18, 30)):
            self.tick(c, when)
        self.assertEqual([r["status"] for r in self.conn.execute(f"SELECT status FROM {CHECKS}")], ["pending_review"])
        self.assertTrue(all(e and "reconcile_mismatch" in e for e in self.last_errors()), self.last_errors())
        self.assertEqual(len(today_day_calls(provider)), 1)


class PartialCommitTests(Base):
    def test_write_failure_is_not_success_and_not_a_provider_failure(self):
        # F15：部分提交后写失败不报成功、不计 provider 失败，其他代码继续
        self.known_calendar()
        c = self.make(FullFake(), watch=(CODE, "sz000001"))
        real = facts.commit_minute_rows

        def flaky(conn, rows, **kw):
            if rows and rows[0].code == CODE:
                raise facts.FactsWriteError("disk full")
            return real(conn, rows, **kw)

        with unittest.mock.patch.object(facts, "commit_minute_rows", flaky):
            self.tick(c, at(17, 30))
        self.assertTrue(self.final_today("sz000001"))
        self.assertIn((FACT, "finalize"), self.gaps())
        self.assertTrue(all(e and e.startswith("finalize/commit:write_failed") for e in self.last_errors()),
                        self.last_errors())
        self.assertEqual((c.code_backoff.failures[CODE], c.source_breaker.failures["mairui"]), (0, 0))

    def test_binding_switch_during_fetch_counts_the_attempt_and_retries_after_interval(self):
        # 有意改写（第三阶段第三轮复审阻断 2）：原用例要求绑定切换暂缓后下一轮立即重试、不计次。日线请求已经发出，
        # 按「一旦发出请求，后续暂缓保留次数」这次照算，隔 2 小时再试
        conn = self.conn

        class Switching(FullFake):
            switched = False

            def day_history(inner, code, start, end):
                if start == end == DAY and not inner.switched:
                    inner.switched = True
                    bindings.switch(conn, "CN", "stock", FetchItem.DAY_HISTORY, "baostock", reason="test")
                    bindings.switch(conn, "CN", "stock", FetchItem.DAY_HISTORY, "mairui", reason="test")
                return FullFake.day_history(inner, code, start, end)

        self.known_calendar()
        c = self.make(Switching())
        self.tick(c, at(17, 30))
        self.assertEqual(c.code_backoff.failures[CODE], 0)
        self.tick(c, at(17, 30, 5))
        self.assertFalse(self.final_today())
        self.assertEqual((facts.setting(self.conn, f"finalize_ledger:{DAY}:{CODE}") or {}).get("attempts"), 1)
        self.tick(c, at(19, 30, 5))
        self.assertTrue(self.final_today())
        self.assertEqual(self.gaps(), set())


class ViewingTests(Base):
    def test_scheduler_never_finalizes_viewed_untracked_code(self):
        # 有意改写（目标 2026-09-29 第二阶段）：原 test_viewed_code_retries_only_while_viewed 固定 F17「非自选查看期内由
        # 调度线程按节流重试」；搜索查看没有后台重试承诺，调度线程只为自选定稿，查看中的代码由读取路径追赶补当日
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider, watch=())
        self.clock.t = at(17, 26).timestamp()
        c.touch_viewing(CODE)
        self.ticks(c, at(17, 30), at(17, 59))
        self.assertEqual(provider.times, [])


if __name__ == "__main__":
    unittest.main()


class SlowLive(FullFake):
    """盘中增量：记录每次请求的时钟；slow_at 那一轮的第一次请求耗时 40 秒（整轮拖过定格窗口）。"""

    def __init__(self, clock, slow_at):
        super().__init__()
        self.clock, self.slow_at, self.live = clock, slow_at, []

    def minute_live(self, code, fact_freq, *, now):
        self.live.append((code, datetime.fromtimestamp(self.clock.t).strftime("%H:%M:%S")))
        if now == self.slow_at:
            self.clock.t += 40
        return []


class ClosingRoundTests(Base):
    def test_first_code_of_a_long_round_before_settle_gets_one_more_round(self):
        # 第三阶段复审阻断 4：15:00:29 开始的轮次在首个代码上耗时 40 秒；调度线程下一轮 15:01:14 仍补一轮定格
        self.known_calendar()
        provider = SlowLive(self.clock, at(15, 0, 29))
        c = self.make(provider, watch=(CODE, "sz000001"))
        self.tick(c, at(15, 0, 29))
        self.tick(c, at(15, 1, 14))
        self.tick(c, at(15, 1, 19))
        self.assertEqual([t for code, t in provider.live if code == CODE], ["15:00:29", "15:01:14"])
