"""整轮验收的端到端流程（目标 2026-09-29 第四阶段「新用户 E2E」「收盘 E2E」）。

性质：验收测试，写在第二、三阶段实现之后（tests-after，不是 TDD 的 RED）；这里失败说明已实现行为与使用规则不符。
与 test_kline_viewing_e2e 同一搭建（真实采集器与 FastAPI 路由、临时事实库、记录每次请求的假源、钉住的时钟、后台线程
不启动），补它没有走到的段落：

新用户：
1. 空缓存搜索查看 → 520 根分析 → 关闭 → 跨日（周一收盘后）再看：只取当天，不重取窗口，状态已定稿；
2. 重拉时数据源失败：响应头不报成功，已有 K 线原样；
3. 加入自选但从不打开：历史线程照样补齐（日线回填到上限、分钟规划），之后打开零请求；移出后历史轮次零请求；
3b. 加入自选后立刻打开、深历史上游卡住：图表照样先返回近期窗口，深历史由之后的历史线程补（终审应修的端到端证明）；
3c. 历史线程先拿到同一代码的锁、深历史上游卡住时首开：等锁有预算，超时没有快照就 503（页面受控重试），不无限等待；
    释放后再开即返回窗口（复核必须修的端到端证明）；
4. 指数（非系统依赖的深证成指）搜索查看：只取窗口，不规划完整历史；
5. 前复权链不足：窗口里有缺失交易日时前复权图明确提示「前复权自 … 起可用」，不静默全拉。
收盘：
6. 盘中接纳后收盘、当天历史空返回：自选有界重试（20:00/22:00 两次，之后当天不再请求；2026-09-30 前为 17:30 起三次），状态保持
   「已收盘 · 待定稿」且时间不因失败推进；非自选在收盘后重新打开触发一次请求路径跟进，离开后不再有两小时一次的后台
   请求；次日历史线程补齐自选、非自选再打开时补分析窗口，读取改报「已定稿」。
港股、周线的首开边界与加入自选后的扩展见 test_kline_viewing_e2e（本文件不重复）。
"""
import json
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import quote

from chanapp.engine import data as engine_data
from chanapp.engine import llm as engine_llm
from chanapp.engine.kline import facts, sessions, views
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests.test_kline_collector import FACT, _list_since
from chanapp.tests import test_kline_viewing_e2e as e2e
from chanapp.tests.test_kline_viewing_tracking import A, X, at

Y = "sz000004"                  # 从不打开、直接加入自选
IDX = "sz399001"                # 深证成指：不是系统依赖（系统依赖只有上证指数）

DAY_CAP = e2e.DAY_CAP


