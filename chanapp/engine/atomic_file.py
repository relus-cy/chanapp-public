"""原子写文件：同目录临时文件写完并落盘后一步生效，中途失败目标保持原样、不留临时文件。

临时文件名为 `.<目标名>.<随机>.tmp`（mkstemp 创建，权限 0600）；进程被杀时可能残留，
按这个名字识别（见 `kline/seed_demo.py:_temporary`）。序列化、锁和建目录由调用方负责。
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


@contextmanager
def _staged(path: Path, data: bytes):
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def replace(path, data: bytes, *, sync_dir: bool = False) -> bool:
    """整份替换 `path`。sync_dir 时替换后再落盘父目录；返回 False 表示替换已生效但目录落盘未确认（不抛）。"""
    path = Path(path)
    with _staged(path, data) as temporary:
        os.replace(temporary, path)
    if not sync_dir:
        return True
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


def publish_if_absent(path, data: bytes) -> bool:
    """目标不存在才发布（硬链接，已存在即失败，并发也不覆盖）；返回是否由本次创建。"""
    path = Path(path)
    with _staged(path, data) as temporary:
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
    return True
