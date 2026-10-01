"""校验随包样本，经正式 ingest 初始化独立 demo 实例；不下载、不启动采集。

从包的父目录运行：python -m chanapp.engine.kline.seed_demo INSTANCE_DIRECTORY
再用 CHANAPP_INSTANCE_CONFIG=INSTANCE_DIRECTORY/instance.json 启动应用。
同一输入可重跑，已有自选和周期偏好保留；应用仍只读事实，审计另库。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

from chanapp.engine import atomic_file
from chanapp.engine.kline import collector, ingest, instance
from chanapp.engine.kline.rows import CalendarRow, InstrumentRow

SAMPLE_DIR = Path(__file__).resolve().parents[2] / 'samples' / 'demo'
_FILES = ('bars.csv', 'calendar.json', 'instruments.json', 'instance.json', 'watchlist.json')
_PATH_OVERRIDES = ('CHANAPP_INSTANCE_CONFIG', 'CHANAPP_CACHE_DIR', 'WATCHLIST_PATH',
                   'VIEW_LOG_PATH', 'ANALYSIS_CACHE_DIR')


def initialize(directory, *, sample_dir=SAMPLE_DIR):
    """只接受独立目录；环境路径覆盖须先清除，避免初始化写入其他实例。"""
    conflicts = [key for key in _PATH_OVERRIDES if os.environ.get(key)]
    if conflicts:
        raise ValueError('初始化前请清除路径覆盖变量: ' + ', '.join(conflicts))
    sample_dir, directory = Path(sample_dir), Path(directory).expanduser().resolve()
    manifest = json.loads((sample_dir / 'manifest.json').read_text())
    if manifest.get('schema_version') != 1 or set(manifest.get('files', {})) != set(_FILES):
        raise ValueError('样本 manifest 格式校验失败')
    for name in _FILES:
        content = (sample_dir / name).read_bytes()
        expected = manifest['files'][name]
        if len(content) != expected['bytes'] or hashlib.sha256(content).hexdigest() != expected['sha256']:
            raise ValueError(f'样本校验失败: {name}')
    calendar_rows = [CalendarRow(**r) for r in json.loads((sample_dir / 'calendar.json').read_text())]
    instruments = [InstrumentRow(**r) for r in json.loads((sample_dir / 'instruments.json').read_text())]
    template = (sample_dir / 'instance.json').read_bytes()
    config_path = directory / 'instance.json'
    if directory.exists() and any(p.is_symlink() for p in directory.rglob('*')):
        # 目录本身可以是链接（已按解析后的根处理），内部链接会把写入引到别的实例
        raise ValueError('目标目录内有符号链接；请使用独立目录')
    if config_path.exists():
        if config_path.read_bytes() != template:
            raise ValueError('目标实例配置与 demo 样本不一致；请使用独立目录')
    elif directory.exists() and any(not _temporary(p) for p in directory.iterdir()):
        raise ValueError('目标目录已有文件且没有 demo 配置；请使用空目录')
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    # 配置先落：它是「这是 demo 目录」的标记；两份都整份写完才出现，中断后可重跑
    for name in ('instance.json', 'watchlist.json'):
        _publish(directory / name, (sample_dir / name).read_bytes())
    if config_path.read_bytes() != template:
        raise ValueError('目标实例配置已改变')
    settings = instance.load_instance(config_path, environ={})
    # 只初始化绑定和偏好；不创建在线 provider，不走采集/首取/追赶路径。
    from chanapp.engine import instance_paths
    with instance_paths.activate(settings.instance_dir, cache_dir=directory / 'data') as paths:
        with instance.activate(settings, paths.cache_dir):
            worker = collector.Collector(paths.cache_dir, providers={})
            try:
                report = ingest.import_file(sample_dir / 'bars.csv', worker)
                worker.commit_import_metadata(calendar_rows, instruments)
            finally:
                worker.conn().close()
    return {'status': 'ok', 'sample_id': manifest['sample_id'], **report}


def _temporary(path):
    return path.name.startswith(('.instance.json.', '.watchlist.json.')) and path.name.endswith('.tmp')


def _publish(target, content):
    """0600 临时文件写完并落盘后硬链接到目标；目标已存在则保留原文件。"""
    atomic_file.publish_if_absent(target, content)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print('用法: python -m chanapp.engine.kline.seed_demo INSTANCE_DIRECTORY', file=sys.stderr)
        return 2
    try:
        result = initialize(args[0])
    except (ValueError, OSError, collector.CollectorLocked) as exc:
        print(json.dumps({'status':'error', 'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
