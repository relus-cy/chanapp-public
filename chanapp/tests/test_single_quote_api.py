"""单代码报价（目标 2026-09-29 第三阶段「报价、F10 与状态栏」）：搜索查看的非自选代码也有卡头价格。

实现前列出的失败方式：
- 非自选代码没有任何报价入口（/api/quotes 只报自选），卡头价格恒为空；
- 单代码请求覆盖自选报价缓存（显示层报价缓存按请求代码集合判断命中，单代码请求会把自选缓存换成一只）；
- A 股单代码报价改用显示层价格，与自选行（K 线事实派生）口径不一致；事实读取失败时编造价格。
"""
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import display_feed, swr
from chanapp.tests import cache_support

HK_PAYLOAD = {"data": {"diff": [{"f2": 512.5, "f3": -1.2, "f12": "00700", "f13": 116, "f14": "腾讯控股", "f18": 518.7}]}}


class SingleQuoteApiTests(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        tmp = Path(cache_support.temp_dir(self))
        cache_support.set_env(self, "WATCHLIST_PATH", str(tmp / "w.json"))
        (tmp / "w.json").write_text(json.dumps([{"code": "sh600036", "name": "招商银行"}]), encoding="utf-8")
        cache_support.isolate_cache_dir(self)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_cn_code_uses_fact_quote_without_touching_display_quotes(self):
        fact = {"price": 7.12, "pct": 0.85, "limit_up": False, "price_time": "2026-09-30 10:05",
                "price_label": "最新", "stale": False}
        with mock.patch("chanapp.api.main.engine_data.quote", return_value=fact, create=True) as q, \
             mock.patch("chanapp.api.main.engine_feed.get_quotes") as display:
            r = self.c.get("/api/quote?code=sz000002")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["code"], "sz000002")
        self.assertEqual((body["quote"]["price"], body["quote"]["price_label"], body["quote"]["price_unavailable"]),
                         (7.12, "最新", False))
        q.assert_called_once_with("sz000002")
        display.assert_not_called()

    def test_cn_fact_failure_is_unavailable_not_invented(self):
        with mock.patch("chanapp.api.main.engine_data.quote", side_effect=RuntimeError("db"), create=True):
            r = self.c.get("/api/quote?code=sz000002")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["quote"]["price"], r.json()["quote"]["price_unavailable"]), (None, True))

    def test_invalid_code_rejected(self):
        self.assertEqual(self.c.get("/api/quote?code=../x").status_code, 422)


class SingleQuoteCacheTests(unittest.TestCase):
    def setUp(self):
        root = cache_support.temp_dir(self)
        cache_support.isolate_cache_dir(self, root)
        self.dir = Path(root) / "display"
        self.dir.mkdir()
        for method in ("_backup_quotes", "_backup_f10"):
            patcher = mock.patch.object(display_feed, method, side_effect=OSError("offline backup"))
            patcher.start()
            self.addCleanup(patcher.stop)
        display_feed._reset_blocked()
        swr._inflight.clear()

    def tearDown(self):
        for t in list(swr._inflight.values()):
            t.join(5)

    def test_single_code_quote_keeps_watchlist_cache(self):
        watch = {"ts": time.time(), "data": {"sh600036": {"price": 40.0, "pct": 1.0, "name": "招商银行"}},
                 "requested_codes": ["sh600036"]}
        (self.dir / "quotes.json").write_text(json.dumps(watch, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(display_feed, "_fetch_json", return_value=HK_PAYLOAD):
            r = display_feed.get_quote("hk00700")
        self.assertEqual(r["quotes"]["hk00700"]["price"], 512.5)
        self.assertEqual(json.loads((self.dir / "quotes.json").read_text(encoding="utf-8"))["data"], watch["data"])
        with mock.patch.object(display_feed, "_fetch_json", side_effect=AssertionError("自选缓存仍新鲜，不应重取")):
            self.assertEqual(display_feed.get_quotes(["sh600036"])["quotes"]["sh600036"]["price"], 40.0)


if __name__ == "__main__":
    unittest.main()
