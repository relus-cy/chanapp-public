"""Display-cache publication failures: bad timestamps, abandoned tasks, damaged files."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from chanapp.engine import cache_store


class CacheRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_older_display_write_is_superseded_and_future_timestamp_rejected(self):
        path = self.root / 'display' / 'quotes.json'
        now = time.time()
        self.assertEqual(cache_store.publish_json(path, {'data': {'v': 2}, 'ts': now}), 'published')
        result = cache_store.publish_json(path, {'data': {'v': 1}, 'ts': now - 10})
        self.assertEqual(result, 'superseded')
        self.assertEqual(json.loads(path.read_text())['data'], {'v': 2})
        with self.assertRaises(ValueError):
            cache_store.publish_json(path, {'data': {}, 'ts': time.time() + 120})

    def test_damaged_display_cache_is_quarantined_and_rebuilt(self):
        damaged = {'json': '{broken', 'type': '[]',
                   'ts': json.dumps({'data': {}, 'ts': 'yesterday'})}
        for name, content in damaged.items():
            with self.subTest(damage=name):
                path = self.root / name / 'quotes.json'
                path.parent.mkdir(parents=True)
                path.write_text(content)
                result = cache_store.publish_json(path, {'data': {'v': 1}, 'ts': time.time()})
                self.assertEqual(result, 'published')
                self.assertEqual(json.loads(path.read_text())['data'], {'v': 1})
                self.assertEqual(len(list(path.parent.glob('corrupt-*.json'))), 1)

    def test_quarantine_keeps_at_most_two_diagnostics(self):
        path = self.root / 'display' / 'quotes.json'
        path.parent.mkdir(parents=True)
        for _ in range(4):
            path.write_text('{broken')
            cache_store.publish_json(path, {'data': {}, 'ts': time.time()})
        self.assertLessEqual(len(list(path.parent.glob('corrupt-*.json'))), 2)


if __name__ == '__main__':
    unittest.main()
