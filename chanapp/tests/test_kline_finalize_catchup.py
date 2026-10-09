"""请求路径限时同步追赶（计划 2026-09-29 决策 D4，失败模式 F16；另含 F9 多入口、F10 请求路径两例）。

门面接真实事实库（临时目录）与注入假 provider 的采集器实例，不联网；视图读取用真实时钟，
采集器用注入时钟：断言只看事实库、请求记录与返回体里的 bar 日期。"""
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from chanapp.engine import data as engine_data
from chanapp.engine.kline import collector, config, facts
from chanapp.engine.kline.providers.raw import ProviderError
from chanapp.tests import cache_support
from chanapp.tests.test_kline_collector import (DAY_VOL, FACT, Clock, FullFake, _list_since, _pin_mechanism_schedule,
                                                _weekdays)

DAY = "2026-09-28"                                   # 周一
CODE = "sh600036"


def at(hh, mm, ss=0, day=28):
    return datetime(2026, 9, day, hh, mm, ss)


class Recording(FullFake):
    """fail_today：当日日线抛错；block：当日日线在 release 前阻塞（可控阻塞点）；fail_history：历史日线抛错。"""

    def __init__(self):
        super().__init__()
        self.fail_today = self.fail_history = False
        self.block = None
        self.entered = threading.Event()

    def day_history(self, code, start, end):
        if start == end == DAY:
            if self.block is not None:
                self.entered.set()
                self.block.wait(10)
            if self.fail_today:
                self.calls.append(("day", code, start, end))
                raise ProviderError("503")
        elif self.fail_history:
            self.calls.append(("day", code, start, end))
            raise ProviderError("503")
        return super().day_history(code, start, end)


class CatchUpBase(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "1")
        _pin_mechanism_schedule(self)
        tmp = cache_support.temp_dir(self)
        cache_support.isolate_cache_dir(self, tmp)
        self.dir = Path(tmp)
        self.clock = Clock(at(20, 0).timestamp())
        self.provider = Recording()
        self.c = collector.Collector(self.dir, providers={"mairui": self.provider}, clock=self.clock,
                                     watchlist_fn=lambda: [])
        patcher = mock.patch.object(engine_data, "_collector", lambda watchlist_fn=None: self.c)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.join_catchups)
        _list_since(self.dir, [CODE, "sh000001"], "2026-09-01")
        self.conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(self.conn.close)
        with facts.write_txn(self.conn):
            for d in _weekdays("2026-09-01", "2026-10-30"):
                self.conn.execute("INSERT OR REPLACE INTO calendar(market, date, is_open, sessions, source,"
                                  " fetched_at) VALUES ('CN', ?, 1, '[]', 'test', ?)", (d, facts.now_iso()))

    def join_catchups(self):
        if self.provider.block is not None:
            self.provider.block.set()
        for event in list(getattr(self.c, "_catchups", {}).values()):
            event.wait(5)

    def history(self, through, *, code=CODE):
        """through（含）之前的交易日事实齐全，历史规划与发现水位都已推进到 through。"""
        days = [d for d in _weekdays("2026-09-01", through)]
        fake = FullFake()
        facts.commit_day_rows(self.conn, fake.day_history(code, days[0], through), market="CN", kind="stock",
                              item="day_history", source="mairui", binding_gen=1, today=DAY)
        facts.commit_minute_rows(self.conn, fake.minute_history(code, FACT, f"{days[0]} 09:30", f"{through} 15:00",
                                                                now=None),
                                 market="CN", kind="stock", item="minute_history", fact_freq=FACT, source="mairui",
                                 binding_gen=1, today=DAY)
        with facts.write_txn(self.conn):
            for c in (code, "sh000001"):
                facts.set_setting(self.conn, collector.plan_key(c, "backfill"), "2026-09-01")
                facts.set_setting(self.conn, collector.plan_key(c, "minute"), "2026-09-01")
            for dataset in ("day", FACT):
                facts.set_setting(self.conn, f"discovered:{dataset}:{code}", through)

    def complete_today(self):
        fake = FullFake()
        facts.commit_day_rows(self.conn, fake.day_history(CODE, DAY, DAY), market="CN", kind="stock",
                              item="day_history", source="mairui", binding_gen=1, today=DAY)
        facts.commit_minute_rows(self.conn, fake.minute_history(CODE, FACT, f"{DAY} 09:30", f"{DAY} 15:00", now=None),
                                 market="CN", kind="stock", item="minute_history", fact_freq=FACT, source="mairui",
                                 binding_gen=1, today=DAY)

    def set_now(self, when):
        self.clock.t = when.timestamp()

    def today_calls(self):
        # 只数本代码：调度线程另为不在自选的上证指数定稿当日日线（系统依赖，第二阶段评审修复）
        return [c for c in self.provider.calls if c[1] == CODE and DAY in (c[2][:10], c[3][:10])]

    def final(self, day=DAY):
        return any(r["trade_date"] == day and r["provenance"] == "final" for r in facts.read_day_rows(self.conn, CODE))


