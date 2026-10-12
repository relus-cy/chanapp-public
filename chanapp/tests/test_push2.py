"""push2 显示层解析口径（payload 结构录制自 2026-08-27 tmp/push2_probe 实测）。"""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from chanapp.engine import display_feed
from chanapp.engine import swr
from chanapp.tests import cache_support


ULIST_PAYLOAD = {
    "rc": 0,
    "data": {"diff": [
        {"f2": 128.66, "f3": 3.21, "f12": "001309", "f13": 0, "f14": "德明利", "f18": 124.66},
        {"f2": 149.59, "f3": 10.0, "f12": "300953", "f13": 0, "f14": "震裕科技", "f18": 124.66},
        {"f2": 2865.23, "f3": 0.42, "f12": "000001", "f13": 1, "f14": "上证指数", "f18": 2853.21},
        {"f2": "-", "f3": "-", "f12": "688806", "f13": 1, "f14": "泰诺麦博-U", "f18": "-"},
        {"f2": 1024.5, "f3": 1.85, "f12": "BK1036", "f13": 90, "f14": "半导体", "f18": 1005.9},
    ]},
}

STOCK_GET_DATA = {
    "f43": 128.66, "f48": 5.33e8, "f49": 2.7e8, "f50": 1.85,
    "f51": 137.13, "f52": 112.19, "f116": 2.864e10, "f117": 1.2e10,
    "f127": "半导体", "f129": "存储芯片,汽车电子", "f161": 2.63e8,
    "f164": 45.6, "f167": 3.21, "f168": 4.56, "f171": 5.67, "f198": "BK1036",
    "f135": 1.5e8, "f136": 2.4e7, "f137": 1.26e8,
    "f138": 9.0e7, "f139": 7.0e6, "f140": 8.3e7,
    "f141": 5.0e7, "f142": 7.0e6, "f143": 4.3e7,
    "f144": -1.0e7, "f145": 1.1e7, "f146": -2.1e7,
    "f147": -8.0e7, "f148": 2.5e7, "f149": -1.05e8,
}


class TestParseF10Flow(unittest.TestCase):
    def test_f10(self):
        f = display_feed.parse_f10(STOCK_GET_DATA)
        self.assertEqual(f["total_mv"], 2.864e10)
        self.assertEqual(f["pe_ttm"], 45.6)
        self.assertEqual(f["outer"], 2.7e8)
        self.assertEqual(f["inner"], 2.63e8)
        self.assertEqual(f["concepts"], ["存储芯片", "汽车电子"])
        self.assertEqual(f["board_code"], "BK1036")

    def test_flow(self):
        fl = display_feed.parse_flow(STOCK_GET_DATA)
        self.assertEqual(fl["main"], 1.26e8)
        self.assertEqual(fl["super"], 8.3e7)
        self.assertEqual(fl["small"], -1.05e8)
        self.assertAlmostEqual(fl["super"] + fl["large"], fl["main"], places=1)  # 超大+大=主力勾稽


