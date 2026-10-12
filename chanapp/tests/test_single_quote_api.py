"""单代码报价（目标 2026-09-29 第三阶段「报价、F10 与状态栏」）：搜索查看的非自选代码也有卡头价格。

实现前列出的失败方式：
- 非自选代码没有任何报价入口（/api/quotes 只报自选），卡头价格恒为空；
- 单代码报价改用显示层价格，与自选行（K 线事实派生）口径不一致；事实读取失败时编造价格。
2026-10-12 起港股同样只取事实派生报价（与 A 股同一口径），页面不再调用本入口，保留兼容。
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.tests import cache_support


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
        with mock.patch("chanapp.api.main.engine_data.quote", return_value=fact, create=True) as q:
            r = self.c.get("/api/quote?code=sz000002")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["code"], "sz000002")
        self.assertEqual((body["quote"]["price"], body["quote"]["price_label"], body["quote"]["price_unavailable"]),
                         (7.12, "最新", False))
        q.assert_called_once_with("sz000002")

    def test_hk_code_uses_fact_quote_not_display_layer(self):
        fact = {"price": 512.5, "pct": -1.2, "limit_up": False, "price_time": "2026-09-30 10:30",
                "price_label": "最新", "stale": False}
        with mock.patch("chanapp.api.main.engine_data.quote", return_value=fact, create=True) as q:
            r = self.c.get("/api/quote?code=hk00700")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["quote"]["price"], r.json()["quote"]["price_time"]), (512.5, "2026-09-30 10:30"))
        q.assert_called_once_with("hk00700")

    def test_cn_fact_failure_is_unavailable_not_invented(self):
        with mock.patch("chanapp.api.main.engine_data.quote", side_effect=RuntimeError("db"), create=True):
            r = self.c.get("/api/quote?code=sz000002")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["quote"]["price"], r.json()["quote"]["price_unavailable"]), (None, True))

    def test_invalid_code_rejected(self):
        self.assertEqual(self.c.get("/api/quote?code=../x").status_code, 422)


if __name__ == "__main__":
    unittest.main()
