"""显示层 stale-while-revalidate（v1.3.1 补充）：过期缓存立即回旧 + 后台异步刷新。

- 过期但有缓存：get_quotes/get_f10 同步返回旧数据（degraded=True + ts 为旧缓存时间，
  不新增响应字段），并触发后台线程异步刷新（按缓存 key 防踩踏：quotes.json 全局一个、
  f10_{code}.json 按 code）；
- 后台刷新失败仅记 warning；退避计数与请求路径同语义（连败计入 _record_failure、
  成功清零），连败触发的退避同样压制后台刷新的触发；
- 完全无缓存（首冷）才在请求路径同步抓取；退避优先级不变：退避中+有缓存→降级返回
  （不触发刷新），退避中+无缓存→照抛。
全程 mock display_feed._fetch_json + 门面 CACHE_DIR 钉到临时目录（缓存在其下 display/），不打外网；
后台线程一律 join（含 tearDown 兜底），防泄漏线程在 mock 拆除后打真实上游。
"""
import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from chanapp.engine import display_feed
from chanapp.engine import swr
from chanapp.tests import cache_support

OLD_QUOTES = {"sz001309": {"price": 100.0, "pct": 1.0, "prev_close": 99.01,
                           "name": "旧快照", "limit_up": False}}
NEW_ULIST_PAYLOAD = {"data": {"diff": [
    {"f2": 128.66, "f3": 3.21, "f12": "001309", "f13": 0, "f14": "德明利", "f18": 124.66},
]}}

OLD_F10_RESULT = {"f10": {"price": 100.0, "industry": "旧行业"}, "flow": {"main": 1.0},
                  "industry_pct": 0.5}
NEW_STOCK_GET = {"data": {
    "f43": 128.66, "f48": 5.33e8, "f49": 2.7e8, "f50": 1.85,
    "f51": 137.13, "f52": 112.19, "f116": 2.864e10, "f117": 1.2e10,
    "f127": "半导体", "f129": "存储芯片", "f161": 2.63e8,
    "f164": 45.6, "f167": 3.21, "f168": 4.56, "f171": 5.67, "f198": "",
    "f137": 1.26e8, "f140": 8.3e7, "f143": 4.3e7, "f146": -2.1e7, "f149": -1.05e8,
}}


