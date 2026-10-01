"""定稿重试台账（目标 2026-09-29「周期、刷新与定稿」，第三阶段）：失败后隔 2 小时、每个（代码，交易日）至多 3 次
自动尝试；次数与下次资格跨重启保留；暂缓不计次；手动重拉与自动次数分开；读取追赶不能绕过上限。

判据按结果写：当日请求发生的时刻（Timed.times）、lagging 与持久台账。"""
import sqlite3
import threading
import unittest
from unittest import mock

from chanapp.engine.kline import collector, facts, sessions
from chanapp.engine.kline import hk_vendor_qfq as vq
from chanapp.engine.kline.providers.raw import ProviderError, ProviderUnsupported
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests.test_kline_collector import FullFake
from chanapp.tests.test_kline_finalize_schedule import (CODE, DAY, Base, HKRawFake, HKVendorOnlyTests, Timed,
                                                        _mute_system_index, at)


class FailEveryDay(FullFake):
    """任何单日日线请求都失败（跨日台账用）。"""

    def day_history(self, code, start, end):
        if start == end:
            self.calls.append(("day", code, start, end))
            raise ProviderError("503")
        return super().day_history(code, start, end)


class SlowFail(Timed):
    """当日日线请求耗时 3 分钟后失败（第三阶段复审阻断 3：间隔须从实际失败时刻算，而非本轮开始时刻）。"""

    def day_history(self, code, start, end):
        if start == end == DAY:
            self.times.append((code, collector.datetime.fromtimestamp(self.clock.t).strftime("%H:%M")))
            self.clock.t += 180
            raise ProviderError("503")
        return super().day_history(code, start, end)


class Blocking(Timed):
    """当日日线请求在 release 之前挂起（在途请求）。"""

    def __init__(self, clock):
        super().__init__(clock)
        self.entered, self.release = threading.Event(), threading.Event()

    def day_history(self, code, start, end):
        if start == end == DAY and code == CODE:
            self.entered.set()
            self.release.wait(5)
        return super().day_history(code, start, end)


class _Called(Exception):
    pass


def _method_of(fn):
    """_call 收到的 lambda 要调 provider 的哪个方法（测试里区分日线与分钟请求）。"""
    class Probe:
        def __getattr__(self, name):
            raise _Called(name)
    try:
        fn(Probe())
    except _Called as called:
        return called.args[0]
    return None


