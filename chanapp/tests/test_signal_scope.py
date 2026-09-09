import unittest
from types import SimpleNamespace as NS
from chanapp.engine.chanpy_profiles import effective_config, profile_identity
from chanapp.engine.chanpy_adapter import normalize_points


class SignalScopeTest(unittest.TestCase):
    def test_scope_changes_only_stroke_center_threshold(self):
        ids = set()
        for profile in ('strict', 'relaxed'):
            standard = effective_config(profile, 'standard')
            expanded = effective_config(profile, 'expanded')
            for side in ('b_conf', 's_conf'):
                self.assertEqual(standard['bs_point_conf'][side]['min_zs_cnt'], 1)
                self.assertEqual(expanded['bs_point_conf'][side]['min_zs_cnt'], 0)
                standard['bs_point_conf'][side]['min_zs_cnt'] = 0
            self.assertEqual(standard, expanded)
            for scope in ('standard', 'expanded'):
                ids.add(profile_identity(profile, scope)['calculation_id'])
        self.assertEqual(len(ids), 4)

    def test_native_context_strength_and_confirmation(self):
        from chanapp.engine.chanpy_profiles import make_config
        def point(ratio, centers, types=('1p',), relation=None):
            line = NS(idx=2, is_sure=True, parent_seg=NS(idx=1, zs_lst=[object()]*centers), get_end_val=lambda: 10.)
            return NS(bi=line, klu=NS(idx=0), type=[NS(value=t) for t in types], is_buy=True,
                      relate_bsp1=relation, features={} if ratio is None else {'divergence_rate': ratio})
        bars = [{'dt': '2026-01-01'}]
        for ratio, state in ((0., 'weaker'), (1., 'equal'), (2., 'stronger'), (None, 'unavailable'), (float('inf'), 'unavailable'), (float('nan'), 'unavailable')):
            p = point(ratio, 1)
            output = normalize_points(NS(getSortedBspList=lambda: [p], last_sure_pos=-1), bars, 'bi', make_config().bs_point_conf)[0]
            self.assertEqual(output['strength']['state'], state)
            self.assertEqual(output['context']['origin'], 'centered')
            self.assertEqual(output['context']['zs_count'], 1)
            self.assertEqual(output['status'], 'provisional')
        p = point(None, 2, ('2', '2'), point(0.5, 0))
        output = normalize_points(NS(getSortedBspList=lambda: [p], last_sure_pos=0), bars, 'bi', make_config().bs_point_conf)[0]
        self.assertEqual(output['context']['origin'], 'zero_center')
        self.assertEqual(output['context']['zs_count'], 2)
        self.assertEqual(output['context']['related_bsp1_context']['zs_count'], 0)
        self.assertEqual(output['strength']['state'], 'unavailable')
        self.assertEqual(output['types'], ['2'])
        self.assertEqual(output['status'], 'confirmed')
        p.bi.parent_seg = None
        p.bi.seg_idx = 7
        parents = [NS(idx=7, zs_lst=[object()])]
        output = normalize_points(NS(getSortedBspList=lambda: [p], last_sure_pos=0), bars, 'bi', make_config().bs_point_conf, parents)[0]
        self.assertEqual(output['context']['parent_segment_index'], 7)
        self.assertEqual(output['context']['zs_count'], 1)
        p.relate_bsp1 = None
        output = normalize_points(NS(getSortedBspList=lambda: [p], last_sure_pos=0), bars, 'bi', make_config().bs_point_conf)[0]
        self.assertIsNone(output['context']['zs_count'])
        self.assertEqual(output['context']['origin'], 'unavailable')

    def test_recorded_native_parent_context_and_no_duplicates(self):
        from pathlib import Path
        from chanapp.tests.test_engine import load_fixture
        from chanapp.engine.chanpy_adapter import build_native, extract_structure
        bars = load_fixture(Path(__file__).parent / 'fixtures' / 'sh000688_m30.csv')
        for profile in ('strict', 'relaxed'):
            for scope in ('standard', 'expanded'):
                native = build_native(bars, 'm30', profile, scope)
                result = extract_structure(native, bars, profile, scope)
                self.assertEqual(result['signal_scope'], scope)
                signals = result['_native_signals']
                self.assertEqual(len(signals), len({(s['level'], s['structure_ref']['index']) for s in signals}))
                for level, points in (('bi', native.bs_point_lst), ('seg', native.seg_bs_point_lst)):
                    for point in points.getSortedBspList():
                        signal = next(s for s in signals if s['level'] == level and s['structure_ref']['index'] == point.bi.idx)
                        if point.bi.parent_seg is not None:
                            self.assertEqual(signal['context']['zs_count'], len(point.bi.parent_seg.zs_lst))
                        confirmed = point.bi.is_sure and point.klu.idx <= points.last_sure_pos
                        self.assertEqual(signal['status'], 'confirmed' if confirmed else 'provisional')

    def test_evidence_names_metric_and_related_origin(self):
        from chanapp.engine.evidence import build_evidence
        signal = dict(level='bi', status='confirmed', types=['2'], macd_algo='peak',
                      label='B2', dt='2026-01-01', price=10., side='buy', forming=False,
                      context=dict(origin='zero_center', origin_source='related_bsp1', zs_count=2),
                      strength=dict(metric='peak', value=None, state='unavailable'))
        card = build_evidence([signal], {})[0]
        self.assertIn('关联无中枢一类点', card['text'])
        self.assertIn('MACD同向柱峰值', card['text'])
        self.assertIn('无可用原生力度比', card['text'])
        self.assertEqual(card['detail']['context'], signal['context'])
        signal['strength'] = dict(metric='slope', value=0., state='weaker')
        text = build_evidence([signal], {})[0]['text']
        self.assertIn('价格变化斜率', text)
        self.assertIn('力度比 0（减弱）', text)
