"""搜索查看与自选跟踪分离的端到端请求记录（目标 2026-09-29 第二阶段退出证据）。

经 FastAPI 路由走一遍用户路径，采集器是真实实现（事实库在临时目录），只把数据源换成记录每次请求的假源、
时钟钉住；后台线程不启动，历史轮次与盘中轮次由用例按步推进。每一步断言数据源请求（代码、种类、起止、次数）：

1. 搜索查看非自选代码：只取分析窗口（日线约两年、分钟约半年），不登记三年分钟、不规划完整历史；查看记录落盘；
2. 再次查看其他周期、AI 与左拉分页（令牌过期均 409）：窗口已覆盖，零请求；周线需要足量日线（520 周），只补日线、截到 10 年上限；
3. 历史线程轮次：查看中的非自选零请求；
4. 手动重拉：重新请求窗口，范围不越出窗口，关注状态不变；
5. 加入自选：历史线程补完整历史，分钟窗口不重取、日线仍是一次回填请求；多轮与重启后不重复规划；
6. 移出自选：此后历史轮次零请求，遗留缺口保留但不再续传；
7. 重启：查看记录仍在；
8. 离开页面：盘中取数在查看窗口期后停止，自选照常。
港股另走一遍：前复权默认读供应商成套缓存（日线 + m30），非自选首开两套缓存都只到窗口；加入自选后才扩到回填目标。
重拉忙（第四轮阶段复审）：同一代码已有重拉在途时，第二个重拉与普通读取都不得进入首取、补窗口或追赶去等它的锁，
须在首个重拉释放前返回（有快照回快照、头为 busy；冷窗口明确回「正在更新」）。
重拉不错误报成功（第五轮阶段复审；修复后补写的 API 回归，采集器侧先 RED 的用例见 test_kline_viewing_tracking
V37–V39）：窗口内有待核验、盘中返回全被拒收、首行之前有分钟证据而日线缺失，经 API 的 X-Refetch-Status 都不是 ok，
旧快照照常服务。
"""
import json
import tempfile
import threading
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from chanapp.api import view_log
from chanapp.engine import data as engine_data
from chanapp.engine import llm as engine_llm
from chanapp.engine.kline import collector, config, facts, sessions
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests import cache_support
from chanapp.tests.test_kline_collector import DAY_VOL, FACT, Clock, HKFullFake, _enable_collector, _list_since
from chanapp.tests.test_kline_viewing_tracking import A, INDEX, X, Recording, at

WINDOW_DAY_FLOOR = "2024-06-01"       # 520 根日线 + 余量约两年
WINDOW_MINUTE_FLOOR = "2026-02-01"    # 520 根 m60 ≈ 130 个交易日 + 余量
DAY_CAP = "2016-09-26"                # 日线回填上限（DAY_BACKFILL_YEARS=10，自钉住的 2026-09-26 起算）


