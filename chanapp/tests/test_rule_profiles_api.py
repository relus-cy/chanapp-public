"""Rule identity must isolate chart, summaries and analysis on identical bars."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from chanapp.engine import compute_cache
from chanapp.tests.test_compute_cache import load_bars


class TestRuleProfilesAPI(unittest.TestCase):
    def setUp(self):
        compute_cache.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"WARMER_ENABLED": "0", "LLM_API_KEY": "test",
                                           "ANALYSIS_CACHE_DIR": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        from chanapp.api.main import app
        self.client = TestClient(app)

    def test_cache_isolates_rule_identity(self):
        compute_cache.put("code", "day", "same-bars", {"mode": "strict"}, {}, [],
                          calculation_id="strict-id")
        self.assertIsNone(compute_cache.get("code", "day", "same-bars", "relaxed-id"))
        self.assertEqual(compute_cache.get("code", "day", "same-bars", "strict-id")
                         ["structure"]["mode"], "strict")

    def test_invalid_profile_rejected_before_fetch(self):
        with patch("chanapp.engine.data.get_bars") as fetch:
            for route in ("chart", "analysis"):
                r = self.client.get(f"/api/{route}?code=sh000001&rule_profile=invalid")
                self.assertEqual(r.status_code, 422)
            fetch.assert_not_called()

    def test_chart_roundtrip_and_analysis_cache_separation(self):
        dataset = {"bars": load_bars(), "data_version": "fixed-input"}
        answer = json.dumps({"current_state": "example", "scenarios": []})
        with patch("chanapp.engine.data.get_bars", return_value=dataset), \
             patch("chanapp.engine.llm.analyze", return_value=answer) as llm:
            responses = []
            for mode in ("strict", "relaxed", "strict"):
                chart = self.client.get(f"/api/chart?code=sh000001&rule_profile={mode}")
                self.assertEqual(chart.status_code, 200, chart.text)
                chart = chart.json()
                self.assertEqual(chart["rule_profile"], mode)
                self.assertNotIn("forming_signal", chart)
                for frame in chart["resonance"]:
                    self.assertEqual(frame["calculation_id"], chart["calculation_id"])
                    self.assertTrue(all(s["level"] in ("bi", "seg") for s in frame["signals"]))
                analysis = self.client.get(f"/api/analysis?code=sh000001&rule_profile={mode}")
                self.assertEqual(analysis.status_code, 200, analysis.text)
                result = analysis.json()
                self.assertEqual(result["calculation_id"], chart["calculation_id"])
                responses.append(result)
            self.assertEqual(llm.call_count, 2)
            self.assertNotEqual(responses[0]["hash"], responses[1]["hash"])
            self.assertEqual(responses[0]["hash"], responses[2]["hash"])
            self.assertTrue(responses[2]["cached"])

    def test_hash_isolates_profiles_even_without_signal_changes(self):
        from chanapp.api.analysis import _structure_hash
        args = ("code", "day", [], {"signals": []})
        self.assertNotEqual(_structure_hash(*args, calculation_id="strict-id"),
                            _structure_hash(*args, calculation_id="relaxed-id"))

    def test_scope_is_independent_and_roundtrip_cached(self):
        dataset = {"bars": load_bars(), "data_version": "same-input"}
        with patch("chanapp.engine.data.get_bars", return_value=dataset), \
             patch("chanapp.engine.llm.analyze", return_value='{"current_state":"s","scenarios":[]}') as llm:
            identities = {}
            hashes = {}
            for profile, scope in (("strict", "standard"), ("strict", "expanded"),
                                   ("relaxed", "expanded"), ("relaxed", "standard"),
                                   ("strict", "standard")):
                query = f"code=sh000001&rule_profile={profile}&signal_scope={scope}"
                chart = self.client.get('/api/chart?' + query)
                self.assertEqual(chart.status_code, 200, chart.text)
                body = chart.json()
                self.assertEqual(body['signal_scope'], scope)
                self.assertEqual(body['schema_version'], 'chanpy_v2')
                self.assertTrue(all(f['signal_scope'] == scope for f in body['resonance']))
                analysis = self.client.get('/api/analysis?' + query).json()
                self.assertEqual(analysis['signal_scope'], scope)
                self.assertEqual(analysis['calculation_id'], body['calculation_id'])
                key = (profile, scope)
                if key in hashes:
                    self.assertEqual(analysis['hash'], hashes[key])
                    self.assertTrue(analysis['cached'])
                identities[key] = body['calculation_id']
                hashes[key] = analysis['hash']
            self.assertEqual(len(set(identities.values())), 4)
            self.assertEqual(len(set(hashes.values())), 4)
            self.assertEqual(llm.call_count, 4)
            self.assertIn('signal_scope', llm.call_args.args[0])

    def test_bad_scope_rejected_before_fetch_and_default_is_expanded(self):
        with patch("chanapp.engine.data.get_bars") as fetch:
            for route in ('chart', 'analysis'):
                self.assertEqual(self.client.get(f'/api/{route}?code=sh000001&signal_scope=oops').status_code, 422)
            fetch.assert_not_called()
        with patch("chanapp.engine.llm.is_configured", return_value=False):
            body = self.client.get('/api/analysis?code=sh000001').json()
        self.assertEqual(body['signal_scope'], 'expanded')
