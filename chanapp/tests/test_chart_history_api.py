"""历史分页 API 契约（公开侧，中性）。"""
import asyncio
import copy
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.api import main


class TestChartHistoryApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_history_page_shape(self):
        page = {
            "code": "sh600519",
            "freq": "m30",
            "has_more": True,
            "oldest_dt": "2026-03-01 10:00",
            "basis_id": "b0",
            "revision_gen": 3,
            "segments": [{
                "basis_id": "b0",
                "from_dt": "2026-03-01 10:00",
                "to_dt": "2026-05-01 10:00",
            }],
            "incomplete_days": [{"date": "2026-03-01", "kind": "incomplete", "slots": 3}],
            "bars": [{
                "dt": "2026-03-01 10:00",
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            }],
        }
        with mock.patch.object(main.engine_data, "get_bars_history", return_value=page,
                               create=True) as getter:
            response = self.client.get(
                "/api/chart?code=sh600519&freq=m30&before=2026-06-01%2010:00&limit=200"
            )

        getter.assert_called_once_with("sh600519", "m30", "2026-06-01 10:00", 200)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], "sh600519")
        self.assertEqual(body["freq"], "m30")
        self.assertTrue(body["meta"]["history"])
        self.assertTrue(body["meta"]["has_more"])
        self.assertEqual(body["meta"]["basis_id"], "b0")
        self.assertEqual(body["meta"]["revision_gen"], 3)
        self.assertEqual(body["meta"]["oldest_dt"], "2026-03-01 10:00")
        self.assertEqual(body["meta"]["segments"], page["segments"])
        self.assertEqual(body["meta"]["incomplete_days"], page["incomplete_days"])
        self.assertEqual(body["kline"][0]["time"], "2026-03-01 10:00")
        self.assertNotIn("signals", body)
        self.assertNotIn("structure", body)
        self.assertNotIn("resonance", body)
        self.assertNotIn("macd", body)
        self.assertTrue(response.headers["ETag"].startswith('W/"'))
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def test_history_page_etag_supports_304(self):
        page = {
            "code": "sh600519",
            "freq": "day",
            "has_more": False,
            "oldest_dt": "2026-03-01",
            "basis_id": "b0",
            "revision_gen": 1,
            "segments": [],
            "incomplete_days": [],
            "bars": [{
                "dt": "2026-03-01",
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            }],
        }
        with mock.patch.object(main.engine_data, "get_bars_history", return_value=page, create=True), \
             mock.patch.object(main.engine_data, "get_bars", side_effect=AssertionError("current chart path used")):
            first = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01")
            cached = self.client.get(
                "/api/chart?code=sh600519&freq=day&before=2026-06-01",
                headers={"If-None-Match": first.headers["ETag"]},
            )

        self.assertTrue(first.headers["ETag"].startswith('W/"'))
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b"")
        self.assertEqual(cached.headers["ETag"], first.headers["ETag"])
        self.assertEqual(cached.headers["Cache-Control"], "no-cache")

    def test_unsupported_freq_400(self):
        response = self.client.get("/api/chart?code=sh600519&freq=m15&before=2026-06-01")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "该周期暂不支持历史分页"})

    def test_m60_history_400_when_flag_off(self):
        with mock.patch.object(main.engine_data, "DERIVED_READ", frozenset(), create=True):
            response = self.client.get("/api/chart?code=sh600519&freq=m60&before=2026-06-01")
        self.assertEqual(response.status_code, 400)

    def test_m60_history_200_when_flag_on(self):
        page = {"code": "sh600519", "freq": "m60", "has_more": False,
                "oldest_dt": "2026-03-01 10:30", "basis_id": "b0", "revision_gen": 1,
                "segments": [], "incomplete_days": [],
                "bars": [{"dt": "2026-03-01 10:30", "open": 1, "high": 2,
                          "low": 0.5, "close": 1.5, "volume": 100}]}
        with mock.patch.object(main.engine_data, "DERIVED_READ", {"m60"}, create=True), \
             mock.patch.object(main.engine_data, "get_bars_history", return_value=page, create=True), \
             mock.patch.object(main.engine_data, "basis_annotation", return_value=None, create=True):
            response = self.client.get("/api/chart?code=sh600519&freq=m60&before=2026-06-01")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kline"][0]["time"], "2026-03-01 10:30")

    def test_before_garbage_rejected_400(self):
        response = self.client.get("/api/chart?code=sh600519&freq=day&before=garbage")
        self.assertEqual(response.status_code, 400)

    def test_before_iso_t_normalized_to_space_form(self):
        page = {"code": "sh600519", "freq": "day", "has_more": False,
                "oldest_dt": "2026-03-01", "basis_id": "b0", "revision_gen": 1,
                "segments": [], "incomplete_days": [],
                "bars": [{"dt": "2026-03-01", "open": 1, "high": 2,
                          "low": 0.5, "close": 1.5, "volume": 100}]}
        with mock.patch.object(main.engine_data, "get_bars_history", return_value=page, create=True) as getter, \
             mock.patch.object(main.engine_data, "basis_annotation", return_value=None, create=True):
            response = self.client.get(
                "/api/chart?code=sh600519&freq=day&before=2026-06-01T10:00")
        self.assertEqual(response.status_code, 200)
        getter.assert_called_once_with("sh600519", "day", "2026-06-01 10:00", 520)

    def test_backend_missing_503(self):
        with mock.patch.object(main.engine_data, "get_bars_history", None, create=True):
            response = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "历史分页不可用"})

    def test_backend_without_history_data_503(self):
        with mock.patch.object(main.engine_data, "get_bars_history", return_value=None, create=True):
            response = self.client.get("/api/chart?code=sh600519&freq=day&before=2026-06-01")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "历史分页不可用"})

    def test_no_param_chart_gains_history_cursor(self):
        dataset = {
            "bars": [{
                "dt": "2026-09-18",
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            }],
            "source": "unit",
            "fqf": "qfq",
            "fetch_time": "2026-09-19T10:00:00+08:00",
            "degraded": True,
            "degraded_note": "t",
            "from_cache": True,
            "cache_ttl": 300,
            "stale": False,
        }
        payload = {
            "calculation_id": "relaxed-standard-v1",
            "rule_profile": "relaxed",
            "signal_scope": "standard",
            "kline": [{
                "time": "2026-09-18",
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            }],
            "macd": {"rows": []},
            "structure": {"bi": [], "xd": [], "zs": [], "forming": {}},
            "signals": [],
            "evidence": {"recent": []},
            "channels": [],
            "resonance": [],
            "meta": {
                "source": "unit",
                "bars": 1,
                "stale": False,
                "fetch_time": "2026-09-19T10:00:00+08:00",
            },
        }
        expected = copy.deepcopy(payload)
        cursor = {"basis_id": "b0", "revision_gen": 1, "has_more": False}
        basis = {"basis_id": "b0", "marker": "", "since": "2026-09-01",
                 "pending_rebuild": False}

        def build_payload(*args, timings=None, **kwargs):
            timings.update({"compute_ms": 0, "resonance_ms": 0})
            return copy.deepcopy(payload)

        with mock.patch.object(main.engine_data, "get_bars", return_value=dataset), \
             mock.patch.object(main.engine_data, "history_cursor", return_value=cursor, create=True) as cursor_getter, \
             mock.patch.object(main.engine_data, "basis_annotation", return_value=basis, create=True) as basis_getter, \
             mock.patch.object(main.engine_chart_payload, "build_chart_payload",
                               side_effect=build_payload) as builder:
            response = self.client.get(
                "/api/chart?code=sh600519&freq=day&rule_profile=relaxed&signal_scope=standard"
            )

        builder.assert_called_once()
        cursor_getter.assert_called_once_with(
            "sh600519", "day",
            expected={"source": "unit", "normalization": "baseline-v1", "fqf": "qfq",
                      "oldest_dt": "2026-09-18", "close_at_oldest": 1.5})
        basis_getter.assert_called_once_with(
            "sh600519", "day", source="unit", normalization="baseline-v1", fqf="qfq")
        self.assertEqual(builder.call_args.kwargs["rule_profile"], "relaxed")
        self.assertEqual(builder.call_args.kwargs["signal_scope"], "standard")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        expected["meta"]["history_cursor"] = cursor
        expected["meta"]["basis"] = basis
        self.assertEqual(body, expected)

    def test_no_param_chart_allows_null_history_cursor(self):
        payload = {
            "kline": [],
            "macd": {"rows": []},
            "structure": {},
            "signals": [],
            "evidence": {},
            "channels": [],
            "resonance": [],
            "meta": {"bars": 0},
        }

        def build_payload(*args, timings=None, **kwargs):
            timings.update({"compute_ms": 0, "resonance_ms": 0})
            return payload

        with mock.patch.object(main.engine_data, "get_bars", return_value={"bars": []}), \
             mock.patch.object(main.engine_data, "history_cursor", return_value=None, create=True), \
             mock.patch.object(main.engine_chart_payload, "build_chart_payload", side_effect=build_payload):
            response = self.client.get("/api/chart?code=sh600519&freq=day")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["meta"]["history_cursor"])

    def test_no_param_chart_cursor_failure_degrades_to_null(self):
        payload = {
            "kline": [],
            "macd": {"rows": []},
            "structure": {},
            "signals": [],
            "evidence": {},
            "channels": [],
            "resonance": [],
            "meta": {"bars": 0},
        }

        def build_payload(*args, timings=None, **kwargs):
            timings.update({"compute_ms": 0, "resonance_ms": 0})
            return payload

        with mock.patch.object(main.engine_data, "get_bars", return_value={"bars": []}), \
             mock.patch.object(main.engine_data, "history_cursor",
                               side_effect=RuntimeError("cursor unavailable"), create=True), \
             mock.patch.object(main.engine_chart_payload, "build_chart_payload",
                               side_effect=build_payload), \
             self.assertLogs(main.log, level="WARNING") as captured:
            response = self.client.get("/api/chart?code=sh600519&freq=day")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["meta"]["history_cursor"])
        self.assertTrue(any("cursor" in line.lower() for line in captured.output))

    def test_startup_reconciliation_failure_is_isolated(self):
        async def run_lifespan():
            async with main.lifespan(main.app):
                pass

        with mock.patch.object(main.supply_api, "candidate", None), \
             mock.patch.object(main.engine_data, "reconcile_factstore",
                               side_effect=RuntimeError("reconciliation unavailable"),
                               create=True) as reconcile, \
             self.assertLogs(main.log, level="WARNING") as captured:
            asyncio.run(run_lifespan())

        reconcile.assert_called_once_with()
        self.assertTrue(any("reconciliation" in line.lower() for line in captured.output))

    def test_startup_without_reconciliation_capability_is_normal(self):
        async def run_lifespan():
            async with main.lifespan(main.app):
                pass

        with mock.patch.object(main.supply_api, "candidate", None), \
             mock.patch.object(main.engine_data, "reconcile_factstore", None, create=True), \
             mock.patch.object(main.log, "warning") as warning:
            asyncio.run(run_lifespan())

        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