class ViewingTrackingEndToEnd(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        cache_support.isolate_cache_dir(self, self.dir)
        cache_support.set_env(self, "ANALYSIS_CACHE_DIR", str(self.dir / "analysis"))
        self.watchlist = self.dir / "watchlist.json"
        self.watchlist.write_text(json.dumps([{"code": A, "name": "招商银行"}], ensure_ascii=False))
        cache_support.set_env(self, "WATCHLIST_PATH", str(self.watchlist))
        cache_support.set_env(self, "VIEW_LOG_PATH", str(self.dir / "views.sqlite"))
        _list_since(self.dir, [A, X, INDEX], "2010-01-04")
        self.clock = Clock(at("2026-09-26", 20).timestamp())          # 周六晚
        self.provider = Recording()
        self.hk = HKFullFake()
        self.addCleanup(collector._shared.clear)
        self.addCleanup(engine_data._window_attempts.clear)
        self.worker = self.restart()
        from chanapp.api.main import app
        self.api = TestClient(app)                                     # 不进 with：不跑 lifespan，不起后台线程
        self.conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(self.conn.close)

    def restart(self):
        """新采集器实例接管同一事实库（等同服务重启：内存状态清零，持久状态保留）。"""
        from chanapp.api.main import _read_watchlist_raw
        worker = collector.Collector(self.dir, providers={"mairui": self.provider, "longbridge": self.hk},
                                     clock=self.clock,
                                     watchlist_fn=lambda: [w["code"] for w in _read_watchlist_raw()])
        collector._shared.clear()
        collector._shared[str(self.dir.resolve())] = worker
        engine_data._window_attempts.clear()
        return worker

    # ---------- 请求记录 ----------
    def calls(self, code, kind=None, since=0):
        return [c for c in self.provider.calls[since:] if c[1] == code and (kind is None or c[0] == kind)]

    def mark(self):
        return len(self.provider.calls)

    def planned(self, code):
        return {k for k in ("backfill", "minute") if facts.setting(self.conn, collector.plan_key(code, k))}

    def history_rounds(self, n, start_minute=1):
        for minute in range(start_minute, start_minute + n):
            self.clock.t = at("2026-09-26", 20, minute).timestamp()
            self.worker.history_tick(at("2026-09-26", 20, minute))

    def chart(self, code, freq="day", **extra):
        engine_data._window_attempts.clear()                          # 去掉门面节流，零请求须来自窗口已覆盖
        params = "&".join(f"{k}={v}" for k, v in extra.items())
        r = self.api.get(f"/api/chart?code={code}&freq={freq}" + ("&" + params if params else ""))
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.last_headers = r.headers
        return r.json()

    def assert_within_window(self, calls):
        for kind, code, start, end in calls:
            floor = WINDOW_DAY_FLOOR if kind == "day" else WINDOW_MINUTE_FLOOR
            self.assertGreaterEqual(start[:10], floor, (kind, code, start, end))

    def test_search_view_then_track_then_untrack_request_log(self):
        # 1. 搜索查看：打开图表并记查看
        m = self.mark()
        self.assertEqual(self.api.post("/api/views", json={"code": X, "name": "万科A", "freq": "day",
                                                            "adjust": "qfq"}).status_code, 200)
        first = self.chart(X, "day")
        self.assertGreaterEqual(first["meta"]["bars"], 500)
        opened = self.calls(X, since=m)
        self.assertTrue(self.calls(X, "day", since=m) and self.calls(X, FACT, since=m), opened)
        self.assert_within_window(opened)
        self.assertLessEqual(len(opened), 12, opened)                  # 日线 1～2 次 + 分钟按月分片
        self.assertEqual(facts.open_gaps(self.conn, X, FACT), [])
        self.assertEqual(self.planned(X), set())
        self.assertEqual(self.calls(A, since=m), [], "查看一只股票不顺带请求别的代码")

        # 2. 再次查看其他周期、AI 读取（令牌过期 409）：窗口已覆盖，零请求
        m = self.mark()
        self.assertGreaterEqual(self.chart(X, "m60")["meta"]["bars"], 500)
        self.chart(X, "m30")
        with unittest.mock.patch.object(engine_llm, "is_configured", return_value=True):
            engine_data._window_attempts.clear()
            stale = self.api.get(f"/api/analysis?code={X}&freq=day&tokens=%7B%7D")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(set(stale.json()["tokens"]), {"day", "m60", "m30"})
        page = self.api.get(f"/api/chart?code={X}&freq=day&before={first['kline'][0]['time']}&token=stale")
        self.assertEqual(page.status_code, 409)                        # 左拉分页令牌过期：只读，不联网
        self.assertEqual(self.calls(X, since=m), [])
        m = self.mark()
        self.chart(X, "week")
        weekly = self.calls(X, since=m)
        self.assertTrue(weekly and all(c[0] == "day" and c[2] >= DAY_CAP for c in weekly), weekly)
        self.assertLessEqual(len(weekly), 2, weekly)
        self.assertEqual(self.planned(X), set())

        # 3. 历史线程：查看中的非自选零请求
        m = self.mark()
        self.history_rounds(3)
        self.assertEqual(self.calls(X, since=m), [])
        self.assertEqual(self.planned(X), set())

        # 4. 手动重拉：重新请求窗口，不越界、不升级关注；结果如实回报（响应头）
        m = self.mark()
        self.chart(X, "day", refetch=1)
        refetched = self.calls(X, since=m)
        self.assertTrue(self.calls(X, "day", since=m) and self.calls(X, FACT, since=m), refetched)
        self.assert_within_window(refetched)
        self.assertEqual(self.last_headers.get("X-Refetch-Status"), "ok")
        # 4b. 周线上重拉：日线按周线窗口整段重取（不止 m60 依赖的窗口）
        m = self.mark()
        self.chart(X, "week", refetch=1)
        weekly_refetch = self.calls(X, "day", since=m)
        self.assertTrue(weekly_refetch and min(c[2] for c in weekly_refetch) < WINDOW_DAY_FLOOR, weekly_refetch)
        self.assertTrue(all(c[2] >= DAY_CAP for c in weekly_refetch), weekly_refetch)
        self.assertEqual(self.planned(X), set())
        self.assertNotIn(X, self.worker.tracked_codes())

        # 5. 加入自选：补完整历史；已有日线与分钟窗口不重复取；多轮与重启不重复规划
        window_lo = min(r["trade_date"] for r in facts.read_minute_rows(self.conn, X, FACT))
        self.assertEqual(self.api.post("/api/watchlist", json={"code": X, "name": "万科A"}).status_code, 200)
        m = self.mark()
        self.history_rounds(3, start_minute=10)
        tracked = self.calls(X, since=m)
        self.assertTrue(self.calls(X, FACT, since=m), tracked)        # 窗口之前的三年分钟开始续传
        self.assertEqual(self.planned(X), {"backfill", "minute"})
        # 分钟：只续传窗口之前的三年，窗口本身不重取（按自然月分片，只有窗口起点所在月份可能重叠几天）
        for _, _, start, end in self.calls(X, FACT, since=m):
            self.assertLessEqual(end[:7], window_lo[:7], ("查看窗口不重复请求", start, end))
        # 日线：完整回填沿用既有的一次请求取满 10 年（与已有行重叠部分幂等写入），请求次数不因已有窗口增加
        days = self.calls(X, "day", since=m)
        self.assertLessEqual(len(days), 1, days)
        self.assertTrue(all(c[2] >= DAY_CAP for c in days), days)
        self.worker = self.restart()
        m = self.mark()
        self.history_rounds(2, start_minute=20)
        self.assertEqual(self.calls(X, "day", since=m), [], "重启后不重复日线回填")
        self.assertEqual(self.planned(X), {"backfill", "minute"})

        # 6. 移出自选：此后历史轮次零请求，遗留缺口保留（假源几轮就补完三年分钟，这里补登一段未完成的续传）
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, X, FACT, "2023-10-09 09:30", "2023-10-31 15:00", "backfill")
        self.assertEqual(self.api.delete(f"/api/watchlist/{X}").status_code, 200)
        leftover = facts.open_gaps(self.conn, X)
        self.assertTrue(leftover)
        m = self.mark()
        self.history_rounds(4, start_minute=30)
        self.assertEqual(self.calls(X, since=m), [])
        self.assertEqual(len(facts.open_gaps(self.conn, X)), len(leftover))

        # 7. 重启：查看记录仍在，关注状态以自选为准
        self.worker = self.restart()
        recent = TestClient(self.api.app).get("/api/views").json()["recent"]
        self.assertEqual([(r["code"], r["watched"]) for r in recent], [(X, False)])
        self.assertEqual([e["code"] for e in view_log.ViewLog(self.dir / "views.sqlite").events()], [X])
        m = self.mark()
        self.history_rounds(2, start_minute=40)
        self.assertEqual(self.calls(X, since=m), [])

        # 8. 离开页面：查看期内盘中取数，窗口期后停止；自选照常
        self.clock.t = at("2026-09-28", 10).timestamp()
        self.chart(X, "day")
        self.assertEqual(self.calls(X, "live"), [])      # 读取本身不取盘中增量：页面与调度线程不各取一次（回归守护）
        self.worker.tick(at("2026-09-28", 10))
        seen = len(self.calls(X, "live"))
        self.assertGreater(seen, 0)
        later = at("2026-09-28", 10) + timedelta(seconds=config.VIEWING_WINDOW_S + 10)
        self.clock.t = later.timestamp()
        self.worker.tick(later)
        self.assertEqual(len(self.calls(X, "live")), seen)
        self.assertGreater(len(self.calls(A, "live")), 1)

    def test_hk_search_view_keeps_vendor_cache_within_window(self):
        hk = "hk00700"
        self.assertEqual(self.api.post("/api/views", json={"code": hk, "name": "腾讯控股", "freq": "day",
                                                            "adjust": "qfq"}).status_code, 200)
        self.chart(hk, "day")
        vendor = [c for c in self.hk.calls if c[0] in ("day", "m30")]
        self.assertEqual({c[0] for c in vendor}, {"day", "m30"}, vendor)       # 成套缓存两个周期都建
        self.assertTrue(all(c[1][:10] >= WINDOW_DAY_FLOOR for c in vendor), vendor)
        self.assertEqual(self.planned(hk), set())
        m = len(self.hk.calls)
        self.chart(hk, "m60")
        self.chart(hk, "m30")
        self.history_rounds(2)
        self.assertEqual(self.hk.calls[m:], [], "再次查看与历史轮次不扩缓存")
        self.assertEqual(self.api.post("/api/watchlist", json={"code": hk, "name": "腾讯控股"}).status_code, 200)
        self.history_rounds(3, start_minute=10)
        self.assertTrue([c for c in self.hk.calls[m:] if c[0] == "day" and c[1] < WINDOW_DAY_FLOOR],
                        "加入自选后缓存随历史扩到回填目标")

    # ---------- 重拉不错误报成功（API 回归） ----------
    def test_refetch_with_conflict_in_window_is_not_ok_over_api(self):
        self.chart(X)
        orig = self.provider.day_history

        def conflicting(code, start, end):
            return [RawDayRow(r.code, r.trade_date, 10, 11, 10, 11, DAY_VOL, "lot", 1, "CNY", 10, 0, "final",
                              new_batch_id()) if r.trade_date == "2026-09-15" and r.code == X else r
                    for r in orig(code, start, end)]
        self.provider.day_history = conflicting
        body = self.chart(X, refetch=1)
        self.assertNotEqual(self.last_headers.get("X-Refetch-Status"), "ok")
        self.assertTrue(body["kline"])
        self.provider.day_history = orig                         # 本次返回正常，但既有冲突仍未裁决
        self.chart(X, refetch=1)
        self.assertNotEqual(self.last_headers.get("X-Refetch-Status"), "ok")

    def test_intraday_refetch_all_rejected_is_not_ok_over_api(self):
        self.clock.t = at("2026-09-28", 10).timestamp()
        self.chart(X)
        day = "2026-09-28"
        self.provider.minute_live = lambda code, fact_freq, *, now: [
            RawMinuteRow(code, day, f"{day} 10:00", -1, -1, -1, -1, 1, "lot", 1, "forming", "traded", new_batch_id())]
        body = self.chart(X, refetch=1)
        self.assertNotEqual(self.last_headers.get("X-Refetch-Status"), "ok")
        self.assertTrue(body["kline"])

    def test_minute_evidence_before_first_day_row_is_not_ok_over_api(self):
        hk, day = "hk00700", "2026-09-21"
        rows = [RawMinuteRow(hk, day, f"{day} {t}", 100, 100, 100, 100, 10, "share", None, "closed", "traded",
                             new_batch_id()) for t in sessions.slots("HK", "m30")]
        facts.commit_minute_rows(self.conn, rows, market="HK", kind="stock", item="minute_history", fact_freq="m30",
                                 source="longbridge", binding_gen=1, today="2026-09-26")
        orig = self.hk.day_history
        self.hk.day_history = lambda code, start, end: [r for r in orig(code, start, end) if r.trade_date >= "2026-09-22"]
        self.chart(hk, "m60")
        body = self.chart(hk, "m60", refetch=1)
        self.assertNotEqual(self.last_headers.get("X-Refetch-Status"), "ok")
        self.assertTrue(body["kline"])

    def test_intraday_refetch_on_quarantined_slots_is_not_ok_over_api(self):   # 第六轮；修复后补写，含正常对照
        day = "2026-09-28"
        self.clock.t = at(day, 10, 10).timestamp()
        self.chart(X, "m60")
        slots = ["10:00", "10:15"]
        self.provider.minute_live = lambda code, fact_freq, *, now: [
            RawMinuteRow(code, day, f"{day} {t}", 10, 10, 10, 10, 1, "lot", 1, "forming", "traded", new_batch_id())
            for t in slots]
        self.chart(X, "m60", refetch=1)
        self.assertEqual(self.last_headers.get("X-Refetch-Status"), "ok")          # 对照：可读接纳
        with facts.write_txn(self.conn):
            facts.quarantine(self.conn, X, FACT, f"{day} 10:15", "review_conflict")
        for live in (["10:00", "10:15"], ["10:15"]):                              # 部分、全部落在隔离槽位
            slots[:] = live
            body = self.chart(X, "m60", refetch=1)
            self.assertNotEqual(self.last_headers.get("X-Refetch-Status"), "ok", live)
            self.assertTrue(body["kline"])

    # ---------- 重拉在途时的读取 ----------
    def _hold_first_day_request(self, code):
        gate, entered = threading.Event(), threading.Event()

        def hold(call):
            if call[:2] == ("day", code) and not entered.is_set():
                entered.set()
                gate.wait(10)
        self.provider.hook = hold
        self.addCleanup(gate.set)
        return gate, entered

    def _get_async(self, url):
        box = {}

        def run():
            box["r"] = self.api.get(url)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, box

    def test_busy_refetch_on_cold_window_returns_before_first_refetch_finishes(self):
        gate, entered = self._hold_first_day_request(X)
        first, _ = self._get_async(f"/api/chart?code={X}&freq=day&refetch=1")
        self.assertTrue(entered.wait(5))
        for url in (f"/api/chart?code={X}&freq=day&refetch=1", f"/api/chart?code={X}&freq=day"):
            t, box = self._get_async(url)
            t.join(3)
            self.assertFalse(t.is_alive(), url + " 等在首个重拉的锁上")
            self.assertEqual(box["r"].status_code, 503, box["r"].text[:200])
        gate.set()
        first.join(10)

    def test_busy_refetch_on_warm_window_serves_snapshot_immediately(self):   # 回归守护（修前已成立）
        self.chart(X)
        gate, entered = self._hold_first_day_request(X)
        first, _ = self._get_async(f"/api/chart?code={X}&freq=day&refetch=1")
        self.assertTrue(entered.wait(5))
        t, box = self._get_async(f"/api/chart?code={X}&freq=day&refetch=1")
        t.join(3)
        self.assertFalse(t.is_alive(), "第二个重拉等在首个重拉的锁上")
        self.assertEqual(box["r"].status_code, 200, box["r"].text[:200])
        self.assertEqual(box["r"].headers.get("X-Refetch-Status"), "busy")
        self.assertTrue(box["r"].json()["kline"])
        gate.set()
        first.join(10)


if __name__ == "__main__":
    unittest.main()
