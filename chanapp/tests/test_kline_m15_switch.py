"""A 股分钟事实改原生 m15（目标 2026-09-29「周期、刷新与定稿」，第三阶段最后一片）。

实现前列出的失败方式（每条对应下方用例；未覆盖的写在末尾）：
- m5 与 m15 混读：库里留着的 m5 行仍进入 m30/m60 聚合，或 m15 周期仍从 m5 聚合；
- 旧 m5 规划标记让 m15 永远不做分钟回填；旧 m5 缺口继续驱动请求（按 m5 取数、写 m5 行），或让读取判为落后；
- 迟到的旧代次批次写入；切换不更新绑定代次（视图令牌不变，旧页面分页不 409）；重复启动反复推进代次；
- provider 仍按 5 分钟端点取数；冷备 pytdx 拒绝 m15；
- 旧 m5 行被删或改写（回退后无法复用）。
未覆盖：迁移失败回到迁移前快照属于运维步骤（事实库 .backup），由隔离副本演练取证，不在单测里。
"""
import json
from unittest import mock
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import bindings, collector, facts, sessions, views
from chanapp.engine.kline.providers import mairui, pytdx_raw
from chanapp.engine.kline.rows import FetchItem, RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests.test_kline_collector import FullFake
from chanapp.tests.test_kline_finalize_schedule import CODE, DAY, Base, at

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw" / "mairui"
M15 = sessions.slots("CN", "m15")
M5 = sessions.slots("CN", "m5")


def minute_rows(code, day, slots, price, state="closed"):
    return [RawMinuteRow(code, day, f"{day} {t}", price, price, price, price, 1, "lot", 1, state, "traded",
                         new_batch_id()) for t in slots]


class Recorder(FullFake):
    """记录每次分钟请求的事实粒度；按所请求粒度的网格返回。"""

    def minute_history(self, code, fact_freq, start, end, *, now):
        self.calls.append(("minute", code, fact_freq, start, end))
        return [r for r in minute_rows(code, start[:10], sessions.slots("CN", fact_freq), 10)
                if start <= r.slot_end <= end]


class ReadSwitchTests(Base):
    def setUp(self):
        super().setUp()
        self.known_calendar()

    def commit(self, fact, slots, price):
        facts.commit_minute_rows(self.conn, minute_rows(CODE, DAY, slots, price), market="CN", kind="stock",
                                 item="minute_history", fact_freq=fact, source="mairui", binding_gen=1, today=DAY)

    def test_cn_minute_views_read_native_m15_and_ignore_old_m5_rows(self):
        self.commit("m5", M5, 9)                          # 切换前留下的 m5 事实（价格 9）
        self.commit("m15", M15, 11)                       # 新 m15 事实（价格 11）
        for freq in ("m30", "m60"):
            bars = views.read_view(self.conn, CODE, freq, adjust="raw", now=at(17, 40)).bars
            self.assertTrue(bars, freq)
            self.assertEqual({b["close"] for b in bars}, {11}, freq)
        m15 = views.read_view(self.conn, CODE, "m15", adjust="raw", now=at(17, 40)).bars
        self.assertEqual([b["dt"][11:] for b in m15], list(M15))
        m5 = views.read_view(self.conn, CODE, "m5", adjust="raw", now=at(17, 40))   # 细于事实粒度：不伪造
        self.assertEqual((m5.bars, [n["code"] for n in m5.notices]), ([], ["unsupported"]))
        self.assertEqual(len(facts.read_minute_rows(self.conn, CODE, "m5")), len(M5))   # 旧行只读保留

    def test_old_m5_gap_neither_drives_requests_nor_marks_lagging(self):
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, CODE, "m5", "2026-09-24 09:35", "2026-09-24 15:00", "backfill")
        provider = Recorder()
        c = self.make(provider)
        self.clock.t = at(12, 0).timestamp()
        self.assertFalse(c.lagging(CODE, at(12, 0)))      # 盘中：今天的定稿不参与，只看缺口与水位
        self.clock.t = at(20, 0).timestamp()
        c.drain_gaps(10)
        c.history_tick(at(20, 0))
        self.assertFalse([call for call in provider.calls if call[0] == "minute" and call[2] == "m5"])
        self.assertIn(("m5", "backfill"), {(g["dataset"], g["reason"]) for g in facts.open_gaps(self.conn, CODE)})


