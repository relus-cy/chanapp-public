"""多级别共振角标：/api/chart 响应带 resonance 字段。

离线可跑：engine_data.get_bars 用 fixture 替换（day/m60/m30 均给同一批日线 bar，
只验证字段形状与降级行为，不验证跨级别语义）。
"""
import csv
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.tests import cache_support, facade_support

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
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        cache_support.isolate_cache_dir(self)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_chart_includes_three_levels(self):
        with facade_support.fake_facade(fake_dataset):
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

        with facade_support.fake_facade(flaky):
            r = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["freq"] for x in r.json()["resonance"]],
                         ["day", "m30"])

    def test_all_levels_fail_gives_empty_list(self):
        def down(code, freq="day"):
            if freq == "day":
                return fake_dataset(code, freq)
            raise RuntimeError("down")

        with facade_support.fake_facade(down):
            r = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["freq"] for x in r.json()["resonance"]], ["day"])

    def test_resonance_runs_sequentially_without_thread_pool(self):
        """三级别摘要是本地计算，顺序执行，不创建线程池。"""
        from chanapp.engine import chart_payload
        bundle = {f: fake_dataset("sh000001", f) for f in ("day", "m60", "m30")}
        with mock.patch("chanapp.engine.chart_payload.ThreadPoolExecutor", create=True) as pool:
            res = chart_payload._resonance("sh000001", bundle)
        pool.assert_not_called()
        self.assertEqual([x["freq"] for x in res], ["day", "m60", "m30"])

    def test_resonance_uses_requested_adjust_mode(self):
        seen = []

        def record(code, freq="day"):
            return fake_dataset(code, freq)

        with facade_support.fake_facade(record) as gb:
            r = self.c.get("/api/chart?code=sh000001&freq=day&adjust=raw")
            seen = [c.kwargs.get("adjust") for c in gb.call_args_list]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(seen), {"raw"})


if __name__ == "__main__":
    unittest.main()
