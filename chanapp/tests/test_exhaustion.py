"""Native evidence does not synthesize old anchor or exhaustion metrics."""
import unittest
from chanapp.tests.test_engine import load_fixture
from chanapp.engine.structure import compute_structure
from chanapp.engine.signals import compute_signals
from chanapp.engine.evidence import build_evidence

class TestNativeEvidence(unittest.TestCase):
    def test_actual_native_features_only(self):
        bars=load_fixture(); structure=compute_structure(bars,'test')
        signals=compute_signals(bars,structure)['signals']
        cards=build_evidence(signals,structure)
        self.assertTrue(cards)
        for signal,card in zip(signals,cards):
            self.assertEqual(card['detail']['features'],signal['features'])
            self.assertEqual(card['status'],signal['status'])
            for retired in ('exhaustion_pct','anchor_price','area_pair','dif_pair','fallback'):
                self.assertNotIn(retired,card['detail'])
            self.assertIn('不要求 MACD',card['text'])
