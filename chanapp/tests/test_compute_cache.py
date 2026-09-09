"""结构计算缓存：命中/末bar失效/LRU 淘汰 + /api/chart→/api/analysis 复用集成。"""
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import compute_cache, structure as real_structure

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
        compute_cache.put("sh000001", "day", "2026-08-25", {"bi": []}, {"signals": []}, [])
        self.assertIsNotNone(compute_cache.get("sh000001", "day", "2026-08-25"))
        self.assertIsNone(compute_cache.get("sh000001", "day", "2026-08-26"))  # 末bar 变化
        self.assertIsNone(compute_cache.get("sh000001", "m30", "2026-08-25"))  # freq 不同

    def test_late_put_preserves_new_content_version(self):
        compute_cache.put('a', 'day', 'new', {'value': 2}, {}, [])
        compute_cache.put('a', 'day', 'old', {'value': 1}, {}, [])
        self.assertEqual(compute_cache.get('a', 'day', 'new')['structure'], {'value': 2})
        self.assertEqual(compute_cache.get('a', 'day', 'old')['structure'], {'value': 1})

    def test_lru_eviction(self):
        for i in range(40):
            compute_cache.put(f"code{i:03d}", "day", "d", {}, {}, [])
        self.assertIsNone(compute_cache.get("code000", "day", "d"))
        self.assertIsNotNone(compute_cache.get("code039", "day", "d"))


class TestChartAnalysisReuse(unittest.TestCase):
    """chart 算完后 analysis 同 code/freq/末bar 不再重复 compute_structure。"""

    def setUp(self):
        compute_cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["ANALYSIS_CACHE_DIR"] = self._tmp.name
        self.addCleanup(os.environ.pop, "ANALYSIS_CACHE_DIR")
        os.environ["WARMER_ENABLED"] = "0"
        self.addCleanup(os.environ.pop, "WARMER_ENABLED")
        os.environ["LLM_API_KEY"] = "k"
        self.addCleanup(os.environ.pop, "LLM_API_KEY")
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_analysis_reuses_chart_computation(self):
        dataset = {"bars": load_bars(), "meta": {"source": "fixture"}}
        payload = json.dumps({"current_state": "s", "scenarios": []})
        with mock.patch("chanapp.api.main.engine_data.get_bars", return_value=dataset), \
             mock.patch("chanapp.api.analysis.engine_data.get_bars", return_value=dataset), \
             mock.patch("chanapp.api.analysis.engine_llm.analyze", return_value=payload), \
             mock.patch("chanapp.engine.structure.compute_structure",
                        wraps=real_structure.compute_structure) as m:
            r1 = self.c.get("/api/chart?code=sh000001&freq=day")
            self.assertEqual(r1.status_code, 200)
            r2 = self.c.get("/api/analysis?code=sh000001&freq=day")
            self.assertEqual(r2.status_code, 200)
        # chart 算 day + 共振 m60/m30 共 3 次；analysis 命中缓存未重算（否则应为 4 次）
        self.assertEqual(m.call_count, 3)
