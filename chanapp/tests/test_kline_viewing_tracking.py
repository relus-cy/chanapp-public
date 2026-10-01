"""搜索查看与自选跟踪分离（目标 2026-09-29-kline-viewing-tracking-goal 第二阶段）：采集器侧。

实现前列出的失败方式（每条对应下方用例；API 与页面侧见 test_views_api.py 与 test_viewing_ui.js）：
- V1 非自选首开：登记三年分钟缺口、标记已规划，或日线取满 10 年（窗口之外）；
- V2 非自选首开周线：日线超出 10 年回填上限，或顺带取分钟；
- V3 查看中的非自选被历史线程规划（10 年日线、三年分钟）；
- V4 加入自选后不补完整历史、重复规划，或已有窗口被重复登记；
- V5 移出自选（或从未跟踪的遗留缺口）仍被活跃续传或收盘后全库续传处理；
- V6 离开页面后非自选仍长期盘中取数；
- V7 非自选进入调度线程定稿；
- V8 上证指数不在自选时失去系统依赖（日线历史），或因此被规划三年分钟、被盘中取数；
- V9 再次查看跨日的非自选：请求路径追赶不补窗口内缺的日子，或越出窗口、请求无上限；
- V10 窗口按全库行数判断够不够：旧行冒充近期窗口；
- V11 手动重拉：窗口已覆盖就不重取、失败时删旧数据，或把代码升级成跟踪。
- V12 窗口分钟缺口被周末/节假日切成按周的碎段，请求数成倍增加（端到端请求记录发现）；
- V13 非自选港股首开：供应商前复权日线缓存借「回填目标」一次拉到港股既有起点，越出查看窗口。
阶段评审（astra，NO-GO）回放出的反例，修复前先写成下列用例：
- V14 移出自选发生在同一轮前一代码的请求期间：被移出的代码仍被整段规划（历史线程按轮快照）；
- V15 请求路径追赶续传中途移出：余下缺口仍续传；V16 定稿轮中途移出：排在后面的代码仍定稿；
- V17 上证指数不在自选：查看分钟周期被登记三年分钟与规划标记，遗留分钟缺口被活跃或全库续传；
- V18 上证指数不在自选：收盘后没有当日 final 日线（年表缺当天时 A 股前复权断链），调度线程应只为它定稿日线；
  定稿前须先登记它停机期间缺的日子，否则当天 final 让日历推导把缺的日子当休市（见 test_kline_collector 停机用例）；
- V19 窗口把本地首行当历史下限：港股先开日线再开周线零请求；
- V20 分钟窗口下限按补日线之前的首根日线算：本地只有昨天一根日线时分钟远不够；
- V21 重拉只按 m60 窗口：丢主图周期（周线）、定稿时点后不重取当天、港股缓存只核对近 30 天；
- V22 重拉空返回或失败仍报成功，结果不区分成功、部分、失败；
- V23 请求路径的窗口追赶与重拉同时对同一代码请求。
阶段复审（astra，NO-GO）补充：
- V24 同一代码自己的日线请求期间被移出：首取仍接着取三年分钟规划、定稿仍接着取分钟；
- V25 重拉借旧库冒充成功：上游只返回末尾一行、或当天返回空而旧当天已完整，仍报 ok；
- V26 交易日定稿时点后，请求路径追赶在锁外先补当天，与在途重拉重复请求当天。
阶段第三轮复审（astra，NO-GO）补充：
- V27 港股重拉时供应商前复权 day/m30 只返回末尾一行：截短序列被发布、缓存退化，重拉仍报 ok；
- V28 某个开市日的日历行缺失、旧事实仍在：本次漏返回该日被旧库掩盖，重拉报 ok；
- V29 整段（或整月）停牌：合法停牌日线、分钟为空，被算作空返回失败，重拉报 partial；
- V30 重拉与追赶互斥：等待在途追赶超时后仍直接取数；两个重拉并发时先结束者清掉后者的占用（或异常退出不清）；
- V31 港股定稿在分钟请求期间移出自选：仍刷新供应商缓存；缓存刷新在日线请求期间移出仍取 m30；
- V32 查看租期在一轮中途到期：本轮排在后面的非自选代码仍开始盘中增量与盘前补确认；
- V33 V28 的修法把所有日历未知的工作日都当应有：港股过去年份的假日（年表只取当年、raw 只推开市日）让港股重拉永远不是 ok；
  窗口判断同样把这类假日当缺：非自选港股窗口永远不完整、每次打开都判落后（修前已存在，第三轮修复中发现）；
- V34 整段重取的截止日早于已发布缓存末端（续传旧缺口时按缺口日刷新）：新版本丢掉截止日之后已发布的 bar。
阶段第四轮复审（astra，NO-GO）补充：
- V35 供应商 m30 覆盖只按日期检查：一天里缺一个槽位的候选被发布、定稿报追平（追加与整段替换两种）；
- V36 window_absent 只看日线：某日日线漏返回、但分钟与供应商缓存都有该日，被永久豁免、重拉报 ok；
  豁免记下后该日又有了分钟证据，仍被豁免。
阶段第五轮复审（astra，NO-GO）补充：
- V37 首根日线之前的「无数据」水位（day_absent_from）盖过开市证据：已有分钟的日子日线缺失，重拉报 ok、不判落后；
- V38 窗口内有待核验（本次冲突或既有未裁决冲突），重拉仍报 ok；
- V39 盘中返回全被准入拒收，当天部分仍报 ok；
- V40 raw 槽位已隔离，供应商候选不含该槽被误拒、缓存长期 stale。
阶段第六轮复审（astra，NO-GO）补充：
- V41 盘中重拉返回的当天行落在已隔离的槽位上（全部或部分），当天没有可读的新行，仍报 ok；
- V42 分钟待核验在分钟窗口之外（只在更长的日线窗口里），仍让重拉不是 ok；
- V43 港股供应商历史短于窗口：m60 与 m30 的扩窗起点互相覆盖「已试」记录，每次打开都重复整段重取缓存。
「慢历史不阻塞另一标的盘中」已由 test_kline_history_thread.DecisiveTwoCodeTests 覆盖。
"""
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import datetime

from chanapp.engine.kline import collector, config, facts, hk_vendor_qfq, sessions
from chanapp.engine.kline.providers.raw import ProviderError
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests.test_kline_collector import (DAY_VOL, FACT, Clock, FullFake, HKFullFake, _enable_collector, _list_since,
                                                _pin_mechanism_schedule)

PER_HOUR = 60 // sessions.FREQ_MINUTES[FACT]       # 每根 m60 所需的分钟事实行数
from pathlib import Path

A, X, INDEX = "sh600036", "sz000002", "sh000001"


def at(day, hh, mm=0, ss=0):
    return datetime.fromisoformat(f"{day} {hh:02d}:{mm:02d}:{ss:02d}")