class LaggingTodayTests(CatchUpBase):
    def test_get_bars_catches_up_todays_finalize(self):
        self.history("2026-09-25")
        out = engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertTrue(self.final())
        self.assertEqual(out["bars"][-1]["dt"][:10], DAY)

    def test_bundle_catches_up_todays_finalize(self):
        self.history("2026-09-25")
        bundle = engine_data.get_bars_bundle(CODE, ("day", "m60", "m30"), adjust="raw", primary="day")
        self.assertTrue(self.final())
        self.assertEqual(bundle["day"]["bars"][-1]["dt"][:10], DAY)

    def test_up_to_date_makes_no_request(self):
        self.history("2026-09-25")
        self.complete_today()
        engine_data.get_bars(CODE, "day", adjust="raw")
        engine_data.get_bars_bundle(CODE, ("day", "m60", "m30"), adjust="raw")
        self.assertEqual(self.provider.calls, [])

    def test_disabled_collector_never_catches_up(self):
        self.history("2026-09-25")
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        engine_data.get_bars(CODE, "day", adjust="raw")
        engine_data.get_bars_bundle(CODE, ("day", "m30"), adjust="raw")
        self.assertEqual(self.provider.calls, [])

    def test_catch_up_failure_still_serves_view(self):
        self.history("2026-09-25")
        self.provider.fail_today = True
        out = engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertEqual(out["bars"][-1]["dt"][:10], "2026-09-25")
        # 当天日线定稿请求失败恰好 1 次；窗口追赶另补今天已收盘的分钟槽（20:00 全天已收盘，分钟历史可得）
        self.assertEqual([c for c in self.today_calls() if c[0] == "day"], [("day", CODE, DAY, DAY)])
        self.assertEqual([(c[2], c[3]) for c in self.today_calls() if c[0] == FACT],
                         [("2026-09-28 09:30", "2026-09-28 15:00")])
        with mock.patch.object(self.c, "catch_up", side_effect=RuntimeError("boom")):
            self.set_now(at(20, 10))
            self.assertTrue(engine_data.get_bars(CODE, "day", adjust="raw")["bars"])

    def test_scheduler_failure_and_request_path_share_retry_throttle(self):
        # F9 多入口：调度线程 17:30 失败后，17:31 的读取不再重复请求当日数据
        self.history("2026-09-25")
        self.c.watchlist_fn = lambda: [CODE]
        self.provider.fail_today = True
        self.set_now(at(17, 30))
        self.c.tick(at(17, 30))
        self.assertEqual(len(self.today_calls()), 1)
        self.set_now(at(17, 31))
        engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertEqual(len(self.today_calls()), 1)
        self.set_now(at(19, 30))
        engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertEqual(len(self.today_calls()), 2)