class LedgerTests(Base):
    def times(self, provider):
        return [t for code, t in provider.times if code == CODE]

    def test_failures_retry_every_two_hours_at_most_three_times(self):
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider)
        self.ticks(c, at(17, 30), at(23, 59))
        self.assertEqual(self.times(provider), ["17:30", "19:30", "21:30"])

    def test_restart_keeps_attempts_and_next_retry_time(self):
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        self.ticks(self.make(provider), at(17, 30), at(18, 0))
        self.ticks(self.make(provider), at(18, 1), at(21, 40))           # 重启：不立即重试，仍按 19:30、21:30
        self.assertEqual(self.times(provider), ["17:30", "19:30", "21:30"])
        c = self.make(provider)                                           # 再重启：3 次已用完
        self.clock.t = at(22, 0).timestamp()
        self.assertFalse(c.lagging(CODE, at(22, 0)))
        self.ticks(c, at(22, 0), at(23, 59))
        self.assertEqual(len(self.times(provider)), 3)

    def test_read_catch_up_cannot_bypass_interval_or_cap(self):
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider)
        self.tick(c, at(17, 30))
        self.clock.t = at(18, 0).timestamp()
        c.catch_up(CODE, at(18, 0))                                       # 间隔未到
        self.ticks(c, at(18, 1), at(21, 31))
        self.clock.t = at(22, 0).timestamp()
        c.catch_up(CODE, at(22, 0))                                       # 次数已满
        self.assertEqual(self.times(provider), ["17:30", "19:30", "21:30"])

    def test_retry_interval_counts_from_when_the_attempt_failed(self):
        self.known_calendar()
        provider = SlowFail(self.clock)
        c = self.make(provider)
        self.ticks(c, at(17, 30), at(23, 59))
        self.assertEqual(self.times(provider), ["17:30", "19:33", "21:36"])

    def test_no_automatic_request_while_the_ledger_cannot_be_persisted(self):
        # 第三阶段第二轮复审阻断 2：台账写不进时不发自动请求（否则重启后次数归零、可超过 3 次）；恢复后照常
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider)
        real = facts.set_setting

        def broken(conn, key, value):
            if key.startswith("finalize_ledger:"):
                raise facts.FactsWriteError("disk full")
            return real(conn, key, value)
        with mock.patch.object(facts, "set_setting", side_effect=broken):
            self.ticks(c, at(17, 30), at(19, 45))
            c = self.make(provider)                                       # 重启
            self.ticks(c, at(19, 46), at(20, 0))
        self.assertEqual(self.times(provider), [])
        self.ticks(c, at(20, 1), at(23, 59))
        self.assertEqual(self.times(provider), ["20:01", "22:01"])

    def test_writer_locked_deferral_does_not_consume_an_attempt(self):
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider)
        other = collector.Collector(self.dir, providers={}, clock=self.clock)
        holding = other.writer()
        holding.__enter__()
        try:
            self.tick(c, at(17, 30))
        finally:
            holding.__exit__(None, None, None)
        self.ticks(c, at(17, 31), at(23, 59))
        self.assertEqual(self.times(provider), ["17:31", "19:31", "21:31"])

    def test_manual_refetch_neither_counts_nor_resets_automatic_attempts(self):
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider)
        self.tick(c, at(17, 30))
        self.clock.t = at(18, 0).timestamp()
        c.refetch_window(CODE, "day")
        self.ticks(c, at(18, 1), at(23, 59))
        self.assertEqual(self.times(provider), ["17:30", "18:00", "19:30", "21:30"])

    def test_late_join_checks_now_then_two_hours_after_the_failure(self):
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        watch = []
        _mute_system_index(provider)
        c = collector.Collector(self.dir, providers={"mairui": provider}, clock=self.clock,
                                watchlist_fn=lambda: list(watch))
        self.ticks(c, at(17, 30), at(20, 9))
        watch.append(CODE)
        self.ticks(c, at(20, 10), at(23, 59))
        self.ticks(c, at(0, 0, day=29), at(0, 30, day=29))
        self.assertEqual(self.times(provider), ["20:10", "22:10"])

    def test_unknown_calendar_uses_the_new_slots(self):
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider)
        self.ticks(c, at(17, 30), at(23, 55), step_s=300)
        self.assertEqual(self.times(provider), ["17:30", "19:30", "21:30"])

    def test_unknown_calendar_late_start_is_still_two_hours_apart(self):
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider)
        self.ticks(c, at(21, 15), at(23, 55), step_s=300)
        self.assertEqual(self.times(provider), ["21:15", "23:15"])

    def test_previous_days_are_pruned_from_the_ledger(self):
        self.known_calendar()
        provider = FailEveryDay()
        c = self.make(provider)
        self.tick(c, at(17, 30))
        self.assertTrue(self.ledger_days())
        self.tick(c, at(17, 30, day=29))
        self.assertEqual(self.ledger_days(), {"2026-09-29"})

    def ledger(self):
        return facts.setting(self.conn, f"finalize_ledger:{DAY}:{CODE}") or {}

    def test_deferral_after_a_request_was_sent_keeps_the_attempt(self):
        # 第三阶段第三轮复审阻断 2：日线请求已发出、分钟请求因额度没发出——这次尝试已花了请求，不能退还
        self.known_calendar()
        provider = Timed(self.clock)
        c = self.make(provider)
        real = c._call

        def call(code, source, fn, **kw):
            if code == CODE and _method_of(fn) == "minute_history":
                raise collector._BudgetExhausted()
            return real(code, source, fn, **kw)
        with mock.patch.object(c, "_call", side_effect=call):
            self.ticks(c, at(17, 30), at(19, 0))
        self.assertEqual(self.times(provider), ["17:30"])
        self.assertEqual(self.ledger().get("attempts"), 1)

    def test_lock_busy_caller_does_not_refund_the_holders_reservation(self):
        # 第三阶段第三轮复审阻断 2：读取追赶持锁、请求在途时，调度线程取锁失败不能退还持锁方的预占
        self.known_calendar()
        provider = Blocking(self.clock)
        c = self.make(provider)
        self.clock.t = at(17, 31).timestamp()
        worker = threading.Thread(target=c.catch_up, args=(CODE, at(17, 31)))
        worker.start()
        try:
            self.assertTrue(provider.entered.wait(5))
            self.tick(c, at(17, 31, 5))
            self.assertEqual(self.ledger().get("attempts"), 1, "在途请求的预占仍在台账里")
        finally:
            provider.release.set()
            worker.join(5)

    def test_day_only_completion_is_not_full_completion_across_threads(self):
        # 第三阶段第五轮复审阻断 2：只定稿日线的执行在途时，另一线程以完整定稿来看同一（代码，交易日）——
        # 锁忙暂缓，不能在锁外改掉在途执行的模式；日线完成后完整定稿仍要做（分钟没取），不能被当成已完成跳过
        self.known_calendar()
        provider = Blocking(self.clock)
        c = self.make(provider)
        self.clock.t = at(17, 31).timestamp()
        got = {}
        worker = threading.Thread(target=lambda: got.update(
            c.finalize_due([CODE], DAY, at(17, 31), calendar_known=True, day_only=True)))
        worker.start()
        try:
            self.assertTrue(provider.entered.wait(5))
            busy = c.finalize_due([CODE], DAY, at(17, 31, 5), calendar_known=True)
            self.assertEqual(busy["deferred"], [CODE])
        finally:
            provider.release.set()
            worker.join(5)
        self.assertEqual(got["done"], [CODE])
        self.assertEqual(c._final_status(CODE, DAY), "missing_slots")
        self.assertEqual(c.finalize_due([CODE], DAY, at(17, 32), calendar_known=True)["deferred"], [CODE],
                         "没被当成已完成跳过（共用间隔：距上次尝试不到 2 小时）")
        self.clock.t = at(19, 32).timestamp()
        full = c.finalize_due([CODE], DAY, at(19, 32), calendar_known=True)
        self.assertEqual(full["done"], [CODE], full)
        self.assertEqual(c._final_status(CODE, DAY), "complete")

    def test_interrupted_request_still_counts(self):
        # 第三阶段第五轮复审应修 3：请求已进入上游调用、随后被中断（KeyboardInterrupt 等非 Exception）——结果未知，
        # 照算一次，不按「没发出」退还
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True, fail_with=KeyboardInterrupt())
        c = self.make(provider)
        for when in (at(17, 30), at(19, 31), at(21, 32), at(23, 33)):
            try:
                self.tick(c, when)
            except KeyboardInterrupt:
                pass
        self.assertTrue(self.times(provider))
        self.assertEqual(len(self.times(provider)), self.ledger().get("attempts"))
        self.assertLessEqual(len(self.times(provider)), 3)
        self.assertFalse(self.ledger().get("pending"))

    def test_settlement_failure_does_not_mask_the_original_error(self):
        # 第三阶段第五轮复审应修 3：异常退出时了结预占本身再出错，上抛的仍是原异常
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True, fail_with=KeyboardInterrupt())
        c = self.make(provider)
        with mock.patch.object(c, "_release", side_effect=RuntimeError("settle")):
            with self.assertRaises(KeyboardInterrupt):
                self.tick(c, at(17, 30))

    def test_unsupported_before_any_request_does_not_consume_attempts(self):
        # 第三阶段第五轮复审应修 3：上游能力不可用（没有注入 provider）时一个请求都没发出——暂缓，不耗掉当天 3 次
        self.known_calendar()
        provider = Timed(self.clock)
        c = self.make(provider)
        with mock.patch.object(c, "provider", side_effect=ProviderUnsupported("not injected")):
            self.ticks(c, at(17, 30), at(23, 0), step_s=600)
        self.assertEqual(self.ledger().get("attempts", 0), 0)
        self.tick(c, at(23, 10))
        self.assertEqual(self.times(provider), ["23:10"])
        self.assertEqual(c._final_status(CODE, DAY), "complete")

    def test_restart_after_the_failure_time_was_not_saved_still_waits_two_hours(self):
        # 第三阶段第三轮复审应修 3：预占落盘了、失败时刻没落盘就重启——间隔不能按预占时刻算（17:30 → 19:30 只隔
        # 实际失败 117 分钟）；无法确认结束时刻的尝试按重启时发现它的时刻保守计
        self.known_calendar()
        provider = SlowFail(self.clock)
        c = self.make(provider)
        real = facts.set_setting

        def broken(conn, key, value):
            if key.startswith("finalize_ledger:") and at(17, 31).timestamp() <= self.clock.t < at(17, 40).timestamp():
                raise facts.FactsWriteError("disk full")
            return real(conn, key, value)
        with mock.patch.object(facts, "set_setting", side_effect=broken):
            self.tick(c, at(17, 30))
        c = self.make(provider)                                           # 重启
        self.ticks(c, at(17, 40), at(23, 59))
        got = self.times(provider)
        self.assertEqual(got[0], "17:30")
        self.assertGreaterEqual(got[1], "19:33", got)

    def index_times(self, provider):
        return [t for code, t in provider.times if code == "sh000001"]

    def check_index_modes_share_the_cap(self, first, then):
        # 第三阶段第四轮复审阻断 3：上证指数只定稿日线（不在自选）与完整定稿（在自选）是同一（代码，交易日），共用 3 次
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        watch = list(first)
        c = collector.Collector(self.dir, providers={"mairui": provider}, clock=self.clock,
                                watchlist_fn=lambda: list(watch))
        self.ticks(c, at(17, 30), at(21, 59))
        watch[:] = then
        self.ticks(c, at(22, 0), at(23, 59))
        self.assertEqual(self.index_times(provider), ["17:30", "19:30", "21:30"])

    def test_index_day_only_then_tracked_shares_the_day_cap(self):
        self.check_index_modes_share_the_cap([CODE], [CODE, "sh000001"])

    def test_index_tracked_then_day_only_shares_the_day_cap(self):
        self.check_index_modes_share_the_cap([CODE, "sh000001"], [CODE])

    def ledger_days(self):
        return {r["key"].split(":")[1] for r in self.conn.execute(
            "SELECT key FROM settings WHERE key LIKE 'finalize_ledger:%'")}


