"""一致视图：前复权与原始价、周期派生、令牌、来源（spec §6.1–§6.3；计划 B 门面契约）。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import bindings, calendar, facts, sessions, views
from chanapp.engine.kline.rows import CalendarRow, FetchItem, RawDayRow, RawMinuteRow

KW = dict(market="CN", kind="stock", binding_gen=1)
FACT = bindings.binding("CN", "stock", FetchItem.MINUTE_HISTORY).minute_fact_freq   # A 股现行分钟事实粒度
DAYS = ["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"]
NOW = datetime(2026, 9, 28, 10, 0)


def day(date, c, pc, prov="final", code="sh600926"):
    o = h = l = c
    if prov == "preopen":
        o = h = l = c = None
    return RawDayRow(code, date, o, h, l, c, None if prov == "preopen" else 100.0, "lot", 1.0,
                     "CNY", pc, 0, prov, "b")


def minute(slot, c, state="closed", code="sh600926"):
    return RawMinuteRow(code, slot[:10], slot, c, c, c, c, 10.0, "lot", 1.0, state, "traded", "b")


class ViewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)
        with facts.write_txn(self.conn):
            calendar.store_rows(self.conn, [CalendarRow("CN", d, True) for d in DAYS], source="t")
        # 09-25 收盘 20.0；09-28 除权参考价 16.77
        self.commit_day([day("2026-09-23", 19.8, 19.7), day("2026-09-24", 19.9, 19.8),
                         day("2026-09-25", 20.0, 19.9)])

    def commit_day(self, rows, item="day_history", source="mairui", **kw):
        return facts.commit_day_rows(self.conn, rows, item=item, source=source,
                                     today="2026-09-28", **{**KW, **kw})

    def commit_min(self, rows, item="minute_live", source="mairui"):
        return facts.commit_minute_rows(self.conn, rows, item=item, fact_freq=FACT, source=source,
                                        today="2026-09-28", **KW)

    def add_today_minutes(self):
        self.commit_min([minute("2026-09-28 09:45", 16.9), minute("2026-09-28 10:00", 17.0, state="forming")])

    def test_today_unconfirmed_hides_today_in_qfq_but_not_raw(self):
        self.add_today_minutes()
        qfq = views.read_view(self.conn, "sh600926", "day", adjust="qfq", now=NOW)
        raw = views.read_view(self.conn, "sh600926", "day", adjust="raw", now=NOW)
        self.assertEqual(qfq.bars[-1]["dt"], "2026-09-25")
        self.assertEqual(qfq.coverage["qfq_through"], "2026-09-25")
        self.assertIn("today_unconfirmed", [n["code"] for n in qfq.notices])
        self.assertEqual(raw.bars[-1]["dt"], "2026-09-28")
        self.assertEqual(raw.bars[-1]["volume"], 2000)   # 手 → 股
        self.assertTrue(raw.bars[-1]["forming"])

    def test_preopen_confirms_today_and_rescales_history(self):
        self.add_today_minutes()
        self.commit_day([day("2026-09-28", None, 16.77, prov="preopen")], item="preopen_ref")
        view = views.read_view(self.conn, "sh600926", "day", adjust="qfq", now=NOW)
        self.assertEqual(view.bars[-1]["dt"], "2026-09-28")
        self.assertAlmostEqual(view.bars[-2]["close"], 16.77)
        self.assertNotIn("today_unconfirmed", [n["code"] for n in view.notices])

    def test_today_served_once_and_final_day_row_wins_over_minutes(self):
        # 迁自旧派生读「今日只出一根」：当日既有分钟又有定稿日线时，不得重复出今日 bar，且以日线事实为准
        self.add_today_minutes()
        raw = views.read_view(self.conn, "sh600926", "day", adjust="raw", now=NOW)
        self.assertEqual([b["dt"] for b in raw.bars].count("2026-09-28"), 1)
        self.assertTrue(raw.bars[-1]["forming"])
        self.commit_day([day("2026-09-28", 17.3, 16.77)])
        raw = views.read_view(self.conn, "sh600926", "day", adjust="raw", now=NOW)
        self.assertEqual([b["dt"] for b in raw.bars].count("2026-09-28"), 1)
        self.assertEqual((raw.bars[-1]["close"], raw.bars[-1]["forming"]), (17.3, False))

    def test_minute_periods_aggregate_from_the_minute_fact(self):
        self.commit_min([minute("2026-09-25 09:45", 10.0), minute("2026-09-25 10:30", 10.5),
                         minute("2026-09-25 13:15", 10.2)], item="minute_history")
        view = views.read_view(self.conn, "sh600926", "m60", adjust="raw", now=NOW)
        self.assertEqual([b["dt"] for b in view.bars], ["2026-09-25 10:30", "2026-09-25 14:00"])
        self.assertEqual(view.bars[0]["volume"], 2000)

    def test_qfq_minutes_follow_day_factor(self):
        self.commit_min([minute("2026-09-25 09:45", 20.0)], item="minute_history")
        self.commit_day([day("2026-09-28", None, 16.77, prov="preopen")], item="preopen_ref")
        view = views.read_view(self.conn, "sh600926", FACT, adjust="qfq", now=NOW)
        self.assertAlmostEqual(view.bars[0]["close"], 16.77)

    def test_qfq_minutes_on_exdate_are_not_scaled(self):
        # 迁自旧复权测试「除权日当天的分钟 bar 不乘」：分钟按所属交易日取因子
        self.commit_min([minute("2026-09-25 15:00", 20.0)], item="minute_history")
        self.add_today_minutes()
        self.commit_day([day("2026-09-28", None, 16.77, prov="preopen")], item="preopen_ref")
        view = views.read_view(self.conn, "sh600926", FACT, adjust="qfq", now=NOW)
        closes = {b["dt"]: b["close"] for b in view.bars}
        self.assertAlmostEqual(closes["2026-09-25 15:00"], 16.77)
        self.assertEqual(closes["2026-09-28 09:45"], 16.9)

    def test_token_ignores_forming_update_but_follows_pc_only_change(self):
        self.add_today_minutes()
        t0 = views.read_view(self.conn, "sh600926", "m30", now=NOW).token
        self.commit_min([minute("2026-09-28 10:00", 17.1, state="forming")])
        self.assertEqual(views.read_view(self.conn, "sh600926", "m30", now=NOW).token, t0)
        # 只到一条当日前收（无任何价格变化）也必须换令牌：它改变了整段前复权
        self.commit_day([day("2026-09-28", None, 16.77, prov="preopen")], item="preopen_ref")
        self.assertNotEqual(views.read_view(self.conn, "sh600926", "m30", now=NOW).token, t0)

    def test_token_follows_calendar_only_change(self):
        # 事实与代次都不变，只改日历（前复权相邻性与周线归属读取日历）：旧分页必须失效
        before = views.read_view(self.conn, "sh600926", "day", adjust="qfq", now=NOW).token
        with facts.write_txn(self.conn):
            calendar.store_rows(self.conn, [CalendarRow("CN", "2026-09-24", False)], source="t2")
        self.assertNotEqual(views.read_view(self.conn, "sh600926", "day", adjust="qfq", now=NOW).token, before)

    def test_token_changes_on_binding_switch_and_run_identity(self):
        t0 = views.read_view(self.conn, "sh600926", "day", now=NOW).token
        bindings.switch(self.conn, "CN", "stock", "day_history", "baostock", reason="drill")
        t1 = views.read_view(self.conn, "sh600926", "day", now=NOW).token
        facts.new_run_identity(self.conn)
        t2 = views.read_view(self.conn, "sh600926", "day", now=NOW).token
        self.assertEqual(len({t0, t1, t2}), 3)

    def test_index_is_always_raw(self):
        self.commit_day([day("2026-09-25", 3888.37, 3890.0, code="sh000001")], kind="index")
        view = views.read_view(self.conn, "sh000001", "day", adjust="qfq", now=NOW)
        self.assertEqual((view.adjust, view.adjust_label), ("raw", "不复权"))
        # coverage 另带 data_status（状态栏，目标 2026-09-29 第三阶段），这里只看前复权三项
        self.assertEqual({k: view.coverage[k] for k in ("qfq_from", "qfq_through", "stop_reason")},
                         {"qfq_from": None, "qfq_through": None, "stop_reason": None})

    def test_source_and_degraded_follow_served_batches(self):
        # 最新 bar 来自主源、较早 bar 来自冷备：source 取最新 bar 的批次，degraded 看全页
        self.commit_min([minute("2026-09-24 15:00", 10.0)], item="minute_history", source="pytdx")
        self.commit_min([minute("2026-09-25 09:45", 10.1)], item="minute_history")
        view = views.read_view(self.conn, "sh600926", FACT, adjust="raw", now=NOW)
        self.assertEqual((view.source, view.degraded), ("mairui", True))
        latest_only = views.read_view(self.conn, "sh600926", FACT, adjust="raw", now=NOW, limit=1)
        self.assertEqual((latest_only.source, latest_only.degraded), ("mairui", False))

    def test_finer_than_fact_period_is_unsupported(self):
        view = views.read_view(self.conn, "hk00700", "m5", adjust="raw", now=NOW)
        self.assertIsNone(view)   # 无任何港股事实
        facts.commit_day_rows(self.conn, [RawDayRow("hk00700", "2026-09-25", 1, 1, 1, 1, 1, "share", 1,
                                                    "HKD", 1, 0, "final", "b")],
                              market="HK", kind="stock", item="day_history", source="longbridge",
                              binding_gen=1, today="2026-09-28")
        view = views.read_view(self.conn, "hk00700", "m5", adjust="raw", now=NOW)
        self.assertEqual((view.bars, [n["code"] for n in view.notices]), ([], ["unsupported"]))


if __name__ == "__main__":
    unittest.main()


class ViewContractTests(ViewTests):
    """A2：周线、新鲜度、提示、不完整日、分页、行情（spec §6.2–§6.5）。"""

    def test_week_view_uses_same_anchor_and_ignores_preopen(self):
        self.commit_day([day("2026-09-28", None, 16.77, prov="preopen")], item="preopen_ref")
        week = views.read_view(self.conn, "sh600926", "week", adjust="qfq", now=NOW)
        # 09-21 周（09-23..09-25）一根；preopen 行不是价格 bar，09-28 周无 bar
        self.assertEqual([b["dt"] for b in week.bars], ["2026-09-25"])
        self.assertAlmostEqual(week.bars[0]["close"], 16.77)   # 先换到同一锚点再聚合
        self.assertEqual(week.bars[0]["volume"], 30000)

    def test_week_pagination_cuts_whole_weeks(self):
        page = views.read_view(self.conn, "sh600926", "week", adjust="raw", before="2026-09-24", now=NOW)
        self.assertEqual(page.bars, [])                          # 09-24 所在周整周排除
        self.assertFalse(page.has_more)

    def test_day_pagination_and_has_more(self):
        page = views.read_view(self.conn, "sh600926", "day", adjust="raw", before="2026-09-25", limit=1, now=NOW)
        self.assertEqual(([b["dt"] for b in page.bars], page.has_more, page.oldest_dt),
                         (["2026-09-24"], True, "2026-09-24"))

    def test_minute_pagination_excludes_anchor_bucket(self):
        self.commit_min([minute(f"2026-09-25 {t}", 10.0) for t in ("09:45", "10:00", "10:15", "10:30")],
                        item="minute_history")
        page = views.read_view(self.conn, "sh600926", "m60", adjust="raw", before="2026-09-25 10:30", now=NOW)
        self.assertEqual(page.bars, [])

    def test_stale_in_session_by_last_commit(self):
        self.add_today_minutes()
        self.conn.execute("UPDATE series_state SET last_commit_at='2026-09-28T10:00:00'"
                          " WHERE code='sh600926' AND dataset='" + FACT + "'")
        fresh = views.read_view(self.conn, "sh600926", "m30", adjust="raw", now=datetime(2026, 9, 28, 10, 2))
        stale = views.read_view(self.conn, "sh600926", "m30", adjust="raw", now=datetime(2026, 9, 28, 10, 30))
        self.assertEqual((fresh.stale, stale.stale, stale.stale_age_s), (False, True, 1800))

    def test_stale_after_finalize_deadline_without_final_day(self):
        late = datetime(2026, 9, 28, 21, 15)                # 有意改写（所有者 2026-09-30）：A 股截止 18:30 → 21:00
        self.assertTrue(views.read_view(self.conn, "sh600926", "day", adjust="raw", now=late).stale)
        self.commit_day([day("2026-09-28", 17.0, 16.77)])
        self.assertFalse(views.read_view(self.conn, "sh600926", "day", adjust="raw", now=late).stale)

    def test_cn_finalize_deadline_is_21_00(self):
        # A 股首个定稿时点 20:00（所有者 2026-09-30），未定稿判 stale 从 21:00 起；此前不判
        self.assertFalse(views.read_view(self.conn, "sh600926", "day", adjust="raw",
                                         now=datetime(2026, 9, 28, 20, 59)).stale)
        self.assertTrue(views.read_view(self.conn, "sh600926", "day", adjust="raw",
                                        now=datetime(2026, 9, 28, 21, 0)).stale)

    def test_not_stale_between_close_and_deadline_and_on_weekend(self):
        self.assertFalse(views.read_view(self.conn, "sh600926", "day", adjust="raw",
                                         now=datetime(2026, 9, 28, 16, 0)).stale)
        # 周六：最近一个过去交易日（09-25）已有 final 日线
        self.assertFalse(views.read_view(self.conn, "sh600926", "day", adjust="raw",
                                         now=datetime(2026, 9, 26, 12, 0)).stale)

    def test_quarantined_final_day_does_not_count_as_finalized(self):
        late = datetime(2026, 9, 28, 21, 15)                # 有意改写（所有者 2026-09-30）：A 股截止 18:30 → 21:00
        self.commit_day([day("2026-09-28", 17.0, 16.77)])
        with facts.write_txn(self.conn):
            facts.quarantine(self.conn, "sh600926", "day", "2026-09-28", "review_conflict")
        self.assertTrue(views.read_view(self.conn, "sh600926", "day", adjust="raw", now=late).stale)

    def test_in_session_day_view_reports_commit_time_of_the_dataset_judged_for_stale(self):
        # 盘中 day/week 的 stale 看分钟数据集，fetch_time 必须来自同一数据集，否则外部监控误报
        self.add_today_minutes()
        self.conn.execute("UPDATE series_state SET last_commit_at='2026-09-25T15:30:00'"
                          " WHERE code='sh600926' AND dataset='day'")
        self.conn.execute("UPDATE series_state SET last_commit_at='2026-09-28T09:59:00'"
                          " WHERE code='sh600926' AND dataset='" + FACT + "'")
        view = views.read_view(self.conn, "sh600926", "day", adjust="raw", now=NOW)
        self.assertEqual(view.last_commit_at, "2026-09-28T09:59:00")
        self.assertFalse(view.stale)

    def test_hk_in_session_stale_uses_weekday_when_calendar_unknown(self):
        # 港股当日日历在定稿前通常未知：采集器按工作日采集，stale 也要按工作日判盘中停更
        facts.commit_minute_rows(self.conn, [RawMinuteRow("hk00700", "2026-09-28", "2026-09-28 10:00", 1, 1, 1, 1,
                                                          10.0, "share", 1.0, "closed", "traded", "b")],
                                 market="HK", kind="stock", item="minute_live", fact_freq="m30",
                                 source="longbridge", binding_gen=1, today="2026-09-28")
        self.conn.execute("UPDATE series_state SET last_commit_at='2026-09-28T10:00:00'"
                          " WHERE code='hk00700' AND dataset='m30'")
        view = views.read_view(self.conn, "hk00700", "m30", adjust="raw", now=datetime(2026, 9, 28, 11, 0))
        self.assertTrue(view.stale)
        self.assertEqual(view.stale_age_s, 3600)

    def test_backfill_notices_and_incomplete_days(self):
        self.commit_min([minute("2026-09-24 09:45", 10.0)], item="minute_history")
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, "sh600926", FACT, "2026-08-01 09:30", "2026-08-31 15:00", "backfill")
        view = views.read_view(self.conn, "sh600926", "m30", adjust="raw", now=NOW)
        self.assertEqual({n["code"] for n in view.notices}, {"backfill_pending", "structure_short"})
        self.assertEqual(view.incomplete_days, [{"date": "2026-09-24", "kind": "incomplete", "slots": 1},
                                                {"date": "2026-09-25", "kind": "incomplete", "slots": 0}])

    def full_day(self, d, c=10.0, **kw):
        from chanapp.engine.kline import sessions
        self.commit_min([minute(f"{d} {t}", c) for t in sessions.slots("CN", FACT)], item="minute_history", **kw)

    def test_incomplete_days_follow_readable_minutes(self):
        # 完整性统计要和实际可读的集合一致：隔离槽算缺、整日无分钟算缺、槽位冲突待核验要列出
        self.full_day("2026-09-23")
        self.full_day("2026-09-25")
        with facts.write_txn(self.conn):
            facts.quarantine(self.conn, "sh600926", FACT, "2026-09-23 10:00", "review_conflict")
        self.commit_min([minute("2026-09-25 10:00", 10.3)], item="minute_history")   # 与已收盘槽冲突
        view = views.read_view(self.conn, "sh600926", FACT, adjust="raw", limit=200, now=NOW)
        self.assertEqual(view.incomplete_days, [
            {"date": "2026-09-23", "kind": "incomplete", "slots": len(sessions.slots("CN", FACT)) - 1},
            {"date": "2026-09-24", "kind": "incomplete", "slots": 0},
            {"date": "2026-09-25", "kind": "pending_review"},
        ])

    def test_known_gap_alone_does_not_keep_backfill_notice(self):
        # 供应商永久缺的数据转 known_gap 后不再补取，「更早分钟历史加载中」不能一直挂着
        self.full_day("2026-09-25")
        with facts.write_txn(self.conn):
            facts.record_gap(self.conn, "sh600926", FACT, "2026-08-01 09:30", "2026-08-31 15:00", "backfill")
            self.conn.execute("UPDATE coverage_gaps SET reason='known_gap'")
        view = views.read_view(self.conn, "sh600926", "m30", adjust="raw", now=NOW)
        self.assertNotIn("backfill_pending", {n["code"] for n in view.notices})

    def test_short_first_page_carries_structure_short_even_without_gaps(self):
        # 窗口未取够（首页不足默认窗口且没有更早数据）：标「结构输入尚未补齐」；分页残页不标
        self.full_day("2026-09-25")
        view = views.read_view(self.conn, "sh600926", "m30", adjust="raw", now=NOW)
        self.assertIn("structure_short", [n["code"] for n in view.notices])
        self.assertFalse(view.has_more)
        page = views.read_view(self.conn, "sh600926", "m30", adjust="raw", before="2026-09-25 14:00",
                               limit=10, now=NOW)
        self.assertTrue(page.bars)
        self.assertNotIn("structure_short", [n["code"] for n in page.notices])

    def test_half_day_counts_only_session_slots(self):
        # 日历行带会话（半日市）时只要求会话内槽位，与采集器的覆盖判定同一口径
        from chanapp.engine.kline import sessions
        with facts.write_txn(self.conn):
            self.conn.execute("UPDATE calendar SET sessions='[[\"09:30\", \"11:30\"]]' WHERE date='2026-09-25'")
        morning = [t for t in sessions.slots("CN", FACT) if t <= "11:30"]
        self.commit_min([minute(f"2026-09-25 {t}", 10.0) for t in morning], item="minute_history")
        self.full_day("2026-09-24")
        view = views.read_view(self.conn, "sh600926", FACT, adjust="raw", limit=200, now=NOW)
        self.assertNotIn("2026-09-25", [d["date"] for d in view.incomplete_days])

    def test_past_forming_day_listed_incomplete(self):
        # 定稿失败：过去交易日仍是 forming（槽位齐全也要列出）
        self.conn.execute("INSERT INTO minute_bars VALUES ('sh600926','" + FACT + "','2026-09-25 09:45',1,'2026-09-25',"
                          "1,1,1,1,1,'lot',1,'forming','traded','mairui',1,'b','ingest','t')")
        view = views.read_view(self.conn, "sh600926", FACT, adjust="raw", now=NOW)
        self.assertIn({"date": "2026-09-25", "kind": "forming", "slots": 1}, view.incomplete_days)

    def test_quote_shows_prev_close_before_first_minute(self):
        q = views.quote(self.conn, "sh600926", now=datetime(2026, 9, 28, 9, 20))
        self.assertEqual((q["price"], q["price_label"], q["pct"]), (20.0, "昨收", None))

    def test_quote_needs_same_day_pc(self):
        self.add_today_minutes()
        q = views.quote(self.conn, "sh600926", now=NOW)
        self.assertEqual((q["price"], q["price_label"], q["price_time"], q["pct"]),
                         (17.0, "最新", "2026-09-28 10:00", None))
        self.commit_day([day("2026-09-28", None, 16.77, prov="preopen")], item="preopen_ref")
        q = views.quote(self.conn, "sh600926", now=NOW)
        self.assertAlmostEqual(q["pct"], round((17.0 / 16.77 - 1) * 100, 2))

    def test_quote_skips_suspended_placeholder(self):
        self.conn.execute("INSERT INTO minute_bars VALUES ('sh600926','" + FACT + "','2026-09-28 09:45',1,'2026-09-28',"
                          "20,20,20,20,0,'lot',0,'forming','suspended','mairui',1,'b','ingest','t')")
        q = views.quote(self.conn, "sh600926", now=NOW)
        self.assertEqual(q["price_label"], "昨收")

    def test_quote_none_without_facts(self):
        self.assertIsNone(views.quote(self.conn, "sz000002", now=NOW))

    def quote_at(self, code, price, pc, *, name=None, kind="stock"):
        """code 的前一交易日收盘 20.0、当日参考前收 pc（None 表示未确认）、当日最新分钟收盘 price。"""
        kw = dict(kind=kind)
        if code != "sh600926":
            self.commit_day([day("2026-09-25", 20.0, 19.9, code=code)], **kw)
        if pc is not None:
            self.commit_day([day("2026-09-28", None, pc, prov="preopen", code=code)], item="preopen_ref", **kw)
        facts.commit_minute_rows(self.conn, [minute("2026-09-28 09:45", price, code=code)], item="minute_live",
                                 fact_freq=FACT, source="mairui", today="2026-09-28", **{**KW, **kw})
        return views.quote(self.conn, code, now=NOW)

    def test_quote_limit_up_by_board(self):
        # 涨停价 = 前收 × (1 + 比例) 按分四舍五入（交易所规则，十进制半进位）；主板 10%（含主板 ST：沪深交易所
        # 已把主板风险警示股调为 10%），科创板/创业板 20%
        cases = [("sh600926", 18.45, 16.77, True), ("sh600926", 18.44, 16.77, False),
                 ("sh600001", 18.45, 16.77, True), ("sh600001", 17.61, 16.77, False),
                 ("sh600002", 2.26, 2.05, True), ("sh600002", 2.25, 2.05, False),     # 2.255 → 2.26（二进制 round 得 2.25）
                 ("sh688001", 20.12, 16.77, True), ("sh688001", 20.11, 16.77, False),
                 ("sz300001", 12.0, 10.0, True), ("sz300001", 11.0, 10.0, False),
                 ("sz000001", 11.0, 10.0, True)]
        for code, price, pc, expected in cases:
            with self.subTest(code=code, price=price):
                self.setUp()
                q = self.quote_at(code, price, pc)
                self.assertEqual((q["price"], q["price_label"], q["limit_up"]), (price, "最新", expected))

    def test_quote_limit_up_needs_confirmed_pc_and_latest_price(self):
        q = self.quote_at("sh600926", 30.0, None)            # 参考前收未确认：不判涨停
        self.assertEqual((q["pct"], q["limit_up"]), (None, False))
        self.setUp()
        self.commit_day([day("2026-09-28", None, 16.77, prov="preopen")], item="preopen_ref")
        q = views.quote(self.conn, "sh600926", now=datetime(2026, 9, 28, 9, 20))
        self.assertEqual((q["price"], q["price_label"], q["pct"], q["limit_up"]),
                         (20.0, "昨收", None, False))                 # 盘前昨收高于涨停价也不是涨停

    def test_index_quote_uses_previous_close_and_never_limit_up(self):
        # 指数没有盘前参考价取数项：参考前收即上一交易日收盘（与港股同），不判涨停
        q = self.quote_at("sh000001", 22.0, None, kind="index")
        self.assertEqual((q["pc"], q["pct"], q["limit_up"]), (20.0, 10.0, False))

    def test_bundle_reads_one_snapshot(self):
        self.add_today_minutes()
        bundle = views.read_bundle(self.conn, "sh600926", ["day", "m60", "m30"], adjust="raw", now=NOW)
        self.assertEqual(set(bundle), {"day", "m60", "m30"})
        self.assertEqual(len({v.token for v in bundle.values()}), 3)