class Recording(FullFake):
    """FullFake 加盘中接口与日历；fail 为真时历史接口抛错。"""

    def __init__(self):
        super().__init__()
        self.fail = False
        self.empty = False
        self.empty_on = None                               # 结束于该日的请求返回空（模拟当天空返回）
        self.tail = False                                  # 每个请求只返回末尾一行
        self.hook = None                                   # 每次历史请求前回调（模拟请求期间用户操作）

    def day_history(self, code, start, end):
        if self.hook:
            self.hook(("day", code, start, end))
        if self.empty or end[:10] == self.empty_on:
            self.calls.append(("day", code, start, end))
            return []
        if self.fail:
            self.calls.append(("day", code, start, end))
            raise ProviderError("down")
        rows = super().day_history(code, start, end)
        return rows[-1:] if self.tail else rows

    def minute_history(self, code, fact_freq, start, end, *, now):
        if self.hook:
            self.hook((fact_freq, code, start, end))
        if self.empty or end[:10] == self.empty_on:
            self.calls.append((fact_freq, code, start, end))
            return []
        if self.fail:
            self.calls.append((fact_freq, code, start, end))
            raise ProviderError("down")
        rows = super().minute_history(code, fact_freq, start, end, now=now)
        return rows[-1:] if self.tail else rows

    def minute_live(self, code, fact_freq, *, now):
        self.calls.append(("live", code))
        day = now.date().isoformat()
        return [RawMinuteRow(code, day, f"{day} 10:15", 10, 10, 10, 10, 1, "lot", 1, "forming", "traded",
                             new_batch_id())]

    def preopen_ref(self, code, trade_date):
        return None

    def calendar(self, year):
        return []