class VendorDeferralTests(Base):
    """第三阶段复审阻断 3：港股 raw 已齐、供应商缓存请求因退避、冷却或额度没有发出时是暂缓，不扣自动次数。"""
    HK = HKVendorOnlyTests.HK
    setup_hk = HKVendorOnlyTests.setup_hk

    def ledger(self):
        return facts.setting(self.conn, f"finalize_ledger:{DAY}:{self.HK}") or {}

    def check_deferred_not_counted(self, exc):
        fake = HKRawFake()
        c = self.setup_hk(fake)
        real = c._call

        def call(code, source, fn, **kw):
            if kw.get("capability") == "qfq_series":
                raise exc
            return real(code, source, fn, **kw)
        with mock.patch.object(c, "_call", side_effect=call):
            self.tick(c, at(16, 30))
        self.assertEqual(self.ledger().get("attempts", 0), 0)
        self.tick(c, at(16, 32))                                # 暂缓保持期过后照常刷新缓存，一次完成
        self.assertEqual(self.ledger().get("attempts", 0), 1)   # 只记真正发出的这一次（请求前预占），暂缓那次已退还
        self.assertTrue(c._vendor_caught_up(self.HK, DAY))

    def test_automatic_refresh_does_not_request_today_when_cache_already_has_today(self):
        # 第三阶段第四轮复审阻断 1：缓存末端已到当天（之后标了 stale）不是请求当天的授权；非定稿的自动刷新不请求当天，
        # 已发布的当天缓存保留
        fake = HKRawFake()
        c = self.setup_hk(fake)
        c.refresh_vendor_qfq(self.HK, at(16, 20), closed_through=DAY, today_ok=True)
        with c.writer() as wconn:
            vq.mark_stale(wconn, self.HK)
        fake.calls.clear()
        self.clock.t = at(21, 0).timestamp()
        c.refresh_vendor_qfq(self.HK, at(21, 0), eligible=c.is_tracked)
        self.assertEqual([x for x in fake.calls if x[2][:10] >= DAY], [])
        self.assertEqual(vq.read(self.conn, self.HK, "day")[0][-1]["trade_date"], DAY)

    def test_only_ledger_requests_cover_today_after_finalize_success(self):
        # 第三阶段第五轮复审阻断 1（策略断言）：定稿成功之后，补窗口、扩展、续传刷新等非定稿请求一律只到昨天——
        # 覆盖当天的自动缓存请求只来自台账记过的尝试（本夹具每次尝试一次日线请求，所以两数相等；一般不是请求数相等），
        # 缓存标 stale 或失败都不例外
        fake = HKRawFake()
        c = self.setup_hk(fake)
        self.tick(c, at(16, 30))
        self.assertTrue(c._vendor_caught_up(self.HK, DAY))
        self.clock.t = at(21, 0).timestamp()
        c._ensure_vendor(self.HK, window=5000, widen=True, eligible=c.is_tracked)
        c.refresh_vendor_qfq(self.HK, at(21, 0), eligible=c.is_tracked)
        with c.writer() as wconn:
            vq.mark_stale(wconn, self.HK)
        c.refresh_vendor_qfq(self.HK, at(21, 5), eligible=c.is_tracked)
        fake.fail_qfq = True
        c.refresh_vendor_qfq(self.HK, at(21, 10), closed_through="2026-09-24", eligible=c.is_tracked)
        today = [x for x in fake.calls if x[0] == "day" and x[2][:10] >= DAY]
        self.assertGreater(len([x for x in fake.calls if x[0] == "day"]), len(today))    # 确实发过非定稿请求
        self.assertEqual(len(today), self.ledger().get("attempts"))
        self.assertEqual(vq.read(self.conn, self.HK, "day")[0][-1]["trade_date"], DAY)

    def check_requests_match_ledger(self, patch_target, exc, *, fail_qfq=False):
        # 第三阶段第四轮复审阻断 2：缓存请求发出后，发布或标 stale 阶段出异常——已发请求照算，预占不残留到下一次
        fake = HKRawFake()
        c = self.setup_hk(fake)
        fake.fail_qfq = fail_qfq
        with mock.patch.object(vq, patch_target, side_effect=exc):
            for hh in (16, 18, 20, 22):
                try:
                    self.tick(c, at(hh, 30))
                except Exception:          # noqa: BLE001  未捕获的异常照样让本轮结束；看的是台账
                    pass
        sent = len([x for x in fake.calls if x[0] == "day"])
        self.assertTrue(sent)
        self.assertEqual(self.ledger().get("attempts"), sent)
        self.assertLessEqual(sent, 3)

    def test_publish_lock_busy_after_requests_counts_them(self):
        self.check_requests_match_ledger("publish_set", collector.CollectorLocked("busy"))

    def test_publish_database_error_after_requests_counts_them(self):
        self.check_requests_match_ledger("publish_set", sqlite3.OperationalError("disk I/O error"))

    def test_stale_mark_write_error_after_failed_requests_counts_them(self):
        self.check_requests_match_ledger("mark_stale", facts.FactsWriteError("disk full"), fail_qfq=True)

    def test_skipped_vendor_refresh_is_deferred_not_counted(self):
        self.check_deferred_not_counted(collector._Skipped(self.HK))

    def test_quota_vendor_refresh_is_deferred_not_counted(self):
        self.check_deferred_not_counted(collector._BudgetExhausted())


