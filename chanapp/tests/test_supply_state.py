import concurrent.futures
from dataclasses import FrozenInstanceError
import multiprocessing
import os
import stat
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chanapp.engine import supply
from chanapp.engine.data_identity import version


def _competing_switch(path, start, results):
    start.wait(5)
    try:
        result = supply.Manager(Path(path)).switch('primary_candidate', 0, lambda _: {})
        results.put(result['generation'])
    except supply.ConflictError:
        results.put('conflict')


class SupplyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'supply' / 'state.json'
        self.manager = supply.Manager(self.path)

    def test_default_read_does_not_create_state(self):
        state = self.manager.snapshot()
        self.assertEqual(state, supply.Snapshot('baseline', 0))
        self.assertFalse(self.path.parent.exists())
        with self.assertRaises(FrozenInstanceError):
            state.generation = 2

    def test_switch_restart_and_same_scheme_noop(self):
        observed = []
        result = self.manager.switch('primary_candidate', 0,
                                     lambda target: observed.append(target) or {'ok': True})
        self.assertEqual(result, {'scheme': 'primary_candidate', 'generation': 1,
                                  'prepared': {'ok': True}})
        self.assertEqual(observed, [supply.Snapshot('primary_candidate', 1)])
        other = supply.Manager(self.path)
        self.assertEqual(other.snapshot(), observed[0])
        with patch.object(other, '_persist', side_effect=AssertionError('must not persist')), \
             patch('builtins.print') as prepare:
            repeated = other.switch('primary_candidate', 1, prepare)
        prepare.assert_not_called()
        self.assertEqual(repeated['generation'], 1)
        with self.assertRaises(supply.ConflictError):
            other.switch('primary_candidate', 0, prepare)
        other.switch('baseline', 1, lambda _: {})
        self.assertEqual(self.manager.snapshot(), supply.Snapshot('baseline', 2))

    def test_failed_prepare_leaves_state(self):
        def fail(_):
            raise RuntimeError('not ready')
        with self.assertRaises(RuntimeError):
            self.manager.switch('primary_candidate', 0, fail)
        self.assertEqual(self.manager.snapshot(), supply.Snapshot('baseline', 0))
        self.assertFalse(self.path.exists())

    def test_failed_persistence_leaves_committed_state(self):
        self.manager.switch('primary_candidate', 0, lambda _: {})
        original = self.path.read_bytes()
        for operation in ('os.replace', 'os.fsync'):
            with self.subTest(operation=operation):
                with patch('chanapp.engine.supply.' + operation, side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError):
                        self.manager.switch('baseline', 1, lambda _: {})
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(self.manager.snapshot(), supply.Snapshot('primary_candidate', 1))

    def test_directory_sync_failure_returns_committed_state_warning(self):
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('directory sync failed')
            return real_sync(fd)
        with patch('chanapp.engine.supply.os.fsync', side_effect=sync):
            result = self.manager.switch('primary_candidate', 0, lambda _: {})
        self.assertEqual(result['durability_warning'], 'directory_sync_failed')
        self.assertEqual(result['generation'], 1)
        self.assertEqual(supply.Manager(self.path).snapshot(), supply.Snapshot('primary_candidate', 1))

    def test_success_syncs_parent_after_replace(self):
        real_sync = os.fsync
        observed = []
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                observed.append(supply.Manager(self.path).snapshot())
            return real_sync(fd)
        with patch('chanapp.engine.supply.os.fsync', side_effect=sync):
            self.manager.switch('primary_candidate', 0, lambda _: {})
        self.assertEqual(observed, [supply.Snapshot('primary_candidate', 1)])

    def test_module_follows_environment_path_changes(self):
        with patch.object(supply, '_default_manager', None):
            with patch.dict(os.environ, {'SUPPLY_STATE_PATH': str(self.path)}):
                supply.switch('primary_candidate', 0, lambda _: {})
                self.assertEqual(supply.current().generation, 1)
            with patch.dict(os.environ, {'SUPPLY_STATE_PATH': '',
                                        'CHANAPP_CACHE_DIR': self.temp.name + '/other'}):
                self.assertEqual(supply.current(), supply.Snapshot('baseline', 0))
                supply.switch('primary_candidate', 0, lambda _: {})
                self.assertTrue((Path(self.temp.name) / 'other/supply/state.json').exists())
            with patch.dict(os.environ, {'SUPPLY_STATE_PATH': str(self.path)}):
                self.assertEqual(supply.current(), supply.Snapshot('primary_candidate', 1))

    def test_reject_invalid_state_and_stale_generation(self):
        with self.assertRaises(ValueError):
            self.manager.switch('unknown', 0, lambda _: {})
        with self.assertRaises(supply.ConflictError):
            self.manager.switch('baseline', 1, lambda _: self.fail('must not prepare'))
        self.path.parent.mkdir(exist_ok=True)
        for content in ('broken', '{}', '{"scheme":"unknown","generation":0}',
                        '{"scheme":"baseline","generation":true}',
                        '{"scheme":"baseline","generation":-1}'):
            self.path.write_text(content)
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.manager.snapshot()
                with self.assertRaises(ValueError):
                    self.manager.switch('baseline', 0, lambda _: {})
                self.assertEqual(self.path.read_text(), content)

    def test_prepare_does_not_block_reads_and_cross_manager_cas(self):
        entered, release = threading.Event(), threading.Event()
        def prepare(_):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timeout')
            return {}
        other = supply.Manager(self.path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.manager.switch, 'primary_candidate', 0, prepare)
            try:
                self.assertTrue(entered.wait(1))
                read = pool.submit(self.manager.snapshot)
                self.assertEqual(read.result(timeout=1), supply.Snapshot('baseline', 0))
                second = pool.submit(other.switch, 'baseline', 0,
                                     lambda _: self.fail('stale prepare'))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2)['generation'], 1)
            with self.assertRaises(supply.ConflictError):
                second.result(timeout=2)

    def test_processes_share_one_compare_and_swap(self):
        context = multiprocessing.get_context('spawn')
        start, results = context.Event(), context.Queue()
        processes = [context.Process(target=_competing_switch,
                                     args=(str(self.path), start, results)) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            start.set()
            outcomes = [results.get(timeout=10) for _ in processes]
            self.assertCountEqual(outcomes, [1, 'conflict'])
            self.assertEqual(self.manager.snapshot(), supply.Snapshot('primary_candidate', 1))
        finally:
            for process in processes:
                process.join(timeout=3)
                if process.is_alive():
                    process.terminate()
                    process.join()
            results.close()

    def test_context_is_fixed_nested_and_thread_local(self):
        with patch.object(supply, '_default_manager', self.manager):
            old = supply.snapshot()
            with supply.use(old):
                supply.switch('primary_candidate', 0, lambda _: {})
                self.assertEqual(supply.current(), old)
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    self.assertEqual(pool.submit(supply.current).result(), supply.Snapshot('primary_candidate', 1))
                with supply.use(supply.snapshot()):
                    self.assertEqual(supply.current().generation, 1)
                self.assertEqual(supply.current(), old)
            self.assertEqual(supply.current().generation, 1)


class DataIdentityTests(unittest.TestCase):
    def test_canonical_keys_complete_content_and_identity(self):
        bars = [{'time': 1, 'close': 10}, {'time': 2, 'close': 20}]
        original = version(bars, {'scheme': 'baseline', 'normalization': 1})
        self.assertRegex(original, r'^[0-9a-f]{64}$')
        self.assertEqual(original, version([{'close': 10, 'time': 1}, {'close': 20, 'time': 2}],
                                            {'normalization': 1, 'scheme': 'baseline'}))
        self.assertNotEqual(original, version(bars, {'scheme': 'primary_candidate', 'normalization': 1}))
        bars[0]['close'] = 11
        self.assertNotEqual(original, version(bars, {'scheme': 'baseline', 'normalization': 1}))
        self.assertNotEqual(version(bars), version(list(reversed(bars))))
        self.assertNotEqual(version([]), version(bars))
