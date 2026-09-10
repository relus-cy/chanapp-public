"""Task 4: 自选股管理（watchlist CRUD）测试。"""
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

_PKG_ROOT = Path(__file__).resolve().parent.parent
SEED = _PKG_ROOT / "watchlist.json"


class TestWatchlist(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["WATCHLIST_PATH"] = str(Path(self._tmp.name) / "w.json")
        self.addCleanup(os.environ.pop, "WATCHLIST_PATH")
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_seed_file_valid(self):
        items = json.loads(SEED.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(items), 1)
        for w in items:
            self.assertIn("code", w)
            self.assertIn("name", w)

    def test_list_defaults_to_seed(self):
        seed = json.loads(SEED.read_text(encoding="utf-8"))
        r = self.c.get("/api/watchlist")
        self.assertEqual(r.status_code, 200)
        codes = [w["code"] for w in r.json()]
        self.assertEqual(len(codes), len(seed))
        self.assertIn(seed[0]["code"], codes)

    def test_add_list_delete(self):
        r = self.c.post("/api/watchlist", json={"code": "sh600519", "name": "贵州茅台"})
        self.assertEqual(r.status_code, 200)
        codes = [w["code"] for w in self.c.get("/api/watchlist").json()]
        self.assertIn("sh600519", codes)

        r = self.c.post("/api/watchlist", json={"code": "sh600519", "name": "贵州茅台"})
        self.assertEqual(r.status_code, 409)

        r = self.c.delete("/api/watchlist/sh600519")
        self.assertEqual(r.status_code, 200)
        codes = [w["code"] for w in self.c.get("/api/watchlist").json()]
        self.assertNotIn("sh600519", codes)

        # 持久化：文件内容与新 client 读取一致
        saved = json.loads(Path(os.environ["WATCHLIST_PATH"]).read_text(encoding="utf-8"))
        self.assertNotIn("sh600519", [w["code"] for w in saved])
        c2 = TestClient(self.c.app)
        self.assertNotIn("sh600519", [w["code"] for w in c2.get("/api/watchlist").json()])

    def test_concurrent_adds_preserve_every_item(self):
        from chanapp.api import main

        path = Path(os.environ["WATCHLIST_PATH"])
        path.write_text("[]\n", encoding="utf-8")
        codes = [f"sh600{i:03d}" for i in range(8)]
        start = threading.Barrier(len(codes))
        original_save = main._save_watchlist

        def slow_save(items):
            time.sleep(0.02)
            original_save(items)

        def add(code):
            start.wait()
            main.add_watch(main.WatchItem(code=code, name=code))

        with mock.patch.object(main, "_save_watchlist", side_effect=slow_save):
            with ThreadPoolExecutor(max_workers=len(codes)) as pool:
                list(pool.map(add, codes))

        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual({w["code"] for w in saved}, set(codes))

    def test_failed_atomic_replace_preserves_previous_file(self):
        from chanapp.api import main

        path = Path(os.environ["WATCHLIST_PATH"])
        original = [{"code": "sh600000", "name": "浦发银行", "starred": False, "tags": []}]
        path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

        with mock.patch.object(main.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                main._save_watchlist([
                    {"code": "sh600519", "name": "贵州茅台", "starred": False, "tags": []},
                ])

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_delete_missing_returns_404(self):
        r = self.c.delete("/api/watchlist/sh000000")
        self.assertEqual(r.status_code, 404)

    def test_star_pins_to_top_and_persists(self):
        # seed 文件可能被 UI 写入 starred:true；先落一份全 False 的副本使期望顺序确定
        seed = json.loads(SEED.read_text(encoding="utf-8"))
        for w in seed:
            w["starred"] = False
        Path(os.environ["WATCHLIST_PATH"]).write_text(
            json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        seed_codes = [w["code"] for w in seed]
        self.c.post("/api/watchlist", json={"code": "sh600519", "name": "贵州茅台"})

        r = self.c.post("/api/watchlist/sh600519/star")
        self.assertEqual(r.status_code, 200)
        items = r.json()
        self.assertEqual(items[0]["code"], "sh600519")
        self.assertTrue(items[0]["starred"])
        # 响应口径：所有条目带 starred 字段，星标在最前，其余保持原相对顺序
        self.assertEqual([w["code"] for w in items], ["sh600519"] + seed_codes)
        self.assertTrue(all("starred" in w for w in items))
        self.assertFalse(any(w["starred"] for w in items[1:]))
        # GET 与 POST 返回同一口径
        got = self.c.get("/api/watchlist").json()
        self.assertEqual([w["code"] for w in got], [w["code"] for w in items])
        # 持久化：文件保持 append 序，starred 落盘
        saved = json.loads(Path(os.environ["WATCHLIST_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual([w["code"] for w in saved], seed_codes + ["sh600519"])
        self.assertTrue({w["code"]: w for w in saved}["sh600519"]["starred"])

        # 取消星标：回到 append 序中的原位置
        r = self.c.post("/api/watchlist/sh600519/star")
        self.assertEqual(r.status_code, 200)
        items = r.json()
        self.assertEqual([w["code"] for w in items], seed_codes + ["sh600519"])
        self.assertFalse({w["code"]: w for w in items}["sh600519"]["starred"])

    def test_star_missing_returns_404(self):
        r = self.c.post("/api/watchlist/sh000000/star")
        self.assertEqual(r.status_code, 404)

    def test_add_requires_code_and_name(self):
        r = self.c.post("/api/watchlist", json={"code": "sh600519"})
        self.assertEqual(r.status_code, 422)

    def test_set_tags_roundtrip(self):
        self.c.post("/api/watchlist", json={"code": "sh600519", "name": "贵州茅台"})
        r = self.c.put("/api/watchlist/sh600519/tags", json={"tags": ["白酒", "龙头"]})
        self.assertEqual(r.status_code, 200)
        hit = {w["code"]: w for w in r.json()}["sh600519"]
        self.assertEqual(hit["tags"], ["白酒", "龙头"])
        # GET 同一口径；seed 旧数据无 tags 字段，归一化为 []
        got = {w["code"]: w for w in self.c.get("/api/watchlist").json()}
        self.assertEqual(got["sh600519"]["tags"], ["白酒", "龙头"])
        seed0 = json.loads(SEED.read_text(encoding="utf-8"))[0]["code"]
        self.assertEqual(got[seed0]["tags"], [])

    def test_set_tags_cleans_input(self):
        self.c.post("/api/watchlist", json={"code": "sh600519", "name": "贵州茅台"})
        messy = ["  白酒 ", "", "   ", "白酒", "一二三四五六七八九十甲乙丙丁",
                 "b", "c", "d", "e", "f", "g", "h", "i"]
        r = self.c.put("/api/watchlist/sh600519/tags", json={"tags": messy})
        self.assertEqual(r.status_code, 200)
        tags = {w["code"]: w for w in r.json()}["sh600519"]["tags"]
        # strip 去空、保序去重、每条截断 12 字符、总数截断 8 条
        self.assertEqual(tags, ["白酒", "一二三四五六七八九十甲乙", "b", "c", "d", "e", "f", "g"])

    def test_set_tags_deduplicates_after_truncation(self):
        self.c.post("/api/watchlist", json={"code": "sh600519", "name": "贵州茅台"})
        r = self.c.put("/api/watchlist/sh600519/tags", json={
            "tags": ["一二三四五六七八九十甲乙丙", "一二三四五六七八九十甲乙丁"],
        })
        self.assertEqual(r.status_code, 200)
        tags = {w["code"]: w for w in r.json()}["sh600519"]["tags"]
        self.assertEqual(tags, ["一二三四五六七八九十甲乙"])

    def test_set_tags_missing_returns_404(self):
        r = self.c.put("/api/watchlist/sh000000/tags", json={"tags": ["x"]})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "sh000000 不在自选中")

    def test_tags_normalized_on_read(self):
        # 手改文件：tags 非 list / 元素非 str 或 strip 后为空 → 归一化
        Path(os.environ["WATCHLIST_PATH"]).write_text(json.dumps([
            {"code": "sh600519", "name": "贵州茅台", "tags": "白酒"},
            {"code": "sh600000", "name": "浦发银行", "tags": ["银行", 123, "  ", None, " 白马 "]},
        ], ensure_ascii=False), encoding="utf-8")
        got = {w["code"]: w for w in self.c.get("/api/watchlist").json()}
        self.assertEqual(got["sh600519"]["tags"], [])
        self.assertEqual(got["sh600000"]["tags"], ["银行", "白马"])

    def test_set_tags_persists(self):
        self.c.post("/api/watchlist", json={"code": "sh600519", "name": "贵州茅台"})
        self.c.put("/api/watchlist/sh600519/tags", json={"tags": ["白酒"]})
        saved = json.loads(Path(os.environ["WATCHLIST_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual({w["code"]: w for w in saved}["sh600519"]["tags"], ["白酒"])
        c2 = TestClient(self.c.app)
        got = {w["code"]: w for w in c2.get("/api/watchlist").json()}["sh600519"]
        self.assertEqual(got["tags"], ["白酒"])


if __name__ == "__main__":
    unittest.main()