class EmptyVendor(HKRawFake):
    """供应商前复权每次都「成功」返回空序列（覆盖检查不通过、不发布；上游调用本身不算失败，不触发退避）。"""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.times = clock, []

    def qfq_series(self, code, freq, start, end):
        self.calls.append((freq, start, end))
        self.times.append(collector.datetime.fromtimestamp(self.clock.t).strftime("%H:%M"))
        return []


class TruncatedVendor(HKRawFake):
    """供应商每次都少回请求区间的最后一个交易日（截短序列：覆盖检查不通过、不发布）。"""

    def qfq_series(self, code, freq, start, end):
        return [r for r in super().qfq_series(code, freq, start, end)
                if (r.get("trade_date") or r["slot_end"][:10]) < end[:10]]


class VendorFirstBuildBudgetTests(Base):
    """第三阶段第二轮复审阻断 1：缓存从未建成的港股自选，历史线程的自动首建不能绕过当天的定稿上限。
    交易日过了首个定稿时点，首建让位给定稿（缓存没追上时定稿本来就重试缓存，受同一台账约束）；此前与非交易日，
    首建有自己的持久日预算（每天至多 3 次、失败后隔 2 小时，重启不清零），供应商「成功」返回空集或截短序列也计次。"""
    HK = HKVendorOnlyTests.HK

    def hk_unbuilt(self, fake):
        """原始事实当日已完整、规划已完成，供应商缓存从未建成。"""
        self.known_calendar("HK")
        with facts.write_txn(self.conn):
            for kind in ("backfill", "minute"):
                facts.set_setting(self.conn, collector.plan_key(self.HK, kind), DAY)
        c = self.make(fake, watch=(self.HK,), source="longbridge")
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

    def rounds(self, c, start, end, step_min=10, scheduler=False):
        t = start
        while t <= end:
            self.clock.t = t.timestamp()
            if scheduler:
                c.tick(t)
            c.history_tick(t)
            t = t + collector.timedelta(minutes=step_min)

    def check_day_cap_shared(self, fake):
        c = self.hk_unbuilt(fake)
        self.rounds(c, at(16, 30), at(23, 50), scheduler=True)
        days = [call for call in fake.calls if call[0] == "day"]
        self.assertTrue(days)
        self.assertLessEqual(len(days), 3, days)          # 定稿与历史线程合计，不超过当天的自动上限
        self.assertEqual(vq.read(self.conn, self.HK, "day")[1], None)

    def test_first_build_yields_to_finalize_on_empty_vendor(self):
        self.check_day_cap_shared(EmptyVendor(self.clock))

    def test_first_build_yields_to_finalize_on_truncated_vendor(self):
        self.check_day_cap_shared(TruncatedVendor())

    def test_first_build_yields_to_finalize_when_day_ok_but_m30_fails(self):
        fake = HKRawFake()
        fake.fail_freqs = {"m30"}
        self.check_day_cap_shared(fake)

    def test_read_catch_up_after_exhaustion_does_not_request_today_cache(self):
        # 第三阶段第三轮复审阻断 1：定稿 3 次用完后，读取追赶续传旧缺口有进展时刷新缓存，不能再请求覆盖当天的缓存
        fake = EmptyVendor(self.clock)
        c = self.hk_unbuilt(fake)
        self.rounds(c, at(16, 30), at(20, 50), scheduler=True)
        with c.writer() as conn:
            c._record_gap_safely(conn, self.HK, "day", "2026-09-24", "2026-09-24", "finalize")
        self.clock.t = at(21, 0).timestamp()
        out = c.catch_up(self.HK, at(21, 0))
        self.assertTrue(out.get("drain", {}).get("done"), out)
        today = [call for call in fake.calls if call[0] == "day" and call[2][:10] >= DAY]
        self.assertLessEqual(len(today), 3, fake.calls)

    def test_history_first_build_is_capped_per_day(self):
        # 有意改写（第三阶段第二轮复审阻断 1）：非交易日（周六）的首建自有日预算；交易日过了定稿时点改由定稿负责
        fake = EmptyVendor(self.clock)
        with facts.write_txn(self.conn):                     # 规划视为已完成：只看缓存首建
            for kind in ("backfill", "minute"):
                facts.set_setting(self.conn, collector.plan_key(self.HK, kind), DAY)
        c = self.make(fake, watch=(self.HK,), source="longbridge")
        self.rounds(c, at(10, 0, day=26), at(12, 30, day=26))
        c = self.make(fake, watch=(self.HK,), source="longbridge")          # 重启不清零
        self.rounds(c, at(12, 40, day=26), at(23, 50, day=26))
        self.assertEqual(sorted(set(fake.times)), ["10:00", "12:00", "14:00"])


