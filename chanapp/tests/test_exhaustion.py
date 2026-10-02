"""Native evidence does not synthesize old anchor or exhaustion metrics."""
import unittest
from chanapp.tests.test_engine import load_fixture
from chanapp.engine.chanpy_adapter import compute_analysis

class TestNativeEvidence(unittest.TestCase):
    def test_actual_native_features_only(self):
        result=compute_analysis(load_fixture())
        signals=result['sig']['signals']
        cards=result['evidence']
        self.assertTrue(cards)
        for signal,card in zip(signals,cards):
            self.assertEqual(card['detail']['features'],signal['features'])
            self.assertEqual(card['status'],signal['status'])
            for retired in ('exhaustion_pct','anchor_price','area_pair','dif_pair','fallback'):
                self.assertNotIn(retired,card['detail'])
            self.assertIn('力度仅作标注，不作硬过滤',card['text'])
