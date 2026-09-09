"""Offline native engine data/display contracts; old rule goldens are retired."""
import csv
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from chanapp.engine import evidence, signals, structure

FIXTURE = Path(__file__).parent / 'fixtures' / 'sh000001_day_qfq.csv'

def load_fixture(path=FIXTURE):
    with open(path, encoding='utf-8') as stream:
        return [{k: v if k == 'dt' else float(v) for k,v in row.items()} for row in csv.DictReader(stream)]

class TestNativeEngine(unittest.TestCase):
    def test_pipeline_raw_coordinates_and_evidence(self):
        bars = load_fixture()
        for profile in ('strict', 'relaxed'):
            result = structure.compute_structure(bars, 'test', rule_profile=profile)
            points = signals.compute_signals(bars, result)
            self.assertTrue(result['bi'])
            self.assertTrue(points['signals'])
            for key in ('bi', 'xd'):
                for line in result[key]:
                    self.assertEqual(line['dt0'], bars[line['x0']]['dt'])
                    self.assertEqual(line['dt1'], bars[line['x1']]['dt'])
                    endpoint = bars[line['x1']]
                    self.assertEqual(line['y1'], endpoint['high' if line['direction']=='up' else 'low'])
            self.assertEqual(len(evidence.build_evidence(points['signals'],result)),len(points['signals']))
            for values in points['macd'].values(): self.assertEqual(len(values),len(bars))

class TestMinuteFreqStructure(unittest.TestCase):
    def test_m15_m5_structure(self):
        for freq, step in (('m15',15),('m5',5)):
            price=100.; bars=[]
            for i in range(200):
                price += 1 if (i//5)%2==0 else -1
                bars.append(dict(dt=(datetime(2026,9,1,9,30)+timedelta(minutes=i*step)).strftime('%Y-%m-%d %H:%M'),
                                 open=price-.2,high=price+.5,low=price-.6,close=price,volume=1000.))
            self.assertTrue(structure.compute_structure(bars,'test',freq)['bi'])
