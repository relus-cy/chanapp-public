"""实例加载→事实写入→冻结门面的离线集成验收。

2024-09-27 一组：规范化 m15 仅由同一 m5 录制每三槽聚合，另以同日独立录制的 m30 核对。
2026-09-29 一组是同日原生 m5、m15、m30、m60（m5/m30/m60 出自 2026-09-30 同一轮取证，
m15/m30 与 09-29 晚的录制逐行一致），用于证明两种事实粒度聚合出的 30 分、60 分相同。
"""
from datetime import datetime
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from chanapp.engine import data
from chanapp.engine.kline import bindings, collector, facts, instance
from chanapp.engine.kline.providers.mairui import MairuiProvider
from chanapp.engine.kline.providers.catalog import REGISTRY
from chanapp.engine.kline.rows import FetchItem, RawDayRow, new_batch_id
from chanapp.tests import cache_support

FIX = Path(__file__).parent / 'fixtures' / 'kline_raw' / 'mairui'
CODE = 'sh600036'
DAY = '2024-09-27'
NOW = datetime(2026, 10, 1, 22)


def recording(name):
    return json.loads((FIX / name).read_text())


def aggregate_recorded(rows, count):
    """测试输入规范化，不调用产品聚合函数；不声明为原生录制。"""
    groups = [rows[n:n + count] for n in range(0, len(rows), count)]
    out = []
    for group in groups:
        # 录制中的零量平价槽是无成交占位：只在整组都无成交时保留。
        traded = [r for r in group if r['v'] > 0] or group
        out.append({**group[-1], 'o': traded[0]['o'], 'c': traded[-1]['c'],
                    'h': max(r['h'] for r in traded), 'l': min(r['l'] for r in traded),
                    'v': sum(r['v'] for r in group), 'a': sum(r['a'] for r in group)})
    return out


class InstanceReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cache_support.isolate_cache_dir(self, self.root)
        self.enterContext(patch.dict('os.environ', {'COLLECTOR_ENABLED': '0'}))
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('no network')))
        self.worker = collector.shared(self.root)
        self.addCleanup(self.worker.conn().close)
        self.config_path = self.root / 'instance.json'

    def settings(self, freq='m15'):
        self.config_path.write_text(json.dumps({
            'mode': 'real', 'markets': {'CN': {'source': 'mairui', 'minute_fact_freq': freq},
                                      'HK': {'source': 'longbridge', 'minute_fact_freq': 'm30'}},
            'quota': {'per_minute': 300, 'per_day': 10000}}))
        credentials = {key: 'recorded-fixture-only' for p in REGISTRY.values() for key in p.credentials}
        return instance.load_instance(self.config_path, environ=credentials)

    def rows(self, freq, body=None, name=None):
        rec = recording(name or ('m5-600036-20240927.json' if freq == 'm5' else 'm15-600036-20260929.json'))
        payload = rec['body'] if body is None else body
        self.transport_calls = []

        def transport(path, params, timeout):
            self.transport_calls.append((path, params))
            return 200, payload

        rows = MairuiProvider(transport=transport, licence='recorded-fixture-only').minute_history(
            CODE, freq, payload[0]['t'][:10] + ' 09:30', payload[-1]['t'][:10] + ' 15:00', now=NOW)
        self.assertIn('/5/' if freq == 'm5' else '/15/', self.transport_calls[-1][0])
        return rows

    def seed_minutes(self, rows, freq, gen=None):
        with self.worker.writer() as conn:
            source, current_gen = bindings.active(conn, 'CN', 'stock', FetchItem.MINUTE_HISTORY)
            result = facts.commit_minute_rows(conn, rows, market='CN', kind='stock',
                item='minute_history', fact_freq=freq, source=source,
                binding_gen=current_gen if gen is None else gen, today=NOW.date().isoformat())
        self.assertEqual(result.rejected, [])
        return current_gen

    def seed_day(self, rows):
        # 辅助日线由同一录制输入规范化，避免声称为独立日线行情。
        day = rows[0].trade_date
        row = RawDayRow(CODE, day, rows[0].open, max(r.high for r in rows),
                        min(r.low for r in rows), rows[-1].close, sum(r.volume for r in rows),
                        'lot', sum(r.amount or 0 for r in rows), 'CNY', rows[0].open, 0,
                        'final', new_batch_id())
        with self.worker.writer() as conn:
            source, gen = bindings.active(conn, 'CN', 'stock', FetchItem.DAY_HISTORY)
            result = facts.commit_day_rows(conn, [row], market='CN', kind='stock',
                item='day_history', source=source, binding_gen=gen, today=NOW.date().isoformat())
        self.assertEqual(result.rejected, [])

    def test_daily_only_cold_cache_returns_explicit_unsupported(self):
        with instance.activate(self.settings(None), self.root):
            for freq in ('m30', 'm60'):
                out = data.get_bars(CODE, freq, adjust='raw')
                self.assertEqual(out['bars'], [])
                self.assertIn('unsupported', [n['code'] for n in out['notices']])

    def assert_recorded_bars(self, actual, expected):
        self.assertEqual([b['dt'] for b in actual], [b['t'][:16] for b in expected])
        for got, reference in zip(actual, expected):
            for field, raw in [('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c')]:
                self.assertAlmostEqual(got[field], reference[raw], delta=0.01)
            # 上游手转换成股；容差只允许浮点累计误差。
            self.assertAlmostEqual(got['volume'], reference['v'] * 100, delta=0.01)

    def test_recorded_m5_switches_to_normalized_m15_without_mixing_old_facts(self):
        raw_m5 = recording('m5-600036-20240927.json')['body']
        native_m30 = recording('m30-600036-20240927.json')['body']
        normalized_m15 = aggregate_recorded(raw_m5, 3)
        with instance.activate(self.settings('m5'), self.root):
            old_rows = self.rows('m5')
            self.seed_day(old_rows)
            old_gen = self.seed_minutes(old_rows, 'm5')
            before = {freq: data.get_bars(CODE, freq, adjust='raw') for freq in ('m30', 'm60')}
            self.assert_recorded_bars(before['m30']['bars'], native_m30)
            self.assert_recorded_bars(before['m60']['bars'], aggregate_recorded(native_m30, 2))

        with instance.activate(self.settings('m15'), self.root):
            # 尚未采到新粒度时不能借旧 m5 冒充 m15。
            with self.assertRaises(data.DataUnavailable):
                data.get_bars(CODE, 'm30', adjust='raw')
            new_rows = self.rows('m15', normalized_m15)
            new_gen = self.seed_minutes(new_rows, 'm15')
            self.assertGreater(new_gen, old_gen)
            with self.assertRaises(facts.StaleBinding):
                self.seed_minutes(old_rows, 'm5', gen=old_gen)
            for freq in ('m30', 'm60'):
                after = data.get_bars(CODE, freq, adjust='raw')
                self.assertNotEqual(before[freq]['token'], after['token'])
                self.assertEqual(before[freq]['bars'], after['bars'])
            unsupported = data.get_bars(CODE, 'm5', adjust='raw')
            self.assertEqual(unsupported['bars'], [])
            self.assertIn('unsupported', [n['code'] for n in unsupported['notices']])

        # 回切继续读取旧 m5，证明升级保留了旧事实；新 m15 不串入结果。
        with instance.activate(self.settings('m5'), self.root):
            back = data.get_bars(CODE, 'm30', adjust='raw')
            self.assertEqual(back['bars'], before['m30']['bars'])
            self.assertNotEqual(back['token'], before['m30']['token'])

    def test_daily_only_keeps_day_week_and_hides_existing_minute_facts(self):
        with instance.activate(self.settings('m5'), self.root):
            rows = self.rows('m5')
            self.seed_day(rows)
            self.seed_minutes(rows, 'm5')
            expected = {freq: data.get_bars(CODE, freq, adjust='raw')['bars'] for freq in ('day', 'week')}
        with instance.activate(self.settings(None), self.root):
            for freq in ('m30', 'm60'):
                out = data.get_bars(CODE, freq, adjust='raw')
                self.assertEqual(out['bars'], [])
                self.assertIn('unsupported', [n['code'] for n in out['notices']])
            for freq in ('day', 'week'):
                bars = data.get_bars(CODE, freq, adjust='raw')['bars']
                self.assertTrue(bars)
                self.assertEqual(bars, expected[freq])

    def test_same_day_native_m5_and_m15_give_same_m30_m60(self):
        # 工单 #38 验收：同日原生 m5 作 A 股事实粒度，聚合的 30 分、60 分与原生 m15 基线一致，
        # 两者都与同日原生 m30、m60 录制一致。
        m15_rows = self.rows('m15')
        self.seed_day(m15_rows)
        reads = {}
        for freq, name in (('m5', 'm5-600036-20260929.json'), ('m15', 'm15-600036-20260929.json')):
            with instance.activate(self.settings(freq), self.root):
                self.seed_minutes(self.rows(freq, name=name), freq)
                reads[freq] = {p: data.get_bars(CODE, p, adjust='raw')['bars'] for p in ('m30', 'm60')}
        for period in ('m30', 'm60'):
            native = recording(f'{period}-600036-20260929.json')['body']
            for freq in ('m5', 'm15'):
                with self.subTest(fact=freq, period=period):
                    self.assert_recorded_bars(reads[freq][period], native)
            with self.subTest(period=period, compare='m5-vs-m15'):
                for a, b in zip(reads['m5'][period], reads['m15'][period], strict=True):
                    self.assertEqual(a['dt'], b['dt'])
                    for field in ('open', 'high', 'low', 'close', 'volume'):
                        self.assertAlmostEqual(a[field], b[field], delta=0.01)

    def test_author_override_preserves_native_m15_baseline_bars(self):
        # 基线是现有直接库调用的 m15 绑定；新覆盖应只使配置显式化。
        rows = self.rows('m15')
        self.seed_day(rows)
        self.seed_minutes(rows, 'm15')
        expected = {freq: data.get_bars(CODE, freq, adjust='raw')['bars']
                    for freq in ('m30', 'm60', 'day', 'week')}
        self.assert_recorded_bars(expected['m30'], recording('m30-600036-20260929.json')['body'])
        with instance.activate(self.settings(), self.root):
            for freq, bars in expected.items():
                self.assertTrue(bars)
                self.assertEqual(data.get_bars(CODE, freq, adjust='raw')['bars'], bars)
            self.assertEqual(self.worker.status([CODE], now=NOW)['mode'], 'real')
            self.assertEqual((instance.current().per_minute, instance.current().per_day), (300, 10000))
