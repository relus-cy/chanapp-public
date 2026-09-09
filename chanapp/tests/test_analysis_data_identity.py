"""Offline content identity and request-snapshot isolation checks."""
import copy
import tempfile
import unittest
from unittest import mock

from chanapp.api import analysis
from chanapp.engine import chart_payload, compute_cache, supply
from chanapp.tests.test_compute_cache import load_bars


class TestAnalysisDataIdentity(unittest.TestCase):
    def setUp(self):
        compute_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict('os.environ', {'ANALYSIS_CACHE_DIR': self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_history_revision_changes_hash_without_timestamp_change(self):
        bars = load_bars()
        revised = copy.deepcopy(bars)
        revised[0]['close'] += .01
        sig = {'signals': []}
        self.assertNotEqual(analysis._structure_hash('a', 'day', bars, sig),
                            analysis._structure_hash('a', 'day', revised, sig))

    def test_equal_bars_different_schemes_have_distinct_hash(self):
        bars = load_bars()
        sig = {'signals': []}
        self.assertNotEqual(
            analysis._structure_hash('a', 'day', bars, sig, identity={'scheme': 'baseline'}),
            analysis._structure_hash('a', 'day', bars, sig, identity={'scheme': 'primary_candidate'}))

    def test_resonance_workers_use_bound_snapshot(self):
        old = supply.Snapshot('baseline', 4)
        new = supply.Snapshot('primary_candidate', 5)
        observed = []
        def read(code, freq):
            observed.append(supply.current())
            return {'bars': []}
        with mock.patch.object(supply, 'snapshot', return_value=new), supply.use(old):
            chart_payload._resonance('a', read)
        self.assertEqual(observed, [old] * 3)

    def test_cached_response_uses_current_generation_and_content_identity(self):
        dataset = {'bars': load_bars(), 'source': 'fixture', 'fqf': 'adjusted'}
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             mock.patch.object(analysis.engine_data, 'get_bars', return_value=dataset):
            with supply.use(supply.Snapshot('baseline', 0)):
                first = analysis.api_analysis('a', 'day')
            with supply.use(supply.Snapshot('baseline', 2)):
                cached = analysis.api_analysis('a', 'day')
            with supply.use(supply.Snapshot('primary_candidate', 3)):
                candidate = analysis.api_analysis('a', 'day')
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['generation'], 2)
        self.assertEqual(first['data_version'], cached['data_version'])
        self.assertNotEqual(first['data_version'], candidate['data_version'])
        self.assertEqual(llm.call_count, 2)

    def test_history_revision_recomputes_analysis(self):
        dataset = {'bars': load_bars()}
        with supply.use(supply.Snapshot('baseline', 0)), \
             mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             mock.patch.object(analysis.engine_data, 'get_bars', return_value=dataset):
            first = analysis.api_analysis('a', 'day')
            dataset['bars'][0]['close'] += .01
            second = analysis.api_analysis('a', 'day')
        self.assertFalse(second['cached'])
        self.assertNotEqual(first['data_version'], second['data_version'])
        self.assertEqual(llm.call_count, 2)

    def test_data_failure_does_not_expose_source_exception(self):
        with supply.use(supply.Snapshot('baseline', 0)), \
             mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_data, 'get_bars', side_effect=RuntimeError('private-source-url')):
            with self.assertRaises(analysis.HTTPException) as raised:
                analysis.api_analysis('a', 'day')
        self.assertEqual(raised.exception.detail, '分析数据暂不可用')

    def test_unconfigured_returns_identity_without_fetching(self):
        with supply.use(supply.Snapshot('baseline', 8)), \
             mock.patch.object(analysis.engine_llm, 'is_configured', return_value=False), \
             mock.patch.object(analysis.engine_data, 'get_bars') as fetch:
            result = analysis.api_analysis('a', 'day')
        fetch.assert_not_called()
        self.assertEqual((result['scheme'], result['generation'], result['data_version']),
                         ('baseline', 8, None))

    def test_chart_carries_content_and_snapshot_identity(self):
        with supply.use(supply.Snapshot('baseline', 6)), \
             mock.patch.object(chart_payload, '_resonance', return_value=[]):
            result = chart_payload.build_chart_payload('a', 'day', {'bars': load_bars()})
        self.assertEqual(result['generation'], 6)
        self.assertEqual(result['scheme'], 'baseline')
        self.assertEqual(result['meta']['generation'], 6)
        self.assertEqual(len(result['meta']['data_version']), 64)

