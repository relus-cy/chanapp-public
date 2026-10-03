"""结构计算缓存：命中/末bar失效/LRU 淘汰 + /api/chart→/api/analysis 复用集成。"""
import csv
import json
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import compute_cache, chanpy_adapter
from chanapp.tests import cache_support, facade_support

FIXTURE = Path(__file__).parent / "fixtures" / "sh000001_day_qfq.csv"


def load_bars():
    with open(FIXTURE, encoding="utf-8") as f:
        return [{"dt": r["dt"], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r["volume"])} for r in csv.DictReader(f)]


class TestComputeCacheUnit(unittest.TestCase):
    def setUp(self):
        compute_cache.clear()

    def test_hit_and_miss(self):
        compute_cache.put("sh000001", "day", "2026-08-25", {"bi": []}, {"signals": []}, [], "calc-id")
        self.assertIsNotNone(compute_cache.get("sh000001", "day", "2026-08-25", "calc-id"))
        self.assertIsNone(compute_cache.get("sh000001", "day", "2026-08-26", "calc-id"))  # 末bar 变化
        self.assertIsNone(compute_cache.get("sh000001", "m30", "2026-08-25", "calc-id"))  # freq 不同

    def test_late_put_preserves_new_content_version(self):
        compute_cache.put('a', 'day', 'new', {'value': 2}, {}, [], "calc-id")
        compute_cache.put('a', 'day', 'old', {'value': 1}, {}, [], "calc-id")
        self.assertEqual(compute_cache.get('a', 'day', 'new', "calc-id")['structure'], {'value': 2})
        self.assertEqual(compute_cache.get('a', 'day', 'old', "calc-id")['structure'], {'value': 1})

    def test_lru_eviction(self):
        for i in range(40):
            compute_cache.put(f"code{i:03d}", "day", "d", {}, {}, [], "calc-id")
        self.assertIsNone(compute_cache.get("code000", "day", "d", "calc-id"))
        self.assertIsNotNone(compute_cache.get("code039", "day", "d", "calc-id"))


class TestChartAnalysisReuse(unittest.TestCase):
    """chart 算完后 analysis 同 code/freq/末bar 不再重复 compute_analysis。"""

    def setUp(self):
        compute_cache.clear()
        tmp = cache_support.temp_dir(self)
        cache_support.set_env(self, "ANALYSIS_CACHE_DIR", tmp)
        cache_support.isolate_cache_dir(self, tmp)
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        cache_support.set_env(self, "LLM_API_KEY", "k")
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_analysis_reuses_chart_computation(self):
        dataset = {"bars": load_bars(), "meta": {"source": "fixture"}}
        payload = json.dumps({"current_state": "s", "scenarios": []})
        with facade_support.fake_facade(return_value=dataset), \
             mock.patch("chanapp.api.analysis.engine_llm.analyze", return_value=payload), \
             mock.patch("chanapp.engine.chanpy_adapter.compute_analysis",
                        wraps=chanpy_adapter.compute_analysis) as m:
            r1 = self.c.get("/api/chart?code=sh000001&freq=day")
            self.assertEqual(r1.status_code, 200)
            # AI 请求带主图给出的 analysis_tokens（与共振同一次读取）
            r2 = self.c.get("/api/analysis", params={
                "code": "sh000001", "freq": "day",
                "tokens": json.dumps(r1.json()["meta"]["analysis_tokens"])})
            self.assertEqual(r2.status_code, 200)
        # chart 算 day + 共振 m60/m30 共 3 次；analysis 命中缓存未重算（否则应为 4 次）
        self.assertEqual(m.call_count, 3)
