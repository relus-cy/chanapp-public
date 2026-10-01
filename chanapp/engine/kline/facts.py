"""K 线事实层（新 schema）：只存原始事实，修订追加，单写者。

事实提交、修订与读取 schema。
- 库文件 .cache/facts.sqlite，与旧 kline.sqlite 分开，不迁移旧库；
- 行主键含 revision，当前值取每键最大 revision（current_* 视图）；
- series_state.revision_gen 记所有变化，closed_gen 不记 forming 行的正常更新（视图令牌用它）；
- 隔离状态持久化，读取排除隔离键；撤销隔离不复活旧 revision；
- 写事务 BEGIN IMMEDIATE；写失败抛 FactsWriteError，调用方不得报告已发布。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chanapp.engine.kline import admission
from chanapp.engine.kline.rows import PROVENANCE_RANK, kind_of, market_of

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# 各市场改动前的分钟事实粒度（2026-09-30 A 股改 m15 之前）：没有粒度记录的旧设置键与旧 day_checks 行按它解释
LEGACY_MINUTE_FACT = {"CN": "m5", "HK": "m30"}
DB_NAME = "facts.sqlite"
WRITER_LOCK_NAME = "facts.writer.lock"

_DDL = """
CREATE TABLE day_bars(
  code TEXT NOT NULL, trade_date TEXT NOT NULL, revision INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, volume_unit TEXT NOT NULL,
  amount REAL, currency TEXT NOT NULL, pc REAL, sf INTEGER NOT NULL, provenance TEXT NOT NULL,
  source TEXT NOT NULL, binding_gen INTEGER NOT NULL, batch_id TEXT NOT NULL,
  rev_kind TEXT NOT NULL, recorded_at TEXT NOT NULL,
  PRIMARY KEY(code, trade_date, revision));
CREATE TABLE minute_bars(
  code TEXT NOT NULL, fact_freq TEXT NOT NULL, slot_end TEXT NOT NULL, revision INTEGER NOT NULL,
  trade_date TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL, volume_unit TEXT NOT NULL, amount REAL,
  state TEXT NOT NULL, trade_state TEXT NOT NULL,
  source TEXT NOT NULL, binding_gen INTEGER NOT NULL, batch_id TEXT NOT NULL,
  rev_kind TEXT NOT NULL, recorded_at TEXT NOT NULL,
  PRIMARY KEY(code, fact_freq, slot_end, revision));
CREATE INDEX minute_bars_day ON minute_bars(code, fact_freq, trade_date);
CREATE TABLE series_state(
  code TEXT NOT NULL, dataset TEXT NOT NULL,
  revision_gen INTEGER NOT NULL DEFAULT 0, closed_gen INTEGER NOT NULL DEFAULT 0,
  last_commit_at TEXT, PRIMARY KEY(code, dataset));
CREATE TABLE quarantine(
  code TEXT NOT NULL, dataset TEXT NOT NULL, key TEXT NOT NULL, reason TEXT NOT NULL,
  since TEXT NOT NULL, released_at TEXT, PRIMARY KEY(code, dataset, key));
CREATE TABLE pending_review(
  review_id INTEGER PRIMARY KEY, code TEXT NOT NULL, dataset TEXT NOT NULL, key TEXT NOT NULL,
  incoming TEXT NOT NULL, reason TEXT NOT NULL, batch_id TEXT NOT NULL, created_at TEXT NOT NULL,
  verdict TEXT, decided_at TEXT);
CREATE TABLE day_checks(
  code TEXT NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
  detail TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY(code, trade_date));
