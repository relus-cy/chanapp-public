"""联想搜索（/api/search + engine/search.py）测试：离线，mock smartbox HTTP。"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import search as engine_search

# GBK 编码的假响应（兼容旧口径）：含 sh/sz/hk/us，us 应被过滤
GBK_HINT = (
    'v_hint="sh~600519~贵州茅台~gzmt~GP-A^'
    'sz~000001~平安银行~payh~GP-A^'
    'hk~00700~腾讯控股~txkg~GP^'
    'us~tcehy.ps~腾讯控股(adr)~txkgadr~GP^'
    'hk~13005~腾讯法兴三三购A~txfxqsga~QZ"'
).encode("gbk")

# 实测口径（2026-08-23）：ASCII + \uXXXX 转义
ESCAPED_HINT = (
    b'v_hint="sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A^'
    b'hk~00700~\\u817e\\u8baf\\u63a7\\u80a1~txkg~GP^'
    b'us~tcehy.ps~\\u817e\\u8baf\\u63a7\\u80a1(adr)~txkgadr~GP"'
)


class TestParseHint(unittest.TestCase):
    def test_gbk_decode_and_market_filter(self):
        items = engine_search.parse_hint(GBK_HINT)
        self.assertEqual(
            items,
            [
                {"code": "sh600519", "name": "贵州茅台", "type": "GP-A"},
                {"code": "sz000001", "name": "平安银行", "type": "GP-A"},
                {"code": "hk00700", "name": "腾讯控股", "type": "GP"},
                {"code": "hk13005", "name": "腾讯法兴三三购A", "type": "QZ"},
            ],
        )

    def test_unicode_escape_decode(self):
        items = engine_search.parse_hint(ESCAPED_HINT)
        self.assertEqual(items[0], {"code": "sh600519", "name": "贵州茅台", "type": "GP-A"})
        self.assertEqual(items[1], {"code": "hk00700", "name": "腾讯控股", "type": "GP"})
        self.assertEqual(len(items), 2)  # us 被过滤

    def test_garbage_returns_empty(self):
        self.assertEqual(engine_search.parse_hint(b""), [])
        self.assertEqual(engine_search.parse_hint(b"not a hint"), [])
        self.assertEqual(engine_search.parse_hint('v_hint=""'.encode("gbk")), [])


class TestSearchFunction(unittest.TestCase):
    def test_empty_q_skips_http(self):
        with mock.patch.object(engine_search, "_fetch_bytes") as fb:
            self.assertEqual(engine_search.search(""), [])
            self.assertEqual(engine_search.search("   "), [])
            fb.assert_not_called()

    def test_search_fetch_and_parse(self):
        with mock.patch.object(engine_search, "_fetch_bytes", return_value=GBK_HINT) as fb:
            items = engine_search.search("茅台")
        self.assertEqual(items[0]["code"], "sh600519")
        url = fb.call_args[0][0]
        self.assertIn("smartbox.gtimg.cn", url)
        self.assertIn("q=%E8%8C%85%E5%8F%B0", url)  # 茅台 urlencode


class TestSearchApi(unittest.TestCase):
    def setUp(self):
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_api_happy_path(self):
        with mock.patch.object(engine_search, "_fetch_bytes", return_value=ESCAPED_HINT):
            r = self.c.get("/api/search", params={"q": "茅台"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()[0], {"code": "sh600519", "name": "贵州茅台", "type": "GP-A"})

    def test_api_empty_q(self):
        with mock.patch.object(engine_search, "_fetch_bytes") as fb:
            r = self.c.get("/api/search", params={"q": ""})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), [])
            fb.assert_not_called()

    def test_api_upstream_failure_returns_502(self):
        with mock.patch.object(
            engine_search, "_fetch_bytes", side_effect=TimeoutError("timeout")
        ):
            r = self.c.get("/api/search", params={"q": "茅台"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("联想搜索失败", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