class LegacyReviewTests(Base):
    """第三阶段复审应修 5：切换前留下的 m5 待核验与 m5 分钟日线核对（day_checks）不影响 m15 的完成判断；
    第二轮阻断 3：m15 的核对另存 minute_day_checks，不覆盖同一天的 m5 记录。"""

    def setUp(self):
        super().setUp()
        self.known_calendar()
        self.commit_today()                                  # 当天 final 日线 + 全部 m15 槽
        with facts.write_txn(self.conn):
            self.conn.execute("INSERT INTO pending_review(code, dataset, key, incoming, reason, batch_id, created_at)"
                              " VALUES (?,?,?,?,?,?,?)", (CODE, "m5", f"{DAY} 10:05", "{}", "closed_conflict",
                                                          "b", facts.now_iso()))
            self.conn.execute("INSERT INTO day_checks(code, trade_date, status, reason, detail, checked_at)"
                              " VALUES (?,?,?,?,?,?)", (CODE, DAY, "pending_review", "close_mismatch",
                                                        json.dumps({"minute": {}}), facts.now_iso()))

    def test_final_status_and_window_reviews_ignore_old_m5_evidence(self):
        c = self.make(FullFake())
        self.assertEqual(c._final_status(CODE, DAY), "complete")
        self.assertEqual(c._window_reviews(CODE, DAY, DAY, DAY), 0)

    def test_views_ignore_old_m5_evidence(self):
        v = views.read_view(self.conn, CODE, "m30", adjust="raw", now=at(17, 40))
        self.assertEqual(v.coverage["data_status"]["phase"], "final")
        self.assertNotIn("pending_review", {d["kind"] for d in v.incomplete_days})

    def test_same_day_m15_check_keeps_the_old_m5_check_for_rollback(self):
        # 第三阶段第二轮复审阻断 3：同一天 m5 核对不一致，m15 核对也不一致 → 修好 m15 → 回退 m5 时旧证据还在
        legacy = lambda: self.conn.execute("SELECT * FROM day_checks WHERE code=? AND trade_date=?",  # noqa: E731
                                           (CODE, DAY)).fetchone()
        before = tuple(legacy())
        with mock.patch.object(facts.admission, "minute_day_mismatch", return_value="close_mismatch"):
            self.assertTrue(facts.reconcile_day(self.conn, CODE, DAY, market="CN", fact_freq="m15"))
        self.assertEqual(tuple(legacy()), before)
        self.assertEqual(self.make(FullFake())._final_status(CODE, DAY), "reconcile_mismatch")
        self.assertIsNone(facts.reconcile_day(self.conn, CODE, DAY, market="CN", fact_freq="m15"))
        self.assertEqual(tuple(legacy()), before, "m15 核对通过只清 m15 的记录")
        self.assertEqual(self.make(FullFake())._final_status(CODE, DAY), "complete")

    def test_new_mismatch_is_kept_per_fact_freq_and_blocks_m15(self):
        nxt = "2026-09-29"
        facts.commit_day_rows(self.conn, [RawDayRow(CODE, nxt, 12, 12, 12, 12, 1, "lot", 1, "CNY", 10, 0, "final",
                                                    new_batch_id())],
                              market="CN", kind="stock", item="day_history", source="mairui", binding_gen=1, today=nxt)
        facts.commit_minute_rows(self.conn, minute_rows(CODE, nxt, M15, 10), market="CN", kind="stock",
                                 item="minute_history", fact_freq="m15", source="mairui", binding_gen=1, today=nxt)
        self.assertTrue(facts.reconcile_day(self.conn, CODE, nxt, market="CN", fact_freq="m15"))
        self.assertEqual(self.conn.execute("SELECT fact_freq FROM minute_day_checks WHERE code=? AND trade_date=?",
                                           (CODE, nxt)).fetchone()[0], "m15")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM day_checks WHERE code=? AND trade_date=?",
                                            (CODE, nxt)).fetchone(), "m15 核对不写旧表")
        self.assertEqual(self.make(FullFake())._final_status(CODE, nxt), "reconcile_mismatch")

    def test_day_only_ignores_minute_reconciliation(self):
        # 第三阶段第二轮复审可后置 7（补写守卫，行为已由阻断 3 的修法带出）：系统依赖只定稿日线，没有分钟事实，
        # 任何粒度的分钟日线核对都不挡它的日线完成
        with mock.patch.object(facts.admission, "minute_day_mismatch", return_value="close_mismatch"):
            facts.reconcile_day(self.conn, CODE, DAY, market="CN", fact_freq="m15")
        self.assertEqual(facts.day_unsettled(self.conn, CODE, DAY, "m15"), "reconcile_mismatch")
        self.assertIsNone(facts.day_unsettled(self.conn, CODE, DAY, None))
        self.assertEqual(self.make(FullFake())._final_status(CODE, DAY, day_only=True), "complete")


class PlanSwitchTests(Base):
    def test_legacy_minute_plan_does_not_block_m15_backfill(self):
        self.known_calendar()
        with facts.write_txn(self.conn):                   # m5 时代的旧键：分钟回填已规划
            facts.set_setting(self.conn, f"minute_planned:{CODE}", DAY)
            self.conn.execute("DELETE FROM settings WHERE key=?", (collector.plan_key(CODE, "minute"),))
        provider = Recorder()
        c = self.make(provider)
        self.clock.t = at(20, 0).timestamp()
        c.history_tick(at(20, 0))
        self.assertTrue(any(g["dataset"] == "m15" for g in facts.open_gaps(self.conn, CODE))
                        or any(call[0] == "minute" and call[2] == "m15" for call in provider.calls))


