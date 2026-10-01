"""Offline content identity checks: analysis prompt hash, bundle-sourced tokens, no supply identity."""
import copy
import hashlib
import unittest
from unittest import mock

from chanapp.api import analysis
from chanapp.engine import chart_payload, compute_cache
from chanapp.tests import cache_support, facade_support
from chanapp.tests.test_compute_cache import load_bars

TOKENS = facade_support.tokens()


class TestAnalysisDataIdentity(unittest.TestCase):
    def setUp(self):
        compute_cache.clear()
        tmp = cache_support.temp_dir(self)
        cache_support.set_env(self, 'ANALYSIS_CACHE_DIR', tmp)
        cache_support.isolate_cache_dir(self, tmp)

    def test_resonance_and_analysis_tokens_come_from_one_bundle_read(self):
        datasets = {f: {'bars': load_bars(), 'token': f't-{f}'} for f in ('day', 'm60', 'm30')}
        calls = []
        def bundle(code, freqs, *, adjust='qfq'):
            calls.append((code, tuple(freqs), adjust))
            return {f: datasets[f] for f in freqs}
        with mock.patch.object(chart_payload.engine_data, 'get_bars_bundle', side_effect=bundle, create=True):
            result = chart_payload.build_chart_payload('a', 'day', {'bars': load_bars()}, adjust='raw')
        self.assertEqual(calls, [('a', ('day', 'm60', 'm30'), 'raw')])
        self.assertEqual(result['meta']['analysis_tokens'], {'day': 't-day', 'm60': 't-m60', 'm30': 't-m30'})
        self.assertEqual([r['freq'] for r in result['resonance']], ['day', 'm60', 'm30'])

    def test_cache_key_is_prompt_hash(self):
        """结果缓存键 = prompt 文本哈希：miss ⇔ LLM 输入真的变化。"""
        dataset = {'bars': load_bars()}
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             facade_support.fake_facade(return_value=dataset):
            result = analysis.api_analysis('a', 'day', tokens=TOKENS)
        prompt = llm.call_args.args[0]
        self.assertEqual(result['hash'], hashlib.sha256(prompt.encode('utf-8')).hexdigest())

    def test_cached_response_keeps_content_identity(self):
        dataset = {'bars': load_bars(), 'source': 'fixture', 'fqf': 'adjusted'}
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             facade_support.fake_facade(return_value=dataset):
            first = analysis.api_analysis('a', 'day', tokens=TOKENS)
            cached = analysis.api_analysis('a', 'day', tokens=TOKENS)
        self.assertTrue(cached['cached'])
        self.assertEqual(first['data_version'], cached['data_version'])
        self.assertEqual(llm.call_count, 1)
        self.assertFalse({'scheme', 'generation', 'epoch'} & set(cached))
        revised = {'bars': copy.deepcopy(dataset['bars']), 'source': 'fixture', 'fqf': 'adjusted'}
        revised['bars'][-1]['close'] += .01   # prompt 可见内容变化 → 重新分析
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm2, \
             facade_support.fake_facade(return_value=revised):
            changed = analysis.api_analysis('a', 'day', tokens=TOKENS)
        self.assertFalse(changed['cached'])
        self.assertNotEqual(first['data_version'], changed['data_version'])
        self.assertEqual(llm2.call_count, 1)

    def test_last_bar_change_recomputes_analysis(self):
        dataset = {'bars': load_bars()}
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             facade_support.fake_facade(return_value=dataset):
            first = analysis.api_analysis('a', 'day', tokens=TOKENS)
            dataset['bars'][-1]['close'] += .01   # 末bar（prompt 可见）变化 → 重新分析
            second = analysis.api_analysis('a', 'day', tokens=TOKENS)
        self.assertFalse(second['cached'])
        self.assertNotEqual(first['data_version'], second['data_version'])
        self.assertEqual(llm.call_count, 2)

    def test_data_failure_does_not_expose_source_exception(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             facade_support.fake_facade(lambda c, f: (_ for _ in ()).throw(RuntimeError('private-source-url'))):
            with self.assertRaises(analysis.HTTPException) as raised:
                analysis.api_analysis('a', 'day', tokens=TOKENS)
        self.assertEqual(raised.exception.detail, '分析数据暂不可用')

    def test_unconfigured_returns_identity_without_fetching(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=False), \
             facade_support.fake_facade(return_value={'bars': []}) as fetch:
            result = analysis.api_analysis('a', 'day')
        fetch.assert_not_called()
        self.assertEqual((result['adjust'], result['tokens'], result['data_version']), ('qfq', None, None))

    def test_chart_carries_content_identity(self):
        with mock.patch.object(chart_payload, '_resonance', return_value=[]):
            result = chart_payload.build_chart_payload('a', 'day', {'bars': load_bars()},
                                                       bundle={'day': None, 'm60': None, 'm30': None})
        self.assertFalse({'scheme', 'generation', 'epoch'} & set(result))
        self.assertEqual(len(result['meta']['data_version']), 64)
        self.assertEqual(result['meta']['analysis_tokens'], {'day': None, 'm60': None, 'm30': None})

class TestChartSingleRead(unittest.TestCase):
    """主图、共振与 AI 令牌出自同一次 bundle 读取（spec §6.3）：当前周期不在共振三周期内时也并入同一次读取。"""

    def setUp(self):
        TestAnalysisDataIdentity.setUp(self)
        cache_support.set_env(self, 'COLLECTOR_ENABLED', '0')

    def chart(self, freq, datasets=None):
        from fastapi.testclient import TestClient
        from chanapp.api.main import app
        datasets = datasets or {}
        calls = []

        def bundle(code, freqs, *, adjust='qfq', primary=None):
            calls.append((tuple(freqs), primary))
            return {f: copy.deepcopy(datasets.get(f) or {'bars': load_bars(), 'token': f't-{f}'}) for f in freqs}

        with mock.patch.object(chart_payload.engine_data, 'get_bars_bundle', side_effect=bundle, create=True), \
             mock.patch.object(chart_payload.engine_data, 'get_bars', side_effect=AssertionError('第二次读取')):
            response = TestClient(app).get(f'/api/chart?code=sh000001&freq={freq}')
        return response, calls

    def test_chart_reads_main_and_resonance_in_one_bundle(self):
        for freq, freqs in (('day', ('day', 'm60', 'm30')), ('m30', ('day', 'm60', 'm30')),
                            ('week', ('day', 'm60', 'm30', 'week'))):     # m5 已下线（目标 2026-09-29 第三阶段）
            with self.subTest(freq=freq):
                response, calls = self.chart(freq)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(calls, [(freqs, freq)])        # 主图周期作为 primary 传给门面
                meta = response.json()['meta']
                self.assertEqual(meta['token'], f't-{freq}')
                self.assertEqual(meta['analysis_tokens'], {'day': 't-day', 'm60': 't-m60', 'm30': 't-m30'})

    def test_empty_main_view_is_502_unless_unsupported(self):
        # 主图用周线（共振以外的周期；原用 m5，已下线）
        empty = {'bars': [], 'token': 't-week', 'notices': []}
        response, _ = self.chart('week', {'week': empty})
        self.assertEqual(response.status_code, 502)
        unsupported = dict(empty, notices=[{'code': 'unsupported', 'text': '该市场暂不提供'}])
        response, _ = self.chart('week', {'week': unsupported})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['kline'], [])


