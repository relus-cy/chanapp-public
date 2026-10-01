"""可用分析周期 HTTP E2E：真实偏好、事实库、聚合、结构、缓存，仅替换上游与 LLM。

失败方式：少分钟误报 502；取消日线展示丢掉分析；旧组合令牌被接受；组合缓存/身份串用；
缺数据静默忽略；完整目录被选择或市场能力截断。
"""
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from chanapp.api.main import app
from chanapp.engine import data, period_preferences
from chanapp.engine.kline import bindings, collector, sessions
from chanapp.engine.kline.providers.registry import REGISTRY
from chanapp.tests.test_kline_collector import Clock, DAY_VOL, _list_since
from chanapp.tests.test_kline_viewing_tracking import Recording, at


class PeriodProvider(Recording):
    def minute_history(self, code, fact_freq, start, end, *, now):
        return [replace(row, volume=DAY_VOL / len(sessions.slots('CN', fact_freq)))
                for row in super().minute_history(code, fact_freq, start, end, now=now)]


class AvailablePeriodsApiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / 'config.json'
        config.write_text(json.dumps({'mode': 'real', 'instance_dir': str(self.root / 'state')}))
        env = {k: v for k, v in os.environ.items() if k not in (
            'WATCHLIST_PATH', 'VIEW_LOG_PATH', 'CHANAPP_CACHE_DIR', 'ANALYSIS_CACHE_DIR')}
        env.update(CHANAPP_INSTANCE_CONFIG=str(config), COLLECTOR_ENABLED='0', LLM_API_KEY='offline-fixture')
        env.update({key: 'offline-fixture' for spec in REGISTRY.values() for key in spec.credentials})
        self.enterContext(patch.dict(os.environ, env, clear=True))
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('unexpected network')))
        self.client = self.enterContext(TestClient(app))
        source = bindings.binding('CN', 'stock', bindings.F.MINUTE_HISTORY).primary
        worker = collector.Collector(data.CACHE_DIR, providers={source: PeriodProvider()},
            clock=Clock(at('2026-09-26', 20).timestamp()),
            minute_enabled=period_preferences.current().minute_enabled)
        self.addCleanup(worker.conn().close)
        _list_since(data.CACHE_DIR, ['sh600036', 'sh000001'], '2023-01-04')
        with patch.dict(os.environ, {'COLLECTOR_ENABLED': '1'}):
            worker.ensure_window('sh600036', 'day')
            worker.ensure_window('sh600036', 'm60')
        self.prompts = []
        def model(prompt):
            self.prompts.append(prompt)
            frames = json.loads(prompt.split('数据（JSON）：\n')[1])['timeframes']
            return json.dumps({'current_state': '本次：' + '/'.join(frames), 'scenarios': []})
        self.enterContext(patch('chanapp.api.analysis.engine_llm.analyze', side_effect=model))

    def select(self, selected):
        state = self.client.get('/api/periods').json()
        r = self.client.put('/api/periods', json={'selected': selected, 'revision': state['revision']})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def chart(self, freq='day'):
        r = self.client.get('/api/chart', params={'code': 'sh600036', 'freq': freq, 'adjust': 'raw'})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def analyze(self, tokens):
        return self.client.get('/api/analysis', params={'code': 'sh600036', 'adjust': 'raw',
                                                      'tokens': json.dumps(tokens)})

    def check_combination(self, selected, expected, text, freq='day'):
        self.select(selected)
        chart = self.chart(freq)
        self.assertEqual([x['freq'] for x in chart['resonance']], expected)
        response = self.analyze(chart['meta']['analysis_tokens'])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['freqs'], expected)
        self.assertEqual(body['current_state'], text)
        self.assertEqual(body['scenarios'], [])
        self.assertEqual(list(body['tokens']), expected)
        self.assertEqual(chart['meta']['analysis_freqs'], expected)
        self.assertEqual(body['analysis_calculation_id'], chart['meta']['analysis_calculation_id'])
        frames = json.loads(self.prompts[-1].split('数据（JSON）：\n')[1])['timeframes']
        self.assertEqual(list(frames), expected)
        self.assertEqual(frames['day']['last_bar'], {'dt': '2026-09-25', 'close': 10.0})
        if len(expected) < 3:
            self.assertIn('未提供的周期不得推断', self.prompts[-1])
        return body

    def test_day_only(self):
        self.check_combination(['day'], ['day'], '本次：day')

    def test_day_and_hour(self):
        self.check_combination(['day', 'm60'], ['day', 'm60'], '本次：day/m60')

    def test_day_and_half_hour(self):
        self.check_combination(['day', 'm30'], ['day', 'm30'], '本次：day/m30')

    def test_all_three(self):
        self.check_combination(['day', 'm60', 'm30'], ['day', 'm60', 'm30'], '本次：day/m60/m30')
        # 从修改前提交的真实 HTTP 链录下：输入、结构、共振与 prompt 保持一致。
        chart = self.chart()
        payload = {k: chart[k] for k in ('kline', 'macd', 'structure', 'signals', 'evidence', 'channels', 'resonance')}
        self.assertEqual(hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                         'bc8ab8840b6df1f96c23bc5c60a0d7e80e7166a6c48d5a79275e3bee0be8d870')
        self.assertEqual(hashlib.sha256(self.prompts[-1].encode()).hexdigest(),
                         '055e173adff489127624ba747d16cb0acf655ea19fb153e08e7dfb4946e85fe2')

    def test_day_required_even_when_only_week_displayed(self):
        self.check_combination(['week'], ['day'], '本次：day', freq='week')

    def test_combinations_have_separate_cache_and_identity_and_reject_old_tokens(self):
        hashes, identities = set(), set()
        for selected, text in ((['day'], '本次：day'), (['day', 'm60'], '本次：day/m60'),
                               (['day', 'm30'], '本次：day/m30'),
                               (['day', 'm60', 'm30'], '本次：day/m60/m30')):
            body = self.check_combination(selected, selected, text)
            self.assertFalse(body['cached'])
            hashes.add(body['hash'])
            identities.add(body['analysis_calculation_id'])
            old_tokens = body['tokens']
        self.assertEqual(len(hashes), 4)
        self.assertEqual(len(identities), 4)
        self.select(['day'])
        self.assertEqual(self.analyze(old_tokens).status_code, 409)
        cached = self.analyze(self.chart()['meta']['analysis_tokens']).json()
        self.assertEqual(cached['current_state'], '本次：day')
        self.assertTrue(cached['cached'])
        self.assertEqual(len(self.prompts), 4)

    def test_catalog_is_complete_after_deselecting_everything(self):
        state = self.select([])
        self.assertEqual(state['catalog'], [
            {'freq': 'day', 'label': '日线'}, {'freq': 'week', 'label': '周线'},
            {'freq': 'm60', 'label': '60分'}, {'freq': 'm30', 'label': '30分'}])
        self.assertEqual(self.client.get('/api/periods').json()['catalog'], state['catalog'])

    def test_read_failure_is_unavailable_not_stale(self):
        # 读失败（如写锁超时）不是数据更新：回 409 会让页面反复整窗重载
        self.select(['day', 'm60'])
        tokens = self.chart()['meta']['analysis_tokens']
        with patch.object(data, 'get_bars_bundle', side_effect=TimeoutError('database is locked')):
            response = self.analyze(tokens)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()['detail'], '分析数据暂不可用')
        self.assertEqual(self.prompts, [])
