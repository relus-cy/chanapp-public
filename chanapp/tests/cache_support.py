"""测试共享临时环境辅助（非测试文件，unittest discover 不收集）。

只负责临时目录与环境隔离，不封装业务 mock；各测试自行持有业务替身。
公开与私有树都隔离同一个核心的 CACHE_DIR。
"""
import os
import tempfile
from pathlib import Path
from unittest import mock


def temp_dir(case):
    """建临时目录并随 case 清理；返回目录路径字符串。"""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    return tmp.name


def set_env(case, name, value):
    """置环境变量并恢复：原不存在则卸载，原存在则还原旧值。"""
    if name in os.environ:
        case.addCleanup(os.environ.__setitem__, name, os.environ[name])
    else:
        case.addCleanup(os.environ.pop, name, None)
    os.environ[name] = value


def isolate_cache_dir(case, path=None):
    """把 engine.data.CACHE_DIR 钉到临时目录，防事实读写与结论记录落到 .cache/ 真实库。

    path 缺省时自建临时目录并随 case 清理；传入已有临时目录则复用。
    """
    from chanapp.engine import data as engine_data
    if path is None:
        path = temp_dir(case)
    patch = mock.patch.object(engine_data, "CACHE_DIR", Path(path))
    patch.start()
    case.addCleanup(patch.stop)