class Base(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        _pin_mechanism_schedule(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = Clock(at("2026-09-26", 20).timestamp())          # 周六
        _list_since(self.dir, [A, X, INDEX], "2010-01-04")
        self.conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(self.conn.close)
        self.provider = Recording()
        self.watch = [A]

    def make(self):
        return collector.Collector(self.dir, providers={"mairui": self.provider}, clock=self.clock,
                                   watchlist_fn=lambda: list(self.watch))

    def calls(self, code, kind=None):
        return [c for c in self.provider.calls if c[1] == code and (kind is None or c[0] == kind)]

    def planned(self, code):
        return {k for k in ("backfill", "minute") if facts.setting(self.conn, collector.plan_key(code, k))}

    def closed_minutes(self, code):
        return [r for r in facts.read_minute_rows(self.conn, code, FACT) if r["state"] == "closed"]

    def set_time(self, when):
        self.clock.t = when.timestamp()
        return when


class WindowFirstOpenTests(Base):
    def test_untracked_first_open_fetches_only_the_recent_window(self):   # V1
        c = self.make()
        self.assertTrue(c.ensure_window(X, "m60", bars=40))
        day_starts = [c[2] for c in self.calls(X, "day")]
        self.assertTrue(day_starts)
        self.assertTrue(all(s >= "2026-06-01" for s in day_starts), day_starts)   # 40 个交易日左右，不是 10 年
        self.assertEqual(facts.open_gaps(self.conn, X), [])                     # 不登记三年分钟缺口
        self.assertEqual(self.planned(X), set())
        rows = self.closed_minutes(X)
        self.assertGreaterEqual(len(rows), 40 * PER_HOUR)
        self.assertGreaterEqual(min(r["trade_date"] for r in rows), "2026-06-01")

    def test_tracked_cold_open_serves_the_window_before_deep_history(self):
        # 终审应修（目标「自选首次打开先服务近期窗口，再异步补完整历史」）：新自选的首开同步只取窗口，
        # 十年日线整段回填与分钟规划交历史线程
        c = self.make()
        self.assertTrue(c.ensure_window(A, "m60", bars=40))
        day_starts = [call[2] for call in self.calls(A, "day")]
        self.assertTrue(day_starts)
        self.assertTrue(all(s >= "2026-06-01" for s in day_starts), day_starts)
        self.assertGreaterEqual(len(self.closed_minutes(A)), 40 * PER_HOUR)
        c.history_tick(self.set_time(at("2026-09-26", 20, 1)))
        self.assertIn("2016-09-26", [call[2] for call in self.calls(A, "day")], "深历史由历史线程补")
        self.assertIn("backfill", self.planned(A))

    def test_failed_listing_lookup_does_not_block_the_window_fetch(self):
        # 第四阶段占位回放发现：上市日查询（instrument）两次失败就触发单标的退避，紧接着的窗口取数被跳过、图表 502。
        # 元数据查询失败只让上市日按未知处理，不挡数据请求
        Z = "sz000004"                                          # 本地没有上市日
        lookups = []

        def instrument(code):
            lookups.append(code)
            raise ProviderError("HTTP 404 /hsstock/instrument")
        self.provider.instrument = instrument
        self.assertTrue(self.make().ensure_window(Z, "m60", bars=40))
        self.assertTrue(lookups)
        self.assertTrue(self.calls(Z, "day"))
        self.assertTrue(self.calls(Z, FACT))

    def test_untracked_week_open_caps_daily_at_backfill_years_without_minutes(self):   # V2
        c = self.make()
        c.ensure_window(X, "week", bars=600)                   # 600 周需要约 11.5 年日线
        starts = [c[2] for c in self.calls(X, "day")]
        self.assertEqual(min(starts), "2016-09-26")             # 截到 DAY_BACKFILL_YEARS
        self.assertEqual(self.calls(X, FACT), [])
        self.assertEqual(self.planned(X), set())

    def test_window_minutes_are_requested_by_month_not_split_by_weekends(self):   # V12
        self.make().ensure_window(X, "m60", bars=40)
        m5 = self.calls(X, FACT)
        self.assertTrue(m5)
        self.assertEqual(len(m5), len({call[2][:7] for call in m5}), m5)   # 每个自然月一次

    def test_old_rows_do_not_count_toward_the_recent_minute_window(self):   # V10
        old = FullFake()
        facts.commit_minute_rows(self.conn, old.minute_history(X, FACT, "2026-03-02 09:30", "2026-04-30 15:00",
                                                               now=None),
                                 market="CN", kind="stock", item="minute_history", fact_freq=FACT, source="mairui",
                                 binding_gen=1, today="2026-04-30")
        self.assertGreater(len(self.closed_minutes(X)), 20 * PER_HOUR)
        self.make().ensure_window(X, "m60", bars=20)
        self.assertTrue([c for c in self.calls(X, FACT) if c[3] >= "2026-09-25"])
        recent = [r for r in self.closed_minutes(X) if r["trade_date"] >= "2026-08-01"]
        self.assertGreaterEqual(len(recent), 20 * PER_HOUR)


class HkBase(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fake = HKFullFake()
        self.watch = []

    def make(self):
        return collector.Collector(self.dir, providers={"longbridge": self.fake},
                                   clock=Clock(at("2026-09-26", 20).timestamp()), watchlist_fn=lambda: list(self.watch))


class HkWindowTests(HkBase):
    def test_untracked_hk_first_open_keeps_vendor_cache_within_window(self):   # V13
        self.make().ensure_window("hk00700", "day")
        vendor_day = [call for call in self.fake.calls if call[0] == "day"]
        self.assertTrue(vendor_day)
        self.assertTrue(all(call[1] >= "2024-06-01" for call in vendor_day), vendor_day)   # 约两年窗口，不是回填目标

    def test_tracked_hk_vendor_day_cache_follows_backfill_target_once_planned(self):   # V13 对照
        # 终审复核后改写：原用例是「自选、还没整段回填」首开就按回填目标建缓存，与「自选首开先服务近期窗口」相反；
        # 回填目标只对已做过整段回填的自选成立
        self.watch.append("hk00700")
        c = self.make()
        with facts.write_txn(c.conn()):
            facts.set_setting(c.conn(), collector.plan_key("hk00700", "backfill"), "2026-09-26")
        c.ensure_window("hk00700", "day")
        vendor_day = [call for call in self.fake.calls if call[0] == "day"]
        self.assertTrue(any(call[1] < "2024" for call in vendor_day), vendor_day)   # 港股回填目标（既有起点）

    def test_tracked_hk_cold_open_builds_vendor_window_then_history_extends(self):
        # 终审复核应修：刚加入、还没整段回填的港股自选首开，前复权缓存与 raw 一样只取窗口；更早的由历史线程补
        self.watch.append("hk00700")
        c = self.make()
        c.ensure_window("hk00700", "m30", bars=520)
        vendor_day = [call for call in self.fake.calls if call[0] == "day"]
        self.assertTrue(vendor_day)
        self.assertTrue(all(call[1] >= "2024-06-01" for call in vendor_day), vendor_day)
        for minute in range(1, 6):
            c.clock.t = at("2026-09-26", 20, minute).timestamp()
            c.history_tick(at("2026-09-26", 20, minute))
        bars, meta = hk_vendor_qfq.read(c.conn(), "hk00700", "day")
        self.assertIsNotNone(meta)
        self.assertLess(bars[0]["dt"][:10], "2024", "历史线程把缓存扩到回填目标")


class ShortMinuteHistory(HKFullFake):
    """港股 raw 分钟接口只给 cutoff 起的历史（设为首开窗口起点）：分钟缓存无从前扩，日线回填照常。"""
    cutoff = ""

    def minute_history(self, code, fact_freq, start, end, *, now):
        return [r for r in super().minute_history(code, fact_freq, start, end, now=now) if r.trade_date >= self.cutoff]


class HkDayExtensionTests(HkBase):
    def test_vendor_day_cache_extends_after_day_backfill_even_if_minutes_cannot(self):
        # 第四阶段复核应修：首开只建窗口后，日线整段回填完成就把前复权日线缓存扩到回填目标，不依赖分钟缓存能否前扩
        self.fake = ShortMinuteHistory()
        self.watch.append("hk00700")
        c = self.make()
        c.ensure_window("hk00700", "m30", bars=520)
        self.fake.cutoff = c._raw_first("hk00700", "m30")
        for minute in range(1, 8):
            c.clock.t = at("2026-09-26", 20, minute).timestamp()
            c.history_tick(at("2026-09-26", 20, minute))
        self.assertLess(c._raw_first("hk00700", "day"), "2024", "日线已回填")
        bars, _ = hk_vendor_qfq.read(c.conn(), "hk00700", "day")
        self.assertLess(bars[0]["dt"][:10], "2024", "前复权日线缓存随日线回填扩展")
        seen = len(self.fake.calls)
        for minute in range(8, 11):                                  # 同一起点只试一次，不每轮整段重取
            c.clock.t = at("2026-09-26", 20, minute).timestamp()
            c.history_tick(at("2026-09-26", 20, minute))
        self.assertFalse([call for call in self.fake.calls[seen:] if call[0] == "day"], self.fake.calls[seen:])


    def test_extension_that_sent_requests_but_failed_to_publish_is_not_retried_every_round(self):
        # 补修复核应修：已发出请求、发布时出错（写库失败等）也算试过同一起点，历史线程不每轮整段重取
        self.fake = ShortMinuteHistory()
        self.watch.append("hk00700")
        c = self.make()
        c.ensure_window("hk00700", "m30", bars=520)
        self.fake.cutoff = c._raw_first("hk00700", "m30")
        with unittest.mock.patch.object(hk_vendor_qfq, "publish_set", side_effect=facts.FactsWriteError("disk full")), \
                self.assertLogs(collector.log, level="WARNING"):
            for minute in range(1, 10):
                c.clock.t = at("2026-09-26", 20, minute).timestamp()
                c.history_tick(at("2026-09-26", 20, minute))
        deep = [call for call in self.fake.calls if call[0] == "day" and call[1] < "2024"]
        self.assertEqual(len(deep), 1, deep)


class TrackingLifecycleTests(Base):
    def test_viewed_untracked_code_is_not_planned_by_history_thread(self):   # V3
        c = self.make()
        c.touch_viewing(X)
        c.history_tick(self.set_time(at("2026-09-26", 20)))
        self.assertEqual(self.calls(X), [])
        self.assertEqual(self.planned(X), set())
        self.assertTrue(self.planned(A))

    def test_library_drain_after_close_skips_untracked_codes(self):   # V5
        with facts.write_txn(self.conn):
            for code in (A, X):
                facts.record_gap(self.conn, code, FACT, "2026-08-03 09:30", "2026-08-31 15:00", "backfill")
            for code in (A, INDEX):
                for kind in ("backfill", "minute"):
                    facts.set_setting(self.conn, collector.plan_key(code, kind), "2026-09-26")
                for ds in ("day", FACT):
                    facts.set_setting(self.conn, f"discovered:{ds}:{code}", "2026-09-25")
        c = self.make()
        for minute in range(3):
            c.history_tick(self.set_time(at("2026-09-26", 20, minute)))
        self.assertTrue(self.calls(A, FACT))
        self.assertEqual(self.calls(X), [])
        self.assertTrue(facts.open_gaps(self.conn, X))          # 数据与缺口记录保留，不清理

    def test_adding_to_watchlist_plans_full_history_once_reusing_window(self):   # V4
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        window_lo = min(r["trade_date"] for r in self.closed_minutes(X))
        self.watch.append(X)
        c.history_tick(self.set_time(at("2026-09-26", 20, 1)))
        full = [call for call in self.calls(X, "day") if call[2] < "2017"]
        self.assertEqual(len(full), 1)
        self.assertEqual(self.planned(X), {"backfill", "minute"})
        gaps = facts.open_gaps(self.conn, X, FACT)
        self.assertTrue(gaps)                                          # 窗口之前的三年分钟登记待补
        self.assertTrue(all(g["start"][:10] < window_lo for g in gaps), [(g["start"], g["end"]) for g in gaps])
        c.history_tick(self.set_time(at("2026-09-26", 20, 2)))
        self.assertEqual(len([call for call in self.calls(X, "day") if call[2] < "2017"]), 1)

    def test_removed_code_stops_background_history(self):   # V5
        self.watch.append(X)
        c = self.make()
        c.history_tick(self.set_time(at("2026-09-26", 20)))
        self.assertTrue(facts.open_gaps(self.conn, X, FACT))
        self.watch.remove(X)
        before = len(self.calls(X))
        for minute in range(1, 4):
            c.history_tick(self.set_time(at("2026-09-26", 20, minute)))
        self.assertEqual(len(self.calls(X)), before)

    def test_leaving_the_page_stops_intraday_fetch_for_untracked(self):   # V6
        c = self.make()
        now = self.set_time(at("2026-09-28", 10))
        c.touch_viewing(X)
        c.tick(now)
        self.assertTrue(self.calls(X, "live"))
        seen = len(self.calls(X, "live"))
        c.tick(self.set_time(at("2026-09-28", 10, 2, 40)))          # 160 秒没有页面请求
        c.tick(self.set_time(at("2026-09-28", 10, 4)))
        self.assertEqual(len(self.calls(X, "live")), seen)
        self.assertGreater(len(self.calls(A, "live")), 1)             # 自选照常

    def test_scheduler_finalizes_watchlist_codes_only(self):   # V7
        c = self.make()
        now = self.set_time(at("2026-09-28", 17, 31))
        c.touch_viewing(X)
        c.tick(now)
        finalized = {call[1] for call in self.provider.calls if call[0] == "day" and call[2] == "2026-09-28"}
        self.assertEqual(finalized, {A, INDEX})                   # 上证指数是系统依赖的日线定稿（V18），不是查看的 X

    def test_system_index_gets_daily_history_without_minute_plan_when_not_watched(self):   # V8
        c = self.make()
        c.history_tick(self.set_time(at("2026-09-26", 20)))
        self.assertTrue([call for call in self.calls(INDEX, "day") if call[2] < "2017"])
        self.assertEqual(self.planned(INDEX), {"backfill"})
        self.assertEqual(facts.open_gaps(self.conn, INDEX, FACT), [])
        self.assertEqual(self.calls(INDEX, FACT), [])

    def test_system_index_is_not_live_fetched_unless_watched_or_viewed(self):   # V8
        c = self.make()
        c.tick(self.set_time(at("2026-09-28", 10)))
        self.assertEqual(self.calls(INDEX, "live"), [])
        self.assertTrue(self.calls(A, "live"))


class WindowCatchUpTests(Base):
    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(config, "DEFAULT_WINDOW", 40)   # 请求路径追赶按默认窗口
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reopening_untracked_after_days_fills_only_the_window(self):   # V9
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        self.provider.calls.clear()
        now = self.set_time(at("2026-10-03", 20))                       # 一周后的周六
        self.assertTrue(c.lagging(X, now))
        c.catch_up(X, now)
        self.assertLessEqual(len(self.provider.calls), config.CATCHUP_MAX_REQUESTS)
        self.assertTrue(all(call[2] >= "2026-09-28" for call in self.calls(X)), self.calls(X))
        days = {r["trade_date"] for r in self.closed_minutes(X)}
        self.assertTrue({"2026-09-28", "2026-10-02"} <= days)
        finals = {r["trade_date"] for r in facts.read_day_rows(self.conn, X) if r["provenance"] == "final"}
        self.assertIn("2026-10-02", finals)
        self.assertEqual(self.planned(X), set())
        self.provider.calls.clear()
        self.assertFalse(c.lagging(X, now))                             # 补齐后零请求

    def test_up_to_date_untracked_window_is_not_lagging(self):   # V9
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        self.assertFalse(c.lagging(X, c.now()))

    def test_lagging_without_local_list_date_sends_no_request(self):   # V9：只读判断不查上市日
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        with facts.write_txn(self.conn):
            self.conn.execute("DELETE FROM instruments WHERE code=?", (X,))
        asked = []
        self.provider.instrument = lambda code: asked.append(code)
        self.provider.calls.clear()
        c.lagging(X, c.now())
        self.assertEqual((asked, self.provider.calls), ([], []))


class RefetchTests(Base):
    def test_refetch_while_another_thread_holds_the_code_reports_busy_quickly(self):
        # 第四阶段补修复核必须修：历史线程等持有同一代码的单飞锁时，手动重拉不无限等待——等锁超过预算回 busy、不发请求
        c = self.make()
        held, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def holder():
            with c._single_flight(X):
                held.set()
                release.wait(20)
        threading.Thread(target=holder, daemon=True).start()
        self.assertTrue(held.wait(5))
        box = {}
        worker = threading.Thread(target=lambda: box.setdefault("out", c.refetch_window(X, "m60")), daemon=True)
        worker.start()
        worker.join(config.REQUEST_LOCK_WAIT_S + 3)
        alive = worker.is_alive()
        release.set()
        worker.join(10)
        self.assertFalse(alive, "手动重拉在等锁上没有期限")
        self.assertEqual(box["out"]["status"], "busy")
        self.assertEqual(self.calls(X), [])
        self.assertFalse(c.refetching(X))

    def test_refetch_requests_the_window_again_without_tracking(self):   # V11
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        self.provider.calls.clear()
        self.assertEqual(c.refetch_window(X, "m60", bars=40)["status"], "ok")
        self.assertTrue(self.calls(X, "day"))
        self.assertTrue(self.calls(X, FACT))
        self.assertEqual(self.planned(X), set())
        self.assertNotIn(X, c.tracked_codes())

    def test_failed_refetch_keeps_existing_rows(self):   # V11
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        before = len(self.closed_minutes(X)), len(facts.read_day_rows(self.conn, X))
        self.provider.fail = True
        self.assertEqual(c.refetch_window(X, "m60", bars=40)["status"], "failed")
        self.assertEqual((len(self.closed_minutes(X)), len(facts.read_day_rows(self.conn, X))), before)


def _plan_done(conn, code, day="2026-09-26", datasets=("day", FACT)):
    with facts.write_txn(conn):
        for kind in ("backfill", "minute"):
            facts.set_setting(conn, collector.plan_key(code, kind), day)
        for ds in datasets:
            facts.set_setting(conn, f"discovered:{ds}:{code}", "2026-09-25")


class MidRoundEligibilityTests(Base):
    """资格在每次请求前复核，而不是按轮快照（V14–V16）。已发出的请求照常完成，但不开始新任务。"""

    def test_code_removed_during_previous_codes_request_is_not_planned(self):   # V14
        self.watch[:] = [A, X]
        c = self.make()

        def remove_x(call):
            if call[:2] == ("day", A) and X in self.watch:
                self.watch.remove(X)
        self.provider.hook = remove_x
        c.history_tick(self.set_time(at("2026-09-26", 20)))
        self.assertTrue(self.calls(A, "day"))
        self.assertEqual(self.calls(X), [])
        self.assertEqual(self.planned(X), set())

    def test_catch_up_stops_draining_after_removal(self):   # V15
        self.watch[:] = [A, X]
        _plan_done(self.conn, X)
        with facts.write_txn(self.conn):
            for day in ("2026-09-22", "2026-09-15"):
                facts.record_gap(self.conn, X, "day", day, day, "backfill")
        c = self.make()

        def remove_x(call):
            if call[1] == X and X in self.watch:
                self.watch.remove(X)
        self.provider.hook = remove_x
        c.catch_up(X, self.set_time(at("2026-09-26", 20)))
        self.assertEqual(len(self.calls(X)), 1, self.calls(X))

    def test_finalize_round_skips_code_removed_mid_round(self):   # V16
        self.watch[:] = [A, X]
        c = self.make()

        def remove_x(call):
            if call[1] == A and call[2].startswith("2026-09-28") and X in self.watch:
                self.watch.remove(X)
        self.provider.hook = remove_x
        c.tick(self.set_time(at("2026-09-28", 17, 31)))
        self.assertTrue([call for call in self.calls(A) if call[2].startswith("2026-09-28")])
        self.assertEqual([call for call in self.calls(X) if call[2].startswith("2026-09-28")], [])


class SystemIndexRoleTests(Base):
    """上证指数不在自选时只承担日线系统职责（V17、V18）；自选或查看时照常。"""

    def test_viewing_index_minutes_does_not_plan_three_years(self):   # V17
        c = self.make()
        c.ensure_window(INDEX, "m60", bars=40)
        self.assertNotIn("minute", self.planned(INDEX))
        self.assertEqual(facts.open_gaps(self.conn, INDEX, FACT), [])
        self.assertTrue(all(call[2] >= "2026-06-01" for call in self.calls(INDEX, FACT)), self.calls(INDEX, FACT))

    def test_leftover_index_minute_gap_is_not_drained_when_not_watched(self):   # V17
        _plan_done(self.conn, A)
        _plan_done(self.conn, INDEX)
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, INDEX, FACT, "2026-08-03 09:30", "2026-08-31 15:00", "backfill")
        c = self.make()
        for minute in range(3):
            c.history_tick(self.set_time(at("2026-09-26", 20, minute)))
        self.assertEqual(self.calls(INDEX, FACT), [])
        self.assertTrue(facts.open_gaps(self.conn, INDEX, FACT))

    def test_index_gets_daily_finalize_only_when_not_watched(self):   # V18
        c = self.make()
        c.tick(self.set_time(at("2026-09-28", 17, 31)))
        self.assertTrue([call for call in self.calls(INDEX, "day") if call[2] == "2026-09-28"])
        self.assertEqual([call for call in self.calls(INDEX, FACT) if call[2].startswith("2026-09-28")], [])
        finals = {r["trade_date"] for r in facts.read_day_rows(self.conn, INDEX) if r["provenance"] == "final"}
        self.assertIn("2026-09-28", finals)

    def test_watched_index_still_finalizes_minutes(self):   # V18 对照
        self.watch.append(INDEX)
        self.make().tick(self.set_time(at("2026-09-28", 17, 31)))
        self.assertTrue([call for call in self.calls(INDEX, FACT) if call[2].startswith("2026-09-28")])


class WindowFloorTests(Base):
    def test_minute_window_floor_follows_the_filled_daily_window(self):   # V20
        seed = FullFake()
        facts.commit_day_rows(self.conn, seed.day_history(X, "2026-09-25", "2026-09-25"), market="CN",
                              kind="stock", item="day_history", source="mairui", binding_gen=1, today="2026-09-26")
        self.make().ensure_window(X, "m60", bars=40)
        self.assertGreaterEqual(len(self.closed_minutes(X)), 40 * PER_HOUR)

    def test_listed_date_unknown_window_proven_empty_is_not_requested_again(self):   # V19 守护（修前已成立）
        y = "sz301999"                                          # 没有上市日记录：窗口起点早于首根
        provider = self.provider

        class Young(Recording):
            def day_history(self, code, start, end):
                return [r for r in super().day_history(code, max(start, "2026-08-03"), end)] \
                    if not self.calls.append(("day", code, start, end)) else []
        self.provider = Young()
        c = self.make()
        c.ensure_window(y, "day", bars=60)
        first = [call for call in self.provider.calls if call[1] == y and call[0] == "day"]
        self.assertTrue(first)
        self.provider.calls.clear()
        c.ensure_window(y, "day", bars=60)
        self.assertEqual([call for call in self.provider.calls if call[1] == y and call[0] == "day"], [])
        self.provider = provider


class HkWeekWindowTests(HkBase):
    def test_week_after_day_extends_raw_and_vendor_daily_window(self):   # V19
        c = self.make()
        c.ensure_window("hk00700", "day")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        day_first = min(r["trade_date"] for r in facts.read_day_rows(conn, "hk00700"))
        self.fake.calls.clear()
        c.ensure_window("hk00700", "week", bars=200)             # 约 1000 个交易日，远早于日线窗口
        raw_first = min(r["trade_date"] for r in facts.read_day_rows(conn, "hk00700"))
        self.assertLess(raw_first, "2023-06-01", (day_first, raw_first))   # 周线窗口（受港股起点约束），远早于日线窗口
        vendor_day = [call for call in self.fake.calls if call[0] == "day"]
        self.assertTrue(vendor_day and min(call[1] for call in vendor_day) <= raw_first, vendor_day)

    def test_week_widen_deferred_before_any_request_is_retried(self):
        # 第三阶段第六轮复审应修：日线缓存补窗口因退避或额度一个请求都没发出——不算「这个 raw 起点已试过」
        from unittest import mock
        from chanapp.engine.kline import collector as collector_mod
        c = self.make()
        c.ensure_window("hk00700", "day")
        real = c._call

        def call(code, source, fn, **kw):
            if kw.get("capability") == "qfq_series":
                raise collector_mod._BudgetExhausted()
            return real(code, source, fn, **kw)
        with mock.patch.object(c, "_call", side_effect=call):
            c.ensure_window("hk00700", "week", bars=200)
        self.fake.calls.clear()
        c.ensure_window("hk00700", "week", bars=200)
        self.assertTrue([x for x in self.fake.calls if x[0] == "day"], "解除限制后再次打开照常补日线缓存")

    def test_hk_refetch_republishes_the_whole_vendor_window(self):   # V21
        c = self.make()
        c.ensure_window("hk00700", "day")
        self.fake.calls.clear()
        c.refetch_window("hk00700", "day")
        vendor_day = [call for call in self.fake.calls if call[0] == "day"]
        self.assertTrue(vendor_day and min(call[1] for call in vendor_day) < "2025-01-01", vendor_day)


class RefetchContractTests(Base):
    def test_refetch_covers_the_primary_week_window(self):   # V21
        c = self.make()
        c.ensure_window(X, "week", bars=100)
        self.provider.calls.clear()
        c.refetch_window(X, "week", bars=100)
        starts = [call[2] for call in self.calls(X, "day")]
        self.assertTrue(starts and min(starts) < "2025-01-01", starts)

    def test_refetch_after_finalize_time_rerequests_today(self):   # V21
        c = self.make()
        now = self.set_time(at("2026-09-28", 18))
        c.ensure_window(X, "m60", bars=40)
        c.finalize([X], "2026-09-28", now)
        self.provider.calls.clear()
        self.assertEqual(c.refetch_window(X, "m60", bars=40)["status"], "ok")
        self.assertTrue([call for call in self.calls(X, "day") if call[3] == "2026-09-28"], self.calls(X))
        self.assertTrue([call for call in self.calls(X, FACT) if call[3].startswith("2026-09-28")], self.calls(X))

    def test_empty_refetch_is_not_reported_as_success(self):   # V22
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        self.provider.empty = True
        res = c.refetch_window(X, "m60", bars=40)
        self.assertNotEqual(res["status"], "ok", res)

    def test_refetch_that_stops_midway_is_partial(self):   # V22
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        self.provider.hook = lambda call: setattr(self.provider, "fail", call[0] == FACT)
        self.assertEqual(c.refetch_window(X, "m60", bars=40)["status"], "partial")

    def test_window_catch_up_waits_for_inflight_refetch(self):   # V23
        c = self.make()
        gate, entered = threading.Event(), threading.Event()

        def hold(call):
            if call[:2] == ("day", X) and not entered.is_set():
                entered.set()
                gate.wait(5)
        self.provider.hook = hold
        now = c.now()
        t1 = threading.Thread(target=lambda: c.refetch_window(X, "m60", bars=40))
        t1.start()
        self.assertTrue(entered.wait(5))
        t2 = threading.Thread(target=lambda: c.catch_up(X, now))
        t2.start()
        time.sleep(0.3)
        during = len(self.calls(X))
        gate.set()
        t1.join(10)
        t2.join(10)
        self.assertEqual(during, 0)                              # 重拉的首个请求还没记账；追赶没有并行发请求
        m5 = self.calls(X, FACT)
        self.assertEqual(len(m5), len(set(m5)), m5)


class SameCodeRemovalTests(Base):
    """V24：同一代码自己的请求期间被移出，已发请求照常提交，但不再开始该代码的后续跟踪工作。"""

    def test_first_open_removed_during_own_backfill_does_not_plan_minutes(self):
        self.watch[:] = [A, X]
        c = self.make()

        def remove_x(call):
            if call[:2] == ("day", X) and call[2] < "2017" and X in self.watch:
                self.watch.remove(X)
        self.provider.hook = remove_x
        c.ensure_window(X, "m60", bars=40)
        self.assertNotIn("minute", self.planned(X))
        self.assertEqual(facts.open_gaps(self.conn, X, FACT), [])
        self.assertTrue(all(call[2] >= "2026-06-01" for call in self.calls(X, FACT)), self.calls(X, FACT))

    def test_finalize_removed_during_own_day_request_skips_minutes(self):
        self.watch[:] = [A, X]
        c = self.make()

        def remove_x(call):
            if call[:2] == ("day", X) and call[2] == "2026-09-28" and X in self.watch:
                self.watch.remove(X)
        self.provider.hook = remove_x
        c.tick(self.set_time(at("2026-09-28", 17, 31)))
        self.assertTrue([call for call in self.calls(X, "day") if call[2] == "2026-09-28"])
        self.assertEqual([call for call in self.calls(X, FACT) if call[2].startswith("2026-09-28")], [])
        self.assertEqual([g for g in facts.open_gaps(self.conn, X) if g["reason"] == "finalize"], [])


class RefetchTruthTests(Base):
    """V25：重拉结果按本次返回判断，旧库完整不能冒充本次成功。"""

    def test_refetch_that_returns_only_tail_rows_is_not_ok(self):
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        self.provider.tail = True
        self.assertNotEqual(c.refetch_window(X, "m60", bars=40)["status"], "ok")

    def test_refetch_with_empty_today_is_not_ok_even_if_old_today_is_complete(self):
        c = self.make()
        now = self.set_time(at("2026-09-28", 18))
        c.ensure_window(X, "m60", bars=40)
        c.finalize([X], "2026-09-28", now)
        self.provider.empty_on = "2026-09-28"
        res = c.refetch_window(X, "m60", bars=40)
        self.assertNotEqual(res["today"], "ok", res)
        self.assertEqual(res["status"], "partial", res)


class RefetchCatchUpTradingDayTests(Base):
    def test_catch_up_after_finalize_time_waits_for_inflight_refetch(self):   # V26
        now = self.set_time(at("2026-09-28", 18))
        c = self.make()
        gate, entered = threading.Event(), threading.Event()

        def hold(call):
            if call[:2] == ("day", X) and not entered.is_set():
                entered.set()
                gate.wait(5)
        self.provider.hook = hold
        t1 = threading.Thread(target=lambda: c.refetch_window(X, "m60", bars=40))
        t1.start()
        self.assertTrue(entered.wait(5))
        t2 = threading.Thread(target=lambda: c.catch_up(X, now))
        t2.start()
        time.sleep(0.3)
        during = [call for call in self.calls(X) if "2026-09-28" in (call[2][:10], call[3][:10])]
        gate.set()
        t1.join(10)
        t2.join(10)
        self.assertEqual(during, [])
        today = [call for call in self.calls(X) if call[2][:10] == "2026-09-28"]
        self.assertEqual(sorted(call[0] for call in today), ["day", FACT], today)



class Suspending(Recording):
    """suspend_from 起（含）的日子停牌：日线 sf=1、量为 0，分钟为空。"""
    suspend_from = None

    def day_history(self, code, start, end):
        rows = super().day_history(code, start, end)
        cut = self.suspend_from
        return [r if not cut or r.trade_date < cut or r.code != code or code == INDEX else
                RawDayRow(r.code, r.trade_date, 10, 10, 10, 10, 0, "lot", 0, "CNY", 10, 1, "final", new_batch_id())
                for r in rows]

    def minute_history(self, code, fact_freq, start, end, *, now):
        rows = super().minute_history(code, fact_freq, start, end, now=now)
        cut = self.suspend_from
        return [r for r in rows if not cut or r.trade_date < cut or code == INDEX]


class RefetchCompletenessTests(Base):
    """V28、V29：重拉按本次返回判断取全；日历缺行不豁免旧事实证明的开市日，合法停牌不算空返回。"""

    def _drop_calendar(self, day):
        with facts.write_txn(self.conn):
            self.conn.execute("DELETE FROM calendar WHERE market='CN' AND date=?", (day,))

    def test_calendar_row_missing_does_not_let_old_rows_hide_an_omitted_day(self):   # V28
        for watch in ([A], [A, X]):
            with self.subTest(tracked=X in watch):
                self.setUp()
                self.watch[:] = watch
                c = self.make()
                c.ensure_window(X, "m60", bars=40)
                self._drop_calendar("2026-09-23")
                self.provider.skip_days = ("2026-09-23",)
                res = c.refetch_window(X, "m60", bars=40)
                self.assertNotEqual(res["status"], "ok", res)

    def test_whole_window_suspension_refetch_is_ok(self):   # V29
        self.provider = Suspending()
        self.provider.suspend_from = "2026-01-01"
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        res = c.refetch_window(X, "m60", bars=40)
        self.assertEqual((res["status"], res["empty"]), ("ok", 0), res)

    def test_suspended_month_slice_is_not_an_empty_failure(self):   # V29
        self.provider = Suspending()
        self.provider.suspend_from = "2026-09-01"
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        res = c.refetch_window(X, "m60", bars=40)
        self.assertEqual((res["status"], res["empty"]), ("ok", 0), res)


class RefetchOwnershipTests(Base):
    """V30：重拉与追赶互斥在超时、多个重拉、异常退出时仍成立。"""

    def _block(self, predicate):
        gate, entered = threading.Event(), threading.Event()

        def hold(call):
            if predicate(call) and not entered.is_set():
                entered.set()
                gate.wait(5)
        self.provider.hook = hold
        return gate, entered

    def test_refetch_times_out_waiting_for_catch_up_and_reports_busy(self):
        c = self.make()
        gate, entered = self._block(lambda call: call[:2] == ("day", X))
        with unittest.mock.patch.object(collector, "_REFETCH_WAIT_CATCHUP_S", 0.1):
            self.assertTrue(c.catch_up_bounded(X, 0))
            self.assertTrue(entered.wait(5))
            before = len(self.provider.calls)
            res = c.refetch_window(X, "m60", bars=40)
            during = self.provider.calls[before:]
        gate.set()
        self.assertEqual(res["status"], "busy", res)
        self.assertEqual(during, [])

    def test_second_concurrent_refetch_is_busy_and_first_keeps_ownership(self):
        c = self.make()
        gate, entered = self._block(lambda call: call[:2] == ("day", X))
        first = {}
        t = threading.Thread(target=lambda: first.update(c.refetch_window(X, "m60", bars=40)))
        t.start()
        self.assertTrue(entered.wait(5))
        before = len(self.provider.calls)
        second = c.refetch_window(X, "m60", bars=40)
        self.assertEqual(second["status"], "busy", second)
        self.assertFalse(c.catch_up_bounded(X, 0))               # 第一个重拉仍占用：追赶让路
        self.assertEqual(self.provider.calls[before:], [])
        gate.set()
        t.join(10)
        self.assertEqual(first["status"], "ok", first)

    def test_refetch_that_raises_releases_ownership(self):   # 回归守护（修前 finally 已释放）
        c = self.make()
        with unittest.mock.patch.object(c, "_refetch_today", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                c.refetch_window(X, "m60", bars=40)
        self.assertNotEqual(c.refetch_window(X, "m60", bars=40)["status"], "busy")


class HkRemovalTests(HkBase):
    """V31：港股定稿与缓存刷新在请求之间复核资格：移出后不再开始新的缓存请求。"""

    def _vendor_calls(self):
        return [call for call in self.fake.calls if call[0] in ("day", "m30")]

    def test_removed_during_finalize_minute_request_skips_vendor_refresh(self):
        self.watch.append("hk00700")
        c = self.make()
        c.ensure_window("hk00700", "day")
        self.fake.calls.clear()
        orig = self.fake.minute_history

        def minute(code, fact_freq, start, end, *, now):
            self.watch.clear()
            return orig(code, fact_freq, start, end, now=now)
        self.fake.minute_history = minute
        c.clock.t = at("2026-09-28", 18).timestamp()
        c.tick(at("2026-09-28", 18))
        self.assertEqual(self._vendor_calls(), [])

    def test_removed_during_vendor_day_request_skips_m30(self):
        self.watch.append("hk00700")
        c = self.make()
        c.ensure_window("hk00700", "day")
        self.fake.calls.clear()
        orig = self.fake.qfq_series

        def qfq(code, freq, start, end):
            if freq == "day":
                self.watch.clear()
            return orig(code, freq, start, end)
        self.fake.qfq_series = qfq
        c.clock.t = at("2026-09-28", 18).timestamp()
        c.tick(at("2026-09-28", 18))
        self.assertEqual([call for call in self._vendor_calls() if call[0] == "m30"], [])


class HkVendorTruncationTests(HkBase):
    """V27：港股供应商前复权整段重取只返回末尾一行：不发布、保留当前版本，重拉不报成功。"""

    def test_truncated_vendor_refetch_keeps_published_cache_and_is_not_ok(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        c = self.make()
        c.ensure_window("hk00700", "m60", bars=40)
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        before = {freq: len(vq.read(conn, "hk00700", freq)[0]) for freq in vq.FREQS}
        self.assertGreater(before["m30"], 40)
        orig = self.fake.qfq_series
        self.fake.qfq_series = lambda code, freq, start, end: orig(code, freq, start, end)[-1:]
        res = c.refetch_window("hk00700", "m60", bars=40)
        self.assertNotEqual(res["vendor"], "ok", res)
        self.assertNotEqual(res["status"], "ok", res)
        self.assertEqual({freq: len(vq.read(conn, "hk00700", freq)[0]) for freq in vq.FREQS}, before)


class HolidayFake(HKFullFake):
    """某个工作日是港股假日：raw 与供应商都没有该日（日历也不知道）。"""
    holiday = "2026-09-09"

    def day_history(self, code, start, end):
        return [r for r in super().day_history(code, start, end) if r.trade_date != self.holiday]

    def minute_history(self, code, fact_freq, start, end, *, now):
        return [r for r in super().minute_history(code, fact_freq, start, end, now=now) if r.trade_date != self.holiday]

    def qfq_series(self, code, freq, start, end):
        return [b for b in super().qfq_series(code, freq, start, end) if b["trade_date"] != self.holiday]


class HkUnknownHolidayTests(HkBase):
    def test_hk_refetch_over_an_unknown_holiday_is_ok(self):   # V33
        self.fake = HolidayFake()
        c = self.make()
        c.ensure_window("hk00700", "m60", bars=40)
        res = c.refetch_window("hk00700", "m60", bars=40)
        self.assertEqual(res["status"], "ok", res)

    def test_hk_window_over_an_unknown_holiday_is_not_lagging(self):   # V33
        self.fake = HolidayFake()
        c = self.make()
        c.ensure_window("hk00700", "m60")                         # 落后判断按默认窗口（520 根）
        self.assertFalse(c.lagging("hk00700", c.now()))

    def test_proven_trading_day_still_counts_when_not_returned(self):   # V33 对照：有证据的日子仍要求
        c = self.make()
        c.ensure_window("hk00700", "m60", bars=40)
        self.fake = HolidayFake()                                  # 该日已由旧事实推为开市日，本次漏返回
        c = self.make()
        res = c.refetch_window("hk00700", "m60", bars=40)
        self.assertNotEqual(res["status"], "ok", res)


class HkVendorTailTests(HkBase):
    def test_full_refresh_with_earlier_cutoff_keeps_published_tail(self):   # V34
        from chanapp.engine.kline import hk_vendor_qfq as vq
        self.watch.append("hk00700")
        c = self.make()
        c.ensure_window("hk00700", "day")
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        tail = {freq: vq.read(conn, "hk00700", freq)[0][-1]["dt"] for freq in vq.FREQS}
        self.assertGreater(tail["day"], "2026-09-10")
        c.refresh_vendor_qfq("hk00700", c.now(), closed_through="2026-09-10", force=True)
        self.assertEqual({freq: vq.read(conn, "hk00700", freq)[0][-1]["dt"] for freq in vq.FREQS}, tail)


class SlotDropFake(HKFullFake):
    """供应商前复权 m30 少一个槽位（drop），raw 完整。"""
    drop = None

    def qfq_series(self, code, freq, start, end):
        return [b for b in super().qfq_series(code, freq, start, end) if freq != "m30" or b["slot_end"] != self.drop]


class DayGapFake(HKFullFake):
    """raw 日线漏返回某日（gap），分钟与供应商缓存都有该日。"""
    gap = "2026-09-09"

    def day_history(self, code, start, end):
        return [r for r in super().day_history(code, start, end) if r.trade_date != self.gap]


class HkVendorSlotTests(HkBase):
    """V35：供应商 m30 候选按 raw 槽位核对覆盖。"""

    def _m30(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        bars, meta = vq.read(conn, "hk00700", "m30")
        return [b["dt"] for b in bars], meta["version"]

    def test_append_missing_one_slot_is_not_published(self):
        self.watch.append("hk00700")
        self.fake = SlotDropFake()
        c = self.make()
        c.ensure_window("hk00700", "day")
        before = self._m30()
        self.fake.drop = "2026-09-28 11:00"
        c.clock.t = at("2026-09-28", 18).timestamp()
        c.tick(at("2026-09-28", 18))
        self.assertEqual(self._m30(), before)
        self.assertFalse(c._vendor_caught_up("hk00700", "2026-09-28"))

    def test_full_refetch_missing_one_interior_slot_is_not_ok(self):   # 回归守护（整段替换按已发布标签核对，修前已成立）
        self.fake = SlotDropFake()
        c = self.make()
        c.ensure_window("hk00700", "m60", bars=40)
        before = self._m30()
        self.fake.drop = "2026-09-15 11:00"
        res = c.refetch_window("hk00700", "m60", bars=40)
        self.assertNotEqual(res["vendor"], "ok", res)
        self.assertEqual(self._m30(), before)


class WindowAbsentEvidenceTests(HkBase):
    """V36：分钟或供应商缓存有该日时，日线漏返回不能被当成无数据豁免。"""

    def test_day_missing_with_minute_evidence_is_not_exempted(self):
        self.fake = DayGapFake()
        c = self.make()
        c.ensure_window("hk00700", "m60")
        self.assertTrue(c.lagging("hk00700", c.now()))
        self.assertNotEqual(c.refetch_window("hk00700", "m60")["status"], "ok")
        self.assertTrue(c.lagging("hk00700", c.now()))

    def test_exemption_is_revoked_when_minute_evidence_appears(self):
        self.fake = HolidayFake()
        c = self.make()
        c.ensure_window("hk00700", "m60")
        self.assertFalse(c.lagging("hk00700", c.now()))
        day = HolidayFake.holiday
        rows = [RawMinuteRow("hk00700", day, f"{day} 16:00", 100, 100, 100, 100, 10, "share", None, "closed",
                             "traded", new_batch_id())]
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        facts.commit_minute_rows(conn, rows, market="HK", kind="stock", item="minute_history", fact_freq="m30",
                                 source="longbridge", binding_gen=1, today="2026-09-26")
        self.assertTrue(c.lagging("hk00700", c.now()))


class LateDayFake(HKFullFake):
    """raw 日线只从 since_day 起返回（更早的日子日线缺失）。"""
    since_day = "2026-09-22"

    def day_history(self, code, start, end):
        return [r for r in super().day_history(code, start, end) if r.trade_date >= self.since_day]


class PrefixEvidenceTests(HkBase):
    def test_minute_evidence_before_first_day_row_is_not_hidden_by_absent_watermark(self):   # V37
        day = "2026-09-21"
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        rows = [RawMinuteRow("hk00700", day, f"{day} {t}", 100, 100, 100, 100, 10, "share", None, "closed", "traded",
                             new_batch_id()) for t in sessions.slots("HK", "m30")]
        facts.commit_minute_rows(conn, rows, market="HK", kind="stock", item="minute_history", fact_freq="m30",
                                 source="longbridge", binding_gen=1, today="2026-09-26")
        self.fake = LateDayFake()
        c = self.make()
        c.ensure_window("hk00700", "m60")
        self.assertNotEqual(c.refetch_window("hk00700", "m60")["status"], "ok")
        self.assertTrue(c.lagging("hk00700", c.now()))


class ConflictFake(Recording):
    """conflict 给定时，该日日线收盘价与已入库的不同（已收盘 final 行冲突 → 待核验）。"""
    conflict = None

    def day_history(self, code, start, end):
        rows = super().day_history(code, start, end)
        return [RawDayRow(r.code, r.trade_date, 10, 11, 10, 11, DAY_VOL, "lot", 1, "CNY", 10, 0, "final", new_batch_id())
                if r.trade_date == self.conflict and r.code == code else r for r in rows]


class RefetchReviewTests(Base):
    def test_refetch_with_pending_review_in_window_is_not_ok(self):   # V38
        self.provider = ConflictFake()
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        self.provider.conflict = "2026-09-15"
        self.assertNotEqual(c.refetch_window(X, "m60", bars=40)["status"], "ok")
        self.provider.conflict = None                             # 本次返回正常，但既有冲突仍未裁决
        self.assertNotEqual(c.refetch_window(X, "m60", bars=40)["status"], "ok")

    def test_intraday_refetch_with_all_rows_rejected_is_not_ok(self):   # V39
        now = self.set_time(at("2026-09-28", 10))
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        day = now.date().isoformat()
        self.provider.minute_live = lambda code, fact_freq, *, now: [
            RawMinuteRow(code, day, f"{day} 10:00", -1, -1, -1, -1, 1, "lot", 1, "forming", "traded", new_batch_id())]
        res = c.refetch_window(X, "m60", bars=40)
        self.assertNotEqual(res["today"], "ok", res)
        self.assertNotEqual(res["status"], "ok", res)


class QuarantinedSlotTests(HkBase):
    def test_vendor_candidate_without_quarantined_raw_slot_is_published(self):   # V40
        self.fake = SlotDropFake()
        c = self.make()
        c.ensure_window("hk00700", "m60", bars=40)
        slot = "2026-09-15 11:00"
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            facts.quarantine(conn, "hk00700", "m30", slot, "review_conflict")
        self.fake.drop = slot
        res = c.refetch_window("hk00700", "m60", bars=40)
        self.assertEqual(res["vendor"], "ok", res)


class IntradayQuarantineTests(Base):
    """V41：盘中当天按本次可读接纳判断；隔离槽位上的行不算取到。"""
    DAY = "2026-09-28"

    def _setup(self, live_slots, quarantined):
        self.set_time(at(self.DAY, 10, 10))
        c = self.make()
        c.ensure_window(X, "m60", bars=40)
        with facts.write_txn(self.conn):
            for slot in quarantined:
                facts.quarantine(self.conn, X, FACT, f"{self.DAY} {slot}", "review_conflict")
        self.provider.minute_live = lambda code, fact_freq, *, now: [
            RawMinuteRow(code, self.DAY, f"{self.DAY} {t}", 10, 10, 10, 10, 1, "lot", 1, "forming", "traded",
                         new_batch_id()) for t in live_slots]
        return c

    def test_live_rows_all_on_quarantined_slots_are_not_ok(self):
        res = self._setup(["10:15"], ["10:15"]).refetch_window(X, "m60", bars=40)
        self.assertNotEqual(res["today"], "ok", res)

    def test_live_rows_partly_on_quarantined_slots_are_not_ok(self):
        res = self._setup(["10:00", "10:15"], ["10:15"]).refetch_window(X, "m60", bars=40)
        self.assertNotEqual(res["today"], "ok", res)

    def test_readable_live_rows_are_ok(self):   # 对照
        res = self._setup(["10:00", "10:15"], []).refetch_window(X, "m60", bars=40)
        self.assertEqual((res["today"], res["status"]), ("ok", "ok"), res)


class ReviewScopeTests(Base):
    def test_minute_review_outside_minute_window_does_not_block_ok(self):   # V42
        c = self.make()
        c.ensure_window(X, "day")
        with facts.write_txn(self.conn):
            self.conn.execute("INSERT INTO pending_review(code, dataset, key, incoming, reason, batch_id, created_at)"
                              " VALUES (?,?,?,?,?,?,?)", (X, FACT, "2025-09-16 10:00", "{}", "closed_conflict",
                                                          "b", facts.now_iso()))
        self.assertEqual(c.refetch_window(X, "day")["status"], "ok")

    def test_day_review_inside_day_window_still_blocks(self):   # V42 对照
        c = self.make()
        c.ensure_window(X, "day")
        with facts.write_txn(self.conn):
            self.conn.execute("INSERT INTO pending_review(code, dataset, key, incoming, reason, batch_id, created_at)"
                              " VALUES (?,?,?,?,?,?,?)", (X, "day", "2025-09-16", "{}", "closed_conflict",
                                                          "b", facts.now_iso()))
        self.assertNotEqual(c.refetch_window(X, "day")["status"], "ok")


class ShortHistoryFake(HKFullFake):
    """raw 日线、分钟与供应商缓存都只从 since_day 起有数据（上市不久、上市日未知）。"""
    since_day = "2026-09-22"

    def day_history(self, code, start, end):
        return [r for r in super().day_history(code, start, end) if r.trade_date >= self.since_day]

    def minute_history(self, code, fact_freq, start, end, *, now):
        return [r for r in super().minute_history(code, fact_freq, start, end, now=now)
                if r.trade_date >= self.since_day]

    def qfq_series(self, code, freq, start, end):
        return [b for b in super().qfq_series(code, freq, start, end) if b["trade_date"] >= self.since_day]


class VendorWidenMemoTests(HkBase):
    def test_alternating_minute_views_do_not_refetch_short_vendor_history(self):   # V43
        self.fake = ShortHistoryFake()
        c = self.make()
        for _ in range(2):                                        # 首建之后两个周期各扩一次窗（各自首次尝试）
            c.ensure_window("hk00700", "m60")
            c.ensure_window("hk00700", "m30")
        before = len(self.fake.calls)
        for _ in range(3):
            c.ensure_window("hk00700", "m60")
            c.ensure_window("hk00700", "m30")
        self.assertEqual(self.fake.calls[before:], [])


class LeaseMidRoundTests(Base):
    """V32：查看租期按每个请求开始时的时钟复核，不按轮首快照。"""

    def test_lease_expiring_mid_round_stops_later_untracked_requests(self):
        start = self.set_time(at("2026-09-28", 10))
        c = self.make()
        c.touch_viewing(X)
        now = self.set_time(at("2026-09-28", 10, 2, 29))            # 最后查看后第 149 秒开始一轮
        live, preopen = self.provider.minute_live, []

        def slow_live(code, fact_freq, *, now):
            if code == A:
                self.clock.t += 2                                    # 前一个自选代码的请求耗时 2 秒
            return live(code, fact_freq, now=now)
        self.provider.minute_live = slow_live
        self.provider.preopen_ref = lambda code, day: preopen.append(code)
        c.tick(now)
        self.assertTrue(self.calls(A, "live"))
        self.assertEqual(self.calls(X, "live"), [])
        self.assertNotIn(X, preopen)
        del start


if __name__ == "__main__":
    unittest.main()
