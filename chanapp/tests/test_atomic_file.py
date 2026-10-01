"""原子写文件：成功一步生效，失败目标保持原样且不留临时文件。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from chanapp.engine import atomic_file


class AtomicFileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.target = self.dir / 'state.json'

    def listing(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_replace_overwrites_with_given_bytes(self):
        self.target.write_bytes(b'old\n')
        atomic_file.replace(self.target, '{"名": 1}\n'.encode('utf-8'), sync_dir=True)
        self.assertEqual(self.target.read_bytes(), b'{"\xe5\x90\x8d": 1}\n')
        self.assertEqual(self.listing(), ['state.json'])

    def test_replace_failure_keeps_old_content_and_no_temporary(self):
        # 写入落盘失败与替换失败两个阶段
        for boundary in ('fsync', 'replace'):
            with self.subTest(boundary=boundary):
                self.target.write_bytes(b'old\n')
                with mock.patch.object(os, boundary, side_effect=OSError('disk full')):
                    with self.assertRaisesRegex(OSError, 'disk full'):
                        atomic_file.replace(self.target, b'new\n', sync_dir=True)
                self.assertEqual(self.target.read_bytes(), b'old\n')
                self.assertEqual(self.listing(), ['state.json'])

    def test_publish_if_absent_creates_private_file_once(self):
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        self.assertTrue(atomic_file.publish_if_absent(self.target, b'first\n'))
        self.assertEqual(self.target.read_bytes(), b'first\n')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(atomic_file.publish_if_absent(self.target, b'second\n'))
        self.assertEqual(self.target.read_bytes(), b'first\n')
        self.assertEqual(self.listing(), ['state.json'])


if __name__ == '__main__':
    unittest.main()
