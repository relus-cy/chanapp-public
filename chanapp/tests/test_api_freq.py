"""freq 白名单：/api/chart 接受 week（新视图周期）；/api/analysis 的联合分析固定 day/m60/m30，拒绝 week；m15/m5 已下线。"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.tests import cache_support, facade_support


class TestFreqWhitelist(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        cache_support.isolate_cache_dir(self)     # 主图走门面 bundle：替身之外的读取落临时目录
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_week_accepted_chart(self):
        # 周线是新视图周期：参数放行后落到数据层（替身抛错 → 502），而非 422
        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=RuntimeError("no net in tests")):
            r = self.c.get("/api/chart?code=sh000001&freq=week")
        self.assertEqual(r.status_code, 502)

    def test_week_rejected_analysis(self):
        r = self.c.get("/api/analysis?code=sh000001&freq=week")
        self.assertEqual(r.status_code, 422)

    def test_day_still_accepted_shape(self):
        # mock 数据层（不打外网）：参数校验放行 → 数据层错误 502，而非 422
        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=RuntimeError("no net in tests")):
            r = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 502)

    def test_m15_m5_rejected(self):
        """有意改写（目标 2026-09-29 第三阶段）：v1.4.1 放开的 15m/5m 下线，/api/chart 与 /api/analysis 明确回 400。"""
        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=RuntimeError("no net in tests")):
            for f in ("m15", "m5"):
                r = self.c.get(f"/api/chart?code=sh000001&freq={f}")
                self.assertEqual(r.status_code, 400)
        with facade_support.fake_facade(lambda c, f: (_ for _ in ()).throw(RuntimeError("no net in tests"))):
            for f in ("m15", "m5"):
                r = self.c.get(f"/api/analysis?code=sh000001&freq={f}")
                self.assertEqual(r.status_code, 400)
