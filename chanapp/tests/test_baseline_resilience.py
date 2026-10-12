"""Offline baseline fallback and cache identity regressions."""
import json
import time
import unittest
from pathlib import Path
from unittest import mock
from chanapp.engine import display_feed as feed
from chanapp.tests import cache_support

BASIC = dict.fromkeys(('amount', 'outer', 'inner', 'volume_ratio',
                       'limit_up_price', 'limit_down_price', 'total_mv', 'float_mv',
                       'pe_ttm', 'pb', 'turnover', 'amplitude'), 1)


def f10_result(price, source_ts=None):
    return {'f10': {'price': price, **BASIC}, 'flow': {'main': 1.0},
            'industry_pct': 0.5, 'source': 'baseline_display',
            'source_ts': time.time() if source_ts is None else source_ts}


class BaselineResilienceTests(unittest.TestCase):
    def setUp(self):
        root = cache_support.temp_dir(self)
        cache_support.isolate_cache_dir(self, root)
        self.dir = Path(root) / 'display'
        self.dir.mkdir()
        feed._reset_blocked()

    def test_failure_backup_and_primary_recovery(self):
        with mock.patch.object(feed, '_fetch_f10', side_effect=OSError('offline')), \
             mock.patch.object(feed, '_backup_f10', return_value=f10_result(10)):
            first = feed.get_f10('sz000001')
        self.assertEqual(first['meta']['field_sources']['price'], 'baseline_backup')
        self.assertEqual(feed._primary_backoff.failure_count(), 1)
        (self.dir / 'f10_sz000001.json').unlink()
        with mock.patch.object(feed, '_fetch_f10', return_value=f10_result(11)), \
             mock.patch.object(feed, '_backup_f10') as backup:
            result = feed.get_f10('sz000001')
        backup.assert_not_called()
        self.assertEqual(result['f10']['price'], 11)
        self.assertEqual(feed._primary_backoff.failure_count(), 0)

    def test_old_source_timestamp_is_not_fetch_timestamp(self):
        with mock.patch.object(feed, '_fetch_f10', side_effect=OSError()), \
             mock.patch.object(feed, '_backup_f10', return_value=f10_result(10, source_ts=100)):
            result = feed.get_f10('sz000001')
        self.assertTrue(result['meta']['source_stale'])
        self.assertEqual(result['source_ts'], 100)
        self.assertFalse(result['degraded'])

    def test_require_fresh_refreshes_expired_cache(self):
        (self.dir / 'f10_sz000001.json').write_text(json.dumps(
            {'ts': 1, 'data': f10_result(1)}))
        with mock.patch.object(feed, '_fetch_f10', return_value=f10_result(12)):
            result = feed.get_f10('sz000001', require_fresh=True)
        self.assertEqual(result['f10']['price'], 12)
        self.assertFalse(result['degraded'])

    def test_primary_backoff_does_not_block_backup(self):
        with mock.patch.object(feed, '_fetch_f10', side_effect=OSError()) as primary, \
             mock.patch.object(feed, '_backup_f10', side_effect=lambda code: f10_result(10)) as backup:
            for code in ('sz000001', 'sz000002', 'sz000003'):
                feed.get_f10(code)
        self.assertEqual(primary.call_count, 2)
        self.assertEqual(backup.call_count, 3)
        self.assertEqual(feed._primary_backoff.failure_count(), 2)
        self.assertEqual(feed._backup_backoff.failure_count(), 0)

    def test_backup_backoff_does_not_block_primary_recovery(self):
        with mock.patch.object(feed, '_fetch_f10', side_effect=OSError()), \
             mock.patch.object(feed, '_backup_f10', side_effect=OSError()):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    feed.get_f10('sz000001')
        self.assertEqual(feed._backup_backoff.failure_count(), 2)
        feed._primary_backoff._cool_until = 0.0
        with mock.patch.object(feed, '_fetch_f10', return_value=f10_result(12)), \
             mock.patch.object(feed, '_backup_f10') as backup:
            result = feed.get_f10('sz000001')
        backup.assert_not_called()
        self.assertEqual(result['f10']['price'], 12)
        self.assertEqual(feed._primary_backoff.failure_count(), 0)
        self.assertEqual(feed._backup_backoff.failure_count(), 2)

    def test_f10_supplements_basic_fields_without_old_flow(self):
        primary = {'f10': {'price': 10, 'total_mv': None}, 'flow': {}, 'source_ts': time.time()}
        backup = {'f10': {'price': 12, 'total_mv': 500}, 'flow': {'main': 99}, 'source_ts': 100}
        with mock.patch.object(feed, '_fetch_f10', return_value=primary), mock.patch.object(feed, '_backup_f10', return_value=backup):
            result = feed.get_f10('sz000001')
        self.assertEqual(result['f10']['price'], 10)
        self.assertEqual(result['f10']['total_mv'], 500)
        self.assertIsNone(result['flow']['main'])
        self.assertIn('flow', result['meta']['unavailable'])
        self.assertTrue(result['meta']['flow_note'])
        self.assertTrue(result['meta']['source_stale'])
        self.assertEqual(result['meta']['field_sources']['total_mv'], 'baseline_backup')

    def test_f10_failure_uses_backup_without_old_cache_mix(self):
        (self.dir / 'f10_sz000001.json').write_text(json.dumps({'ts': 1, 'data': {'f10': {'price': 1, 'industry': 'old'}, 'flow': {'main': 2}}}))
        with mock.patch.object(feed, '_fetch_f10', side_effect=OSError()), mock.patch.object(feed, '_backup_f10', return_value={'f10': {'price': 12}, 'source_ts': 100}):
            result = feed.get_f10('sz000001', require_fresh=True)
        self.assertEqual(result['f10']['price'], 12)
        self.assertEqual(result['f10']['industry'], '')
        self.assertIsNone(result['flow']['main'])
        self.assertEqual(feed._primary_backoff.failure_count(), 1)
        self.assertFalse(result['degraded'])

    def test_unknown_source_time_does_not_trigger_backup(self):
        result = f10_result(10)
        result.pop('source_ts')
        with mock.patch.object(feed, '_fetch_f10', return_value=result), \
             mock.patch.object(feed, '_backup_f10') as backup:
            out = feed.get_f10('sz000001')
        backup.assert_not_called()
        self.assertTrue(out['meta']['source_time_unknown'])
        self.assertFalse(out['degraded'])

    def test_invalid_primary_rows_count_as_failure(self):
        unusable = {'f10': {'price': -1}, 'flow': {}, 'industry_pct': None}
        with mock.patch.object(feed, '_fetch_f10', return_value=unusable), \
             mock.patch.object(feed, '_backup_f10', return_value=f10_result(10)):
            feed.get_f10('sz000001')
        self.assertEqual(feed._primary_backoff.failure_count(), 1)

    def test_f10_negative_basic_values_are_supplemented(self):
        from chanapp.engine.feeds import baseline_resilience as resilience
        result = resilience.merge_f10({'f10': {'price': 10, 'amount': -1, 'total_mv': -2, 'pe_ttm': -3}}, {'f10': {'amount': 100, 'total_mv': 200}})
        self.assertEqual(result['f10']['amount'], 100)
        self.assertEqual(result['f10']['total_mv'], 200)
        self.assertEqual(result['f10']['pe_ttm'], -3)

    def test_source_future_unknown_and_cached_f10_age_recomputed(self):
        from chanapp.engine.feeds import baseline_resilience as resilience
        self.assertTrue(resilience.stamp({'source_ts': time.time() + 3600}, 'baseline_display')['source_time_unknown'])
        result = resilience.merge_f10({'f10': {'price': 10}, 'source_ts': time.time()}, {})
        result['meta']['source_times']['baseline_display'] = 100
        self.assertTrue(resilience.refresh_f10_metadata(result)['meta']['source_stale'])

    def test_sector_failure_keeps_primary_f10(self):
        with mock.patch.object(feed, '_fetch_json', side_effect=[{'data': {'f43': 10, 'f198': 'BK001', 'f137': 5}}, OSError('sector offline')]):
            result = feed._fetch_f10('sz000001')
        self.assertEqual(result['f10']['price'], 10)
        self.assertEqual(result['flow']['main'], 5)
        self.assertIsNone(result['industry_pct'])

    def test_legacy_f10_cache_marks_unknown_source_time(self):
        from chanapp.engine.feeds import baseline_resilience as resilience
        result = resilience.refresh_f10_metadata({'f10': {'price': 10}})
        self.assertTrue(result['meta']['source_time_unknown'])
        self.assertIn('flow', result['meta']['unavailable'])
