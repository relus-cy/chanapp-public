"""Minute fixtures preserve raw timestamps after inclusion merging."""
import unittest
from pathlib import Path
from chanapp.tests.test_engine import load_fixture
from chanapp.engine.structure import compute_structure

class TestM30Structure(unittest.TestCase):
    def test_minute_coordinates(self):
        for name in ('sh000688_m30.csv','sz399006_m30.csv'):
            bars=load_fixture(Path(__file__).parent/'fixtures'/name)
            for mode in ('strict','relaxed'):
                result=compute_structure(bars,'test','m30',mode)
                self.assertLess(result['counts']['cl_kline'],len(bars))
                for key in ('bi','xd','zs','zs_xd'):
                    for item in result[key]:
                        self.assertEqual(item['dt0'],bars[item['x0']]['dt'])
                        self.assertEqual(item['dt1'],bars[item['x1']]['dt'])
