"""Confirmation uses each native list's retained boundary and carrier state."""
import unittest
from pathlib import Path
from chanapp.tests.test_engine import load_fixture
from chanapp.engine.chanpy_adapter import build_native, extract_structure
from chanapp.engine.signals import compute_signals

class TestFormingSignals(unittest.TestCase):
    def test_native_confirmation_and_no_duplicate_tail(self):
        bars=load_fixture(Path(__file__).parent/'fixtures'/'sh000688_m30.csv')
        for mode in ('strict','relaxed'):
            native=build_native(bars,'m30',mode)
            structure=extract_structure(native,bars,mode)
            output=compute_signals(bars,structure)
            self.assertNotIn('forming_signal',output)
            seen=set()
            for level, points in (('bi',native.bs_point_lst),('seg',native.seg_bs_point_lst)):
                for point in points.getSortedBspList():
                    key=(level,point.bi.idx)
                    self.assertNotIn(key,seen); seen.add(key)
                    signal=next(s for s in output['signals'] if s['level']==level and s['structure_ref']['index']==point.bi.idx)
                    expected=point.bi.is_sure and point.klu.idx<=points.last_sure_pos
                    self.assertEqual(signal['status'],'confirmed' if expected else 'provisional')
                    self.assertEqual(signal['forming'],not expected)
                    self.assertEqual(signal['types'],list(dict.fromkeys(t.value for t in point.type)))
            self.assertTrue(seen)