class CnProductionScheduleTests(Base):
    """生产的 A 股日程（所有者 2026-09-30）：20:00 起，间隔仍 2 小时，当天只有两次机会，第三次会落到次日。"""
    real_schedule = True

    def test_cn_day_tries_at_20_00_and_22_00_then_leaves_the_rest_to_next_day(self):
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        c = self.make(provider)
        self.ticks(c, at(15, 1), at(23, 59))
        self.assertEqual([t for code, t in provider.times if code == CODE], ["20:00", "22:00"])

    def test_after_two_failures_restart_past_midnight_makes_no_third_try_and_history_recovers(self):
        # 回归（写在实现之后，不是 RED；Astra 2026-09-30 追加变更评审建议）：两次失败后重启、跨过午夜，调度线程
        # 不对前一交易日发第三次定稿；次日历史线程补齐后该日可读 final
        self.known_calendar()
        provider = Timed(self.clock, fail_today=True)
        self.ticks(self.make(provider), at(15, 1), at(23, 59))
        c = self.make(provider)                                                  # 重启
        self.ticks(c, at(0, 0, day=29), at(1, 30, day=29))
        self.assertEqual([t for code, t in provider.times if code == CODE], ["20:00", "22:00"])
        self.assertFalse(self.final_today())
        self.assertIn(("day", "finalize"), self.gaps())                          # 遗留缺口交次日历史追赶
        provider.fail_today = False
        for minute in range(0, 30, 5):
            when = at(3, minute, day=29)
            self.clock.t = when.timestamp()
            c.history_tick(when)
        self.assertTrue(self.final_today())
        self.assertNotIn(("day", "finalize"), self.gaps())


if __name__ == "__main__":
    unittest.main()
