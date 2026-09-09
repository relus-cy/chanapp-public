"""Multi-type native points remain one point and retain their level."""
import unittest
from types import SimpleNamespace
from chanapp.engine.chanpy_adapter import normalize_points
from chanapp.engine.chanpy_profiles import make_config
from chanapp.engine.chanpy_vendor.Common.CEnum import BSP_TYPE

class TestNativePointMapping(unittest.TestCase):
    def test_multitype_both_levels_and_sides(self):
        for level in ('bi','seg'):
            for buy in (True,False):
                line=SimpleNamespace(idx=4,is_sure=True,get_end_val=lambda:12.)
                point=SimpleNamespace(klu=SimpleNamespace(idx=0),bi=line,is_buy=buy,
                    type=[BSP_TYPE.T1P,BSP_TYPE.T2],relate_bsp1=None,features={'native_value':2.})
                points=SimpleNamespace(last_sure_pos=0,getSortedBspList=lambda:[point])
                config=make_config()
                result=normalize_points(points,[{'dt':'2026-01-01'}],level,
                    config.bs_point_conf if level=='bi' else config.seg_bs_point_conf)
                self.assertEqual(len(result),1)
                self.assertEqual(result[0]['types'],['1p','2'])
                self.assertEqual(result[0]['level'],level)
                self.assertEqual(result[0]['side'],'buy' if buy else 'sell')
                self.assertEqual(result[0]['macd_algo'],'peak' if level=='bi' else 'slope')
                self.assertEqual(result[0]['status'],'confirmed')

    def test_confirmation_requires_both_native_conditions(self):
        for sure,boundary,expected in ((True,0,'confirmed'),(True,-1,'provisional'),(False,0,'provisional')):
            line=SimpleNamespace(idx=0,is_sure=sure,get_end_val=lambda:10.)
            point=SimpleNamespace(klu=SimpleNamespace(idx=0),bi=line,is_buy=True,
                type=[BSP_TYPE.T1],relate_bsp1=None,features={})
            points=SimpleNamespace(last_sure_pos=boundary,getSortedBspList=lambda:[point])
            result=normalize_points(points,[{'dt':'2026-01-01'}],'bi',make_config().bs_point_conf)
            self.assertEqual(result[0]['status'],expected)
