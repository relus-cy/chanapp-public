"""周期偏好的 HTTP / 生命周期契约；录制能力，不访问上游。

失败方式先列：新实例误判升级；重启重复提示；能力变化抹掉勾选；灰色项仍可新增；
并发旧页面覆盖；损坏偏好阻止启动；写盘失败误报成功；未选分钟仍被共振读取；
demo 预置覆盖个人选择；路径偏离 instance_paths；损坏偏好静默恢复分钟采集并覆盖原文件；
提示元素损坏让页面无法渲染；应用生命周期没把偏好接到共享采集器。浏览器/采集验收另在 E2E。
未覆盖：写盘失败的故障注入（实现先原子落盘，再替换内存状态）。
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from chanapp.api.main import app
from chanapp.engine import data
from chanapp.engine.kline.providers.catalog import REGISTRY


class PeriodPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.instance = self.root / 'state'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'mode': 'real', 'instance_dir': str(self.instance)}))
        env = dict(os.environ, CHANAPP_INSTANCE_CONFIG=str(self.config), COLLECTOR_ENABLED='0')
        # 无 mode 的配置是 demo（#42）；这里验真实模式的首次提示与采集器接线，凭据只是占位，不出站
        env.update({key: 'offline-test-placeholder' for spec in REGISTRY.values() for key in spec.credentials})
        for key in ('WATCHLIST_PATH', 'VIEW_LOG_PATH', 'CHANAPP_CACHE_DIR', 'ANALYSIS_CACHE_DIR'):
            env.pop(key, None)
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_new_instance_acknowledgement_survives_restart(self):
        with TestClient(app) as client:
            response = client.get('/api/periods')
            self.assertEqual(response.status_code, 200)
            state = response.json()
            self.assertEqual(state['notice']['kind'], 'first_use')
            self.assertEqual(set(state['selected']), {'day', 'week', 'm60', 'm30'})
            saved = client.put('/api/periods', json={'selected': ['day'], 'revision': state['revision']})
            self.assertEqual(saved.status_code, 200)
            self.assertIsNone(saved.json()['notice'])
        with TestClient(app) as client:
            state = client.get('/api/periods').json()
            self.assertEqual(state['selected'], ['day'])
            self.assertIsNone(state['notice'])
        self.assertTrue((self.instance / 'periods.json').is_file())

    def test_upgrade_keeps_four_and_does_not_prompt(self):
        self.instance.mkdir()
        (self.instance / 'watchlist.json').write_text('[]')
        with TestClient(app) as client:
            state = client.get('/api/periods').json()
            self.assertEqual(set(state['selected']), {'day', 'week', 'm60', 'm30'})
            self.assertIsNone(state['notice'])

    def test_corrupt_nested_preferences_do_not_block_startup(self):
        self.instance.mkdir()
        for damaged in ({'cn': None}, {'cn': 42}):
            (self.instance / 'periods.json').write_text(json.dumps({
                'selected': ['day'], 'capabilities': damaged, 'revision': 'old', 'notice': None}))
            with self.subTest(damaged=damaged), TestClient(app) as client:
                response = client.get('/api/periods')
                self.assertEqual(response.status_code, 200)
                self.assertIn('day', response.json()['selected'])

    def test_unreadable_preferences_pause_minutes_until_confirmed_and_keep_original(self):
        self.instance.mkdir()
        damaged = '{"selected": ["day"], "capab'
        (self.instance / 'periods.json').write_text(damaged)
        with TestClient(app) as client:
            state = client.get('/api/periods').json()
            self.assertEqual(state['selected'], ['day', 'week'])
            self.assertEqual(state['notice'], {'kind': 'first_use', 'changes': []})
        kept = list(self.instance.glob('periods.json.unreadable*'))
        self.assertEqual([k.read_text() for k in kept], [damaged])

    def test_malformed_notice_changes_are_rejected_on_load(self):
        self.instance.mkdir()
        (self.instance / 'periods.json').write_text(json.dumps({
            'selected': ['day', 'm30'], 'capabilities': {'cn': ['day', 'week', 'm60', 'm30'],
                                                         'hk': ['day', 'week', 'm60', 'm30']},
            'revision': 'old', 'notice': {'kind': 'capabilities_changed', 'changes': [None]}}))
        with TestClient(app) as client:
            state = client.get('/api/periods').json()
            self.assertEqual(state['notice'], {'kind': 'first_use', 'changes': []})

    def test_lifecycle_wires_saved_selection_into_shared_collector(self):
        from chanapp.engine.kline import collector
        cache = self.instance / 'data'
        self.addCleanup(collector._shared.pop, str(cache.resolve()), None)
        with TestClient(app) as client:
            state = client.get('/api/periods').json()
            state = client.put('/api/periods', json={'selected': ['day'], 'revision': state['revision']}).json()
            self.assertFalse(collector.shared(cache).minute_enabled('sh600000'))
            client.put('/api/periods', json={'selected': ['day', 'm30'], 'revision': state['revision']})
            self.assertTrue(collector.shared(cache).minute_enabled('sh600000'))

    def test_hidden_chart_rejected_before_read_and_bundle_skips_hidden_minutes(self):
        from chanapp.engine import chart_payload
        with TestClient(app) as client:
            state = client.get('/api/periods').json()
            client.put('/api/periods', json={'selected': ['day'], 'revision': state['revision']})
            with patch.object(data, 'get_bars_bundle') as read:
                self.assertEqual(client.get('/api/chart?code=sh000001&freq=m30').status_code, 400)
                read.assert_not_called()
                read.return_value = {'day': {'bars': [{'dt': '2026-09-30'}]}}
                chart_payload.read_chart_inputs('sh000001', 'day')
                self.assertEqual(tuple(read.call_args.args[1]), ('day',))
                chart_payload.read_bundle('sh000001')
                self.assertEqual(tuple(read.call_args.args[1]), ('day',))

    def test_demo_preset_is_idempotent_and_no_prompt(self):
        from chanapp.engine import instance_paths, period_preferences
        paths = instance_paths.resolve(self.instance)
        frequencies = {'CN': 'm5', 'HK': 'm30'}
        period_preferences.preset(paths, frequencies)
        with TestClient(app) as client:
            state = client.get('/api/periods').json()
            self.assertIsNone(state['notice'])
            client.put('/api/periods', json={'selected': [], 'revision': state['revision']})
        period_preferences.preset(paths, frequencies)
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/periods').json()['selected'], [])

    def test_unacknowledged_capability_notice_disappears_when_capability_returns(self):
        with TestClient(app) as client:
            state = client.get('/api/periods').json()
            client.put('/api/periods', json={'selected': state['selected'], 'revision': state['revision']})
        self.config.write_text(json.dumps({'mode': 'real', 'instance_dir': str(self.instance),
                                          'markets': {'CN': {'minute_fact_freq': None}}}))
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/periods').json()['notice']['kind'], 'capabilities_changed')
        # 未确认前又改回原能力，不应显示已失效的“现已不可用”。
        self.config.write_text(json.dumps({'mode': 'real', 'instance_dir': str(self.instance)}))
        with TestClient(app) as client:
            self.assertIsNone(client.get('/api/periods').json()['notice'])