class TestFetch(unittest.TestCase):
    def setUp(self):
        root = cache_support.temp_dir(self)
        cache_support.isolate_cache_dir(self, root)
        self._dir = str(Path(root) / "display")
        for method in ("_backup_f10",):
            patcher = mock.patch.object(display_feed, method, side_effect=OSError("offline backup"))
            patcher.start()
            self.addCleanup(patcher.stop)
        display_feed._reset_blocked()  # 每个测试清退避状态

    def tearDown(self):
        # 兜底：SWR 后台刷新线程不泄漏到后续用例（泄漏线程会在 mock 拆除后打真实上游）
        for t in list(swr._inflight.values()):
            t.join(5)

    def _join_refresh(self, key):
        """在 mock 上下文内等后台刷新收尾：防线程在 mock 拆除后打真实上游。"""
        t = swr._inflight.get(swr.make_key(key))
        self.assertIsNotNone(t, f"过期缓存应触发后台刷新（key={key}）")
        t.join(5)
        self.assertFalse(t.is_alive(), "后台刷新 5s 内应结束")

    def _stock_payload(self, board=True):
        data = dict(STOCK_GET_DATA)
        if not board:
            data["f198"] = ""
        return {"data": data}

    def test_f10_merges_flow_and_board(self):
        def fake(url):
            return {"rc": 0, "data": dict(STOCK_GET_DATA)} if "stock/get" in url else ULIST_PAYLOAD
        with mock.patch.object(display_feed, "_fetch_json", side_effect=fake) as m:
            r = display_feed.get_f10("sz001309")
        self.assertEqual(r["f10"]["pe_ttm"], 45.6)
        self.assertEqual(r["flow"]["main"], 1.26e8)
        self.assertEqual(r["industry_pct"], 1.85)  # ULIST_PAYLOAD 里 BK1036 的 f3
        self.assertEqual(m.call_count, 2)
        self.assertIn("90.BK1036", m.call_args_list[1].args[0])

    def test_failure_falls_back_to_stale(self):
        with mock.patch.object(display_feed, "_fetch_json",
                               return_value=self._stock_payload(board=False)):
            display_feed.get_f10("sz001309")
        p = os.path.join(self._dir, "f10_sz001309.json")
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": 0, "data": obj["data"]}, f)  # 把 JSON 内 ts 置 0 → 过期
        with mock.patch.object(display_feed, "_fetch_json", side_effect=OSError("reset")):
            r = display_feed.get_f10("sz001309")  # SWR：回旧 + 后台刷新（失败仅记日志）
            self._join_refresh("f10_sz001309.json")
        self.assertTrue(r["degraded"])
        self.assertEqual(r["f10"]["price"], 128.66)

    def test_blocked_backoff_no_cache_raises(self):
        with mock.patch.object(display_feed, "_fetch_json", side_effect=OSError("reset")):
            with self.assertRaises(RuntimeError):
                display_feed.get_f10("sz001309")        # 连续失败 1：抛错但不退避
            with self.assertRaises(RuntimeError):
                display_feed.get_f10("sh600000")        # 连续失败 2 → 进入退避
            with self.assertRaises(display_feed.Push2Blocked):
                display_feed.get_f10("sz002594")        # 退避窗口内：不再发请求

    def test_blocked_with_stale_cache_degrades(self):
        with mock.patch.object(display_feed, "_fetch_json",
                               return_value=self._stock_payload(board=False)):
            display_feed.get_f10("sz001309")
        p = os.path.join(self._dir, "f10_sz001309.json")
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": 0, "data": obj["data"]}, f)  # 过期
        with mock.patch.object(display_feed, "_fetch_json", side_effect=OSError("reset")):
            r = display_feed.get_f10("sz001309")   # SWR 回旧 + 后台刷新失败：连败 1（后台与请求路径同语义），未退避
            self._join_refresh("f10_sz001309.json")  # 等后台失败落计数，顺序才确定
            self.assertTrue(r["degraded"])
            with self.assertRaises(RuntimeError):
                display_feed.get_f10("sh600000")   # 连败 2 → 进入退避
        with mock.patch.object(display_feed, "_fetch_json", side_effect=AssertionError("不该再请求")):
            r2 = display_feed.get_f10("sz001309")  # 退避窗口内 + 有 stale 缓存 → 降级返回，不触发刷新
        self.assertTrue(r2["degraded"])
        self.assertEqual(r2["f10"]["price"], 128.66)

    def test_single_failure_does_not_block(self):
        """单次抖动不退避：下一次请求照常发出。"""
        calls = []
        payload = self._stock_payload(board=False)

        def flaky(url):
            calls.append(url)
            if len(calls) == 1:
                raise OSError("reset")
            return payload

        with mock.patch.object(display_feed, "_fetch_json", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                display_feed.get_f10("sz001309")
            r = display_feed.get_f10("sz001309")  # 未退避：第二次正常成功
        self.assertFalse(r["degraded"])
        self.assertEqual(len(calls), 2)

    def test_success_resets_failure_streak(self):
        """成功后连败计数清零：隔一次的单次失败不退避。"""
        with mock.patch.object(display_feed, "_fetch_json", side_effect=OSError("reset")):
            with self.assertRaises(RuntimeError):
                display_feed.get_f10("sz001309")   # 连败 1
        with mock.patch.object(display_feed, "_fetch_json",
                               return_value=self._stock_payload(board=False)):
            display_feed.get_f10("sz001309")       # 成功 → 计数清零
        p = os.path.join(self._dir, "f10_sz001309.json")
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": 0, "data": obj["data"]}, f)  # 过期，强制下次真抓取
        with mock.patch.object(display_feed, "_fetch_json", side_effect=OSError("reset")):
            r = display_feed.get_f10("sz001309")   # SWR 回旧 + 后台刷新失败：重新计数的连败 1 → degraded 但不退避
            self._join_refresh("f10_sz001309.json")  # 等后台失败落计数，顺序才确定
            self.assertTrue(r["degraded"])
            with self.assertRaises(RuntimeError):
                display_feed.get_f10("sh600000")   # 连败 2 → 此刻才退避
            with self.assertRaises(display_feed.Push2Blocked):
                display_feed.get_f10("sz002594")   # 退避窗口内：不再发请求（sz001309 有 stale 缓存会降级返回，用无缓存代码验证拦截）


if __name__ == "__main__":
    unittest.main()
