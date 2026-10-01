"""真实样本经 CLI 初始化与 HTTP 读取；不替换业务接口。

失败方式：初始化未导入/重复增行或改令牌；覆盖个人状态；路径覆盖变量带进别的库；
校验失败仍写库；量纲错误；缺日历/参考前收使复权截断；短历史伪造补齐；
目录内符号链接把写入引到别的实例；新建状态对其他用户可读；写配置中途失败后无法重跑。
"""
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from chanapp.api.main import app

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / 'samples/demo'
PATH_VARS = ('CHANAPP_INSTANCE_CONFIG', 'CHANAPP_CACHE_DIR', 'WATCHLIST_PATH', 'VIEW_LOG_PATH', 'ANALYSIS_CACHE_DIR')


class DemoSampleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'demo'
        self.env = {k: v for k, v in os.environ.items() if k not in PATH_VARS}
        self.env['COLLECTOR_ENABLED'] = '0'
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))

    def run_cli(self, **kwargs):
        return subprocess.run([sys.executable, '-m', 'chanapp.engine.kline.seed_demo', str(self.root)],
                              cwd=ROOT.parent, env=self.env, capture_output=True, text=True, **kwargs)

    def initialize_cli(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def snapshot(self, directory):
        return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob('*')) if p.is_file()}

    def test_cli_repeat_preserves_facts_tokens_and_personal_state(self):
        first = self.initialize_cli()
        self.assertEqual(first['rows'], 23814)
        with mock.patch.dict(os.environ, {'CHANAPP_INSTANCE_CONFIG': str(self.root / 'instance.json')}):
            with TestClient(app) as client:
                chart = client.get('/api/chart?code=sz300308&freq=day').json()
                client.post('/api/watchlist', json={'code': 'sh000001', 'name': '上证指数'})
                prefs = client.get('/api/periods').json()
                self.assertEqual(prefs['selected'], ['day', 'week', 'm60', 'm30'])
                self.assertIsNone(prefs['notice'])
                saved = client.put('/api/periods', json={'selected':['day','week'], 'revision':prefs['revision']})
                self.assertEqual(saved.status_code, 200)
        watch = (self.root / 'watchlist.json').read_bytes()
        preferences = (self.root / 'periods.json').read_bytes()
        second = self.initialize_cli()
        self.assertEqual(second['rows'], 23814)
        self.assertEqual((self.root / 'watchlist.json').read_bytes(), watch)
        self.assertEqual((self.root / 'periods.json').read_bytes(), preferences)
        for datasets in second['codes'].values():
            for counts in datasets.values():
                self.assertEqual((counts['inserted'], counts['revised'], counts['pending_review'], counts['rejected']), (0, 0, 0, 0))
        with mock.patch.dict(os.environ, {'CHANAPP_INSTANCE_CONFIG': str(self.root / 'instance.json')}):
            with TestClient(app) as client:
                again = client.get('/api/chart?code=sz300308&freq=day').json()
                self.assertEqual(again['meta']['token'], chart['meta']['token'])
                self.assertEqual(again['kline'], chart['kline'])
        with sqlite3.connect(self.root / 'data/facts.sqlite') as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM current_day_bars').fetchone()[0], 486)
            self.assertEqual(conn.execute('SELECT count(*) FROM current_minute_bars').fetchone()[0], 23328)

    def test_sample_http_all_periods_units_short_history_and_local_search(self):
        self.initialize_cli()
        attempted = []
        def deny(*args, **kwargs):
            attempted.append('network')
            raise OSError('network forbidden')
        for name in ('connect', 'connect_ex'):
            self.enterContext(mock.patch.object(socket.socket, name, side_effect=deny))
        self.enterContext(mock.patch.object(socket, 'getaddrinfo', side_effect=deny))
        with mock.patch.dict(os.environ, {'CHANAPP_INSTANCE_CONFIG': str(self.root / 'instance.json')}):
            with TestClient(app) as client:
                self.assertEqual([r['code'] for r in client.get('/api/watchlist').json()], ['sz300308'])
                self.assertEqual(client.get('/api/search?q=上证').json(), [{'code':'sh000001','name':'上证指数','type':'index'}])
                for code in ('sz300308', 'sh000001'):
                    for freq, count in [('day', 243), ('week', 52), ('m30', 520), ('m60', 520)]:
                        with self.subTest(code=code, freq=freq):
                            r = client.get('/api/chart', params={'code':code,'freq':freq,'adjust':'raw'})
                            self.assertEqual(r.status_code, 200, r.text[:200])
                            body = r.json()
                            self.assertEqual(len(body['kline']), count)
                            self.assertEqual(body['meta']['volume_unit'], 'share')
                            self.assertEqual(body['meta']['coverage']['data_status']['phase'], 'historical')
                            self.assertFalse(body['meta']['stale'])
                raw = client.get('/api/chart?code=sz300308&freq=day&adjust=raw').json()
                self.assertEqual(raw['kline'][0], {'time':'2025-09-24','open':416.01,'high':435.0,'low':410.0,'close':423.5,'volume':39338600.0})
                self.assertIn('243', json.dumps(raw['meta']['notices'], ensure_ascii=False))
                self.assertIn('520', json.dumps(raw['meta']['notices'], ensure_ascii=False))
                week = client.get('/api/chart?code=sz300308&freq=week&adjust=raw').json()['kline']
                # 2026-03-09 周五根日线（样本 CSV）：开 520.1、高 568.9、低 506.0、收 542.02、量 1,287,908 手
                self.assertIn({'time':'2026-03-13','open':520.1,'high':568.9,'low':506.0,'close':542.02,
                               'volume':128790800.0}, week)
                index_raw = client.get('/api/chart?code=sh000001&freq=day&adjust=raw')
                index_qfq = client.get('/api/chart?code=sh000001&freq=day&adjust=qfq').json()
                self.assertEqual(index_qfq['kline'], index_raw.json()['kline'])
                self.assertEqual(index_qfq['meta']['adjust'], 'raw')
                cached = client.get('/api/chart?code=sh000001&freq=day&adjust=raw',
                                    headers={'If-None-Match': index_raw.headers['ETag']})
                self.assertEqual((cached.status_code, cached.content), (304, b''))
                qfq = client.get('/api/chart?code=sz300308&freq=day&adjust=qfq').json()
                self.assertEqual(len(qfq['kline']), 243)
                self.assertAlmostEqual(qfq['kline'][0]['close'], 422.56556343577626)
                self.assertEqual(qfq['kline'][-1]['close'], 895.86)
                self.assertEqual(client.get('/api/session').json()['mode'], 'demo')
                f10 = client.get('/api/f10?code=sz300308')
                self.assertEqual(f10.status_code, 200)
                self.assertEqual(f10.json(), {'data_version':None,'meta':{'mode':'demo'},'f10':{},'flow':{},
                                              'industry_pct':None,'degraded':False,'fetch_time':None})
                quotes = client.get('/api/quotes')
                self.assertEqual(quotes.status_code, 200)
                quote = quotes.json()['quotes']['sz300308']
                self.assertEqual((quote['price'], quote['price_time'], quote['price_label']),
                                 (895.86, '2026-09-24', '历史'))
        self.assertEqual(attempted, [])

    def test_corrupted_input_rejected_before_initializing_destination(self):
        from chanapp.engine.kline.seed_demo import initialize
        sample = self.root.parent / 'sample'
        shutil.copytree(SAMPLES, sample)
        with (sample / 'bars.csv').open('a') as stream:
            stream.write('\n')
        with self.assertRaisesRegex(ValueError, '校验'):
            initialize(self.root, sample_dir=sample)
        self.assertFalse(self.root.exists())

    def test_refuses_path_overrides_and_existing_foreign_config(self):
        from chanapp.engine.kline.seed_demo import initialize
        with mock.patch.dict(os.environ, {'CHANAPP_CACHE_DIR': str(self.root.parent / 'other')}):
            with self.assertRaisesRegex(ValueError, 'CHANAPP_CACHE_DIR'):
                initialize(self.root)
        self.assertFalse(self.root.exists())
        self.root.mkdir()
        (self.root / 'instance.json').write_text('{"mode":"real"}')
        with self.assertRaisesRegex(ValueError, '配置'):
            initialize(self.root)
        self.assertFalse((self.root / 'data').exists())

    def test_refuses_links_inside_instance_without_touching_their_targets(self):
        other = self.root.parent / 'other'
        (other / 'data').mkdir(parents=True)
        (other / 'data' / 'keep.txt').write_text('real')
        shutil.copy(SAMPLES / 'instance.json', other / 'instance.json')
        before = self.snapshot(other)
        self.root.mkdir()
        shutil.copy(SAMPLES / 'instance.json', self.root / 'instance.json')
        (self.root / 'data').symlink_to(other / 'data')
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('符号链接', result.stderr)
        (self.root / 'data').unlink()
        (self.root / 'instance.json').unlink()
        (self.root / 'instance.json').symlink_to(other / 'instance.json')
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('符号链接', result.stderr)
        self.assertEqual(self.snapshot(other), before)

    def test_new_instance_is_private_regardless_of_umask(self):
        self.run_cli(preexec_fn=lambda: os.umask(0o022)).check_returncode()
        modes = {name: (self.root / name).stat().st_mode & 0o777 for name in ('.', 'instance.json', 'watchlist.json')}
        self.assertEqual(modes, {'.': 0o700, 'instance.json': 0o600, 'watchlist.json': 0o600})

    def test_write_failure_while_creating_state_leaves_rerunnable_directory(self):
        import resource, signal
        def small_files():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (40, resource.RLIM_INFINITY))
        failed = self.run_cli(preexec_fn=small_files)
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertEqual(self.initialize_cli()['rows'], 23814)
        self.assertEqual((self.root / 'instance.json').read_bytes(), (SAMPLES / 'instance.json').read_bytes())
        self.assertEqual((self.root / 'watchlist.json').read_bytes(), (SAMPLES / 'watchlist.json').read_bytes())
