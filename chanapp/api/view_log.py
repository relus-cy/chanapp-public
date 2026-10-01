"""搜索查看记录：用户实际打开图表的事件日志与按代码去重的最近查看列表（SQLite 单文件，跨服务与浏览器重启保留）。

只记用户的显式打开（页面在选择代码时调用 POST /api/views）；自动刷新与状态探测不记。已下线的 5 分、15 分周期
写入与回读都归到 30 分（旧事件行保留原值）。这里是用户查看记录，
不是取数日志；关注状态（是否自选）以自选列表为唯一事实，本模块不存。

文件位置由 engine/instance_paths.py 解析：VIEW_LOG_PATH；否则与 WATCHLIST_PATH 同目录同名加 .views.sqlite 后缀
（用户状态放一起，随部署保留）；否则在实例目录；都未设置时放在缓存根下的 views.sqlite。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS view_events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT NOT NULL,
  freq TEXT NOT NULL, adjust TEXT NOT NULL, viewed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recent_views(
  code TEXT PRIMARY KEY, name TEXT NOT NULL, freq TEXT NOT NULL, adjust TEXT NOT NULL,
  first_viewed_at TEXT NOT NULL, last_viewed_at TEXT NOT NULL, views INTEGER NOT NULL,
  last_event INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS recent_views_last ON recent_views(last_event);
"""


RETIRED_FREQS = ("m5", "m15")      # 页面已下线的周期（目标 2026-09-29 第三阶段）：记录与回读一律归到 m30


def normalize_freq(freq: str) -> str:
    return "m30" if freq in RETIRED_FREQS else freq


class ViewLog:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:                                   # 新建的查看记录只给运行身份读写；已有文件不改权限
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        """一次操作一条连接：成功提交、异常回滚，之后关闭（请求线程之间不共享连接）。"""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def record(self, code, name, freq, adjust, *, at=None) -> None:
        """一次打开：追加事件并更新最近列表（同一事务）。名称等于代码（链接直接打开时页面只知道代码）时保留已记的名称。"""
        now = (at or datetime.now()).isoformat(timespec="seconds")
        freq = normalize_freq(freq)
        with self._lock, self._connect() as conn:
            event = conn.execute("INSERT INTO view_events(code, name, freq, adjust, viewed_at) VALUES (?,?,?,?,?)",
                                 (code, name, freq, adjust, now)).lastrowid
            conn.execute(
                "INSERT INTO recent_views(code, name, freq, adjust, first_viewed_at, last_viewed_at, views, last_event)"
                " VALUES (?,?,?,?,?,?,1,?) ON CONFLICT(code) DO UPDATE SET name=CASE WHEN excluded.name=excluded.code"
                " THEN recent_views.name ELSE excluded.name END, freq=excluded.freq,"
                " adjust=excluded.adjust, last_viewed_at=excluded.last_viewed_at, views=recent_views.views+1,"
                " last_event=excluded.last_event",
                (code, name, freq, adjust, now, now, event))

    def recent(self, limit=50) -> list:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM recent_views ORDER BY last_event DESC LIMIT ?", (limit,)).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "last_event"}, "freq": normalize_freq(r["freq"])} for r in rows]

    def events(self, code=None) -> list:
        with self._connect() as conn:
            sql, args = "SELECT * FROM view_events", ()
            if code:
                sql, args = sql + " WHERE code=?", (code,)
            return [dict(r) for r in conn.execute(sql + " ORDER BY event_id", args)]
