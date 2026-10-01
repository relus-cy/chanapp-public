"""历史分页与主图 API 契约（公开侧，中性）：令牌、409 整窗重载、采集器生命周期、旧入口。"""
import asyncio
import copy
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.api import main
from chanapp.tests import cache_support


class Mismatch(Exception):
    """门面的令牌不符异常替身。"""

    def __init__(self, token):
        super().__init__("数据已更新")
        self.token = token


def page(freq="m30", dt="2026-03-01 10:00", **extra):
    return {"code": "sh600519", "freq": freq, "adjust": "qfq", "token": "t1", "has_more": True,
            "oldest_dt": dt, "fqf": "前复权", "stale": False,
            "coverage": {"qfq_from": "2020-01-02", "qfq_through": "2026-05-29", "stop_reason": None},
            "notices": [], "incomplete_days": [{"date": dt[:10], "kind": "incomplete", "slots": 3}],
            "bars": [{"dt": dt, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}], **extra}


class TestChartHistoryApi(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        cache_support.isolate_cache_dir(self)     # 主图走门面 bundle：替身之外的读取落临时目录
        self.client = TestClient(main.app)

    def test_history_page_shape(self):
        with mock.patch.object(main.engine_data, "get_bars_history", return_value=page(),
                               create=True) as getter:
            response = self.client.get(
                "/api/chart?code=sh600519&freq=m30&before=2026-06-01%2010:00&limit=200&token=t1")

        getter.assert_called_once_with("sh600519", "m30", "2026-06-01 10:00", 200, adjust="qfq", token="t1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["code"], body["freq"], body["adjust"]), ("sh600519", "m30", "qfq"))
        self.assertEqual(body["meta"], {
            "history": True, "token": "t1", "has_more": True, "oldest_dt": "2026-03-01 10:00",
            "incomplete_days": page()["incomplete_days"], "notices": [], "coverage": page()["coverage"],
            "fqf": "前复权", "stale": False})
        self.assertEqual(body["kline"][0]["time"], "2026-03-01 10:00")
        for absent in ("signals", "structure", "resonance", "macd"):
            self.assertNotIn(absent, body)
        self.assertTrue(response.headers["ETag"].startswith('W/"'))
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def test_history_page_etag_supports_304(self):
        with mock.patch.object(main.engine_data, "get_bars_history", return_value=page("day", "2026-03-01"),
                               create=True), \
             mock.patch.object(main.engine_data, "get_bars", side_effect=AssertionError("current chart path used")):
            first = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01&token=t1")
            cached = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01&token=t1",
                                     headers={"If-None-Match": first.headers["ETag"]})
        self.assertTrue(first.headers["ETag"].startswith('W/"'))
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b"")
        self.assertEqual(cached.headers["ETag"], first.headers["ETag"])

    def test_history_token_mismatch_returns_409(self):
        # 分页中途数据被定稿或修订：扁平 409 带回当前令牌，客户端整窗重载，不混接旧段
        with mock.patch.object(main.engine_data, "TokenMismatch", Mismatch, create=True), \
             mock.patch.object(main.engine_data, "get_bars_history", side_effect=Mismatch("t2"), create=True):
            response = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01&token=t1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "数据已更新", "token": "t2"})

    def test_history_without_token_is_treated_as_old_page(self):
        with mock.patch.object(main.engine_data, "TokenMismatch", Mismatch, create=True), \
             mock.patch.object(main.engine_data, "get_bars_history", side_effect=Mismatch("t2"),
                               create=True) as getter:
            response = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01")
        self.assertEqual(getter.call_args.kwargs["token"], None)
        self.assertEqual((response.status_code, response.json()["token"]), (409, "t2"))

    def test_unsupported_history_periods_400(self):
        # 有意改写（目标 2026-09-29 第三阶段）：m15/m5 整体下线，分页入口同样回「不再提供」
        for freq in ("m15", "m5"):
            response = self.client.get(f"/api/chart?code=sh600519&freq={freq}&before=2026-06-01&token=t")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"detail": "不再提供 5 分与 15 分周期，请改用 30 分及以上"})

    def test_week_and_m60_history_supported(self):
        for freq, dt in (("week", "2026-03-06"), ("m60", "2026-03-02 10:30")):
            with mock.patch.object(main.engine_data, "get_bars_history", return_value=page(freq, dt), create=True):
                response = self.client.get(f"/api/chart?code=sh600519&freq={freq}&before=2026-06-01&token=t1")
            self.assertEqual(response.status_code, 200, freq)
            self.assertEqual(response.json()["kline"][0]["time"], dt)

    def test_before_garbage_rejected_400(self):
        response = self.client.get("/api/chart?code=sh600519&freq=day&before=garbage&token=t")
        self.assertEqual(response.status_code, 400)

    def test_before_iso_t_normalized_and_adjust_forwarded(self):
        with mock.patch.object(main.engine_data, "get_bars_history", return_value=page("day", "2026-03-01"),
                               create=True) as getter:
            response = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01T10:00"
                                       "&adjust=raw&token=t1")
        self.assertEqual(response.status_code, 200)
        getter.assert_called_once_with("sh600519", "day", "2026-06-01 10:00", 520, adjust="raw", token="t1")

    def test_backend_empty_or_failed_503(self):
        for getter in (mock.Mock(return_value=None), mock.Mock(side_effect=RuntimeError("db"))):
            with mock.patch.object(main.engine_data, "get_bars_history", getter, create=True):
                response = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01&token=t")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": "历史分页不可用"})

    def test_no_param_chart_passes_meta_without_cursor_or_basis(self):
        dataset = {"bars": [{"dt": "2026-09-18", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}],
                   "token": "t1", "source": "unit", "fqf": "前复权", "stale": False}
        payload = {"calculation_id": "relaxed-standard-v1", "rule_profile": "relaxed", "signal_scope": "standard",
                   "kline": [], "macd": {"rows": []}, "structure": {}, "signals": [], "evidence": {},
                   "channels": [], "resonance": [],
                   "meta": {"token": "t1", "bars": 1,
                            "analysis_tokens": {"day": "d", "m60": "h", "m30": "m"}}}

        def build_payload(*args, timings=None, **kwargs):
            timings.update({"compute_ms": 0, "resonance_ms": 0})
            return copy.deepcopy(payload)

        def bundle(code, freqs, *, adjust="qfq", primary=None):
            return {f: dict(dataset, token=f"t-{f}") for f in freqs}

        with mock.patch.object(main.engine_data, "get_bars_bundle", side_effect=bundle, create=True) as fetch, \
             mock.patch.object(main.engine_data, "get_bars", side_effect=AssertionError("主图不应单独再读")), \
             mock.patch.object(main.engine_chart_payload, "build_chart_payload",
                               side_effect=build_payload) as builder:
            response = self.client.get("/api/chart?code=sh600519&freq=week&adjust=raw"
                                       "&rule_profile=relaxed&signal_scope=standard")
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with("sh600519", ("day", "m60", "m30", "week"), adjust="raw", primary="week")
        self.assertEqual({k: builder.call_args.kwargs[k] for k in ("rule_profile", "signal_scope", "adjust")},
                         {"rule_profile": "relaxed", "signal_scope": "standard", "adjust": "raw"})
        self.assertEqual(builder.call_args.args[2]["token"], "t-week")       # 主图取自同一次 bundle
        self.assertEqual(set(builder.call_args.kwargs["bundle"]), {"day", "m60", "m30"})
        self.assertEqual(response.json(), payload)
        self.assertNotIn("x-supply-scheme", response.headers)

    def test_old_page_parameters_and_supply_endpoint(self):
        # 旧页面残留参数一律忽略；供数入口已删除（静态托管返回 404），都不报 500
        with mock.patch.object(main.engine_data, "get_bars", side_effect=RuntimeError("no data")):
            chart = self.client.get("/api/chart?code=sh600519&freq=day&scheme=x&generation=3&epoch=e")
        self.assertEqual(chart.status_code, 502)
        self.assertEqual(self.client.get("/api/supply").status_code, 404)
        self.assertEqual(self.client.post("/api/supply", json={"scheme": "x"}).status_code in (404, 405), True)


class TestCollectorLifecycle(unittest.TestCase):
    def run_lifespan(self):
        async def run():
            async with main.lifespan(main.app):
                pass
        asyncio.run(run())

    def test_collector_started_with_watchlist_and_stopped(self):
        with mock.patch.object(main.engine_data, "start_collector", return_value=True, create=True) as start, \
             mock.patch.object(main.engine_data, "stop_collector", create=True) as stop, \
             mock.patch.object(main, "_read_watchlist_raw", return_value=[{"code": "sh600519"}]):
            self.run_lifespan()
            codes = start.call_args.args[0]()
        self.assertEqual(codes, ["sh600519"])
        stop.assert_called_once_with()

    def test_collector_start_failure_is_isolated(self):
        with mock.patch.object(main.engine_data, "start_collector", side_effect=RuntimeError("locked"),
                               create=True), \
             mock.patch.object(main.engine_data, "stop_collector", create=True) as stop, \
             self.assertLogs(main.log, level="ERROR"):
            self.run_lifespan()
        stop.assert_not_called()



if __name__ == "__main__":
    unittest.main()