class TestDisplayFeedSWR(unittest.TestCase):
    def setUp(self):
        root = cache_support.temp_dir(self)
        cache_support.isolate_cache_dir(self, root)
        self._dir = Path(root) / "display"
        self._dir.mkdir()
        for method in ("_backup_quotes", "_backup_f10"):
            patcher = mock.patch.object(display_feed, method, side_effect=OSError("offline backup"))
            patcher.start()
            self.addCleanup(patcher.stop)
        display_feed._reset_blocked()  # 每个测试清退避状态
        # 内核级在飞表：用例间隔离（tearDown 再 join 兜底）
        swr._inflight.clear()

    def tearDown(self):
        # 兜底：用例中途失败也不把在飞的后台刷新泄漏到后续用例（swr._inflight 是内核级）
        for t in list(swr._inflight.values()):
            t.join(5)

    # ---------- 种子/同步点 ----------

    def _seed_quotes(self, expired=True):
        """直接写一份 quotes 缓存；返回缓存 ts（过期时为旧时间）。"""
        ts = time.time() - display_feed.QUOTE_TTL - 10 if expired else time.time()
        (self._dir / "quotes.json").write_text(
            json.dumps({"ts": ts, "data": OLD_QUOTES}, ensure_ascii=False),
            encoding="utf-8")
        return ts

    def _seed_f10(self, code="sz001309", expired=True):
        ts = time.time() - display_feed.F10_TTL - 10 if expired else time.time()
        (self._dir / f"f10_{code}.json").write_text(
            json.dumps({"ts": ts, "data": OLD_F10_RESULT}, ensure_ascii=False),
            encoding="utf-8")
        return ts

    def _join_refresh(self, key):
        t = swr._inflight.get(swr.make_key(key))
        self.assertIsNotNone(t, f"应触发后台刷新线程（key={key}）")
        t.join(5)
        self.assertFalse(t.is_alive(), "后台刷新 5s 内应结束")

    # ---------- quotes ----------

    def test_stale_quotes_served_immediately_with_degraded_marker(self):
        """过期缓存：同步返回旧数据（degraded=True + ts 为旧缓存时间），不现场抓取。

        若实现仍在请求路径抓取，返回的会是新数据且 degraded=False。
        """
        ts_old = self._seed_quotes()
        with mock.patch.object(display_feed, "_fetch_json",
                               return_value=NEW_ULIST_PAYLOAD):
            r = display_feed.get_quotes(["sz001309"])
            self.assertEqual(r["quotes"], OLD_QUOTES)
            self.assertTrue(r["degraded"])
            self.assertEqual(r["ts"], ts_old)
            self._join_refresh("quotes.json")

    def test_quotes_cached_45_seconds_ago_are_still_fresh(self):
        """报价 TTL 与页面轮询、采集器盘中增量同为 60 秒：45 秒前的缓存直接服务，不降级、不刷新。"""
        (self._dir / "quotes.json").write_text(
            json.dumps({"ts": time.time() - 45, "data": OLD_QUOTES}, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(display_feed, "_fetch_json", return_value=NEW_ULIST_PAYLOAD) as m:
            r = display_feed.get_quotes(["sz001309"])
        self.assertEqual((m.call_count, r["degraded"], r["quotes"]), (0, False, OLD_QUOTES))
        self.assertIsNone(swr._inflight.get(swr.make_key("quotes.json")))

    def test_quotes_background_refresh_updates_cache(self):
        """后台刷新落盘后，再次请求拿到新数据（degraded=False），全程仅后台那一次抓取。"""
        self._seed_quotes()
        with mock.patch.object(display_feed, "_fetch_json",
                               return_value=NEW_ULIST_PAYLOAD) as m:
            display_feed.get_quotes(["sz001309"])
            self._join_refresh("quotes.json")
            r = display_feed.get_quotes(["sz001309"])
        self.assertEqual(m.call_count, 1)
        self.assertFalse(r["degraded"])
        self.assertEqual(r["quotes"]["sz001309"]["price"], 128.66)

    def test_quotes_single_refresh_in_flight(self):
        """防踩踏：后台刷新在飞期间，重复过期请求仍秒回旧、不再另起抓取。"""
        self._seed_quotes()
        release = threading.Event()
        calls = []

        def slow(url):
            calls.append(url)
            release.wait(5)  # 撑住在飞窗口
            return NEW_ULIST_PAYLOAD

        with mock.patch.object(display_feed, "_fetch_json", side_effect=slow):
            r1 = display_feed.get_quotes(["sz001309"])
            r2 = display_feed.get_quotes(["sz001309"])  # 在飞期间：仍秒回旧
            self.assertTrue(r1["degraded"] and r2["degraded"])
            deadline = time.time() + 5  # 等后台线程确实进入抓取再断言
            while not calls and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(calls), 1)
            release.set()
            self._join_refresh("quotes.json")
        self.assertEqual(len(calls), 1)

    def test_quotes_background_failure_keeps_stale_and_counts_failure(self):
        """后台刷新失败：仅记 warning，旧缓存继续可服务；失败计数与请求路径同语义
        （连败计入退避计数，连败 BACKOFF_AFTER_FAILURES 次后进入退避）。"""
        ts_old = self._seed_quotes()
        with mock.patch.object(display_feed, "_fetch_json",
                               side_effect=OSError("reset")):
            with self.assertLogs("chanapp.engine.display_feed", level="WARNING") as cm:
                r = display_feed.get_quotes(["sz001309"])  # 回旧不抛错；warning 在后台线程异步发
                self.assertTrue(r["degraded"])
                self.assertEqual(r["ts"], ts_old)
                self._join_refresh("quotes.json")
            self.assertTrue(any("后台刷新失败" in m for m in cm.output))
            self.assertEqual(display_feed._primary_backoff.failure_count(), 1)  # 后台失败同语义计入
            r2 = display_feed.get_quotes(["sz001309"])  # 旧缓存继续可服务，再触发一次后台刷新
            self.assertTrue(r2["degraded"])
            self._join_refresh("quotes.json")
        self.assertEqual(display_feed._primary_backoff.failure_count(), 2)
        self.assertTrue(display_feed._primary_backoff.blocked())  # 连败 → 退避

    def test_quotes_cold_fetch_stays_synchronous(self):
        """首冷（完全无缓存）：仍在请求路径同步抓取，degraded=False，不触发后台刷新。"""
        with mock.patch.object(display_feed, "_fetch_json",
                               return_value=NEW_ULIST_PAYLOAD) as m:
            r = display_feed.get_quotes(["sz001309"])
        self.assertEqual(m.call_count, 1)
        self.assertFalse(r["degraded"])
        self.assertEqual(r["quotes"]["sz001309"]["price"], 128.66)
        self.assertIsNone(swr._inflight.get(swr.make_key("quotes.json")))

    def test_concurrent_cold_fetch_only_once(self):
        """baseline 冷路径 per-key 锁 + 双检：并发首冷只抓一次（行为变化 1）。"""
        calls = []
        gate = threading.Event()
        def slow_fetch(codes):
            calls.append(1)
            gate.wait(5)
            return {c: {"price": 1.0, "pct": 0.0, "prev_close": 1.0, "name": "x",
                        "limit_up": False, "source": "baseline_display", "source_ts": None}
                    for c in codes}
        with mock.patch.object(display_feed, "_fetch_quotes", slow_fetch), \
             mock.patch.object(display_feed, "_backup_quotes", side_effect=AssertionError("不应调备用源")):
            results = []
            threads = [threading.Thread(target=lambda: results.append(
                display_feed.get_quotes(["sh600000"]))) for _ in range(2)]
            for t in threads:
                t.start()
            gate.set()
            for t in threads:
                t.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)

    def test_quotes_fresh_hit_no_refresh(self):
        """TTL 内命中：degraded=False，不抓取、不触发后台刷新。"""
        self._seed_quotes(expired=False)
        with mock.patch.object(display_feed, "_fetch_json",
                               side_effect=AssertionError("新鲜缓存不应抓取")):
            r = display_feed.get_quotes(["sz001309"])
        self.assertFalse(r["degraded"])
        self.assertEqual(r["quotes"], OLD_QUOTES)
        self.assertIsNone(swr._inflight.get(swr.make_key("quotes.json")))

    def test_blocked_with_stale_cache_degrades_without_refresh(self):
        """退避优先级不变：退避中+有旧缓存→降级返回，不抓取、不触发后台刷新。"""
        ts_old = self._seed_quotes()
        with mock.patch.object(display_feed, "_fetch_json", side_effect=OSError("reset")) as m:
            with self.assertRaises(RuntimeError):
                display_feed.get_f10("sz001309")  # 无缓存首冷连败 1
            with self.assertRaises(RuntimeError):
                display_feed.get_f10("sz001309")  # 连败 2 → 进入退避
            self.assertEqual(m.call_count, 2)
            r = display_feed.get_quotes(["sz001309"])  # 退避中+有旧缓存 → 降级返回
            self.assertTrue(r["degraded"])
            self.assertEqual(r["ts"], ts_old)
            self.assertEqual(m.call_count, 2)  # 不再发请求
        self.assertIsNone(swr._inflight.get(swr.make_key("quotes.json")))  # 不触发后台刷新

    # ---------- f10 ----------

    def test_stale_f10_served_and_background_refresh_updates_cache(self):
        """F10 过期缓存：秒回旧（degraded + 旧 ts），后台刷新落盘后新鲜命中新数据。"""
        ts_old = self._seed_f10()
        with mock.patch.object(display_feed, "_fetch_json",
                               return_value=NEW_STOCK_GET) as m:
            r = display_feed.get_f10("sz001309")
            self.assertTrue(r["degraded"])
            self.assertEqual(r["ts"], ts_old)
            self.assertEqual(r["f10"]["price"], 100.0)
            self._join_refresh("f10_sz001309.json")
            r2 = display_feed.get_f10("sz001309")
        self.assertEqual(m.call_count, 1)  # 全程只有后台那一次抓取
        self.assertFalse(r2["degraded"])
        self.assertEqual(r2["f10"]["price"], 128.66)

    def test_f10_inflight_keyed_per_code(self):
        """防踩踏按 code：同 code 在飞去重，异 code 各自刷新。"""
        self._seed_f10("sz001309")
        self._seed_f10("sh600519")
        release = threading.Event()
        calls = []

        def slow(url):
            calls.append(url)
            release.wait(5)  # 撑住在飞窗口
            return NEW_STOCK_GET

        with mock.patch.object(display_feed, "_fetch_json", side_effect=slow):
            display_feed.get_f10("sz001309")
            deadline = time.time() + 5  # 等 sz001309 的后台刷新确实进入抓取
            while not calls and time.time() < deadline:
                time.sleep(0.01)
            display_feed.get_f10("sz001309")   # 同 code 在飞：判活去重，不另起线程
            self.assertEqual(len(calls), 1)    # 去重是同步发生的，无竞态
            display_feed.get_f10("sh600519")   # 异 code：允许并行刷新
            deadline = time.time() + 5
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(calls), 2)
            release.set()
            self._join_refresh("f10_sz001309.json")
            self._join_refresh("f10_sh600519.json")
        self.assertEqual(len(calls), 2)  # 每个 code 恰好一次抓取

    def test_f10_cold_fetch_stays_synchronous(self):
        """F10 首冷：请求路径同步抓取，不触发后台刷新。"""
        with mock.patch.object(display_feed, "_fetch_json",
                               return_value=NEW_STOCK_GET) as m:
            r = display_feed.get_f10("sz001309")
        self.assertEqual(m.call_count, 1)
        self.assertFalse(r["degraded"])
        self.assertEqual(r["f10"]["price"], 128.66)
        self.assertIsNone(swr._inflight.get(swr.make_key("f10_sz001309.json")))


if __name__ == "__main__":
    unittest.main()
