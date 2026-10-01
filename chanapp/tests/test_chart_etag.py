"""/api/chart 条件请求：弱 ETag 覆盖整包（只剔除每秒变化的 stale_age_s），If-None-Match 命中回 304 空体；
meta 的 stale/stale_age_s 标识随响应透传前端。"""
import csv
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.tests import cache_support

FIXTURE = Path(__file__).parent / "fixtures" / "sh000001_day_qfq.csv"


def load_bars():
    with open(FIXTURE, encoding="utf-8") as f:
        return [{"dt": r["dt"], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                for r in csv.DictReader(f)]


def dataset(stale=False, age=1000):
    d = {"bars": load_bars(), "source": "mock 源", "fqf": "qfq", "fetch_time": "2026-08-28 12:00:00",
         "from_cache": True, "stale": stale, "degraded": True, "degraded_note": "n"}
    if stale:
        d["stale_age_s"] = age
    return d


class TestChartETag(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        cache_support.isolate_cache_dir(self)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def test_etag_present_and_stable(self):
        with mock.patch("chanapp.api.main.engine_data.get_bars", side_effect=lambda c, f, **kw: dataset()):
            r1 = self.c.get("/api/chart?code=sh000001&freq=day")
            r2 = self.c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r1.status_code, 200)
        # 有意改写（目标 2026-09-29 第三阶段）：弱 ETag，按语义等价比较（stale_age_s 不进校验器）
        self.assertTrue(r1.headers.get("etag", "").startswith('W/"'))
        self.assertEqual(r1.headers["etag"], r2.headers["etag"])
        self.assertEqual(r1.headers.get("cache-control"), "no-cache")

    def test_if_none_match_hit_returns_304_empty(self):
        with mock.patch("chanapp.api.main.engine_data.get_bars", side_effect=lambda c, f, **kw: dataset()):
            r1 = self.c.get("/api/chart?code=sh000001&freq=day")
            r2 = self.c.get("/api/chart?code=sh000001&freq=day", headers={"If-None-Match": r1.headers["etag"]})
        self.assertEqual(r2.status_code, 304)
        self.assertEqual(r2.content, b"")
        self.assertEqual(r2.headers["etag"], r1.headers["etag"])

    def test_if_none_match_uses_weak_comparison_over_a_list(self):
        """第三阶段复审可后置 7：If-None-Match 按 RFC 9110 §13.1.2 弱比较——列表中任一匹配、强形式与 *。"""
        with mock.patch("chanapp.api.main.engine_data.get_bars", side_effect=lambda c, f, **kw: dataset()):
            tag = self.c.get("/api/chart?code=sh000001&freq=day").headers["etag"]
            for header in (f'"other", {tag}', tag[2:], "*", f' {tag} '):
                r = self.c.get("/api/chart?code=sh000001&freq=day", headers={"If-None-Match": header})
                self.assertEqual(r.status_code, 304, header)
            r = self.c.get("/api/chart?code=sh000001&freq=day", headers={"If-None-Match": 'W/"other"'})
            self.assertEqual(r.status_code, 200)

    def test_stale_flip_changes_etag(self):
        """同一批 bars，meta.stale 翻转必须重发体（ETag 覆盖整包而非仅 data_version）。"""
        with mock.patch("chanapp.api.main.engine_data.get_bars", side_effect=lambda c, f, **kw: dataset(stale=False)):
            fresh = self.c.get("/api/chart?code=sh000001&freq=day")
        with mock.patch("chanapp.api.main.engine_data.get_bars", side_effect=lambda c, f, **kw: dataset(stale=True)):
            stale = self.c.get("/api/chart?code=sh000001&freq=day", headers={"If-None-Match": fresh.headers["etag"]})
        self.assertEqual(stale.status_code, 200)
        self.assertNotEqual(stale.headers["etag"], fresh.headers["etag"])
        # meta 为 dataset 透传：stale/stale_age_s 原样随响应到达前端
        self.assertTrue(stale.json()["meta"]["stale"])
        self.assertEqual(stale.json()["meta"]["stale_age_s"], 1000)

    def test_stale_age_ticking_alone_keeps_etag(self):
        """待定稿（stale）时 stale_age_s 每秒变化；只有它变化时仍是同一份数据，60 秒轮询应得 304 而不是整包重传。"""
        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=lambda c, f, **kw: dataset(stale=True, age=1000)):
            first = self.c.get("/api/chart?code=sh000001&freq=day")
        with mock.patch("chanapp.api.main.engine_data.get_bars",
                        side_effect=lambda c, f, **kw: dataset(stale=True, age=1060)):
            later = self.c.get("/api/chart?code=sh000001&freq=day", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(later.status_code, 304)

    def test_mismatch_returns_full_body(self):
        with mock.patch("chanapp.api.main.engine_data.get_bars", side_effect=lambda c, f, **kw: dataset()):
            r = self.c.get("/api/chart?code=sh000001&freq=day", headers={"If-None-Match": '"deadbeef"'})
        self.assertEqual(r.status_code, 200)
        self.assertIn("kline", r.json())

    def test_nan_payload_fails_closed_with_500(self):
        """payload 含 NaN：json.dumps 直接抛错成 500，不放行非法 JSON（原 JSONResponse 语义）。"""
        bad = {"code": "sh000001", "meta": {}, "kline": [{"close": float("nan")}]}

        def bad_payload(code, freq, ds, timings=None, **kw):
            # 与真实实现一样填 timings；否则序列化若意外放行，KeyError 同样会 500，测试失去判别力
            timings.update({"compute_ms": 0, "resonance_ms": 0})
            return bad

        from chanapp.api.main import app
        c = TestClient(app, raise_server_exceptions=False)
        with mock.patch("chanapp.api.main.engine_data.get_bars", side_effect=lambda d, f, **kw: dataset()), \
             mock.patch("chanapp.api.main.engine_chart_payload.build_chart_payload", side_effect=bad_payload):
            r = c.get("/api/chart?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 500)


if __name__ == "__main__":
    unittest.main()
