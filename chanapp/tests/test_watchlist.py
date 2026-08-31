"""Task 4: 自选股管理（watchlist CRUD）测试。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

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

    def test_delete_missing_returns_404(self):
        r = self.c.delete("/api/watchlist/sh000000")
        self.assertEqual(r.status_code, 404)

    def test_star_pins_to_top_and_persists(self):
        seed_codes = [w["code"] for w in json.loads(SEED.read_text(encoding="utf-8"))]
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


if __name__ == "__main__":
    unittest.main()
