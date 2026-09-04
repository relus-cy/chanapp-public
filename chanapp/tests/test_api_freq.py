"""freq 白名单：week 已废弃，/api/chart 与 /api/analysis 均 422。"""
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class TestFreqWhitelist(unittest.TestCase):
    def setUp(self):
        os.environ["WARMER_ENABLED"] = "0"
        self.addCleanup(os.environ.pop, "WARMER_ENABLED")
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_week_rejected_chart(self):
        r = self.c.get("/api/chart?code=sh000001&freq=week")
        self.assertEqual(r.status_code, 422)

    def test_week_rejected_analysis(self):
        r = self.c.get("/api/analysis?code=sh000001&freq=week")
        self.assertEqual(r.status_code, 422)

    def test_day_still_accepted_shape(self):
        # mock 数据层（不打外网）：参数校验放行 → 数据层错误 502，而非 422
        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=RuntimeError("no net in tests")):
            r = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 502)

    def test_m15_m5_accepted(self):
        """v1.4.1 放开 15m/5m：/api/chart 与 /api/analysis 均不再 422。"""
        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=RuntimeError("no net in tests")):
            for f in ("m15", "m5"):
                r = self.c.get(f"/api/chart?code=sh000001&freq={f}")
                self.assertEqual(r.status_code, 502)  # 放行后落到数据层错误
        with mock.patch("chanapp.api.analysis.engine_data.get_bars",
                        side_effect=RuntimeError("no net in tests")):
            for f in ("m15", "m5"):
                r = self.c.get(f"/api/analysis?code=sh000001&freq={f}")
                self.assertNotEqual(r.status_code, 422)