class _Flow(unittest.TestCase):
    # 只借搭建与辅助方法：模块层不能绑定那个测试类（加载器会把它的用例在本文件重跑一遍）
    _src = e2e.ViewingTrackingEndToEnd
    restart, calls, mark, planned = _src.restart, _src.calls, _src.mark, _src.planned
    history_rounds, chart, assert_within_window = _src.history_rounds, _src.chart, _src.assert_within_window
    del _src

    def setUp(self):
        e2e.ViewingTrackingEndToEnd.setUp(self)
        clock = self.clock

        class PinnedDatetime(datetime):
            """门面读视图不传 now（生产两边都是真实时钟）；这里让视图与采集器共用钉住的时钟。"""
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock.t, tz)

        patcher = mock.patch.object(views, "datetime", PinnedDatetime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def status(self, code, freq="m30"):
        return self.chart(code, freq)["meta"]["coverage"]["data_status"]


class NewUserAcceptance(_Flow):
    def test_view_close_next_day_increment_then_failed_refetch_keeps_bars(self):
        self.assertEqual(self.api.post("/api/views", json={"code": X, "name": "万科A", "freq": "m30",
                                                            "adjust": "qfq"}).status_code, 200)
        first = self.chart(X, "m30")
        self.assertEqual(first["meta"]["bars"], 520)
        self.assertEqual(self.chart(X, "m60")["meta"]["bars"], 520)
        self.assertEqual(self.chart(X, "day")["meta"]["bars"], 520)
        self.assertEqual(self.planned(X), set())

        # 520 根窗口上的 AI 分析：三个周期齐、没有「输入不足」标记，模型只调用一次，不再请求数据源
        m = self.mark()
        with mock.patch.object(engine_llm, "is_configured", return_value=True), \
                mock.patch.object(engine_llm, "analyze",
                                  return_value='{"current_state": "测试", "scenarios": []}') as llm:
            engine_data._window_attempts.clear()
            tokens = self.api.get(f"/api/analysis?code={X}&freq=day&tokens=%7B%7D").json()["tokens"]
            r = self.api.get(f"/api/analysis?code={X}&freq=day&tokens=" + quote(json.dumps(tokens)))
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual((r.json()["status"], r.json()["structure_short"]), ("ok", []))
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(self.calls(X, since=m), [])

        # 关闭页面，周一收盘定稿时点后再打开：只补当天（日线与 m15 各自取当天），不重取窗口
        # 有意改写（所有者 2026-09-30）：A 股首个定稿时点 17:30 → 20:00
        self.clock.t = at("2026-09-28", 20, 10).timestamp()
        m = self.mark()
        again = self.chart(X, "m30")
        added = self.calls(X, since=m)
        self.assertEqual(sorted((c[0], c[2][:10], c[3][:10]) for c in added),
                         [("day", "2026-09-28", "2026-09-28"), (FACT, "2026-09-28", "2026-09-28")],
                         added)                                          # 日线与 m15 各一次，都只到当天
        self.assertEqual(again["kline"][-1]["time"], "2026-09-28 15:00")
        self.assertEqual(again["meta"]["coverage"]["data_status"]["phase"], "final")
        self.assertEqual(again["meta"]["coverage"]["data_status"]["day"], "2026-09-28")

        # 重拉时数据源失败：不报成功，已有 K 线原样
        before = self.chart(X, "m30")["kline"]
        self.provider.fail = True
        m = self.mark()
        r = self.api.get(f"/api/chart?code={X}&freq=m30&refetch=1")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertTrue(self.calls(X, since=m), "重拉应实际请求数据源")
        self.assertNotEqual(r.headers.get("X-Refetch-Status"), "ok")
        self.assertEqual(r.json()["kline"], before)
        self.provider.fail = False
        self.assertEqual(self.chart(X, "m30")["kline"], before)
        self.assertEqual(self.planned(X), set())

    def test_track_without_opening_backfills_then_untrack_stops(self):
        _list_since(self.dir, [Y], "2010-01-04")
        self.assertEqual(self.api.post("/api/watchlist", json={"code": Y, "name": "深物业A"}).status_code, 200)
        m = self.mark()
        self.history_rounds(4)
        days, minutes = self.calls(Y, "day", since=m), self.calls(Y, FACT, since=m)
        self.assertTrue(days and min(c[2] for c in days) <= DAY_CAP[:7] + "-31", days)   # 日线回填到十年上限
        self.assertTrue(minutes, "分钟完整历史开始续传")
        self.assertEqual(self.planned(Y), {"backfill", "minute"})
        m = self.mark()
        self.assertGreaterEqual(self.chart(Y, "m30")["meta"]["bars"], 520)
        self.assertEqual(self.chart(Y, "day")["meta"]["bars"], 520)
        self.assertEqual(self.calls(Y, since=m), [], "已补齐：打开零请求")
        self.assertEqual(self.api.delete(f"/api/watchlist/{Y}").status_code, 200)
        m = self.mark()
        self.history_rounds(3, start_minute=30)
        self.assertEqual(self.calls(Y, since=m), [])

    def test_track_then_open_immediately_serves_window_while_deep_history_is_stuck(self):
        _list_since(self.dir, [Y], "2010-01-04")
        gate, deep = threading.Event(), []
        self.addCleanup(gate.set)

        def stuck(call):                                       # 窗口以外的日线请求：上游卡住
            if call[:2] == ("day", Y) and call[2][:10] < e2e.WINDOW_DAY_FLOOR:
                deep.append(call)
                gate.wait(10)
        self.provider.hook = stuck
        self.assertEqual(self.api.post("/api/watchlist", json={"code": Y, "name": "深物业A"}).status_code, 200)
        box = {}
        opener = threading.Thread(target=lambda: box.setdefault("r", self.chart(Y, "m30")), daemon=True)
        opener.start()
        opener.join(5)
        self.assertFalse(opener.is_alive(), "首开等在深历史上")
        self.assertEqual(box["r"]["meta"]["bars"], 520)
        self.assertEqual(deep, [], "首开没有发深历史请求")
        gate.set()
        self.provider.hook = None
        m = self.mark()
        self.history_rounds(4)
        self.assertIn(DAY_CAP, [c[2] for c in self.calls(Y, "day", since=m)], "深历史交历史线程补")
        self.assertEqual(self.planned(Y), {"backfill", "minute"})

    def test_first_open_while_history_holds_the_code_is_bounded_then_serves(self):
        _list_since(self.dir, [Y], "2010-01-04")
        self.assertEqual(self.api.post("/api/watchlist", json={"code": Y, "name": "深物业A"}).status_code, 200)
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def stuck(call):                                       # 历史线程的十年日线：上游卡住（持着该代码的锁）
            if call[:2] == ("day", Y) and call[2][:10] < e2e.WINDOW_DAY_FLOOR:
                entered.set()
                gate.wait(20)
        self.provider.hook = stuck
        errors = []

        def rounds():
            try:
                self.history_rounds(1)
            except BaseException as exc:  # noqa: BLE001 — 转交主线程断言
                errors.append(exc)
        history = threading.Thread(target=rounds, daemon=True)
        history.start()
        try:
            self.assertTrue(entered.wait(5), "历史线程没有开始整段回填")
            engine_data._window_attempts.clear()
            started = time.monotonic()
            r = self.api.get(f"/api/chart?code={Y}&freq=m30")
            waited = time.monotonic() - started
        finally:                                               # 无论断言成败，都放行并等历史线程收尾
            gate.set()
            history.join(10)
        self.assertFalse(history.is_alive(), "历史线程没有收尾")
        self.assertEqual(errors, [])
        self.assertEqual(r.status_code, 503, r.text[:200])
        self.assertLess(waited, 5, "首开等锁没有预算")
        self.provider.hook = None
        self.assertEqual(self.chart(Y, "m30")["meta"]["bars"], 520)

    def test_index_search_view_stays_within_window(self):
        _list_since(self.dir, [IDX], "2010-01-04")
        m = self.mark()
        self.assertEqual(self.chart(IDX, "day")["meta"]["bars"], 520)
        self.assertEqual(self.chart(IDX, "m60")["meta"]["bars"], 520)
        opened = self.calls(IDX, since=m)
        self.assertTrue(opened)
        self.assert_within_window(opened)
        self.assertEqual(self.planned(IDX), set())
        self.assertNotIn(IDX, self.worker.tracked_codes())
        m = self.mark()
        self.history_rounds(3)
        self.assertEqual(self.calls(IDX, since=m), [])

    def test_qfq_chain_gap_is_announced_not_backfilled(self):
        fetch = self.provider.day_history

        def day_history(code, start, end):              # 只有这只股票缺一个交易日：前复权相邻判断在此中断
            return [r for r in fetch(code, start, end) if not (code == X and r.trade_date == "2025-06-03")]
        self.provider.day_history = day_history
        m = self.mark()
        body = self.chart(X, "day")                         # 默认前复权
        self.assertEqual(body["meta"]["adjust"], "qfq")
        notices = {n["code"]: n for n in body["meta"]["notices"]}
        self.assertIn("qfq_from", notices, body["meta"]["notices"])
        self.assertGreater(body["meta"]["coverage"]["qfq_from"], "2025-06-03")
        self.assert_within_window(self.calls(X, since=m))
        self.assertEqual(self.planned(X), set())


class CloseAcceptance(_Flow):
    DAY = "2026-09-28"                                    # 周一

    def setUp(self):
        super().setUp()
        # 已知交易日历（指数日线只推到 09-25）：定稿走「已知日历」时点，不落到日历未知的兜底
        day = datetime(2026, 9, 26)
        with facts.write_txn(self.conn):
            while day <= datetime(2026, 10, 9):
                if day.weekday() < 5:
                    self.conn.execute("INSERT OR IGNORE INTO calendar(market, date, is_open, sessions, source,"
                                      " fetched_at) VALUES ('CN',?,1,'[]','test',?)",
                                      (day.strftime("%Y-%m-%d"), facts.now_iso()))
                day += timedelta(days=1)

    def finalize_calls(self, code, since):
        return [c for c in self.calls(code, since=since) if c[0] in ("day", FACT) and c[3][:10] == self.DAY]

    def test_awaiting_final_bounded_retries_then_next_day_catch_up(self):
        self.chart(A, "m30")                               # 自选：周末先把窗口建好
        self.clock.t = at(self.DAY, 10, 0).timestamp()
        self.worker.tick(at(self.DAY, 10, 0))               # 盘中接纳一根形成中的 bar
        self.clock.t = at(self.DAY, 15, 30).timestamp()
        self.chart(X, "m30")                               # 非自选：收盘后搜索查看
        live = self.status(A)
        self.assertEqual((live["phase"], live["day"]), ("awaiting_final", self.DAY))
        self.assertIsNotNone(live["at"])

        self.provider.empty_on = self.DAY                  # 当天历史一律空返回
        m = self.mark()
        # 有意改写（所有者 2026-09-30）：A 股首个定稿时点 20:00，间隔 2 小时，当天只有 20:00、22:00 两次
        for hh, mm in ((17, 30), (18, 30), (20, 0), (21, 0), (22, 0), (23, 30)):
            self.clock.t = at(self.DAY, hh, mm).timestamp()
            self.worker.tick(at(self.DAY, hh, mm))
        tries = self.finalize_calls(A, m)
        self.assertEqual(len([c for c in tries if c[0] == "day"]), 2, tries)       # 两次自动尝试后当天不再请求
        self.assertEqual(self.finalize_calls(X, m), [], "非自选没有后台定稿与两小时重试")
        self.assertEqual(self.status(A), live, "失败不推进时间、不改报已定稿")
        m = self.mark()
        self.clock.t = at(self.DAY, 23, 35).timestamp()
        self.assertEqual(self.status(A)["phase"], "awaiting_final")          # 自选重新打开：读取追赶也受台账上限
        self.assertEqual(self.finalize_calls(A, m), [], "当天机会用完后重新打开自选不再请求")

        # 非自选收盘后重新打开：请求路径跟进一次；随后离开，不再有后台请求
        self.clock.t = at(self.DAY, 23, 40).timestamp()
        m = self.mark()
        self.assertEqual(self.status(X)["phase"], "awaiting_final")
        followed = self.finalize_calls(X, m)
        self.assertTrue(followed, "重新打开触发一次有界跟进")
        m = self.mark()
        for hh, mm in ((23, 50), (23, 59)):
            self.clock.t = at(self.DAY, hh, mm).timestamp()
            self.worker.tick(at(self.DAY, hh, mm))
        self.assertEqual(self.calls(X, since=m), [])

        # 次日：历史线程补齐自选的昨天，读取改报已定稿；非自选再打开时补分析窗口
        self.provider.empty_on = None
        self.clock.t = at("2026-09-29", 3, 0).timestamp()
        self.worker.history_tick(at("2026-09-29", 3, 0))
        self.clock.t = at("2026-09-29", 8, 0).timestamp()
        done = self.status(A)
        self.assertEqual((done["phase"], done["day"]), ("final", self.DAY))
        self.assertIsNotNone(done["at"])                    # 接纳时刻是真实写入时间（分钟精度），不随钉住的时钟
        self.assertEqual(self.status(X)["phase"], "final")


class HKNewListingAcceptance(_Flow):
    """第三阶段第四轮复审阻断 4：只有当天历史的非自选港股（新上市），收盘后首开要能建当天的前复权缓存。
    当天缓存只由定稿请求；首开的首建截止日退回上一交易日、为空，读取仍须进入同一台账约束的当天追赶。"""
    NEW, DAY = "hk09999", "2026-09-28"

    def test_untracked_hk_with_only_today_history_builds_cache_on_open(self):
        # 前提与复审回放一致：当天 raw 已在库（例如盘中看过、收盘后已接纳），还没有供应商缓存
        _list_since(self.dir, [self.NEW], self.DAY)
        day_fetch, minute_fetch = self.hk.day_history, self.hk.minute_history

        def listed(rows):
            return [r for r in rows if r.code != self.NEW or r.trade_date >= self.DAY]
        self.hk.day_history = lambda code, start, end: listed(day_fetch(code, start, end))
        self.hk.minute_history = lambda code, fact, start, end, *, now: listed(
            minute_fetch(code, fact, start, end, now=now))
        n = len(sessions.slots("HK", "m30"))
        facts.commit_day_rows(self.conn, [RawDayRow(self.NEW, self.DAY, 100, 100, 100, 100, 10 * n, "share", None,
                                                    "HKD", 100, 0, "final", new_batch_id())],
                              market="HK", kind="stock", item="day_history", source="longbridge", binding_gen=1,
                              today=self.DAY)
        facts.commit_minute_rows(self.conn, [RawMinuteRow(self.NEW, self.DAY, f"{self.DAY} {t}", 100, 100, 100, 100,
                                                          10, "share", None, "closed", "traded", new_batch_id())
                                             for t in sessions.slots("HK", "m30")],
                                 market="HK", kind="stock", item="minute_history", fact_freq="m30",
                                 source="longbridge", binding_gen=1, today=self.DAY)
        self.clock.t = at(self.DAY, 17, 0).timestamp()
        body = self.chart(self.NEW, "day")
        if not body["kline"]:
            body = self.chart(self.NEW, "day")                # 追赶在后台线程：至多再读一次
        self.assertTrue(body["kline"], body["meta"])
        self.assertEqual(body["kline"][-1]["time"][:10], self.DAY)
        self.assertEqual(body["meta"]["adjust"], "qfq")


if __name__ == "__main__":
    unittest.main()
