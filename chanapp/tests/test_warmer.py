"""warmer：交易时段遍历预热、非时段跳过、单条异常不中断、开关与单例。"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from chanapp.engine import warmer


class TestWarmOnce(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = Path(self._tmp.name) / "watchlist.json"
        p.write_text(json.dumps([{"code": "sh000001", "name": "上证指数"},
                                 {"code": "hk00700", "name": "腾讯控股"}]),
                     encoding="utf-8")
        os.environ["WATCHLIST_PATH"] = str(p)
        self.addCleanup(os.environ.pop, "WATCHLIST_PATH")
        self.calls = []

    def _gb(self, code, freq):
        self.calls.append((code, freq))

    def test_in_session_warms_all(self):
        r = warmer.warm_once(now=datetime(2026, 8, 25, 10, 30), get_bars_fn=self._gb)
        self.assertFalse(r["skipped"])
        self.assertEqual(r["ok"], 6)  # 2 codes × 3 freqs
        self.assertEqual(len(self.calls), 6)
        self.assertIn(("hk00700", "m30"), self.calls)

    def test_outside_session_skips(self):
        r = warmer.warm_once(now=datetime(2026, 8, 23, 10, 30),  # 周日
                             get_bars_fn=self._gb)
        self.assertTrue(r["skipped"])
        self.assertEqual(self.calls, [])

    def test_single_failure_does_not_break_loop(self):
        def gb(code, freq):
            if freq == "m30":
                raise RuntimeError("boom")
            self.calls.append((code, freq))

        r = warmer.warm_once(now=datetime(2026, 8, 25, 10, 30), get_bars_fn=gb)
        self.assertEqual(r["ok"], 4)
        self.assertEqual(len(r["errors"]), 2)


class TestStartGuard(unittest.TestCase):
    def setUp(self):
        os.environ["WARMER_ENABLED"] = "0"
        self.addCleanup(os.environ.pop, "WARMER_ENABLED")

    def test_disabled_no_thread(self):
        self.assertFalse(warmer.start())
        self.assertFalse(any(t.name == "chanapp-warmer"
                             for t in __import__("threading").enumerate()))