class GenerationTests(Base):
    ITEMS = [(kind, item) for kind in ("stock", "index") for item in (FetchItem.MINUTE_HISTORY, FetchItem.MINUTE_LIVE)]

    def gens(self, market="CN"):
        return {(kind, item.value): facts.binding_gen(self.conn, market, kind, item.value)
                for kind, item in self.ITEMS} if market == "CN" else \
            {item.value: facts.binding_gen(self.conn, "HK", "stock", item.value)
             for item in (FetchItem.MINUTE_HISTORY, FetchItem.MINUTE_LIVE)}

    def test_switch_bumps_cn_minute_generations_once_and_rejects_late_old_batches(self):
        before, hk_before = self.gens(), self.gens("HK")
        c = self.make(FullFake())
        c.sync_minute_fact_freq()
        after = self.gens()
        self.assertEqual(after, {k: v + 1 for k, v in before.items()})
        self.assertEqual(self.gens("HK"), hk_before)       # 港股仍是 m30，不动
        c.sync_minute_fact_freq()
        self.collector_restart = self.make(FullFake())
        self.collector_restart.sync_minute_fact_freq()
        self.assertEqual(self.gens(), after)                # 幂等：重复启动不再推进
        with self.assertRaises(facts.StaleBinding):   # 切换前领到代次的迟到批次拒写
            facts.commit_minute_rows(self.conn, minute_rows(CODE, DAY, M15, 10), market="CN", kind="stock",
                                     item="minute_history", fact_freq="m15", source="mairui",
                                     binding_gen=before[("stock", "minute_history")], today=DAY)

    def test_switch_keeps_the_active_source(self):
        bindings.switch(self.conn, "CN", "stock", FetchItem.MINUTE_LIVE, "pytdx", reason="test")
        self.make(FullFake()).sync_minute_fact_freq()
        self.assertEqual(bindings.active(self.conn, "CN", "stock", FetchItem.MINUTE_LIVE)[0], "pytdx")

    def test_view_token_changes_with_the_switch(self):
        self.known_calendar()
        self.commit_today()
        before = views.read_view(self.conn, CODE, "day", adjust="raw", now=at(17, 40)).token
        self.make(FullFake()).sync_minute_fact_freq()
        self.assertNotEqual(views.read_view(self.conn, CODE, "day", adjust="raw", now=at(17, 40)).token, before)


class Transport:
    def __init__(self, name):
        rec = json.loads((FIX / name).read_text())
        self.response, self.calls = (rec["status"], rec["body"]), []

    def __call__(self, path, params, timeout):
        self.calls.append((path, params))
        return self.response


class ProviderTests(unittest.TestCase):
    NOW = datetime(2026, 9, 29, 22, 0)

    def test_mairui_history_uses_native_15_minute_endpoints(self):
        for code, name, path in (("sh600036", "m15-600036-20260929.json", "/hsstock/history/600036.SH/15/n/{L}"),
                                 ("sh000001", "idx-m15-000001-20260929.json", "/hsindex/history/000001.SH/15/{L}")):
            t = Transport(name)
            rows = mairui.MairuiProvider(transport=t, licence="x").minute_history(
                code, "m15", "2026-09-29 09:30", "2026-09-29 15:00", now=self.NOW)
            self.assertEqual(t.calls[0][0], path)
            self.assertEqual([r.slot_end[11:] for r in rows], list(M15))
            self.assertEqual({r.state for r in rows}, {"closed"})

    def test_mairui_latest_uses_native_15_minute_endpoint(self):
        t = Transport("latest-600036-m15-lt2.json")
        rows = mairui.MairuiProvider(transport=t, licence="x").minute_live("sh600036", "m15", now=self.NOW)
        self.assertEqual(t.calls[0], ("/hsstock/latest/600036.SH/15/n/{L}", {"lt": 2}))
        self.assertEqual([r.slot_end[11:] for r in rows], ["14:45", "15:00"])
        self.assertEqual({r.state for r in rows}, {"forming"})

    def test_mairui_keeps_m5_for_rollback(self):
        t = Transport("m5-600036-20240927.json")
        mairui.MairuiProvider(transport=t, licence="x").minute_history(
            "sh600036", "m5", "2024-09-27 09:30", "2024-09-27 15:00", now=self.NOW)
        self.assertEqual(t.calls[0][0], "/hsstock/history/600036.SH/5/n/{L}")

    def test_pytdx_cold_serves_m15_with_its_category(self):
        calls = []

        def query(kind, category, market, num, offset, count):
            calls.append(category)
            return []
        pytdx_raw.PytdxRawProvider(query=query).minute_history("sh000001", "m15", "2026-09-29 09:30",
                                                               "2026-09-29 15:00", now=self.NOW)
        self.assertEqual(set(calls), {pytdx_raw.CATEGORY["m15"]})


if __name__ == "__main__":
    unittest.main()
