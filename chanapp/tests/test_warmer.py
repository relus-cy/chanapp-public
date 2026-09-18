"""warmer：交易时段遍历预热、非时段跳过、单条异常不中断、开关与单例。"""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from chanapp.engine import supply, warmer


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
        self.assertEqual(r["ok"], 10)  # 2 codes × 5 freqs
        self.assertEqual(len(self.calls), 10)
        self.assertIn(("hk00700", "m30"), self.calls)

    def test_freqs_subset(self):
        """fetcher 供数策略：warm_once 可按 freqs 子集预热（默认全集不变）。"""
        r = warmer.warm_once(now=datetime(2026, 8, 25, 10, 30), get_bars_fn=self._gb,
                             freqs=("day", "m30", "m60"))
        self.assertEqual(r["ok"], 6)  # 2 codes × 3 freqs
        self.assertEqual({f for _, f in self.calls}, {"day", "m30", "m60"})

    def test_round_keeps_snapshot_when_global_scheme_changes(self):
        manager = supply.Manager(Path(self._tmp.name) / 'supply.json')
        observed = []

        def get_bars(code, freq):
            observed.append(supply.current())
            if len(observed) == 1:
                supply.switch('primary_candidate', 0, lambda _: {})

        with mock.patch.object(supply, '_default_manager', manager):
            result = warmer.warm_once(now=datetime(2026, 8, 25, 10, 30),
                                      get_bars_fn=get_bars)
            self.assertEqual(result['ok'], 10)
            self.assertEqual(observed, [supply.Snapshot('baseline', 0)] * 10)
            self.assertEqual(supply.current(), supply.Snapshot('primary_candidate', 1))

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
        self.assertEqual(r["ok"], 8)
        self.assertEqual(len(r["errors"]), 2)

    def test_obsolete_round_stops_early(self):
        try:
            from chanapp.engine import cache_store
        except ImportError:
            self.skipTest('versioned cache store is not installed (public demo)')
        def gb(code, freq):
            if self.calls:
                raise cache_store.ObsoletePublication()
            self.calls.append((code, freq))

        r = warmer.warm_once(now=datetime(2026, 8, 25, 10, 30), get_bars_fn=gb)
        self.assertTrue(r["obsolete"])
        self.assertEqual(len(self.calls), 1)

    def test_cache_store_binds_real_module(self):
        """完整树：warmer 的 cache_store 守卫绑定真实模块（不降级）；公开树无此模块时跳过。"""
        if importlib.util.find_spec("chanapp.engine.cache_store") is None:
            self.skipTest('versioned cache store is not installed (public demo)')
        self.assertIsNotNone(warmer.cache_store)
        self.assertIs(warmer._Obsolete, warmer.cache_store.ObsoletePublication)

    def test_cache_retention_runs_at_most_once_a_day(self):
        calls = []
        self.addCleanup(setattr, warmer, '_last_gc', warmer._last_gc)
        warmer._last_gc = -100000.0
        self.assertTrue(warmer._maybe_collect(1000.0, lambda: calls.append(1)))
        self.assertFalse(warmer._maybe_collect(1000.0 + 3600, lambda: calls.append(1)))
        self.assertTrue(warmer._maybe_collect(1000.0 + 90000, lambda: calls.append(1)))
        self.assertEqual(len(calls), 2)


class TestStartGuard(unittest.TestCase):
    def setUp(self):
        os.environ["WARMER_ENABLED"] = "0"
        self.addCleanup(os.environ.pop, "WARMER_ENABLED")

    def test_disabled_no_thread(self):
        self.assertFalse(warmer.start())
        self.assertFalse(any(t.name == "chanapp-warmer"
                             for t in __import__("threading").enumerate()))