CREATE TABLE soft_flags(
  code TEXT NOT NULL, dataset TEXT NOT NULL, key TEXT NOT NULL, flag TEXT NOT NULL,
  detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE coverage_gaps(
  gap_id INTEGER PRIMARY KEY, code TEXT NOT NULL, dataset TEXT NOT NULL,
  start TEXT NOT NULL, end TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, resolved_at TEXT);
CREATE TABLE batches(
  batch_id TEXT PRIMARY KEY, code TEXT NOT NULL, item TEXT NOT NULL, source TEXT NOT NULL,
  binding_gen INTEGER NOT NULL, status TEXT NOT NULL, accepted INTEGER NOT NULL,
  rejected INTEGER NOT NULL, detail TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE calendar(
  market TEXT NOT NULL, date TEXT NOT NULL, is_open INTEGER NOT NULL, sessions TEXT NOT NULL,
  source TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(market, date));
CREATE TABLE instruments(
  code TEXT PRIMARY KEY, name TEXT NOT NULL, list_date TEXT, delist_date TEXT,
  kind TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE binding_state(
  market TEXT NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL, active TEXT NOT NULL,
  binding_gen INTEGER NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(market, kind, item));
CREATE TABLE probe_runs(
  run_id INTEGER PRIMARY KEY, probe_id TEXT NOT NULL, market TEXT NOT NULL, kind TEXT NOT NULL,
  item TEXT NOT NULL, source TEXT NOT NULL, verdict TEXT NOT NULL, scope TEXT NOT NULL,
  contract_hash TEXT NOT NULL, verified_at TEXT NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE quota_usage(
  source TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(source, day));
CREATE TABLE vendor_qfq_bars(
  code TEXT NOT NULL, freq TEXT NOT NULL, version INTEGER NOT NULL, dt TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL NOT NULL, PRIMARY KEY(code, freq, version, dt));
CREATE TABLE vendor_qfq_publish(
  code TEXT NOT NULL, freq TEXT NOT NULL, version INTEGER NOT NULL, fetched_at TEXT NOT NULL,
  frozen INTEGER NOT NULL DEFAULT 0, stale INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(code, freq));
CREATE TABLE run_identity(id INTEGER PRIMARY KEY CHECK(id = 1), run_id TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE calc_runs(
  run_id INTEGER PRIMARY KEY, code TEXT NOT NULL, freq TEXT NOT NULL,
  input_start TEXT NOT NULL, input_end TEXT NOT NULL,
  input_data_version TEXT NOT NULL, calculation_id TEXT NOT NULL,
  recorded_at TEXT NOT NULL, source_kind TEXT NOT NULL, reported_at TEXT, status TEXT NOT NULL,
  UNIQUE(code, freq, input_data_version, calculation_id));
CREATE TABLE calc_signals(
  run_id INTEGER NOT NULL REFERENCES calc_runs(run_id),
  dt TEXT NOT NULL, label TEXT NOT NULL, price REAL, side TEXT, level TEXT, types TEXT,
  forming INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL);
CREATE VIEW current_day_bars AS SELECT b.* FROM day_bars b JOIN (
  SELECT code, trade_date, MAX(revision) AS r FROM day_bars GROUP BY code, trade_date) m
  ON m.code = b.code AND m.trade_date = b.trade_date AND m.r = b.revision;
CREATE VIEW current_minute_bars AS SELECT b.* FROM minute_bars b JOIN (
  SELECT code, fact_freq, slot_end, MAX(revision) AS r FROM minute_bars
  GROUP BY code, fact_freq, slot_end) m
  ON m.code = b.code AND m.fact_freq = b.fact_freq AND m.slot_end = b.slot_end AND m.r = b.revision
"""
# 版本 1 之上的纯增量对象（CREATE ... IF NOT EXISTS，打开时补建，不升 user_version）：回退到不认识它们的旧代码时
# 旧代码照常打开库、忽略这些对象
_ADDITIVE_DDL = """
CREATE TABLE IF NOT EXISTS minute_day_checks(
  code TEXT NOT NULL, trade_date TEXT NOT NULL, fact_freq TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT NOT NULL, detail TEXT NOT NULL, checked_at TEXT NOT NULL,
  PRIMARY KEY(code, trade_date, fact_freq))
"""
_REQUIRED = ("day_bars", "minute_bars", "current_day_bars", "current_minute_bars",
             "series_state", "quarantine", "pending_review", "day_checks", "minute_day_checks", "soft_flags",
             "coverage_gaps", "batches", "calendar", "instruments", "binding_state",
             "probe_runs", "settings", "quota_usage", "vendor_qfq_bars", "vendor_qfq_publish",
             "run_identity", "calc_runs", "calc_signals")


class SchemaError(RuntimeError):
    """事实库不可用：未知版本或结构缺失。"""


class FactsWriteError(RuntimeError):
    """写事务失败；该批次未发布，读侧继续返回之前的可信快照。"""


class StaleBinding(Exception):
    """批次携带的 binding_gen 已过期，整批拒写。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def write_txn(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_txn(conn):
    """一次短读事务：视图的全部依赖出自同一快照；已在事务中则复用。"""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


def _migrate(conn) -> None:
    # 完整库只读检查即返回：采集器写事务期间，请求线程首读不排队等写锁。
    # 需要建表或补表时，版本检查与建表在同一个写事务里复核，避免并发首次计算各自看到空审计库；
    # SQLite 自身串行化初始化，也覆盖多连接/多进程；不使用采集器写者锁。
    if _schema_state(conn) == "ready":
        return
    with write_txn(conn):
        state = _schema_state(conn)
        if state == "empty":
            for statement in _DDL.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO run_identity(id, run_id, created_at) VALUES (1, ?, ?)",
                         (uuid.uuid4().hex, now_iso()))
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            state = _schema_state(conn)
        if state == "additive":
            conn.execute(_ADDITIVE_DDL)


def _schema_state(conn) -> str:
    """empty（未建库）/ additive（只缺纯增量表）/ ready；版本过高或半成品库直接拒绝。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaError(f"事实库版本 {version} 高于代码支持的 {SCHEMA_VERSION}，请升级代码")
    if version == 0:
        return "empty"
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    missing = [name for name in _REQUIRED if name not in present and name != "minute_day_checks"]
    if missing:
        raise SchemaError(f"事实库缺少对象 {missing}，拒绝按半成品库运行")
    return "ready" if "minute_day_checks" in present else "additive"


def _chmod_owner_only(path: Path) -> None:
    os.chmod(path, 0o600)
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            os.chmod(side, 0o600)


def open_facts(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None,
                           check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        # journal_mode 的读锁升级遇并发初始化会直接返回 BUSY，SQLite busy_timeout
        # 不总会等待。只对此竞争做有界重试，其余错误仍原样上抛。
        deadline = time.monotonic() + 5.0
        while True:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate(conn)
        _chmod_owner_only(path)
    except Exception:
        conn.close()
        raise
    return conn


def open_readonly(path) -> sqlite3.Connection:
    """demo 读样本不迁移、不 chmod、不申请写者锁；未导入样本时仅用内存空库。"""
    path = Path(path).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro" if path.exists() else ":memory:",
                           uri=True, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        if not path.exists():
            _migrate(conn)
        conn.execute("PRAGMA query_only=ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        if version != SCHEMA_VERSION or any(name not in present for name in _REQUIRED):
            raise SchemaError("demo 样本事实库版本或结构不完整，请重新导入样本")
        return conn
    except Exception:
        conn.close()
        raise


def snapshot(conn, dest) -> None:
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()


def run_identity(conn) -> str:
    """只读：建库时已写入；读事务内调用不得触发写（视图令牌在读事务里取它）。"""
    return conn.execute("SELECT run_id FROM run_identity WHERE id=1").fetchone()["run_id"]


def new_run_identity(conn) -> str:
    """回退恢复后调用：生成新运行身份，旧视图令牌随之失效（spec §6.3、§12）。"""
    run_id = uuid.uuid4().hex
    with write_txn(conn):
        conn.execute("INSERT INTO run_identity(id, run_id, created_at) VALUES (1, ?, ?)"
                     " ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,"
                     " created_at=excluded.created_at", (run_id, now_iso()))
    return run_id


def series_gens(conn, code: str, dataset: str) -> tuple:
    row = conn.execute("SELECT revision_gen, closed_gen FROM series_state WHERE code=? AND dataset=?",
                       (code, dataset)).fetchone()
    return (row["revision_gen"], row["closed_gen"]) if row else (0, 0)


def last_commit_at(conn, code: str, dataset: str) -> str | None:
    row = conn.execute("SELECT last_commit_at FROM series_state WHERE code=? AND dataset=?",
                       (code, dataset)).fetchone()
    return row["last_commit_at"] if row else None


def binding_gen(conn, market: str, kind: str, item: str) -> int:
    row = conn.execute("SELECT binding_gen FROM binding_state WHERE market=? AND kind=? AND item=?",
                       (market, kind, str(item))).fetchone()
    return row["binding_gen"] if row else 1


def setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?,?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (key, json.dumps(value), now_iso()))


def quarantined_keys(conn, code: str, dataset: str) -> set:
    return {row["key"] for row in conn.execute(
        "SELECT key FROM quarantine WHERE code=? AND dataset=? AND released_at IS NULL",
        (code, dataset))}


def quarantine(conn, code, dataset, key, reason) -> None:
    conn.execute("INSERT INTO quarantine(code, dataset, key, reason, since) VALUES (?,?,?,?,?)"
                 " ON CONFLICT(code, dataset, key) DO UPDATE SET reason=excluded.reason,"
                 " since=excluded.since, released_at=NULL", (code, dataset, key, reason, now_iso()))
    bump_gens(conn, code, dataset, closed=True)


def release_quarantine(conn, code, dataset, key) -> None:
    conn.execute("UPDATE quarantine SET released_at=? WHERE code=? AND dataset=? AND key=?",
                 (now_iso(), code, dataset, key))
    bump_gens(conn, code, dataset, closed=True)


def bump_gens(conn, code, dataset, *, closed: bool) -> None:
    conn.execute(
        "INSERT INTO series_state(code, dataset, revision_gen, closed_gen) VALUES (?,?,1,?)"
        " ON CONFLICT(code, dataset) DO UPDATE SET revision_gen=revision_gen+1,"
        " closed_gen=closed_gen+?", (code, dataset, int(closed), int(closed)))


def touch_commit(conn, code, dataset) -> None:
    conn.execute(
        "INSERT INTO series_state(code, dataset, last_commit_at) VALUES (?,?,?)"
        " ON CONFLICT(code, dataset) DO UPDATE SET last_commit_at=excluded.last_commit_at",
        (code, dataset, now_iso()))


def read_day_rows(conn, code, start=None, end=None) -> list:
    sql = "SELECT * FROM current_day_bars WHERE code=?"
    args = [code]
    if start:
        sql += " AND trade_date>=?"
        args.append(start)
    if end:
        sql += " AND trade_date<=?"
        args.append(end)
    hidden = quarantined_keys(conn, code, "day")
    return [r for r in conn.execute(sql + " ORDER BY trade_date", args) if r["trade_date"] not in hidden]


def read_minute_rows(conn, code, fact_freq, start_slot=None, end_slot=None, *,
                     desc=False, limit=None) -> list:
    # 与视图 current_minute_bars 同义（每槽取最大 revision），但按槽相关子查询走主键：
    # 视图的 GROUP BY 会先把该标的全部分钟行分组再过滤范围，三年 m5 每次读取都要整表分组
    sql = ("SELECT b.* FROM minute_bars b WHERE b.code=? AND b.fact_freq=?"
           " AND b.revision=(SELECT MAX(x.revision) FROM minute_bars x WHERE x.code=b.code"
           " AND x.fact_freq=b.fact_freq AND x.slot_end=b.slot_end)")
    args = [code, fact_freq]
    if start_slot:
        sql += " AND b.slot_end>=?"
        args.append(start_slot)
    if end_slot:
        sql += " AND b.slot_end<?"
        args.append(end_slot)
    sql += " ORDER BY b.slot_end" + (" DESC" if desc else "")
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    hidden = quarantined_keys(conn, code, fact_freq)
    rows = [r for r in conn.execute(sql, args) if r["slot_end"] not in hidden]
    return list(reversed(rows)) if desc else rows


def record_gap(conn, code, dataset, start, end, reason) -> None:
    exists = conn.execute(
        "SELECT 1 FROM coverage_gaps WHERE code=? AND dataset=? AND start=? AND end=?"
        " AND resolved_at IS NULL", (code, dataset, start, end)).fetchone()
    if not exists:
        conn.execute("INSERT INTO coverage_gaps(code, dataset, start, end, reason, created_at)"
                     " VALUES (?,?,?,?,?,?)", (code, dataset, start, end, reason, now_iso()))


def open_gaps(conn, code=None, dataset=None) -> list:
    sql = "SELECT * FROM coverage_gaps WHERE resolved_at IS NULL"
    args = []
    if code:
        sql += " AND code=?"
        args.append(code)
    if dataset:
        sql += " AND dataset=?"
        args.append(dataset)
    return list(conn.execute(sql + " ORDER BY end DESC", args))


def resolve_gap(conn, gap_id) -> None:
    conn.execute("UPDATE coverage_gaps SET resolved_at=? WHERE gap_id=?", (now_iso(), gap_id))


def fail_gap(conn, gap_id, error) -> None:
    conn.execute("UPDATE coverage_gaps SET attempts=attempts+1, last_error=? WHERE gap_id=?",
                 (str(error)[:500], gap_id))



# ---- 提交路径：代次、来源优先级、重叠冲突、写失败（spec §5.3、§5.4）----

_DAY_FIELDS = ("open", "high", "low", "close", "volume", "volume_unit", "amount", "currency",
               "pc", "sf", "provenance")
_MINUTE_FIELDS = ("trade_date", "open", "high", "low", "close", "volume", "volume_unit",
                  "amount", "state", "trade_state")
# 分钟聚合与日线核对容差：A 股个股逐分相等；指数来源日线与分钟各自舍入到两位，
# 回放实测 167 个指数日恰差 0.01（与 P8 的 0.01 舍入边界一致）；港股按来源评估的 0.0015。
PRICE_TOL = {("CN", "stock"): 1e-6, ("CN", "index"): 0.0100001, ("HK", "stock"): 0.0015}


@dataclass
class CommitResult:
    batch_id: str
    inserted: int = 0
    revised: int = 0
    skipped: int = 0
    rejected: list = field(default_factory=list)
    pending_review: int = 0
    soft_flags: int = 0
    revision_gen: int = 0
    closed_gen: int = 0


def _record_batch(conn, result, *, code, item, source, gen, status, accepted) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO batches(batch_id, code, item, source, binding_gen, status,"
        " accepted, rejected, detail, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (result.batch_id, code, str(item), source, gen, status, accepted, len(result.rejected),
         json.dumps(result.rejected[:50], default=str, ensure_ascii=False), now_iso()))


def _require_gen(conn, market, kind, item, gen) -> None:
    current = binding_gen(conn, market, kind, item)
    if current != gen:
        raise StaleBinding(f"{market}/{kind}/{item}: batch gen {gen} != current {current}")


def _flag(conn, code, dataset, key, flag, detail) -> None:
    conn.execute("INSERT INTO soft_flags(code, dataset, key, flag, detail, created_at)"
                 " VALUES (?,?,?,?,?,?)",
                 (code, dataset, key, flag, json.dumps(detail, default=str), now_iso()))


def _pending(conn, code, dataset, key, values, reason, batch_id) -> None:
    conn.execute("INSERT INTO pending_review(code, dataset, key, incoming, reason, batch_id,"
                 " created_at) VALUES (?,?,?,?,?,?,?)",
                 (code, dataset, key, json.dumps(values, default=str), reason, batch_id, now_iso()))


def _stale_batch(conn, result, *, code, item, source, gen) -> None:
    try:
        with write_txn(conn):
            _record_batch(conn, result, code=code, item=item, source=source, gen=gen,
                          status="stale_binding", accepted=0)
    except sqlite3.Error:
        log.warning("过期代次批次登记失败 code=%s item=%s", code, item, exc_info=True)


def _single_code(rows) -> str | None:
    codes = {r.code for r in rows}
    if len(codes) > 1:
        raise ValueError(f"一个批次只能包含一个标的: {sorted(codes)}")
    return next(iter(codes), None)


def commit_day_rows(conn, rows, *, market, kind, item, source, binding_gen, today,
                    start=None, end=None) -> CommitResult:
    code = _single_code(rows)
    verdict = admission.check_day_rows(rows, today=today, start=start, end=end)
    result = CommitResult(batch_id=rows[0].batch_id if rows else uuid.uuid4().hex,
                          rejected=[(r.trade_date, why) for r, why in verdict.rejected])
    if code is None:
        return result
    try:
        with write_txn(conn):
            _require_gen(conn, market, kind, item, binding_gen)
            hidden = quarantined_keys(conn, code, "day")
            changed = False
            readable = 0                        # 提交后可读的行（隔离键不可读，不证明新鲜）
            for r in verdict.accepted:
                values = {f: getattr(r, f) for f in _DAY_FIELDS}
                old = conn.execute("SELECT * FROM current_day_bars WHERE code=? AND trade_date=?",
                                   (code, r.trade_date)).fetchone()
                if old is not None and all(old[f] == values[f] for f in _DAY_FIELDS):
                    result.skipped += 1
                    readable += r.trade_date not in hidden
                    continue
                if old is not None:
                    if PROVENANCE_RANK[r.provenance] < PROVENANCE_RANK[old["provenance"]]:
                        result.rejected.append((r.trade_date, admission.LOWER_PROVENANCE))
                        continue
                    if old["provenance"] == "final" and r.provenance == "final":
                        _pending(conn, code, "day", r.trade_date, values, "closed_conflict",
                                 result.batch_id)
                        result.pending_review += 1
                        continue
                    if old["provenance"] == "preopen" and r.provenance != "preopen" and r.pc != old["pc"]:
                        _flag(conn, code, "day", r.trade_date, "preopen_pc_mismatch",
                              {"preopen": old["pc"], "incoming": r.pc})
                        result.soft_flags += 1
                revision = 1 if old is None else old["revision"] + 1
                conn.execute(
                    "INSERT INTO day_bars(code, trade_date, revision, open, high, low, close, volume,"
                    " volume_unit, amount, currency, pc, sf, provenance, source, binding_gen,"
                    " batch_id, rev_kind, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, r.trade_date, revision, *(values[f] for f in _DAY_FIELDS),
                     source, binding_gen, result.batch_id, "ingest", now_iso()))
                if old is None:
                    result.inserted += 1
                else:
                    result.revised += 1
                readable += r.trade_date not in hidden
                changed = True
            if changed:
                bump_gens(conn, code, "day", closed=True)
            if readable:                        # 只有提交后可读的行才证明新鲜
                touch_commit(conn, code, "day")
            _record_batch(conn, result, code=code, item=item, source=source, gen=binding_gen,
                          status="committed", accepted=len(verdict.accepted))
            result.revision_gen, result.closed_gen = series_gens(conn, code, "day")
    except StaleBinding:
        _stale_batch(conn, result, code=code, item=item, source=source, gen=binding_gen)
        raise
    except sqlite3.Error as exc:
        raise FactsWriteError(f"事实库写入失败 code={code} item={item}") from exc
    return result


def commit_minute_rows(conn, rows, *, market, kind, item, fact_freq, source, binding_gen, today,
                       start_slot=None, end_slot=None) -> CommitResult:
    code = _single_code(rows)
    verdict = admission.check_minute_rows(rows, market=market, fact_freq=fact_freq, today=today,
                                          start_slot=start_slot, end_slot=end_slot)
    result = CommitResult(batch_id=rows[0].batch_id if rows else uuid.uuid4().hex,
                          rejected=[(r.slot_end, why) for r, why in verdict.rejected])
    if code is None:
        return result
    if not verdict.accepted:                  # 全批被拒也留批次证据（spec §5.3）
        try:
            with write_txn(conn):
                _record_batch(conn, result, code=code, item=item, source=source, gen=binding_gen,
                              status="committed", accepted=0)
        except sqlite3.Error as exc:
            raise FactsWriteError(f"事实库写入失败 code={code} item={item}") from exc
        return result
    try:
        with write_txn(conn):
            _require_gen(conn, market, kind, item, binding_gen)
            first = min(r.slot_end for r in verdict.accepted)
            prev = conn.execute(
                "SELECT close FROM current_minute_bars WHERE code=? AND fact_freq=? AND slot_end<?"
                " AND volume>0 AND trade_state!='suspended' ORDER BY slot_end DESC LIMIT 1",
                (code, fact_freq, first)).fetchone()
            marked = admission.mark_trade_state(verdict.accepted, prev["close"] if prev else None)
            hidden = quarantined_keys(conn, code, fact_freq)
            changed = closed_changed = False
            readable = 0
            for r in marked:
                values = {f: getattr(r, f) for f in _MINUTE_FIELDS}
                old = conn.execute("SELECT * FROM current_minute_bars WHERE code=? AND fact_freq=?"
                                   " AND slot_end=?", (code, fact_freq, r.slot_end)).fetchone()
                if old is not None and all(old[f] == values[f] for f in _MINUTE_FIELDS):
                    result.skipped += 1
                    readable += r.slot_end not in hidden
                    continue
                if old is not None and old["state"] == "closed":
                    if r.state == "forming":
                        result.rejected.append((r.slot_end, admission.FORMING_OVER_CLOSED))
                        continue
                    # 只差 traded/no_trade 标注时按修订：回填分片首根可能漏标（计划 A 决定 4）
                    diff = {f for f in _MINUTE_FIELDS if old[f] != values[f]}
                    relabel = (diff == {"trade_state"}
                               and {old["trade_state"], r.trade_state} <= {"traded", "no_trade"})
                    if not relabel:
                        _pending(conn, code, fact_freq, r.slot_end, values, "closed_conflict",
                                 result.batch_id)
                        result.pending_review += 1
                        continue
                revision = 1 if old is None else old["revision"] + 1
                conn.execute(
                    "INSERT INTO minute_bars(code, fact_freq, slot_end, revision, trade_date, open,"
                    " high, low, close, volume, volume_unit, amount, state, trade_state, source,"
                    " binding_gen, batch_id, rev_kind, recorded_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, fact_freq, r.slot_end, revision, *(values[f] for f in _MINUTE_FIELDS),
                     source, binding_gen, result.batch_id, "ingest", now_iso()))
                result.inserted += old is None
                result.revised += old is not None
                readable += r.slot_end not in hidden
                changed = True
                closed_changed = closed_changed or r.state == "closed"
            if changed:
                bump_gens(conn, code, fact_freq, closed=closed_changed)
            if readable:
                touch_commit(conn, code, fact_freq)
            _record_batch(conn, result, code=code, item=item, source=source, gen=binding_gen,
                          status="committed", accepted=len(verdict.accepted))
            result.revision_gen, result.closed_gen = series_gens(conn, code, fact_freq)
    except StaleBinding:
        _stale_batch(conn, result, code=code, item=item, source=source, gen=binding_gen)
        raise
    except sqlite3.Error as exc:
        raise FactsWriteError(f"事实库写入失败 code={code} item={item}") from exc
    return result


def _checks_scope(code, fact_freq) -> tuple:
    """分钟日线核对记录放哪张表：改动前的粒度（LEGACY_MINUTE_FACT：港股 m30、回退后的 A 股 m5）照旧在 day_checks
    （主键 (code, trade_date)，旧代码读写它），其他粒度（A 股 m15）在 minute_day_checks（主键带 fact_freq）。
    两者分开，m15 的核对不会覆盖同一天的 m5 记录，回退后旧证据仍在。返回 (表, 额外 WHERE, 额外参数)。"""
    if fact_freq == LEGACY_MINUTE_FACT[market_of(code)]:
        return "day_checks", "", ()
    return "minute_day_checks", " AND fact_freq=?", (fact_freq,)


def day_unsettled(conn, code, day, fact_freq, *, minutes=True) -> str | None:
    """当日数据的待裁决状态（只读，采集器定稿判据与视图状态栏共用）：pending_review（日线键为日期、现行分钟粒度
    键为该日槽位的未裁决待核验；minutes 为假时只看日线的）、reconcile_mismatch（现行粒度最近一次分钟日线核对
    不一致），否则 None。fact_freq 为 None（系统依赖只定稿日线、没有分钟事实）时不看分钟日线核对。"""
    if conn.execute("SELECT 1 FROM pending_review WHERE code=? AND verdict IS NULL AND ((dataset='day' AND"
                    " key=?) OR (dataset=? AND key LIKE ?)) LIMIT 1",
                    (code, day, (fact_freq if minutes else None) or "", f"{day} %")).fetchone():
        return "pending_review"
    if fact_freq is None:
        return None
    table, where, args = _checks_scope(code, fact_freq)
    return "reconcile_mismatch" if conn.execute(
        f"SELECT 1 FROM {table} WHERE code=? AND trade_date=? AND status='pending_review'{where}",
        (code, day, *args)).fetchone() else None


def review_days(conn, code, fact_freq, since) -> set:
    """since 起有未裁决待核验（现行分钟粒度）或现行粒度核对不一致的交易日。"""
    table, where, args = _checks_scope(code, fact_freq)
    days = {r[0] for r in conn.execute(
        f"SELECT trade_date FROM {table} WHERE code=? AND status='pending_review' AND trade_date>=?{where}",
        (code, since, *args))}
    return days | {r[0][:10] for r in conn.execute(
        "SELECT key FROM pending_review WHERE code=? AND dataset=? AND verdict IS NULL AND key>=?",
        (code, fact_freq, since))}


def reconcile_day(conn, code, trade_date, *, market, fact_freq) -> str | None:
    day_rows = [r for r in read_day_rows(conn, code, trade_date, trade_date)
                if r["provenance"] == "final"]
    if not day_rows:
        return None
    minutes = [r for r in read_minute_rows(conn, code, fact_freq,
                                           f"{trade_date} 00:00", f"{trade_date} 23:59")
               if r["state"] == "closed"]
    reason = admission.minute_day_mismatch(minutes, day_rows[0], tol=PRICE_TOL[(market, kind_of(code))])
    soft = admission.volume_not_conserved(minutes, day_rows[0])
    table, where, args = _checks_scope(code, fact_freq)
    with write_txn(conn):
        existed = conn.execute(f"SELECT 1 FROM {table} WHERE code=? AND trade_date=?{where}",
                               (code, trade_date, *args)).fetchone() is not None
        if reason:
            cols, marks = ("code, trade_date, status, reason, detail, checked_at", "?,?,?,?,?,?") if not args else \
                ("code, trade_date, fact_freq, status, reason, detail, checked_at", "?,?,?,?,?,?,?")
            conn.execute(f"INSERT OR REPLACE INTO {table}({cols}) VALUES ({marks})",
                         (code, trade_date, *args, "pending_review", reason,
                          json.dumps({"minute": admission.traded_ohlc(minutes)}), now_iso()))
        else:
            conn.execute(f"DELETE FROM {table} WHERE code=? AND trade_date=?{where}", (code, trade_date, *args))
        if bool(reason) != existed:           # 只在核对结论变化时推进令牌，定稿重试不反复 409
            bump_gens(conn, code, fact_freq, closed=True)
        if soft:
            _flag(conn, code, fact_freq, trade_date, soft, {})
    return reason


def resolve_review(conn, review_id, verdict) -> None:
    """人工或规则裁决待核验冲突：accept 推进修订，keep 维持旧值，quarantine 隔离该槽。
    调用方负责包在 write_txn 里。"""
    if verdict not in ("accept", "keep", "quarantine"):
        raise ValueError(verdict)
    review = conn.execute("SELECT * FROM pending_review WHERE review_id=?", (review_id,)).fetchone()
    values = json.loads(review["incoming"])
    code, dataset, key = review["code"], review["dataset"], review["key"]
    # 被接受的值来自 incoming 批次：来源与绑定代次随批次，不沿用旧行（计划 B 的 source/degraded 契约）
    batch = conn.execute("SELECT source, binding_gen FROM batches WHERE batch_id=?",
                         (review["batch_id"],)).fetchone()
    if verdict == "accept" and dataset == "day":
        old = conn.execute("SELECT * FROM current_day_bars WHERE code=? AND trade_date=?",
                           (code, key)).fetchone()
        conn.execute(
            "INSERT INTO day_bars(code, trade_date, revision, open, high, low, close, volume,"
            " volume_unit, amount, currency, pc, sf, provenance, source, binding_gen, batch_id,"
            " rev_kind, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, key, old["revision"] + 1, *(values[f] for f in _DAY_FIELDS), batch["source"],
             batch["binding_gen"], review["batch_id"], "correction", now_iso()))
        bump_gens(conn, code, dataset, closed=True)
    elif verdict == "accept":
        old = conn.execute("SELECT * FROM current_minute_bars WHERE code=? AND fact_freq=?"
                           " AND slot_end=?", (code, dataset, key)).fetchone()
        conn.execute(
            "INSERT INTO minute_bars(code, fact_freq, slot_end, revision, trade_date, open, high,"
            " low, close, volume, volume_unit, amount, state, trade_state, source, binding_gen,"
            " batch_id, rev_kind, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, dataset, key, old["revision"] + 1, *(values[f] for f in _MINUTE_FIELDS),
             batch["source"], batch["binding_gen"], review["batch_id"], "correction", now_iso()))
        bump_gens(conn, code, dataset, closed=True)
    elif verdict == "quarantine":
        quarantine(conn, code, dataset, key, "review_conflict")
    conn.execute("UPDATE pending_review SET verdict=?, decided_at=? WHERE review_id=?",
                 (verdict, now_iso(), review_id))


# ---- 计算审计（record_calc_run / runs_as_of）：语义沿用 spec §2.7，事务走 write_txn ----

def _signal_row(sig: dict) -> dict | None:
    """信号行规整：types list → JSON；payload 逐字存全量；缺 dt/label 行跳过并告警。"""
    dt, label = sig.get("dt"), sig.get("label")
    if dt is None or label is None:
        log.warning("结论信号缺 dt/label，跳过该行: %s", repr(sig)[:200])
        return None
    types = sig.get("types")
    if isinstance(types, list):
        types = json.dumps(types, ensure_ascii=False)
    return {"dt": dt, "label": label, "price": sig.get("price"), "side": sig.get("side"),
            "level": sig.get("level"), "types": types,
            "forming": int(bool(sig.get("forming"))),
            "payload": json.dumps(sig, sort_keys=True, ensure_ascii=False, default=repr)}


def record_calc_run(conn, code, freq, *, input_start, input_end, input_data_version,
                    calculation_id, source_kind, signals, reported_at=None) -> dict:
    """计算结论持久化：run+signals 同一事务（规避 record_run 内嵌 commit 的半截写）。

    幂等键 (code, freq, input_data_version, calculation_id)；事务失败 → 独立事务
    INSERT OR IGNORE 留 status='failed' 痕 + 告警，仍失败再告警，recorded=False 不抛
    （写库失败可观察、不阻断、不伪装完整，spec §2.7）。
    """
    if source_kind not in ("online_observed", "historical_recompute"):
        raise ValueError("unknown source_kind")
    status = "ok" if signals else "ok_no_signals"
    try:
        with write_txn(conn):
            row = conn.execute(
                "SELECT run_id, status FROM calc_runs WHERE code=? AND freq=?"
                " AND input_data_version=? AND calculation_id=?",
                (code, freq, input_data_version, calculation_id)).fetchone()
            if row and row["status"] != "failed":
                return {"recorded": True, "run_id": row["run_id"], "created": False}
            if row:
                # F5：failed 行不占幂等键——同事务恢复 run 状态并整集合补录
                run_id = row["run_id"]
                conn.execute(
                    "UPDATE calc_runs SET input_start=?, input_end=?, recorded_at=?,"
                    " source_kind=?, reported_at=?, status=? WHERE run_id=?",
                    (input_start, input_end, now_iso(), source_kind, reported_at, status,
                     run_id))
                conn.execute("DELETE FROM calc_signals WHERE run_id=?", (run_id,))
                for sig in signals:
                    row_dict = _signal_row(sig)
                    if row_dict is None:
                        continue
                    conn.execute(
                        "INSERT INTO calc_signals(run_id, dt, label, price, side, level,"
                        " types, forming, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_id, row_dict["dt"], row_dict["label"], row_dict["price"],
                         row_dict["side"], row_dict["level"], row_dict["types"],
                         row_dict["forming"], row_dict["payload"]))
                return {"recorded": True, "run_id": run_id, "created": False,
                        "recovered": True}
            cur = conn.execute(
                "INSERT INTO calc_runs(code, freq, input_start, input_end, input_data_version,"
                " calculation_id, recorded_at, source_kind, reported_at, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, freq, input_start, input_end, input_data_version, calculation_id,
                 now_iso(), source_kind, reported_at, status))
            run_id = cur.lastrowid
            for sig in signals:
                row_dict = _signal_row(sig)
                if row_dict is None:
                    continue
                conn.execute(
                    "INSERT INTO calc_signals(run_id, dt, label, price, side, level, types,"
                    " forming, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, row_dict["dt"], row_dict["label"], row_dict["price"],
                     row_dict["side"], row_dict["level"], row_dict["types"],
                     row_dict["forming"], row_dict["payload"]))
        return {"recorded": True, "run_id": run_id, "created": True}
    except Exception:
        log.warning("结论历史写入失败，尝试 failed 留痕 code=%s freq=%s version=%s calc=%s",
                    code, freq, input_data_version, calculation_id, exc_info=True)
        try:
            with write_txn(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO calc_runs(code, freq, input_start, input_end,"
                    " input_data_version, calculation_id, recorded_at, source_kind,"
                    " reported_at, status) VALUES (?,?,?,?,?,?,?,?,?,'failed')",
                    (code, freq, input_start, input_end, input_data_version,
                     calculation_id, now_iso(), source_kind, reported_at))
        except Exception:
            log.warning("结论历史 failed 留痕同样失败 code=%s freq=%s",
                        code, freq, exc_info=True)
        return {"recorded": False, "run_id": None, "created": False}


def find_calc_run(conn, code, freq, input_data_version, calculation_id):
    """只读：幂等键对应的结论记录行（run_id, status），没有为 None。不开写事务，供写入前先查。"""
    return conn.execute(
        "SELECT run_id, status FROM calc_runs WHERE code=? AND freq=?"
        " AND input_data_version=? AND calculation_id=?",
        (code, freq, input_data_version, calculation_id)).fetchone()


def runs_as_of(conn, code, freq, as_of: str) -> list[dict]:
    """「当时已记录」查询：只含在线观测且 recorded_at <= as_of；回算永不混入。"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM calc_runs WHERE code=? AND freq=? AND source_kind='online_observed'"
        " AND recorded_at<=? ORDER BY recorded_at", (code, freq, as_of))]
