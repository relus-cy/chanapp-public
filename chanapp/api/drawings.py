"""用户画线：按标的存逐条画线（SQLite 单文件，跨设备经同一服务读写）。

每条画线一行：code、id（前端生成）、body（画线内容 JSON，带格式版本 v）、revision（服务端递增）、
created_at、updated_at、deleted_at。写入是条件写：

- 新建要求 base_revision 为空，id 已存在（含已删除）即冲突；
- 修改要求 base_revision 等于当前 revision，否则冲突；已删除的画线不能再改；
- 删除是软删除（留墓碑），同样要求 base_revision 相符；已删除的再删视为成功；
- 清空一个标的把它所有未删除画线一次标为删除，不检查版本（用户明确的整体操作）。

冲突时抛 Conflict，带服务端当前版本（不存在为 None），由接口层回 409。body 的语义归前端，
这里只校验它是对象、v 为支持的版本、序列化后不超过上限；例外是提醒线（可选的 alert），服务端要读它换算价位
（见 alert_lines），所以 alert 只接受在水平线上，方向、复权模式与线价要合法。

文件位置由 engine/instance_paths.py 解析：DRAWINGS_PATH；否则与 WATCHLIST_PATH 同目录同名加 .drawings.sqlite
后缀；否则在实例目录；都未设置时放在缓存根下的 drawings.sqlite。新建文件权限 0600。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline.rows import kind_of

BODY_VERSIONS = (1,)
ALERT_KIND = "horizontal-line"
ALERT_DIRECTIONS = ("below", "above")
MAX_BODY_BYTES = 16 * 1024
MAX_PER_CODE = 500        # 单个标的的未删除画线上限，防止失控的前端灌满

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drawings(
  code TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
  PRIMARY KEY(code, id));
"""


class Conflict(Exception):
    def __init__(self, current: dict | None):
        super().__init__("画线已被修改")
        self.current = current


class NotFound(Exception):
    pass


class LimitExceeded(Exception):
    pass


def validate_body(body) -> str:
    """返回规范化的 JSON 文本；不合格抛 ValueError。"""
    if not isinstance(body, dict):
        raise ValueError("body 必须是对象")
    v = body.get("v")
    if type(v) is not int or v not in BODY_VERSIONS:     # 排除 true、1.0 这类与 1 相等的值
        raise ValueError(f"body.v 只支持 {list(BODY_VERSIONS)}")
    if "alert" in body:
        _validate_alert(body)
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError(f"body 超过 {MAX_BODY_BYTES} 字节")
    return text


def _validate_alert(body) -> None:
    alert = body["alert"]
    if body.get("kind") != ALERT_KIND:
        raise ValueError(f"提醒线只支持 {ALERT_KIND}")
    if not isinstance(alert, dict) or alert.get("direction") not in ALERT_DIRECTIONS:
        raise ValueError(f"alert.direction 只支持 {list(ALERT_DIRECTIONS)}")
    if body.get("adjust") not in ("qfq", "raw") or _line_price(body) is None:
        raise ValueError("提醒线要有复权模式和线价")


def _line_price(body) -> float | None:
    anchors = body.get("anchors")
    price = anchors[0].get("price") if isinstance(anchors, list) and anchors and isinstance(anchors[0], dict) else None
    return float(price) if type(price) in (int, float) else None


def _ref_close(body) -> tuple:
    """(参照日, 参照收盘)；缺失或不合法为 (None, None)。"""
    ref = body.get("ref")
    if not isinstance(ref, dict) or not isinstance(ref.get("day"), str):
        return None, None
    close = ref.get("close")
    return (ref["day"], float(close)) if type(close) in (int, float) and close > 0 else (None, None)


def qfq_ref_days(lines) -> dict:
    """{code: 参照日集合}：前复权下画的提醒线换算要读的日子（指数恒为原始价，不读）。"""
    out: dict = {}
    for line in lines:
        day, _ = _ref_close(line["body"])
        if line["body"]["adjust"] == "qfq" and kind_of(line["code"]) != "index" and day:
            out.setdefault(line["code"], set()).add(day)
    return out


