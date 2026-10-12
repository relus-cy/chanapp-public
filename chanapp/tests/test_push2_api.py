"""push2 路由契约（engine 层一律 mock，照抄 test_api_freq.py 的 env 清理模式）。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from chanapp.api import main
from chanapp.api.main import app
from chanapp.engine import display_feed
from chanapp.tests import cache_support

FACT_QUOTE = {"price": 130.0, "price_time": "2026-09-28 10:05", "price_label": "最新", "pc": 124.0,
              "pct": 4.84, "limit_up": False, "trade_date": "2026-09-28", "stale": False}


class TestPush2Api(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        self.client = TestClient(app)
        # /api/quotes 会真实读 watchlist：注入临时文件，绝不触碰仓库 watchlist.json
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        wl = Path(self._tmp.name) / "w.json"
        wl.write_text(json.dumps(
            [{"code": "sz001309", "name": "德明利"}], ensure_ascii=False),
            encoding="utf-8")
        cache_support.set_env(self, "WATCHLIST_PATH", str(wl))
        # 报价取门面 quotes（事实派生，一次读事务）：一律替身，不读真实事实库；按代码查表，表外代码无事实
        self.facts = {"sz001309": dict(FACT_QUOTE)}
        patcher = mock.patch.object(main.engine_data, "quotes", create=True,
                                    side_effect=lambda codes: {c: self.facts.get(c) for c in codes})
        self.quotes = patcher.start()
        self.addCleanup(patcher.stop)

    def watch(self, *items):
        Path(os.environ["WATCHLIST_PATH"]).write_text(json.dumps(
            [{"code": c, "name": n} for c, n in items], ensure_ascii=False), encoding="utf-8")

    def get_quotes(self, params=None):
        with mock.patch.object(display_feed, "get_quotes", side_effect=AssertionError("报价不经显示层")):
            r = self.client.get("/api/quotes", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_quotes_ok(self):
        j = self.get_quotes()
        self.assertEqual(set(j), {"data_version", "quotes"})   # 显示层的 degraded/meta/missing_codes 已退出

    def test_prices_come_only_from_fact_quote_in_one_read(self):
        self.watch(("sz001309", "德明利"), ("sh600926", "杭州银行"))
        self.facts["sh600926"] = {**FACT_QUOTE, "price": 13.2}
        j = self.get_quotes()
        self.quotes.assert_called_once_with(["sz001309", "sh600926"])   # 全表一次读取
        row = j["quotes"]["sz001309"]
        self.assertEqual(row, {"name": "德明利", "price": 130.0, "pct": 4.84, "limit_up": False,
                               "price_time": "2026-09-28 10:05", "price_label": "最新", "stale": False,
                               "price_unavailable": False})
        self.assertEqual((j["quotes"]["sh600926"]["price"], j["quotes"]["sh600926"]["name"]), (13.2, "杭州银行"))

    def test_hk_rows_come_from_facts_like_a_shares(self):
        self.watch(("sz001309", "德明利"), ("hk00700", "腾讯控股"))
        self.facts["hk00700"] = {**FACT_QUOTE, "price": 500.0, "pct": 1.2, "price_time": "2026-09-28 10:30"}
        row = self.get_quotes()["quotes"]["hk00700"]
        self.assertEqual((row["price"], row["pct"], row["price_time"], row["price_unavailable"]),
                         (500.0, 1.2, "2026-09-28 10:30", False))
        self.assertNotIn("source_ts", row)

    def test_current_code_outside_watchlist_is_included(self):
        self.facts["hk09988"] = {**FACT_QUOTE, "price": 88.0}
        j = self.get_quotes({"code": "hk09988"})
        self.quotes.assert_called_once_with(["sz001309", "hk09988"])
        self.assertEqual((j["quotes"]["hk09988"]["price"], j["quotes"]["hk09988"]["name"]), (88.0, ""))
        self.assertEqual(self.client.get("/api/quotes", params={"code": "../x"}).status_code, 422)

    def test_prev_close_label_passes_through_with_empty_change(self):
        self.facts["sz001309"] = {**FACT_QUOTE, "price": 124.0, "price_time": None, "price_label": "昨收",
                                  "pc": None, "pct": None}
        row = self.get_quotes()["quotes"]["sz001309"]
        self.assertEqual((row["price"], row["price_label"], row["pct"], row["limit_up"]), (124.0, "昨收", None, False))

    def test_without_facts_shows_no_trusted_price(self):
        self.facts.clear()
        row = self.get_quotes()["quotes"]["sz001309"]
        self.assertEqual((row["price"], row["pct"], row["limit_up"], row["price_label"], row["price_time"]),
                         (None, None, False, None, None))
        self.assertTrue(row["price_unavailable"])

    def test_fact_read_failure_is_502(self):
        self.quotes.side_effect = RuntimeError("db")
        r = self.client.get("/api/quotes")
        self.assertEqual(r.status_code, 502)
        self.assertIn("报价暂不可用", r.json()["detail"])

    def test_f10_ok_and_422(self):
        fake = {"f10": {"pe_ttm": 45.6}, "flow": {"main": 1.26e8},
                "industry_pct": 1.85, "degraded": False, "ts": 1787798400.0}
        with mock.patch.object(display_feed, "get_f10", return_value=fake):
            r = self.client.get("/api/f10", params={"code": "sz001309"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["flow"]["main"], 1.26e8)
        for gone in ("scheme", "generation", "epoch"):  # 供数身份已退出响应
            self.assertNotIn(gone, r.json())
        r2 = self.client.get("/api/f10", params={"code": "bad"})
        self.assertEqual(r2.status_code, 422)

    def test_f10_502(self):
        with mock.patch.object(display_feed, "get_f10", side_effect=display_feed.Push2Blocked("退避中")):
            r = self.client.get("/api/f10", params={"code": "sz001309"})
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()
