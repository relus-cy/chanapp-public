"""分段计时日志（v1.3.0）：消融验证基础设施。

/api/chart、/api/analysis 走 TestClient + fixture 假数据（照 test_resonance.py 模式）；
engine.data.get_bars 直接调，事实库放临时目录、同步首取用替身写入。
"""
import csv
import tempfile
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
            "fetch_time": "2026-09-03 12:00:00", "from_cache": True}


class TestChartTimingLog(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        cache_support.isolate_cache_dir(self)   # 主图发布会记计算审计，不能写进真实事实库
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_chart_emits_timing_log(self):
        from chanapp.api import main as api_main
        with facade_support.fake_facade(fake_dataset):
            with self.assertLogs("chanapp.api.main", level="INFO") as cm:
                r = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 200)
        line = next((m for m in cm.output if "[timing] chart" in m), None)
        self.assertIsNotNone(line)
        for field in ("bars=", "compute=", "resonance=", "total="):
            self.assertIn(field, line)


class TestBarsTimingLog(unittest.TestCase):
    def test_first_fetch_and_hit_are_logged(self):
        from chanapp.engine import data as engine_data
        from chanapp.engine.kline import facts
        from chanapp.engine.kline.rows import RawDayRow

        def ensure(code, freq, **_kw):
            with engine_data._collector().writer() as conn:
                facts.commit_day_rows(conn, [RawDayRow(code, "2026-09-01", 1, 1, 1, 1, 1, "lot", 1, "CNY", 1, 0,
                                                       "final", "b")],
                                      market="CN", kind="stock", item="day_history", source="t",
                                      binding_gen=1, today="2026-09-02")
            return True

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(engine_data, "CACHE_DIR", Path(tmp)):
            with mock.patch.object(engine_data._collector(), "ensure_window", side_effect=ensure), \
                 mock.patch.object(engine_data._collector(), "lagging", return_value=False), \
                 self.assertLogs("chanapp.engine.data", level="INFO") as cm:
                first = engine_data.get_bars("sz001309", "day", adjust="raw")
                again = engine_data.get_bars("sz001309", "day", adjust="raw")
        self.assertEqual((first["from_cache"], again["from_cache"]), (False, True))
        self.assertTrue(any("[timing] bars" in m and "cache=first" in m for m in cm.output))
        self.assertTrue(any("[timing] bars" in m and "cache=hit" in m for m in cm.output))


class TestAnalysisTimingLog(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        cache_support.isolate_cache_dir(self)
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        cache_support.set_env(self, "ANALYSIS_CACHE_DIR", self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_analysis_emits_timing_log(self):
        from chanapp.api import analysis as api_analysis
        with facade_support.fake_facade(fake_dataset), \
             mock.patch.object(api_analysis.engine_llm, "is_configured", return_value=True), \
             mock.patch.object(api_analysis.engine_llm, "analyze",
                               return_value='{"current_state": "x", "scenarios": []}'):
            with self.assertLogs("chanapp.api.analysis", level="INFO") as cm:
                r = self.c.get("/api/analysis", params={"code": "sh000001", "freq": "day",
                                                        "tokens": facade_support.tokens()})
        self.assertEqual(r.status_code, 200)
        line = next((m for m in cm.output if "[timing] analysis" in m), None)
        self.assertIsNotNone(line)
        self.assertIn("cache=miss", line)
        self.assertIn("llm=", line)


class TestLoggingConfig(unittest.TestCase):
    def test_chanapp_logger_info_visible(self):
        """生产 uvicorn 下 chanapp.* 的 INFO 日志不被 root 默认值吞掉。"""
        import logging
        import chanapp.api.main  # noqa: F401 触发 app 侧 logging 配置
        log = logging.getLogger("chanapp")
        self.assertLessEqual(log.getEffectiveLevel(), logging.INFO)
        self.assertTrue(log.handlers)


if __name__ == "__main__":
    unittest.main()