class BoundedWaitTests(CatchUpBase):
    def test_wait_is_capped_and_background_commit_continues(self):
        self.history("2026-09-25")
        self.assertLessEqual(config.CATCHUP_WAIT_S, 15)
        self.provider.block = threading.Event()
        with mock.patch.object(config, "CATCHUP_WAIT_S", 0.2):
            t0 = time.monotonic()
            out = engine_data.get_bars(CODE, "day", adjust="raw")
            self.assertLess(time.monotonic() - t0, 3)
        self.assertTrue(self.provider.entered.is_set())
        self.assertEqual(out["bars"][-1]["dt"][:10], "2026-09-25")      # 超时：返回现有视图
        self.assertFalse(self.final())
        self.provider.block.set()
        self.join_catchups()
        self.assertTrue(self.final())                                    # 后台继续提交

    def test_second_request_does_not_wait_for_catch_up_in_flight(self):
        self.history("2026-09-25")
        self.provider.block = threading.Event()
        with mock.patch.object(config, "CATCHUP_WAIT_S", 0.2):
            engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertTrue(self.provider.entered.is_set())
        with mock.patch.object(config, "CATCHUP_WAIT_S", 10):
            t0 = time.monotonic()
            engine_data.get_bars_bundle(CODE, ("day", "m30"), adjust="raw")
            self.assertLess(time.monotonic() - t0, 3)
        self.provider.block.set()
        self.join_catchups()
        self.assertEqual(len([c for c in self.today_calls() if c[0] == "day"]), 1)


class LaggingHistoryTests(CatchUpBase):
    def test_reopen_during_session_fills_missing_days_and_today_slots(self):
        # 几天前打开过的代码再次打开（盘中）：补上中间缺的交易日（到昨天），另用分钟历史补今天已收盘的槽——
        # 盘中增量只从打开时刻往后追加，此前已收盘的槽没有别的来源（2026-10-09 沃尔德事件）；当天日线仍只来自定稿
        self.history("2026-09-22")
        self.set_now(at(10, 0))
        engine_data.get_bars(CODE, "day", adjust="raw")
        for day in ("2026-09-23", "2026-09-24", "2026-09-25"):
            self.assertTrue(self.final(day), day)
        self.assertEqual([c for c in self.today_calls() if c[0] == "day"], [])
        # 10:00 整越过 60 秒稳定余量的只有 09:45 槽（10:00 槽刚过边界、接口仍会改）
        self.assertEqual([(c[2], c[3]) for c in self.today_calls() if c[0] == FACT],
                         [("2026-09-28 09:30", "2026-09-28 09:45")])
        today = facts.read_minute_rows(self.conn, CODE, FACT, f"{DAY} 00:00", f"{DAY} 23:59")
        self.assertEqual(sorted(r["slot_end"] for r in today if r["state"] == "closed"),
                         ["2026-09-28 09:45"])

    def test_after_close_catches_up_history_and_today_minutes(self):
        # F10 请求路径：收盘后（15:00–17:30）重开仍不取当天日线（当天日线只来自定稿），
        # 但分钟已全天收盘（m15 实测 15:07 起全槽可取），窗口追赶把今天槽位一并补齐
        self.history("2026-09-24")
        self.set_now(at(16, 0))
        engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertTrue(self.final("2026-09-25"))
        self.assertEqual([c for c in self.today_calls() if c[0] == "day"], [])
        self.assertEqual([(c[2], c[3]) for c in self.today_calls() if c[0] == FACT],
                         [("2026-09-28 09:30", "2026-09-28 15:00")])

    def test_catch_up_is_throttled_per_code(self):
        self.history("2026-09-22")
        self.provider.fail_history = True
        self.set_now(at(10, 0))
        engine_data.get_bars(CODE, "day", adjust="raw")
        first = len(self.provider.calls)
        self.assertGreater(first, 0)
        self.set_now(at(10, 0, 30))
        engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertEqual(len(self.provider.calls), first)                  # 60 秒内不再追赶
        self.c.code_backoff.until.clear()
        self.set_now(at(10, 1, 1))
        engine_data.get_bars(CODE, "day", adjust="raw")
        self.join_catchups()
        self.assertGreater(len(self.provider.calls), first)


if __name__ == "__main__":
    unittest.main()
