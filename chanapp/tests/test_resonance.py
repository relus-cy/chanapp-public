"""多级别共振角标：/api/chart 响应带 resonance 字段。

离线可跑：engine_data.get_bars 用 fixture 替换（day/m60/m30 均给同一批日线 bar，
只验证字段形状与降级行为，不验证跨级别语义）。
"""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "sh000001_day_qfq.csv"


def load_bars():
    with open(FIXTURE, encoding="utf-8") as f:
        return [
            {"dt": r["dt"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "volume": float(r["volume"])}
            for r in csv.DictReader(f)
        ]


def fake_dataset(code, freq="day"):
    return {"bars": load_bars(), "source": "fixture", "fqf": "qfq",
            "fetch_time": "2026-08-28 12:00:00", "from_cache": True}


class TestResonance(unittest.TestCase):
    def setUp(self):
        import os
        os.environ["WARMER_ENABLED"] = "0"
        self.addCleanup(os.environ.pop, "WARMER_ENABLED")
        # 结论记录会写 CACHE_DIR/kline.sqlite：隔离 CACHE_DIR，防 .cache/ 真实库被测试写入
        # 公开演示门面无 CACHE_DIR（无真实缓存写），此时跳过隔离
        self._cache_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache_tmp.cleanup)
        from chanapp.engine import data as engine_data
        if hasattr(engine_data, "CACHE_DIR"):
            cache_patch = mock.patch.object(engine_data, "CACHE_DIR", Path(self._cache_tmp.name))
            cache_patch.start()
            self.addCleanup(cache_patch.stop)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_chart_includes_three_levels(self):
        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=fake_dataset):
            r = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 200)
        res = r.json()["resonance"]
        self.assertEqual([x["freq"] for x in res], ["day", "m60", "m30"])
        for lv in res:
            self.assertIn("signals", lv)
            self.assertIn("zs", lv)
            self.assertIn("calculation_id", lv)
            self.assertLessEqual(len(lv["signals"]), 2)
            for point in lv["signals"]:
                self.assertIn("label", point)
                self.assertIn("dt", point)
                self.assertIn(point["level"], ("bi", "seg"))
                self.assertIn(point["status"], ("confirmed", "provisional"))
            if lv["zs"] is not None:
                self.assertIsInstance(lv["zs"]["inside"], bool)
                self.assertGreater(lv["zs"]["zg"], lv["zs"]["zd"])

    def test_level_failure_degrades_not_500(self):
        """某级取数失败 → 该级缺省，主响应仍 200。"""
        def flaky(code, freq="day"):
            if freq == "m60":
                raise RuntimeError("m60 source down")
            return fake_dataset(code, freq)

        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=flaky):
            r = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["freq"] for x in r.json()["resonance"]],
                         ["day", "m30"])

    def test_all_levels_fail_gives_empty_list(self):
        def down(code, freq="day"):
            if freq == "day":
                return fake_dataset(code, freq)
            raise RuntimeError("down")

        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=down):
            r = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["freq"] for x in r.json()["resonance"]], ["day"])

    def test_resonance_runs_sequentially_without_thread_pool(self):
        """SWR 后三级 get_bars 均为本地读，_resonance 顺序执行，不再创建线程池。"""
        from chanapp.engine import chart_payload
        with mock.patch("chanapp.engine.chart_payload.ThreadPoolExecutor", create=True) as pool:
            res = chart_payload._resonance("sh000001", get_bars_fn=fake_dataset)
        pool.assert_not_called()
        self.assertEqual([x["freq"] for x in res], ["day", "m60", "m30"])

    def test_warm_compute_fills_compute_cache(self):
        """data.on_refreshed → chart_payload.warm_compute：默认 profile 的结构计算进 compute_cache。"""
        from chanapp.engine import chart_payload, compute_cache, data as engine_data
        from chanapp.engine.chanpy_profiles import profile_identity
        compute_cache.clear()
        self.addCleanup(compute_cache.clear)
        dataset = fake_dataset("sh000001", "day")
        self.assertIs(engine_data.on_refreshed, chart_payload.warm_compute)
        chart_payload.warm_compute("sh000001", "day", dataset)
        version = compute_cache.dataset_version(dataset)
        cid = profile_identity("strict", "expanded")["calculation_id"]
        self.assertIsNotNone(compute_cache.get("sh000001", "day", version, cid))


if __name__ == "__main__":
    unittest.main()