class TestMultiTimeframeAnalysis(unittest.TestCase):
    setUp = TestAnalysisDataIdentity.setUp

    def test_chart_frequency_reuses_combined_analysis(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             mock.patch.object(analysis.engine_data, 'get_bars', return_value={'bars': load_bars()}) as fetch:
            first = analysis.api_analysis('a', 'day')
            second = analysis.api_analysis('a', 'm5')
        self.assertEqual(first['analysis_scope'], 'multi_timeframe')
        self.assertEqual(first['freqs'], ['day', 'm60', 'm30'])
        self.assertEqual(set(first['data_versions']), {'day', 'm60', 'm30'})
        self.assertEqual(first['hash'], second['hash'])
        self.assertTrue(second['cached'])
        self.assertEqual(llm.call_count, 1)
        self.assertEqual([c.args[1] for c in fetch.call_args_list], ['day', 'm60', 'm30'] * 2)
        for freq in first['freqs']:
            self.assertIn('"freq": "' + freq + '"', llm.call_args.args[0])

    def test_missing_timeframe_refuses_partial_analysis(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze') as llm, \
             mock.patch.object(analysis.engine_data, 'get_bars', side_effect=lambda code, freq: {'bars': [] if freq == 'm30' else load_bars()}):
            with self.assertRaises(analysis.HTTPException) as raised:
                analysis.api_analysis('a', 'day')
        self.assertEqual(raised.exception.status_code, 502)
        llm.assert_not_called()

    def test_minute_history_revision_invalidates_combined_analysis(self):
        datasets = {freq: {'bars': load_bars()} for freq in ('day', 'm60', 'm30')}
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             mock.patch.object(analysis.engine_data, 'get_bars', side_effect=lambda code, freq: datasets[freq]):
            first = analysis.api_analysis('a', 'day')
            datasets['m30']['bars'][0]['close'] += .01
            revised = analysis.api_analysis('a', 'day')
        self.assertEqual(first['data_versions']['day'], revised['data_versions']['day'])
        self.assertNotEqual(first['data_versions']['m30'], revised['data_versions']['m30'])
        self.assertNotEqual(first['data_version'], revised['data_version'])
        self.assertEqual(llm.call_count, 2)

    def test_concurrent_chart_frequencies_share_one_llm_call(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier = threading.Barrier(2)
        original_lock = analysis._analysis_lock
        def synchronized_lock(key):
            barrier.wait(timeout=5)
            return original_lock(key)
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             mock.patch.object(analysis.engine_data, 'get_bars', return_value={'bars': load_bars()}), \
             mock.patch.object(analysis, '_analysis_lock', side_effect=synchronized_lock), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda freq: analysis.api_analysis('a', freq), ['day', 'm60']))
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(results[0]['hash'], results[1]['hash'])
        self.assertEqual(sorted(r['cached'] for r in results), [False, True])
        self.assertFalse(analysis._analysis_locks)

    def test_scope_version_invalidates_previous_analysis(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             mock.patch.object(analysis.engine_data, 'get_bars', return_value={'bars': load_bars()}):
            first = analysis.api_analysis('a', 'day')
            with mock.patch.object(analysis, 'ANALYSIS_SCOPE_VERSION', 'multi_timeframe_v2'):
                revised = analysis.api_analysis('a', 'day')
        self.assertNotEqual(first['hash'], revised['hash'])
        self.assertEqual(llm.call_count, 2)
