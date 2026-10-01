"""5 分与 15 分周期下线（目标 2026-09-29 第三阶段）：页面只保留 30 分、60 分、日线、周线。

实现前列出的失败方式：
- /api/chart、/api/analysis 带 m5/m15 仍按分钟图返回数据（或静默改成别的周期冒充）；
- 查看记录继续写入 m5/m15，最近列表把旧记录里的 m5/m15 原样交给页面，页面据此打开已下线的周期。
"""
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.tests import cache_support


class RetiredPeriodsApiTests(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        tmp = Path(cache_support.temp_dir(self))
        cache_support.set_env(self, "WATCHLIST_PATH", str(tmp / "w.json"))
        self.log_path = tmp / "views.sqlite"
        cache_support.set_env(self, "VIEW_LOG_PATH", str(self.log_path))
        cache_support.isolate_cache_dir(self)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_chart_rejects_retired_periods_without_reading(self):
        for freq in ("m5", "m15"):
            with mock.patch("chanapp.api.main.engine_data.get_bars") as read:
                r = self.c.get(f"/api/chart?code=sh600519&freq={freq}")
            self.assertEqual(r.status_code, 400, (freq, r.text))
            self.assertIn("不再提供", r.json()["detail"])
            read.assert_not_called()

    def test_analysis_rejects_retired_periods(self):
        for freq in ("m5", "m15"):
            r = self.c.get(f"/api/analysis?code=sh600519&freq={freq}&tokens=x")
            self.assertEqual(r.status_code, 400, (freq, r.text))
            self.assertIn("不再提供", r.json()["detail"])

    def test_view_log_stores_and_serves_m30_for_retired_periods(self):
        r = self.c.post("/api/views", json={"code": "sh600519", "name": "贵州茅台", "freq": "m5", "adjust": "qfq"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["recent"][0]["freq"], "m30")
        with sqlite3.connect(self.log_path) as conn:          # 升级前写下的旧记录
            conn.execute("UPDATE recent_views SET freq='m15'")
        self.assertEqual(self.c.get("/api/views").json()["recent"][0]["freq"], "m30")
        from chanapp.api import view_log
        self.assertEqual([e["freq"] for e in view_log.ViewLog(self.log_path).events()], ["m30"])


if __name__ == "__main__":
    unittest.main()
