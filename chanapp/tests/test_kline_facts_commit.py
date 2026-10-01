"""事实提交：绑定代次、修订优先级、重叠冲突、写失败（spec §5.3、§5.4）。"""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from chanapp.engine.kline import admission as adm
from chanapp.engine.kline import facts
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow

TODAY = "2026-09-28"
KW = dict(market="CN", kind="stock", source="mairui", binding_gen=1, today=TODAY)


def day(date="2026-09-24", c=10.5, pc=10.0, prov="final", o=10.0, h=11.0, l=9.5, v=100.0):
    return RawDayRow("sh600036", date, o, h, l, c, v, "lot", 1.0, "CNY", pc, 0, prov, "b")


def minute(slot="2026-09-28 09:35", c=10.1, state="closed", v=10.0):
    return RawMinuteRow("sh600036", slot[:10], slot, 10.0, max(10.2, c), 9.9, c, v, "lot", 1.0,
                        state, "traded", "b")


class CommitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)

    def commit_day(self, rows, **kw):
        return facts.commit_day_rows(self.conn, rows, **{**KW, "item": "day_history", **kw})

    def commit_min(self, rows, **kw):
        return facts.commit_minute_rows(self.conn, rows, fact_freq="m5",
                                        **{**KW, "item": "minute_live", **kw})

    def test_insert_then_identical_is_skip_but_refreshes_commit_time(self):
        self.assertEqual(self.commit_day([day()]).inserted, 1)
        first = facts.last_commit_at(self.conn, "sh600036", "day")
        result = self.commit_day([day()])
        self.assertEqual((result.inserted, result.skipped), (0, 1))
        self.assertIsNotNone(first)
        self.assertEqual(facts.series_gens(self.conn, "sh600036", "day"), (1, 1))

    def test_rejected_or_review_only_batches_do_not_refresh_commit_time(self):
        # 格式准入通过、但因低优先级或冲突没有任何行可读的批次，不能让 stale 看起来新鲜
        self.commit_day([day()])
        self.commit_min([minute()])
        self.conn.execute("UPDATE series_state SET last_commit_at='2026-09-28T09:00:00'")
        self.commit_day([day(c=10.8, prov="live")])          # 低优先级被拒
        self.commit_day([day(c=10.6)])                       # final/final 冲突转待核验
        self.commit_min([minute(state="forming", c=10.3)])   # forming 覆盖 closed 被拒
        self.assertEqual(facts.last_commit_at(self.conn, "sh600036", "day"), "2026-09-28T09:00:00")
        self.assertEqual(facts.last_commit_at(self.conn, "sh600036", "m5"), "2026-09-28T09:00:00")
        self.commit_min([minute()])                           # 与已存行一致：确认仍新鲜
        self.assertNotEqual(facts.last_commit_at(self.conn, "sh600036", "m5"), "2026-09-28T09:00:00")

    def test_quarantined_keys_do_not_refresh_commit_time(self):
        # 已隔离的键不可读：上游再次提交相同值或新值（revision 推进）都不能让 stale 看起来新鲜
        self.commit_day([day()])
        self.commit_min([minute()])
        with facts.write_txn(self.conn):
            facts.quarantine(self.conn, "sh600036", "day", "2026-09-24", "proven_wrong")
            facts.quarantine(self.conn, "sh600036", "m5", "2026-09-28 09:35", "proven_wrong")
            self.conn.execute("UPDATE series_state SET last_commit_at='2026-09-28T09:00:00'")
        self.commit_day([day()])                              # 相同值：skipped
        self.commit_day([day(c=10.8, prov="live")])           # 低优先级被拒
        self.commit_min([minute()])                           # 相同值
        self.commit_min([minute(state="closed", c=10.15)])    # closed 冲突转待核验
        self.assertEqual(facts.last_commit_at(self.conn, "sh600036", "day"), "2026-09-28T09:00:00")
        self.assertEqual(facts.last_commit_at(self.conn, "sh600036", "m5"), "2026-09-28T09:00:00")
        pre = day(TODAY, c=None, o=None, h=None, l=None, v=None, prov="preopen", pc=16.77)
        with facts.write_txn(self.conn):
            facts.quarantine(self.conn, "sh600036", "day", TODAY, "proven_wrong")
        self.commit_day([pre], item="preopen_ref")
        result = self.commit_day([day(TODAY, pc=16.70)])      # 隔离键上的新 revision
        self.assertEqual(result.revised, 1)                   # 计数语义不变
        self.assertEqual(facts.last_commit_at(self.conn, "sh600036", "day"), "2026-09-28T09:00:00")
        self.commit_day([day("2026-09-25"), day(TODAY, pc=16.70)])   # 同批有未隔离的可读行：照常刷新
        self.assertNotEqual(facts.last_commit_at(self.conn, "sh600036", "day"), "2026-09-28T09:00:00")
        self.commit_min([minute("2026-09-28 09:40")])
        self.assertNotEqual(facts.last_commit_at(self.conn, "sh600036", "m5"), "2026-09-28T09:00:00")

    def test_stale_binding_rejects_whole_batch(self):
        with facts.write_txn(self.conn):
            self.conn.execute("INSERT INTO binding_state VALUES ('CN','stock','day_history',"
                              "'baostock',2,'switch','t')")
        with self.assertRaises(facts.StaleBinding):
            self.commit_day([day()], binding_gen=1)
        self.assertEqual(facts.read_day_rows(self.conn, "sh600036"), [])
        status = self.conn.execute("SELECT status FROM batches").fetchone()["status"]
        self.assertEqual(status, "stale_binding")

    def test_live_never_overwrites_final(self):
        self.commit_day([day()])
        result = self.commit_day([day(c=10.8, prov="live")])
        self.assertEqual(result.rejected, [("2026-09-24", adm.LOWER_PROVENANCE)])
        self.assertEqual(facts.read_day_rows(self.conn, "sh600036")[0]["close"], 10.5)

    def test_final_final_conflict_goes_to_review(self):
        self.commit_day([day()])
        result = self.commit_day([day(c=10.6)])
        self.assertEqual(result.pending_review, 1)
        self.assertEqual(facts.read_day_rows(self.conn, "sh600036")[0]["close"], 10.5)
        review = self.conn.execute("SELECT review_id FROM pending_review").fetchone()["review_id"]
        with facts.write_txn(self.conn):
            facts.resolve_review(self.conn, review, "accept")
        self.assertEqual(facts.read_day_rows(self.conn, "sh600036")[0]["close"], 10.6)

    def test_accepted_cross_source_revision_carries_incoming_source(self):
        # 主源 final 后切冷备同键冲突，人工接受冷备值：修订的来源与代次必须是冷备批次，视图才能报 degraded
        self.commit_day([day()])
        with facts.write_txn(self.conn):
            self.conn.execute("INSERT INTO binding_state VALUES ('CN','stock','day_history',"
                              "'baostock',2,'switch','t')")
        self.commit_day([replace(day(c=10.6), batch_id="cold-day")], source="baostock", binding_gen=2)
        self.commit_min([replace(minute(), batch_id="main-min")])
        self.commit_min([replace(minute(c=10.15), batch_id="cold-min")], source="pytdx")
        with facts.write_txn(self.conn):
            for r in self.conn.execute("SELECT review_id FROM pending_review").fetchall():
                facts.resolve_review(self.conn, r["review_id"], "accept")
        d = facts.read_day_rows(self.conn, "sh600036")[0]
        self.assertEqual((d["close"], d["source"], d["binding_gen"]), (10.6, "baostock", 2))
        m = facts.read_minute_rows(self.conn, "sh600036", "m5")[0]
        self.assertEqual((m["close"], m["source"]), (10.15, "pytdx"))

    def test_preopen_then_final_pc_change_advances_token_gen_and_flags(self):
        pre = day(TODAY, c=None, o=None, h=None, l=None, v=None, prov="preopen", pc=16.77)
        self.commit_day([pre], item="preopen_ref")
        gen_before = facts.series_gens(self.conn, "sh600036", "day")[1]
        result = self.commit_day([day(TODAY, pc=16.70)])
        self.assertEqual(result.revised, 1)
        self.assertEqual(result.soft_flags, 1)
        self.assertGreater(facts.series_gens(self.conn, "sh600036", "day")[1], gen_before)

    def test_pc_only_change_is_a_revision(self):
        self.commit_day([day(prov="live")])
        result = self.commit_day([day(pc=9.9, prov="live")])
        self.assertEqual(result.revised, 1)

    def test_forming_updates_do_not_advance_closed_gen(self):
        self.commit_min([minute(state="forming")])
        self.commit_min([minute(c=10.15, state="forming")])
        revision_gen, closed_gen = facts.series_gens(self.conn, "sh600036", "m5")
        self.assertEqual((revision_gen, closed_gen), (2, 0))
        self.commit_min([minute(c=10.18, state="closed")])
        self.assertEqual(facts.series_gens(self.conn, "sh600036", "m5")[1], 1)

    def test_forming_after_closed_rejected(self):
        self.commit_min([minute()])
        result = self.commit_min([minute(c=10.3, state="forming")])
        self.assertEqual(result.rejected, [("2026-09-28 09:35", adm.FORMING_OVER_CLOSED)])

    def test_write_failure_raises_and_publishes_nothing(self):
        self.conn.execute("CREATE TRIGGER boom BEFORE INSERT ON day_bars WHEN NEW.close = 99"
                          " BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertRaises(facts.FactsWriteError):
            self.commit_day([day(), day("2026-09-25", c=99.0, h=99.0)])
        self.assertEqual(facts.read_day_rows(self.conn, "sh600036"), [])
        self.assertIsNone(facts.last_commit_at(self.conn, "sh600036", "day"))

    def test_reconcile_day_marks_pending_check(self):
        self.commit_day([day("2026-09-24", o=10.0, h=10.6, l=9.5, c=10.5)])
        rows = [minute("2026-09-24 09:35", c=10.1), minute("2026-09-24 15:00", c=10.5)]
        facts.commit_minute_rows(self.conn, rows, item="minute_history", fact_freq="m5", **KW)
        reason = facts.reconcile_day(self.conn, "sh600036", "2026-09-24", market="CN", fact_freq="m5")
        self.assertEqual(reason, adm.MINUTE_DAY_OHLC)
        status = self.conn.execute("SELECT status FROM day_checks").fetchone()["status"]
        self.assertEqual(status, "pending_review")

    def test_closed_trade_state_only_change_is_revision_not_review(self):
        """回填分片首根拿不到上一根成交价时可能漏标 no_trade；只差 trade_state 时按修订处理。"""
        flat = RawMinuteRow("sh600036", "2024-09-27", "2024-09-27 10:30", 34.97, 34.97, 34.97, 34.97,
                            0.0, "lot", 0.0, "closed", "traded", "b")
        facts.commit_minute_rows(self.conn, [flat], item="minute_history", fact_freq="m5", **KW)
        result = facts.commit_minute_rows(self.conn, [replace(flat, trade_state="no_trade")],
                                          item="minute_history", fact_freq="m5", **KW)
        self.assertEqual((result.revised, result.pending_review), (1, 0))

    def test_rejected_rows_never_readable(self):
        bad = day("2026-09-23", l=10.6)          # low > min(o, c)
        result = self.commit_day([day(), bad])
        self.assertEqual(result.rejected, [("2026-09-23", adm.BAD_OHLC)])
        self.assertEqual([r["trade_date"] for r in facts.read_day_rows(self.conn, "sh600036")],
                         ["2026-09-24"])


class ReconcileToleranceTests(unittest.TestCase):
    """回放实测：麦蕊指数日线与分钟各自舍入到两位，167 个指数日恰差 0.01；个股 0 差。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)

    def seed(self, code, kind, day_close):
        facts.commit_day_rows(self.conn, [RawDayRow(code, "2024-09-27", 10.0, 10.5, 9.9, day_close, 20.0,
                                                    "lot", 1.0, "CNY", 9.9, 0, "final", "b")],
                              market="CN", kind=kind, item="day_history", source="mairui",
                              binding_gen=1, today=TODAY)
        rows = [RawMinuteRow(code, "2024-09-27", "2024-09-27 09:35", 10.0, 10.5, 9.9, 10.2, 10.0, "lot",
                             1.0, "closed", "traded", "b"),
                RawMinuteRow(code, "2024-09-27", "2024-09-27 15:00", 10.2, 10.4, 10.0, 10.3, 10.0, "lot",
                             1.0, "closed", "traded", "b")]
        facts.commit_minute_rows(self.conn, rows, market="CN", kind=kind, item="minute_history",
                                 fact_freq="m5", source="mairui", binding_gen=1, today=TODAY)

    def test_index_rounding_boundary_is_not_a_mismatch(self):
        self.seed("sh000001", "index", 10.31)
        self.assertIsNone(facts.reconcile_day(self.conn, "sh000001", "2024-09-27", market="CN",
                                              fact_freq="m5"))

    def test_stock_keeps_exact_tolerance(self):
        self.seed("sh600036", "stock", 10.31)
        self.assertEqual(facts.reconcile_day(self.conn, "sh600036", "2024-09-27", market="CN",
                                             fact_freq="m5"), adm.MINUTE_DAY_OHLC)


class AuditAndTokenChurnTests(CommitTests):
    def test_all_rejected_minute_batch_is_recorded(self):
        bad = minute("2026-09-28 13:41")                      # 非网格槽
        result = self.commit_min([bad])
        self.assertEqual(result.rejected, [("2026-09-28 13:41", adm.OFF_GRID)])
        batch = self.conn.execute("SELECT status, accepted, rejected FROM batches WHERE batch_id=?",
                                  (result.batch_id,)).fetchone()
        self.assertEqual(tuple(batch), ("committed", 0, 1))

    def test_repeated_reconcile_does_not_churn_token(self):
        self.commit_day([day("2026-09-24", o=10.0, h=10.6, l=9.5, c=10.5)])
        rows = [minute("2026-09-24 09:35", c=10.1), minute("2026-09-24 15:00", c=10.5)]
        facts.commit_minute_rows(self.conn, rows, item="minute_history", fact_freq="m5", **KW)
        facts.reconcile_day(self.conn, "sh600036", "2026-09-24", market="CN", fact_freq="m5")
        gen = facts.series_gens(self.conn, "sh600036", "m5")[1]
        facts.reconcile_day(self.conn, "sh600036", "2026-09-24", market="CN", fact_freq="m5")
        self.assertEqual(facts.series_gens(self.conn, "sh600036", "m5")[1], gen)