class TestShortInput(unittest.TestCase):
    """结构输入不足（structure_short）：共振与 AI 带标记、不阻断，不以短窗口结果冒充完整结论。"""
    setUp = TestAnalysisDataIdentity.setUp
    SHORT = {'code': 'structure_short', 'text': '结构输入尚未补齐'}

    def dataset(self, code, freq):
        return {'bars': load_bars(), 'notices': [self.SHORT] if freq == 'm60' else []}

    def test_resonance_marks_short_levels(self):
        bundle = {f: self.dataset('a', f) for f in ('day', 'm60', 'm30')}
        res = chart_payload._resonance('a', bundle)
        self.assertEqual({r['freq']: r['structure_short'] for r in res}, {'day': False, 'm60': True, 'm30': False})

    def test_analysis_flags_short_frames_in_prompt_and_response(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             facade_support.fake_facade(self.dataset):
            result = analysis.api_analysis('a', 'day', tokens=TOKENS)
        self.assertEqual(result['structure_short'], ['m60'])
        prompt = llm.call_args.args[0]
        self.assertIn('"structure_short": true', prompt)
        self.assertIn('输入不足', prompt)

    def test_full_input_prompt_has_no_short_marker(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             facade_support.fake_facade(return_value={'bars': load_bars()}):
            result = analysis.api_analysis('a', 'day', tokens=TOKENS)
        self.assertEqual(result['structure_short'], [])
        self.assertNotIn('structure_short', llm.call_args.args[0])


class TestMultiTimeframeAnalysis(unittest.TestCase):
    setUp = TestAnalysisDataIdentity.setUp

    def test_chart_frequency_reuses_combined_analysis(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             facade_support.fake_facade(return_value={'bars': load_bars()}) as fetch:
            first = analysis.api_analysis('a', 'day', tokens=TOKENS)
            second = analysis.api_analysis('a', 'm30', tokens=TOKENS)   # 原用 m5（已下线）
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
             facade_support.fake_facade(lambda code, freq: {'bars': [] if freq == 'm30' else load_bars()}):
            with self.assertRaises(analysis.HTTPException) as raised:
                analysis.api_analysis('a', 'day', tokens=TOKENS)
        self.assertEqual(raised.exception.status_code, 502)
        llm.assert_not_called()

    def test_minute_last_bar_change_invalidates_combined_analysis(self):
        datasets = {freq: {'bars': load_bars()} for freq in ('day', 'm60', 'm30')}
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             facade_support.fake_facade(lambda code, freq: datasets[freq]):
            first = analysis.api_analysis('a', 'day', tokens=TOKENS)
            datasets['m30']['bars'][-1]['close'] += .01   # 末bar（prompt 可见）变化 → 换键
            revised = analysis.api_analysis('a', 'day', tokens=TOKENS)
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
             facade_support.fake_facade(return_value={'bars': load_bars()}), \
             mock.patch.object(analysis, '_analysis_lock', side_effect=synchronized_lock), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda freq: analysis.api_analysis('a', freq, tokens=TOKENS), ['day', 'm60']))
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(results[0]['hash'], results[1]['hash'])
        self.assertEqual(sorted(r['cached'] for r in results), [False, True])
        self.assertFalse(analysis._analysis_locks)

    def test_scope_version_invalidates_previous_analysis(self):
        with mock.patch.object(analysis.engine_llm, 'is_configured', return_value=True), \
             mock.patch.object(analysis.engine_llm, 'analyze', return_value='{"current_state":"s","scenarios":[]}') as llm, \
             facade_support.fake_facade(return_value={'bars': load_bars()}):
            first = analysis.api_analysis('a', 'day', tokens=TOKENS)
            with mock.patch.object(analysis, 'ANALYSIS_SCOPE_VERSION', 'multi_timeframe_v2'):
                revised = analysis.api_analysis('a', 'day', tokens=TOKENS)
        self.assertNotEqual(first['hash'], revised['hash'])
        self.assertEqual(llm.call_count, 2)
