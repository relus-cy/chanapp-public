"""实例状态路径：一个实例目录派生全部运行数据路径，已有环境变量逐项优先。

每条路径按下列顺序取第一个可用值（空字符串视为未设）：

| 路径 | 显式变量 | 随 WATCHLIST_PATH | 实例目录 | 两者都未设 |
| --- | --- | --- | --- | --- |
| 缓存根（事实库含计算审计、显示缓存、日历导出） | CHANAPP_CACHE_DIR | — | `<实例>/data` | 门面现值（缺省 `<包根>/.cache`） |
| AI 分析缓存 | ANALYSIS_CACHE_DIR | — | `<实例>/data/analysis` | `<包根>/.cache/analysis` |
| 自选 | WATCHLIST_PATH | — | `<实例>/watchlist.json` | `<缓存根>/watchlist.json` |
| 查看记录 | VIEW_LOG_PATH | 同目录 `<名>.views.sqlite` | `<实例>/views.sqlite` | `<缓存根>/views.sqlite` |
| 周期偏好 | — | 同目录 `<名>.periods.json` | `<实例>/periods.json` | `<缓存根>/periods.json` |

仓库自带的 `watchlist.json` 只是种子：读取时个人文件不存在才回退到它，写入一律落在个人路径。
只有配置了实例目录才在启动时初始化（建目录、种子只读复制、已有文件一律不动）；未配置时路径与
初始化方式保持原样，不搬迁已有状态。

一个进程只服务一个实例：生命周期退出恢复先前路径，但退出时不收束显示层后台刷新；未设实例目录时缓存根
取门面现值（导入时读 CHANAPP_CACHE_DIR，测试直接改门面），进程内事后改这个变量不生效。
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_SEED = PKG_ROOT / "watchlist.json"


@dataclass(frozen=True)
class InstancePaths:
    instance_dir: Path | None
    cache_dir: Path
    analysis_dir: Path
    watchlist: Path
    view_log: Path
    period_prefs: Path


def resolve(instance_dir=None, *, cache_dir=None) -> InstancePaths:
    """cache_dir 为未设实例目录时的缓存根（门面已按 CHANAPP_CACHE_DIR 解析的现值）。"""

    def explicit(name):
        value = os.environ.get(name)
        return Path(value) if value else None

    watchlist_env = explicit("WATCHLIST_PATH")
    if instance_dir is None:
        cache = Path(cache_dir) if cache_dir is not None else explicit("CHANAPP_CACHE_DIR") or PKG_ROOT / ".cache"
        analysis, state_dir = PKG_ROOT / ".cache" / "analysis", cache
    else:
        root = Path(instance_dir)
        cache = explicit("CHANAPP_CACHE_DIR") or root / "data"
        analysis, state_dir = root / "data" / "analysis", root
    return InstancePaths(
        instance_dir=None if instance_dir is None else Path(instance_dir),
        cache_dir=cache,
        analysis_dir=explicit("ANALYSIS_CACHE_DIR") or analysis,
        watchlist=watchlist_env or state_dir / "watchlist.json",
        view_log=explicit("VIEW_LOG_PATH") or _beside(watchlist_env, ".views.sqlite") or state_dir / "views.sqlite",
        period_prefs=_beside(watchlist_env, ".periods.json") or state_dir / "periods.json",
    )


def _beside(watchlist: Path | None, suffix: str) -> Path | None:
    """个人状态与显式指定的自选文件放在一起（随部署保留）。"""
    return watchlist.with_name(watchlist.stem + suffix) if watchlist is not None else None


def initialize(paths: InstancePaths) -> None:
    """建目录并把种子复制成个人自选；可重复执行，已有文件不覆盖。

    新建的实例根目录只给运行身份访问（0700）；已存在的目录（含显式指定的路径）不改权限。"""
    seed = WATCHLIST_SEED
    if paths.instance_dir is not None:
        paths.instance_dir.parent.mkdir(parents=True, exist_ok=True)
        paths.instance_dir.mkdir(mode=0o700, exist_ok=True)
    for directory in {paths.cache_dir, paths.analysis_dir, paths.watchlist.parent,
                      paths.view_log.parent, paths.period_prefs.parent}:
        directory.mkdir(parents=True, exist_ok=True)
    if paths.watchlist.exists() or not seed.is_file():
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{paths.watchlist.name}.", suffix=".tmp",
                                     dir=paths.watchlist.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(seed.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, paths.watchlist)       # 目标已存在即失败：并发初始化也不覆盖
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)


_active_instance_dir: Path | None = None
_new_instance = False


def is_new_instance() -> bool:
    """初始化个人文件之前是否不存在任何实例状态。"""
    return _new_instance


def current(cache_dir=None) -> InstancePaths:
    """请求时解析：实例目录取自当前生命周期，环境变量每次重读（测试可逐例注入）。"""
    return resolve(_active_instance_dir, cache_dir=cache_dir)


@contextmanager
def activate(instance_dir, *, cache_dir=None):
    """一次应用生命周期：配置了实例目录才初始化；退出恢复先前状态。"""
    global _active_instance_dir, _new_instance
    paths = resolve(instance_dir, cache_dir=cache_dir)
    # 必须在种子复制和事实库建表之前判别，否则首次打开会被误当升级。
    existing = (paths.watchlist.exists() or paths.view_log.exists() or paths.period_prefs.exists()
                or (paths.cache_dir.is_dir() and any(paths.cache_dir.iterdir())))
    if instance_dir is not None:
        try:
            initialize(paths)
        except OSError as exc:
            raise ValueError(f"instance_dir 无法初始化：{exc}") from None
    previous, _active_instance_dir = _active_instance_dir, paths.instance_dir
    previous_new, _new_instance = _new_instance, not existing
    try:
        yield paths
    finally:
        _active_instance_dir = previous
        _new_instance = previous_new
