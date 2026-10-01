"""push2 路由契约（engine 层一律 mock，照抄 test_api_freq.py 的 env 清理模式）。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["COLLECTOR_ENABLED"] = "0"

from fastapi.testclient import TestClient
from chanapp.api import main
from chanapp.api.main import app
from chanapp.engine import display_feed

FACT_QUOTE = {"price": 130.0, "price_time": "2026-09-28 10:05", "price_label": "最新", "pc": 124.0,
              "pct": 4.84, "limit_up": False, "trade_date": "2026-09-28", "stale": False}


class TestPush2Api(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # /api/quotes 会真实读 watchlist：注入临时文件，绝不触碰仓库 watchlist.json
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        wl = Path(self._tmp.name) / "w.json"
        wl.write_text(json.dumps(
            [{"code": "sz001309", "name": "德明利"}], ensure_ascii=False),
            encoding="utf-8")
        os.environ["WATCHLIST_PATH"] = str(wl)
        self.addCleanup(os.environ.pop, "WATCHLIST_PATH")
        # A 股行情取门面 quote（事实派生）：一律替身，不读真实事实库
        patcher = mock.patch.object(main.engine_data, "quote", return_value=dict(FACT_QUOTE), create=True)
        self.quote = patcher.start()
        self.addCleanup(patcher.stop)

    def watch(self, *items):
        Path(os.environ["WATCHLIST_PATH"]).write_text(json.dumps(
            [{"code": c, "name": n} for c, n in items], ensure_ascii=False), encoding="utf-8")

    def get_quotes(self, rows, **extra):
        fake = {"quotes": rows, "degraded": False, "ts": 1787798400.0, **extra}
        with mock.patch.object(display_feed, "get_quotes", return_value=fake):
            r = self.client.get("/api/quotes")
        self.assertEqual(r.status_code, 200)
        return r.json()

    DISPLAY_ROW = {"price": 128.66, "pct": 3.21, "prev_close": 124.66, "name": "德明利", "limit_up": True}

    def test_quotes_ok(self):
        j = self.get_quotes({"sz001309": dict(self.DISPLAY_ROW)})
        self.assertFalse(j["degraded"])
        self.assertIn("fetch_time", j)
        for gone in ("scheme", "generation", "epoch"):  # 供数身份已退出响应
            self.assertNotIn(gone, j)

    def test_a_share_prices_come_only_from_fact_quote(self):
        j = self.get_quotes({"sz001309": dict(self.DISPLAY_ROW)})
        self.quote.assert_called_once_with("sz001309")
        row = j["quotes"]["sz001309"]
        self.assertEqual({k: row[k] for k in ("price", "pct", "limit_up", "price_time", "price_label", "stale")},
                         {"price": 130.0, "pct": 4.84, "limit_up": False,
                          "price_time": "2026-09-28 10:05", "price_label": "最新", "stale": False})
        self.assertFalse(row["price_unavailable"])
        self.assertNotIn("prev_close", row)                  # 显示层价格字段不再随 A 股行下发
        self.assertEqual(row["name"], "德明利")

    def test_prev_close_label_passes_through_with_empty_change(self):
        self.quote.return_value = {**FACT_QUOTE, "price": 124.0, "price_time": None, "price_label": "昨收",
                                   "pc": None, "pct": None}
        row = self.get_quotes({"sz001309": dict(self.DISPLAY_ROW)})["quotes"]["sz001309"]
        self.assertEqual((row["price"], row["price_label"], row["pct"], row["limit_up"]), (124.0, "昨收", None, False))

    def test_a_share_without_facts_shows_no_trusted_price_not_display_price(self):
        self.quote.return_value = None
        row = self.get_quotes({"sz001309": dict(self.DISPLAY_ROW)})["quotes"]["sz001309"]
        self.assertEqual((row["price"], row["pct"], row["limit_up"], row["price_label"], row["price_time"]),
                         (None, None, False, None, None))
        self.assertTrue(row["price_unavailable"])

    def test_a_share_missing_from_display_still_gets_fact_row(self):
        self.watch(("sz001309", "德明利"), ("sh600926", "杭州银行"))
        j = self.get_quotes({"sz001309": dict(self.DISPLAY_ROW)}, missing_codes=["sh600926"])
        self.assertEqual(j["quotes"]["sh600926"]["price"], 130.0)
        self.assertEqual(j["quotes"]["sh600926"]["name"], "杭州银行")

    def test_hk_keeps_display_quote_until_ph3(self):
        self.watch(("sz001309", "德明利"), ("hk00700", "腾讯控股"))
        hk = {"price": 500.0, "pct": 1.2, "prev_close": 494.0, "name": "腾讯控股", "limit_up": False}
        j = self.get_quotes({"sz001309": dict(self.DISPLAY_ROW), "hk00700": dict(hk)})
        self.assertEqual(j["quotes"]["hk00700"], hk)
        self.assertEqual([c.args[0] for c in self.quote.call_args_list], ["sz001309"])

    def test_one_failing_fact_quote_does_not_fail_the_list(self):
        self.watch(("sz001309", "德明利"), ("sh600926", "杭州银行"))
        self.quote.side_effect = lambda code, name=None: (_ for _ in ()).throw(RuntimeError("db")) \
            if code == "sz001309" else dict(FACT_QUOTE)
        with self.assertLogs("chanapp.api.main", level="WARNING"):
            j = self.get_quotes({})
        self.assertTrue(j["quotes"]["sz001309"]["price_unavailable"])
        self.assertIsNone(j["quotes"]["sz001309"]["price"])
        self.assertEqual(j["quotes"]["sh600926"]["price"], 130.0)

    def test_quotes_preserves_source_quality_metadata(self):
        meta = {"sources": ["baseline_backup"], "source_stale": True, "source_ts": 1787790000}
        fake = {"quotes": {}, "degraded": False, "ts": 1787798400,
                "meta": meta, "missing_codes": ["sz001309"]}
        with mock.patch.object(display_feed, "get_quotes", return_value=fake):
            result = self.client.get("/api/quotes").json()
        self.assertEqual(result.get("meta"), meta)
        self.assertEqual(result["missing_codes"], ["sz001309"])
        self.assertFalse(result["degraded"])

    def test_quotes_502(self):
        with mock.patch.object(display_feed, "get_quotes", side_effect=RuntimeError("boom")):
            r = self.client.get("/api/quotes")
        self.assertEqual(r.status_code, 502)
        self.assertIn("快照失败", r.json()["detail"])

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