def alert_lines(lines, quotes, closes) -> list:
    """提醒线换算到当前原始价口径（按画线记下的参照收盘 ref）：
    - 不复权下画的线、指数（恒为原始价）原样给出；
    - 前复权下画的线乘「参照日当前的前复权收盘 ÷ 记录的参照收盘」：最新一天的前复权价等于原始价，所以换算后
      就是今天的原始价口径；港股前复权已核实为等比，同一换算。拿不到参照日的当前前复权收盘（不在覆盖区、
      缓存未建、没有参照）时 price 为 None，不编造。
    quotes：{code: 门面 quote() 或 None}；closes：{code: {day: 当前前复权收盘或 None}}。"""
    out = []
    for line in lines:
        code, body = line["code"], line["body"]
        drawn = _line_price(body)
        price = drawn
        if body["adjust"] == "qfq" and kind_of(code) != "index":
            day, ref_close = _ref_close(body)
            now_close = (closes.get(code) or {}).get(day) if day else None
            # 页面记下的参照收盘是图表上 4 位小数的价：当前收盘同样舍入，没有新除权时比例恰为 1
            price = drawn * round(now_close, 4) / ref_close if now_close else None
        q = quotes.get(code) or {}
        out.append({"code": code, "drawing_id": line["id"], "direction": body["alert"]["direction"],
                    "price": price, "drawn_adjust": body["adjust"], "drawn_price": drawn,
                    "latest": {"price": q.get("price"), "price_time": q.get("price_time"), "stale": q.get("stale")}})
    return out


def _row(r) -> dict:
    out = {"id": r["id"], "body": json.loads(r["body"]), "revision": r["revision"], "updated_at": r["updated_at"]}
    if r["deleted_at"]:
        out["deleted_at"] = r["deleted_at"]
    return out


class DrawingStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:                                   # 新建的画线库只给运行身份读写；已有文件不改权限
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

    @contextmanager
    def _write(self):
        """写操作：进程内锁加 BEGIN IMMEDIATE，读当前版本与改写在同一写事务里，多进程同时写也不会丢更新。"""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def _get(conn, code, id_):
        return conn.execute("SELECT * FROM drawings WHERE code=? AND id=?", (code, id_)).fetchone()

    def items(self, code) -> list:
        """未删除的画线，按创建先后（绘制叠放顺序）。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM drawings WHERE code=? AND deleted_at IS NULL"
                                " ORDER BY created_at, rowid", (code,)).fetchall()
        return [_row(r) for r in rows]

    def alert_items(self, code=None) -> list:
        """未删除、带 alert 的水平线 [{code, id, body}]，按标的、创建先后；code 为 None 时全部标的。"""
        sql = "SELECT * FROM drawings WHERE deleted_at IS NULL" + (" AND code=?" if code else "")
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY code, created_at, rowid", (code,) if code else ()).fetchall()
        out = []
        for r in rows:
            body = json.loads(r["body"])
            if "alert" not in body:
                continue
            try:
                _validate_alert(body)               # 校验之前存下的坏记录跳过，不拖垮其余提醒线
            except ValueError:
                continue
            out.append({"code": r["code"], "id": r["id"], "body": body})
        return out

    def put(self, code, id_, body, base_revision, *, at=None) -> dict:
        text = validate_body(body)
        now = (at or datetime.now()).isoformat(timespec="seconds")
        with self._write() as conn:
            current = self._get(conn, code, id_)
            if base_revision is None:
                if current is not None:
                    raise Conflict(_row(current))
                live = conn.execute("SELECT count(*) FROM drawings WHERE code=? AND deleted_at IS NULL",
                                    (code,)).fetchone()[0]
                if live >= MAX_PER_CODE:
                    raise LimitExceeded(f"单个标的最多 {MAX_PER_CODE} 条画线")
                conn.execute("INSERT INTO drawings(code, id, body, revision, created_at, updated_at)"
                             " VALUES (?,?,?,1,?,?)", (code, id_, text, now, now))
            else:
                if current is None or current["deleted_at"] or current["revision"] != base_revision:
                    raise Conflict(_row(current) if current is not None else None)
                conn.execute("UPDATE drawings SET body=?, revision=revision+1, updated_at=? WHERE code=? AND id=?",
                             (text, now, code, id_))
            return _row(self._get(conn, code, id_))

    def delete(self, code, id_, base_revision, *, at=None) -> dict:
        now = (at or datetime.now()).isoformat(timespec="seconds")
        with self._write() as conn:
            current = self._get(conn, code, id_)
            if current is None:
                raise NotFound(id_)
            if current["deleted_at"]:
                return _row(current)
            if current["revision"] != base_revision:
                raise Conflict(_row(current))
            conn.execute("UPDATE drawings SET revision=revision+1, updated_at=?, deleted_at=? WHERE code=? AND id=?",
                         (now, now, code, id_))
            return _row(self._get(conn, code, id_))

    def clear(self, code, *, at=None) -> int:
        now = (at or datetime.now()).isoformat(timespec="seconds")
        with self._write() as conn:
            return conn.execute("UPDATE drawings SET revision=revision+1, updated_at=?, deleted_at=?"
                                " WHERE code=? AND deleted_at IS NULL", (now, now, code)).rowcount
