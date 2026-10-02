"""/api/status：采集器状态原样透传（公开侧，中性）。"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.api import main


class TestApiStatus(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_status_passes_collector_view_for_watchlist_codes(self):
        body = {"checked_at": "2026-09-28T10:00:00", "enabled": True, "datasets": [], "probes": [],
                "budget": {}, "calendar_export": "selfcheck_calendar.json"}
        with mock.patch.object(main.engine_data, "status", return_value=body, create=True) as status, \
             mock.patch.object(main, "_read_watchlist_raw", return_value=[{"code": "sh600519"},
                                                                         {"code": "hk00700"}]):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), body)
        status.assert_called_once_with(["sh600519", "hk00700"])


class TestApiVersion(unittest.TestCase):
    def test_openapi_reports_package_version(self):
        self.assertEqual(TestClient(main.app).get("/openapi.json").json()["info"]["version"], "0.7.18")


if __name__ == "__main__":
    unittest.main()
