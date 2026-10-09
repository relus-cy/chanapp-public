"""搜索查看记录（目标 2026-09-29 第二阶段）：打开事件日志、按代码去重的最近查看列表、跨重启保留、手动重拉入口。

实现前列出的失败方式：
- V14 打开事件没记下，或最近列表没按代码去重、不是最近在前、丢了名称/周期/复权/首次与最近时间；
- V14 自动刷新（/api/chart 读取）也记成「查看」，事件被轮询灌满；
- V15 服务重启（新实例读同一文件）后记录丢失；
- V17 加入或移出自选删掉查看记录；最近列表不标是否已在自选；
- V16 /api/chart?refetch=1 不触发重拉，或不带参数时也重拉；
- 非法代码与周期写进记录。
阶段评审（astra）补充：
- 链接直接打开只知道代码（名称即代码），把已记下的中文名冲掉；
- 重拉结果不告诉页面：空返回、部分失败、异常与门面不支持都和成功一样（页面据此只能说「已重拉」）。
"""
import os
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.tests import cache_support


class ViewLogApiTests(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        tmp = Path(cache_support.temp_dir(self))
        cache_support.set_env(self, "WATCHLIST_PATH", str(tmp / "w.json"))
        cache_support.set_env(self, "VIEW_LOG_PATH", str(tmp / "views.sqlite"))
        cache_support.isolate_cache_dir(self)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def view(self, code, name, freq="day", adjust="qfq"):
        r = self.c.post("/api/views", json={"code": code, "name": name, "freq": freq, "adjust": adjust})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_open_events_and_deduplicated_recent_list(self):
        self.view("sh600519", "贵州茅台", "day")
        self.view("sz000002", "万科A", "m60", "raw")
        self.view("sh600519", "贵州茅台", "m30")
        recent = self.c.get("/api/views").json()["recent"]
        self.assertEqual([r["code"] for r in recent], ["sh600519", "sz000002"])
        first = recent[0]
        self.assertEqual((first["name"], first["freq"], first["adjust"], first["views"]),
                         ("贵州茅台", "m30", "qfq", 2))
        self.assertLessEqual(first["first_viewed_at"], first["last_viewed_at"])
        self.assertEqual(recent[1]["adjust"], "raw")
        from chanapp.api import view_log
        events = view_log.ViewLog(os.environ["VIEW_LOG_PATH"]).events()
        self.assertEqual([(e["code"], e["freq"]) for e in events],
                         [("sh600519", "day"), ("sz000002", "m60"), ("sh600519", "m30")])

    def test_chart_reads_do_not_record_views(self):
        with mock.patch("chanapp.api.main.engine_data.get_bars", side_effect=RuntimeError("no net in tests")):
            self.c.get("/api/chart?code=sh600519&freq=day")
        self.assertEqual(self.c.get("/api/views").json()["recent"], [])

    def test_records_survive_restart(self):
        self.view("sh600519", "贵州茅台")
        from chanapp.api import view_log
        again = view_log.ViewLog(os.environ["VIEW_LOG_PATH"])        # 新实例、新连接：等同服务重启
        self.assertEqual([r["code"] for r in again.recent()], ["sh600519"])
        fresh = TestClient(self.c.app)
        self.assertEqual([r["code"] for r in fresh.get("/api/views").json()["recent"]], ["sh600519"])

    def test_watchlist_changes_keep_records_and_recent_marks_watched(self):
        self.view("sh600519", "贵州茅台")
        self.c.post("/api/watchlist", json={"code": "sh600519", "name": "贵州茅台"})
        recent = self.c.get("/api/views").json()["recent"]
        self.assertEqual([(r["code"], r["watched"]) for r in recent], [("sh600519", True)])
        self.c.delete("/api/watchlist/sh600519")
        recent = self.c.get("/api/views").json()["recent"]
        self.assertEqual([(r["code"], r["watched"]) for r in recent], [("sh600519", False)])

    def test_link_open_without_name_keeps_known_name(self):
        self.view("sh600519", "贵州茅台")
        self.view("sh600519", "sh600519")                           # URL ?code= 打开：页面只知道代码
        recent = self.c.get("/api/views").json()["recent"]
        self.assertEqual([(r["code"], r["name"], r["views"]) for r in recent], [("sh600519", "贵州茅台", 2)])

    def test_invalid_code_or_freq_rejected(self):
        for body in ({"code": "600519", "name": "x", "freq": "day", "adjust": "qfq"},
                     {"code": "sh600519", "name": "x", "freq": "m7", "adjust": "qfq"},
                     {"code": "sh600519", "name": "x", "freq": "day", "adjust": "hfq"}):
            self.assertEqual(self.c.post("/api/views", json=body).status_code, 422)
        self.assertEqual(self.c.get("/api/views").json()["recent"], [])


class RefetchParamTests(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        cache_support.isolate_cache_dir(self)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_refetch_param_asks_facade_to_refetch_window_first(self):
        order = []
        refetch = mock.Mock(side_effect=lambda code, freq: order.append(("refetch", code)) or {"status": "ok"})
        read = mock.Mock(side_effect=lambda *a, **k: order.append(("read",)) or (_ for _ in ()).throw(
            RuntimeError("no data")))
        with mock.patch("chanapp.api.main.engine_data.refetch_window", refetch, create=True), \
                mock.patch("chanapp.api.main.engine_chart_payload.read_chart_inputs", read):
            self.c.get("/api/chart?code=sh600519&freq=day&refetch=1")
            self.assertEqual(order, [("refetch", "sh600519"), ("read",)])
            order.clear()
            self.c.get("/api/chart?code=sh600519&freq=day")
            self.assertEqual(order, [("read",)])

    def _served(self, refetch):
        """读取成功（桩）时的重拉响应：图表照常返回，重拉结果在 X-Refetch-Status。"""
        with mock.patch("chanapp.api.main.engine_data.refetch_window", refetch, create=True), \
                mock.patch("chanapp.api.main.engine_chart_payload.read_chart_inputs",
                           return_value=({}, {}, None)), \
                mock.patch("chanapp.api.main.engine_chart_payload.build_chart_payload",
                           side_effect=lambda *a, timings, **k: timings.update(compute_ms=0, resonance_ms=0)
                           or {"kline": []}):
            return self.c.get("/api/chart?code=sh600519&freq=week&refetch=1")

    def test_refetch_result_is_reported_with_the_served_chart(self):
        seen = []
        for status in ("ok", "partial", "failed"):
            r = self._served(mock.Mock(side_effect=lambda code, freq, s=status: seen.append(freq) or {"status": s}))
            self.assertEqual((r.status_code, r.headers.get("X-Refetch-Status")), (200, status))
        self.assertEqual(seen, ["week"] * 3)                          # 主图周期交给门面，重拉覆盖它的窗口
        r = self._served(mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual((r.status_code, r.headers.get("X-Refetch-Status")), (200, "failed"))
        with mock.patch("chanapp.api.main.engine_chart_payload.read_chart_inputs",
                        return_value=({}, {}, None)), \
                mock.patch("chanapp.api.main.engine_chart_payload.build_chart_payload",
                           side_effect=lambda *a, timings, **k: timings.update(compute_ms=0, resonance_ms=0)
                           or {"kline": []}):
            self.assertIsNone(self.c.get("/api/chart?code=sh600519&freq=day").headers.get("X-Refetch-Status"))


if __name__ == "__main__":
    unittest.main()
