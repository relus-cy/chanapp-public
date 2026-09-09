import unittest
from chanapp.engine.structure import compute_structure

class CoreContractTest(unittest.TestCase):
    def test_rule_identity(self):
        import chanapp.engine.structure as module
        self.assertIn('rule_profile', __import__('inspect').signature(module.compute_structure).parameters)
    def test_reject_duplicate_time(self):
        bar = dict(dt='2026-01-01', open=10., high=11., low=9., close=10., volume=1)
        with self.assertRaises(ValueError):
            compute_structure([bar, bar], 'test')

    def test_profiles_differ_only_in_strictness(self):
        from chanapp.engine.chanpy_profiles import effective_config, profile_identity
        strict, relaxed=effective_config(),effective_config('relaxed')
        self.assertTrue(strict['bi_conf']['is_strict'])
        self.assertFalse(relaxed['bi_conf']['is_strict'])
        strict['bi_conf']['is_strict']=False
        self.assertEqual(strict,relaxed)
        self.assertTrue(strict['trigger_step'])
        self.assertNotEqual(profile_identity()['calculation_id'],profile_identity('relaxed')['calculation_id'])
        with self.assertRaises(ValueError): profile_identity('invalid')

    def test_invalid_prices_and_reversed_time(self):
        good=dict(dt='2026-01-01',open=10.,high=11.,low=9.,close=10.,volume=1.)
        for patch in ({'high':8.},{'low':12.},{'close':float('nan')},{'open':0.},{'volume':-1.}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                compute_structure([{**good,**patch}],'test')
        with self.assertRaises(ValueError):
            compute_structure([{**good,'dt':'2026-01-02'},good],'test')

    def test_empty_and_short(self):
        from chanapp.engine.signals import compute_signals
        for bars in ([],[dict(dt='2026-01-01',open=10.,high=11.,low=9.,close=10.,volume=1.)]):
            result=compute_structure(bars,'test')
            self.assertEqual(result['bi'],[])
            self.assertEqual(compute_signals(bars,result)['signals'],[])

    def test_committed_effective_snapshots_match_runtime(self):
        import json
        from pathlib import Path
        from chanapp.engine.chanpy_profiles import effective_config, profile_identity
        snapshots=json.loads((Path(__file__).parents[1]/'engine/chanpy_vendor/PROFILE_SNAPSHOTS.json').read_text())
        for mode in ('strict','relaxed'):
            for scope in ('standard', 'expanded'):
                self.assertEqual(snapshots[mode][scope], {**profile_identity(mode, scope), 'effective_config':effective_config(mode, scope)})

    def test_inclusion_equal_extremes_gaps_and_mirror(self):
        from datetime import datetime, timedelta
        # Repeated equal peaks/troughs, inside bars, and price gaps exercise raw-index mapping.
        prices=[10,12,12,11,15,14,11,8,8,9,6,7]*8
        for mirrored in (False,True):
            bars=[]
            for i,value in enumerate(prices):
                value=30-value if mirrored else value
                width=.1 if i%4==2 else .5
                bars.append(dict(dt=(datetime(2026,1,1)+timedelta(days=i)).strftime('%Y-%m-%d'),
                    open=value,close=value,high=value+width,low=value-width,volume=1.))
            for mode in ('strict','relaxed'):
                result=compute_structure(bars,'test','day',mode)
                for line in result['bi']:
                    self.assertLess(line['x0'],line['x1'])
                    self.assertEqual(line['dt1'],bars[line['x1']]['dt'])
