"""新事实库 schema、事务与基础读写（spec §5.3、§5.4）。"""
import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path

from chanapp.engine.kline import facts


class FactsSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / facts.DB_NAME
        self.conn = facts.open_facts(self.path)
        self.addCleanup(self.conn.close)

    def test_fresh_db_has_all_objects_and_owner_only_mode(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master")}
        for name in ("day_bars", "minute_bars", "current_day_bars", "current_minute_bars",
                     "series_state", "quarantine", "pending_review", "day_checks", "soft_flags",
                     "coverage_gaps", "batches", "calendar", "instruments", "binding_state",
                     "probe_runs", "settings", "quota_usage", "vendor_qfq_bars",
                     "vendor_qfq_publish", "run_identity", "calc_runs", "calc_signals"):
            self.assertIn(name, names)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_existing_v1_db_gains_additive_checks_table_without_version_bump(self):
        # 第三阶段第二轮复审阻断 3（实现后补写）：m15 核对表是版本 1 之上的纯增量，打开旧库时补建、不升版本号，
        # 回退到不认识它的旧代码时旧代码照常打开
        with facts.write_txn(self.conn):
            self.conn.execute("DROP TABLE minute_day_checks")
        self.conn.close()
        self.conn = facts.open_facts(self.path)
        self.addCleanup(self.conn.close)
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master")}
        self.assertIn("minute_day_checks", names)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], facts.SCHEMA_VERSION)

    def test_refuses_newer_schema(self):
        self.conn.execute(f"PRAGMA user_version={facts.SCHEMA_VERSION + 1}")
        self.conn.close()
        with self.assertRaises(facts.SchemaError):
            facts.open_facts(self.path)

    def test_refuses_half_built_db(self):
        self.conn.execute("DROP VIEW current_minute_bars")
        self.conn.close()
        with self.assertRaises(facts.SchemaError):
            facts.open_facts(self.path)

    def test_opening_complete_db_does_not_wait_for_writer(self):
        # #42 复审：并发首次建表的修复曾让每次打开都 BEGIN IMMEDIATE，采集器写事务期间请求线程首读等满 5 秒后失败
        with facts.write_txn(self.conn):
            facts.set_setting(self.conn, "committed", "yes")
        self.conn.execute("BEGIN IMMEDIATE")
        self.addCleanup(lambda: self.conn.in_transaction and self.conn.execute("ROLLBACK"))
        facts.set_setting(self.conn, "pending", "no")
        started = time.monotonic()
        reader = facts.open_facts(self.path)
        self.addCleanup(reader.close)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(facts.setting(reader, "committed", None), "yes")
        self.assertIsNone(facts.setting(reader, "pending", None))

    def test_write_txn_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with facts.write_txn(self.conn):
                facts.set_setting(self.conn, "k", "v")
                raise RuntimeError("boom")
        self.assertIsNone(facts.setting(self.conn, "k", None))

    def test_run_identity_is_stable_until_renewed(self):
        first = facts.run_identity(self.conn)
        self.assertEqual(first, facts.run_identity(self.conn))
        renewed = facts.new_run_identity(self.conn)
        self.assertNotEqual(first, renewed)
        self.assertEqual(renewed, facts.run_identity(self.conn))

    def test_binding_gen_defaults_to_one(self):
        self.assertEqual(facts.binding_gen(self.conn, "CN", "stock", "day_history"), 1)

    def test_quarantined_keys_hidden_from_reads_and_persist(self):
        with facts.write_txn(self.conn):
            self.conn.execute(
                "INSERT INTO day_bars(code, trade_date, revision, open, high, low, close, volume,"
                " volume_unit, amount, currency, pc, sf, provenance, source, binding_gen, batch_id,"
                " rev_kind, recorded_at) VALUES ('sh600036','2026-09-24',1,1,1,1,1,1,'lot',1,'CNY',"
                "1,0,'final','mairui',1,'b','ingest','t')")
            facts.quarantine(self.conn, "sh600036", "day", "2026-09-24", "proven_bad")
        self.assertEqual(facts.read_day_rows(self.conn, "sh600036"), [])
        self.conn.close()
        conn = facts.open_facts(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(facts.quarantined_keys(conn, "sh600036", "day"), {"2026-09-24"})

    def test_gap_lifecycle(self):
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, "sh600036", "m5", "2024-01-01 09:30", "2024-01-31 15:00", "backfill")
        gap = facts.open_gaps(self.conn, "sh600036", "m5")[0]
        with facts.write_txn(self.conn):
            facts.fail_gap(self.conn, gap["gap_id"], "timeout")
        self.assertEqual(facts.open_gaps(self.conn)[0]["attempts"], 1)
        with facts.write_txn(self.conn):
            facts.resolve_gap(self.conn, gap["gap_id"])
        self.assertEqual(facts.open_gaps(self.conn), [])

    def test_calc_run_idempotent(self):
        kwargs = dict(input_start="a", input_end="b", input_data_version="v",
                      calculation_id="c", source_kind="online_observed",
                      signals=[{"dt": "2026-09-24", "label": "b1"}])
        first = facts.record_calc_run(self.conn, "sh600036", "day", **kwargs)
        second = facts.record_calc_run(self.conn, "sh600036", "day", **kwargs)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])

    def test_snapshot_is_consistent_copy(self):
        dest = Path(self.tmp.name) / "snap.sqlite"
        facts.snapshot(self.conn, dest)
        copy = sqlite3.connect(dest)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], facts.SCHEMA_VERSION)

    def test_calc_run_write_is_atomic(self):
        """信号写入中途失败时 run 与 signals 一起回滚，只留 failed 痕（spec §2.7 语义沿用）。"""
        bad = [{"dt": "2026-09-24", "label": "b1", "price": object()}]  # price 无法入库
        result = facts.record_calc_run(self.conn, "sh600036", "day", input_start="a", input_end="b",
                                       input_data_version="v2", calculation_id="c",
                                       source_kind="online_observed", signals=bad)
        self.assertFalse(result["recorded"])
        rows = self.conn.execute("SELECT status FROM calc_runs WHERE input_data_version='v2'").fetchall()
        self.assertEqual([r["status"] for r in rows], ["failed"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM calc_signals").fetchone()[0], 0)


class FactsFileContractTests(unittest.TestCase):
    """迁自旧事实层 TestSnapshotContract / TestPragmas：文件权限、连接参数、快照可只读查询。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def open(self, name=facts.DB_NAME):
        conn = facts.open_facts(self.dir / name)
        self.addCleanup(conn.close)
        return conn

    def test_owner_only_under_permissive_umask_including_wal_files(self):
        # umask 000 下建库：只断 &0o077 会在 CI 的 umask 077 下误绿，这里强制宽松 umask
        saved = os.umask(0)
        try:
            self.open("umasked.sqlite")
        finally:
            os.umask(saved)
        files = sorted(self.dir.glob("umasked.sqlite*"))
        self.assertEqual({p.name for p in files},
                         {"umasked.sqlite", "umasked.sqlite-wal", "umasked.sqlite-shm"})
        for path in files:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)

    def test_reopen_tightens_existing_db_mode(self):
        self.open().close()
        os.chmod(self.dir / facts.DB_NAME, 0o644)
        self.open()
        self.assertEqual(stat.S_IMODE((self.dir / facts.DB_NAME).stat().st_mode), 0o600)

    def test_connection_pragmas(self):
        conn = self.open()
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_snapshot_queryable_read_only_with_current_views(self):
        conn = self.open()
        with facts.write_txn(conn):
            conn.execute(
                "INSERT INTO day_bars(code, trade_date, revision, open, high, low, close, volume,"
                " volume_unit, amount, currency, pc, sf, provenance, source, binding_gen, batch_id,"
                " rev_kind, recorded_at) VALUES ('sh600036','2026-09-24',1,1,1,1,2,1,'lot',1,'CNY',"
                "1,0,'final','mairui',1,'b','ingest','t')")
        dest = self.dir / "snap.sqlite"
        facts.snapshot(conn, dest)
        ro = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        self.addCleanup(ro.close)
        self.assertEqual(ro.execute("SELECT trade_date, close FROM current_day_bars").fetchall(),
                         [("2026-09-24", 2.0)])
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE t(x)")


class CalcRunTests(unittest.TestCase):
    """计算审计的保留语义（迁自旧事实层测试 TestCalcRuns / TestRecordCalcRun）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)

    def record(self, version="v1", signals=(), source_kind="online_observed"):
        return facts.record_calc_run(self.conn, "sh600036", "day", input_start="a", input_end="b",
                                     input_data_version=version, calculation_id="c",
                                     source_kind=source_kind, signals=list(signals))

    def test_zero_signals_is_recorded_distinct_from_missing_run(self):
        self.assertTrue(self.record(signals=[])["recorded"])
        rows = facts.runs_as_of(self.conn, "sh600036", "day", "2999-01-01")
        self.assertEqual([r["status"] for r in rows], ["ok_no_signals"])

    def test_historical_recompute_never_appears_as_of(self):
        self.record("v1")
        self.record("v0-old", source_kind="historical_recompute")
        rows = facts.runs_as_of(self.conn, "sh600036", "day", "2999-01-01")
        self.assertEqual([r["input_data_version"] for r in rows], ["v1"])

    def test_failed_run_does_not_hold_idempotency_key_and_recovers_on_retry(self):
        self.assertFalse(self.record(signals=[{"dt": "2026-09-24", "label": "b1", "price": object()}])["recorded"])
        retry = self.record(signals=[{"dt": "2026-09-24", "label": "b1", "price": 10.0}])
        self.assertTrue(retry["recorded"])
        self.assertTrue(retry.get("recovered"))
        status = self.conn.execute("SELECT status FROM calc_runs WHERE input_data_version='v1'").fetchall()
        self.assertEqual([r["status"] for r in status], ["ok"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM calc_signals").fetchone()[0], 1)

    def test_signal_payload_keeps_every_field(self):
        sig = {"dt": "2026-09-24", "label": "B1", "price": 10.0, "types": ["1"],
               "evidence": {"a": [1, "x"]}, "raw": "逐 字"}
        self.record(signals=[sig])
        row = self.conn.execute("SELECT types, payload FROM calc_signals").fetchone()
        self.assertEqual(json.loads(row["types"]), ["1"])
        self.assertEqual(json.loads(row["payload"]), sig)
