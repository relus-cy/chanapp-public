"""Part 2: /api/analysis LLM 失败缓存（llm_error 条目，10 分钟 TTL）测试。

mock engine_llm.analyze 抛 LLMError，ANALYSIS_CACHE_DIR 注入临时目录，不打外网。
"""
import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import llm as engine_llm

FIXTURE = Path(__file__).parent / "fixtures" / "sh000001_day_qfq.csv"


def load_bars():
    with open(FIXTURE, encoding="utf-8") as f:
        return [
            {"dt": r["dt"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "volume": float(r["volume"])}
            for r in csv.DictReader(f)
        ]


class TestAnalysisFailureCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["ANALYSIS_CACHE_DIR"] = self._tmp.name
        self.addCleanup(os.environ.pop, "ANALYSIS_CACHE_DIR")
        self._saved = {k: os.environ.get(k)
                       for k in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL")}
        os.environ["LLM_API_KEY"] = "k"
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _client(self):
        from chanapp.api.main import app
        return TestClient(app)

    def _dataset(self):
        return {"bars": load_bars(), "meta": {"source": "fixture"}}

    def _cache_files(self):
        return list(Path(self._tmp.name).glob("*.json"))

    def test_failure_writes_cache_and_suppresses_retry(self):
        """analyze 抛 LLMError → 502 + 失败条目落盘；TTL 内第二次直接 502 不重试。"""
        c = self._client()
        with mock.patch("chanapp.api.analysis.engine_data.get_bars",
                        return_value=self._dataset()), \
             mock.patch("chanapp.api.analysis.engine_llm.analyze",
                        side_effect=engine_llm.LLMError("deepseek down")) as an:
            r1 = c.get("/api/analysis?code=sh000001&freq=day")
            self.assertEqual(r1.status_code, 502)
            r2 = c.get("/api/analysis?code=sh000001&freq=m30")
            self.assertEqual(r2.status_code, 502)
            self.assertEqual(an.call_count, 1)  # 第二次未重试

        files = self._cache_files()
        self.assertEqual(len(files), 1)
        entry = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(entry["status"], "llm_error")
        self.assertIn("deepseek down", entry["error"])
        self.assertGreater(time.time() - entry["fetched_at"], -1)

    def test_stale_failure_retries(self):
        """失败条目过期（≥600s）→ 恢复重试 LLM。"""
        c = self._client()
        with mock.patch("chanapp.api.analysis.engine_data.get_bars",
                        return_value=self._dataset()), \
             mock.patch("chanapp.api.analysis.engine_llm.analyze",
                        side_effect=engine_llm.LLMError("deepseek down")) as an:
            self.assertEqual(c.get("/api/analysis?code=sh000001&freq=day").status_code, 502)
            self.assertEqual(an.call_count, 1)
            # 把失败条目的 fetched_at 改老 601s
            f = self._cache_files()[0]
            entry = json.loads(f.read_text(encoding="utf-8"))
            entry["fetched_at"] = time.time() - 601
            f.write_text(json.dumps(entry), encoding="utf-8")
            self.assertEqual(c.get("/api/analysis?code=sh000001&freq=day").status_code, 502)
            self.assertEqual(an.call_count, 2)  # 过期后重试

    def test_success_overwrites_failure_entry(self):
        """失败后 LLM 恢复：正常结果覆写同一哈希文件，后续命中正常缓存。"""
        c = self._client()
        ok_payload = json.dumps({"current_state": "s", "scenarios": []})
        with mock.patch("chanapp.api.analysis.engine_data.get_bars",
                        return_value=self._dataset()), \
             mock.patch("chanapp.api.analysis.engine_llm.analyze",
                        side_effect=[engine_llm.LLMError("down"), ok_payload]) as an:
            self.assertEqual(c.get("/api/analysis?code=sh000001&freq=day").status_code, 502)
            f = self._cache_files()[0]
            entry = json.loads(f.read_text(encoding="utf-8"))
            entry["fetched_at"] = time.time() - 601  # 让失败条目过期
            f.write_text(json.dumps(entry), encoding="utf-8")
            r = c.get("/api/analysis?code=sh000001&freq=day")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "ok")
            entry2 = json.loads(self._cache_files()[0].read_text(encoding="utf-8"))
            self.assertEqual(entry2["status"], "ok")
            self.assertEqual(an.call_count, 2)


if __name__ == "__main__":
    unittest.main()
