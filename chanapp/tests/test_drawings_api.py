"""用户画线存储（#67）：经 HTTP 接口读写，按标的逐条存、条件写、软删除。

实现前列出的失败方式：
- 迟到的旧写入覆盖另一台设备的新版本（丢失更新），或新建时同 id 静默覆盖；
- 删除后另一台设备的迟到写入把画线复活，或已删除的画线仍出现在读取结果里；
- 清空一个标的波及其他标的；
- 服务重启后画线丢失，或新建文件对其他用户可读；
- 非法 body（非对象、未知版本、超大）被存下；
- 作者实例按 WATCHLIST_PATH 派生路径时画线落到别处。
"""
import os
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from chanapp.tests import cache_support

LINE = {"v": 1, "type": "segment", "anchors": [{"freq": "day", "dt": "2026-09-01", "price": 800.5}]}
MOVED = {"v": 1, "type": "segment", "anchors": [{"freq": "day", "dt": "2026-09-02", "price": 812.0}]}


class DrawingsApiTests(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        self.tmp = Path(cache_support.temp_dir(self))
        cache_support.set_env(self, "WATCHLIST_PATH", str(self.tmp / "chanapp.watchlist.json"))
        cache_support.set_env(self, "DRAWINGS_PATH", "")
        cache_support.isolate_cache_dir(self)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def put(self, code, id_, body, base=None, status=200):
        r = self.c.put(f"/api/drawings/{code}/{id_}", json={"body": body, "base_revision": base})
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def listed(self, code):
        r = self.c.get(f"/api/drawings?code={code}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        return r.json()["drawings"]

    def test_conditional_writes_reject_stale_and_duplicate_versions(self):
        created = self.put("sz300308", "line-0001", LINE)
        self.assertEqual((created["revision"], created["body"]), (1, LINE))
        dup = self.put("sz300308", "line-0001", MOVED, status=409)
        self.assertEqual((dup["current"]["revision"], dup["current"]["body"]), (1, LINE))
        moved = self.put("sz300308", "line-0001", MOVED, base=1)
        self.assertEqual(moved["revision"], 2)
        stale = self.put("sz300308", "line-0001", LINE, base=1, status=409)
        self.assertEqual((stale["detail"], stale["current"]["revision"], stale["current"]["body"]),
                         ("画线已被修改", 2, MOVED))
        self.assertEqual([(d["id"], d["revision"], d["body"]) for d in self.listed("sz300308")],
                         [("line-0001", 2, MOVED)])
        missing = self.put("sz300308", "line-0002", LINE, base=1, status=409)
        self.assertIsNone(missing["current"])

    def test_delete_leaves_a_tombstone_that_blocks_late_writes(self):
        self.put("sz300308", "line-0001", LINE)
        self.put("sz300308", "line-0002", MOVED)
        stale = self.c.delete("/api/drawings/sz300308/line-0001?base_revision=0")
        self.assertEqual(stale.status_code, 409)
        gone = self.c.delete("/api/drawings/sz300308/line-0001?base_revision=1")
        self.assertEqual(gone.status_code, 200)
        self.assertEqual(gone.json()["revision"], 2)
        self.assertIn("deleted_at", gone.json())
        again = self.c.delete("/api/drawings/sz300308/line-0001?base_revision=1")
        self.assertEqual(again.status_code, 200)       # 已删除的再删视为成功
        self.assertEqual([d["id"] for d in self.listed("sz300308")], ["line-0002"])
        late = self.put("sz300308", "line-0001", MOVED, base=1, status=409)
        self.assertEqual(late["current"]["revision"], 2)
        self.assertIn("deleted_at", late["current"])
        self.put("sz300308", "line-0001", MOVED, status=409)      # 墓碑占着 id，不能当新建复活
        self.assertEqual(self.c.delete("/api/drawings/sz300308/none-0001?base_revision=1").status_code, 404)

    def test_clear_only_affects_one_code(self):
        self.put("sz300308", "line-0001", LINE)
        self.put("sz300308", "line-0002", MOVED)
        self.put("hk00700", "line-0003", LINE)
        r = self.c.delete("/api/drawings/sz300308")
        self.assertEqual(r.json(), {"code": "sz300308", "deleted": 2})
        self.assertEqual(self.listed("sz300308"), [])
        self.assertEqual([d["id"] for d in self.listed("hk00700")], ["line-0003"])
        self.put("sz300308", "line-0001", LINE, base=1, status=409)

    def test_restart_keeps_drawings_in_a_private_file_beside_the_watchlist(self):
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        self.put("sh600519", "line-0001", LINE)
        path = self.tmp / "chanapp.watchlist.drawings.sqlite"
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        from chanapp.api import main
        main._drawing_stores.clear()                     # 等同服务重启：新实例读同一文件
        self.assertEqual([(d["id"], d["body"]) for d in self.listed("sh600519")], [("line-0001", LINE)])

    def test_explicit_path_wins(self):
        explicit = self.tmp / "elsewhere" / "d.sqlite"
        os.environ["DRAWINGS_PATH"] = str(explicit)
        self.put("sh600519", "line-0001", LINE)
        self.assertTrue(explicit.is_file())
        self.assertFalse((self.tmp / "chanapp.watchlist.drawings.sqlite").exists())

    def test_invalid_input_is_not_stored(self):
        for body in ([1, 2], {"type": "segment"}, {"v": 2}, {"v": True}, {"v": 1.0}, {"v": 1, "pad": "x" * 20000}):
            self.put("sz300308", "line-0001", body, status=422)
        self.put("sz300309x", "line-0001", LINE, status=422)
        self.put("sz300308", "short", LINE, status=422)
        self.assertEqual(self.c.get("/api/drawings?code=bad").status_code, 422)
        self.assertEqual(self.listed("sz300308"), [])

    def test_body_round_trips_unicode_exactly(self):
        body = {"v": 1, "type": "rect", "style": {"color": "#ff000080", "label": "箱体·上沿"}}
        self.put("hk00700", "rect-0001", body)
        self.assertEqual(self.listed("hk00700")[0]["body"], body)


if __name__ == "__main__":
    unittest.main()
